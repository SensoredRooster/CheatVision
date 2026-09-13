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

Five Qt worker threads plus the UI thread. Nobody queues a backlog of live
frames: each stage keeps **one latest** `FrameContext` and drops the rest.

```
DirectShow / ffmpeg / MSS / VOD file
            │
            ▼
   CaptureWorker or PlaybackWorker     (grab loop, publishes latest frame; wakes waiters)
            │  wait_for_frame(last_id)
            ├──────────────────────────────► RenderWorker (≤240 Hz, latest frame only)
            │                                overlays + fit-to-canvas resize, then a
            │                                single-slot mailbox → UI paints it
            ├──────────────────────────────► AnalysisWorker
            │                                process_frame() every unique id on a
            │                                960×540 downscale; telemetry ≤20 Hz
            └──────────────────────────────► DetectionWorker (YOLO, VOD or opt-in LIVE)
                                             writes boxes back onto the pipeline
```

- **CaptureWorker** — owns `FrameSource`. Tight read loop; frames the driver
  merely repeated are dropped here so nothing downstream works on duplicates.
  Reports the measured feed rate (delivered / new pictures) once a second and
  warns when the card is being starved by another client.
- **PlaybackWorker** — mounted VOD. Opens with `cv2.CAP_FFMPEG` first. Clamps
  reported FPS to **12–480** (else **30**) because OpenCV often reports `0` or
  `1000` on Streamlabs MP4s. Paces frames on an absolute schedule so timer
  granularity cannot accumulate into drift and catch-up bursts.
- **AnalysisWorker** — calls `AntiCheatPipeline.process_frame`. Emits telemetry
  and `CheatEvent`s.
- **DetectionWorker** — optional YOLO (ONNX Runtime capped at 4 intra-op
  threads). Off for live HDMI unless **ANALYZE LIVE**. Always on for VOD. Idles
  when disabled instead of waking per frame.
- **RenderWorker** — builds the display frame off the UI thread; the UI thread
  only blits.
- **MainWindow** — composition root; never blocks on capture or analysis.

`FrameContext` carries `frame` (display BGR), optional `analysis_frame`,
`timestamp`, `frame_id`, `source`.

---

## Capture (HDMI / GC573)

`src/core/frame_source.py`

Capture cards go through **ffmpeg dshow**, not OpenCV’s camera API.

1. `ffmpeg -list_options` on the device, parse **bgr24** modes.
2. AUTO ladder tries the **requested mode first** (default 2560×1440@144), then
   the requested resolution at its other advertised rates, then other
   height ≥ 720, then sub-720 last.
3. Each candidate is probed for ~2.5s after a 1s warmup. Pass if no
   `too full` overflow, the device is not busy, and frames arrive steadily
   (≥ 92% of target or ≥ 50 fps). **The passing probe is kept as the live
   capture** — closing it and re-opening the same mode a moment later was a
   race that turned the real capture into a starved second client.
4. Height **< 720 is not AUTO success** if any HD mode exists. The SOURCE
   card shows a yellow **low mode** chip; the log line is `[LOW MODE]` not
   `[SUCCESS]`.
5. Preview geometry will not squash an HD source below 720p (the old 1280-wide
   cap turned 2560×1080 into 1280×540).

`FFmpegRawVideoCapture` runs ffmpeg with `-fps_mode passthrough` into a raw
BGR24 pipe (64 MB kernel buffer, unbuffered `readinto` straight into the frame
array — the default 32 KB pipe plus Python's `BufferedReader` could not keep up
with 1440p144 and made ffmpeg drop frames). Passthrough matters: constant-frame-
rate output pads the stream with repeated frames to reach the requested rate;
the GC573 driver itself yields ~70 frames/s at 2560×1440 whatever is requested,
so the old output was half duplicates. Frames the driver repeats are flagged and
skipped by `CaptureWorker`; the left-rail **FEED** row shows `delivered (new)` fps.

`release()` sends ffmpeg `q` on stdin and waits for it to exit before anything
else; a hard kill is only the fallback. Killing ffmpeg while the graph streams
can hang the AVerMedia driver's close path: the process becomes an unkillable
zombie that still owns the device and every later open fails with *"device
already in use"* until a reboot. All ffmpeg children also sit in a Windows job
object with *kill-on-close*, so a crash of the app cannot leave one behind.

**One client per card.** If OBS / Streamlabs / RECentral / a browser tab has the
card open, opening fails with a clear message, or frames arrive as a trickle —
the status text in the top bar then warns *CAPTURE CARD DELIVERING ONLY N FPS*.

Freeze latch: 320×180 nearest-neighbour gray sample, mean absdiff. Frozen only
after the greater of **90 frames** or **1.5 s** below **1.5**. HUD/smoke/facecam
motion clears it.

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
2. Take a center ROI (~22% of the frame). The reticle is the geometric centre
   unless a clearly isolated bright mark sits within 12 px (the old refinement
   chased random bright specks ±20 px per frame and injected fake tremor).
3. Phase-correlation vs the previous ROI → scene translation `(dx, dy)`; aim
   delta is the negation. Fine estimate from four edge bands (sub-pixel), plus
   a coarse whole-ROI estimate that takes over when they disagree — the bands
   cannot see a 60 px snap and used to report it as ~7 px.
4. Over an 18-sample window: straightness = displacement / path length;
   tremor = max(residual variance off a line, step variance); **jerk** =
   variance of the second difference of the path (hand tremor while tracking a
   *curving* target — a line fit reads a smooth curve as tremor 10–16 while
   the hand is doing nothing).
5. `UNNATURAL_GEOMETRIC_LINE`: velocity ≥ 16 px and straightness ≥ 0.985 held
   **continuously for ≥ 0.25 s**. `MECHANICAL_LOCK_NO_TREMOR`: fast, with
   tremor *or* jerk ≤ 0.45, for a lock streak held ≥ 0.12 s. Brief
   measurement dropouts (flat texture) do not reset the hold clocks.

**Why time-based.** Measured on 2560×1440@144 legit Warzone highlights
(~5,600 analysed frames): straightness ≥ 0.985 occurs on 7–48 frames per clip
(every flick is briefly perfectly straight), single steps reach 47 px at
960×540, but tremor ≤ 0.45 while moving fast occurred **zero** times. So a
single-window snapshot or a raw step size is not evidence; a *sustained*
tremor-free or perfectly straight run is. Frame-count persistence also made
60 Hz and 144 Hz sources behave differently; everything is now in seconds.

Replica-aim (`AntiCheatPipeline._score_replica_aim`), using YOLO boxes when
present. Detections whose centre falls in the game profile's
`detection_ignore_frac` (the player's own weapon/hands, bottom-centre) are
discarded first — the largest "person" YOLO finds in legit footage is the
viewmodel.

| event | idea |
|---|---|
| `SNAP_TO_TARGET` | step ≥ 12 px that lands ≤ 34 px from a player head, moving toward where the head was; the verdict is latched for 0.35 s while the aim stays on that head so a one-frame snap survives the persistence gate |
| `STICKY_AIM` | reticle stays ≤ 22 px from a head while the *camera* is moving, for ≥ 6 hits (a perfect lock keeps the head still on screen — camera motion is the evidence) |
| `FLICK_SNAP` | one step much larger than mean velocity, landing near the nearest head |

Kinematic flags without a replica event still require a YOLO box overlapping the
reticle (plus corroboration margin) when the detector is ready — otherwise the
event is dropped. A verdict must then persist 0.05 s (target-corroborated) or
0.20 s (free-space) and is reported **once per streak**.

Validation: `tests/test_aim_tracker.py` covers a jittery human flick (no flag)
and bot line / curve-lock / snap-to-head patterns (must flag). The grey
`clean_*.mp4` / `suspicious_*.mp4` clips under `data/` are the synthetic
fixtures from `tools/make_synthetic_eval.py`, not captures — they smoke-test
the math and are far easier than real footage. Precision is measured with
`tools/import_dataset.py --analyze` on real legit VODs; recall against real
cheats still needs labelled cheat footage.

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

The window is three pieces: a 44 px control bar, the tool rail (toggle with
**TOOLS**), and the video canvas, which runs to the right and bottom edges. The
VOD scrubber appears under the canvas only while a file is mounted.

| piece | job |
|---|---|
| `ControlBar` | Import VOD, Rescan, TOOLS, SRC profile, live status text (mode · profile · warnings, elided with tooltip), optional RES/FPS, clean baseline |
| `LeftRail` | SOURCE (mode / feed rate / low mode), SIGNAL (STR + TREMOR sparkline), DETECT (YOLO / tracks / gate), PROFILE, INCIDENTS (flag table with count; double-click seeks a VOD) |
| `VideoCanvas` | paints the latest rendered frame; `RenderWorker` builds it off the UI thread and hands it over through a single-slot mailbox (no backlog) |
| View modes | STANDARD, HEATMAP, FLAGGED |

Paint path: `RenderWorker` always renders the **live** frame (never the gate's
held frame, which used to freeze the picture and snap forward). If the gate is
not live, skip overlays and stamp `GATE:<reason>`.

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

**RECORD CLEAN BASELINE** writes `data/clean/baseline_session_<ts>.mp4` from
the live feed. Frames go through a small queue into an ffmpeg encoder process —
`h264_nvenc` → `h264_qsv` → `h264_amf` → `libx264`, whichever works on the
machine (probed once at startup) — with OpenCV's writers only as a last resort
(they manage ~23 fps at 1440p, which used to drop most frames and let the queue
balloon to 660 MB). Only new pictures are recorded and the file is stamped with
the measured unique-picture rate, so it plays back at real speed. Stopping is
instant for the UI; the mp4 trailer is written in the background (the app waits
up to 10 s for it on exit). The log line `[EXPORT] baseline saved ...` reports
frames written and any drops.

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
| `src/core/frame_source.py` | dshow/ffmpeg/MSS, AUTO ladder, freeze, graceful ffmpeg lifecycle + job object |
| `src/core/scene_gate.py` | Live/Held hysteresis |
| `src/core/anti_cheat_pipeline.py` | skip reasons, gate, kinematics, replica-aim, events |
| `src/core/anomaly_detector.py` | phase-correlation aim |
| `src/core/hud_masker.py` | fractional HUD mask, letterbox, HUD energy |
| `src/core/game_profiles.py` | JSON HUD layouts |
| `src/core/object_detector.py` | YOLO + IOU tracker |
| `src/core/dataset_exporter.py` | clean baseline (ffmpeg/NVENC pipe) + flagged clips |
| `src/core/train_workflow.py` | optional classifier |
| `src/ui/main_window.py` | composition root, worker wiring |
| `src/ui/control_bar.py` / `left_rail.py` / `video_canvas.py` / `incident_queue.py` / `playback_controls.py` / `theme.py` | widgets |
| `src/ui/workers.py` | capture / playback / analysis / detection / render threads |
| `config/settings.json` | defaults |
| `config/game_profiles/` | `warzone.json`, `generic.json` |
| `tests/` | unit tests |
