"""
video_ingest/ingest_pipeline.py
───────────────────────────────
Production-grade video ingestion pipeline for the Police AI surveillance layer
(Node B). This is a standalone process, separate from the FastAPI camera-registry
service in `main.py`:

    CameraRegistry      – loads cameras from PostgreSQL, health-checks RTSP
                          endpoints every 10s, records status, audits transitions.
    RTSPIngestor        – one multiprocessing.Process per camera. Decodes frames
                          via cv2.VideoCapture(..., CAP_FFMPEG, rtsp_transport=tcp),
                          resizes to 640x640, tags, and pushes to a per-camera
                          multiprocessing.Queue (drops, never blocks).
    FakeStreamIngestor  – local-dev replacement that loops a local .mp4 file.
    StreamDistributor   – fans every camera frame out to the AI worker queues
                          (traffic / crowd / face / emergency) and publishes
                          per-camera FPS to Prometheus.
    WebRTCRelay         – talks to the MediaMTX REST API (/v3) to register paths
                          and verify stream readiness.
    HealthServer        – GET /health and GET /metrics (aiohttp).

Runtime model:
    * asyncio for health checks, MediaMTX API calls, distribution and HTTP.
    * multiprocessing.Process per camera ingestor (GIL bypass for decode).
    * structlog JSON logging everywhere (no print statements).
    * configuration purely from environment (python-dotenv).

Environment variables:
    DB_URL              PostgreSQL DSN          (fallback: DATABASE_URL)
    MEDIAMTX_API        MediaMTX API base       (fallback: MEDIAMTX_URL, +/v3)
    CAMERA_FPS          target decode FPS       (default 15, capped at 25)
    LOG_LEVEL           structlog level         (default INFO)
    USE_FAKE_STREAMS    'true' -> FakeStreamIngestor
    FAKE_STREAM_FILE    path to looped .mp4     (default ./sample.mp4)
    HEALTH_HOST/PORT    health+metrics bind     (default 0.0.0.0:8005)
    CAMERA_QUEUE_SIZE   per-camera queue depth  (default 30)
    WORKER_QUEUE_SIZE   per-worker queue depth  (default 120)
"""

from __future__ import annotations

import asyncio
import contextlib
import multiprocessing as mp
import os
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from multiprocessing.synchronize import Event as EventType
from queue import Empty, Full
from typing import Callable, Protocol
from urllib.parse import urlparse

import asyncpg
import cv2
import httpx
import numpy as np
import structlog
from aiohttp import web
from dotenv import load_dotenv
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
def configure_logging(level: str = "INFO") -> None:
    """Configure structlog for JSON output on stdout. Safe to call per-process."""
    import logging

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


log = structlog.get_logger("video-ingest")

# Codecs the WHEP/WebRTC layer can hand to browsers (kept for parity with main.py).
WORKER_NAMES: tuple[str, ...] = ("traffic", "crowd", "face", "emergency")

VIDEO_INGEST_FPS = Gauge(
    "video_ingest_fps", "Decoded frames per second per camera", ["camera_id"]
)
VIDEO_INGEST_FANOUT_DROPS = Counter(
    "video_ingest_fanout_dropped_total",
    "Frames dropped while fanning out to a saturated AI worker queue",
    ["camera_id", "worker"],
)
VIDEO_INGEST_FRAMES = Counter(
    "video_ingest_frames_total", "Frames distributed to workers", ["camera_id"]
)


# ── Configuration ───────────────────────────────────────────────────────────--
@dataclass(frozen=True)
class Settings:
    db_url: str
    mediamtx_api: str
    camera_fps: int
    log_level: str
    use_fake_streams: bool
    fake_stream_file: str
    health_host: str
    health_port: int
    camera_queue_size: int
    worker_queue_size: int
    frame_size: tuple[int, int] = (640, 640)
    max_fps: int = 25

    @classmethod
    def from_env(cls) -> "Settings":
        db_url = os.getenv("DB_URL") or os.getenv("DATABASE_URL")
        if not db_url:
            raise RuntimeError("DB_URL (or DATABASE_URL) must be set — no hardcoded DSNs allowed")

        api = os.getenv("MEDIAMTX_API") or os.getenv("MEDIAMTX_URL")
        if not api:
            raise RuntimeError("MEDIAMTX_API (or MEDIAMTX_URL) must be set")
        api = api.rstrip("/")
        if not api.endswith("/v3"):
            api = f"{api}/v3"

        fps = int(os.getenv("CAMERA_FPS", "15"))
        fps = max(1, min(fps, 25))

        return cls(
            db_url=db_url,
            mediamtx_api=api,
            camera_fps=fps,
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            use_fake_streams=os.getenv("USE_FAKE_STREAMS", "false").lower() == "true",
            fake_stream_file=os.getenv("FAKE_STREAM_FILE", "./sample.mp4"),
            health_host=os.getenv("HEALTH_HOST", "0.0.0.0"),
            health_port=int(os.getenv("HEALTH_PORT", "8005")),
            camera_queue_size=int(os.getenv("CAMERA_QUEUE_SIZE", "30")),
            worker_queue_size=int(os.getenv("WORKER_QUEUE_SIZE", "120")),
        )


@dataclass(frozen=True)
class CameraConfig:
    camera_id: str
    name: str
    rtsp_url: str
    location_tag: str | None = None
    vlan: int | None = None
    zone_type: str | None = None
    enabled: bool = True


def _now_ms() -> int:
    return int(time.time() * 1000)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Frame sources (injectable for testing) ─────────────────────────────────────
class FrameSource(Protocol):
    """Abstraction over a video source so ingestors are unit-testable with mocks."""

    is_file: bool

    def open(self) -> bool: ...
    def read(self) -> tuple[bool, np.ndarray | None]: ...
    def release(self) -> None: ...


class CV2FrameSource:
    """OpenCV/FFmpeg-backed source. RTSP forced over TCP (more reliable for CCTV)."""

    def __init__(
        self,
        uri: str,
        *,
        is_file: bool = False,
        open_timeout_ms: int = 8000,
        read_timeout_ms: int = 8000,
    ) -> None:
        self.uri = uri
        self.is_file = is_file
        self._open_timeout_ms = open_timeout_ms
        self._read_timeout_ms = read_timeout_ms
        self._cap: cv2.VideoCapture | None = None

    def open(self) -> bool:
        if not self.is_file:
            # Applied to the FFmpeg backend at capture-open time.
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
        cap = cv2.VideoCapture(self.uri, cv2.CAP_FFMPEG)
        if not self.is_file:
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, self._open_timeout_ms)
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, self._read_timeout_ms)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # always serve the freshest frame
        self._cap = cap
        return cap.isOpened()

    def read(self) -> tuple[bool, np.ndarray | None]:
        if self._cap is None:
            return False, None
        return self._cap.read()

    def release(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


SourceFactory = Callable[[CameraConfig], FrameSource]


def rtsp_source_factory(camera: CameraConfig) -> FrameSource:
    return CV2FrameSource(camera.rtsp_url, is_file=False)


def make_fake_source_factory(path: str) -> SourceFactory:
    def factory(_camera: CameraConfig) -> FrameSource:
        return CV2FrameSource(path, is_file=True)

    return factory


# ── RTSP ingestor (runs inside its own Process) ────────────────────────────────
class RTSPIngestor:
    """Decode one camera's stream and push tagged 640x640 frames to a queue.

    Never blocks: when the queue is full the frame is dropped and counted.
    Reconnects with exponential backoff (1,2,4..max 60s) on stream failure.
    """

    def __init__(
        self,
        camera: CameraConfig,
        frame_queue: "mp.Queue",
        stop_event: EventType,
        *,
        target_fps: int = 15,
        max_fps: int = 25,
        frame_size: tuple[int, int] = (640, 640),
        source_factory: SourceFactory = rtsp_source_factory,
        log_level: str = "INFO",
    ) -> None:
        self.camera = camera
        self.frame_queue = frame_queue
        self.stop_event = stop_event
        self.target_fps = max(1, min(target_fps, max_fps))
        self.frame_size = frame_size
        self._source_factory = source_factory
        self._log_level = log_level
        self._interval = 1.0 / self.target_fps
        self._seq = 0
        self._dropped = 0
        self._null_frames = 0
        self._last_drop_report = time.monotonic()

    # -- helpers ----------------------------------------------------------------
    def _make_source(self) -> FrameSource:
        return self._source_factory(self.camera)

    def _tag_and_enqueue(self, frame: np.ndarray) -> None:
        height, width = frame.shape[:2]
        resized = cv2.resize(frame, self.frame_size, interpolation=cv2.INTER_LINEAR)
        message = {
            "camera_id": self.camera.camera_id,
            "timestamp_ms": _now_ms(),
            "frame_seq": self._seq,
            "resolution_orig": (int(width), int(height)),
            "frame": resized,
        }
        self._seq += 1
        try:
            self.frame_queue.put_nowait(message)
        except Full:
            self._dropped += 1

    def _maybe_report_drops(self) -> None:
        elapsed = time.monotonic() - self._last_drop_report
        if elapsed >= 60.0:
            if self._dropped or self._null_frames:
                log.warning(
                    "ingest_drop_report",
                    camera_id=self.camera.camera_id,
                    dropped_frames=self._dropped,
                    null_frames=self._null_frames,
                    window_seconds=round(elapsed, 1),
                )
            self._dropped = 0
            self._null_frames = 0
            self._last_drop_report = time.monotonic()

    # -- main loop --------------------------------------------------------------
    def run(self) -> None:
        configure_logging(self._log_level)
        log.info(
            "ingestor_start",
            camera_id=self.camera.camera_id,
            target_fps=self.target_fps,
            fake=False,
        )
        backoff = 1.0
        source: FrameSource | None = None
        try:
            while not self.stop_event.is_set():
                if source is None:
                    source = self._make_source()
                    if not source.open():
                        log.warning(
                            "ingestor_open_failed",
                            camera_id=self.camera.camera_id,
                            retry_in=backoff,
                        )
                        source.release()
                        source = None
                        self._sleep_interruptible(backoff)
                        backoff = min(backoff * 2, 60.0)
                        continue
                    backoff = 1.0
                    log.info("ingestor_connected", camera_id=self.camera.camera_id)

                t0 = time.monotonic()
                ret, frame = source.read()

                if not ret or frame is None:
                    self._null_frames += 1
                    if source.is_file:
                        # Loop the file: reopen from the start.
                        source.release()
                        source = None
                        continue
                    log.warning(
                        "ingestor_read_failed",
                        camera_id=self.camera.camera_id,
                        retry_in=backoff,
                    )
                    source.release()
                    source = None
                    self._sleep_interruptible(backoff)
                    backoff = min(backoff * 2, 60.0)
                    continue

                self._tag_and_enqueue(frame)
                self._maybe_report_drops()

                # Throttle to the configured FPS.
                wait = self._interval - (time.monotonic() - t0)
                if wait > 0:
                    self._sleep_interruptible(wait)
        finally:
            if source is not None:
                source.release()
            log.info(
                "ingestor_stop",
                camera_id=self.camera.camera_id,
                frames_emitted=self._seq,
            )

    def _sleep_interruptible(self, seconds: float) -> None:
        # Wait on the stop event so shutdown is responsive even during backoff.
        self.stop_event.wait(timeout=seconds)


class FakeStreamIngestor(RTSPIngestor):
    """Local-dev ingestor that loops a local .mp4 file as a fake camera."""

    def __init__(
        self,
        camera: CameraConfig,
        frame_queue: "mp.Queue",
        stop_event: EventType,
        *,
        file_path: str,
        target_fps: int = 15,
        max_fps: int = 25,
        frame_size: tuple[int, int] = (640, 640),
        log_level: str = "INFO",
    ) -> None:
        super().__init__(
            camera,
            frame_queue,
            stop_event,
            target_fps=target_fps,
            max_fps=max_fps,
            frame_size=frame_size,
            source_factory=make_fake_source_factory(file_path),
            log_level=log_level,
        )
        self._file_path = file_path

    def run(self) -> None:
        configure_logging(self._log_level)
        log.info(
            "ingestor_start",
            camera_id=self.camera.camera_id,
            target_fps=self.target_fps,
            fake=True,
            file=self._file_path,
        )
        if not os.path.exists(self._file_path):
            log.error(
                "fake_stream_missing",
                camera_id=self.camera.camera_id,
                file=self._file_path,
            )
            return
        # Reuse the parent loop; the file source loops on EOF.
        super().run()


def _ingestor_process_target(
    camera: CameraConfig,
    frame_queue: "mp.Queue",
    stop_event: EventType,
    settings: Settings,
) -> None:
    """Picklable entrypoint for multiprocessing.Process."""
    if settings.use_fake_streams:
        ingestor: RTSPIngestor = FakeStreamIngestor(
            camera,
            frame_queue,
            stop_event,
            file_path=settings.fake_stream_file,
            target_fps=settings.camera_fps,
            max_fps=settings.max_fps,
            frame_size=settings.frame_size,
            log_level=settings.log_level,
        )
    else:
        ingestor = RTSPIngestor(
            camera,
            frame_queue,
            stop_event,
            target_fps=settings.camera_fps,
            max_fps=settings.max_fps,
            frame_size=settings.frame_size,
            log_level=settings.log_level,
        )
    try:
        ingestor.run()
    except KeyboardInterrupt:
        pass


# ── MediaMTX WebRTC/REST relay ──────────────────────────────────────────────--
class WebRTCRelay:
    """Thin async client for the MediaMTX REST API (/v3)."""

    def __init__(self, api_base: str, client: httpx.AsyncClient) -> None:
        self.api_base = api_base.rstrip("/")
        self._client = client

    async def list_ready_paths(self) -> set[str]:
        try:
            resp = await self._client.get(f"{self.api_base}/paths/list", timeout=5.0)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # network/parse errors are non-fatal for health
            log.warning("mediamtx_list_failed", error=str(exc))
            return set()
        items = data.get("items", []) if isinstance(data, dict) else []
        return {p["name"] for p in items if p.get("ready")}

    async def verify_streams(self, camera_ids: list[str]) -> dict[str, bool]:
        ready = await self.list_ready_paths()
        return {cid: cid in ready for cid in camera_ids}

    async def register_camera(self, camera: CameraConfig) -> bool:
        """Register/ensure a path for a camera on startup (idempotent)."""
        url = camera.rtsp_url or ""
        source = url if url.startswith(("rtsp://", "rtsps://")) and "localhost" not in url else "publisher"
        body = {"source": source, "sourceOnDemand": source != "publisher"}
        try:
            resp = await self._client.post(
                f"{self.api_base}/config/paths/add/{camera.camera_id}",
                json=body,
                timeout=5.0,
            )
            if resp.status_code in (200, 201):
                log.info("mediamtx_path_registered", camera_id=camera.camera_id, source=source)
                return True
            if resp.status_code == 400 and "already" in resp.text.lower():
                return True  # path already exists — fine
            log.warning(
                "mediamtx_register_failed",
                camera_id=camera.camera_id,
                status=resp.status_code,
                body=resp.text[:200],
            )
            return False
        except Exception as exc:
            log.warning("mediamtx_register_error", camera_id=camera.camera_id, error=str(exc))
            return False


# ── Camera registry (PostgreSQL + health) ──────────────────────────────────────
@dataclass
class CameraRuntimeState:
    status: str = "unknown"
    fps: float = 0.0
    last_frame_ts: int | None = None
    backoff: float = 1.0


class CameraRegistry:
    """Loads cameras from PostgreSQL and health-checks them every 10s.

    Injectable: pass an existing asyncpg pool (`pool=`) and/or a custom
    `relay` for unit testing without a live database or MediaMTX.
    """

    HEALTH_INTERVAL_S = 10.0

    def __init__(
        self,
        db_url: str,
        relay: WebRTCRelay,
        state: dict[str, CameraRuntimeState],
        *,
        pool: asyncpg.Pool | None = None,
    ) -> None:
        self._db_url = db_url
        self._relay = relay
        self._state = state
        self._pool = pool
        self._owns_pool = pool is None
        self.cameras: list[CameraConfig] = []

    @property
    def camera_ids(self) -> list[str]:
        return [c.camera_id for c in self.cameras]

    async def connect(self) -> None:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(self._db_url, min_size=1, max_size=5)
        await self._ensure_status_table()

    async def close(self) -> None:
        if self._pool is not None and self._owns_pool:
            await self._pool.close()

    async def _ensure_status_table(self) -> None:
        assert self._pool is not None
        await self._pool.execute(
            """
            CREATE TABLE IF NOT EXISTS camera_status (
                camera_id     VARCHAR(20) PRIMARY KEY,
                status        VARCHAR(20) NOT NULL,
                fps           DOUBLE PRECISION DEFAULT 0,
                last_frame_ts TIMESTAMPTZ,
                detail        TEXT,
                last_checked  TIMESTAMPTZ DEFAULT NOW(),
                updated_at    TIMESTAMPTZ DEFAULT NOW()
            )
            """
        )

    async def load_cameras(self) -> list[CameraConfig]:
        assert self._pool is not None
        rows = await self._pool.fetch(
            """
            SELECT camera_id, name, rtsp_url, location_name, zone_type, active
            FROM cameras
            WHERE active = TRUE
            ORDER BY camera_id
            """
        )
        self.cameras = [
            CameraConfig(
                camera_id=r["camera_id"],
                name=r["name"] or r["camera_id"],
                rtsp_url=r["rtsp_url"],
                location_tag=r["location_name"],
                zone_type=r["zone_type"],
                enabled=bool(r["active"]),
            )
            for r in rows
        ]
        for cam in self.cameras:
            self._state.setdefault(cam.camera_id, CameraRuntimeState())
        log.info("cameras_loaded", count=len(self.cameras))
        return self.cameras

    async def audit(self, action: str, camera_id: str, details: dict | None = None) -> None:
        """Append an immutable audit_log entry (INSERT only)."""
        assert self._pool is not None
        payload = {"camera_id": camera_id, "timestamp": _utcnow().isoformat()}
        if details:
            payload.update(details)
        try:
            import json

            await self._pool.execute(
                "INSERT INTO audit_log (action, resource_type, details) "
                "VALUES ($1, 'camera', $2::jsonb)",
                action,
                json.dumps(payload),
            )
        except Exception as exc:
            log.error("audit_write_failed", action=action, camera_id=camera_id, error=str(exc))

    async def _upsert_status(self, cam: CameraConfig, state: CameraRuntimeState, detail: str) -> None:
        assert self._pool is not None
        last_frame_dt = (
            datetime.fromtimestamp(state.last_frame_ts / 1000, tz=timezone.utc)
            if state.last_frame_ts
            else None
        )
        await self._pool.execute(
            """
            INSERT INTO camera_status (camera_id, status, fps, last_frame_ts, detail, last_checked, updated_at)
            VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
            ON CONFLICT (camera_id) DO UPDATE SET
                status = EXCLUDED.status,
                fps = EXCLUDED.fps,
                last_frame_ts = EXCLUDED.last_frame_ts,
                detail = EXCLUDED.detail,
                last_checked = NOW(),
                updated_at = NOW()
            """,
            cam.camera_id,
            state.status,
            float(state.fps),
            last_frame_dt,
            detail,
        )

    @staticmethod
    async def _ping_rtsp(rtsp_url: str, timeout: float = 5.0) -> bool:
        """Lightweight TCP reachability probe of the RTSP endpoint."""
        parsed = urlparse(rtsp_url)
        host = parsed.hostname
        port = parsed.port or 554
        if not host:
            return False
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout
            )
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return True
        except Exception:
            return False

    async def health_check_loop(self, stop: asyncio.Event) -> None:
        log.info("health_loop_start", interval_s=self.HEALTH_INTERVAL_S)
        while not stop.is_set():
            try:
                await self._run_health_pass()
            except Exception as exc:
                log.error("health_pass_error", error=str(exc))
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.HEALTH_INTERVAL_S)

    async def _run_health_pass(self) -> None:
        ready = await self._relay.verify_streams(self.camera_ids)
        for cam in self.cameras:
            state = self._state.setdefault(cam.camera_id, CameraRuntimeState())
            prev = state.status

            if ready.get(cam.camera_id):
                new_status, detail = "live", "stream ready in MediaMTX"
                state.backoff = 1.0
            elif await self._ping_rtsp(cam.rtsp_url):
                new_status, detail = "reachable", "RTSP endpoint reachable, stream not ready"
                state.backoff = 1.0
            else:
                new_status = "offline"
                state.backoff = min(state.backoff * 2, 60.0)
                detail = f"RTSP endpoint unreachable; reconnect backoff {state.backoff:.0f}s"

            state.status = new_status
            await self._upsert_status(cam, state, detail)

            if prev != new_status and prev != "unknown":
                if new_status == "offline":
                    log.warning("camera_disconnected", camera_id=cam.camera_id, detail=detail)
                    await self.audit("camera_disconnected", cam.camera_id, {"detail": detail})
                elif prev == "offline":
                    log.info("camera_reconnected", camera_id=cam.camera_id, status=new_status)
                    await self.audit("camera_connected", cam.camera_id, {"status": new_status})
            elif prev == "unknown" and new_status != "offline":
                await self.audit("camera_connected", cam.camera_id, {"status": new_status})


# ── Stream distributor ──────────────────────────────────────────────────────--
class StreamDistributor:
    """Fan camera frames out to every AI worker queue and publish FPS metrics."""

    def __init__(
        self,
        camera_queues: dict[str, "mp.Queue"],
        worker_queues: dict[str, "mp.Queue"],
        state: dict[str, CameraRuntimeState],
    ) -> None:
        self._camera_queues = camera_queues
        self._worker_queues = worker_queues
        self._state = state
        self._windows: dict[str, deque[float]] = {
            cid: deque() for cid in camera_queues
        }

    def _record_fps(self, camera_id: str) -> None:
        now = time.monotonic()
        window = self._windows[camera_id]
        window.append(now)
        cutoff = now - 1.0
        while window and window[0] < cutoff:
            window.popleft()
        fps = float(len(window))
        VIDEO_INGEST_FPS.labels(camera_id=camera_id).set(fps)
        st = self._state.setdefault(camera_id, CameraRuntimeState())
        st.fps = fps

    def _fan_out(self, camera_id: str, message: dict) -> None:
        for worker, queue in self._worker_queues.items():
            try:
                queue.put_nowait(message)
            except Full:
                VIDEO_INGEST_FANOUT_DROPS.labels(camera_id=camera_id, worker=worker).inc()
        VIDEO_INGEST_FRAMES.labels(camera_id=camera_id).inc()
        st = self._state.setdefault(camera_id, CameraRuntimeState())
        st.last_frame_ts = message.get("timestamp_ms")

    async def run(self, stop: asyncio.Event) -> None:
        log.info("distributor_start", workers=list(self._worker_queues.keys()))
        while not stop.is_set():
            idle = True
            for camera_id, queue in self._camera_queues.items():
                # Drain up to a small batch per camera per tick to stay fair.
                for _ in range(8):
                    try:
                        message = queue.get_nowait()
                    except Empty:
                        break
                    idle = False
                    self._fan_out(camera_id, message)
                    self._record_fps(camera_id)
            if idle:
                await asyncio.sleep(0.005)
        log.info("distributor_stop")


# ── Health / metrics HTTP server ────────────────────────────────────────────--
class HealthServer:
    def __init__(
        self,
        host: str,
        port: int,
        registry: CameraRegistry,
        state: dict[str, CameraRuntimeState],
    ) -> None:
        self._host = host
        self._port = port
        self._registry = registry
        self._state = state
        self._runner: web.AppRunner | None = None

    async def _health(self, _request: web.Request) -> web.Response:
        cameras = []
        for cid in self._registry.camera_ids:
            st = self._state.get(cid, CameraRuntimeState())
            cameras.append(
                {
                    "camera_id": cid,
                    "status": st.status,
                    "fps": round(st.fps, 2),
                    "last_frame_ts": st.last_frame_ts,
                }
            )
        return web.json_response({"service": "video-ingest-pipeline", "cameras": cameras})

    async def _metrics(self, _request: web.Request) -> web.Response:
        return web.Response(body=generate_latest(), content_type=CONTENT_TYPE_LATEST.split(";")[0])

    async def start(self) -> None:
        app = web.Application()
        app.add_routes([web.get("/health", self._health), web.get("/metrics", self._metrics)])
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        log.info("health_server_start", host=self._host, port=self._port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


# ── Orchestrator ────────────────────────────────────────────────────────────--
class IngestService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._state: dict[str, CameraRuntimeState] = {}
        self._client: httpx.AsyncClient | None = None
        self._relay: WebRTCRelay | None = None
        self._registry: CameraRegistry | None = None
        self._distributor: StreamDistributor | None = None
        self._health_server: HealthServer | None = None
        self._camera_queues: dict[str, "mp.Queue"] = {}
        self._worker_queues: dict[str, "mp.Queue"] = {}
        self._processes: dict[str, mp.Process] = {}
        self._stop_events: dict[str, EventType] = {}
        self._async_stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._client = httpx.AsyncClient()
        self._relay = WebRTCRelay(self.settings.mediamtx_api, self._client)
        self._registry = CameraRegistry(self.settings.db_url, self._relay, self._state)
        await self._registry.connect()
        cameras = await self._registry.load_cameras()

        self._worker_queues = {
            name: mp.Queue(maxsize=self.settings.worker_queue_size) for name in WORKER_NAMES
        }

        for cam in cameras:
            await self._relay.register_camera(cam)
            self._spawn_ingestor(cam)
            await self._registry.audit("ingestor_started", cam.camera_id)

        self._distributor = StreamDistributor(self._camera_queues, self._worker_queues, self._state)
        self._health_server = HealthServer(
            self.settings.health_host, self.settings.health_port, self._registry, self._state
        )
        await self._health_server.start()

        self._tasks = [
            asyncio.create_task(self._registry.health_check_loop(self._async_stop), name="health"),
            asyncio.create_task(self._distributor.run(self._async_stop), name="distributor"),
        ]
        log.info("service_started", cameras=len(cameras), use_fake_streams=self.settings.use_fake_streams)

    def _spawn_ingestor(self, cam: CameraConfig) -> None:
        queue: "mp.Queue" = mp.Queue(maxsize=self.settings.camera_queue_size)
        stop_event = mp.Event()
        proc = mp.Process(
            target=_ingestor_process_target,
            args=(cam, queue, stop_event, self.settings),
            name=f"ingestor-{cam.camera_id}",
            daemon=True,
        )
        proc.start()
        self._camera_queues[cam.camera_id] = queue
        self._stop_events[cam.camera_id] = stop_event
        self._processes[cam.camera_id] = proc
        log.info("ingestor_process_spawned", camera_id=cam.camera_id, pid=proc.pid)

    async def run_forever(self) -> None:
        await self._async_stop.wait()

    async def shutdown(self) -> None:
        if self._async_stop.is_set():
            return
        log.info("shutdown_begin")
        self._async_stop.set()

        # 1. Signal every ingestor process to stop.
        for stop_event in self._stop_events.values():
            stop_event.set()

        # 2. Stop async tasks (health loop, distributor).
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

        # 3. Join (then terminate) child processes.
        loop = asyncio.get_running_loop()
        for cid, proc in self._processes.items():
            await loop.run_in_executor(None, proc.join, 5.0)
            if proc.is_alive():
                log.warning("ingestor_force_terminate", camera_id=cid)
                proc.terminate()
                await loop.run_in_executor(None, proc.join, 2.0)

        # 4. Flush/drain all queues so buffers are released.
        for queue in list(self._camera_queues.values()) + list(self._worker_queues.values()):
            _drain_queue(queue)

        # 5. Audit + close resources.
        if self._registry is not None:
            for cid in list(self._processes.keys()):
                await self._registry.audit("ingestor_stopped", cid)
            await self._registry.audit("service_shutdown", "*", {"cameras": len(self._processes)})
            await self._registry.close()
        if self._health_server is not None:
            await self._health_server.stop()
        if self._client is not None:
            await self._client.aclose()
        log.info("shutdown_complete")


def _drain_queue(queue: "mp.Queue") -> None:
    with contextlib.suppress(Exception):
        while True:
            try:
                queue.get_nowait()
            except Empty:
                break
    with contextlib.suppress(Exception):
        queue.close()
        queue.join_thread()


async def _amain() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    service = IngestService(settings)

    loop = asyncio.get_running_loop()

    def _handle_signal(signame: str) -> None:
        log.info("signal_received", signal=signame)
        loop.create_task(service.shutdown())

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _handle_signal, sig.name)

    await service.start()
    try:
        await service.run_forever()
    finally:
        await service.shutdown()


def main() -> None:
    with contextlib.suppress(RuntimeError):
        mp.set_start_method("fork")  # Linux/Node B; safe to ignore if already set
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
