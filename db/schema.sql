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
                    'operator','supervisor','station_commander','dig','igp','admin'
                )),
    station_id  UUID,
    keycloak_id VARCHAR(100) UNIQUE,
    active      BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
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
    thana_id      UUID,
    active        BOOLEAN DEFAULT TRUE,
    last_seen_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ DEFAULT NOW()
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
                        CHECK (status IN ('pending','accepted','rejected','escalated','closed')),
    incident_id     UUID,                   -- grouped into incident card
    raw_frame_ts    TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX alerts_status_idx ON alerts(status);
CREATE INDEX alerts_camera_idx ON alerts(camera_id);
CREATE INDEX alerts_created_idx ON alerts(created_at DESC);
CREATE INDEX alerts_type_idx ON alerts(alert_type);

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

-- ── Seed: demo officer ─────────────────────────
INSERT INTO officers (badge_no, full_name, rank, role)
VALUES
    ('OPS001',  'Demo Operator',    'Constable',      'operator'),
    ('SUP001',  'Demo Supervisor',  'Sub-Inspector',  'supervisor'),
    ('CMD001',  'Station Commander','Inspector',      'station_commander');

-- ── Seed: demo camera ─────────────────────────
INSERT INTO cameras (camera_id, name, rtsp_url, location_name, zone_type, brand, connection_mode)
VALUES
    ('cam01', 'Main Gate Camera', 'rtsp://localhost:8554/cam01',
     'Main Gate', 'entry_exit', 'custom', 'publish');

-- ── Seed: demo detection zones for cam01 ──────
-- All polygon/line coordinates are normalised 0-1 relative to the 640×640
-- inference frame.  Adjust to match actual camera layout before deployment.
INSERT INTO camera_zones
    (camera_id, zone_type, zone_name,
     polygon_points_json, stop_line_y, lane_boundary_json,
     speed_limit_kmh, speed_cal_ppm, camera_direction, is_active)
VALUES
    -- Red-light box: upper-centre of frame (intersection stop zone)
    ('cam01', 'red_light', 'Main Gate Red Light',
     '[[0.3,0.1],[0.7,0.1],[0.7,0.45],[0.3,0.45]]',
     NULL, NULL,
     60, 100.0, 'down', TRUE),

    -- Stop line: horizontal line at 65% of frame height
    ('cam01', 'stop_line', 'Main Gate Stop Line',
     NULL,
     0.65, NULL,
     60, 100.0, 'down', TRUE),

    -- No-parking zone: lower-left quadrant (shoulder area)
    ('cam01', 'no_parking', 'Main Gate No-Parking',
     '[[0.0,0.6],[0.25,0.6],[0.25,1.0],[0.0,1.0]]',
     NULL, NULL,
     60, 100.0, 'down', TRUE);
