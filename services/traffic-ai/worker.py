"""
traffic_ai/worker.py  [LEGACY — DO NOT USE]
───────────────────────────────────────────
This is the original simplified mock worker kept for reference only.
The active entry point is traffic_ai_worker.py (see Dockerfile CMD).

traffic_ai_worker.py supersedes this file with:
  - Full 80-class COCO detection + 3 Bangladesh-specific classes
  - DeepSORT multi-object tracking
  - Zone-based violation analysis (red light, stop line, wrong lane,
    no parking, speed) loaded from camera_zones DB table
  - PaddleOCR ANPR plate reading
  - Annotated MJPEG preview endpoint (/preview/{camera_id}.mjpg)
  - Prometheus metrics + structured JSON logging
  - FRAME_SOURCE=rtsp|queue|mock selection via env var

Do not add features here.  Modify traffic_ai_worker.py instead.
"""

import asyncio
import json
import os
import random
import time
import uuid
import logging
import base64
from datetime import datetime, timezone
from typing import Any

import cv2
import numpy as np
import redis.asyncio as aioredis
from fastapi import FastAPI

log = logging.getLogger("traffic-ai")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

REDIS_URL          = os.getenv("REDIS_URL",       "redis://localhost:6379")
RTSP_BASE_URL      = os.getenv("RTSP_BASE_URL",    "rtsp://localhost:8554")
CAMERA_IDS         = os.getenv("CAMERA_IDS",       "cam01").split(",")
MOCK_MODE          = os.getenv("MOCK_MODE",        "true").lower() == "true"
CONFIDENCE_MIN     = float(os.getenv("CONFIDENCE_MIN", "0.55"))

# Redis Streams channel for all traffic alerts
REDIS_STREAM       = "alerts:traffic"

# ── YOLO class mapping ────────────────────────
VEHICLE_CLASSES = {
    2: "car", 3: "motorcycle", 5: "bus", 7: "truck",
    1: "bicycle", 8: "truck_large"
}

VIOLATION_TYPES = [
    "red_light_violation",
    "stop_line_violation",
    "wrong_lane",
    "helmet_missing",
    "illegal_parking",
    "speeding_estimated",
]

# ── FastAPI status endpoint ───────────────────
app = FastAPI(title="Traffic AI Worker")
_worker_stats: dict[str, Any] = {
    "alerts_generated": 0,
    "frames_processed": 0,
    "last_alert_at": None,
    "cameras": CAMERA_IDS,
    "mock_mode": MOCK_MODE,
}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "traffic-ai", **_worker_stats}


# ── Redis publisher ───────────────────────────
async def publish_alert(redis: aioredis.Redis, alert: dict):
    """
    Publish alert candidate to Redis Streams.
    Schema matches alert-service consumer expectations.
    """
    payload = {
        "alert_id":       str(uuid.uuid4()),
        "alert_type":     alert["alert_type"],
        "camera_id":      alert["camera_id"],
        "confidence":     str(round(alert["confidence"], 4)),
        "severity":       str(alert.get("severity", 2)),
        "snapshot_b64":   alert.get("snapshot_b64", ""),
        "object_metadata": json.dumps(alert.get("metadata", {})),
        "location_name":  alert.get("location_name", ""),
        "latitude":       str(alert.get("latitude", 0)),
        "longitude":      str(alert.get("longitude", 0)),
        "frame_ts":       datetime.now(timezone.utc).isoformat(),
        "worker":         "traffic-ai",
    }
    msg_id = await redis.xadd(REDIS_STREAM, payload, maxlen=10000)
    log.info(f"Alert published → {REDIS_STREAM} [{msg_id}]: "
             f"{alert['alert_type']} on {alert['camera_id']} "
             f"confidence={alert['confidence']:.2f}")
    _worker_stats["alerts_generated"] += 1
    _worker_stats["last_alert_at"] = datetime.now(timezone.utc).isoformat()


# ── Mock mode (local dev without GPU) ─────────
async def mock_worker(redis: aioredis.Redis, camera_id: str):
    """
    Generates realistic synthetic alerts on a random schedule.
    Useful for testing the full pipeline without GPU hardware.
    """
    log.info(f"[MOCK] Traffic worker started for {camera_id}")
    while True:
        # Random inter-alert interval: 10–60 seconds
        await asyncio.sleep(random.uniform(10, 60))

        violation = random.choice(VIOLATION_TYPES)
        confidence = random.uniform(0.60, 0.97)

        # Simulate a bounding box
        x1, y1 = random.randint(50, 400), random.randint(50, 300)
        x2, y2 = x1 + random.randint(80, 200), y1 + random.randint(60, 150)

        # Create a fake coloured frame for snapshot
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        frame[:] = (30, 30, 40)
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 200, 255), 2)
        cv2.putText(frame, f"[MOCK] {violation}", (x1, y1 - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 255), 2)
        cv2.putText(frame, f"CAM: {camera_id}  CONF: {confidence:.2f}",
                    (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        snapshot_b64 = base64.b64encode(buf).decode()

        await publish_alert(redis, {
            "alert_type":     violation,
            "camera_id":      camera_id,
            "confidence":     confidence,
            "severity":       2 if confidence < 0.8 else 3,
            "snapshot_b64":   snapshot_b64,
            "location_name":  f"Test Zone {camera_id.upper()}",
            "metadata": {
                "bbox":         [x1, y1, x2, y2],
                "vehicle_type": random.choice(["car", "motorcycle", "bus"]),
                "track_id":     random.randint(1, 999),
                "mock":         True,
            }
        })


# ── Real inference mode (GPU required) ────────
async def real_worker(redis: aioredis.Redis, camera_id: str):
    """
    Production traffic AI worker.
    Requires: ultralytics (YOLO v8), NVIDIA GPU, CUDA 12.
    """
    # Import here so local dev (MOCK_MODE=true) works without GPU libs
    from ultralytics import YOLO
    import sys
    sys.path.insert(0, "/app")
    from video_ingest.main import FrameStream

    # Load YOLO model — use TensorRT engine in production for 3-5x speedup
    # model_path = "/app/models/yolov8n_traffic_tensorrt.engine"  # production
    model_path   = os.getenv("YOLO_MODEL_PATH", "yolov8n.pt")    # dev fallback
    model        = YOLO(model_path)
    log.info(f"YOLO model loaded: {model_path}")

    # Deduplication: don't re-alert same track within window
    dedup: dict[str, float] = {}     # track_key → last_alert_time
    DEDUP_WINDOW = 30.0              # seconds

    rtsp_url = f"{RTSP_BASE_URL}/{camera_id}"
    stream   = FrameStream(rtsp_url, target_fps=5)

    async for frame, ts in stream.read():
        _worker_stats["frames_processed"] += 1

        # Run inference on GPU
        results = model.predict(frame, conf=CONFIDENCE_MIN,
                                device="cuda:0", verbose=False)[0]

        for box in results.boxes:
            cls_id     = int(box.cls[0])
            confidence = float(box.conf[0])
            bbox       = box.xyxy[0].tolist()

            if cls_id not in VEHICLE_CLASSES:
                continue

            vehicle_type = VEHICLE_CLASSES[cls_id]
            violation    = classify_violation(bbox, vehicle_type, frame.shape)

            if not violation:
                continue

            # Dedup: same camera + violation type within window
            dedup_key = f"{camera_id}:{violation}"
            now = time.time()
            if dedup.get(dedup_key, 0) + DEDUP_WINDOW > now:
                continue
            dedup[dedup_key] = now

            # Crop snapshot around the violation bounding box
            x1, y1, x2, y2 = [int(v) for v in bbox]
            snapshot = draw_alert_frame(frame, bbox, violation, confidence)
            _, buf = cv2.imencode(".jpg", snapshot, [cv2.IMWRITE_JPEG_QUALITY, 85])
            snapshot_b64 = base64.b64encode(buf).decode()

            await publish_alert(redis, {
                "alert_type":   violation,
                "camera_id":    camera_id,
                "confidence":   confidence,
                "severity":     compute_severity(violation, confidence),
                "snapshot_b64": snapshot_b64,
                "metadata": {
                    "bbox":          [x1, y1, x2, y2],
                    "vehicle_type":  vehicle_type,
                    "frame_ts":      ts,
                }
            })


def classify_violation(bbox: list, vehicle_type: str,
                        frame_shape: tuple) -> str | None:
    """
    Rule-based violation classifier.
    In production: replace with a trained violation classifier head on top of YOLO.
    """
    h, w = frame_shape[:2]
    x1, y1, x2, y2 = bbox

    # Helmet check — only for motorcycles
    if vehicle_type == "motorcycle":
        # TODO: add helmet classifier model
        return "helmet_missing"   # placeholder

    # Stop-line check — bottom quarter of frame
    if y2 > h * 0.75:
        return "stop_line_violation"

    # Wrong lane — centre strip area
    if x1 > w * 0.4 and x2 < w * 0.6:
        return "wrong_lane"

    return None


def compute_severity(violation: str, confidence: float) -> int:
    """Map violation type + confidence to L1–L4 severity."""
    base = {
        "red_light_violation":  3,
        "wrong_lane":           3,
        "helmet_missing":       2,
        "speeding_estimated":   3,
        "stop_line_violation":  2,
        "illegal_parking":      1,
    }.get(violation, 2)
    if confidence > 0.90:
        return min(base + 1, 4)
    return base


def draw_alert_frame(frame: np.ndarray, bbox: list,
                     violation: str, confidence: float) -> np.ndarray:
    """Draw bounding box and alert label on frame."""
    img = frame.copy()
    x1, y1, x2, y2 = [int(v) for v in bbox]
    cv2.rectangle(img, (x1, y1), (x2, y2), (0, 0, 255), 3)
    label = f"{violation.replace('_', ' ').upper()} {confidence:.2f}"
    cv2.putText(img, label, (x1, max(y1 - 12, 20)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 0, 255), 2)
    return img


# ── Entry point ───────────────────────────────
async def main():
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    log.info(f"Traffic AI worker starting — cameras={CAMERA_IDS} mock={MOCK_MODE}")

    worker_fn = mock_worker if MOCK_MODE else real_worker

    # Launch one worker coroutine per camera in parallel
    tasks = [worker_fn(redis, cam_id.strip()) for cam_id in CAMERA_IDS]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    import uvicorn
    import threading

    # Run FastAPI in a thread, asyncio worker in main thread
    def run_api():
        uvicorn.run(app, host="0.0.0.0", port=8002, log_level="warning")

    threading.Thread(target=run_api, daemon=True).start()
    asyncio.run(main())
