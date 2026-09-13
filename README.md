# CheatVision

Windows **spectator / VOD review console** for FPS gameplay. It watches a capture
card, webcam, monitor, or mounted clip and flags aim that looks mechanical:
perfectly straight camera pans, high-speed locks with no tremor, snaps onto
player heads, and sticky tracking.

It does **not** inject into a game. It only looks at pixels.

Repo: https://github.com/SensoredRooster/CheatVision

Entry: `python main.py` (`src/app.py` loads `config/settings.json` and opens
`MainWindow`). Window title and left-rail brand are **CheatVision**.

---

## Architecture

Four Qt worker threads plus the UI thread. Nobody queues a backlog of live
frames: each stage keeps **one latest** `FrameContext` and drops the rest.

```
DirectShow / ffmpeg / MSS / VOD file
            │
            ▼
   CaptureWorker or PlaybackWorker     (grab loop, latest-frame only)
            │  frameAvailable(frame_id)
            ├──────────────────────────────► UI display timer (~60 Hz)
            │                                paints pipeline.get_display_frame()
            ├──────────────────────────────► AnalysisWorker
            │                                process_frame() every unique id
            │                                downscale to 960×540 on analysis stride
            └──────────────────────────────► DetectionWorker (YOLO, VOD or opt-in LIVE)
                                             writes boxes back onto the pipeline
```

- **CaptureWorker** — owns `FrameSource`. Tight read loop. `ack_frame_signal()`
  lets the grabber emit the next frame only after the UI has taken the last one.
- **PlaybackWorker** — mounted VOD. Opens with `cv2.CAP_FFMPEG` first. Clamps
  reported FPS to **12–120** (else **30**) because OpenCV often reports `0` or
  `1000` on Streamlabs MP4s. Waits for UI ack before `cap.read()` of the next
  frame, then sleeps the remainder of `1/fps`.
- **AnalysisWorker** — calls `AntiCheatPipeline.process_frame`. Emits telemetry
  and `CheatEvent`s. Downscales only when `should_analyze_frame` (stride).
- **DetectionWorker** — optional YOLO. Off for live HDMI unless **Analyze this
  display**. Always on for VOD.
- **MainWindow** — composition root. Display timer paints; it never blocks on
  analysis.

`FrameContext` carries `frame` (display BGR), optional `analysis_frame`,
`timestamp`, `frame_id`, `source`.

---

## Capture (HDMI / GC573)

`src/core/frame_source.py`

Capture cards go through **ffmpeg dshow**, not OpenCV’s camera API.

1. `ffmpeg -list_options` on the device, parse **bgr24** modes.
2. AUTO ladder tries **1920×1080@60**, then **2560×1440@60**, then other
   height ≥ 720, then sub-720 last.
3. Each candidate is probed for ~2.5s after a 1s warmup. Pass if no
   `too full` overflow and achieved FPS ≥ 92% of target.
4. Height **< 720 is not AUTO success** if any HD mode exists. The SOURCE
   card shows a yellow **low mode** chip; the log line is `[LOW MODE]` not
   `[SUCCESS]`.
5. Preview geometry will not squash an HD source below 720p (the old 1280-wide
   cap turned 2560×1080 into 1280×540).

`FFmpegRawVideoCapture` runs ffmpeg with a raw BGR24 pipe, a reader thread, and
a stderr drain. `release()` closes both pipes and `taskkill /F /T` the process
tree so calibration probes do not leak ffmpeg.exe.

Freeze latch: downscale to max 320px gray, mean absdiff. Frozen only after **90**
consecutive frames below **1.5**. HUD/smoke/facecam motion clears it.

---

## Scene gate

`src/core/scene_gate.py` + `AntiCheatPipeline.process_frame`

Raw skip reasons (from the analysis frame):

| reason | meaning |
|---|---|
| `live` | HUD energy present, not black, not letterbox, not frozen |
| `black` | mean luma on a 32×18 shrink < 8 |
| `letterbox` | cinematic bars in the content frac |
| `no_hud` | no minimap/ammo/stance energy (`hud_masker.hud_energy_present`) |
| `frozen` | capture freeze latch |

Published state is only **Live** or **Held** (N = 20 frames):

- Leave Live after 20 consecutive non-live raw frames (black and no_hud count
  together; the chip text is the **latest** raw reason).
- Re-enter Live after 20 consecutive raw-live frames.
- Five-frame black burst stays Live. Five-frame live flicker does not leave Held.

When **Held**:

- Canvas paints `scene_gate.display_frame(current)` = last Live BGR frame, never
  a black fill. The last Live buffer is copied once on the Live→Held edge so a
  reused capture buffer cannot wipe it.
- Overlay boxes are skipped. A `GATE:<reason>` chip is drawn.
- Analyzer + replica head trackers reset. Telemetry STR/TREMOR = 0.

When raw skip but still published Live (short menu flash): idle telemetry with
that reason, **no** analyzer reset.

---

## Aim scoring

FPS reticles sit at screen center. Cheats snap the **camera**, not a crosshair
sprite.

`CrosshairKinematicsAnalyzer` (`src/core/anomaly_detector.py`):

1. HUD-mask the analysis frame; zero facecam / chrome / chat pixels.
2. Take a center ROI (~22% of the frame).
3. Phase-correlation vs the previous ROI → scene translation `(dx, dy)`.
   Aim delta is the negation of that.
4. Over an 18-sample window: path straightness =
   displacement / path length; tremor = residual variance off a line fit.
5. Flag `MECHANICAL_LOCK_NO_TREMOR` when velocity is high, straightness ≥ 0.985,
   and tremor stays near zero for a lock streak.

Replica-aim (`AntiCheatPipeline._score_replica_aim`), using YOLO boxes when
present:

| event | idea |
|---|---|
| `SNAP_TO_TARGET` | large reticle step that lands on a player head |
| `STICKY_AIM` | reticle stays on a moving head for ≥ 6 hits |
| `FLICK_SNAP` | one step much larger than mean velocity |

Kinematic flags without a replica event still require a YOLO box overlapping the
reticle (plus corroboration margin) when the detector is ready — otherwise the
event is dropped.

Events are `CheatEvent` JSONL lines under `logs/`. Suspicious clips can be
exported when the type is snap / sticky / mechanical lock.

---

## YOLO

`PixelVisionObjectDetector` loads `data/models/yolov8n.onnx` (gitignored).
Default class list is COCO person (`0`).

Boxes live in **analysis** space (960×540). Overlays scale them to display
space. Centers inside ignore rects or boxes > 35% of the frame are dropped.

Live capture does not run YOLO unless the left-rail **ANALYZE LIVE** box is
checked (desktop/task-manager frames false-positive). VOD always can.

---

## UI

| piece | job |
|---|---|
| `ControlBar` | Import VOD, Rescan, SRC profile, view mode, optional RES/FPS, clean baseline |
| `LeftRail` | SOURCE (mode / low mode), SIGNAL (STR + TREMOR sparkline), DETECT (YOLO / tracks / gate), PROFILE |
| `VideoCanvas` | latest QImage, 60 Hz timer, not every capture callback |
| `IncidentQueueTable` | collapsed 28px until first flag; seek-to-frame on VOD |
| View modes | STANDARD, HEATMAP, FLAGGED |

Paint path: `display_frame = pipeline.get_display_frame(ctx.frame)`. If the gate
is not live, skip overlays and stamp `GATE:<reason>`.

---

## Source and game profiles

`source_profile` (`config/settings.json` or SRC combo):

| id | use | ignore rects |
|---|---|---|
| `hdmi_game` | capture card | facecam `(0.62, 0.42, 0.99, 0.82)` unless `facecam_roi` is set |
| `stream_window` | Kick/Twitch/YouTube window | top 0–0.10, bottom 0.88–1.0, chat 0.80–1.0, facecam |
| `vod_file` | mounted file (HDMI VODs often switch here) | same as stream window |

`game_profile` JSON under `config/game_profiles/` (`warzone`, `generic`) sets
HUD mask fractions and whether HUD energy is required for “live gameplay”.

---

## Settings (`config/settings.json`)

| key | meaning |
|---|---|
| `capture_mode` | `camera` or `screen` |
| `source_profile` | `hdmi_game` / `stream_window` / `vod_file` |
| `game_profile` | `warzone` (default) or `generic` |
| `capture_width` / `height` / `fps` | requested mode; AUTO calibrates the card |
| `playback_fps` | VOD; `0` = use file (then clamp 12–120) |
| `player_detector_model_path` | ONNX, default `data/models/yolov8n.onnx` |
| `detection_fps` | YOLO thread target |
| `facecam_roi` | empty = default ignore box |
| `stream_chat_ignore` | right-chat ignore on stream/VOD |
| `window_title` | `CheatVision` |

---

## Dataset and training

Drop clips in `data/clean/` (legit) and `data/suspicious/` (cheat). Both are
gitignored.

```bash
python tools/import_dataset.py --input <clips> --labels <csv> --output data
python src/core/train_workflow.py
```

`train_workflow.py` inventories clips, builds feature tensors (velocity,
straightness, tremor, snap size) via `dataset_loader`, fits
`PixelVisionClassifier`, exports `data/models/cheatvision_detector.onnx`.
Torch is only required for that path (`pip install -r requirements.txt` then
`torch`). It does **not** train on the rule engine’s own flags.

`tools/make_synthetic_eval.py` builds fake clean vs mechanical clips.
`python -m unittest discover -s tests -v` covers SceneGate, aim tracker, and
coordinate-space scaling.

---

## Layout

| path | role |
|---|---|
| `main.py` | entry |
| `src/app.py` | Qt bootstrap + crash log |
| `src/core/frame_source.py` | dshow/ffmpeg/MSS, AUTO ladder, freeze, process-tree kill |
| `src/core/scene_gate.py` | Live/Held hysteresis |
| `src/core/anti_cheat_pipeline.py` | skip reasons, gate, kinematics, replica-aim, events |
| `src/core/anomaly_detector.py` | phase-correlation aim |
| `src/core/hud_masker.py` | fractional HUD mask, letterbox, HUD energy |
| `src/core/game_profiles.py` | JSON HUD layouts |
| `src/core/object_detector.py` | YOLO + IOU tracker |
| `src/core/dataset_exporter.py` | clean baseline + flagged clips |
| `src/core/train_workflow.py` | optional classifier |
| `src/ui/main_window.py` | composition root, paint, GATE chip |
| `src/ui/workers.py` | capture / playback / analysis / detection threads |
| `config/settings.json` | defaults |
| `config/game_profiles/` | `warzone.json`, `generic.json` |
| `tests/` | unit tests |
