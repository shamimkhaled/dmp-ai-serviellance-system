# Security notes — VMS Intelligence

This is a lab/on-prem stack. Phase 1 hardens exposure and secrets. **Phase 5 wires Keycloak JWT + RBAC** on REST and WebSocket APIs. Treat any host that can reach port 80, 8554, 8888, 8889, or 8189 as able to see **live video** (WHEP is not JWT-gated).

## What you must do operationally

1. **Rotate the database password** if `.env.example` or a previous `.env` with a real `DATABASE_URL` was ever committed, copied, or pushed.
2. **Rotate camera RTSP passwords** if `admin:123456` (or any camera userinfo) was stored in git, MediaMTX YAML, or `db/migrations/003_eyenor_cam1.sql`.
3. Copy `.env.example` to `.env` and fill placeholders. Never commit `.env`.
4. Set `KEYCLOAK_ADMIN_PASSWORD` (and change lab user passwords in Keycloak). Lab users `vms-admin` / `vms-operator` / `vms-investigator` / `vms-viewer` start with password `changeme`.
5. Put cameras and RTSP (`:8554`) on a **private VLAN**. Do not forward 8554, 8888, 8889, 8189, or 9997 to the public Internet.
6. Terminate **TLS** in front of nginx (or add `listen 443 ssl`) before any untrusted network can reach the UI.

## Authentication (Phase 5)

Compose default is `AUTH_MODE=keycloak`. The dashboard redirects to Keycloak (`http://127.0.0.1:8080`, realm `vms`, client `vms-dashboard`, PKCE). APIs verify RS256 access tokens against Keycloak JWKS.

- **Roles (backend-authoritative):** `admin`, `operator`, `investigator`, `viewer`. Legacy ranks `igp` / `dig` / `station_commander` map to admin; `supervisor` maps to investigator.
- **Camera ACL:** empty `user_site_access` for a user means all cameras (single-tenant). Non-empty restricts to those `site_id`s. Admins bypass.
- **WebSocket:** pass `?access_token=` (browsers cannot set `Authorization` on WS). Nginx forwards `Authorization` and `X-Access-Token`.
- **Health checks** (`/health`, `/metrics`) stay unauthenticated so Compose probes keep working.
- **Lab escape hatch:** `AUTH_MODE=off` treats callers as local admin. Use only on a trusted host when Keycloak is down.
- **Keycloak is localhost-only** (`127.0.0.1:8080`). LAN browsers cannot complete OIDC unless you publish Keycloak or set `AUTH_MODE=off`.
- **Existing Keycloak DB:** `--import-realm` imports the `vms` realm only if it does not already exist in the `keycloak` Postgres database. Drop that database and recreate `pai_keycloak` for a fresh import.

## Still not JWT-gated

- **WHEP / WebRTC ICE / HLS** on MediaMTX (`:8889`, `:8189`, `:8888`) — network-trusted (localhost/LAN). Anyone who can reach those ports can watch live video.
- **MJPEG preview** URLs include `access_token` in the query string (visible in logs, Referer, and browser history). Prefer a trusted network.
- RTSP passwords remain plaintext in PostgreSQL `cameras.rtsp_url` (server-side only; never returned on GET).
- Snapshots still stored as TEXT in Postgres.
- Face match **payloads** still store `nid` / `matched_name` in Redis/Postgres. APIs redact them for roles without `faces:view`. Face snapshots are hidden unless the caller has `faces:view` or (non-face) `evidence:read`.

## Nginx map

| Browser path | Backend |
|---|---|
| `/` | dashboard `:3000` |
| `/ingest/` | video-ingest `:8001/` |
| `/api/cameras` | video-ingest `/cameras` |
| `/api/` | alert-service `:8004/` |
| `/ws/` | alert-service `/ws/` |
| `/api/ws/` | alert-service `/ws/` (also works via `/api/`) |
| `/ai/` | traffic-ai `:8002/` |
| `/api/draft` | drafting `/draft` |

WHEP remains `http://<HOST_IP>:8889/<camera>/whep`. ICE uses UDP/TCP `8189`.
