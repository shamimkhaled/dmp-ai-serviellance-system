-- Promote runtime-created camera_status and camera_zones into versioned schema.
-- Safe to re-run (IF NOT EXISTS). Existing traffic-ai / ingest_pipeline DDL is compatible.

CREATE TABLE IF NOT EXISTS camera_status (
    camera_id     VARCHAR(20) PRIMARY KEY,
    status        VARCHAR(20) NOT NULL,
    fps           DOUBLE PRECISION DEFAULT 0,
    last_frame_ts TIMESTAMPTZ,
    detail        TEXT,
    last_checked  TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS camera_zones (
    id                   UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    camera_id            VARCHAR(20) NOT NULL,
    zone_type            VARCHAR(30) NOT NULL
                         CHECK (zone_type IN (
                           'red_light','stop_line','wrong_lane',
                           'no_parking','speed','detection'
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
