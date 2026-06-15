# Deployment Guide — Police AI Surveillance Stack

This guide covers deploying the full docker-compose stack (PostgreSQL, Redis,
MediaMTX, video-ingest, traffic-ai, face-ai, alert-service, drafting,
Keycloak, dashboard, nginx) to a remote Linux server / VM.

---

## 1. Prerequisites on the target server

- Linux server (Ubuntu/Debian recommended) with a public or LAN-reachable IP
  (e.g. `172.1.0.0`)
- SSH access with a sudo-capable user
- Docker Engine + Docker Compose plugin

Check / install Docker:

```bash
ssh <user>@<server-ip>

# Check if already installed
docker --version
docker compose version

# If missing, install Docker Engine (Ubuntu/Debian)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker
```

> GPU note: the AI workers (`traffic-ai`, `face-ai`) run in CPU/mock mode by
> default (`USE_GPU: "false"`, `MOCK_MODE`/`FRAME_SOURCE` env vars in
> `docker-compose.yml`). For real-time YOLO inference on a GPU node, install
> the NVIDIA Container Toolkit and flip those flags — that is a separate,
> larger change and not covered here.

---

## 2. Get the code onto the server

**Option A — git clone (recommended, repo already on GitHub):**

```bash
ssh <user>@<server-ip>
git clone https://github.com/shamimkhaled/dmp-ai-serviellance-system.git police-ai-starter
cd police-ai-starter
```

**Option B — copy your local working tree (includes any uncommitted changes):**

```bash
# from your local machine
rsync -avz --exclude node_modules --exclude .git \
  /home/shamimkhaled/police-ai-starter/ \
  <user>@<server-ip>:~/police-ai-starter/
```

---

## 3. Configure the server's IP and database

On the server:

```bash
cd ~/police-ai-starter
cp .env.example .env
```

### 3a. `HOST_IP`

The dashboard, MediaMTX (WebRTC/WHEP) and video-ingest all need to know the
IP/hostname that **operator browsers** will use to reach this server. This
is controlled by a single `HOST_IP` variable. Edit `.env`:

```
HOST_IP=172.19.1.8
```

This single value feeds:
- `MTX_WEBRTCADDITIONALHOSTS` (MediaMTX — fixes "ICE failed" for remote browsers)
- `WHEP_BASE_URL`, `HLS_BASE_URL`, `PUBLIC_RTSP_URL` (video-ingest)
- `VITE_ALERT_SERVICE_URL`, `VITE_VIDEO_INGEST_URL`, `VITE_MEDIAMTX_WHEP_URL`,
  `VITE_MEDIAMTX_HLS_URL`, `VITE_TRAFFIC_AI_URL` (dashboard)

If `.env` is absent or `HOST_IP` is unset, everything falls back to
`localhost` / `127.0.0.1` (local-dev behavior, unchanged).

### 3b. `DATABASE_URL` (external PostgreSQL)

This stack does **not** bundle its own PostgreSQL container — every service
(`video-ingest`, `traffic-ai`, `face-ai`, `alert-service`, `drafting`)
connects directly to an external/managed PostgreSQL server (with the
`pgvector` extension available). `DATABASE_URL` is **required** — services
will fail to start without it.

Edit `.env`:

```
DATABASE_URL=postgresql://dmp_user:DMPAI2026@103.146.220.225:5432/dmp_db
```

Before the first start, apply the schema to that database **once**:

```bash
psql "postgresql://dmp_user:DMPAI2026@103.146.220.225:5432/dmp_db" -f db/schema.sql
```

This requires the `vector` and `uuid-ossp` extensions to be installable on
that server (`CREATE EXTENSION IF NOT EXISTS vector;` — the `pgvector`
extension package must be installed on the Postgres server itself, e.g.
`apt install postgresql-16-pgvector` or use a `pgvector/pgvector` image if
that server is also containerized).

On subsequent deploys, `alert-service` runs idempotent
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` migrations on startup, so you
don't need to re-run `schema.sql` for small additive changes.

---

## 4. Open firewall ports

The stack exposes these ports on the host:

| Port        | Service          | Purpose                              |
|-------------|------------------|---------------------------------------|
| 80          | nginx            | Reverse proxy (main entry point)       |
| 3000        | dashboard        | React dev server (direct access)       |
| 8001        | video-ingest     | Camera registry API                    |
| 8002        | traffic-ai       | Traffic AI worker API + preview        |
| 8003        | face-ai          | Face recognition worker API            |
| 8004        | alert-service    | Alert REST + WebSocket                 |
| 8006        | drafting         | GD/FIR drafting service                |
| 8080        | keycloak         | Auth (RBAC)                            |
| 8554        | mediamtx         | RTSP ingest                            |
| 8888        | mediamtx         | HLS playback                           |
| 8889        | mediamtx         | WebRTC/WHEP signaling                  |
| 8189 udp/tcp| mediamtx         | WebRTC ICE media — required for video  |
| 9997        | mediamtx         | MediaMTX API                           |
| 6379        | redis            | Redis (keep internal-only unless needed)|

> PostgreSQL (5432) is on the external DB server (103.146.220.225), not this
> host — ensure that server's firewall allows inbound connections from this
> VM's IP, but does **not** expose 5432 to the public internet.

Using `ufw`:

```bash
sudo ufw allow 80/tcp
sudo ufw allow 3000/tcp
sudo ufw allow 8001:8004/tcp
sudo ufw allow 8006/tcp
sudo ufw allow 8080/tcp
sudo ufw allow 8554/tcp
sudo ufw allow 8888:8889/tcp
sudo ufw allow 8189/tcp
sudo ufw allow 8189/udp
sudo ufw allow 9997/tcp
```

> Do **not** expose 5432 (Postgres) or 6379 (Redis) to the public internet —
> leave them firewalled to localhost/LAN only.

---

## 5. Build and start the stack

```bash
cd ~/police-ai-starter
docker compose up -d --build
```

First build will take a while (pulls base images, builds Python/Node
services). Watch logs:

```bash
docker compose logs -f
```

Check all services are healthy:

```bash
docker compose ps
```

---

## 6. Verify

From your local machine, browse to:

- Dashboard: `http://172.19.1.8:3000` (or `http://172.19.1.8` via nginx)
- Alert service health: `http://172.19.1.8:8004/health`
- Video ingest health: `http://172.19.1.8:8001/health`
- Traffic AI health: `http://172.19.1.8:8002/health`
- MediaMTX API: `http://172.19.1.8:9997/v3/paths/list`

In the dashboard's camera grid, the live video tile for `cam01` should
connect via WHEP (WebRTC) — if it shows "ICE failed" or a black screen,
double-check `HOST_IP` in `.env` matches the IP you're browsing from, then
`docker compose up -d --build mediamtx dashboard video-ingest`.

---

## 7. Production hardening (recommended before real use)

The default `docker-compose.yml` ships with **dev-only secrets**. Before
exposing this to real traffic, change:

- The external PostgreSQL credentials in `DATABASE_URL` — make sure that
  user has access only to `dmp_db`, and that the DB server's firewall only
  allows connections from this VM (and any other trusted hosts)
- `JWT_SECRET` in `alert-service` (currently `dev_jwt_secret_change_in_prod`)
- `KEYCLOAK_ADMIN_PASSWORD` (currently `admin`)
- MediaMTX `authMethod: internal` / `authInternalUsers` in
  `services/video-ingest/mediamtx.yml` currently allows **any** client to
  publish/read streams — restrict this for production

Also consider:
- Putting nginx behind TLS (Let's Encrypt / reverse proxy with HTTPS)
- Restricting the Redis port to localhost (`127.0.0.1:6379:6379`)
- Setting up SSH key-based auth for the server and disabling password auth

---

## 8. Updating a deployed instance

```bash
cd ~/police-ai-starter
git pull
docker compose up -d --build
```

To apply DB schema changes (e.g. new columns), the alert-service runs
idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` migrations on startup,
so a normal restart picks up new columns automatically.
