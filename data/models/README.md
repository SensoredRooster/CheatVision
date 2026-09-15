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
here. Equivalent manual steps:

```powershell
pip install -r requirements-train.txt
python tools/export_player_model.py
```

`python tools/setup_check.py` shows `[OK] Player detector` when it is in place.
COCO class `0` (`person`) is the default in `config/settings.json`. A
game-tuned model will beat COCO person boxes for Warzone silhouettes; drop it
here and point `player_detector_model_path` at it.
