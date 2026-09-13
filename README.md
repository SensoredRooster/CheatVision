# CheatVision

Windows **review console** for FPS gameplay. It watches a capture card, webcam,
monitor, or mounted VOD and flags aim that looks mechanical: perfectly straight
camera pans and high-speed locks with no tremor.

This is a **spectator / VOD tool**. It does not inject into a game.

The desktop UI is **PySide6**. Entry point is `python main.py`. Window title and
left-rail brand are **CheatVision**.

## What it does

- Live capture from DirectShow devices (webcam vs capture card, auto-labeled)
- ffmpeg(dshow) calibration so HDMI cards do not freeze on an unsupported mode
- AUTO capture prefers **1920×1080@60**, then **2560×1440@60**. Height below 720 is **low mode**, not a successful AUTO result
- ffmpeg capture processes are killed as a tree on stop so probes do not leak
- **Latest-frame LIVE path** — slow analysis/UI/baseline drops frames; it does not queue a backlog
- VOD playback opens with **CAP_FFMPEG**, clamps garbage FPS (0/1000) to 30, and waits for a UI ack before the next frame
- Status bar shows the **negotiated** capture `width×height @ fps`
- Source profiles (`SRC`): HDMI game, stream window, VOD file
- Scene gate: 20-frame Live/Held hysteresis. Held keeps the last Live frame and a `GATE:<reason>` chip
- Ignore rects for facecam / stream chrome / chat
- Aim scoring via **phase correlation** under the reticle
- Optional YOLO player boxes (off in LIVE unless you opt in)
- View modes: STANDARD, HEATMAP, FLAGGED
- Local JSONL event logs, clean-baseline recording, and flagged-clip export

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

ffmpeg must be on `PATH` for capture-card input.

```bash
python -m unittest discover -s tests -v
```

## Review console

Top bar: IMPORT, RESCAN, HDMI, RECORD CLEAN BASELINE.
Left rail brand is **CHEATVISION**.

HDMI gameplay: `hdmi_game`. Browser stream: `stream_window`.

## Layout

- `main.py` — entry
- `src/app.py` — Qt bootstrap
- `src/core/` — capture, scene gate, aim scoring, export
- `src/ui/` — review console
- `config/settings.json` — defaults (`window_title`: CheatVision)
- `tests/` — unit tests
