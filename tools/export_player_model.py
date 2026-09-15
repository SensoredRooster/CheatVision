#!/usr/bin/env python3
"""Export a local YOLOv8/v11 ONNX player detector for CheatVision.

Weights stay gitignored under data/models/.

The default input is 960: the app hands the detector a 960x540 frame, so a
960 model sees it at full analysis resolution, while a 640 model shrinks it
to 640x360 and loses players under ~20 px (measured on real footage: the 960
model boxed a mid-distance operator in most frames the 640 one missed). Cost
on the CPU is ~2.2x per pass; on a GPU (onnxruntime-directml) both are cheap.

Usage:
    python tools/export_player_model.py
    python tools/export_player_model.py --imgsz 640          # slow CPU, no GPU
    python tools/export_player_model.py --model yolov8n.pt --out data/models/yolov8n.onnx
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser(description="Export YOLO ONNX for CheatVision")
    ap.add_argument("--model", default="yolov8n.pt", help="Ultralytics model name or path")
    ap.add_argument("--out", default="data/models/yolov8n.onnx", help="Destination ONNX path")
    ap.add_argument("--imgsz", type=int, default=960, help="model input size (default 960; 640 for a slow CPU)")
    args = ap.parse_args()

    try:
        from ultralytics import YOLO
    except ImportError:
        print("[ERR] ultralytics not installed. pip install ultralytics")
        return 1

    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"Loading {args.model}...")
    model = YOLO(args.model)
    exported = Path(model.export(format="onnx", imgsz=args.imgsz, simplify=True))
    if not exported.is_file():
        print(f"[ERR] export did not produce a file: {exported}")
        return 1

    if exported.resolve() != out.resolve():
        shutil.move(str(exported), str(out))

    print(f"Wrote {out} ({out.stat().st_size} bytes)")
    print("CheatVision settings default: player_detector_model_path = data/models/yolov8n.onnx")
    return 0


if __name__ == "__main__":
    sys.exit(main())
