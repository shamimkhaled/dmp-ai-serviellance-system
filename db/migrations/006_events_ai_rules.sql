-- Event store, DB-backed AI rules (severity lives here), alert lifecycle, DLQ.
-- Safe to re-run.

-- ── Events (normalized detections / worker messages) ──────────────────────────
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
);
CREATE INDEX IF NOT EXISTS events_camera_time_idx ON events(camera_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS events_type_time_idx ON events(event_type, occurred_at DESC);

-- ── Rules: WHEN match → THEN severity / alert_type ────────────────────────────
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

-- ── Alert lifecycle columns ───────────────────────────────────────────────────
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS assigned_to VARCHAR(100);
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS acknowledged_at TIMESTAMPTZ;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS event_id UUID;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS rule_id UUID;

DO $$
DECLARE r RECORD;
BEGIN
  FOR r IN
    SELECT c.conname
    FROM pg_constraint c
    JOIN pg_class t ON c.conrelid = t.oid
    JOIN pg_namespace n ON n.oid = t.relnamespace
    WHERE t.relname = 'alerts'
      AND n.nspname = 'public'
      AND c.contype = 'c'
      AND pg_get_constraintdef(c.oid) ILIKE '%status%'
  LOOP
    EXECUTE format('ALTER TABLE alerts DROP CONSTRAINT %I', r.conname);
  END LOOP;
END $$;

ALTER TABLE alerts ADD CONSTRAINT alerts_status_check
  CHECK (status IN (
    'pending','acknowledged','assigned','investigating','resolved',
    'accepted','rejected','escalated','closed'
  ));

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

-- ── Seed default rules (severity is defined only here) ────────────────────────
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
