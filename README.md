# DMP AI Serveillance System —  Documentation

Bangladesh DMP · Sovereign AI Operating Layer  

On-premise surveillance and AI-assisted command platform: camera ingestion, realtime object detection, alert pipeline, operator dashboard (Bangla/English), GD/FIR drafting, and immutable audit logging.

---

## Table of contents

1. [Architecture](#architecture)
2. [Prerequisites](#prerequisites)
3. [Quick start (Docker)](#quick-start-docker)
4. [Project structure](#project-structure)
5. [Services and ports](#services-and-ports)
6. [Environment variables](#environment-variables)
7. [Running individual services](#running-individual-services)
8. [Dashboard development](#dashboard-development)
9. [Cameras and live video](#cameras-and-live-video)
10. [Traffic AI and realtime detection](#traffic-ai-and-realtime-detection)
11. [Alert pipeline](#alert-pipeline)
12. [Database](#database)
13. [Common development tasks](#common-development-tasks)
14. [Troubleshooting](#troubleshooting)
15. [Production deployment notes](#production-deployment-notes)
16. [Governance rules](#governance-rules)
17. [Security](SECURITY.md)

---

## Architecture

```mermaid
flowchart LR
  subgraph cameras [IP Cameras]
    CAM[RTSP H.264/H.265]
  end

  subgraph nodeB [Node B — Ingest]
    MTX[MediaMTX]
    VI[video-ingest :8001]
    CAM -->|pull or push| MTX
    VI -->|register paths| MTX
    VI -->|health| MTX
  end

  subgraph nodeA [Node A — AI]
    TAI[traffic-ai :8002]
    FAI[face-ai :8003]
    MTX -->|RTSP relay| TAI
    MTX -->|RTSP relay| FAI
  end

  subgraph pipeline [Alert pipeline]
    REDIS[(Redis Streams)]
    AS[alert-service :8004]
    PG[(PostgreSQL)]
    TAI -->|alerts:traffic| REDIS
    FAI -->|alerts:face| REDIS
    REDIS --> AS
    AS --> PG
  end

  subgraph ui [Operator UI]
    DASH[dashboard :3000]
    AS -->|WebSocket| DASH
    VI -->|REST| DASH
    TAI -->|AI preview MJPEG| DASH
    MTX -->|WHEP/HLS| DASH
  end
```

**Data flow (simplified)**

1. **Cameras** push or are pulled into **MediaMTX** (RTSP hub).
2. **video-ingest** registers camera paths in MediaMTX, health-checks streams, and exposes CRUD APIs for the dashboard.
3. **traffic-ai** / **face-ai** read frames from MediaMTX, run inference, and publish alert candidates to **Redis Streams**.
4. **alert-service** normalizes stream messages into **events**, evaluates **ai_rules** (severity lives only in rules), persists matching **alerts** to **PostgreSQL**, and pushes live updates to the dashboard via **WebSocket**. Failed Redis deliveries are reclaimed from the PEL and dead-lettered after a bounded retry.
5. Officers **acknowledge / assign / investigate / resolve** (and still reject or escalate) alerts in the dashboard; actions and notes are written to the immutable **audit_log**.

---

## Prerequisites

| Requirement | Version | Notes |
|-------------|---------|--------|
| Docker | 24+ | Engine + Compose plugin |
| Docker Compose | v2 | `docker compose` (not `docker-compose`) |
| Git | any | Clone the repo |
| Disk | ~8 GB+ | First `traffic-ai` build downloads PyTorch (~1.5 GB) |
| GPU | optional | Not required for local dev; CPU inference works |

**Optional (run services outside Docker)**

| Tool | Version |
|------|---------|
| Python | 3.11+ |
| Node.js | 20+ |
| PostgreSQL | 16 + pgvector |
| Redis | 7 |
| FFmpeg / ffprobe | for RTSP debugging |

---

## Quick start (Docker)

### 1. Clone and enter the project

```bash
git clone <repo-url> dmp-ai-serviellance-system
cd dmp-ai-serviellance-system
```

### 2. Start the full stack

```bash
docker compose up -d --build
```

Or use the helper script (checks Arch Linux kernel/module mismatch before starting):

```bash
chmod +x scripts/up.sh
./scripts/up.sh
```

First build can take **15–20 minutes** (traffic-ai installs CPU PyTorch + OpenCV). Subsequent rebuilds use Docker layer cache.

### 3. Wait for services to become healthy

```bash
docker compose ps
```

Wait until `pai_redis` and `pai_mediamtx` show `(healthy)`. PostgreSQL is external — it is not a compose service.

### 4. Open the operator dashboard

The UI is branded **VMS Intelligence**. Opening `http://localhost/` redirects to Keycloak (realm `vms`). Police GD/FIR drafting is under Administration → Industry pack.

Lab users (password `changeme` — change after first login):

| Username | Role |
|----------|------|
| `vms-admin` | admin |
| `vms-operator` | operator |
| `vms-investigator` | investigator |
| `vms-viewer` | viewer |

| URL | Purpose |
|-----|---------|
| http://localhost/ | **Dashboard** (nginx front door; OIDC login) |
| http://localhost/ingest/health | Video ingest (via nginx; no JWT) |
| http://localhost/api/health | Alert service (via nginx; no JWT) |
| http://localhost/ai/health | Traffic AI (via nginx; no JWT) |
| http://127.0.0.1:8080 | Keycloak admin (`KEYCLOAK_ADMIN`) and user login |
| http://127.0.0.1:8889 | MediaMTX WHEP (browser ICE; not via nginx; no JWT) |

Set `AUTH_MODE=off` in `.env` only for a trusted lab when Keycloak is unavailable. Recreate the dashboard container after changing `VITE_*` / `AUTH_MODE`.

If the `vms` realm is missing, drop the `keycloak` database and recreate the container: `docker compose up -d --force-recreate keycloak`.

### 5. Verify the stack

```bash
# All containers running
docker compose ps

# Traffic AI processing frames
curl -s http://127.0.0.1:8002/health | python3 -m json.tool

# Alert service (localhost bind)
curl -s http://127.0.0.1:8004/health

# MediaMTX paths (cam01 = fake test stream)
curl -s http://localhost:9997/v3/paths/list | python3 -m json.tool
```

### 6. Stop the stack

```bash
docker compose down          # keep volumes (DB data)
docker compose down -v       # delete volumes (fresh DB)
```

---

## Project structure

```
dmp-ai-serviellance-system/
├── docker-compose.yml          # Local dev stack (all services)
├── db/
│   ├── schema.sql              # PostgreSQL schema (auto-applied on first postgres start)
│   └── migrations/             # Incremental SQL migrations
├── infra/
│   ├── nginx.conf              # Reverse proxy (:80)
│   └── mediamtx/
│       └── mediamtx.yml        # Production MediaMTX config (Node B)
├── scripts/
│   └── up.sh                   # Safe compose up (Arch kernel check)
├── services/
│   ├── video-ingest/
│   │   ├── main.py             # FastAPI camera registry + MediaMTX sync
│   │   ├── ingest_pipeline.py  # Optional multiprocessing frame pipeline (not in compose)
│   │   ├── mediamtx_client.py  # MediaMTX REST API client
│   │   ├── transcode_manager.py# H.265 → H.264 browser relay
│   │   ├── camera_urls.py      # Brand RTSP URL templates
│   │   └── mediamtx.yml        # Dev MediaMTX config (mounted in compose)
│   ├── traffic-ai/
│   │   ├── traffic_ai_worker.py# Production worker (YOLO + DeepSORT + zones)
│   │   ├── worker.py           # Legacy mock worker (deprecated)
│   │   └── fine_tune_bd_vehicles.py
│   ├── face-ai/
│   │   └── worker.py           # InsightFace watchlist worker
│   ├── alert-service/
│   │   └── main.py             # Redis → PostgreSQL → WebSocket
│   └── drafting/
│       └── main.py             # GD/FIR LLM drafting (Ollama)
└── dashboard/
    ├── src/
    │   ├── App.jsx             # VMS Intelligence shell (Overview, Live, Alerts, …)
    │   ├── lib/constants.js    # URLs, SEVERITY, ALERT_LABELS
    │   ├── pages/              # Live, Events, Investigation, AdminHub
    │   ├── components/index.jsx# CommandCenter, CameraGrid, AlertPanel, …
    │   └── hooks/index.js      # useAlerts, useWebSocket
    ├── Dockerfile.dev          # Vite dev server in Docker
    └── package.json
```

---

## Services and ports

Browser entry is **nginx on port 80**. Application APIs are bound to localhost on the host so they are not reachable from other machines. WHEP/ICE/HLS stay on MediaMTX because WebRTC ICE cannot go through an HTTP proxy.

| Service | Container | Host bind | Description |
|---------|-----------|-----------|-------------|
| Nginx | `pai_nginx` | `0.0.0.0:80` | Dashboard + `/api` + `/ingest` + `/ai` + `/ws` |
| Dashboard | `pai_dashboard` | `127.0.0.1:3000` | Vite (open via nginx) |
| Video ingest | `pai_video_ingest` | `127.0.0.1:8001` | Camera registry (use `/ingest`) |
| Traffic AI | `pai_traffic_ai` | `127.0.0.1:8002` | YOLO / detections (use `/ai`) |
| Face AI | `pai_face_ai` | `127.0.0.1:8003` | Watchlist worker |
| Alert service | `pai_alert_service` | `127.0.0.1:8004` | Alerts + WS (use `/api` and `/ws`) |
| Drafting | `pai_drafting` | `127.0.0.1:8006` | GD/FIR (use `/api/draft`) |
| Keycloak | `pai_keycloak` | `127.0.0.1:8080` | Realm `vms`, client `vms-dashboard` |
| MediaMTX RTSP | `pai_mediamtx` | `0.0.0.0:8554` | Camera ingest — private network only |
| MediaMTX WHEP | `pai_mediamtx` | `0.0.0.0:8889` | Browser live signaling |
| MediaMTX ICE | `pai_mediamtx` | `0.0.0.0:8189` | WebRTC media (UDP+TCP) |
| MediaMTX HLS | `pai_mediamtx` | `0.0.0.0:8888` | HLS fallback |
| MediaMTX API | `pai_mediamtx` | not published | `http://mediamtx:9997` on compose network |
| Redis | `pai_redis` | not published | `redis://redis:6379` on compose network |
| PostgreSQL | external | your DB host | Apply `db/schema.sql` once |

See [SECURITY.md](SECURITY.md) for exposure rules and secret handling.

---

## Environment variables

Default values are set in `docker-compose.yml`. Override with a `.env` file in the project root or by editing compose.

### video-ingest

| Variable | Default (compose) | Description |
|----------|-------------------|-------------|
| `DATABASE_URL` | from `.env` | PostgreSQL DSN (required) |
| `MEDIAMTX_URL` | `http://mediamtx:9997` | MediaMTX API base (compose network) |
| `RTSP_BASE_URL` | `rtsp://mediamtx:8554` | Internal RTSP relay |
| `WHEP_BASE_URL` | `http://localhost:8889` | Browser WHEP base (host-facing) |
| `HLS_BASE_URL` | `http://localhost:8888` | Browser HLS base |
| `PUBLIC_RTSP_URL` | `rtsp://localhost:8554` | Publish URL shown to operators |
| `EYENOR_CAM1_RTSP` | unset | Optional lab camera seed; never commit the value |

### traffic-ai (`traffic_ai_worker.py`)

| Variable | Default (compose) | Description |
|----------|-------------------|-------------|
| `REDIS_URL` | `redis://redis:6379` | Alert publish target |
| `DATABASE_URL` | (see above) | Camera list + zones + locations |
| `RTSP_BASE_URL` | `rtsp://mediamtx:8554` | Frame source per camera |
| `VIDEO_INGEST_URL` | `http://video-ingest:8001` | Live camera discovery |
| `FRAME_SOURCE` | `rtsp` | `rtsp` \| `mock` \| `queue` |
| `USE_GPU` | `false` | `true` on GPU node (CUDA) |
| `CAMERA_IDS` | `cam01` | Fallback if DB unavailable |
| `TRAFFIC_CONF_THRESHOLD` | `0.35` | YOLO confidence cutoff |
| `CAMERA_FPS` | `10` | Default inference FPS when a camera has no `inference_fps` (1–25). Keep 10 on CPU; raise only when Administration shows GPU inference p95 under 50ms |
| `DET_WS_COALESCE_MS` | `150` | Latest-wins detection overlay WS interval (0 = every inference frame) |
| `BATCH_SIZE` | `4` on GPU, `1` on CPU | YOLO batch; RTSP/queue now share one infer loop so GPU batching actually runs |
| `ANPR_ENABLED` | `false` | PaddleOCR plate reading (heavy) |
| `PREVIEW_ENABLED` | `true` | Annotated MJPEG for `/preview` |
| `LOG_LEVEL` | `INFO` | structlog level |
| `AUTH_MODE` | `keycloak` | Verifies tokens via alert-service (no PyJWT in this image) |
| `AUTH_VERIFY_URL` | `http://alert-service:8004/auth/verify` | Internal token check |

### face-ai

| Variable | Default | Description |
|----------|---------|-------------|
| `MOCK_MODE` | `true` | Synthetic face-match alerts in dev |
| `FACE_MATCH_THRESHOLD` | `0.75` | Cosine similarity cutoff |
| `REDIS_URL` | `redis://redis:6379` | Publishes to `alerts:face` |

### alert-service

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | (see above) | Alert persistence |
| `REDIS_URL` | `redis://redis:6379` | Stream consumer |
| `JWT_SECRET` | from `.env` | Legacy HS256 secret; APIs verify Keycloak RS256 JWTs |
| `AUTH_MODE` | `keycloak` | `off` = lab admin bypass |
| `OIDC_ISSUER` | `http://<HOST_IP>:8080/realms/vms` | Must match access-token `iss` (localhost and 127.0.0.1 both accepted) |
| `OIDC_JWKS_URL` | `http://keycloak:8080/realms/vms/protocol/openid-connect/certs` | Internal JWKS |
| `DEDUP_WINDOW_SECONDS` | `30` | Legacy env; alert creation cooldown is per-rule (`cooldown_seconds`) |
| `PEL_MIN_IDLE_MS` | `60000` | Reclaim unacked Redis messages after this idle time |
| `PEL_MAX_DELIVERIES` | `5` | After this many failed deliveries, move to `alerts:dead` |

### drafting

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_API_URL` | `http://host.docker.internal:11434` | Ollama API on host |
| `LLM_MODEL` | `llama3.1:8b` | Model name |

### dashboard (Vite — baked at container start)

| Variable | Default | Description |
|----------|---------|-------------|
| `VITE_ALERT_SERVICE_URL` | `http://<HOST_IP>/api` | Alerts REST |
| `VITE_WS_URL` | `ws://<HOST_IP>` | Origin for `/ws/{session}` |
| `VITE_VIDEO_INGEST_URL` | `http://<HOST_IP>/ingest` | Camera CRUD |
| `VITE_MEDIAMTX_WHEP_URL` | `http://<HOST_IP>:8889` | Live WHEP streams |
| `VITE_MEDIAMTX_HLS_URL` | `http://<HOST_IP>:8888` | HLS fallback |
| `VITE_TRAFFIC_AI_URL` | `http://<HOST_IP>/ai` | Detection overlay WS |
| `VITE_AUTH_MODE` | `keycloak` | `off` skips OIDC redirect |
| `VITE_OIDC_AUTHORITY` | `http://<HOST_IP>:8080/realms/vms` | Keycloak realm URL |
| `VITE_OIDC_CLIENT_ID` | `vms-dashboard` | Public PKCE client |

> **Note:** Changing `VITE_*` variables requires recreating the dashboard container:  
> `docker compose up -d dashboard`

---

## Running individual services

### Rebuild one service after code changes

```bash
docker compose build traffic-ai
docker compose up -d traffic-ai
```

### View logs

```bash
docker logs -f pai_traffic_ai
docker logs -f pai_video_ingest
docker logs -f pai_alert_service
docker compose logs -f mediamtx
```

### Restart MediaMTX + re-sync cameras

After restarting MediaMTX, pull-mode cameras must be re-registered by video-ingest:

```bash
docker compose restart mediamtx
sleep 10
docker compose restart video-ingest
```

### Run video-ingest locally (without Docker)

```bash
cd services/video-ingest
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL=postgresql://policeai:policeai_dev_secret@localhost:5432/policeai
export MEDIAMTX_URL=http://localhost:9997
uvicorn main:app --host 0.0.0.0 --port 8001 --reload
```

### Run traffic-ai locally

Requires postgres, redis, and mediamtx running (via compose or native):

```bash
cd services/traffic-ai
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt   # slow: downloads torch
export REDIS_URL=redis://localhost:6379
export DATABASE_URL=postgresql://policeai:policeai_dev_secret@localhost:5432/policeai
export RTSP_BASE_URL=rtsp://localhost:8554
export USE_GPU=false
export FRAME_SOURCE=rtsp
python traffic_ai_worker.py
```

### Run ingest_pipeline (optional, not in compose)

Standalone multiprocessing frame distributor for Node B production:

```bash
cd services/video-ingest
export DB_URL=postgresql://policeai:policeai_dev_secret@localhost:5432/policeai
export MEDIAMTX_API=http://localhost:9997/v3
export USE_FAKE_STREAMS=true    # loop local .mp4 instead of RTSP
python ingest_pipeline.py
# Health: http://localhost:8005/health
```

---

## Dashboard development

The dashboard runs **Vite** with hot reload. Source is mounted into the container:

```yaml
volumes:
  - ./dashboard/src:/app/src
```

Edit files under `dashboard/src/` and refresh the browser — changes apply without rebuild.

### Run dashboard on the host (outside Docker)

```bash
cd dashboard
npm install
export VITE_ALERT_SERVICE_URL=http://localhost:8004
export VITE_VIDEO_INGEST_URL=http://localhost:8001
export VITE_MEDIAMTX_WHEP_URL=http://localhost:8889
export VITE_MEDIAMTX_HLS_URL=http://localhost:8888
export VITE_TRAFFIC_AI_URL=http://localhost:8002
npm run dev
```

### Dashboard tabs

| Tab | Bangla | Function |
|-----|--------|----------|
| Alerts | সতর্কতা | Pending AI alerts — Accept / Reject / Escalate |
| Cameras | ক্যামেরা | Live view + add/edit cameras + **AI** detection toggle |
| Incidents | ঘটনা | Grouped incident cards |
| Forensics | ফরেনসিক সার্চ | Natural-language footage search |
| GD/FIR | জিডি / এফআইআর | LLM-assisted drafting |

Language toggle: **EN / বাং** in the top bar.

---

## Cameras and live video

### Built-in test camera

`fake-camera` pushes a synthetic H.264 test pattern to `rtsp://mediamtx:8554/cam01`. No physical camera needed for basic dev.

### Add a physical camera

**Dashboard:** Cameras tab → **+ Add camera**

**API:**

```bash
# Pull mode — server reads from camera (most brands)
curl -X POST http://localhost:8001/cameras/connect \
  -H "Content-Type: application/json" \
  -d '{
    "camera_id": "cam03",
    "name": "Main Gate",
    "brand": "hikvision",
    "connection_mode": "pull",
    "host": "192.168.1.100",
    "port": 554,
    "username": "admin",
    "password": "your_password",
    "channel": 1,
    "location_name": "Station Gate"
  }'

# Custom RTSP URL
curl -X POST http://localhost:8001/cameras/connect \
  -H "Content-Type: application/json" \
  -d '{
    "camera_id": "cam05",
    "name": "Parking",
    "brand": "custom",
    "connection_mode": "pull",
    "rtsp_url": "rtsp://user:pass@192.168.1.50:554/Streaming/Channels/101"
  }'

# Push mode — camera publishes to MediaMTX
curl -X POST http://localhost:8001/cameras/connect \
  -H "Content-Type: application/json" \
  -d '{
    "camera_id": "cam06",
    "name": "Remote Cam",
    "brand": "custom",
    "connection_mode": "publish"
  }'
# Configure camera to push to: rtsp://<server-ip>:8554/cam06
```

**Supported brands:** `hikvision`, `dahua`, `axis`, `tplink`, `reolink`, `uniview`, `onvif`, `custom`

List brands: `GET http://localhost:8001/cameras/brands`

### Live browser playback

| Method | URL | When to use |
|--------|-----|-------------|
| WHEP (WebRTC) | `http://localhost:8889/<camera_id>/whep` | H.264 streams |
| HLS | `http://localhost:8888/<camera_id>/index.m3u8` | Safari / fallback |
| H.264 relay | `http://localhost:8889/<camera_id>_view/whep` | H.265 cameras (auto-created) |

**H.265 cameras:** Browsers cannot decode H.265 over WebRTC. video-ingest auto-creates an H.264 relay at `<camera_id>_view`. Use the camera's **substream** (H.264) when possible:

- Hikvision: `/Streaming/Channels/102`
- TP-Link/Tapo: `stream2`
- Dahua: `subtype=1`

### Verify RTSP before adding

```bash
ffprobe -rtsp_transport tcp "rtsp://user:pass@192.168.x.x:554/Streaming/Channels/101"
```

Or use **Test (⚡)** on each camera tile in the dashboard.

### Camera reachable from Docker?

Cameras on your LAN must be routable from the `pai_mediamtx` container:

```bash
docker exec pai_mediamtx ping -c2 192.168.x.x
```

If the host can reach the camera but the container cannot, check firewall/VLAN routing or use host networking for mediamtx in advanced setups.

---

## Traffic AI and realtime detection

The production worker is **`traffic_ai_worker.py`** (not the legacy `worker.py`).

### How it works

```
MediaMTX RTSP → decode frame → YOLO detect → DeepSORT track →
zone violation check → [draw boxes on /preview] →
on violation: Redis alerts:traffic → alert-service → dashboard
```

- **Auto-discovers cameras** from PostgreSQL + video-ingest API every ~60s.
- **No restart needed** when you add a camera in the dashboard.

### See detection in the dashboard

1. Open http://localhost:3000 → **Cameras** tab.
2. On any live camera tile, click the **AI** button.
3. The tile switches to the annotated MJPEG stream (green boxes = objects, red = violations).

Standalone preview grid: http://localhost:8002/preview

### Frame sources (`FRAME_SOURCE`)

| Value | Behavior |
|-------|----------|
| `rtsp` | Live detection from MediaMTX (default in compose) |
| `mock` | Synthetic violation alerts every ~30s (no camera needed) |
| `queue` | Read from `ingest_pipeline` multiprocessing queue |

### Violation alerts vs object detection

- **Object detection** runs at the camera's `inference_fps` (or `CAMERA_FPS` if unset). Display stays WHEP at the camera's native rate.
- **Violation alerts** (red-light, stop-line, wrong-lane, speeding, parking, restricted-area) require rows in `camera_zones`. Without zones, only the motorcycle helmet heuristic can fire alerts. The helmet check is a brightness heuristic, not a trained classifier.
- **Counting / line crossing / dwell** use DeepSORT `track_id`. A `counting_line` zone increments enter/exit once per track crossing (same `track_id` is not double-counted on a single crossing). Dwell is milliseconds inside a polygon, exposed on the detection overlay.

### Zone API (video-ingest)

Coordinates are normalised 0–1 relative to the inference frame.

```bash
# List / create / update / soft-delete
curl http://localhost/ingest/cameras/cam01/zones
curl -X POST http://localhost/ingest/cameras/cam01/zones \
  -H 'Content-Type: application/json' \
  -d '{"zone_type":"restricted","zone_name":"server room","polygon_points":[[0.1,0.1],[0.9,0.1],[0.9,0.9],[0.1,0.9]]}'
curl -X POST http://localhost/ingest/cameras/cam01/zones \
  -H 'Content-Type: application/json' \
  -d '{"zone_type":"counting_line","zone_name":"lobby door","line_points":[[0.2,0.5],[0.8,0.5]],"camera_direction":"down"}'
```

`camera_direction` `down` treats the positive side of the line as entry; `up` reverses that. Traffic-ai reloads zones on `ZONE_REFRESH_INTERVAL`.

Live counts: `GET /ai/counts/{camera_id}` (also on the detection WebSocket as `counts`).

### Health and metrics

```bash
curl http://localhost:8002/health
curl http://localhost:8002/metrics | grep traffic_
```

### Fine-tune Bangladesh vehicle model

```bash
cd services/traffic-ai
python fine_tune_bd_vehicles.py --help
```

---

## Alert pipeline

### Redis streams consumed by alert-service

| Stream | Producer |
|--------|----------|
| `alerts:traffic` | traffic-ai |
| `alerts:face` | face-ai |
| `alerts:crowd` | (future worker) |
| `alerts:emergency` | (future worker) |

### WebSocket (dashboard live updates)

```
ws://localhost:8004/ws/<session-id>
```

The dashboard connects automatically and receives `new_alert`, `alert_updated`, and `alert_catchup` messages.

### REST API (alert-service)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/alerts?limit=100` | List alerts |
| GET | `/alerts/{id}` | Alert detail + notes + snapshot |
| POST | `/alerts/{id}/action` | `acknowledge` / `assign` / `investigate` / `resolve` / `note` (legacy aliases: accepted, rejected, escalated, closed) |
| POST | `/alerts/{id}/notes` | Add an investigation note |
| GET | `/events` | Normalized events (no snapshots) |
| GET/POST | `/rules` | List / create AI rules (severity is defined here) |
| PATCH/DELETE | `/rules/{id}` | Update or disable a rule |
| GET | `/dead-letters` | Redis processing failures |
| GET | `/incidents?limit=50` | Incident cards |
| GET | `/health` | Service health |

### Mock alerts in dev

Set `FRAME_SOURCE=mock` on traffic-ai, or keep `MOCK_MODE=true` on face-ai (default in compose).

---

## Database

### Connection (from host)

```
postgresql://policeai:policeai_dev_secret@localhost:5432/policeai
```

### Schema

Applied automatically on first `postgres` container start from `db/schema.sql`.

Key tables: `cameras`, `events`, `ai_rules`, `alerts`, `alert_notes`, `incidents`, `watchlist`, `audit_log`, `drafts`, `officers`

### Migrations

```bash
docker exec -i pai_postgres psql -U policeai -d policeai \
  < db/migrations/002_camera_brands.sql
```

### Reset database

```bash
docker compose down -v
docker compose up -d postgres
# schema.sql re-applies on fresh volume
```

### Query examples

```bash
# Active cameras
docker exec pai_postgres psql -U policeai -d policeai \
  -c "SELECT camera_id, connection_mode, active, location_name FROM cameras;"

# Recent alerts
docker exec pai_postgres psql -U policeai -d policeai \
  -c "SELECT alert_type, camera_id, status, created_at FROM alerts ORDER BY created_at DESC LIMIT 10;"
```

---

## Common development tasks

### Add a new AI worker

1. Create `services/your-ai/worker.py` (copy structure from `traffic-ai/traffic_ai_worker.py`).
2. Publish to Redis stream `alerts:your_type`.
3. Add stream to `ALERT_STREAMS` in `services/alert-service/main.py`.
4. Add service to `docker-compose.yml`.
5. Add alert type labels in `dashboard/src/lib/constants.js` (`ALERT_LABELS`).

### Rebuild entire stack

```bash
docker compose down
docker compose up -d --build
```

### Check MediaMTX stream status

```bash
curl -s http://localhost:9997/v3/paths/list | python3 -m json.tool
```

### Inspect Redis alert stream

```bash
docker exec pai_redis redis-cli XLEN alerts:traffic
docker exec pai_redis redis-cli XREVRANGE alerts:traffic + - COUNT 3
```

---

## Troubleshooting

### Docker: `veth ... operation not supported` (Arch Linux)

Kernel was upgraded but machine not rebooted. `/lib/modules/` does not match `uname -r`.

```bash
uname -r
ls /lib/modules/
sudo reboot
docker compose up -d --build
```

Or: `./scripts/up.sh`

### WebRTC: ICE failed in browser console

MediaMTX must advertise a host-reachable ICE candidate. Dev config already sets:

- `webrtcAdditionalHosts: [127.0.0.1]`
- Port `8189` published (UDP + TCP) in `docker-compose.yml`

If ICE still fails after MediaMTX restart:

```bash
docker compose restart mediamtx video-ingest
```

Use the **AI** toggle on camera tiles (MJPEG, no WebRTC) or HLS fallback.

### Browser console noise (not from this project)

Messages from `contentscript.js`, `inpage.js`, MetaMask `ObjectMultiplex`, `InstallTrigger` deprecated — these come from **browser extensions**, not Police AI. Ignore them or test in a private window without extensions.

### Camera shows offline / not live in MediaMTX

| Symptom | Fix |
|---------|-----|
| `401 Unauthorized` | Wrong username/password — edit camera in dashboard |
| WHEP 400 on H.265 | Use `_view` relay or H.264 substream |
| Camera reachable on host, not in Docker | Check routing from `pai_mediamtx` container |
| Lost after MediaMTX restart | `docker compose restart video-ingest` |

### traffic-ai slow / low FPS

Expected on CPU (`USE_GPU=false`, ~1 fps). For production, deploy on GPU node with `USE_GPU=true` and TensorRT engine.

### traffic-ai first build very slow

`requirements.txt` installs CPU PyTorch + PaddleOCR dependencies (~1.5 GB). Subsequent builds cache the pip layer.

### drafting service cannot reach LLM

Install [Ollama](https://ollama.com) on the host and pull a model:

```bash
ollama pull llama3.1:8b
```

Compose uses `host.docker.internal:11434` to reach Ollama on the host.

---

## Production deployment notes

Three-node layout (reference architecture):

| Node | Role | Key services |
|------|------|--------------|
| Node B | Ingest | MediaMTX, video-ingest, `ingest_pipeline` |
| Node A | AI | traffic-ai, face-ai, crowd-ai (GPU) |
| Node C | Storage | NFS archive `/mnt/storage/archive/` |

Production MediaMTX config: `infra/mediamtx/mediamtx.yml` (VLAN binding, 30-day recording, cam01–cam32 paths).

On GPU node:

```bash
USE_GPU=true
FRAME_SOURCE=rtsp          # or queue if co-located with ingest_pipeline
YOLO_TRT_ENGINE_PATH=/models/traffic_yolov8m_bd.engine
ANPR_ENABLED=true
MOCK_MODE=false            # face-ai
```

> `docker-compose.prod.yml` is not included in this repo yet — use `docker-compose.yml` as a template and harden secrets, networking, and GPU runtime (`nvidia-container-toolkit`).

---

## Governance rules

These are non-negotiable product constraints:

- AI generates **candidate alerts only** — an officer must Accept or Reject.
- Every officer action is written to the immutable `audit_log` table (no UPDATE/DELETE).
- No alert has legal standing until an officer accepts it.
- Face recognition watchlist access is role-gated and fully logged.
- Raw camera video never leaves the thana node.
- All sensitive data stays on-premise — no third-party cloud APIs for police data.

---

## Key dependencies

### Backend (Python 3.11+)

```
fastapi, uvicorn, asyncpg, redis, httpx, pydantic
opencv-python-headless, numpy, structlog

# traffic-ai (production)
ultralytics, torch, deep-sort-realtime, shapely, paddleocr

# face-ai (production)
insightface, faiss-gpu, onnxruntime-gpu
```

### Frontend (Node 20+)

```
react 18, vite, hls.js
```

---

## License and support

Internal Bangladesh Police / Unicorns Codes project. For development questions, refer to service logs and health endpoints listed above.
