"""
alert_service/main.py
──────────────────────
The core alert pipeline service.
- Consumes all Redis Streams (traffic, face, crowd, emergency)
- Deduplicates alerts within configured time windows
- Groups related alerts into incident cards
- Persists to PostgreSQL
- Pushes live alerts to dashboard clients via WebSocket
- Provides REST endpoints for alert management
- Writes every action to immutable audit log
"""

import asyncio
import json
import logging
import os
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

import asyncpg
import redis.asyncio as aioredis
from fastapi import (FastAPI, WebSocket, WebSocketDisconnect,
                     HTTPException, Request)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from auth import (
    AUTH_MODE, Principal, allowed_camera_ids, cors_kwargs, ensure_auth_schema,
    install_auth, principal_of, redact_alert, require_perm, upsert_officer,
    verify_token,
)
from engine import (
    ensure_schema,
    ingest_stream_message,
    refresh_rules,
    serialize_rule,
)

log = logging.getLogger("alert-service")

_INGEST_MS: deque[float] = deque(maxlen=200)
_WS_MS: deque[float] = deque(maxlen=200)


def _percentile(values, p: float):
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((p / 100.0) * (len(ordered) - 1)))))
    return round(ordered[idx], 1)


def _latency_block(window) -> dict:
    vals = list(window)
    return {"p50": _percentile(vals, 50), "p95": _percentile(vals, 95), "n": len(vals)}


logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

DATABASE_URL       = os.getenv("DATABASE_URL", "postgresql://policeai:policeai_dev_secret@localhost:5432/policeai")
REDIS_URL          = os.getenv("REDIS_URL",    "redis://localhost:6379")
JWT_SECRET         = os.getenv("JWT_SECRET",   "dev_jwt_secret_change_in_prod")
DEDUP_WINDOW       = int(os.getenv("DEDUP_WINDOW_SECONDS", "30"))
CONSUMER_GROUP     = "alert-service"
DEAD_STREAM        = "alerts:dead"
PEL_MIN_IDLE_MS    = int(os.getenv("PEL_MIN_IDLE_MS", "60000"))
PEL_MAX_DELIVERIES = int(os.getenv("PEL_MAX_DELIVERIES", "5"))
PEL_RECLAIM_S      = float(os.getenv("PEL_RECLAIM_SECONDS", "5"))

# All alert streams to consume
ALERT_STREAMS = [
    "alerts:traffic",
    "alerts:face",
    "alerts:crowd",
    "alerts:emergency",
]

# ── App lifespan ──────────────────────────────
pool: asyncpg.Pool | None = None
redis_client: aioredis.Redis | None = None
ws_manager: "ConnectionManager" = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    global pool, redis_client, ws_manager

    pool         = await asyncpg.create_pool(DATABASE_URL, min_size=3, max_size=20)
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    ws_manager   = ConnectionManager()

    # Ensure consumer groups exist in Redis Streams
    for stream in ALERT_STREAMS + [DEAD_STREAM]:
        try:
            await redis_client.xgroup_create(
                stream, CONSUMER_GROUP, id="0", mkstream=True
            )
        except Exception:
            pass  # Group already exists

    await ensure_schema(pool)
    await ensure_auth_schema(pool)
    await refresh_rules(pool, force=True)
    log.info("Alert service ready")
    consumer_task = asyncio.create_task(consume_alerts())
    yield
    consumer_task.cancel()
    await pool.close()
    await redis_client.aclose()


app = FastAPI(title="VMS Intelligence – Alert Service", lifespan=lifespan)
app.add_middleware(CORSMiddleware, **cors_kwargs())
install_auth(app)


# ── WebSocket connection manager ──────────────
class ConnectionManager:
    """Manages all active WebSocket connections from dashboard clients."""

    def __init__(self):
        self.connections: dict[str, tuple[WebSocket, Principal, list[str] | None]] = {}

    async def connect(self, ws: WebSocket, session_id: str, principal: Principal,
                      allowed: list[str] | None):
        await ws.accept()
        self.connections[session_id] = (ws, principal, allowed)
        log.info("WS connected: %s role=%s (total=%s)",
                 session_id, principal.role, len(self.connections))

    def disconnect(self, session_id: str):
        self.connections.pop(session_id, None)
        log.info("WS disconnected: %s (total=%s)", session_id, len(self.connections))

    async def broadcast(self, message: dict):
        cam = message.get("camera_id")
        dead = []
        for sid, (ws, principal, allowed) in list(self.connections.items()):
            if allowed is not None and cam not in allowed:
                continue
            try:
                await ws.send_json(redact_alert(message, principal))
            except Exception:
                dead.append(sid)
        for sid in dead:
            self.connections.pop(sid, None)

    async def send_to(self, session_id: str, message: dict):
        entry = self.connections.get(session_id)
        if not entry:
            return
        ws, principal, _allowed = entry
        await ws.send_json(redact_alert(message, principal))


def _user(request: Request) -> Principal:
    return principal_of(request)


async def _camera_acl(request: Request) -> list[str] | None:
    return await allowed_camera_ids(pool, _user(request))


def _camera_allowed(allowed: list[str] | None, camera_id: str | None) -> bool:
    if allowed is None:
        return True
    return bool(camera_id) and camera_id in allowed


# ── Redis Streams consumer ────────────────────
async def _delivery_count(stream: str, msg_id: str) -> int:
    try:
        pending = await redis_client.xpending_range(
            stream, CONSUMER_GROUP, min=msg_id, max=msg_id, count=1
        )
        if pending:
            return int(pending[0].get("times_delivered") or 1)
    except Exception:
        pass
    return 1


async def dead_letter(stream: str, msg_id: str, data: dict, error: str) -> None:
    payload = {k: v for k, v in (data or {}).items() if k != "snapshot_b64"}
    try:
        await pool.execute(
            """INSERT INTO alert_dead_letters (source_stream, source_id, payload, error)
               VALUES ($1,$2,$3::jsonb,$4)""",
            stream, msg_id, json.dumps(payload), error[:2000],
        )
    except Exception as exc:
        log.error("dead_letter_db_failed: %s", exc)
    try:
        await redis_client.xadd(
            DEAD_STREAM,
            {
                "source_stream": stream,
                "source_id": msg_id,
                "error": error[:500],
                "payload": json.dumps(payload)[:8000],
            },
            maxlen=5000,
        )
    except Exception as exc:
        log.error("dead_letter_redis_failed: %s", exc)
    try:
        await redis_client.xack(stream, CONSUMER_GROUP, msg_id)
    except Exception:
        pass
    log.error("moved_to_dead_letter stream=%s id=%s err=%s", stream, msg_id, error)


async def handle_stream_message(stream: str, msg_id: str, data: dict) -> None:
    try:
        t0 = time.perf_counter()
        payloads = await ingest_stream_message(pool, redis_client, stream, msg_id, data)
        _INGEST_MS.append((time.perf_counter() - t0) * 1000)
        t1 = time.perf_counter()
        for payload in payloads:
            try:
                await ws_manager.broadcast(payload)
            except Exception as exc:
                log.warning("ws_broadcast_failed: %s", exc)
            try:
                await pool.execute(
                    """INSERT INTO audit_log (action, resource_type, resource_id, details)
                       VALUES ('alert_created', 'alert', $1::uuid, $2)""",
                    payload["alert_id"],
                    json.dumps({
                        "alert_type": payload.get("alert_type"),
                        "camera_id": payload.get("camera_id"),
                        "severity": payload.get("severity"),
                        "rule_id": payload.get("rule_id"),
                    }),
                )
            except Exception as exc:
                log.debug("audit_create_skip: %s", exc)
            log.info(
                "Alert %s [%s] cam=%s conf=%.2f severity=L%s",
                payload.get("alert_id"), payload.get("alert_type"),
                payload.get("camera_id"), float(payload.get("confidence") or 0),
                payload.get("severity"),
            )
        if payloads:
            _WS_MS.append((time.perf_counter() - t1) * 1000)
        await redis_client.xack(stream, CONSUMER_GROUP, msg_id)
    except Exception as e:
        log.exception("Failed to process alert %s: %s", msg_id, e)
        if await _delivery_count(stream, msg_id) >= PEL_MAX_DELIVERIES:
            await dead_letter(stream, msg_id, data, str(e))


async def reclaim_pending(consumer_name: str) -> None:
    for stream in ALERT_STREAMS:
        try:
            result = await redis_client.xautoclaim(
                name=stream,
                groupname=CONSUMER_GROUP,
                consumername=consumer_name,
                min_idle_time=PEL_MIN_IDLE_MS,
                start_id="0-0",
                count=20,
            )
        except Exception as exc:
            log.debug("xautoclaim %s: %s", stream, exc)
            continue
        messages = []
        if result and len(result) >= 2:
            messages = result[1] or []
        for msg_id, data in messages:
            await handle_stream_message(stream, msg_id, data)


async def consume_alerts():
    """
    Continuously read from all alert Redis Streams.
    ACK on success. Failed messages stay in the PEL, are reclaimed after
    PEL_MIN_IDLE_MS, and move to alerts:dead after PEL_MAX_DELIVERIES.
    """
    consumer_name = f"alert-svc-{uuid.uuid4().hex[:8]}"
    log.info("Redis consumer started: %s", consumer_name)

    stream_ids = {s: ">" for s in ALERT_STREAMS}
    last_reclaim = 0.0

    while True:
        try:
            now = time.monotonic()
            if now - last_reclaim >= PEL_RECLAIM_S:
                await reclaim_pending(consumer_name)
                last_reclaim = now

            entries = await redis_client.xreadgroup(
                groupname=CONSUMER_GROUP,
                consumername=consumer_name,
                streams=stream_ids,
                count=50,
                block=1000,
            )
            if not entries:
                continue

            for stream_name, messages in entries:
                for msg_id, data in messages:
                    await handle_stream_message(stream_name, msg_id, data)

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error("Consumer loop error: %s", e)
            await asyncio.sleep(2)


# ── REST endpoints ────────────────────────────
@app.get("/")
async def root():
    return {
        "service": "alert-service",
        "version": "1.0",
        "endpoints": {
            "alerts": "GET /alerts",
            "events": "GET /events",
            "rules": "GET /rules",
            "incidents": "GET /incidents",
            "dead_letters": "GET /dead-letters",
            "websocket": "WS /ws/{session_id}",
            "health": "GET /health",
        },
    }


@app.get("/health")
async def health():
    dead = 0
    rules_n = 0
    try:
        dead = await pool.fetchval("SELECT COUNT(*) FROM alert_dead_letters") or 0
        rules_n = await pool.fetchval("SELECT COUNT(*) FROM ai_rules WHERE enabled=TRUE") or 0
    except Exception:
        pass
    return {
        "status": "ok",
        "service": "alert-service",
        "ws_connections": len(ws_manager.connections),
        "rules_enabled": rules_n,
        "dead_letters": dead,
        "perf": {
            "ingest_latency_ms": _latency_block(_INGEST_MS),
            "ws_broadcast_ms": _latency_block(_WS_MS),
            "ws_connections": len(ws_manager.connections),
        },
    }


@app.get("/auth/verify")
async def auth_verify(request: Request):
    """Used by other services (and optionally nginx) to check a bearer token."""
    p = _user(request)
    return {"ok": True, "sub": p.sub, "role": p.role, "username": p.username}


@app.get("/me")
async def me(request: Request):
    p = _user(request)
    officer_id = await upsert_officer(pool, p)
    sites = await allowed_camera_ids(pool, p)
    return {
        "sub": p.sub,
        "username": p.username,
        "name": p.name,
        "role": p.role,
        "officer_id": officer_id,
        "tenant_id": p.tenant_id,
        "camera_ids": sites,
        "auth_mode": AUTH_MODE,
    }


def _serialize_alert(row: asyncpg.Record, *, include_snapshot: bool = False) -> dict:
    r = dict(row)
    alert_id = str(r.pop("id", ""))
    meta = r.get("object_metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            meta = {}
    ts = r.get("created_at") or r.get("raw_frame_ts")
    has_snapshot = bool(r.get("has_snapshot") or r.get("snapshot_b64") or r.get("snapshot_path"))
    snapshot = r.get("snapshot_b64", "") if include_snapshot else ""
    assigned = r.get("assigned_to")
    event_id = r.get("event_id")
    rule_id = r.get("rule_id")
    ack_at = r.get("acknowledged_at")
    res_at = r.get("resolved_at")
    out = {
        "alert_id":     alert_id,
        "alert_type":   r.get("alert_type"),
        "camera_id":    r.get("camera_id"),
        "confidence":   float(r.get("confidence") or 0),
        "severity":     int(r.get("severity") or 2),
        "status":       r.get("status", "pending"),
        "location":     r.get("location_name") or "",
        "has_snapshot": has_snapshot,
        "snapshot_b64": snapshot or "",
        "metadata":     meta or {},
        "timestamp":    ts.isoformat() if hasattr(ts, "isoformat") else str(ts or ""),
        "incident_id":  str(r["incident_id"]) if r.get("incident_id") else None,
        "assigned_to":  assigned,
        "event_id":     str(event_id) if event_id else None,
        "rule_id":      str(rule_id) if rule_id else None,
        "acknowledged_at": ack_at.isoformat() if hasattr(ack_at, "isoformat") else ack_at,
        "resolved_at":  res_at.isoformat() if hasattr(res_at, "isoformat") else res_at,
    }
    if include_snapshot and r.get("notes") is not None:
        out["notes"] = r["notes"]
    return out


ALERT_LIST_COLUMNS = """
    id, alert_type, camera_id, confidence, severity, snapshot_path,
    object_metadata, location_name, latitude, longitude, status,
    incident_id, raw_frame_ts, created_at, assigned_to, event_id, rule_id,
    acknowledged_at, resolved_at,
    (snapshot_b64 IS NOT NULL) AS has_snapshot
"""


@app.get("/alerts")
async def list_alerts(request: Request, limit: int = 50, status: str | None = None):
    """Paginated alert list. Snapshots omitted — use GET /alerts/{id}."""
    require_perm(_user(request), "alerts:read")
    limit = max(1, min(int(limit), 500))
    allowed = await _camera_acl(request)
    clauses: list[str] = []
    args: list[Any] = []
    if status:
        args.append(status)
        clauses.append(f"status=${len(args)}")
    if allowed is not None:
        args.append(allowed)
        clauses.append(f"camera_id = ANY(${len(args)}::text[])")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    args.append(limit)
    rows = await pool.fetch(
        f"SELECT {ALERT_LIST_COLUMNS} FROM alerts {where} "
        f"ORDER BY created_at DESC LIMIT ${len(args)}",
        *args,
    )
    p = _user(request)
    return [redact_alert(_serialize_alert(r), p) for r in rows]


@app.get("/alerts/{alert_id}")
async def get_alert(request: Request, alert_id: str):
    """Single alert including snapshot_b64 for the detail pane."""
    require_perm(_user(request), "alerts:read")
    try:
        uuid.UUID(alert_id)
    except ValueError:
        raise HTTPException(400, "Invalid alert id")
    row = await pool.fetchrow(
        "SELECT * FROM alerts WHERE id=$1::uuid", alert_id
    )
    if not row:
        raise HTTPException(404, "Alert not found")
    allowed = await _camera_acl(request)
    if not _camera_allowed(allowed, row["camera_id"]):
        raise HTTPException(404, "Alert not found")
    p = _user(request)
    include_snap = p.can("evidence:read")
    payload = redact_alert(_serialize_alert(row, include_snapshot=include_snap), p)
    notes = await pool.fetch(
        "SELECT id, officer_id, note, created_at FROM alert_notes "
        "WHERE alert_id=$1::uuid ORDER BY created_at ASC",
        alert_id,
    )
    payload["notes"] = [
        {
            "id": str(n["id"]),
            "officer_id": n["officer_id"],
            "note": n["note"],
            "created_at": n["created_at"].isoformat() if n["created_at"] else None,
        }
        for n in notes
    ]
    return payload


@app.get("/incidents")
async def list_incidents(limit: int = 20):
    rows = await pool.fetch(
        "SELECT * FROM incidents ORDER BY created_at DESC LIMIT $1", limit
    )
    return [dict(r) for r in rows]


class AlertAction(BaseModel):
    action: str
    notes: str | None = None
    officer_id: str | None = None
    assigned_to: str | None = None


class AlertNoteBody(BaseModel):
    note: str = Field(..., min_length=1, max_length=4000)
    officer_id: str | None = None


class RuleBody(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    priority: int | None = None
    event_type: str | None = None
    object_type: str | None = None
    zone_type: str | None = None
    camera_id: str | None = None
    min_confidence: float | None = None
    match_json: dict | None = None
    count_threshold: int | None = Field(default=None, ge=1)
    window_seconds: int | None = Field(default=None, ge=0)
    distinct_tracks: bool | None = None
    severity: int | None = Field(default=None, ge=1, le=4)
    alert_type: str | None = None
    cooldown_seconds: int | None = Field(default=None, ge=0)


class RuleCreate(RuleBody):
    name: str
    severity: int = Field(..., ge=1, le=4)


class ForensicQuery(BaseModel):
    query: str
    limit: int = 20


@app.post("/forensics/search")
async def forensic_search(body: ForensicQuery):
    """Stub — returns empty until forensic index is wired."""
    return {"results": [], "query": body.query, "note": "Forensic search not yet indexed"}


# ── Analytics endpoints ───────────────────────────────────────────────────────

@app.get("/analytics/summary")
async def analytics_summary():
    """
    Today's alert summary:
    - total alerts today
    - breakdown by alert_type
    - breakdown by severity
    - top cameras by alert volume (last 24 h)
    - pending count
    """
    type_rows = await pool.fetch("""
        SELECT alert_type, COUNT(*) AS count
        FROM   alerts
        WHERE  created_at >= CURRENT_DATE
        GROUP BY alert_type
        ORDER BY count DESC
    """)
    sev_rows = await pool.fetch("""
        SELECT severity, COUNT(*) AS count
        FROM   alerts
        WHERE  created_at >= CURRENT_DATE
        GROUP BY severity
        ORDER BY severity DESC
    """)
    cam_rows = await pool.fetch("""
        SELECT camera_id,
               COUNT(*) AS total,
               SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending
        FROM   alerts
        WHERE  created_at >= NOW() - INTERVAL '24 hours'
        GROUP BY camera_id
        ORDER BY total DESC
        LIMIT 10
    """)
    pending_count = await pool.fetchval(
        "SELECT COUNT(*) FROM alerts WHERE status = 'pending'"
    )
    total_today = sum(r["count"] for r in type_rows)
    return {
        "total_today":   total_today,
        "pending":       pending_count or 0,
        "by_type":       [{"type": r["alert_type"], "count": r["count"]} for r in type_rows],
        "by_severity":   [{"severity": r["severity"], "count": r["count"]} for r in sev_rows],
        "by_camera":     [
            {"camera_id": r["camera_id"], "total": r["total"], "pending": r["pending"] or 0}
            for r in cam_rows
        ],
    }


@app.get("/analytics/violations")
async def analytics_violations(hours: int = 24, camera_id: str | None = None):
    """
    Violations grouped by hour + alert_type for the last N hours.
    Used for the hourly trend line chart on the analytics page.
    """
    hours = max(1, min(int(hours), 168))
    rows = await pool.fetch(
        """
        SELECT
            DATE_TRUNC('hour', created_at) AS hour,
            alert_type,
            COUNT(*) AS count
        FROM alerts
        WHERE created_at > NOW() - ($1::int * INTERVAL '1 hour')
          AND ($2::text IS NULL OR camera_id = $2)
        GROUP BY hour, alert_type
        ORDER BY hour ASC
        """,
        hours,
        camera_id,
    )
    return [
        {
            "hour":  r["hour"].isoformat(),
            "type":  r["alert_type"],
            "count": r["count"],
        }
        for r in rows
    ]


@app.get("/analytics/camera-stats")
async def analytics_camera_stats(days: int = 7):
    """Per-camera alert counts for the last N days — used for heatmap/bar chart."""
    days = max(1, min(int(days), 90))
    rows = await pool.fetch(
        """
        SELECT
            camera_id,
            alert_type,
            COUNT(*) AS count
        FROM alerts
        WHERE created_at > NOW() - ($1::int * INTERVAL '1 day')
        GROUP BY camera_id, alert_type
        ORDER BY camera_id, count DESC
        """,
        days,
    )
    return [{"camera_id": r["camera_id"], "type": r["alert_type"], "count": r["count"]}
            for r in rows]


# ── Incident detail ───────────────────────────────────────────────────────────

@app.get("/incidents/{incident_id}")
async def get_incident_detail(incident_id: str):
    """Full incident detail with linked alert list."""
    inc = await pool.fetchrow(
        "SELECT * FROM incidents WHERE id = $1::uuid", incident_id
    )
    if not inc:
        raise HTTPException(404, "Incident not found")

    alert_rows = await pool.fetch(
        f"""SELECT {ALERT_LIST_COLUMNS} FROM alerts WHERE incident_id = $1::uuid
           ORDER BY created_at DESC LIMIT 100""",
        incident_id,
    )
    inc_dict = dict(inc)
    inc_dict["id"] = str(inc_dict["id"])
    if inc_dict.get("assigned_to"):
        inc_dict["assigned_to"] = str(inc_dict["assigned_to"])
    for k, v in inc_dict.items():
        if hasattr(v, "isoformat"):
            inc_dict[k] = v.isoformat()

    return {
        **inc_dict,
        "alerts": [_serialize_alert(r) for r in alert_rows],
    }


@app.post("/incidents/{incident_id}/action")
async def update_incident(incident_id: str, body: AlertAction):
    """Officer action on an incident (assign, close, dispatch)."""
    valid = {"assigned", "dispatched", "closed"}
    if body.action not in valid:
        raise HTTPException(400, f"Invalid action. Must be one of: {valid}")
    await pool.execute(
        "UPDATE incidents SET status=$1 WHERE id=$2::uuid", body.action, incident_id
    )
    return {"incident_id": incident_id, "status": body.action}


# ── Evidence endpoint ─────────────────────────────────────────────────────────

@app.get("/evidence")
async def list_evidence(request: Request, limit: int = 50, camera_id: str | None = None, alert_type: str | None = None):
    """
    Alerts that have snapshot images attached — used by the Evidence page.
    Supports filtering by camera and/or alert type.
    """
    require_perm(_user(request), "evidence:read")
    clauses = ["snapshot_b64 IS NOT NULL"]
    params: list = []
    if camera_id:
        params.append(camera_id)
        clauses.append(f"camera_id = ${len(params)}")
    if alert_type:
        params.append(alert_type)
        clauses.append(f"alert_type = ${len(params)}")
    allowed = await _camera_acl(request)
    if allowed is not None:
        params.append(allowed)
        clauses.append(f"camera_id = ANY(${len(params)}::text[])")
    where = " AND ".join(clauses)
    params.append(limit)
    rows = await pool.fetch(
        f"SELECT * FROM alerts WHERE {where} ORDER BY created_at DESC LIMIT ${len(params)}",
        *params,
    )
    p = _user(request)
    return [redact_alert(_serialize_alert(r, include_snapshot=p.can("faces:view") or r["alert_type"] != "face_match"), p) for r in rows]


@app.get("/system/health")
async def system_health(request: Request):
    """Aggregate health: DB row counts + WebSocket connections."""
    require_perm(_user(request), "admin:read")
    alert_count = await pool.fetchval("SELECT COUNT(*) FROM alerts") or 0
    camera_count = await pool.fetchval("SELECT COUNT(*) FROM cameras WHERE active = TRUE") or 0
    incident_count = await pool.fetchval(
        "SELECT COUNT(*) FROM incidents WHERE status IN ('open','assigned')"
    ) or 0
    t_db = time.perf_counter()
    await pool.fetchval("SELECT 1")
    db_ms = round((time.perf_counter() - t_db) * 1000, 2)
    return {
        "status":           "ok",
        "ws_connections":   len(ws_manager.connections),
        "total_alerts":     alert_count,
        "active_cameras":   camera_count,
        "open_incidents":   incident_count,
        "perf": {
            "db_ping_ms": db_ms,
            "ingest_latency_ms": _latency_block(_INGEST_MS),
            "ws_broadcast_ms": _latency_block(_WS_MS),
        },
    }


ACTION_STATUS = {
    "acknowledge": "acknowledged",
    "accepted": "acknowledged",
    "assign": "assigned",
    "assigned": "assigned",
    "investigate": "investigating",
    "investigating": "investigating",
    "resolve": "resolved",
    "resolved": "resolved",
    "closed": "resolved",
    "reject": "rejected",
    "rejected": "rejected",
    "escalate": "escalated",
    "escalated": "escalated",
    "note": None,
}


def _parse_alert_uuid(alert_id: str) -> str:
    try:
        return str(uuid.UUID(alert_id))
    except ValueError as exc:
        raise HTTPException(400, "Invalid alert id") from exc


async def _audit_alert(alert_id: str, action: str, officer_id: str | None, details: dict) -> None:
    officer_uuid = None
    if officer_id:
        try:
            officer_uuid = uuid.UUID(officer_id)
        except ValueError:
            officer_uuid = None
    extra = dict(details)
    if officer_id and not officer_uuid:
        extra["officer_id"] = officer_id
    await pool.execute(
        """INSERT INTO audit_log (officer_id, action, resource_type, resource_id, details)
           VALUES ($1, $2, 'alert', $3, $4)""",
        officer_uuid,
        f"alert_{action}",
        uuid.UUID(alert_id),
        json.dumps(extra),
    )


@app.post("/alerts/{alert_id}/action")
async def update_alert(request: Request, alert_id: str, body: AlertAction):
    """
    Alert lifecycle: acknowledge, assign, investigate, resolve, note.
    Legacy accepted/rejected/escalated/closed remain valid aliases.
    Officer identity comes from the access token, not the request body.
    """
    require_perm(_user(request), "alerts:act")
    actor = await upsert_officer(pool, _user(request))
    officer_id = actor or body.officer_id
    alert_id = _parse_alert_uuid(alert_id)
    action = (body.action or "").strip().lower()
    if action not in ACTION_STATUS:
        raise HTTPException(400, f"Invalid action. Must be one of: {sorted(ACTION_STATUS)}")

    exists = await pool.fetchrow(
        "SELECT id, camera_id FROM alerts WHERE id=$1::uuid", alert_id
    )
    if not exists:
        raise HTTPException(404, "Alert not found")
    allowed = await _camera_acl(request)
    if not _camera_allowed(allowed, exists["camera_id"]):
        raise HTTPException(404, "Alert not found")

    new_status = ACTION_STATUS[action]
    assigned_to = body.assigned_to or (body.officer_id if action in ("assign", "assigned") else None)
    if action in ("assign", "assigned") and not assigned_to:
        raise HTTPException(400, "assign requires assigned_to (or officer_id)")

    if new_status:
        sets = ["status=$1"]
        args: list[Any] = [new_status]
        n = 2
        if new_status == "acknowledged":
            sets.append("acknowledged_at=COALESCE(acknowledged_at, NOW())")
        if new_status == "resolved":
            sets.append("resolved_at=COALESCE(resolved_at, NOW())")
        if new_status == "assigned" or assigned_to:
            sets.append(f"assigned_to=${n}")
            args.append(assigned_to)
            n += 1
        args.append(alert_id)
        await pool.execute(
            f"UPDATE alerts SET {', '.join(sets)} WHERE id=${n}::uuid",
            *args,
        )

    if body.notes and body.notes.strip():
        await pool.execute(
            """INSERT INTO alert_notes (alert_id, officer_id, note)
               VALUES ($1::uuid, $2, $3)""",
            alert_id, officer_id, body.notes.strip(),
        )

    await _audit_alert(
        alert_id, action, officer_id,
        {"notes": body.notes, "status": new_status, "assigned_to": assigned_to},
    )

    status_out = new_status
    if status_out is None:
        status_out = await pool.fetchval(
            "SELECT status FROM alerts WHERE id=$1::uuid", alert_id
        )

    await ws_manager.broadcast({
        "type":        "alert_updated",
        "alert_id":    alert_id,
        "status":      status_out,
        "notes":       body.notes,
        "assigned_to": assigned_to,
    })

    log.info("Alert %s → %s by %s", alert_id, action, officer_id)
    return {"alert_id": alert_id, "status": status_out, "assigned_to": assigned_to}


@app.post("/alerts/{alert_id}/notes")
async def add_alert_note(request: Request, alert_id: str, body: AlertNoteBody):
    require_perm(_user(request), "alerts:act")
    officer_id = await upsert_officer(pool, _user(request)) or body.officer_id
    alert_id = _parse_alert_uuid(alert_id)
    exists = await pool.fetchval("SELECT 1 FROM alerts WHERE id=$1::uuid", alert_id)
    if not exists:
        raise HTTPException(404, "Alert not found")
    row = await pool.fetchrow(
        """INSERT INTO alert_notes (alert_id, officer_id, note)
           VALUES ($1::uuid, $2, $3)
           RETURNING id, officer_id, note, created_at""",
        alert_id, officer_id, body.note.strip(),
    )
    await _audit_alert(alert_id, "note", officer_id, {"note": body.note})
    return {
        "id": str(row["id"]),
        "alert_id": alert_id,
        "officer_id": row["officer_id"],
        "note": row["note"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


@app.get("/events")
async def list_events(request: Request, limit: int = 50, camera_id: str | None = None, event_type: str | None = None):
    require_perm(_user(request), "alerts:read")
    limit = max(1, min(int(limit), 500))
    clauses = []
    args: list[Any] = []
    if camera_id:
        args.append(camera_id)
        clauses.append(f"camera_id=${len(args)}")
    if event_type:
        args.append(event_type)
        clauses.append(f"event_type=${len(args)}")
    allowed = await _camera_acl(request)
    if allowed is not None:
        args.append(allowed)
        clauses.append(f"camera_id = ANY(${len(args)}::text[])")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    args.append(limit)
    rows = await pool.fetch(
        f"""SELECT id, event_type, camera_id, object_type, track_id, zone_id, zone_type,
                   confidence, attributes, occurred_at, created_at
            FROM events {where}
            ORDER BY occurred_at DESC LIMIT ${len(args)}""",
        *args,
    )
    out = []
    for r in rows:
        item = dict(r)
        item["id"] = str(item["id"])
        if item.get("confidence") is not None:
            item["confidence"] = float(item["confidence"])
        for k, v in list(item.items()):
            if hasattr(v, "isoformat"):
                item[k] = v.isoformat()
        out.append(item)
    return out


@app.get("/rules")
async def list_rules(request: Request, include_disabled: bool = False):
    require_perm(_user(request), "rules:read")
    if include_disabled:
        rows = await pool.fetch("SELECT * FROM ai_rules ORDER BY priority DESC, name")
    else:
        rows = await pool.fetch(
            "SELECT * FROM ai_rules WHERE enabled=TRUE ORDER BY priority DESC, name"
        )
    return [serialize_rule(r) for r in rows]


@app.post("/rules", status_code=201)
async def create_rule(request: Request, body: RuleCreate):
    require_perm(_user(request), "rules:write")
    try:
        row = await pool.fetchrow(
            """INSERT INTO ai_rules
               (name, enabled, priority, event_type, object_type, zone_type, camera_id,
                min_confidence, match_json, count_threshold, window_seconds, distinct_tracks,
                severity, alert_type, cooldown_seconds)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11,$12,$13,$14,$15)
               RETURNING *""",
            body.name,
            True if body.enabled is None else body.enabled,
            body.priority or 0,
            body.event_type,
            body.object_type,
            body.zone_type,
            body.camera_id,
            body.min_confidence or 0,
            json.dumps(body.match_json) if body.match_json is not None else None,
            body.count_threshold or 1,
            body.window_seconds or 0,
            True if body.distinct_tracks is None else body.distinct_tracks,
            body.severity,
            body.alert_type or body.event_type,
            30 if body.cooldown_seconds is None else body.cooldown_seconds,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(409, "Rule name already exists") from exc
    await refresh_rules(pool, force=True)
    return serialize_rule(row)


@app.patch("/rules/{rule_id}")
async def update_rule(request: Request, rule_id: str, body: RuleBody):
    require_perm(_user(request), "rules:write")
    try:
        rid = str(uuid.UUID(rule_id))
    except ValueError as exc:
        raise HTTPException(400, "Invalid rule id") from exc
    row = await pool.fetchrow("SELECT * FROM ai_rules WHERE id=$1::uuid", rid)
    if not row:
        raise HTTPException(404, "Rule not found")
    fields = body.model_dump(exclude_unset=True)
    allowed = {
        "name", "enabled", "priority", "event_type", "object_type", "zone_type",
        "camera_id", "min_confidence", "match_json", "count_threshold",
        "window_seconds", "distinct_tracks", "severity", "alert_type",
        "cooldown_seconds",
    }
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return serialize_rule(row)
    sets = []
    args: list[Any] = []
    for key, val in fields.items():
        if key == "match_json":
            args.append(json.dumps(val) if val is not None else None)
            sets.append(f"match_json=${len(args)}::jsonb")
        else:
            args.append(val)
            sets.append(f"{key}=${len(args)}")
    sets.append("updated_at=NOW()")
    args.append(rid)
    updated = await pool.fetchrow(
        f"UPDATE ai_rules SET {', '.join(sets)} WHERE id=${len(args)}::uuid RETURNING *",
        *args,
    )
    await refresh_rules(pool, force=True)
    return serialize_rule(updated)


@app.delete("/rules/{rule_id}")
async def disable_rule(request: Request, rule_id: str):
    require_perm(_user(request), "rules:write")
    try:
        rid = str(uuid.UUID(rule_id))
    except ValueError as exc:
        raise HTTPException(400, "Invalid rule id") from exc
    result = await pool.execute(
        "UPDATE ai_rules SET enabled=FALSE, updated_at=NOW() WHERE id=$1::uuid AND enabled=TRUE",
        rid,
    )
    if result == "UPDATE 0":
        raise HTTPException(404, "Rule not found")
    await refresh_rules(pool, force=True)
    return {"id": rid, "status": "disabled"}


@app.get("/dead-letters")
async def list_dead_letters(request: Request, limit: int = 50):
    require_perm(_user(request), "admin:read")
    limit = max(1, min(int(limit), 200))
    rows = await pool.fetch(
        """SELECT id, source_stream, source_id, payload, error, created_at
           FROM alert_dead_letters ORDER BY created_at DESC LIMIT $1""",
        limit,
    )
    out = []
    for r in rows:
        item = dict(r)
        item["id"] = str(item["id"])
        if item.get("created_at"):
            item["created_at"] = item["created_at"].isoformat()
        out.append(item)
    return out


# ── WebSocket endpoint ────────────────────────
@app.websocket("/ws/{session_id}")
async def websocket_endpoint(ws: WebSocket, session_id: str, access_token: str | None = None):
    """
    Dashboard clients connect here.
    Token: ?access_token= (browser WebSocket cannot set Authorization).
    """
    token = (access_token or ws.query_params.get("access_token") or "").strip()
    try:
        if AUTH_MODE in ("off", "disabled", "false", "0"):
            principal = Principal(sub="local", username="local", name="Local operator", role="admin")
        else:
            principal = verify_token(token)
    except HTTPException:
        await ws.close(code=4401)
        return
    allowed = await allowed_camera_ids(pool, principal)
    await ws_manager.connect(ws, session_id, principal, allowed)
    try:
        clauses = ["status='pending'"]
        args: list[Any] = []
        if allowed is not None:
            args.append(allowed)
            clauses.append(f"camera_id = ANY(${len(args)}::text[])")
        where = " AND ".join(clauses)
        args.append(20)
        recent = await pool.fetch(
            f"SELECT {ALERT_LIST_COLUMNS} FROM alerts WHERE {where} "
            f"ORDER BY created_at DESC LIMIT ${len(args)}",
            *args,
        )
        for row in recent:
            payload = redact_alert(_serialize_alert(row), principal)
            payload["type"] = "alert_catchup"
            await ws.send_json(payload)

        while True:
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_text("pong")

    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(session_id)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8004, reload=True)
