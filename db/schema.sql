-- ─────────────────────────────────────────────
-- Police AI – Database Schema
-- PostgreSQL 16 + pgvector extension
-- ─────────────────────────────────────────────

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;

-- ── Officers / Users ──────────────────────────
CREATE TABLE officers (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    badge_no    VARCHAR(20) UNIQUE NOT NULL,
    full_name   VARCHAR(100) NOT NULL,
    rank        VARCHAR(50),
    role        VARCHAR(30) NOT NULL CHECK (role IN (
                    'operator','supervisor','station_commander','dig','igp','admin',
                    'investigator','viewer'
                )),
    station_id  UUID,
    keycloak_id VARCHAR(100) UNIQUE,
    tenant_id   UUID,
    active      BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sites (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id   UUID,
    name        VARCHAR(120) NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_site_access (
    keycloak_sub VARCHAR(100) NOT NULL,
    site_id      UUID NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    PRIMARY KEY (keycloak_sub, site_id)
);

-- ── Camera registry ───────────────────────────
CREATE TABLE cameras (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    camera_id     VARCHAR(20) UNIQUE NOT NULL,  -- e.g. "cam01"
    name          VARCHAR(100),
    rtsp_url      VARCHAR(255) NOT NULL,
    location_name VARCHAR(100),
    latitude      DECIMAL(10,7),
    longitude     DECIMAL(10,7),
    zone_type     VARCHAR(30) CHECK (zone_type IN (
                      'traffic','crowd','entry_exit','emergency','facility'
                  )),
    brand             VARCHAR(30) DEFAULT 'custom',
    connection_mode   VARCHAR(20) DEFAULT 'pull',
    host              VARCHAR(100),
    port              INT DEFAULT 554,
    username          VARCHAR(100),
    channel           INT DEFAULT 1,
    inference_fps INTEGER CHECK (inference_fps IS NULL OR (inference_fps >= 1 AND inference_fps <= 25)),
    thana_id      UUID,
    active        BOOLEAN DEFAULT TRUE,
    last_seen_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    tenant_id     UUID,
    site_id       UUID
);

-- ── Watchlist (face recognition) ─────────────
CREATE TABLE watchlist (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name          VARCHAR(100),
    risk_category VARCHAR(20) CHECK (risk_category IN ('critical','high','medium','low')),
    nid           VARCHAR(20),
    notes         TEXT,
    added_by      UUID REFERENCES officers(id),
    active        BOOLEAN DEFAULT TRUE,
    expires_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE watchlist_faces (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    watchlist_id  UUID NOT NULL REFERENCES watchlist(id) ON DELETE CASCADE,
    image_path    VARCHAR(255),
    embedding     vector(512),       -- ArcFace 512-dim embedding
    created_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX watchlist_face_embedding_idx
    ON watchlist_faces USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- ── Alerts (raw AI output) ────────────────────
CREATE TABLE alerts (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    alert_type      VARCHAR(50) NOT NULL,   -- 'red_light','crowd_dense','face_match', etc.
    camera_id       VARCHAR(20) NOT NULL,
    confidence      DECIMAL(5,4) NOT NULL,
    severity        SMALLINT CHECK (severity BETWEEN 1 AND 4),
    snapshot_path   VARCHAR(255),
    snapshot_b64    TEXT,              -- base64 JPEG of the violation crop (for dashboard display)
    clip_path       VARCHAR(255),
    object_metadata JSONB,                  -- bbox, track_id, vehicle_type, etc.
    location_name   VARCHAR(100),
    latitude        DECIMAL(10,7),
    longitude       DECIMAL(10,7),
    status          VARCHAR(20) DEFAULT 'pending'
                        CHECK (status IN (
                          'pending','acknowledged','assigned','investigating','resolved',
                          'accepted','rejected','escalated','closed'
                        )),
    assigned_to     VARCHAR(100),
    acknowledged_at TIMESTAMPTZ,
    resolved_at     TIMESTAMPTZ,
    event_id        UUID,
    rule_id         UUID,
    incident_id     UUID,                   -- grouped into incident card
    raw_frame_ts    TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    tenant_id       UUID
);
CREATE INDEX alerts_status_idx ON alerts(status);
CREATE INDEX alerts_camera_idx ON alerts(camera_id);
CREATE INDEX alerts_created_idx ON alerts(created_at DESC);
CREATE INDEX alerts_type_idx ON alerts(alert_type);

-- ── Normalized events (worker messages before rules) ──
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
    tenant_id       UUID,
    UNIQUE (source_stream, source_id)
);
CREATE INDEX IF NOT EXISTS events_camera_time_idx ON events(camera_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS events_type_time_idx ON events(event_type, occurred_at DESC);

-- ── AI rules (severity is defined only here) ──
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
);
CREATE INDEX IF NOT EXISTS ai_rules_enabled_idx ON ai_rules(enabled, priority DESC);

CREATE TABLE IF NOT EXISTS alert_notes (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    alert_id    UUID NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    officer_id  VARCHAR(100),
    note        TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS alert_notes_alert_idx ON alert_notes(alert_id, created_at);

CREATE TABLE IF NOT EXISTS alert_dead_letters (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_stream   VARCHAR(50) NOT NULL,
    source_id       VARCHAR(64) NOT NULL,
    payload         JSONB,
    error           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS alert_dead_letters_created_idx
  ON alert_dead_letters(created_at DESC);

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
ON CONFLICT (name) DO NOTHING;

UPDATE ai_rules SET match_json = '{"risk_category":"critical"}'::jsonb
 WHERE name = 'face_match_critical' AND match_json IS NULL;

-- ── Incident cards (grouped alerts) ───────────
CREATE TABLE incidents (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    title           VARCHAR(200),
    alert_types     TEXT[],
    severity        SMALLINT CHECK (severity BETWEEN 1 AND 4),
    status          VARCHAR(20) DEFAULT 'open'
                        CHECK (status IN ('open','assigned','dispatched','closed')),
    assigned_to     UUID REFERENCES officers(id),
    location_name   VARCHAR(100),
    latitude        DECIMAL(10,7),
    longitude       DECIMAL(10,7),
    notes           TEXT,
    closed_at       TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ── Alert metadata for vector forensic search ──
CREATE TABLE alert_search_index (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    alert_id        UUID REFERENCES alerts(id) ON DELETE CASCADE,
    description     TEXT,                   -- human-readable description for NL search
    embedding       vector(1536),           -- text embedding of description
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX alert_search_embedding_idx
    ON alert_search_index USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- ── GD / FIR drafts ───────────────────────────
CREATE TABLE drafts (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    draft_type      VARCHAR(10) CHECK (draft_type IN ('GD','FIR','case_note')),
    incident_id     UUID REFERENCES incidents(id),
    officer_id      UUID REFERENCES officers(id),
    raw_notes       TEXT,
    structured_json JSONB,                  -- AI-structured fields
    missing_fields  TEXT[],
    draft_text      TEXT,
    language        VARCHAR(10) DEFAULT 'bn', -- 'bn' = Bangla, 'en' = English
    status          VARCHAR(20) DEFAULT 'draft'
                        CHECK (status IN ('draft','reviewed','approved','submitted')),
    approved_by     UUID REFERENCES officers(id),
    approved_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ── Immutable audit log ───────────────────────
-- Append-only. No UPDATE or DELETE allowed (enforced via trigger).
CREATE TABLE audit_log (
    id              BIGSERIAL PRIMARY KEY,
    officer_id      UUID,
    action          VARCHAR(50) NOT NULL,   -- 'alert_accepted', 'login', 'search', etc.
    resource_type   VARCHAR(50),
    resource_id     UUID,
    details         JSONB,
    ip_address      INET,
    session_id      VARCHAR(100),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX audit_officer_idx ON audit_log(officer_id);
CREATE INDEX audit_created_idx ON audit_log(created_at DESC);

-- Prevent any modification to audit_log
CREATE OR REPLACE FUNCTION audit_log_immutable()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is immutable — no UPDATE or DELETE allowed';
END;
$$;
CREATE TRIGGER no_update_audit BEFORE UPDATE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();
CREATE TRIGGER no_delete_audit BEFORE DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();

-- ── Camera health (ingest pipeline) ───────────
CREATE TABLE IF NOT EXISTS camera_status (
    camera_id     VARCHAR(20) PRIMARY KEY,
    status        VARCHAR(20) NOT NULL,
    fps           DOUBLE PRECISION DEFAULT 0,
    last_frame_ts TIMESTAMPTZ,
    detail        TEXT,
    last_checked  TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW()
);

-- ── Detection zones (traffic-ai; additional types added in later phases) ──
CREATE TABLE IF NOT EXISTS camera_zones (
    id                   UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    camera_id            VARCHAR(20) NOT NULL,
    zone_type            VARCHAR(30) NOT NULL
                         CHECK (zone_type IN (
                           'red_light','stop_line','wrong_lane',
                           'no_parking','speed','detection',
                           'restricted','counting_line'
                         )),
    zone_name            VARCHAR(100),
    polygon_points_json  JSONB,
    stop_line_y          DECIMAL(6,4),
    lane_boundary_json   JSONB,
    speed_limit_kmh      INTEGER  DEFAULT 60,
    speed_cal_ppm        DECIMAL(10,4) DEFAULT 100.0,
    camera_direction     VARCHAR(10) DEFAULT 'down',
    is_active            BOOLEAN DEFAULT TRUE,
    created_at           TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS camera_zones_cam_idx ON camera_zones(camera_id);

-- ── Shifts / Roster ───────────────────────────
CREATE TABLE shifts (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    officer_id      UUID NOT NULL REFERENCES officers(id),
    station_id      UUID,
    shift_start     TIMESTAMPTZ NOT NULL,
    shift_end       TIMESTAMPTZ,
    role_on_shift   VARCHAR(30),
    handover_notes  TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
