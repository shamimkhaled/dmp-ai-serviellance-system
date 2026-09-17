"""
Event + rule engine for alert-service.

Workers still publish Redis stream messages. This module:
  1. Normalizes each message into `events` (idempotent on stream+id)
  2. Evaluates DB-backed `ai_rules` — severity comes only from rules
  3. Correlates (e.g. 3 distinct persons in a restricted zone within a window)
  4. Inserts `alerts` when a rule fires (after cooldown)
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import asyncpg

log = logging.getLogger("alert-service.engine")

RULE_REFRESH_S = 30

_SCHEMA_STMTS = [
    """
    CREATE TABLE IF NOT EXISTS events (
        id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        event_type      VARCHAR(50) NOT NULL,
        camera_id       VARCHAR(32) NOT NULL,
        object_type     VARCHAR(50),
        track_id        VARCHAR(50),
        zone_id         VARCHAR(64),
        zone_type       VARCHAR(30),
        confidence      DECIMAL(5,4),
        bbox            JSONB,
        attributes      JSONB,
        source_stream   VARCHAR(50) NOT NULL,
        source_id       VARCHAR(64) NOT NULL,
        occurred_at     TIMESTAMPTZ NOT NULL,
        created_at      TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (source_stream, source_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS events_camera_time_idx ON events(camera_id, occurred_at DESC)",
    "CREATE INDEX IF NOT EXISTS events_type_time_idx ON events(event_type, occurred_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS ai_rules (
        id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        name              VARCHAR(120) NOT NULL UNIQUE,
        enabled           BOOLEAN DEFAULT TRUE,
        priority          INTEGER DEFAULT 0,
        event_type        VARCHAR(50),
        object_type       VARCHAR(50),
        zone_type         VARCHAR(30),
        camera_id         VARCHAR(32),
        min_confidence    DECIMAL(5,4) DEFAULT 0,
        match_json        JSONB,
        count_threshold   INTEGER NOT NULL DEFAULT 1
                          CHECK (count_threshold >= 1),
        window_seconds    INTEGER NOT NULL DEFAULT 0
                          CHECK (window_seconds >= 0),
        distinct_tracks   BOOLEAN DEFAULT TRUE,
        severity          SMALLINT NOT NULL CHECK (severity BETWEEN 1 AND 4),
        alert_type        VARCHAR(50),
        cooldown_seconds  INTEGER NOT NULL DEFAULT 30
                          CHECK (cooldown_seconds >= 0),
        created_at        TIMESTAMPTZ DEFAULT NOW(),
        updated_at        TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ai_rules_enabled_idx ON ai_rules(enabled, priority DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ai_rules_name_uidx ON ai_rules (name)",
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS assigned_to VARCHAR(100)",
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS acknowledged_at TIMESTAMPTZ",
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ",
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS event_id UUID",
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS rule_id UUID",
    """
    CREATE TABLE IF NOT EXISTS alert_notes (
        id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        alert_id    UUID NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
        officer_id  VARCHAR(100),
        note        TEXT NOT NULL,
        created_at  TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS alert_notes_alert_idx ON alert_notes(alert_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS alert_dead_letters (
        id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        source_stream   VARCHAR(50) NOT NULL,
        source_id       VARCHAR(64) NOT NULL,
        payload         JSONB,
        error           TEXT,
        created_at      TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS alert_dead_letters_created_idx ON alert_dead_letters(created_at DESC)",
]


async def ensure_schema(pool: asyncpg.Pool) -> None:
    await pool.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS snapshot_b64 TEXT")
    for sql in _SCHEMA_STMTS:
        await pool.execute(sql)
    try:
        await pool.execute(
            """
            DO $$
            DECLARE r RECORD;
            BEGIN
              FOR r IN
                SELECT c.conname
                FROM pg_constraint c
                JOIN pg_class t ON c.conrelid = t.oid
                JOIN pg_namespace n ON n.oid = t.relnamespace
                WHERE t.relname = 'alerts' AND n.nspname = 'public'
                  AND c.contype = 'c'
                  AND pg_get_constraintdef(c.oid) ILIKE '%status%'
              LOOP
                EXECUTE format('ALTER TABLE alerts DROP CONSTRAINT %I', r.conname);
              END LOOP;
            END $$
            """
        )
        await pool.execute(
            """
            ALTER TABLE alerts ADD CONSTRAINT alerts_status_check
              CHECK (status IN (
                'pending','acknowledged','assigned','investigating','resolved',
                'accepted','rejected','escalated','closed'
              ))
            """
        )
    except Exception as exc:
        log.warning("alerts_status_constraint_skip: %s", exc)
    await pool.execute(
        """
        INSERT INTO ai_rules
          (name, enabled, priority, event_type, object_type, zone_type, min_confidence,
           count_threshold, window_seconds, distinct_tracks, severity, alert_type, cooldown_seconds)
        VALUES
          ('restricted_zone_3_persons', TRUE, 50, 'restricted_area', 'person', NULL, 0,
           3, 60, TRUE, 4, 'restricted_crowd', 60),
          ('face_match_critical', TRUE, 40, 'face_match', NULL, NULL, 0,
           1, 0, TRUE, 4, 'face_match', 30),
          ('red_light_violation', TRUE, 20, 'red_light_violation', NULL, NULL, 0,
           1, 0, TRUE, 3, 'red_light_violation', 30),
          ('stop_line_violation', TRUE, 20, 'stop_line_violation', NULL, NULL, 0,
           1, 0, TRUE, 2, 'stop_line_violation', 30),
          ('wrong_lane', TRUE, 20, 'wrong_lane', NULL, NULL, 0,
           1, 0, TRUE, 3, 'wrong_lane', 30),
          ('helmet_missing', TRUE, 20, 'helmet_missing', NULL, NULL, 0,
           1, 0, TRUE, 2, 'helmet_missing', 30),
          ('illegal_parking', TRUE, 20, 'illegal_parking', NULL, NULL, 0,
           1, 0, TRUE, 2, 'illegal_parking', 30),
          ('speeding', TRUE, 20, 'speeding', NULL, NULL, 0,
           1, 0, TRUE, 3, 'speeding', 30),
          ('restricted_area', TRUE, 20, 'restricted_area', NULL, NULL, 0,
           1, 0, TRUE, 3, 'restricted_area', 30),
          ('face_match', TRUE, 10, 'face_match', NULL, NULL, 0,
           1, 0, TRUE, 3, 'face_match', 30)
        ON CONFLICT (name) DO NOTHING
        """
    )
    await pool.execute(
        """
        UPDATE ai_rules SET match_json = '{"risk_category":"critical"}'::jsonb
         WHERE name = 'face_match_critical' AND match_json IS NULL
        """
    )
    log.info("event_schema_ready")


_rules: list[dict] = []
_rules_loaded_at: float = 0.0


def _parse_ts(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except ValueError:
        return datetime.now(timezone.utc)


def _parse_meta(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            out = json.loads(raw)
            return out if isinstance(out, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _json_contains(need: Any, have: Any) -> bool:
    if need is None:
        return True
    if isinstance(need, dict):
        if not isinstance(have, dict):
            return False
        return all(k in have and _json_contains(v, have[k]) for k, v in need.items())
    if isinstance(need, list):
        if not isinstance(have, list):
            return False
        return all(any(_json_contains(item, h) for h in have) for item in need)
    return need == have


def normalize_event(stream: str, msg_id: str, data: dict) -> dict:
    meta = _parse_meta(data.get("object_metadata"))
    event_type = (data.get("alert_type") or data.get("event_type") or "unknown").strip()
    object_type = (
        meta.get("object_type")
        or meta.get("vehicle_class")
        or meta.get("class_name")
        or None
    )
    if event_type == "face_match" and not object_type:
        object_type = "person"
    track_id = meta.get("track_id", data.get("alert_id", ""))
    track_id = "" if track_id is None else str(track_id)
    zone_id = meta.get("zone_id") or meta.get("violation_zone")
    zone_type = meta.get("zone_type")
    try:
        confidence = float(data.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    bbox = meta.get("bbox") or meta.get("bbox_xyxy")
    return {
        "event_type":    event_type,
        "camera_id":     str(data.get("camera_id") or ""),
        "object_type":   object_type,
        "track_id":      track_id,
        "zone_id":       str(zone_id) if zone_id else None,
        "zone_type":     str(zone_type) if zone_type else None,
        "confidence":    confidence,
        "bbox":          bbox,
        "attributes":    meta,
        "source_stream": stream,
        "source_id":     str(msg_id),
        "occurred_at":   _parse_ts(data.get("frame_ts")),
        "location_name": data.get("location_name") or "",
        "latitude":      float(data.get("latitude", 0) or 0),
        "longitude":     float(data.get("longitude", 0) or 0),
        "snapshot_b64":  data.get("snapshot_b64") or "",
        "snapshot_path": None,
    }


def rule_matches(rule: dict, event: dict) -> bool:
    if rule.get("event_type") and rule["event_type"] != event["event_type"]:
        return False
    if rule.get("object_type") and rule["object_type"] != event.get("object_type"):
        return False
    if rule.get("zone_type") and rule["zone_type"] != event.get("zone_type"):
        return False
    if rule.get("camera_id") and rule["camera_id"] != event["camera_id"]:
        return False
    min_conf = float(rule.get("min_confidence") or 0)
    if event["confidence"] < min_conf:
        return False
    match_json = rule.get("match_json")
    if match_json:
        if isinstance(match_json, str):
            try:
                match_json = json.loads(match_json)
            except json.JSONDecodeError:
                match_json = None
        if match_json and not _json_contains(match_json, event.get("attributes") or {}):
            return False
    return True


async def refresh_rules(pool: asyncpg.Pool, force: bool = False) -> list[dict]:
    global _rules, _rules_loaded_at
    now = time.monotonic()
    if not force and _rules and (now - _rules_loaded_at) < RULE_REFRESH_S:
        return _rules
    rows = await pool.fetch(
        "SELECT * FROM ai_rules WHERE enabled=TRUE ORDER BY priority DESC, name"
    )
    _rules = [dict(r) for r in rows]
    _rules_loaded_at = now
    return _rules


async def insert_event(pool: asyncpg.Pool, event: dict) -> str | None:
    """Insert event. Returns id, or None if this Redis message was already stored."""
    row = await pool.fetchrow(
        """INSERT INTO events
           (event_type, camera_id, object_type, track_id, zone_id, zone_type,
            confidence, bbox, attributes, source_stream, source_id, occurred_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10,$11,$12)
           ON CONFLICT (source_stream, source_id) DO NOTHING
           RETURNING id""",
        event["event_type"],
        event["camera_id"][:32],
        event["object_type"],
        (event["track_id"] or None),
        event["zone_id"],
        event["zone_type"],
        event["confidence"],
        json.dumps(event["bbox"]) if event["bbox"] is not None else None,
        json.dumps(event["attributes"] or {}),
        event["source_stream"],
        event["source_id"],
        event["occurred_at"],
    )
    return str(row["id"]) if row else None


async def _correlation_count(pool: asyncpg.Pool, rule: dict, event: dict) -> int:
    window = int(rule.get("window_seconds") or 0)
    if window <= 0:
        return 1
    distinct = bool(rule.get("distinct_tracks", True))
    agg = "COUNT(DISTINCT NULLIF(track_id, ''))" if distinct else "COUNT(*)"
    clauses = ["camera_id = $1", "occurred_at > NOW() - ($2::int * INTERVAL '1 second')"]
    args: list[Any] = [event["camera_id"], window]
    n = 3
    if rule.get("event_type"):
        clauses.append(f"event_type = ${n}")
        args.append(rule["event_type"])
        n += 1
    if rule.get("object_type"):
        clauses.append(f"object_type = ${n}")
        args.append(rule["object_type"])
        n += 1
    if rule.get("zone_type"):
        clauses.append(f"zone_type = ${n}")
        args.append(rule["zone_type"])
        n += 1
    sql = f"SELECT {agg} FROM events WHERE " + " AND ".join(clauses)
    val = await pool.fetchval(sql, *args)
    return int(val or 0)


async def _cooldown_ok(redis, rule: dict, event: dict) -> bool:
    ttl = int(rule.get("cooldown_seconds") or 0)
    if ttl <= 0:
        return True
    rid = str(rule["id"])
    if int(rule.get("count_threshold") or 1) > 1:
        key = f"rulefire:{rid}:{event['camera_id']}"
    else:
        key = f"rulefire:{rid}:{event['camera_id']}:{event.get('track_id') or '-'}"
    ok = await redis.set(key, "1", ex=ttl, nx=True)
    return bool(ok)


async def get_or_create_incident(
    pool: asyncpg.Pool,
    alert_type: str,
    camera_id: str,
    severity: int,
    location: str,
    lat: float,
    lng: float,
) -> Any:
    existing = await pool.fetchval(
        """SELECT id FROM incidents
           WHERE status IN ('open','assigned')
           AND location_name = $1
           AND $2 = ANY(alert_types)
           AND created_at > NOW() - INTERVAL '5 minutes'
           ORDER BY created_at DESC LIMIT 1""",
        location, alert_type,
    )
    if existing:
        return existing
    title = f"{alert_type.replace('_', ' ').title()} at {location or camera_id}"
    return await pool.fetchval(
        """INSERT INTO incidents
           (title, alert_types, severity, location_name, latitude, longitude)
           VALUES ($1, $2, $3, $4, $5, $6)
           RETURNING id""",
        title, [alert_type], severity, location,
        lat or None, lng or None,
    )


async def _fire_alert(
    pool: asyncpg.Pool,
    event: dict,
    event_id: str,
    rule: dict,
    extra_meta: dict | None = None,
) -> dict:
    alert_type = (rule.get("alert_type") or event["event_type"]).strip()
    severity = int(rule["severity"])
    meta = dict(event.get("attributes") or {})
    meta["rule_id"] = str(rule["id"])
    meta["rule_name"] = rule.get("name")
    meta["event_id"] = event_id
    if extra_meta:
        meta.update(extra_meta)
    snapshot_b64 = event.get("snapshot_b64") or ""
    snapshot_path = None
    if snapshot_b64:
        snapshot_path = (
            f"snapshots/{event['camera_id']}/"
            f"{datetime.now().strftime('%Y/%m/%d')}/{event['source_id']}.jpg"
        )
    alert_id = await pool.fetchval(
        """INSERT INTO alerts
           (alert_type, camera_id, confidence, severity, snapshot_path, snapshot_b64,
            object_metadata, location_name, latitude, longitude, raw_frame_ts,
            event_id, rule_id, status)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::uuid,$13::uuid,'pending')
           RETURNING id""",
        alert_type,
        event["camera_id"][:20] if event["camera_id"] else event["camera_id"],
        event["confidence"],
        severity,
        snapshot_path,
        snapshot_b64 or None,
        json.dumps(meta),
        event["location_name"],
        event["latitude"] or None,
        event["longitude"] or None,
        event["occurred_at"],
        event_id,
        str(rule["id"]),
    )
    incident_id = await get_or_create_incident(
        pool, alert_type, event["camera_id"], severity,
        event["location_name"], event["latitude"], event["longitude"],
    )
    await pool.execute(
        "UPDATE alerts SET incident_id=$1 WHERE id=$2", incident_id, alert_id
    )
    return {
        "type":         "new_alert",
        "alert_id":     str(alert_id),
        "incident_id":  str(incident_id),
        "alert_type":   alert_type,
        "camera_id":    event["camera_id"],
        "confidence":   event["confidence"],
        "severity":     severity,
        "location":     event["location_name"],
        "latitude":     event["latitude"],
        "longitude":    event["longitude"],
        "snapshot_b64": snapshot_b64,
        "metadata":     meta,
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "status":       "pending",
        "event_id":     event_id,
        "rule_id":      str(rule["id"]),
        "assigned_to":  None,
    }


async def evaluate_rules(
    pool: asyncpg.Pool,
    redis,
    event: dict,
    event_id: str,
) -> list[dict]:
    """Return dashboard payloads for alerts created by matching rules."""
    rules = await refresh_rules(pool)
    payloads: list[dict] = []
    fired_single = False
    for rule in rules:
        if not rule_matches(rule, event):
            continue
        threshold = int(rule.get("count_threshold") or 1)
        extra: dict = {}
        if threshold > 1:
            count = await _correlation_count(pool, rule, event)
            if count < threshold:
                continue
            extra = {"correlated": True, "match_count": count}
        else:
            if fired_single:
                continue
        if not await _cooldown_ok(redis, rule, event):
            continue
        payload = await _fire_alert(pool, event, event_id, rule, extra or None)
        payloads.append(payload)
        if threshold <= 1:
            fired_single = True
        log.info(
            "rule_fired name=%s alert_type=%s severity=%s camera=%s",
            rule.get("name"), payload["alert_type"], payload["severity"],
            event["camera_id"],
        )
    return payloads


async def ingest_stream_message(
    pool: asyncpg.Pool,
    redis,
    stream: str,
    msg_id: str,
    data: dict,
) -> list[dict]:
    """
    Persist event + maybe create alerts. Producer `severity` is ignored.
    Returns [] if the Redis message was already processed (idempotent).
    """
    event = normalize_event(stream, msg_id, data)
    event_id = await insert_event(pool, event)
    if not event_id:
        log.debug("duplicate stream message %s %s", stream, msg_id)
        return []
    return await evaluate_rules(pool, redis, event, event_id)


def serialize_rule(row: asyncpg.Record | dict) -> dict:
    r = dict(row)
    out = {}
    for k, v in r.items():
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif hasattr(v, "hex") and k in ("id",):
            out[k] = str(v)
        else:
            out[k] = v if not isinstance(v, (bytes,)) else v.decode()
        if k == "id":
            out[k] = str(r["id"])
    if r.get("match_json") is not None and not isinstance(out.get("match_json"), (dict, list)):
        out["match_json"] = r["match_json"]
    if out.get("min_confidence") is not None:
        out["min_confidence"] = float(out["min_confidence"])
    if out.get("severity") is not None:
        out["severity"] = int(out["severity"])
    return out
