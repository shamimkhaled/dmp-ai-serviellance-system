# ── Traffic AI Worker — Docker image ─────────────────────────────────────────
# Dev:  python:3.11-slim  (CPU only, MOCK_MODE=true)
# Prod: nvcr.io/nvidia/tensorrt:23.10-py3  (A100 + TRT 8.6, swap base image)

FROM python:3.11-slim

WORKDIR /app

# System libraries needed by OpenCV, PaddleOCR, and shapely
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxrender1 \
    libxext6 \
    libgomp1 \
    wget \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy both the legacy mock worker and the new production worker
COPY worker.py              .
COPY traffic_ai_worker.py   .
COPY fine_tune_bd_vehicles.py .

EXPOSE 8002

# Use traffic_ai_worker.py by default.
# FRAME_SOURCE=mock  → synthetic alerts (local dev, no GPU)
# FRAME_SOURCE=rtsp  → live MediaMTX RTSP streams
# FRAME_SOURCE=queue → multiprocessing.Queue from ingest_pipeline (same node)
CMD ["python", "traffic_ai_worker.py"]
