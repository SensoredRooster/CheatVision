# Detector weights

This folder is empty in git on purpose: model weights are not committed
(size, and the Ultralytics licence). A fresh clone therefore has **no player
detector** until you fetch one. The app still runs without it and scores aim
motion; the snap / sticky / flick rules need player boxes, so get it.

## Get it (once)

Easiest: `setup.bat` asks and does this for you. By hand, from the repo folder
with the `.venv` active:

```powershell
python tools/setup_check.py --get-model
```

That installs `requirements-train.txt` (torch + ultralytics, about 300 MB) if
needed, downloads the official `yolov8n.pt` weights, and writes `yolov8n.onnx`
here with a **960** input (the app's analysis frame is 960×540, so nothing is
lost; a 640 model sees it at 640×360 and misses distant players). Equivalent
manual steps:

```powershell
pip install -r requirements-train.txt
python tools/export_player_model.py
```

`python tools/setup_check.py` shows `[OK] Player detector` when it is in place,
with its input size and whether it will run on the GPU (install
`onnxruntime-directml` in place of `onnxruntime` for that: ~27 ms a frame end
to end at 960 instead of ~90 ms on a CPU). Add `--imgsz 640` to the export on
a slow CPU with no usable GPU.
COCO class `0` (`person`) is the default in `config/settings.json`. A
game-tuned model will beat COCO person boxes for Warzone silhouettes; drop it
here and point `player_detector_model_path` at it.
