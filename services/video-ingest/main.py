"""
video_ingest/main.py
════════════════════
Police AI — Camera Registry + Frame Distribution Service (Node B)

Two responsibilities in one process:
  1. FastAPI HTTP layer  — camera CRUD, snapshots, MediaMTX proxy,
                           health endpoint consumed by the dashboard.
  2. IngestService        — one multiprocessing.Process per camera,
                           distributing 640×640 frames to AI workers
                           via per-worker queues; FPS published to
                           Prometheus.  Started/stopped inside the
                           FastAPI asyncio lifespan.

External dependencies (environment-variable driven, no hardcoded values):
  DATABASE_URL / DB_URL   PostgreSQL DSN
  MEDIAMTX_URL            MediaMTX base URL  (default http://localhost:9997)
  MEDIAMTX_API            MediaMTX API base  (overrides MEDIAMTX_URL)
  RTSP_BASE_URL           RTSP re-stream base (default rtsp://localhost:8554)
  WHEP_BASE_URL           WHEP base          (default http://localhost:8889)
  HLS_BASE_URL            HLS base           (default http://localhost:8888)
  PUBLIC_RTSP_URL         Public RTSP URL advertised in API responses
  CAMERA_FPS              Target decode FPS  (default 15, max 25)
  LOG_LEVEL               structlog level    (default INFO)
  USE_FAKE_STREAMS        'true' → FakeStreamIngestor mode
  FAKE_STREAM_FILE        Path to looped .mp4 (default ./sample.mp4)
  HEALTH_HOST / PORT      Pipeline health bind (default 0.0.0.0:8005)
  CAMERA_QUEUE_SIZE       Per-camera queue depth  (default 30)
  WORKER_QUEUE_SIZE       Per-AI-worker queue depth (default 120)

face-ai/worker.py and traffic-ai/worker.py import FrameStream from
this module; that class must remain exported.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import signal
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator

import asyncpg
import cv2
import httpx
import structlog
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from camera_urls import BRAND_TEMPLATES, build_rtsp_url, list_brands
from mediamtx_client import ensure_path, get_path, list_paths, remove_path
from transcode_manager import (
    ensure_h264_relay,
    stop_h264_relay,
    sync_h265_relays,
    view_path_id,
)

# Pipeline classes (multiprocessing ingestors, distributor, registry).
# Imported — not duplicated — to keep a single source of truth.
from ingest_pipeline import (
    CameraConfig,
    CameraRegistry,
    CameraRuntimeState,
    FakeStreamIngestor,
    IngestService,
    RTSPIngestor,
    Settings as PipelineSettings,
    StreamDistributor,
    WebRTCRelay,
    configure_logging as _configure_pipeline_logging,
)

# ── Bootstrap ──────────────────────────────────────────────────────────────────
load_dotenv()


def _configure_structlog(level: str = "INFO") -> None:
    """Configure structlog for JSON output on stdout (Loki-friendly)."""
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=numeric)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
_configure_structlog(_LOG_LEVEL)
log: structlog.BoundLogger = structlog.get_logger("video-ingest")

# ── Configuration ──────────────────────────────────────────────────────────────
DATABASE_URL  = os.getenv("DATABASE_URL") or os.getenv("DB_URL", "")
MEDIAMTX_URL  = os.getenv("MEDIAMTX_URL",  "http://localhost:9997")
RTSP_BASE_URL = os.getenv("RTSP_BASE_URL",  "rtsp://localhost:8554")
WHEP_BASE_URL = os.getenv("WHEP_BASE_URL", "http://localhost:8889")
HLS_BASE_URL  = os.getenv("HLS_BASE_URL",  "http://localhost:8888")
PUBLIC_RTSP   = os.getenv("PUBLIC_RTSP_URL", RTSP_BASE_URL)

if not DATABASE_URL:
    log.error("startup_error", detail="DATABASE_URL / DB_URL must be set")

# Codecs browsers can receive over WebRTC/WHEP (H.265 is ingest-only for most).
WEBRTC_CODECS: frozenset[str] = frozenset(
    {"H264", "VP8", "VP9", "AV1", "Opus", "G711", "MPEG4Audio"}
)

# ── Process-level state ────────────────────────────────────────────────────────
pool: asyncpg.Pool | None = None
_ingest_service: IngestService | None = None
_pipeline_state: dict[str, CameraRuntimeState] = {}


# ── DB migrations (idempotent column additions) ────────────────────────────────
async def _migrate(pg: asyncpg.Pool) -> None:
    stmts = [
        "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS brand VARCHAR(30) DEFAULT 'custom'",
        "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS connection_mode VARCHAR(20) DEFAULT 'pull'",
        "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS host VARCHAR(100)",
        "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS port INT DEFAULT 554",
        "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS username VARCHAR(100)",
        "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS channel INT DEFAULT 1",
    ]
    for sql in stmts:
        await pg.execute(sql)
    log.info("db_migrations_applied")


# ── Lifespan ───────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Startup:  open DB pool → run migrations → sync cameras to MediaMTX
              → start IngestService (one process per camera).
    Shutdown: flush queues → join child processes → audit log → close pool.
    """
    global pool, _ingest_service

    # 1. Database
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    await _migrate(pool)
    log.info("db_pool_ready")

    # 2. Sync existing cameras to MediaMTX paths (idempotent on restart).
    await _sync_all_cameras_to_mediamtx()

    # 3. Background health loop (updates last_seen_at + H.265 relay management).
    health_task = asyncio.create_task(_camera_health_loop(), name="health-loop")

    # 4. IngestService — multiprocessing frame pipeline.
    try:
        pipeline_settings = PipelineSettings.from_env()
        _ingest_service = IngestService(pipeline_settings)
        await _ingest_service.start()
        log.info("ingest_pipeline_started")
    except Exception as exc:
        log.warning("ingest_pipeline_skipped", reason=str(exc))
        _ingest_service = None

    # 5. Signal handlers so SIGTERM triggers clean shutdown.
    loop = asyncio.get_running_loop()

    def _on_signal(sig: str) -> None:
        log.info("signal_received", signal=sig)
        loop.create_task(_graceful_shutdown())

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _on_signal, sig.name)

    yield  # ─── application serves requests ───────────────────────────────────

    # Shutdown
    health_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await health_task

    if _ingest_service is not None:
        await _ingest_service.shutdown()
        log.info("ingest_pipeline_stopped")

    if pool is not None:
        await pool.close()
    log.info("shutdown_complete")


async def _graceful_shutdown() -> None:
    """Called on SIGTERM/SIGINT; complements FastAPI's own shutdown path."""
    if _ingest_service is not None:
        await _ingest_service.shutdown()


# ── FastAPI app ────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Police AI – Video Ingest Service",
    version="2.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RuntimeError)
async def _runtime_error(_request: Request, exc: RuntimeError) -> JSONResponse:
    log.error("runtime_error", detail=str(exc))
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def _generic_error(_request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled_error", detail=str(exc))
    return JSONResponse(status_code=500, content={"detail": str(exc)})


# ── Pydantic models ────────────────────────────────────────────────────────────
class CameraConnect(BaseModel):
    camera_id: str = Field(..., pattern=r"^[a-zA-Z0-9_-]{2,32}$")
    name: str
    brand: str = "custom"
    connection_mode: str = "pull"   # "pull" | "publish"
    host: str | None = None
    port: int | None = 554
    username: str | None = None
    password: str | None = None
    channel: int = 1
    rtsp_url: str | None = None
    location_name: str | None = None
    zone_type: str = "entry_exit"
    latitude: float | None = None
    longitude: float | None = None


class CameraUpdate(BaseModel):
    name: str | None = None
    brand: str | None = None
    connection_mode: str | None = None
    host: str | None = None
    port: int | None = None
    username: str | None = None
    password: str | None = None
    channel: int | None = None
    rtsp_url: str | None = None
    location_name: str | None = None
    zone_type: str | None = None
    latitude: float | None = None
    longitude: float | None = None


# ── Helpers ────────────────────────────────────────────────────────────────────
def _validate_rtsp_url(url: str) -> None:
    from urllib.parse import urlparse as _parse

    p = _parse(url.strip())
    if p.scheme not in ("rtsp", "rtsps"):
        raise ValueError("RTSP URL must start with rtsp:// or rtsps://")
    if not p.hostname:
        raise ValueError("RTSP URL must include camera IP or hostname")
    if not p.path or p.path in ("/", ""):
        raise ValueError(
            "RTSP URL must include a stream path — "
            "e.g. /Streaming/Channels/101 or /cam/realmonitor?channel=1&subtype=0"
        )


def _camera_row_to_dict(row: asyncpg.Record) -> dict:
    cam = dict(row)
    if cam.get("last_seen_at"):
        cam["last_seen_at"] = cam["last_seen_at"].isoformat()
    cam["publish_url"] = (
        f"{PUBLIC_RTSP}/{cam['camera_id']}"
        if cam.get("connection_mode") == "publish"
        else None
    )
    return cam


def _apply_playback_urls(cam: dict, playback_id: str) -> None:
    cam["playback_id"] = playback_id
    cam["whep_url"] = f"{WHEP_BASE_URL}/{playback_id}/whep"
    cam["hls_url"] = f"{HLS_BASE_URL}/{playback_id}/index.m3u8"


async def _enrich_camera_status(cam: dict, path: dict | None) -> dict:
    """Derive stream_status, status_message, and playback URLs from MediaMTX path state."""
    cid = cam["camera_id"]
    mode = cam.get("connection_mode") or "pull"
    ready = bool(path and path.get("ready"))
    tracks: list[str] = (path or {}).get("tracks") or []
    video_codec = next(
        (t for t in tracks if t not in ("Opus", "G711", "MPEG4Audio")), None
    )
    cam["video_codec"] = video_codec
    cam["webrtc_compatible"] = not video_codec or video_codec in WEBRTC_CODECS
    playback_id = cid

    if ready:
        cam["streaming"] = True
        cam["stream_status"] = "live"
        if video_codec == "H265":
            view = view_path_id(cid)
            view_path = await get_path(MEDIAMTX_URL, view)
            if view_path and view_path.get("ready"):
                playback_id = view
                cam["webrtc_compatible"] = True
                cam["playback_mode"] = "whep"
                cam["status_message"] = (
                    "Live (H.265 ingested, H.264 relay for browser playback)"
                )
            else:
                cam["playback_mode"] = "hls"
                cam["status_message"] = (
                    "Live (H.265) — starting H.264 relay for browser playback…"
                )
        else:
            cam["playback_mode"] = "whep"
            cam["status_message"] = "Video stream active"
    elif mode == "publish":
        cam.update(
            stream_status="waiting",
            playback_mode="none",
            streaming=False,
            status_message=(
                f"Registered — configure camera to push RTSP to: {PUBLIC_RTSP}/{cid}"
            ),
        )
    else:
        cam.update(
            stream_status="error",
            playback_mode="none",
            streaming=False,
            status_message=(
                "Registered but no video yet. Check: (1) camera IP reachable from "
                "this server, (2) RTSP URL includes stream path, (3) username/password "
                "correct (ffprobe may show 401 Unauthorized), "
                "(4) camera on same network/VLAN as Docker host."
            ),
        )

    _apply_playback_urls(cam, playback_id)
    return cam


async def _wait_path_ready(camera_id: str, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        items = await list_paths(MEDIAMTX_URL)
        for p in items:
            if p.get("name") == camera_id and p.get("ready"):
                return True
        await asyncio.sleep(1)
    return False


def _resolve_source(
    brand: str,
    *,
    host: str | None,
    port: int | None,
    username: str | None,
    password: str | None,
    channel: int,
    rtsp_url: str | None,
    existing_rtsp: str | None = None,
) -> str:
    pwd = password
    if not pwd and existing_rtsp and brand != "custom":
        from urllib.parse import urlparse as _parse
        pwd = _parse(existing_rtsp).password
    return build_rtsp_url(
        brand,
        host=host,
        port=port,
        username=username,
        password=pwd,
        channel=channel,
        rtsp_url=rtsp_url or (existing_rtsp if brand == "custom" else None),
    )


def _rtsp_path_hint(url: str) -> str:
    from urllib.parse import urlparse as _parse
    path = (_parse(url).path or "").lower()
    if path in ("/stream1", "/stream2"):
        return (
            " Tip: /stream1 is TP-Link/Tapo format — Hikvision uses "
            "/Streaming/Channels/101, Dahua uses /cam/realmonitor?channel=1&subtype=0."
        )
    return ""


async def _digest_auth_check(url: str) -> bool:
    """
    Perform a full RTSP Digest-auth handshake over raw TCP.
    Returns True if the embedded credentials are accepted by the camera,
    False if the camera returns 401 even after the correct Digest response.
    This confirms whether the password itself is wrong (vs a network/path issue).
    """
    import hashlib
    import socket
    from urllib.parse import urlparse as _parse

    p = _parse(url)
    host, port = p.hostname, p.port or 554
    user, pwd  = p.username or "", p.password or ""
    if not host:
        return False

    loop = asyncio.get_event_loop()

    def _do_handshake() -> bool:
        try:
            s = socket.create_connection((host, port), timeout=5)
            # Step 1 – DESCRIBE without auth to get the 401 + WWW-Authenticate
            s.sendall(
                f"DESCRIBE {url} RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\n\r\n"
                .encode()
            )
            r1 = s.recv(4096).decode(errors="replace")
            if "401" not in r1:
                s.close()
                return True  # no auth required — already OK

            # Parse realm + nonce
            realm = nonce = ""
            for part in r1.split(","):
                pt = part.strip()
                if "realm=" in pt:
                    realm = pt.split("realm=")[1].strip().strip('"')
                if "nonce=" in pt:
                    nonce = pt.split("nonce=")[1].strip().strip('"')
            if not nonce:
                s.close()
                return False

            # Step 2 – compute Digest response and re-send DESCRIBE
            ha1 = hashlib.md5(f"{user}:{realm}:{pwd}".encode()).hexdigest()
            ha2 = hashlib.md5(f"DESCRIBE:{url}".encode()).hexdigest()
            resp_hash = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
            auth = (
                f'Digest username="{user}", realm="{realm}", '
                f'nonce="{nonce}", uri="{url}", response="{resp_hash}"'
            )
            s.sendall(
                f"DESCRIBE {url} RTSP/1.0\r\nCSeq: 2\r\nAccept: application/sdp\r\n"
                f"Authorization: {auth}\r\n\r\n".encode()
            )
            r2 = s.recv(8192).decode(errors="replace")
            s.close()
            # 200 OK = credentials accepted
            return r2.startswith("RTSP/1.0 200")
        except Exception:
            return False

    return await loop.run_in_executor(None, _do_handshake)


async def _probe_rtsp(url: str, camera_id: str | None = None) -> str | None:
    """
    Diagnose an RTSP URL and return a human-readable error, or None on success.
    On 401: performs a full Digest handshake to distinguish wrong-password from
    camera-lockout, then pauses MediaMTX retries (sourceOnDemand=true) to stop
    hammering the camera and avoid account lockout.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-rtsp_transport", "tcp",
            "-show_entries", "format=format_name",
            "-of", "default=noprint_wrappers=1:nokey=1",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
    except asyncio.TimeoutError:
        return "RTSP probe timed out — camera unreachable from the video-ingest container."
    except FileNotFoundError:
        return None  # ffprobe not installed

    if proc.returncode == 0:
        return None

    err = stderr.decode(errors="replace")
    if "401" in err or "Unauthorized" in err:
        # Verify credentials via raw Digest handshake
        creds_ok = await _digest_auth_check(url)
        if creds_ok:
            # Credentials are valid — transient 401 (camera warming up or path wrong)
            return (
                "Camera accepted credentials but stream is not ready yet. "
                "Wait 10–20 s and click Test again. "
                "If it persists, check the RTSP stream path (e.g. /Streaming/Channels/101)."
                + _rtsp_path_hint(url)
            )
        # Credentials genuinely rejected — pause MediaMTX retries immediately
        # to stop locking out the camera account with repeated failed auths.
        if camera_id:
            asyncio.create_task(_pause_mediamtx_retries(camera_id))

        return (
            "RTSP authentication failed — wrong username or password.\n\n"
            "What to check:\n"
            "1. Log in to the camera web interface (http://192.168.68.2) and verify the admin password.\n"
            "2. Some cameras need a separate 'streaming user' account — check Security → User settings.\n"
            "3. If the camera shows 'account locked', wait 30 minutes or reset via web UI → Security → User.\n"
            "4. Hikvision cameras require a STRONG password (≥8 chars, upper+lower+digit+symbol).\n\n"
            "Auto-retry has been paused to prevent further account lockout. "
            "Edit and re-save the camera after correcting the credentials."
            + _rtsp_path_hint(url)
        )
    if "Connection refused" in err:
        return "Connection refused — wrong IP/port or RTSP disabled on the camera."
    if "No route to host" in err or "Network is unreachable" in err:
        return "Camera IP unreachable from the server — check network/VLAN/firewall."
    if "404" in err or "Not Found" in err:
        return "RTSP path not found — check stream URL (e.g. /Streaming/Channels/101)."
    line = next((ln.strip() for ln in err.splitlines() if ln.strip()), "")
    return (line[:240] if line else "RTSP probe failed — verify URL and credentials.")


async def _restore_mediamtx_retries(camera_id: str) -> None:
    """Undo a previous pause — re-enable active pull so MediaMTX retries immediately."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.patch(
                f"{MEDIAMTX_URL}/v3/config/paths/patch/{camera_id}",
                json={"sourceOnDemand": False},
            )
    except Exception:
        pass


async def _pause_mediamtx_retries(camera_id: str) -> None:
    """
    Switch a camera path to sourceOnDemand=true so MediaMTX stops retrying
    the RTSP connection every 5 s with wrong credentials.
    Without this, repeated 401s can trigger the camera's account-lockout policy.
    The path is restored to active pull when the camera is re-saved via PATCH.
    """
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.patch(
                f"{MEDIAMTX_URL}/v3/config/paths/patch/{camera_id}",
                json={"sourceOnDemand": True},
            )
        log.warning(
            "mediamtx_retries_paused",
            camera_id=camera_id,
            reason="401 Unauthorized — wrong credentials detected via Digest handshake",
        )
    except Exception as exc:
        log.warning("pause_retries_failed", camera_id=camera_id, error=str(exc))


# ── MediaMTX synchronisation ───────────────────────────────────────────────────
async def _register_mediamtx_path(
    camera_id: str, source: str, mode: str = "pull"
) -> None:
    on_demand = mode == "publish" or source == "publisher"
    await ensure_path(
        MEDIAMTX_URL,
        camera_id,
        source=source,
        source_on_demand=on_demand if source != "publisher" else False,
    )


async def _sync_all_cameras_to_mediamtx() -> None:
    try:
        rows = await pool.fetch(
            "SELECT camera_id, rtsp_url, connection_mode FROM cameras WHERE active=TRUE"
        )
        for row in rows:
            source = "publisher" if row["connection_mode"] == "publish" else row["rtsp_url"]
            try:
                await _register_mediamtx_path(row["camera_id"], source, row["connection_mode"])
            except Exception as exc:
                log.warning("sync_skip", camera_id=row["camera_id"], reason=str(exc))
    except Exception as exc:
        log.warning("startup_sync_failed", reason=str(exc))


async def _get_active_streams() -> set[str]:
    try:
        items = await list_paths(MEDIAMTX_URL)
        return {p["name"] for p in items if p.get("ready")}
    except Exception as exc:
        log.warning("mediamtx_health_check_failed", reason=str(exc))
        return set()


async def _camera_health_loop() -> None:
    """
    Every 15 s:
      - Sync H.265 relay processes.
      - Update last_seen_at for live cameras.
      - Log offline cameras.
    """
    while True:
        try:
            rows = await pool.fetch("SELECT camera_id FROM cameras WHERE active = TRUE")
            camera_ids = [r["camera_id"] for r in rows]
            await sync_h265_relays(MEDIAMTX_URL, RTSP_BASE_URL, camera_ids)
            active = await _get_active_streams() & set(camera_ids)
            if active:
                await pool.execute(
                    "UPDATE cameras SET last_seen_at = NOW() "
                    "WHERE camera_id = ANY($1::text[])",
                    list(active),
                )
            offline_rows = await pool.fetch(
                "SELECT camera_id FROM cameras WHERE active = TRUE "
                "AND (last_seen_at IS NULL OR last_seen_at < NOW() - INTERVAL '2 minutes')"
            )
            for r in offline_rows:
                log.warning("camera_offline", camera_id=r["camera_id"])
        except Exception as exc:
            log.error("health_loop_error", detail=str(exc))
        await asyncio.sleep(15)


# ── Routes: root / health ──────────────────────────────────────────────────────
@app.get("/")
async def root() -> dict:
    return {
        "service": "video-ingest",
        "version": "2.0",
        "endpoints": {
            "cameras":  "/cameras",
            "brands":   "/cameras/brands",
            "connect":  "POST /cameras/connect",
            "update":   "PATCH /cameras/{camera_id}",
            "delete":   "DELETE /cameras/{camera_id}",
            "test":     "POST /cameras/{camera_id}/test",
            "snapshot": "GET /cameras/{camera_id}/snapshot",
            "health":   "/health",
        },
        "whep_base": WHEP_BASE_URL,
        "hls_base":  HLS_BASE_URL,
    }


@app.get("/health")
async def health() -> dict:
    """
    Returns stream counts from MediaMTX plus per-camera pipeline metrics
    if IngestService is running.
    """
    active = await _get_active_streams()

    pipeline: dict = {}
    if _ingest_service is not None and _pipeline_state:
        pipeline = {
            cid: {
                "status":        st.status,
                "fps":           round(st.fps, 2),
                "last_frame_ts": st.last_frame_ts,
            }
            for cid, st in _pipeline_state.items()
        }

    return {
        "status":         "ok",
        "service":        "video-ingest",
        "active_streams": len(active),
        "live_cameras":   sorted(active),
        "pipeline":       pipeline,
    }


# ── Routes: camera brands ──────────────────────────────────────────────────────
@app.get("/cameras/brands")
async def camera_brands() -> dict:
    return {"brands": list_brands()}


# ── Routes: camera CRUD ────────────────────────────────────────────────────────
@app.get("/cameras")
async def list_cameras() -> list[dict]:
    rows = await pool.fetch(
        """SELECT camera_id, name, brand, connection_mode, location_name,
                  zone_type, last_seen_at, active, host, port, channel, username
           FROM cameras WHERE active = TRUE ORDER BY camera_id"""
    )
    cameras: list[dict] = []
    for r in rows:
        cam = _camera_row_to_dict(r)
        path = await get_path(MEDIAMTX_URL, cam["camera_id"])
        cameras.append(await _enrich_camera_status(cam, path))
    return cameras


@app.get("/cameras/{camera_id}")
async def get_camera(camera_id: str) -> dict:
    row = await pool.fetchrow(
        """SELECT camera_id, name, brand, connection_mode, location_name,
                  zone_type, last_seen_at, active, host, port, channel,
                  username, rtsp_url, latitude, longitude
           FROM cameras WHERE camera_id=$1 AND active=TRUE""",
        camera_id,
    )
    if not row:
        raise HTTPException(404, "Camera not found")
    cam = _camera_row_to_dict(row)
    cam["rtsp_url"] = row["rtsp_url"] if cam.get("brand") == "custom" else ""
    path = await get_path(MEDIAMTX_URL, camera_id)
    cam = await _enrich_camera_status(cam, path)
    if (
        cam["stream_status"] != "live"
        and cam.get("connection_mode") == "pull"
        and row["rtsp_url"]
    ):
        probe = await _probe_rtsp(row["rtsp_url"], camera_id)
        if probe:
            cam["status_message"] = probe
    return cam


@app.post("/cameras/connect", status_code=201)
async def connect_camera(data: CameraConnect) -> dict:
    """
    Universal camera registration.
      pull    — MediaMTX pulls RTSP from the camera.
      publish — Camera pushes to rtsp://<server>:8554/<camera_id>.
    """
    mode = data.connection_mode.lower()
    brand = data.brand.lower()

    if brand not in BRAND_TEMPLATES:
        raise HTTPException(400, "Unknown brand. Use GET /cameras/brands")

    if mode == "publish":
        source_url = "publisher"
        stored_url = f"{PUBLIC_RTSP}/{data.camera_id}"
    else:
        try:
            source_url = build_rtsp_url(
                brand,
                host=data.host,
                port=data.port,
                username=data.username,
                password=data.password,
                channel=data.channel,
                rtsp_url=data.rtsp_url,
            )
            _validate_rtsp_url(source_url)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        stored_url = source_url

    await pool.execute(
        """INSERT INTO cameras
           (camera_id, name, rtsp_url, brand, connection_mode, host, port,
            username, channel, location_name, zone_type, latitude, longitude, active)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,TRUE)
           ON CONFLICT (camera_id) DO UPDATE SET
             name=$2, rtsp_url=$3, brand=$4, connection_mode=$5, host=$6, port=$7,
             username=$8, channel=$9, location_name=$10, zone_type=$11,
             latitude=$12, longitude=$13, active=TRUE""",
        data.camera_id, data.name, stored_url, brand, mode,
        data.host, data.port or 554, data.username, data.channel,
        data.location_name, data.zone_type, data.latitude, data.longitude,
    )
    await _register_mediamtx_path(data.camera_id, source_url, mode)

    log.info("camera_connected", camera_id=data.camera_id, brand=brand, mode=mode)

    stream_ok = False
    if mode == "pull":
        stream_ok = await _wait_path_ready(data.camera_id, timeout=12)
    elif mode == "publish":
        stream_ok = await _wait_path_ready(data.camera_id, timeout=3)

    path = await get_path(MEDIAMTX_URL, data.camera_id)
    if path and path.get("ready"):
        await ensure_h264_relay(MEDIAMTX_URL, RTSP_BASE_URL, data.camera_id)

    status_info = await _enrich_camera_status(
        {"camera_id": data.camera_id, "connection_mode": mode}, path
    )
    return {
        "camera_id":       data.camera_id,
        "status":          "connected",
        "connection_mode": mode,
        "stream_status":   status_info["stream_status"],
        "status_message":  status_info["status_message"],
        "stream_ok":       stream_ok or status_info["stream_status"] == "live",
        "whep_url":        status_info["whep_url"],
        "hls_url":         status_info["hls_url"],
        "playback_id":     status_info.get("playback_id"),
        "publish_url":     f"{PUBLIC_RTSP}/{data.camera_id}" if mode == "publish" else None,
        "instructions":    status_info["status_message"],
    }


@app.patch("/cameras/{camera_id}")
async def update_camera(camera_id: str, data: CameraUpdate) -> dict:
    row = await pool.fetchrow(
        "SELECT * FROM cameras WHERE camera_id=$1 AND active=TRUE", camera_id
    )
    if not row:
        raise HTTPException(404, "Camera not found")

    brand = (data.brand or row["brand"] or "custom").lower()
    mode  = (data.connection_mode or row["connection_mode"] or "pull").lower()

    if brand not in BRAND_TEMPLATES:
        raise HTTPException(400, "Unknown brand. Use GET /cameras/brands")

    name          = data.name          if data.name          is not None else row["name"]
    host          = data.host          if data.host          is not None else row["host"]
    port          = data.port          if data.port          is not None else (row["port"] or 554)
    username      = data.username      if data.username      is not None else row["username"]
    channel       = data.channel       if data.channel       is not None else (row["channel"] or 1)
    location_name = data.location_name if data.location_name is not None else row["location_name"]
    zone_type     = data.zone_type     if data.zone_type     is not None else row["zone_type"]
    latitude      = data.latitude      if data.latitude      is not None else row["latitude"]
    longitude     = data.longitude     if data.longitude     is not None else row["longitude"]
    rtsp_url      = data.rtsp_url      if data.rtsp_url      is not None else row["rtsp_url"]

    if mode == "publish":
        source_url = "publisher"
        stored_url = f"{PUBLIC_RTSP}/{camera_id}"
    else:
        try:
            stored_url = _resolve_source(
                brand,
                host=host, port=port, username=username,
                password=data.password, channel=channel,
                rtsp_url=rtsp_url if brand == "custom" else None,
                existing_rtsp=row["rtsp_url"],
            )
            source_url = stored_url
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    await pool.execute(
        """UPDATE cameras SET
             name=$2, rtsp_url=$3, brand=$4, connection_mode=$5, host=$6, port=$7,
             username=$8, channel=$9, location_name=$10, zone_type=$11,
             latitude=$12, longitude=$13
           WHERE camera_id=$1""",
        camera_id, name, stored_url, brand, mode,
        host, port, username, channel,
        location_name, zone_type, latitude, longitude,
    )
    # Restore active pull (undo any auth-error pause) before re-registering.
    await _restore_mediamtx_retries(camera_id)
    await _register_mediamtx_path(camera_id, source_url, mode)
    log.info("camera_updated", camera_id=camera_id)
    return {"camera_id": camera_id, "status": "updated"}


@app.delete("/cameras/{camera_id}")
async def disconnect_camera(camera_id: str) -> dict:
    await pool.execute(
        "UPDATE cameras SET active=FALSE WHERE camera_id=$1", camera_id
    )
    await stop_h264_relay(MEDIAMTX_URL, camera_id)
    await remove_path(MEDIAMTX_URL, camera_id)
    log.info("camera_disconnected", camera_id=camera_id)
    return {"camera_id": camera_id, "status": "disconnected"}


@app.post("/cameras/{camera_id}/test", response_model=None)
async def test_camera(camera_id: str) -> JSONResponse | dict:
    """
    Test camera stream via MediaMTX; run ffprobe on pull cameras for
    a human-readable error when the stream is not live.
    Returns HTTP 200 in all cases — ok=True/False distinguishes success.
    """
    row = await pool.fetchrow(
        "SELECT camera_id, rtsp_url, connection_mode FROM cameras "
        "WHERE camera_id=$1 AND active=TRUE",
        camera_id,
    )
    if not row:
        raise HTTPException(404, "Camera not found")

    mode = row["connection_mode"]
    path = await get_path(MEDIAMTX_URL, camera_id)
    info = await _enrich_camera_status(
        {"camera_id": camera_id, "connection_mode": mode}, path
    )

    if info["stream_status"] != "live":
        detail = info["status_message"]
        if mode == "pull" and row["rtsp_url"]:
            probe = await _probe_rtsp(row["rtsp_url"], camera_id)
            if probe:
                detail = probe
        log.info(
            "camera_test_failed",
            camera_id=camera_id,
            stream_status=info["stream_status"],
        )
        return JSONResponse(
            status_code=200,
            content={
                "camera_id":     camera_id,
                "ok":            False,
                "stream_status": info["stream_status"],
                "status_message": detail,
                "error":         detail,
            },
        )

    frame_b64 = capture_snapshot(f"{RTSP_BASE_URL}/{camera_id}")
    log.info("camera_test_ok", camera_id=camera_id, snapshot=bool(frame_b64))
    return {
        "camera_id":     camera_id,
        "ok":            True,
        "stream_status": info["stream_status"],
        "status_message": info["status_message"],
        "video_codec":   info.get("video_codec"),
        "playback_mode": info.get("playback_mode"),
        "frame_base64":  frame_b64 or "",
    }


@app.get("/cameras/{camera_id}/snapshot")
async def get_snapshot(camera_id: str) -> dict:
    url = f"{RTSP_BASE_URL}/{camera_id}"
    frame_b64 = capture_snapshot(url)
    if not frame_b64:
        raise HTTPException(404, "Could not capture frame — camera may be offline")
    return {"camera_id": camera_id, "frame_base64": frame_b64}


# ── Frame utilities (exported for AI workers) ──────────────────────────────────
def capture_snapshot(rtsp_url: str, timeout_ms: int = 8000) -> str | None:
    """
    Open an RTSP stream, read one frame, encode as JPEG base64.
    Returns None if the stream cannot be opened or a frame cannot be read.
    """
    cap = cv2.VideoCapture(rtsp_url)
    cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms)
    cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_ms)
    ret, frame = cap.read()
    cap.release()
    if not ret or frame is None:
        return None
    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return base64.b64encode(buf).decode()


class FrameStream:
    """
    Continuous RTSP frame reader for AI workers (face-ai, traffic-ai).

    Usage::
        async for frame, ts in FrameStream(rtsp_url, target_fps=5).read():
            ...

    The stream reconnects automatically on read failure.
    """

    def __init__(self, rtsp_url: str, target_fps: int = 5) -> None:
        self.rtsp_url   = rtsp_url
        self.target_fps = max(1, min(target_fps, 25))
        self._interval  = 1.0 / self.target_fps

    async def read(self) -> AsyncGenerator:
        loop = asyncio.get_event_loop()
        cap  = await loop.run_in_executor(None, self._open_capture)
        if not cap or not cap.isOpened():
            log.error("frame_stream_open_failed", url=self.rtsp_url)
            return

        log.info("frame_stream_opened", url=self.rtsp_url, fps=self.target_fps)
        try:
            while True:
                t0 = time.monotonic()
                ret, frame = await loop.run_in_executor(None, cap.read)
                if not ret or frame is None:
                    log.warning("frame_read_failed", url=self.rtsp_url)
                    await asyncio.sleep(2)
                    cap.release()
                    cap = await loop.run_in_executor(None, self._open_capture)
                    continue
                yield frame, time.time()
                wait = self._interval - (time.monotonic() - t0)
                if wait > 0:
                    await asyncio.sleep(wait)
        finally:
            cap.release()

    def _open_capture(self) -> cv2.VideoCapture:
        os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
        cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 8000)
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 8000)
        return cap


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8001,
        reload=False,
        log_config=None,   # structlog owns all logging
    )
