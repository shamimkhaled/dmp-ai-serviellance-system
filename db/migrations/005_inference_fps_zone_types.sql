-- Per-camera inference FPS (NULL = CAMERA_FPS env default, clamped 1–25).
-- Zone types: restricted (polygon) and counting_line (entry/exit).
-- Safe to re-run.

ALTER TABLE cameras ADD COLUMN IF NOT EXISTS inference_fps INTEGER;

DO $$ BEGIN
  ALTER TABLE cameras ADD CONSTRAINT cameras_inference_fps_range
    CHECK (inference_fps IS NULL OR (inference_fps >= 1 AND inference_fps <= 25));
EXCEPTION
  WHEN duplicate_object THEN NULL;
END $$;

DO $$
DECLARE
  r RECORD;
BEGIN
  FOR r IN
    SELECT c.conname
    FROM pg_constraint c
    JOIN pg_class t ON c.conrelid = t.oid
    JOIN pg_namespace n ON n.oid = t.relnamespace
    WHERE t.relname = 'camera_zones'
      AND n.nspname = 'public'
      AND c.contype = 'c'
      AND pg_get_constraintdef(c.oid) ILIKE '%zone_type%'
  LOOP
    EXECUTE format('ALTER TABLE camera_zones DROP CONSTRAINT %I', r.conname);
  END LOOP;
END $$;

ALTER TABLE camera_zones ADD CONSTRAINT camera_zones_zone_type_check
  CHECK (zone_type IN (
    'red_light','stop_line','wrong_lane','no_parking','speed','detection',
    'restricted','counting_line'
  ));
