"""
services/traffic-ai/fine_tune_bd_vehicles.py
═════════════════════════════════════════════
Fine-tune YOLOv8m to detect Bangladesh-specific vehicle classes:
  class 6: CNG (compressed natural gas auto-rickshaw)
  class 7: tempo / human-hauler (three-wheeled passenger vehicle)
  class 8: battery_van / easy-bike (electric cargo / passenger van)

Base model: YOLOv8m pretrained on COCO (already detects car, motorcycle,
bus, truck, bicycle, person). We freeze the first 10 backbone layers
(freeze=10) and fine-tune only the detection head on 500–1000 annotated
frames from actual Bangladesh camera sites.

Usage:
  1. Collect 500–1000 images of CNG / tempo / battery_van from your cameras.
  2. Annotate with Roboflow, CVAT, or Label Studio.
  3. Export in YOLO format → update BD_VEHICLES_YAML path below.
  4. Set env vars and run: python fine_tune_bd_vehicles.py

Environment variables:
  BD_YAML          path to bd_vehicles.yaml   (default ./bd_vehicles.yaml)
  EPOCHS           training epochs             (default 50)
  BATCH            batch size per GPU          (default 16)
  IMG_SZ           image size                  (default 640)
  BASE_MODEL       base checkpoint             (default yolov8m.pt)
  FREEZE           layers to freeze            (default 10)
  LR0              initial learning rate       (default 0.001)
  DEVICE           GPU device id              (default 0)
  OUTPUT_DIR       where to save results       (default ./runs/finetune_bd)
  EXPORT_TRT       'true' to export .engine    (default true)
  HALF_PRECISION   'true' for FP16 TRT export  (default true)

bd_vehicles.yaml template
─────────────────────────
# Place in the same directory as your YOLO-format dataset.
# Directory structure:
#   datasets/bd_vehicles/
#     images/
#       train/   ← training images
#       val/     ← validation images
#     labels/
#       train/   ← YOLO .txt label files
#       val/
#
# Label format (YOLO): <class_id> <cx> <cy> <w> <h>  (all normalised 0-1)
# class_id 0-5 are COCO classes (car, bicycle, motorcycle, person, bus, truck).
# Classes 6-8 are the BD-specific additions:
#
# path: /datasets/bd_vehicles
# train: images/train
# val:   images/val
#
# nc: 9   # total classes (6 COCO + 3 BD)
# names:
#   0: person
#   1: bicycle
#   2: car
#   3: motorcycle
#   4: airplane       # COCO slot — not relevant, keep for index alignment
#   5: bus
#   6: cng            # ← BD fine-tune class
#   7: tempo          # ← BD fine-tune class
#   8: battery_van    # ← BD fine-tune class
#
# NOTE: YOLOv8m COCO weights use class indices 0-79.
# Our fine-tune adds classes at index 6, 7, 8 after mapping:
#   car(2)→0, motorcycle(3)→1, bus(5)→2, truck(7)→3,
#   bicycle(1)→4, person(0)→5, cng→6, tempo→7, battery_van→8.
# Re-index your COCO labels accordingly in the dataset YAML.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(key: str, default: str) -> str:
    return os.getenv(key, default)


def main() -> None:
    try:
        from ultralytics import YOLO
    except ImportError:
        print("ERROR: ultralytics not installed. pip install ultralytics>=8.0", file=sys.stderr)
        sys.exit(1)

    yaml_path   = _env("BD_YAML",        "./bd_vehicles.yaml")
    epochs      = int(_env("EPOCHS",     "50"))
    batch       = int(_env("BATCH",      "16"))
    img_sz      = int(_env("IMG_SZ",     "640"))
    base_model  = _env("BASE_MODEL",     "yolov8m.pt")
    freeze      = int(_env("FREEZE",     "10"))
    lr0         = float(_env("LR0",      "0.001"))
    device      = _env("DEVICE",         "0")
    output_dir  = _env("OUTPUT_DIR",     "./runs/finetune_bd")
    export_trt  = _env("EXPORT_TRT",     "true").lower() == "true"
    half        = _env("HALF_PRECISION", "true").lower() == "true"

    yaml_file = Path(yaml_path)
    if not yaml_file.exists():
        print(f"ERROR: dataset YAML not found at {yaml_path}", file=sys.stderr)
        print("Create bd_vehicles.yaml using the template in this script's docstring.")
        sys.exit(1)

    print(f"[fine-tune] Loading base model: {base_model}")
    model = YOLO(base_model)

    print(
        f"[fine-tune] Starting training — {epochs} epochs, "
        f"freeze={freeze} layers, lr0={lr0}, batch={batch}, img={img_sz}"
    )
    t0 = time.time()

    results = model.train(
        data      = str(yaml_file),
        epochs    = epochs,
        batch     = batch,
        imgsz     = img_sz,
        lr0       = lr0,
        freeze    = freeze,       # freeze first N backbone layers; head trains freely
        device    = device,
        project   = output_dir,
        name      = "bd_vehicles",
        exist_ok  = True,
        patience  = 15,           # early stop if no improvement for 15 epochs
        augment   = True,
        mosaic    = 1.0,
        mixup     = 0.1,
        copy_paste= 0.1,
        flipud    = 0.0,          # vehicles don't appear upside-down
        fliplr    = 0.5,
        degrees   = 5.0,          # small rotation for cameras not perfectly levelled
        translate = 0.1,
        scale     = 0.5,
        shear     = 2.0,
        hsv_h     = 0.015,
        hsv_s     = 0.7,
        hsv_v     = 0.4,
        val       = True,
        save      = True,
        save_period= 10,          # checkpoint every 10 epochs
        verbose   = True,
    )

    elapsed = time.time() - t0
    print(f"[fine-tune] Training complete in {elapsed / 60:.1f} min")

    best_pt = Path(output_dir) / "bd_vehicles" / "weights" / "best.pt"
    if not best_pt.exists():
        # Fallback path used by ultralytics
        best_pt = Path(output_dir) / "bd_vehicles" / "best.pt"

    if not best_pt.exists():
        print(f"[fine-tune] WARNING: best.pt not found at {best_pt}")
        return

    print(f"[fine-tune] Best weights: {best_pt}")

    if export_trt:
        print("[fine-tune] Exporting to TensorRT (.engine) — requires CUDA + TRT ≥8.6")
        try:
            best_model = YOLO(str(best_pt))
            best_model.export(
                format    = "engine",
                device    = device,
                half      = half,          # FP16 for A100 throughput
                imgsz     = img_sz,
                simplify  = True,
                dynamic   = False,         # fixed batch for TRT optimisation
                batch     = 4,             # A100 batch size
            )
            engine_path = best_pt.with_suffix(".engine")
            print(f"[fine-tune] TRT engine saved: {engine_path}")
            print(f"[fine-tune] Set env: YOLO_TRT_ENGINE_PATH={engine_path}")
        except Exception as exc:
            print(f"[fine-tune] TRT export failed: {exc}")
            print("[fine-tune] Exporting to ONNX as fallback...")
            try:
                best_model = YOLO(str(best_pt))
                best_model.export(format="onnx", imgsz=img_sz, half=half, simplify=True)
                onnx_path = best_pt.with_suffix(".onnx")
                print(f"[fine-tune] ONNX saved: {onnx_path}")
                print(f"[fine-tune] Set env: YOLO_ONNX_PATH={onnx_path}")
            except Exception as exc2:
                print(f"[fine-tune] ONNX export failed: {exc2}")

    print("\n[fine-tune] ── Deployment instructions ─────────────────────────────")
    print(f"  1. Copy {best_pt} → /models/traffic_yolov8m_bd.pt")
    if export_trt:
        print(f"  2. Copy {best_pt.with_suffix('.engine')} → /models/traffic_yolov8m_bd.engine")
    print(f"  3. Set YOLO_TRT_ENGINE_PATH=/models/traffic_yolov8m_bd.engine")
    print(f"     Set YOLO_PT_PATH=/models/traffic_yolov8m_bd.pt")
    print(f"     Set USE_GPU=true  BATCH_SIZE=4")
    print(f"  4. Restart traffic-ai container")
    print(f"  5. Confirm model_format=tensorrt in /health response")
    print("─────────────────────────────────────────────────────────────────────")


if __name__ == "__main__":
    main()
