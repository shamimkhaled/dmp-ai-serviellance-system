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
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

import asyncpg
import redis.asyncio as aioredis
from fastapi import (FastAPI, WebSocket, WebSocketDisconnect,
                     Depends, HTTPException, Header)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

log = logging.getLogger("alert-service")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

DATABASE_URL       = os.getenv("DATABASE_URL", "postgresql://policeai:policeai_dev_secret@localhost:5432/policeai")
REDIS_URL          = os.getenv("REDIS_URL",    "redis://localhost:6379")
JWT_SECRET         = os.getenv("JWT_SECRET",   "dev_jwt_secret_change_in_prod")
DEDUP_WINDOW       = int(os.getenv("DEDUP_WINDOW_SECONDS", "30"))

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
    for stream in ALERT_STREAMS:
        try:
            await redis_client.xgroup_create(
                stream, "alert-service", id="0", mkstream=True
            )
        except Exception:
            pass  # Group already exists

    # Idempotent migration: add snapshot_b64 column if it doesn't exist yet
    await pool.execute(
        "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS snapshot_b64 TEXT"
    )
    log.info("Alert service ready")
    consumer_task = asyncio.create_task(consume_alerts())
    yield
    consumer_task.cancel()
    await pool.close()
    await redis_client.aclose()


app = FastAPI(title="Police AI – Alert Service", lifespan=lifespan)
app.add_middleware(CORSMiddleware,
                   allow_origins=["*"],
                   allow_methods=["*"],
                   allow_headers=["*"])


# ── WebSocket connection manager ──────────────
class ConnectionManager:
    """Manages all active WebSocket connections from dashboard clients."""

    def __init__(self):
        self.connections: dict[str, WebSocket] = {}   # session_id → ws

    async def connect(self, ws: WebSocket, session_id: str):
        await ws.accept()
        self.connections[session_id] = ws
        log.info(f"WS connected: {session_id} (total={len(self.connections)})")

    def disconnect(self, session_id: str):
        self.connections.pop(session_id, None)
        log.info(f"WS disconnected: {session_id} (total={len(self.connections)})")

    async def broadcast(self, message: dict):
        """Push alert to all connected dashboard clients."""
        dead = []
        for sid, ws in self.connections.items():
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(sid)
        for sid in dead:
            self.connections.pop(sid, None)

    async def send_to(self, session_id: str, message: dict):
        ws = self.connections.get(session_id)
        if ws:
            await ws.send_json(message)


# ── Redis Streams consumer ────────────────────
async def consume_alerts():
    """
    Continuously read from all alert Redis Streams.
    Uses consumer groups for reliable delivery (ACK on success).
    """
    consumer_name = f"alert-svc-{uuid.uuid4().hex[:8]}"
    log.info(f"Redis consumer started: {consumer_name}")

    # Stream → last-processed message id
    stream_ids = {s: ">" for s in ALERT_STREAMS}

    while True:
        try:
            entries = await redis_client.xreadgroup(
                groupname="alert-service",
                consumername=consumer_name,
                streams=stream_ids,
                count=50,
                block=1000,   # ms — blocks 1s if no messages
            )
            if not entries:
                continue

            for stream_name, messages in entries:
                for msg_id, data in messages:
                    try:
                        await process_alert(stream_name, msg_id, data)
                        # ACK the message
                        await redis_client.xack(stream_name, "alert-service", msg_id)
                    except Exception as e:
                        log.error(f"Failed to process alert {msg_id}: {e}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"Consumer loop error: {e}")
            await asyncio.sleep(2)


# ── Alert processing ──────────────────────────
async def process_alert(stream: str, msg_id: str, data: dict):
    """
    For each incoming alert:
    1. Dedup check (same type + camera within window)
    2. Insert into alerts table
    3. Group into incident card
    4. Broadcast to dashboard via WebSocket
    5. Write audit log entry
    """
    alert_type   = data.get("alert_type", "unknown")
    camera_id    = data.get("camera_id", "")
    confidence   = float(data.get("confidence", 0))
    severity     = int(data.get("severity", 2))
    snapshot_b64 = data.get("snapshot_b64", "")
    metadata     = json.loads(data.get("object_metadata", "{}"))
    location     = data.get("location_name", "")
    lat          = float(data.get("latitude", 0) or 0)
    lng          = float(data.get("longitude", 0) or 0)
    frame_ts_str = data.get("frame_ts", datetime.now(timezone.utc).isoformat())
    frame_ts     = datetime.fromisoformat(frame_ts_str)

    # ── 1. Dedup check ────────────────────────
    # Key includes track_id so different vehicles of the same type are not
    # merged into one alert.  Falls back to alert_id when track_id is absent
    # (e.g. face-ai / crowd alerts that don't carry a track_id).
    track_id = metadata.get("track_id", data.get("alert_id", ""))
    dedup_key = f"dedup:{alert_type}:{camera_id}:{track_id}"
    if await redis_client.get(dedup_key):
        log.debug(f"Dedup suppressed: {dedup_key}")
        return
    await redis_client.setex(dedup_key, DEDUP_WINDOW, "1")

    # ── 2. Save alert snapshot ────────────────
    snapshot_path = None
    if snapshot_b64:
        snapshot_path = f"snapshots/{camera_id}/{datetime.now().strftime('%Y/%m/%d')}/{msg_id}.jpg"
        # In production: write bytes to MinIO / Node C at snapshot_path

    # ── 3. Insert alert into PostgreSQL ──────
    alert_id = await pool.fetchval(
        """INSERT INTO alerts
           (alert_type, camera_id, confidence, severity, snapshot_path, snapshot_b64,
            object_metadata, location_name, latitude, longitude, raw_frame_ts)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
           RETURNING id""",
        alert_type, camera_id, confidence, severity, snapshot_path, snapshot_b64 or None,
        json.dumps(metadata), location, lat or None, lng or None, frame_ts
    )

    # ── 4. Group into incident card ───────────
    incident_id = await get_or_create_incident(
        alert_type, camera_id, severity, location, lat, lng
    )
    await pool.execute(
        "UPDATE alerts SET incident_id=$1 WHERE id=$2", incident_id, alert_id
    )

    # ── 5. Build dashboard payload ─────────────
    alert_payload = {
        "type":         "new_alert",
        "alert_id":     str(alert_id),
        "incident_id":  str(incident_id),
        "alert_type":   alert_type,
        "camera_id":    camera_id,
        "confidence":   confidence,
        "severity":     severity,
        "location":     location,
        "latitude":     lat,
        "longitude":    lng,
        "snapshot_b64": snapshot_b64,
        "metadata":     metadata,
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "status":       "pending",
    }

    # ── 6. Broadcast to all dashboard clients ──
    await ws_manager.broadcast(alert_payload)

    # ── 7. Audit log ──────────────────────────
    await pool.execute(
        """INSERT INTO audit_log (action, resource_type, resource_id, details)
           VALUES ('alert_created', 'alert', $1, $2)""",
        alert_id, json.dumps({"alert_type": alert_type, "camera_id": camera_id,
                               "confidence": confidence})
    )

    log.info(f"Alert processed: [{alert_type}] cam={camera_id} "
             f"conf={confidence:.2f} severity=L{severity} → incident={incident_id}")


async def get_or_create_incident(alert_type: str, camera_id: str,
                                  severity: int, location: str,
                                  lat: float, lng: float) -> uuid.UUID:
    """
    Find an open incident for this camera+type within 5 minutes,
    or create a new one. This is the 'incident grouping' logic.
    """
    existing = await pool.fetchval(
        """SELECT id FROM incidents
           WHERE status IN ('open','assigned')
           AND location_name = $1
           AND $2 = ANY(alert_types)
           AND created_at > NOW() - INTERVAL '5 minutes'
           ORDER BY created_at DESC LIMIT 1""",
        location, alert_type
    )
    if existing:
        return existing

    # Create new incident card
    title = f"{alert_type.replace('_', ' ').title()} at {location or camera_id}"
    incident_id = await pool.fetchval(
        """INSERT INTO incidents
           (title, alert_types, severity, location_name, latitude, longitude)
           VALUES ($1, $2, $3, $4, $5, $6)
           RETURNING id""",
        title, [alert_type], severity, location,
        lat or None, lng or None
    )
    return incident_id


# ── REST endpoints ────────────────────────────
@app.get("/")
async def root():
    return {
        "service": "alert-service",
        "version": "1.0",
        "endpoints": {
            "alerts": "GET /alerts",
            "incidents": "GET /incidents",
            "websocket": "WS /ws/{session_id}",
            "health": "GET /health",
        },
    }


@app.get("/health")
async def health():
    return {"status": "ok", "service": "alert-service",
            "ws_connections": len(ws_manager.connections)}


def _serialize_alert(row: asyncpg.Record) -> dict:
    r = dict(row)
    alert_id = str(r.pop("id", ""))
    meta = r.get("object_metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            meta = {}
    ts = r.get("created_at") or r.get("raw_frame_ts")
    return {
        "alert_id":     alert_id,
        "alert_type":   r.get("alert_type"),
        "camera_id":    r.get("camera_id"),
        "confidence":   float(r.get("confidence") or 0),
        "severity":     int(r.get("severity") or 2),
        "status":       r.get("status", "pending"),
        "location":     r.get("location_name") or "",
        "snapshot_b64": r.get("snapshot_b64", ""),
        "metadata":     meta or {},
        "timestamp":    ts.isoformat() if hasattr(ts, "isoformat") else str(ts or ""),
        "incident_id":  str(r["incident_id"]) if r.get("incident_id") else None,
    }


@app.get("/alerts")
async def list_alerts(limit: int = 50, status: str | None = None):
    """Paginated alert list for dashboard."""
    if status:
        rows = await pool.fetch(
            "SELECT * FROM alerts WHERE status=$1 ORDER BY created_at DESC LIMIT $2",
            status, limit
        )
    else:
        rows = await pool.fetch(
            "SELECT * FROM alerts ORDER BY created_at DESC LIMIT $1", limit
        )
    return [_serialize_alert(r) for r in rows]


@app.get("/incidents")
async def list_incidents(limit: int = 20):
    rows = await pool.fetch(
        "SELECT * FROM incidents ORDER BY created_at DESC LIMIT $1", limit
    )
    return [dict(r) for r in rows]


class AlertAction(BaseModel):
    action: str       # accepted | rejected | escalated | closed
    notes: str | None = None
    officer_id: str | None = None


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
    cam_filter = f"AND camera_id = '{camera_id}'" if camera_id else ""
    rows = await pool.fetch(f"""
        SELECT
            DATE_TRUNC('hour', created_at) AS hour,
            alert_type,
            COUNT(*) AS count
        FROM alerts
        WHERE created_at > NOW() - INTERVAL '{hours} hours'
        {cam_filter}
        GROUP BY hour, alert_type
        ORDER BY hour ASC
    """)
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
    rows = await pool.fetch(f"""
        SELECT
            camera_id,
            alert_type,
            COUNT(*) AS count
        FROM alerts
        WHERE created_at > NOW() - INTERVAL '{days} days'
        GROUP BY camera_id, alert_type
        ORDER BY camera_id, count DESC
    """)
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
        """SELECT * FROM alerts WHERE incident_id = $1::uuid
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
async def list_evidence(limit: int = 50, camera_id: str | None = None, alert_type: str | None = None):
    """
    Alerts that have snapshot images attached — used by the Evidence page.
    Supports filtering by camera and/or alert type.
    """
    clauses = ["snapshot_b64 IS NOT NULL"]
    params: list = []
    if camera_id:
        params.append(camera_id)
        clauses.append(f"camera_id = ${len(params)}")
    if alert_type:
        params.append(alert_type)
        clauses.append(f"alert_type = ${len(params)}")
    where = " AND ".join(clauses)
    params.append(limit)
    rows = await pool.fetch(
        f"SELECT * FROM alerts WHERE {where} ORDER BY created_at DESC LIMIT ${len(params)}",
        *params,
    )
    return [_serialize_alert(r) for r in rows]


@app.get("/system/health")
async def system_health():
    """Aggregate health: DB row counts + WebSocket connections."""
    alert_count = await pool.fetchval("SELECT COUNT(*) FROM alerts") or 0
    camera_count = await pool.fetchval("SELECT COUNT(*) FROM cameras WHERE is_active") or 0
    incident_count = await pool.fetchval(
        "SELECT COUNT(*) FROM incidents WHERE status IN ('open','assigned')"
    ) or 0
    return {
        "status":           "ok",
        "ws_connections":   len(ws_manager.connections),
        "total_alerts":     alert_count,
        "active_cameras":   camera_count,
        "open_incidents":   incident_count,
    }


@app.post("/alerts/{alert_id}/action")
async def update_alert(alert_id: str, body: AlertAction):
    """
    Officer takes action on an alert.
    All actions written to immutable audit log.
    """
    valid_actions = {"accepted", "rejected", "escalated", "closed"}
    if body.action not in valid_actions:
        raise HTTPException(400, f"Invalid action. Must be one of: {valid_actions}")

    await pool.execute(
        "UPDATE alerts SET status=$1 WHERE id=$2::uuid", body.action, alert_id
    )

    # Immutable audit log (officer_id optional in dev)
    officer_uuid = None
    if body.officer_id:
        try:
            officer_uuid = uuid.UUID(body.officer_id)
        except ValueError:
            officer_uuid = None

    await pool.execute(
        """INSERT INTO audit_log (officer_id, action, resource_type, resource_id, details)
           VALUES ($1, $2, 'alert', $3, $4)""",
        officer_uuid,
        f"alert_{body.action}",
        uuid.UUID(alert_id),
        json.dumps({"notes": body.notes, "action": body.action})
    )

    # Broadcast status update to all dashboard clients
    await ws_manager.broadcast({
        "type":     "alert_updated",
        "alert_id": alert_id,
        "status":   body.action,
        "notes":    body.notes,
    })

    log.info(f"Alert {alert_id} → {body.action} by officer {body.officer_id}")
    return {"alert_id": alert_id, "status": body.action}


# ── WebSocket endpoint ────────────────────────
@app.websocket("/ws/{session_id}")
async def websocket_endpoint(ws: WebSocket, session_id: str):
    """
    Dashboard clients connect here.
    Receives real-time alert pushes and status updates.
    """
    await ws_manager.connect(ws, session_id)
    try:
        # Send last 20 pending alerts on connect (catch-up)
        recent = await pool.fetch(
            "SELECT * FROM alerts WHERE status='pending' "
            "ORDER BY created_at DESC LIMIT 20"
        )
        for row in recent:
            payload = _serialize_alert(row)
            payload["type"] = "alert_catchup"
            await ws.send_json(payload)

        # Keep connection alive — wait for client pings
        while True:
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_text("pong")

    except WebSocketDisconnect:
        ws_manager.disconnect(session_id)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8004, reload=True)
