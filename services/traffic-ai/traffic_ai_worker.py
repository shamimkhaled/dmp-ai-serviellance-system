"""
services/traffic-ai/traffic_ai_worker.py
═════════════════════════════════════════════════════════════════════════════
Police AI — Traffic Violation Detection Worker  (Node A: 2× A100 80GB)

Frame sources (priority order):
  1. multiprocessing.Queue   FRAME_SOURCE=queue  — co-located with ingest_pipeline
  2. MediaMTX RTSP streams   FRAME_SOURCE=rtsp   — default Docker deployment
  3. Synthetic mock frames   FRAME_SOURCE=mock   — local dev / UI testing

Detection pipeline per frame batch:
  Frames → BDVehicleDetector (TRT/ONNX/PT) → DeepSORTTracker →
  ViolationAnalyzer (shapely zone checks) → ANPRReader (PaddleOCR) →
  AlertPublisher (Redis XADD + Prometheus)

Environment variables (all required paths / thresholds):
  REDIS_URL                 redis://redis:6379
  DATABASE_URL              PostgreSQL DSN
  RTSP_BASE_URL             rtsp://mediamtx:8554
  VIDEO_INGEST_URL          http://video-ingest:8001
  CAMERA_IDS                comma-separated list (fallback if DB unavailable)
  FRAME_SOURCE              queue | rtsp | mock  (default rtsp)
  USE_GPU                   true | false          (default true)
  YOLO_TRT_ENGINE_PATH      /models/traffic_yolov8m_bd.engine
  YOLO_ONNX_PATH            /models/traffic_yolov8m_bd.onnx
  YOLO_PT_PATH              /models/traffic_yolov8m_bd.pt  (fallback yolov8n.pt)
  TRAFFIC_CONF_THRESHOLD    0.45
  TRAFFIC_NMS_IOU           0.45
  SPEED_ALERT_THRESHOLD     60
  CAMERA_FPS                15
  BATCH_SIZE                4
  ZONE_REFRESH_INTERVAL     300
  CAMERA_REFRESH_INTERVAL   60
  LOG_LEVEL                 INFO
  INJECT_TEST_VIOLATIONS    false
  ANPR_MIN_CONFIDENCE       0.6
  ANPR_MIN_BBOX_AREA        4800
  HELMET_CONF_THRESHOLD     0.6
  DEDUP_TTL_SECONDS         30
  HEALTH_PORT               8002
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import multiprocessing as mp
import os
import re
import signal
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from queue import Empty
from typing import Any

import asyncpg
import cv2
import httpx
import numpy as np
import redis.asyncio as aioredis
import structlog
from aiohttp import web
from dotenv import load_dotenv
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
def configure_logging(level: str = "INFO") -> None:
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


configure_logging(os.getenv("LOG_LEVEL", "INFO"))
log: structlog.BoundLogger = structlog.get_logger("traffic-ai")


# ── Settings ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Settings:
    redis_url: str
    db_url: str
    rtsp_base_url: str
    video_ingest_url: str
    camera_ids_fallback: list[str]
    frame_source: str          # "queue" | "rtsp" | "mock"
    use_gpu: bool
    trt_engine_path: str
    onnx_path: str
    pt_path: str
    conf_threshold: float
    nms_iou: float
    speed_alert_kmh: float
    camera_fps: int
    batch_size: int
    zone_refresh_s: int
    camera_refresh_s: int
    log_level: str
    inject_test_violations: bool
    anpr_min_confidence: float
    anpr_min_bbox_area: int
    helmet_conf_threshold: float
    dedup_ttl: int
    health_port: int
    warmup_iters: int
    anpr_enabled: bool
    preview_enabled: bool

    @classmethod
    def from_env(cls) -> "Settings":
        use_gpu = os.getenv("USE_GPU", "true").lower() == "true"
        return cls(
            redis_url           = os.getenv("REDIS_URL", "redis://localhost:6379"),
            db_url              = os.getenv("DATABASE_URL", os.getenv("DB_URL", "")),
            rtsp_base_url       = os.getenv("RTSP_BASE_URL", "rtsp://localhost:8554"),
            video_ingest_url    = os.getenv("VIDEO_INGEST_URL", "http://localhost:8001"),
            camera_ids_fallback = [c.strip() for c in os.getenv("CAMERA_IDS", "cam01").split(",")],
            frame_source        = os.getenv("FRAME_SOURCE", "rtsp"),
            use_gpu             = use_gpu,
            trt_engine_path     = os.getenv("YOLO_TRT_ENGINE_PATH", "/models/traffic_yolov8m_bd.engine"),
            onnx_path           = os.getenv("YOLO_ONNX_PATH",       "/models/traffic_yolov8m_bd.onnx"),
            pt_path             = os.getenv("YOLO_PT_PATH",          "/models/traffic_yolov8m_bd.pt"),
            conf_threshold      = float(os.getenv("TRAFFIC_CONF_THRESHOLD", "0.45")),
            nms_iou             = float(os.getenv("TRAFFIC_NMS_IOU",         "0.45")),
            speed_alert_kmh     = float(os.getenv("SPEED_ALERT_THRESHOLD",   "60")),
            camera_fps          = int(os.getenv("CAMERA_FPS", "15")),
            batch_size          = int(os.getenv("BATCH_SIZE", "4")) if use_gpu else 1,
            zone_refresh_s      = int(os.getenv("ZONE_REFRESH_INTERVAL", "300")),
            camera_refresh_s    = int(os.getenv("CAMERA_REFRESH_INTERVAL", "60")),
            log_level           = os.getenv("LOG_LEVEL", "INFO"),
            inject_test_violations = os.getenv("INJECT_TEST_VIOLATIONS", "false").lower() == "true",
            anpr_min_confidence = float(os.getenv("ANPR_MIN_CONFIDENCE", "0.6")),
            anpr_min_bbox_area  = int(os.getenv("ANPR_MIN_BBOX_AREA", "4800")),
            helmet_conf_threshold = float(os.getenv("HELMET_CONF_THRESHOLD", "0.6")),
            dedup_ttl           = int(os.getenv("DEDUP_TTL_SECONDS", "30")),
            health_port         = int(os.getenv("HEALTH_PORT", "8002")),
            warmup_iters        = int(os.getenv("MODEL_WARMUP_ITERS", "10")),
            anpr_enabled        = os.getenv("ANPR_ENABLED", "true").lower() == "true",
            preview_enabled     = os.getenv("PREVIEW_ENABLED", "true").lower() == "true",
        )


# ── Prometheus ────────────────────────────────────────────────────────────────
ALERTS_TOTAL = Counter(
    "traffic_alerts_total", "Alerts published to Redis", ["alert_type", "camera_id"]
)
FRAMES_TOTAL = Counter(
    "traffic_frames_total", "Frames processed", ["camera_id"]
)
ERRORS_TOTAL = Counter(
    "traffic_errors_total", "Processing errors skipped", ["camera_id"]
)
INFERENCE_LATENCY = Histogram(
    "traffic_inference_latency_ms",
    "Full detect→publish latency (ms)",
    buckets=[5, 10, 25, 50, 100, 200, 500, 1000],
)
FPS_GAUGE = Gauge("traffic_fps", "Frames processed per second", ["camera_id"])
QUEUE_DEPTH = Gauge("traffic_queue_depth", "Unprocessed frames in queue")

# ── Class maps ────────────────────────────────────────────────────────────────
# Full COCO 80-class map — used for detection overlay on all objects.
COCO_80_CLASSES: dict[int, str] = {
    0:  "person",        1:  "bicycle",       2:  "car",
    3:  "motorcycle",    4:  "airplane",       5:  "bus",
    6:  "train",         7:  "truck",          8:  "boat",
    9:  "traffic light", 10: "fire hydrant",   11: "stop sign",
    12: "parking meter", 13: "bench",          14: "bird",
    15: "cat",           16: "dog",            17: "horse",
    18: "sheep",         19: "cow",            20: "elephant",
    21: "bear",          22: "zebra",          23: "giraffe",
    24: "backpack",      25: "umbrella",       26: "handbag",
    27: "tie",           28: "suitcase",       29: "frisbee",
    30: "skis",          31: "snowboard",      32: "sports ball",
    33: "kite",          34: "baseball bat",   35: "baseball glove",
    36: "skateboard",    37: "surfboard",      38: "tennis racket",
    39: "bottle",        40: "wine glass",     41: "cup",
    42: "fork",          43: "knife",          44: "spoon",
    45: "bowl",          46: "banana",         47: "apple",
    48: "sandwich",      49: "orange",         50: "broccoli",
    51: "carrot",        52: "hot dog",        53: "pizza",
    54: "donut",         55: "cake",           56: "chair",
    57: "couch",         58: "potted plant",   59: "bed",
    60: "dining table",  61: "toilet",         62: "tv",
    63: "laptop",        64: "mouse",          65: "remote",
    66: "keyboard",      67: "cell phone",     68: "microwave",
    69: "oven",          70: "toaster",        71: "sink",
    72: "refrigerator",  73: "book",           74: "clock",
    75: "vase",          76: "scissors",       77: "teddy bear",
    78: "hair drier",    79: "toothbrush",
}

# Bangladesh-specific vehicle classes (fine-tuned, appended after COCO 80).
BD_CLASSES: dict[int, str] = {
    80: "cng", 81: "tempo", 82: "battery_van",
}

# ALL_CLASSES: every detectable label (used for annotation overlay on preview).
ALL_CLASSES: dict[int, str] = {**COCO_80_CLASSES, **BD_CLASSES}

# VIOLATION_CLASSES: subset used for the traffic violation analysis pipeline.
# Only road users and vehicles are relevant for zone/speed/helmet checks.
VIOLATION_CLASSES: dict[int, str] = {
    0:  "person",      1:  "bicycle",     2:  "car",
    3:  "motorcycle",  5:  "bus",         7:  "truck",
    80: "cng",         81: "tempo",       82: "battery_van",
}

PARKABLE_CLASSES = {"car", "truck", "bus", "tempo", "battery_van"}

SEVERITY_MAP: dict[str, int] = {
    "red_light_violation":  3,
    "stop_line_violation":  2,
    "wrong_lane":           3,
    "helmet_missing":       2,
    "illegal_parking":      2,
    "speeding":             3,
}

BD_PLATE_RE = re.compile(r"[A-Z]{2,3}[\s-]?\d{2}[\s-]?\d{4,5}")


# ── Domain types ──────────────────────────────────────────────────────────────
@dataclass
class Detection:
    class_id:    int
    class_name:  str
    confidence:  float
    bbox_xyxy:   list[float]
    frame_id:    int
    camera_id:   str
    timestamp_ms: int


@dataclass
class Zone:
    zone_id:           str
    zone_type:         str   # red_light|stop_line|wrong_lane|no_parking|speed
    polygon_pts:       list[tuple[float, float]] | None   # normalised 0-1 coords
    stop_line_y:       float | None
    lane_pts:          list[tuple[float, float]] | None
    speed_limit_kmh:   float
    speed_cal_ppm:     float   # pixels per metre at 640×640
    camera_direction:  str     # "down" | "up" | "side"


@dataclass
class TrackState:
    track_id:           int
    class_name:         str
    positions:          deque = field(default_factory=lambda: deque(maxlen=60))
    stationary_frames:  int = 0
    last_centroid:      tuple[float, float] | None = None
    plate_text:         str | None = None
    plate_confidence:   float = 0.0
    violation_flags:    set[str] = field(default_factory=set)
    last_frame_seq:     int = 0
    lane_cross_count:   int = 0


@dataclass
class ViolationResult:
    alert_type:       str
    track_id:         int
    class_name:       str
    confidence:       float
    bbox_xyxy:        list[float]
    zone_id:          str
    zone_type:        str
    speed_kmh:        float = 0.0
    camera_id:        str = ""
    timestamp_ms:     int = 0
    frame_seq:        int = 0


@dataclass
class ANPRResult:
    plate_text:      str
    ocr_confidence:  float
    raw_output:      str


# ── ModelLoader ───────────────────────────────────────────────────────────────
class ModelLoader:
    """
    Priority: TRT engine → ONNX → PyTorch .pt
    CPU fallback: yolov8n.pt (nano) via ultralytics.
    Warms up with dummy inferences to avoid cold-start latency.
    """

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._model: Any = None
        self._device: str = "cpu"
        self._format: str = "none"
        self._use_raw_trt: bool = False

    def load(self) -> None:
        if not self._s.use_gpu:
            self._load_cpu_nano()
            return

        if self._try_trt():
            return
        if self._try_onnx():
            return
        self._load_pt_gpu()

    def _try_trt(self) -> bool:
        path = self._s.trt_engine_path
        if not os.path.exists(path):
            log.info("trt_engine_missing", path=path)
            return False
        try:
            from ultralytics import YOLO
            self._model = YOLO(path)
            self._format = "tensorrt"
            self._device = "cuda:0"
            log.info("model_loaded", format="tensorrt", path=path, device=self._device)
            self._warmup()
            return True
        except Exception as exc:
            log.warning("trt_load_failed", error=str(exc))
            return False

    def _try_onnx(self) -> bool:
        path = self._s.onnx_path
        if not os.path.exists(path):
            log.info("onnx_missing", path=path)
            return False
        try:
            from ultralytics import YOLO
            self._model = YOLO(path)
            self._format = "onnx"
            self._device = "cuda:0"
            log.info("model_loaded", format="onnx", path=path, device=self._device)
            self._warmup()
            return True
        except Exception as exc:
            log.warning("onnx_load_failed", error=str(exc))
            return False

    def _load_pt_gpu(self) -> None:
        from ultralytics import YOLO
        path = self._s.pt_path
        if not os.path.exists(path):
            log.warning("pt_path_missing", path=path, fallback="yolov8m.pt")
            path = "yolov8m.pt"
        self._model = YOLO(path)
        self._format = "pytorch"
        self._device = "cuda:0"
        log.info("model_loaded", format="pytorch", path=path, device=self._device)
        self._warmup()

    def _load_cpu_nano(self) -> None:
        from ultralytics import YOLO
        path = self._s.pt_path
        if not os.path.exists(path):
            path = "yolov8n.pt"
        self._model = YOLO(path)
        self._format = "pytorch_cpu"
        self._device = "cpu"
        log.info(
            "model_loaded",
            mode="LOCALHOST_CPU",
            format="pytorch_cpu",
            path=path,
            batch_size=1,
            note="LOCALHOST MODE: CPU inference, no TRT, batch_size=1",
        )
        self._warmup()

    def _warmup(self) -> None:
        dummy = [np.zeros((640, 640, 3), dtype=np.uint8)] * min(4, self._s.warmup_iters)
        t0 = time.perf_counter()
        for _ in range(self._s.warmup_iters):
            self._model.predict(
                dummy[0], device=self._device, verbose=False, conf=0.5
            )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        log.info(
            "model_warmup_done",
            iters=self._s.warmup_iters,
            avg_ms=round(elapsed_ms / self._s.warmup_iters, 1),
            format=self._format,
        )

    def predict(self, frames: list[np.ndarray]) -> list[Any]:
        """Return ultralytics Results list, one per frame."""
        if not frames:
            return []
        if self._s.use_gpu and self._s.batch_size > 1:
            return self._model.predict(
                frames,
                conf=self._s.conf_threshold,
                iou=self._s.nms_iou,
                device=self._device,
                verbose=False,
                stream=False,
            )
        results = []
        for frame in frames:
            res = self._model.predict(
                frame,
                conf=self._s.conf_threshold,
                iou=self._s.nms_iou,
                device=self._device,
                verbose=False,
            )
            results.extend(res)
        return results

    @property
    def loaded(self) -> bool:
        return self._model is not None


# ── BDVehicleDetector ─────────────────────────────────────────────────────────
class BDVehicleDetector:
    """
    Batch inference over a list of tagged frame dicts.
    Injectable: pass a mock model_loader for unit tests.
    """

    def __init__(self, model_loader: ModelLoader) -> None:
        self._loader = model_loader

    def detect(self, frame_metas: list[dict]) -> list[list[Detection]]:
        """
        frame_metas: [{"camera_id", "timestamp_ms", "frame_seq", "frame": np.ndarray}]
        Returns one Detection list per input frame (same order).
        """
        if not frame_metas:
            return []
        frames = [m["frame"] for m in frame_metas]
        try:
            results = self._loader.predict(frames)
        except Exception as exc:
            log.error("inference_failed", error=str(exc))
            return [[] for _ in frame_metas]

        out: list[list[Detection]] = []
        for i, res in enumerate(results):
            meta = frame_metas[i]
            dets: list[Detection] = []
            if res.boxes is None:
                out.append(dets)
                continue
            for box in res.boxes:
                cls_id = int(box.cls[0])
                # Accept all 80 COCO classes + BD classes for overlay display.
                # Unknown class ids (e.g. from a custom model) fall back to
                # a generic label so they still appear in the preview.
                label = ALL_CLASSES.get(cls_id, f"class_{cls_id}")
                dets.append(
                    Detection(
                        class_id    = cls_id,
                        class_name  = label,
                        confidence  = float(box.conf[0]),
                        bbox_xyxy   = box.xyxy[0].tolist(),
                        frame_id    = meta["frame_seq"],
                        camera_id   = meta["camera_id"],
                        timestamp_ms= meta["timestamp_ms"],
                    )
                )
            out.append(dets)
        return out


# ── DeepSORTTracker ───────────────────────────────────────────────────────────
class DeepSORTTracker:
    """One DeepSort instance per camera_id, lazily created."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._trackers: dict[str, Any] = {}

    def _get_tracker(self, camera_id: str) -> Any:
        if camera_id not in self._trackers:
            try:
                from deep_sort_realtime.deepsort_tracker import DeepSort
                self._trackers[camera_id] = DeepSort(
                    max_age=30,
                    n_init=3,
                    max_iou_distance=0.7,
                    embedder="mobilenet",
                    half=self._s.use_gpu,
                    embedder_gpu=self._s.use_gpu,
                )
            except ImportError:
                # Fallback: centroid-only tracker stub
                self._trackers[camera_id] = _CentroidTracker()
        return self._trackers[camera_id]

    def update(
        self, camera_id: str, detections: list[Detection], frame: np.ndarray
    ) -> list[dict]:
        """Returns list of active tracks: {track_id, bbox_xyxy, class_name, hits, age}."""
        tracker = self._get_tracker(camera_id)
        if not detections:
            try:
                tracker.update_tracks([], frame=frame)
            except Exception:
                pass
            return []
        raw = []
        for d in detections:
            x1, y1, x2, y2 = d.bbox_xyxy
            w, h = x2 - x1, y2 - y1
            raw.append(([x1, y1, w, h], d.confidence, d.class_name))
        try:
            tracks = tracker.update_tracks(raw, frame=frame)
        except Exception as exc:
            log.warning("deepsort_update_failed", camera_id=camera_id, error=str(exc))
            return []
        out = []
        for t in tracks:
            if not t.is_confirmed():
                continue
            ltrb = t.to_ltrb()
            out.append({
                "track_id":  t.track_id,
                "bbox_xyxy": [float(v) for v in ltrb],
                "class_name": t.det_class or "unknown",
                "hits":      t.hits,
                "age":       t.age,
            })
        return out

    def remove_camera(self, camera_id: str) -> None:
        self._trackers.pop(camera_id, None)


class _CentroidTracker:
    """Minimal centroid-based tracker stub when deep_sort_realtime is unavailable."""

    def __init__(self) -> None:
        self._next_id = 1
        self._tracks: dict[int, dict] = {}

    def update_tracks(self, raw_dets: list, frame: np.ndarray | None = None) -> list:
        class _T:
            def __init__(self, tid, ltrb, cls):
                self.track_id = tid
                self._ltrb = ltrb
                self.det_class = cls
                self.hits = 1
                self.age = 1

            def is_confirmed(self): return True
            def to_ltrb(self): return self._ltrb

        out = []
        for raw in raw_dets:
            (x1, y1, w, h), conf, cls = raw
            tid = self._next_id
            self._next_id += 1
            out.append(_T(tid, [x1, y1, x1 + w, y1 + h], cls))
        return out


# ── ViolationAnalyzer ─────────────────────────────────────────────────────────
class ViolationAnalyzer:
    """
    Zone-based violation detection using shapely geometry.
    Zones loaded from PostgreSQL camera_zones table, refreshed every zone_refresh_s.
    All polygon coordinates are normalised (0–1) and scaled to 640×640 at check time.
    Injectable: pass zone_override dict for unit tests.
    """

    FRAME_W = FRAME_H = 640

    def __init__(
        self,
        settings: Settings,
        zone_override: dict[str, list[Zone]] | None = None,
    ) -> None:
        self._s = settings
        self._zones: dict[str, list[Zone]] = zone_override or {}
        self._track_states: dict[str, dict[int, TrackState]] = {}
        self._last_zone_load: float = 0.0
        self._pool: asyncpg.Pool | None = None

    async def connect(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        await self._ensure_zones_table()
        await self._load_zones()

    async def _ensure_zones_table(self) -> None:
        assert self._pool
        await self._pool.execute(
            """
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
            )
            """
        )
        await self._pool.execute(
            "CREATE INDEX IF NOT EXISTS camera_zones_cam_idx ON camera_zones(camera_id)"
        )

    async def _load_zones(self) -> None:
        if not self._pool:
            return
        try:
            rows = await self._pool.fetch(
                "SELECT id, camera_id, zone_type, polygon_points_json, "
                "stop_line_y, lane_boundary_json, speed_limit_kmh, "
                "speed_cal_ppm, camera_direction FROM camera_zones WHERE is_active=TRUE"
            )
            zones: dict[str, list[Zone]] = {}
            for r in rows:
                z = Zone(
                    zone_id           = str(r["id"]),
                    zone_type         = r["zone_type"],
                    polygon_pts       = [tuple(p) for p in (r["polygon_points_json"] or [])],
                    stop_line_y       = float(r["stop_line_y"]) if r["stop_line_y"] else None,
                    lane_pts          = [tuple(p) for p in (r["lane_boundary_json"] or [])],
                    speed_limit_kmh   = float(r["speed_limit_kmh"] or 60),
                    speed_cal_ppm     = float(r["speed_cal_ppm"] or 100.0),
                    camera_direction  = r["camera_direction"] or "down",
                )
                zones.setdefault(r["camera_id"], []).append(z)
            self._zones = zones
            self._last_zone_load = time.monotonic()
            log.info("zones_loaded", cameras=len(zones), total=sum(len(v) for v in zones.values()))
        except Exception as exc:
            log.error("zone_load_failed", error=str(exc))

    async def maybe_refresh(self) -> None:
        if time.monotonic() - self._last_zone_load > self._s.zone_refresh_s:
            await self._load_zones()

    def _state(self, camera_id: str, track_id: int, class_name: str) -> TrackState:
        cam = self._track_states.setdefault(camera_id, {})
        if track_id not in cam:
            cam[track_id] = TrackState(track_id=track_id, class_name=class_name)
        return cam[track_id]

    def _centroid(self, bbox: list[float]) -> tuple[float, float]:
        x1, y1, x2, y2 = bbox
        return ((x1 + x2) / 2, (y1 + y2) / 2)

    def _scale(self, pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
        return [(x * self.FRAME_W, y * self.FRAME_H) for x, y in pts]

    def analyze(
        self,
        camera_id:    str,
        tracks:       list[dict],
        frame_seq:    int,
        timestamp_ms: int,
    ) -> list[ViolationResult]:
        zones = self._zones.get(camera_id, [])
        violations: list[ViolationResult] = []

        for track in tracks:
            track_id  = track["track_id"]
            bbox      = track["bbox_xyxy"]
            cls_name  = track["class_name"]
            cx, cy    = self._centroid(bbox)
            state     = self._state(camera_id, track_id, cls_name)

            # Update position history
            state.positions.append((cx, cy))
            displacement = 0.0
            if state.last_centroid:
                dx = cx - state.last_centroid[0]
                dy = cy - state.last_centroid[1]
                displacement = (dx * dx + dy * dy) ** 0.5
            state.last_centroid = (cx, cy)
            state.last_frame_seq = frame_seq

            # Stationary counter
            if displacement < 3.0:
                state.stationary_frames += 1
            else:
                state.stationary_frames = 0

            for zone in zones:
                v = self._check_zone(camera_id, zone, track, state, displacement, timestamp_ms, frame_seq)
                if v:
                    violations.append(v)

        # Prune stale track states
        self._prune(camera_id, frame_seq)
        return violations

    def _check_zone(
        self,
        camera_id:    str,
        zone:         Zone,
        track:        dict,
        state:        TrackState,
        displacement: float,
        timestamp_ms: int,
        frame_seq:    int,
    ) -> ViolationResult | None:
        ztype = zone.zone_type
        if ztype == "red_light":
            return self._red_light(camera_id, zone, track, state, displacement, timestamp_ms, frame_seq)
        if ztype == "stop_line":
            return self._stop_line(camera_id, zone, track, state, displacement, timestamp_ms, frame_seq)
        if ztype == "wrong_lane":
            return self._wrong_lane(camera_id, zone, track, state, displacement, timestamp_ms, frame_seq)
        if ztype == "no_parking":
            return self._parking(camera_id, zone, track, state, timestamp_ms, frame_seq)
        if ztype == "speed":
            return self._speed(camera_id, zone, track, state, displacement, timestamp_ms, frame_seq)
        return None

    def _in_polygon(self, pt: tuple[float, float], scaled_pts: list[tuple]) -> bool:
        if len(scaled_pts) < 3:
            return False
        try:
            from shapely.geometry import Point, Polygon
            return Point(pt).within(Polygon(scaled_pts))
        except Exception:
            return False

    def _red_light(self, camera_id, zone, track, state, displacement, ts, seq) -> ViolationResult | None:
        if not zone.polygon_pts or len(zone.polygon_pts) < 3:
            return None
        bbox = track["bbox_xyxy"]
        cx, cy = self._centroid(bbox)
        scaled = self._scale(zone.polygon_pts)
        if not self._in_polygon((cx, cy), scaled):
            return None
        # Only flag slow/stationary vehicles (not passing through on green)
        if displacement > 5.0:
            return None
        key = f"red_light_{zone.zone_id}"
        if key in state.violation_flags:
            return None
        state.violation_flags.add(key)
        return ViolationResult(
            alert_type   = "red_light_violation",
            track_id     = state.track_id,
            class_name   = state.class_name,
            confidence   = 0.75,
            bbox_xyxy    = bbox,
            zone_id      = zone.zone_id,
            zone_type    = zone.zone_type,
            camera_id    = camera_id,
            timestamp_ms = ts,
            frame_seq    = seq,
        )

    def _stop_line(self, camera_id, zone, track, state, displacement, ts, seq) -> ViolationResult | None:
        if zone.stop_line_y is None:
            return None
        bbox  = track["bbox_xyxy"]
        x1, y1, x2, y2 = bbox
        line_y = zone.stop_line_y * self.FRAME_H
        # Front edge: bottom for downward cameras, top for upward
        front_y = y2 if zone.camera_direction == "down" else y1
        positions = list(state.positions)
        if len(positions) < 2:
            return None
        prev_cy = positions[-2][1]
        # Crossed line this frame (was behind, now past)
        crossed = (
            (zone.camera_direction == "down"  and prev_cy + (y2 - y1) / 2 < line_y <= front_y) or
            (zone.camera_direction == "up"    and prev_cy - (y2 - y1) / 2 > line_y >= front_y)
        )
        if not crossed:
            return None
        key = f"stop_line_{zone.zone_id}"
        if key in state.violation_flags:
            return None
        state.violation_flags.add(key)
        return ViolationResult(
            alert_type   = "stop_line_violation",
            track_id     = state.track_id,
            class_name   = state.class_name,
            confidence   = 0.80,
            bbox_xyxy    = bbox,
            zone_id      = zone.zone_id,
            zone_type    = zone.zone_type,
            camera_id    = camera_id,
            timestamp_ms = ts,
            frame_seq    = seq,
        )

    def _wrong_lane(self, camera_id, zone, track, state, displacement, ts, seq) -> ViolationResult | None:
        if not zone.lane_pts or len(zone.lane_pts) < 2 or len(state.positions) < 3:
            return None
        try:
            from shapely.geometry import LineString, Point
        except ImportError:
            return None
        lane_line = LineString(self._scale(zone.lane_pts))
        recent = list(state.positions)[-3:]
        # Count consecutive frames where track is on wrong side
        wrong_side = False
        for pt in recent:
            p = Point(pt)
            if lane_line.distance(p) < 8 and not lane_line.contains(p):
                state.lane_cross_count += 1
            else:
                state.lane_cross_count = 0
        wrong_side = state.lane_cross_count >= 3
        if not wrong_side:
            return None
        key = f"wrong_lane_{zone.zone_id}"
        if key in state.violation_flags:
            return None
        state.violation_flags.add(key)
        state.lane_cross_count = 0
        return ViolationResult(
            alert_type   = "wrong_lane",
            track_id     = state.track_id,
            class_name   = state.class_name,
            confidence   = 0.70,
            bbox_xyxy    = track["bbox_xyxy"],
            zone_id      = zone.zone_id,
            zone_type    = zone.zone_type,
            camera_id    = camera_id,
            timestamp_ms = ts,
            frame_seq    = seq,
        )

    def _parking(self, camera_id, zone, track, state, ts, seq) -> ViolationResult | None:
        if state.class_name not in PARKABLE_CLASSES:
            return None
        if not zone.polygon_pts or len(zone.polygon_pts) < 3:
            return None
        cx, cy = self._centroid(track["bbox_xyxy"])
        if not self._in_polygon((cx, cy), self._scale(zone.polygon_pts)):
            return None
        # 30 s stationary at 15 fps = 450 frames
        threshold = int(30 * self._s.camera_fps)
        if state.stationary_frames < threshold:
            return None
        key = f"parking_{zone.zone_id}"
        if key in state.violation_flags:
            return None
        state.violation_flags.add(key)
        return ViolationResult(
            alert_type   = "illegal_parking",
            track_id     = state.track_id,
            class_name   = state.class_name,
            confidence   = 0.80,
            bbox_xyxy    = track["bbox_xyxy"],
            zone_id      = zone.zone_id,
            zone_type    = zone.zone_type,
            camera_id    = camera_id,
            timestamp_ms = ts,
            frame_seq    = seq,
        )

    def _speed(self, camera_id, zone, track, state, displacement, ts, seq) -> ViolationResult | None:
        ppm = zone.speed_cal_ppm
        if ppm <= 0:
            return None
        speed_kmh = (displacement * self._s.camera_fps * 3.6) / ppm
        limit = zone.speed_limit_kmh or self._s.speed_alert_kmh
        if speed_kmh <= limit:
            return None
        key = f"speed_{zone.zone_id}"
        if key in state.violation_flags:
            return None
        state.violation_flags.add(key)
        return ViolationResult(
            alert_type   = "speeding",
            track_id     = state.track_id,
            class_name   = state.class_name,
            confidence   = min(0.95, 0.5 + (speed_kmh - limit) / limit),
            bbox_xyxy    = track["bbox_xyxy"],
            zone_id      = zone.zone_id,
            zone_type    = zone.zone_type,
            speed_kmh    = round(speed_kmh, 1),
            camera_id    = camera_id,
            timestamp_ms = ts,
            frame_seq    = seq,
        )

    def check_helmet(self, track: dict, frame: np.ndarray, conf_threshold: float) -> bool:
        """Return True if no helmet detected on motorcycle rider."""
        if track["class_name"] != "motorcycle":
            return False
        x1, y1, x2, y2 = [int(v) for v in track["bbox_xyxy"]]
        h = y2 - y1
        head_y2 = y1 + max(1, int(h * 0.25))
        crop = frame[max(0, y1):head_y2, max(0, x1):min(frame.shape[1], x2)]
        if crop.size == 0:
            return False
        # Heuristic: high green-channel relative to red = helmet visible (orange/yellow helmets)
        # A proper helmet classifier CNN/YOLO-cls would replace this in production.
        mean = crop.mean(axis=(0, 1))        # BGR
        b, g, r = float(mean[0]), float(mean[1]), float(mean[2])
        brightness = (b + g + r) / 3
        # Very dark head region suggests bare head (hair) vs bright region (helmet)
        return brightness < 40.0

    def _prune(self, camera_id: str, frame_seq: int, max_age: int = 60) -> None:
        cam = self._track_states.get(camera_id, {})
        stale = [tid for tid, s in cam.items() if frame_seq - s.last_frame_seq > max_age]
        for tid in stale:
            cam.pop(tid, None)


# ── ANPRReader ────────────────────────────────────────────────────────────────
class ANPRReader:
    """
    PaddleOCR-based ANPR for Bangladesh plates.
    Cache: skip re-OCR for same track_id if already read with confidence > threshold.
    """

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._ocr: Any = None
        self._cache: dict[int, ANPRResult] = {}    # track_id → result

    def load(self) -> None:
        try:
            from paddleocr import PaddleOCR
        except Exception as exc:
            log.warning("anpr_import_failed", error=str(exc))
            return
        # PaddleOCR's constructor signature changed across 2.x → 3.x
        # (use_gpu / show_log / use_angle_cls were removed). Try newest → oldest.
        attempts: list[dict[str, Any]] = [
            # PaddleOCR 3.x — skip doc orientation/unwarping for plate crops (lighter, faster).
            {
                "lang": "en",
                "use_doc_orientation_classify": False,
                "use_doc_unwarping": False,
                "use_textline_orientation": False,
            },
            {"lang": "en"},
            # PaddleOCR 2.x signatures.
            {"lang": "en", "use_angle_cls": True},
            {"lang": "en", "use_angle_cls": True, "use_gpu": self._s.use_gpu, "show_log": False},
        ]
        last_exc: Exception | None = None
        for kwargs in attempts:
            try:
                self._ocr = PaddleOCR(**kwargs)
                log.info("anpr_loaded", args=sorted(kwargs.keys()), gpu=self._s.use_gpu)
                return
            except Exception as exc:
                last_exc = exc
        log.warning("anpr_load_failed", error=str(last_exc))

    def read(
        self,
        track: dict,
        frame: np.ndarray,
        violation: ViolationResult,
    ) -> ANPRResult | None:
        if self._ocr is None:
            return None
        track_id = track["track_id"]

        # Return cached result if high-confidence
        if track_id in self._cache and self._cache[track_id].ocr_confidence > 0.75:
            return self._cache[track_id]

        x1, y1, x2, y2 = [int(v) for v in track["bbox_xyxy"]]
        h, w = y2 - y1, x2 - x1

        # Minimum bbox size check
        if w * h < self._s.anpr_min_bbox_area:
            return None
        if float(track.get("confidence", 0)) < self._s.anpr_min_confidence:
            return None

        # Plate crop: bottom 30% for most vehicles, 20% for CNG
        crop_frac = 0.20 if violation.class_name == "cng" else 0.30
        plate_y1 = max(0, y2 - int(h * crop_frac))
        crop = frame[plate_y1:y2, max(0, x1):min(frame.shape[1], x2)]
        if crop.size == 0:
            return None

        try:
            result = self._ocr.ocr(crop, cls=True)
        except Exception as exc:
            log.warning("ocr_failed", track_id=track_id, error=str(exc))
            return None

        if not result or not result[0]:
            return None

        best_text, best_conf = "", 0.0
        raw_lines = []
        for line in (result[0] or []):
            if line and len(line) >= 2 and line[1]:
                text, conf = str(line[1][0]), float(line[1][1])
                raw_lines.append(f"{text}({conf:.2f})")
                if conf > best_conf:
                    best_text, best_conf = text, conf

        # Validate BD plate format
        match = BD_PLATE_RE.search(best_text.upper().replace(" ", ""))
        plate_text = match.group(0) if match else best_text.strip()

        anpr = ANPRResult(
            plate_text     = plate_text,
            ocr_confidence = best_conf,
            raw_output     = " | ".join(raw_lines),
        )
        if best_conf > 0.5:
            self._cache[track_id] = anpr
        return anpr

    def evict(self, track_id: int) -> None:
        self._cache.pop(track_id, None)


# ── AlertPublisher ────────────────────────────────────────────────────────────
class AlertPublisher:
    """
    Dedup via Redis SET with TTL, then XADD to Redis Stream.
    snapshot_b64: bbox crop + 20px padding, JPEG q=85.
    """

    STREAM = "alerts:traffic"

    def __init__(self, settings: Settings, redis: aioredis.Redis) -> None:
        self._s = settings
        self._redis = redis
        self._location_cache: dict[str, dict] = {}

    async def load_locations(self, pool: asyncpg.Pool) -> None:
        try:
            rows = await pool.fetch(
                "SELECT camera_id, location_name, latitude, longitude FROM cameras WHERE active=TRUE"
            )
            for r in rows:
                self._location_cache[r["camera_id"]] = {
                    "location_name": r["location_name"] or "",
                    "latitude":      float(r["latitude"] or 0),
                    "longitude":     float(r["longitude"] or 0),
                }
        except Exception as exc:
            log.warning("location_cache_load_failed", error=str(exc))

    async def publish(
        self,
        violation:   ViolationResult,
        anpr:        ANPRResult | None,
        frame:       np.ndarray,
        camera_id:   str,
    ) -> bool:
        """Returns True if published, False if deduped."""
        dedup_key = (
            f"dedup:traffic:{camera_id}:{violation.track_id}:{violation.alert_type}"
        )
        if await self._redis.exists(dedup_key):
            return False

        snapshot_b64 = self._crop_snapshot(frame, violation.bbox_xyxy)
        loc = self._location_cache.get(camera_id, {})
        severity = SEVERITY_MAP.get(violation.alert_type, 2)
        if violation.confidence > 0.90:
            severity = min(severity + 1, 4)

        plate_text = (anpr.plate_text if anpr else "") or ""
        plate_conf = (anpr.ocr_confidence if anpr else 0.0)

        metadata = {
            "track_id":        violation.track_id,
            "vehicle_class":   violation.class_name,
            "violation_zone":  violation.zone_id,
            "speed_kmh":       violation.speed_kmh,
            "plate_text":      plate_text,
            "plate_confidence": round(plate_conf, 3),
            "frame_seq":       violation.frame_seq,
        }

        payload = {
            "alert_id":        str(uuid.uuid4()),
            "alert_type":      violation.alert_type,
            "camera_id":       camera_id,
            "confidence":      str(round(violation.confidence, 4)),
            "severity":        str(severity),
            "snapshot_b64":    snapshot_b64,
            "object_metadata": json.dumps(metadata),
            "location_name":   loc.get("location_name", ""),
            "latitude":        str(loc.get("latitude", 0)),
            "longitude":       str(loc.get("longitude", 0)),
            "frame_ts":        datetime.fromtimestamp(
                                   violation.timestamp_ms / 1000, tz=timezone.utc
                               ).isoformat(),
            "worker":          "traffic-ai",
        }

        await self._redis.xadd(self.STREAM, payload, maxlen=10000)
        await self._redis.set(dedup_key, "1", ex=self._s.dedup_ttl)

        ALERTS_TOTAL.labels(
            alert_type=violation.alert_type, camera_id=camera_id
        ).inc()
        log.info(
            "alert_published",
            alert_type=violation.alert_type,
            camera_id=camera_id,
            track_id=violation.track_id,
            class_name=violation.class_name,
            speed_kmh=violation.speed_kmh,
            plate=plate_text or None,
        )
        return True

    def _crop_snapshot(self, frame: np.ndarray, bbox: list[float]) -> str:
        x1, y1, x2, y2 = [int(v) for v in bbox]
        pad = 20
        h, w = frame.shape[:2]
        crop = frame[
            max(0, y1 - pad) : min(h, y2 + pad),
            max(0, x1 - pad) : min(w, x2 + pad),
        ]
        if crop.size == 0:
            crop = frame
        _, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return base64.b64encode(buf).decode()


# ── TrafficWorker ─────────────────────────────────────────────────────────────
class TrafficWorker:
    """
    Orchestrates the full detection → track → analyze → ANPR → publish pipeline.

    Frame sources:
      - queue: multiprocessing.Queue provided externally (co-located with ingest_pipeline)
      - rtsp: reads from MediaMTX per camera, with dynamic camera discovery
      - mock: generates synthetic violations every 30 s per camera
    """

    def __init__(
        self,
        settings:    Settings,
        frame_queue: mp.Queue | None = None,
    ) -> None:
        self._s            = settings
        self._frame_queue  = frame_queue
        self._stop_event   = asyncio.Event()
        self._redis:       aioredis.Redis | None = None
        self._pool:        asyncpg.Pool | None   = None
        self._model:       ModelLoader | None    = None
        self._detector:    BDVehicleDetector | None = None
        self._tracker:     DeepSORTTracker | None   = None
        self._analyzer:    ViolationAnalyzer | None = None
        self._anpr:        ANPRReader | None         = None
        self._publisher:   AlertPublisher | None      = None
        self._cameras:     list[str]  = list(settings.camera_ids_fallback)
        self._fps_windows: dict[str, deque] = {}
        # Latest annotated JPEG per camera, served by the /preview MJPEG endpoint.
        self._latest_jpeg: dict[str, bytes] = {}
        # ── Motion detection (per-camera background subtractors) ──────────────
        self._bg_subtractors:   dict[str, cv2.BackgroundSubtractor] = {}
        self._motion_skip_count: dict[str, int]  = {}   # consecutive no-motion frames
        # Latest serialised detection list per camera, broadcast to WS clients
        self._latest_detections: dict[str, list] = {}
        self._stats = {
            "frames_processed":   0,
            "alerts_generated":   0,
            "cameras":            list(settings.camera_ids_fallback),
            "model_format":       "none",
            "use_gpu":            settings.use_gpu,
            "frame_source":       settings.frame_source,
            "motion_skipped":     0,
        }

    async def start(self) -> None:
        loop = asyncio.get_running_loop()

        # Redis
        self._redis = aioredis.from_url(self._s.redis_url, decode_responses=True)

        # DB
        if self._s.db_url:
            self._pool = await asyncpg.create_pool(
                self._s.db_url, min_size=1, max_size=5
            )

        # Model (blocking load in thread to not block event loop)
        self._model = ModelLoader(self._s)
        await loop.run_in_executor(None, self._model.load)
        self._stats["model_format"] = self._model._format

        self._detector  = BDVehicleDetector(self._model)
        self._tracker   = DeepSORTTracker(self._s)
        self._analyzer  = ViolationAnalyzer(self._s)
        self._anpr      = ANPRReader(self._s)

        if self._pool:
            await self._analyzer.connect(self._pool)
            await self._load_cameras()

        self._publisher = AlertPublisher(self._s, self._redis)
        if self._pool:
            await self._publisher.load_locations(self._pool)

        # ANPR (PaddleOCR) is heavy and downloads models on first run.
        # Load it in the background so detection starts immediately; plate
        # reads simply return None until the OCR model is ready.
        if self._s.anpr_enabled:
            asyncio.create_task(self._load_anpr_background())
        else:
            log.info("anpr_disabled", note="set ANPR_ENABLED=true to enable plate reading")

        log.info(
            "worker_ready",
            frame_source=self._s.frame_source,
            cameras=self._cameras,
            use_gpu=self._s.use_gpu,
            model_format=self._model._format,
            batch_size=self._s.batch_size,
        )

    async def _load_anpr_background(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._anpr.load)
        except Exception as exc:
            log.warning("anpr_background_load_failed", error=str(exc))

    async def _load_cameras(self) -> None:
        if not self._pool:
            return
        try:
            rows = await self._pool.fetch(
                "SELECT camera_id FROM cameras WHERE active=TRUE ORDER BY camera_id"
            )
            if rows:
                self._cameras = [r["camera_id"] for r in rows]
                self._stats["cameras"] = self._cameras
        except Exception as exc:
            log.warning("camera_list_load_failed", error=str(exc))

        # Also try video-ingest API
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self._s.video_ingest_url}/cameras")
                if resp.status_code == 200:
                    data = resp.json()
                    ids = [c["camera_id"] for c in data if c.get("streaming")]
                    if ids:
                        self._cameras = ids
                        self._stats["cameras"] = ids
        except Exception:
            pass

    async def run(self) -> None:
        src = self._s.frame_source
        if src == "queue" and self._frame_queue is not None:
            await self._run_queue_mode()
        elif src == "mock" or (src == "rtsp" and not self._cameras):
            await self._run_mock_mode()
        else:
            await self._run_rtsp_mode()

    # ── Queue mode ────────────────────────────────────────────────────────────
    async def _run_queue_mode(self) -> None:
        log.info("frame_source_queue")
        loop = asyncio.get_running_loop()
        batch: list[dict] = []
        last_refresh = time.monotonic()

        while not self._stop_event.is_set():
            try:
                msg = await loop.run_in_executor(
                    None, self._frame_queue.get, True, 0.1
                )
                batch.append(msg)
                QUEUE_DEPTH.set(self._frame_queue.qsize())
            except Empty:
                pass
            except Exception as exc:
                log.warning("queue_read_error", error=str(exc))

            if len(batch) >= self._s.batch_size or (batch and len(batch) >= 1):
                await self._process_batch(batch)
                batch = []

            # Periodic refreshes
            if time.monotonic() - last_refresh > min(self._s.zone_refresh_s, 60):
                await self._analyzer.maybe_refresh()
                await self._load_cameras()
                if self._pool:
                    await self._publisher.load_locations(self._pool)
                last_refresh = time.monotonic()

    # ── RTSP mode ─────────────────────────────────────────────────────────────
    async def _run_rtsp_mode(self) -> None:
        log.info("frame_source_rtsp", cameras=self._cameras)
        cam_tasks: dict[str, asyncio.Task] = {}
        last_refresh = time.monotonic()

        while not self._stop_event.is_set():
            # Start tasks for new cameras
            for cam_id in self._cameras:
                if cam_id not in cam_tasks or cam_tasks[cam_id].done():
                    cam_tasks[cam_id] = asyncio.create_task(
                        self._rtsp_reader(cam_id), name=f"rtsp-{cam_id}"
                    )

            # Cancel tasks for removed cameras
            for cam_id in list(cam_tasks):
                if cam_id not in self._cameras and not cam_tasks[cam_id].done():
                    cam_tasks[cam_id].cancel()

            # Periodic zone/camera refresh
            if time.monotonic() - last_refresh > self._s.camera_refresh_s:
                await self._analyzer.maybe_refresh()
                await self._load_cameras()
                if self._pool:
                    await self._publisher.load_locations(self._pool)
                last_refresh = time.monotonic()

            await asyncio.sleep(self._s.camera_refresh_s)

    async def _rtsp_reader(self, camera_id: str) -> None:
        """Read RTSP stream for one camera and feed frames into the pipeline."""
        import importlib.util
        spec = importlib.util.find_spec("video_ingest")
        if spec:
            sys.path.insert(0, "/app")
            from video_ingest.main import FrameStream  # type: ignore
        else:
            # Inline minimal FrameStream if video_ingest not on path
            FrameStream = _LocalFrameStream  # type: ignore

        rtsp_url = f"{self._s.rtsp_base_url}/{camera_id}"
        log.info("rtsp_reader_start", camera_id=camera_id, url=rtsp_url)
        stream = FrameStream(rtsp_url, target_fps=self._s.camera_fps)
        frame_seq = 0

        try:
            async for frame, ts in stream.read():
                if self._stop_event.is_set():
                    break
                frame_640 = cv2.resize(frame, (640, 640)) if frame.shape[:2] != (640, 640) else frame
                meta = {
                    "camera_id":    camera_id,
                    "timestamp_ms": int(ts * 1000),
                    "frame_seq":    frame_seq,
                    "frame":        frame_640,
                }
                frame_seq += 1
                await self._process_batch([meta])
        except asyncio.CancelledError:
            log.info("rtsp_reader_cancelled", camera_id=camera_id)
        except Exception as exc:
            log.error("rtsp_reader_error", camera_id=camera_id, error=str(exc))

    # ── Mock mode ─────────────────────────────────────────────────────────────
    async def _run_mock_mode(self) -> None:
        log.info("frame_source_mock", cameras=self._cameras)
        tasks = [
            asyncio.create_task(self._mock_camera(c), name=f"mock-{c}")
            for c in self._cameras
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _mock_camera(self, camera_id: str) -> None:
        import random
        VIOLATIONS = [
            "red_light_violation", "stop_line_violation", "wrong_lane",
            "helmet_missing", "illegal_parking", "speeding",
        ]
        VEHICLES = ["car", "motorcycle", "bus", "cng", "tempo"]

        while not self._stop_event.is_set():
            interval = 30.0 if not self._s.inject_test_violations else 5.0
            await asyncio.sleep(interval)
            if self._stop_event.is_set():
                break

            x1, y1 = random.randint(50, 400), random.randint(50, 300)
            x2, y2 = x1 + random.randint(80, 200), y1 + random.randint(60, 150)
            frame = np.zeros((640, 640, 3), dtype=np.uint8)
            frame[:] = (25, 30, 40)
            alert_type = random.choice(VIOLATIONS)
            cls_name   = random.choice(VEHICLES)
            confidence = random.uniform(0.62, 0.96)
            severity   = SEVERITY_MAP.get(alert_type, 2)
            if confidence > 0.90:
                severity = min(severity + 1, 4)

            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 200, 255), 2)
            label = f"[MOCK] {alert_type.upper()}"
            cv2.putText(frame, label, (x1, max(y1 - 10, 14)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 200, 255), 2)
            cv2.putText(frame, f"CAM: {camera_id}  {cls_name}  {confidence:.2f}",
                        (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

            _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            snap_b64 = base64.b64encode(buf).decode()
            loc = self._publisher._location_cache.get(camera_id, {}) if self._publisher else {}

            payload = {
                "alert_id":        str(uuid.uuid4()),
                "alert_type":      alert_type,
                "camera_id":       camera_id,
                "confidence":      str(round(confidence, 4)),
                "severity":        str(severity),
                "snapshot_b64":    snap_b64,
                "object_metadata": json.dumps({
                    "track_id":      random.randint(1, 9999),
                    "vehicle_class": cls_name,
                    "mock":          True,
                    "speed_kmh":     round(random.uniform(40, 90), 1) if "speed" in alert_type else 0,
                }),
                "location_name":   loc.get("location_name", f"Zone {camera_id.upper()}"),
                "latitude":        str(loc.get("latitude", 0)),
                "longitude":       str(loc.get("longitude", 0)),
                "frame_ts":        datetime.now(timezone.utc).isoformat(),
                "worker":          "traffic-ai",
            }
            try:
                await self._redis.xadd("alerts:traffic", payload, maxlen=10000)
                ALERTS_TOTAL.labels(alert_type=alert_type, camera_id=camera_id).inc()
                self._stats["alerts_generated"] += 1
                self._stats["last_alert_at"] = datetime.now(timezone.utc).isoformat()
                log.info(
                    "mock_alert_published",
                    alert_type=alert_type,
                    camera_id=camera_id,
                    vehicle=cls_name,
                    confidence=round(confidence, 2),
                )
            except Exception as exc:
                log.error("mock_publish_failed", camera_id=camera_id, error=str(exc))

    # ── Batch processing ──────────────────────────────────────────────────────
    async def _process_batch(self, batch: list[dict]) -> None:
        loop = asyncio.get_running_loop()
        t0   = time.perf_counter()

        # ── Motion pre-filter ────────────────────────────────────────────────
        # Skip expensive YOLO inference on frames where no significant pixel
        # movement is detected.  The background model is still updated each
        # frame, and every 30th no-motion frame is forced through anyway as a
        # safety net for very slow-moving objects.
        active_batch: list[dict] = []
        for meta in batch:
            cam_id = meta["camera_id"]
            frame  = meta["frame"]
            # Run in executor: cv2 ops are CPU-bound but fast (<2 ms per frame)
            has_motion, ratio = await loop.run_in_executor(
                None, self._has_motion, frame, cam_id
            )
            skip_n = self._motion_skip_count.get(cam_id, 0)
            if has_motion or skip_n >= 30:
                # Reset skip counter and include in YOLO batch
                self._motion_skip_count[cam_id] = 0
                active_batch.append(meta)
            else:
                # No motion — update counter, refresh preview with empty overlay
                self._motion_skip_count[cam_id] = skip_n + 1
                self._stats["motion_skipped"] += 1
                if self._s.preview_enabled:
                    try:
                        self._annotate(cam_id, frame, [], [], [])
                    except Exception:
                        pass

        if not active_batch:
            return

        try:
            detections_per_frame = await loop.run_in_executor(
                None, self._detector.detect, active_batch
            )
        except Exception as exc:
            log.error("detection_failed", error=str(exc))
            return

        for i, meta in enumerate(active_batch):
            camera_id   = meta["camera_id"]
            frame       = meta["frame"]
            frame_seq   = meta["frame_seq"]
            ts_ms       = meta["timestamp_ms"]
            dets        = detections_per_frame[i] if i < len(detections_per_frame) else []

            try:
                await self._process_single(camera_id, frame, frame_seq, ts_ms, dets)
            except Exception as exc:
                ERRORS_TOTAL.labels(camera_id=camera_id).inc()
                log.error(
                    "frame_processing_error",
                    camera_id=camera_id,
                    frame_seq=frame_seq,
                    error=str(exc),
                )

        elapsed_ms = (time.perf_counter() - t0) * 1000
        INFERENCE_LATENCY.observe(elapsed_ms)

    async def _process_single(
        self,
        camera_id: str,
        frame:     np.ndarray,
        frame_seq: int,
        ts_ms:     int,
        dets:      list[Detection],
    ) -> None:
        loop = asyncio.get_running_loop()

        # Only pass road-user / vehicle classes into DeepSORT and the violation
        # analyzer.  All detections (all 80 COCO classes) are kept for the
        # annotation overlay (_annotate receives the full `dets` list below).
        violation_dets = [d for d in dets if d.class_id in VIOLATION_CLASSES]

        # Track (violation classes only — tracker doesn't need benches or cats)
        tracks = await loop.run_in_executor(
            None, self._tracker.update, camera_id, violation_dets, frame
        )

        # Helmet check (motorcycle-specific)
        for track in tracks:
            if (
                track["class_name"] == "motorcycle"
                and self._analyzer.check_helmet(track, frame, self._s.helmet_conf_threshold)
            ):
                v = ViolationResult(
                    alert_type   = "helmet_missing",
                    track_id     = track["track_id"],
                    class_name   = "motorcycle",
                    confidence   = 0.72,
                    bbox_xyxy    = track["bbox_xyxy"],
                    zone_id      = "helmet",
                    zone_type    = "helmet",
                    camera_id    = camera_id,
                    timestamp_ms = ts_ms,
                    frame_seq    = frame_seq,
                )
                anpr = self._anpr.read(track, frame, v)
                if self._publisher:
                    await self._publisher.publish(v, anpr, frame, camera_id)
                    self._stats["alerts_generated"] += 1

        # Zone violations (violation classes only)
        violations = await loop.run_in_executor(
            None, self._analyzer.analyze, camera_id, tracks, frame_seq, ts_ms
        )
        for v in violations:
            track = next((t for t in tracks if t["track_id"] == v.track_id), None)
            anpr: ANPRResult | None = None
            if track:
                anpr = await loop.run_in_executor(
                    None, self._anpr.read, track, frame, v
                )
            if self._publisher:
                published = await self._publisher.publish(v, anpr, frame, camera_id)
                if published:
                    self._stats["alerts_generated"] += 1

        # FPS tracking
        FRAMES_TOTAL.labels(camera_id=camera_id).inc()
        self._stats["frames_processed"] += 1
        now = time.monotonic()
        win = self._fps_windows.setdefault(camera_id, deque())
        win.append(now)
        cutoff = now - 1.0
        while win and win[0] < cutoff:
            win.popleft()
        FPS_GAUGE.labels(camera_id=camera_id).set(float(len(win)))

        # Live preview: annotate with ALL detections (all 80 COCO classes) so
        # every detected object gets a bounding box, not just vehicles.
        if self._s.preview_enabled:
            try:
                self._annotate(camera_id, frame, dets, tracks, violations)
            except Exception as exc:
                log.debug("annotate_failed", camera_id=camera_id, error=str(exc))

        # Broadcast structured detection data to WebSocket subscribers so the
        # dashboard can render canvas overlays on the live low-latency stream.
        if detection_subscribers.get(camera_id):
            try:
                payload = self._build_det_payload(camera_id, dets, tracks, violations, frame)
                self._latest_detections[camera_id] = payload["detections"]
                asyncio.create_task(
                    self._broadcast_detections_ws(camera_id, json.dumps(payload))
                )
            except Exception as exc:
                log.debug("ws_broadcast_failed", camera_id=camera_id, error=str(exc))

    def _annotate(
        self,
        camera_id:  str,
        frame:      np.ndarray,
        dets:       list[Detection],
        tracks:     list[dict],
        violations: list[ViolationResult],
    ) -> None:
        """Draw detection/track boxes onto the frame and cache the JPEG."""
        img = frame.copy()
        viol_ids = {v.track_id for v in violations}
        # Prefer confirmed tracks (with IDs); fall back to raw detections so
        # boxes appear immediately before DeepSORT confirms a track.
        if tracks:
            # Confirmed tracks: show class + track ID; red for violators.
            for tk in tracks:
                x1, y1, x2, y2 = (int(v) for v in tk["bbox_xyxy"])
                is_viol = tk["track_id"] in viol_ids
                color = (0, 0, 255) if is_viol else (0, 200, 0)
                cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
                cv2.putText(
                    img, f'{tk["class_name"]} #{tk["track_id"]}',
                    (x1, max(y1 - 6, 12)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1,
                )

            # Also draw any non-violation-class detections (people, animals,
            # objects) that DeepSORT never saw — they're real detections too.
            tracked_approx = {
                (int(tk["bbox_xyxy"][0]), int(tk["bbox_xyxy"][1])) for tk in tracks
            }
            for d in dets:
                if d.class_id in VIOLATION_CLASSES:
                    continue  # already drawn via tracks above
                x1, y1, x2, y2 = (int(v) for v in d.bbox_xyxy)
                cv2.rectangle(img, (x1, y1), (x2, y2), (255, 165, 0), 1)
                cv2.putText(
                    img, f"{d.class_name} {d.confidence:.2f}",
                    (x1, max(y1 - 6, 12)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 165, 0), 1,
                )
            object_count = len(dets)
        else:
            # No confirmed tracks yet — draw all raw detections.
            for d in dets:
                x1, y1, x2, y2 = (int(v) for v in d.bbox_xyxy)
                cv2.rectangle(img, (x1, y1), (x2, y2), (0, 200, 0), 2)
                cv2.putText(
                    img, f"{d.class_name} {d.confidence:.2f}",
                    (x1, max(y1 - 6, 12)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 0), 1,
                )
            object_count = len(dets)

        fps = len(self._fps_windows.get(camera_id, ()))
        banner = f"{camera_id}  objects:{object_count}  fps:{fps}"
        cv2.rectangle(img, (0, 0), (img.shape[1], 24), (0, 0, 0), -1)
        cv2.putText(img, banner, (6, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1)

        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if ok:
            self._latest_jpeg[camera_id] = buf.tobytes()

    async def stop(self) -> None:
        self._stop_event.set()
        if self._redis:
            await self._redis.aclose()
        if self._pool:
            await self._pool.close()
        log.info("worker_stopped")

    # ── Motion detection helpers ──────────────────────────────────────────────

    def _get_bg_subtractor(self, camera_id: str) -> cv2.BackgroundSubtractor:
        if camera_id not in self._bg_subtractors:
            self._bg_subtractors[camera_id] = cv2.createBackgroundSubtractorMOG2(
                history=500, varThreshold=50, detectShadows=False
            )
        return self._bg_subtractors[camera_id]

    def _has_motion(self, frame: np.ndarray, camera_id: str, min_area: int = 1500) -> tuple[bool, float]:
        """
        Returns (has_motion, motion_ratio).

        Always feeds the frame to the background model so the model stays
        updated even on skipped frames.  Only returns True when movement
        covering > 0.3 % of the frame is detected after morphological cleanup.
        """
        bgsub = self._get_bg_subtractor(camera_id)
        gray  = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        fg    = bgsub.apply(gray)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, kernel)
        contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        motion_px = sum(cv2.contourArea(c) for c in contours if cv2.contourArea(c) > min_area)
        ratio = motion_px / max(frame.shape[0] * frame.shape[1], 1)
        return ratio > 0.003, round(ratio, 5)

    # ── Detection WebSocket broadcast ─────────────────────────────────────────

    def _build_det_payload(
        self,
        camera_id:  str,
        dets:       list,
        tracks:     list,
        violations: list,
        frame:      np.ndarray,
    ) -> dict:
        """Serialise detections + tracks into a JSON-safe dict for WS clients."""
        viol_map: dict[int, str] = {v.track_id: v.alert_type for v in violations}
        items: list[dict] = []

        if tracks:
            for tk in tracks:
                x1, y1, x2, y2 = [int(v) for v in tk["bbox_xyxy"]]
                items.append({
                    "bbox":       [x1, y1, x2, y2],
                    "class":      tk["class_name"],
                    "track_id":   tk["track_id"],
                    "confidence": round(float(tk.get("confidence", 0.8)), 2),
                    "violation":  viol_map.get(tk["track_id"]),
                })
            # Non-vehicle detections not handled by DeepSORT
            tracked_cls = {c for c in VIOLATION_CLASSES}
            for d in dets:
                if d.class_id in tracked_cls:
                    continue
                x1, y1, x2, y2 = [int(v) for v in d.bbox_xyxy]
                items.append({
                    "bbox":       [x1, y1, x2, y2],
                    "class":      d.class_name,
                    "track_id":   None,
                    "confidence": round(float(d.confidence), 2),
                    "violation":  None,
                })
        else:
            for d in dets:
                x1, y1, x2, y2 = [int(v) for v in d.bbox_xyxy]
                items.append({
                    "bbox":       [x1, y1, x2, y2],
                    "class":      d.class_name,
                    "track_id":   None,
                    "confidence": round(float(d.confidence), 2),
                    "violation":  None,
                })

        return {
            "type":        "detections",
            "camera_id":   camera_id,
            "ts":          int(time.time() * 1000),
            "frame_w":     frame.shape[1],
            "frame_h":     frame.shape[0],
            "detections":  items,
            "object_count": len(items),
        }

    async def _broadcast_detections_ws(self, camera_id: str, payload_str: str) -> None:
        """Fire-and-forget send to all WebSocket subscribers for this camera."""
        subs = detection_subscribers.get(camera_id)
        if not subs:
            return
        dead: set = set()
        for ws in list(subs):
            try:
                await ws.send_str(payload_str)
            except Exception:
                dead.add(ws)
        subs -= dead


# ── Inline FrameStream (fallback when video_ingest not on path) ───────────────
class _LocalFrameStream:
    def __init__(self, rtsp_url: str, target_fps: int = 15) -> None:
        self.rtsp_url   = rtsp_url
        self._interval  = 1.0 / max(1, min(target_fps, 25))

    async def read(self):  # type: ignore[override]
        loop = asyncio.get_event_loop()
        import os as _os
        _os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
        cap = await loop.run_in_executor(None, self._open)
        if not cap or not cap.isOpened():
            log.error("rtsp_open_failed", url=self.rtsp_url)
            return
        try:
            while True:
                t0 = time.monotonic()
                ret, frame = await loop.run_in_executor(None, cap.read)
                if not ret or frame is None:
                    await asyncio.sleep(2)
                    cap.release()
                    cap = await loop.run_in_executor(None, self._open)
                    continue
                yield frame, time.time()
                wait = self._interval - (time.monotonic() - t0)
                if wait > 0:
                    await asyncio.sleep(wait)
        finally:
            cap.release()

    def _open(self) -> cv2.VideoCapture:
        cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 8000)
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 8000)
        return cap


# ── FastAPI health / metrics server ───────────────────────────────────────────
_worker_ref: TrafficWorker | None = None

# Camera-id → set of live aiohttp WebSocket connections for detection streaming
detection_subscribers: dict[str, set] = {}


async def _health_handler(_: web.Request) -> web.Response:
    stats = _worker_ref._stats if _worker_ref else {}
    return web.json_response({"status": "ok", "service": "traffic-ai", **stats})


async def _metrics_handler(_: web.Request) -> web.Response:
    return web.Response(
        body=generate_latest(),
        content_type=CONTENT_TYPE_LATEST.split(";")[0],
    )


async def _preview_index(_: web.Request) -> web.Response:
    """HTML grid of live annotated MJPEG streams — one tile per camera."""
    cams: list[str] = []
    if _worker_ref:
        cams = list(_worker_ref._latest_jpeg.keys()) or list(_worker_ref._cameras)
    tiles = "".join(
        f'<div class="tile"><div class="cap">{c}</div>'
        f'<img src="/preview/{c}.mjpg" alt="{c}"/></div>'
        for c in cams
    )
    body = tiles or '<p style="padding:16px">No active cameras yet — waiting for frames…</p>'
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='30'>"
        "<title>Traffic AI — Live Detection</title><style>"
        "body{margin:0;background:#0b0f17;color:#e5e7eb;"
        "font-family:system-ui,-apple-system,sans-serif}"
        "h1{font-size:15px;padding:12px 16px;margin:0;border-bottom:1px solid #1f2937}"
        ".sub{color:#9ca3af;font-weight:400}"
        ".grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(440px,1fr));"
        "gap:12px;padding:16px}"
        ".tile{background:#111827;border:1px solid #1f2937;border-radius:10px;overflow:hidden}"
        ".cap{padding:6px 10px;font-size:13px;color:#9ca3af}"
        "img{width:100%;display:block;background:#000}"
        "</style></head><body>"
        "<h1>Traffic AI — Realtime Object Detection <span class='sub'>(YOLO · green=object, red=violation)</span></h1>"
        f"<div class='grid'>{body}</div></body></html>"
    )
    return web.Response(text=html, content_type="text/html")


async def _preview_stream(request: web.Request) -> web.StreamResponse:
    """multipart/x-mixed-replace MJPEG stream of the latest annotated frame."""
    cam = request.match_info["camera_id"]
    if cam.endswith(".mjpg"):
        cam = cam[:-5]
    boundary = "frame"
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": f"multipart/x-mixed-replace; boundary={boundary}",
            "Cache-Control": "no-cache, private",
            "Pragma": "no-cache",
        },
    )
    await resp.prepare(request)
    try:
        while True:
            jpeg = _worker_ref._latest_jpeg.get(cam) if _worker_ref else None
            if jpeg:
                await resp.write(
                    b"--" + boundary.encode() + b"\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                    + jpeg + b"\r\n"
                )
            await asyncio.sleep(0.1)
    except (asyncio.CancelledError, ConnectionResetError, ConnectionError):
        pass
    return resp


async def _detection_ws_handler(request: web.Request) -> web.WebSocketResponse:
    """
    WebSocket endpoint: `GET /detections/{camera_id}/ws`

    The dashboard connects here to receive real-time structured detection data
    (bounding boxes, class labels, track IDs, violations) without the overhead
    of re-encoding a full MJPEG stream.  The frontend draws bboxes on a
    <canvas> overlay on top of the low-latency WebRTC/HLS live stream.

    Each message is a JSON object:
    {
      "type":        "detections",
      "camera_id":   "cam01",
      "ts":          1718000000000,   // epoch ms
      "frame_w":     1280,
      "frame_h":     720,
      "object_count": 3,
      "detections":  [
        {"bbox": [x1,y1,x2,y2], "class": "car", "track_id": 42,
         "confidence": 0.87, "violation": "red_light_violation"},
        ...
      ]
    }
    """
    cam_id = request.match_info["camera_id"]
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)

    # Register subscriber
    if cam_id not in detection_subscribers:
        detection_subscribers[cam_id] = set()
    detection_subscribers[cam_id].add(ws)
    log.info("detection_ws_connected", camera_id=cam_id,
             subscribers=len(detection_subscribers[cam_id]))

    # Send last known detection snapshot immediately on connect
    if _worker_ref and cam_id in _worker_ref._latest_detections:
        try:
            snap = {
                "type":       "detections",
                "camera_id":  cam_id,
                "ts":         int(time.time() * 1000),
                "frame_w":    640, "frame_h": 360,
                "detections": _worker_ref._latest_detections[cam_id],
                "object_count": len(_worker_ref._latest_detections[cam_id]),
            }
            await ws.send_str(json.dumps(snap))
        except Exception:
            pass

    try:
        async for msg in ws:
            # Clients may send "ping" keep-alives
            if msg.type == web.WSMsgType.TEXT and msg.data == "ping":
                await ws.send_str("pong")
    except asyncio.CancelledError:
        pass
    finally:
        detection_subscribers.get(cam_id, set()).discard(ws)
        log.info("detection_ws_disconnected", camera_id=cam_id)

    return ws


async def _detection_snapshot_handler(request: web.Request) -> web.Response:
    """GET /detections/{camera_id}/latest — latest detections as JSON (no WS needed)."""
    cam_id = request.match_info["camera_id"]
    dets = _worker_ref._latest_detections.get(cam_id, []) if _worker_ref else []
    return web.json_response({
        "camera_id":   cam_id,
        "ts":          int(time.time() * 1000),
        "detections":  dets,
        "object_count": len(dets),
    })


async def _motion_stats_handler(_: web.Request) -> web.Response:
    """GET /motion/stats — per-camera motion skip counts for diagnostics."""
    if not _worker_ref:
        return web.json_response({"error": "worker not ready"}, status=503)
    return web.json_response({
        cam: {
            "skip_count": _worker_ref._motion_skip_count.get(cam, 0),
            "total_skipped": _worker_ref._stats.get("motion_skipped", 0),
        }
        for cam in _worker_ref._cameras
    })


async def _start_health_server(port: int) -> web.AppRunner:
    sapp = web.Application()
    # CORS for dashboard (all origins in dev)
    async def _cors(request: web.Request, handler):
        resp = await handler(request)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "*"
        return resp
    sapp.middlewares.append(_cors)

    sapp.router.add_get("/health",   _health_handler)
    sapp.router.add_get("/metrics",  _metrics_handler)
    sapp.router.add_get("/",         _preview_index)
    sapp.router.add_get("/preview",  _preview_index)
    sapp.router.add_get("/preview/{camera_id}", _preview_stream)
    # Detection WebSocket and REST snapshot
    sapp.router.add_get("/detections/{camera_id}/ws",     _detection_ws_handler)
    sapp.router.add_get("/detections/{camera_id}/latest", _detection_snapshot_handler)
    sapp.router.add_get("/motion/stats", _motion_stats_handler)

    runner = web.AppRunner(sapp)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("health_server_started", port=port)
    return runner


# ── main ──────────────────────────────────────────────────────────────────────
async def _amain() -> None:
    global _worker_ref

    settings = Settings.from_env()
    configure_logging(settings.log_level)

    worker = TrafficWorker(settings)
    _worker_ref = worker

    loop = asyncio.get_running_loop()
    runner = await _start_health_server(settings.health_port)

    def _on_signal(sig: str) -> None:
        log.info("signal_received", signal=sig)
        loop.create_task(worker.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _on_signal, sig.name)

    await worker.start()
    try:
        await worker.run()
    finally:
        await worker.stop()
        await runner.cleanup()
        log.info("shutdown_complete")


if __name__ == "__main__":
    asyncio.run(_amain())
