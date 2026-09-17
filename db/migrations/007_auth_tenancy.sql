-- Phase 5: nullable tenancy, sites, camera ACL, wider officer roles.
-- Additive only. Existing single-tenant rows stay valid (NULL tenant_id / site_id).

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

ALTER TABLE officers ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE cameras  ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE cameras  ADD COLUMN IF NOT EXISTS site_id UUID REFERENCES sites(id);
ALTER TABLE alerts   ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE events   ADD COLUMN IF NOT EXISTS tenant_id UUID;

DO $$
DECLARE r RECORD;
BEGIN
  FOR r IN
    SELECT c.conname
    FROM pg_constraint c
    JOIN pg_class t ON c.conrelid = t.oid
    JOIN pg_namespace n ON n.oid = t.relnamespace
    WHERE t.relname = 'officers' AND n.nspname = 'public'
      AND c.contype = 'c'
      AND pg_get_constraintdef(c.oid) ILIKE '%role%'
  LOOP
    EXECUTE format('ALTER TABLE officers DROP CONSTRAINT %I', r.conname);
  END LOOP;
END $$;

ALTER TABLE officers ADD CONSTRAINT officers_role_check
  CHECK (role IN (
    'operator','supervisor','station_commander','dig','igp','admin',
    'investigator','viewer'
  ));

CREATE INDEX IF NOT EXISTS cameras_site_idx ON cameras(site_id);
CREATE INDEX IF NOT EXISTS cameras_tenant_idx ON cameras(tenant_id);
CREATE INDEX IF NOT EXISTS alerts_camera_idx ON alerts(camera_id);
