"""
face_ai/worker.py
──────────────────
Face recognition worker.
- Detects faces in frames using InsightFace (RetinaFace detector)
- Extracts 512-dim ArcFace embeddings
- Queries FAISS index loaded from PostgreSQL watchlist
- Publishes matches to Redis Streams with confidence score
- Sub-5 second latency target (camera to alert)
- MOCK_MODE for local dev without GPU
"""

import asyncio
import base64
import json
import logging
import os
import random
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg
import cv2
import numpy as np
import redis.asyncio as aioredis
from fastapi import FastAPI

log = logging.getLogger("face-ai")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

REDIS_URL          = os.getenv("REDIS_URL",       "redis://localhost:6379")
RTSP_BASE_URL      = os.getenv("RTSP_BASE_URL",   "rtsp://localhost:8554")
DATABASE_URL       = os.getenv("DATABASE_URL",    "postgresql://policeai:policeai_dev_secret@localhost:5432/policeai")
CAMERA_IDS         = os.getenv("CAMERA_IDS",      "cam01").split(",")
MOCK_MODE          = os.getenv("MOCK_MODE",       "true").lower() == "true"
FACE_MATCH_THRESHOLD = float(os.getenv("FACE_MATCH_THRESHOLD", "0.75"))
REDIS_STREAM       = "alerts:face"
WATCHLIST_RELOAD_INTERVAL = 300   # reload watchlist every 5 minutes

app = FastAPI(title="Face AI Worker")
_stats: dict[str, Any] = {
    "faces_detected": 0,
    "watchlist_matches": 0,
    "watchlist_size": 0,
    "last_reload_at": None,
    "mock_mode": MOCK_MODE,
}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "face-ai", **_stats}


# ── Watchlist loader ──────────────────────────
class WatchlistIndex:
    """
    Loads face embeddings from PostgreSQL into a FAISS index
    for fast approximate nearest-neighbour search.

    In production with InsightFace:
        embeddings = insightface_model.get(face_crop).embedding  # shape (512,)
        Then query this index.
    """

    def __init__(self):
        self.index     = None      # faiss.IndexFlatIP
        self.ids: list[dict] = []  # metadata parallel to FAISS index
        self.loaded_at = 0.0

    async def load(self, db_pool: asyncpg.Pool):
        """Load all active watchlist face embeddings from DB into FAISS."""
        try:
            import faiss
        except ImportError:
            log.warning("faiss not installed — watchlist matching disabled")
            return

        rows = await db_pool.fetch(
            """SELECT wf.id, wf.watchlist_id, wf.embedding,
                      w.name, w.risk_category, w.nid
               FROM watchlist_faces wf
               JOIN watchlist w ON wf.watchlist_id = w.id
               WHERE w.active = TRUE
               ORDER BY w.risk_category"""
        )

        if not rows:
            log.info("Watchlist is empty")
            return

        # Build embedding matrix — shape (N, 512)
        vectors = np.array([
            np.frombuffer(bytes(r["embedding"]), dtype=np.float32)
            for r in rows
        ], dtype=np.float32)

        # L2-normalise for cosine similarity via inner product
        faiss.normalize_L2(vectors)

        index = faiss.IndexFlatIP(512)
        index.add(vectors)

        self.index = index
        self.ids   = [
            {"face_id":       str(r["id"]),
             "watchlist_id":  str(r["watchlist_id"]),
             "name":          r["name"],
             "risk_category": r["risk_category"],
             "nid":           r["nid"]}
            for r in rows
        ]
        self.loaded_at      = time.time()
        _stats["watchlist_size"]  = len(rows)
        _stats["last_reload_at"]  = datetime.now(timezone.utc).isoformat()
        log.info(f"Watchlist loaded: {len(rows)} faces into FAISS index")

    def search(self, embedding: np.ndarray, top_k: int = 3
               ) -> list[dict]:
        """
        Search FAISS index for closest faces.
        Returns list of {name, risk_category, similarity, face_id}.
        """
        if self.index is None or self.index.ntotal == 0:
            return []

        vec = embedding.reshape(1, -1).astype(np.float32)
        import faiss
        faiss.normalize_L2(vec)
        D, I = self.index.search(vec, min(top_k, self.index.ntotal))

        results = []
        for dist, idx in zip(D[0], I[0]):
            if idx < 0 or dist < FACE_MATCH_THRESHOLD:
                continue
            results.append({**self.ids[idx], "similarity": float(dist)})
        return results


# ── Redis publisher ───────────────────────────
async def publish_face_alert(redis: aioredis.Redis, camera_id: str,
                              match: dict, snapshot_b64: str):
    risk_to_severity = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    payload = {
        "alert_id":        str(uuid.uuid4()),
        "alert_type":      "face_match",
        "camera_id":       camera_id,
        "confidence":      str(round(match["similarity"], 4)),
        "severity":        str(risk_to_severity.get(match["risk_category"], 2)),
        "snapshot_b64":    snapshot_b64,
        "object_metadata": json.dumps({
            "matched_name":     match["name"],
            "risk_category":    match["risk_category"],
            "watchlist_id":     match["watchlist_id"],
            "nid":              match.get("nid"),
            "similarity_score": match["similarity"],
        }),
        "frame_ts":        datetime.now(timezone.utc).isoformat(),
        "worker":          "face-ai",
    }
    msg_id = await redis.xadd(REDIS_STREAM, payload, maxlen=5000)
    log.info(f"Face match alert → {REDIS_STREAM} [{msg_id}]: "
             f"{match['name']} ({match['risk_category']}) "
             f"sim={match['similarity']:.3f} on {camera_id}")
    _stats["watchlist_matches"] += 1


# ── Mock worker (no GPU) ──────────────────────
async def mock_face_worker(redis: aioredis.Redis, camera_id: str):
    log.info(f"[MOCK] Face worker started for {camera_id}")
    mock_suspects = [
        {"name": "Test Subject A", "risk_category": "high",
         "similarity": 0.88, "watchlist_id": str(uuid.uuid4()),
         "face_id": str(uuid.uuid4()), "nid": "TEST001"},
        {"name": "Test Subject B", "risk_category": "medium",
         "similarity": 0.79, "watchlist_id": str(uuid.uuid4()),
         "face_id": str(uuid.uuid4()), "nid": "TEST002"},
    ]
    while True:
        await asyncio.sleep(random.uniform(30, 120))  # rare alerts
        match = random.choice(mock_suspects)

        # Synthetic face snapshot
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        frame[:] = (20, 20, 30)
        # Draw a fake face circle
        cx, cy = random.randint(200, 440), random.randint(100, 380)
        cv2.circle(frame, (cx, cy), 60, (180, 150, 120), -1)
        cv2.circle(frame, (cx, cy), 60, (0, 255, 0), 2)
        cv2.putText(frame, f"MATCH: {match['name']}", (cx - 70, cy - 75),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2)
        cv2.putText(frame, f"SIM: {match['similarity']:.2f}", (cx - 40, cy + 85),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        snapshot_b64 = base64.b64encode(buf).decode()

        await publish_face_alert(redis, camera_id, match, snapshot_b64)


# ── Real worker (InsightFace + FAISS, GPU) ─────
async def real_face_worker(redis: aioredis.Redis, camera_id: str,
                            watchlist: WatchlistIndex):
    """
    Production face recognition worker.
    Requires: insightface, faiss-gpu, NVIDIA GPU, CUDA 12.
    """
    import insightface
    import sys
    sys.path.insert(0, "/app")
    from video_ingest.main import FrameStream

    # Load InsightFace model (buffalo_l = best accuracy)
    face_app = insightface.app.FaceAnalysis(name="buffalo_l",
                                             providers=["CUDAExecutionProvider"])
    face_app.prepare(ctx_id=0, det_size=(640, 640))
    log.info("InsightFace model loaded (buffalo_l)")

    # Dedup: don't re-alert same track within 60s for face matches
    dedup: dict[str, float] = {}

    rtsp_url = f"{RTSP_BASE_URL}/{camera_id}"
    stream   = FrameStream(rtsp_url, target_fps=2)  # Face at 2fps — expensive

    async for frame, ts in stream.read():
        _stats["faces_detected"] += 0  # will increment below

        # Reload watchlist if stale
        if time.time() - watchlist.loaded_at > WATCHLIST_RELOAD_INTERVAL:
            # Will be handled by the reload loop
            pass

        # Run InsightFace detection + embedding
        faces = face_app.get(frame)
        if not faces:
            continue

        _stats["faces_detected"] += len(faces)

        for face in faces:
            det_score = float(face.det_score)
            if det_score < 0.70:    # low-quality detection — skip
                continue

            embedding = face.embedding   # shape (512,) float32
            matches   = watchlist.search(embedding)

            if not matches:
                continue

            top_match = matches[0]

            # Dedup
            dedup_key = f"{camera_id}:{top_match['watchlist_id']}"
            now = time.time()
            if dedup.get(dedup_key, 0) + 60 > now:
                continue
            dedup[dedup_key] = now

            # Draw face box + match on snapshot
            bbox = face.bbox.astype(int)
            snapshot = frame.copy()
            cv2.rectangle(snapshot, tuple(bbox[:2]), tuple(bbox[2:4]),
                          (0, 255, 0), 2)
            cv2.putText(snapshot,
                        f"MATCH: {top_match['name']} ({top_match['similarity']:.2f})",
                        (bbox[0], max(bbox[1] - 12, 20)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
            _, buf = cv2.imencode(".jpg", snapshot, [cv2.IMWRITE_JPEG_QUALITY, 85])
            snapshot_b64 = base64.b64encode(buf).decode()

            await publish_face_alert(redis, camera_id, top_match, snapshot_b64)


# ── Watchlist reload loop ─────────────────────
async def watchlist_reload_loop(watchlist: WatchlistIndex, db_pool: asyncpg.Pool):
    while True:
        await watchlist.load(db_pool)
        await asyncio.sleep(WATCHLIST_RELOAD_INTERVAL)


# ── Entry point ───────────────────────────────
async def main():
    redis    = aioredis.from_url(REDIS_URL, decode_responses=True)
    db_pool  = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
    watchlist = WatchlistIndex()

    log.info(f"Face AI worker starting — cameras={CAMERA_IDS} mock={MOCK_MODE}")

    if not MOCK_MODE:
        await watchlist.load(db_pool)
        tasks = [
            watchlist_reload_loop(watchlist, db_pool),
            *[real_face_worker(redis, c.strip(), watchlist) for c in CAMERA_IDS]
        ]
    else:
        tasks = [mock_face_worker(redis, c.strip()) for c in CAMERA_IDS]

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    import uvicorn, threading
    threading.Thread(target=lambda: uvicorn.run(app, host="0.0.0.0",
                     port=8003, log_level="warning"), daemon=True).start()
    asyncio.run(main())
