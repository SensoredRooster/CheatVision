<p align="center">
  <img src="assets/brand/cheatvision_mark.png" alt="CheatVision" width="160">
</p>

# CheatVision — How-To Guide

CheatVision is a Windows program that **watches FPS gameplay and flags aim that
looks like a machine did it**: perfectly straight camera pans, high-speed locks
with no hand tremor, instant snaps onto player heads, and "sticky" tracking.

It only looks at pixels. It never touches, reads, or injects into the game.

You can feed it:

- a **capture card** (HDMI from the gaming PC or console),
- another app's **virtual camera** (so you can record with OBS / Streaming Center / Streamlabs at the same time),
- a **saved video file** (VOD review),
- a **browser window** showing a Twitch/Kick/YouTube stream.

Repo: https://github.com/SensoredRooster/CheatVision

---

## 0. What you need

| item | notes |
|---|---|
| Windows 10/11 PC | the app is Windows-only (DirectShow capture) |
| Python 3.11 or newer | https://www.python.org/downloads/ — tick **"Add python.exe to PATH"** during install |
| ffmpeg | https://www.gyan.dev/ffmpeg/builds/ (or `winget install ffmpeg`). Must be on PATH: open a terminal and type `ffmpeg -version` — if it prints a version, you're good |
| a video source | any capture card Windows can see (AVerMedia, Elgato, Magewell, Razer, generic USB HDMI dongles…) — the app lists **your** hardware under the name Windows gives it. Developed and tested on an AVerMedia Live Gamer 4K (GC573). Or a video file. |
| optional: NVIDIA/Intel/AMD GPU | makes baseline recording free (hardware encoder). Works without |

---

## 1. Install (one time)

Open **PowerShell** in the folder you downloaded/cloned the repo into, then:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

`torch` at the bottom of `requirements.txt` is **only** for retraining a
classifier — if that line fails or takes forever, delete it; the app does not
need it to run.

### Optional: player detector (YOLO)

Without it, CheatVision still scores aim motion. With it, flags can be
**confirmed against a player box** (much stronger evidence, fewer false alarms).

```powershell
pip install ultralytics
python tools/export_player_model.py
```

That writes `data/models/yolov8n.onnx`. Done once.

---

## 2. Start the app

```powershell
.\.venv\Scripts\Activate.ps1      # only if you opened a new terminal
python main.py
```

What happens on start (takes ~5 s):

1. It lists your DirectShow video devices and picks the capture card (or your
   last chosen device).
2. It asks the card for its supported modes and **tests the requested mode for
   real** (`[CAPTURE] [CALIBRATE] 2560x1440@144: 70.6 fps -> ok` in the console).
3. The picture appears. The top bar reads e.g.
   `LIVE · 2560×1440 @ 144 · CAP_FFMPEG · HDMI GAME · STANDARD`.

If the canvas says **Waiting for capture device** — no signal is reaching the
card. Check the HDMI cable and that the gaming PC/console is outputting.

---

## 3. The screen, top to bottom

```
┌──────────────────────────────────────────────────────────────────┐
│ IMPORT  RESCAN  TOOLS   LIVE · 2560×1440 @ 144 · … · STANDARD    │  ← control bar
├────────────────┬─────────────────────────────────────────────────┤
│  CHEATVISION   │                                                 │
│ [RECORD CLEAN  │                                                 │
│   BASELINE]    │                                                 │
│ ┌ SOURCE ────┐ │                                                 │
│ │ device·prof▾│ │                                                 │
│ │ MODE FEED  │ │              live video                         │
│ │ PIPE       │ │       (fills to the window edge)                │
│ │ IGNORE BASE│ │                                                 │
│ ├ DETECT ────┤ │                                                 │
│ ├ SIGNAL ────┤ │                                                 │
│ ├ INCIDENTS ─┤ │                                                 │
│ └────────────┘ │                                                 │
└────────────────┴─────────────────────────────────────────────────┘
```

### Control bar

| button | does |
|---|---|
| **IMPORT** | open a video file (`.mp4 .mkv .avi`) for review. Stops live capture. |
| **RESCAN** | re-list devices and reconnect. Use after plugging in a card or closing another capture app. |
| **TOOLS** | hide/show the left panel (video gets wider). Detection keeps running either way. |
| status text | what's connected and how. Turns **amber** with a warning when something is wrong (see §8). |
| RES / FPS boxes | only appear on VOD; pin a playback size/rate if a file is mis-labelled. For live capture use **MODE** in the SOURCE card. |

### SOURCE card

| row | meaning |
|---|---|
| source selector | **device · profile** in one pick, e.g. `HD60 X · HDMI GAME`, `GC573 1 · HDMI GAME` or `OBS VCam · STREAM WIN` — the device part is **your** hardware's own name as Windows reports it, trimmed of vendor boilerplate to fit. The device is *what to read* (capture card, or another app's **Virtual Camera**, see §5); the profile is *which screen regions to ignore* (see §6). Hover it for the full device name. Remembered across restarts. |
| **MODE** | a picker. **AUTO** tests the device's modes, keeps the fastest one that streams cleanly, and shows what it negotiated (`AUTO · 2560×1440 @ 144`). The other entries are **only the modes this device advertised** at the last scan — nothing generic. Pick one to restart capture on it; if the device rejects it, capture falls back to AUTO and the status text says so. Remembered across restarts. |
| **FEED** | what the device is **really delivering**: `71 fps (60 new)` = 71 frames/s handed over, 60 of them new pictures. This is the honest number — see §7. Turns red if starved. |
| **ANALYSIS** | how many new pictures a second the aim analyser scores, and what fraction of the feed that is: `20 Hz · 1 in 3` on a 60 fps feed, `21 Hz · 1 in 7` on 144, `20 Hz · 1 in 12` on 240. The stride follows the feed so every source is judged at the same cadence — see §7. |
| **PIPE** | how frames get in: `CAP_FFMPEG` (card), `VIRTUAL_CAM`, `GDI_BROWSER`, `MSS` (screen), `VOD` |
| IGNORE | how many ignore rectangles the profile is applying |
| BASE | `idle` or `rec` while a baseline is recording |

### DETECT card

| row | meaning |
|---|---|
| YOLO | `ON`/`OFF`. On automatically for VODs; off for live HDMI unless you tick **ANALYZE LIVE** |
| TRACKS | player boxes currently tracked |
| GATE | `live` = analysing. Anything else (`black`, `no_hud`, `letterbox`, `frozen`) = the analyser is deliberately idle (menus, loading screens, no signal). Normal. |
| **ANALYZE LIVE** | run the YOLO player detector on the live feed too (costs ~4 CPU cores; makes flags target-confirmed) |

### SIGNAL card

| row | meaning |
|---|---|
| LIVE AIM / FREEZE | FREEZE = the picture has not changed for 1.5 s (paused, alt-tabbed, no signal) |
| **STR** | straightness of the recent aim path, 0–1. Humans wobble: mostly 0.3–0.9. Turns amber ≥ 0.92 |
| **TREMOR** | hand jitter. Humans while moving fast: never below ~0.5 in testing. Turns red ≤ 0.05 with STR ≥ 0.90 — that is the mechanical signature |
| graph | STR (line) and TREMOR (fill) over the last ~12 s |

### INCIDENTS card

Every flag lands here: time, class, confidence, track id. The count is in the
title. Nothing else in the app shows flags twice.

**Double-click a row to see the proof.** Every flag gets its own folder under
`data/incidents/`, and double-clicking opens it in Explorer with the snapshot
selected:

| file | what it is |
|---|---|
| `snapshot.png` | the flagged frame with the aim path drawn on it (yellow line ending at the red reticle) and the numbers in the corner |
| `clip.mp4` | ~1.5 s before the flag to ~1 s after, at analysis size (960×540) |
| `event.json` | everything the detector measured: class, confidence, velocity, straightness, tremor, target track, the raw path |

The folder is created the instant the flag fires; the clip finishes writing
about a second later (it needs the "after" frames). If a VOD is mounted, the
double-click also jumps the video to that frame.

---

## 4. Review a saved video (the easiest way to start)

1. Click **IMPORT**, pick an `.mp4`.
2. The profile switches to `VOD FILE` and YOLO turns on.
3. Playback controls appear under the video: ⏸/▶ and a scrub bar.
4. Watch INCIDENTS fill in. Double-click any row to jump there.
5. Judge each flag yourself — CheatVision *points at* suspicious motion; it does
   not convict.

Streamlabs / OBS recordings work directly. If a file plays at the wrong speed,
its frame-rate tag is wrong: pin the real rate in the **FPS** box (top right)
or set `playback_fps` in `config/settings.json`.

---

## 5. Record with OBS / Streaming Center **and** run CheatVision at the same time

A capture card only lets **one program** read it. Two programs opening the card
= one of them gets a trickle of frames (CheatVision will show a slideshow and
the amber warning *CAPTURE CARD DELIVERING ONLY N FPS*).

The fix is built in:

1. In OBS (or Streaming Center / Streamlabs), select the card as the source and turn on its
   **Virtual Camera** output. Record/stream as usual.
2. In CheatVision, SOURCE → selector → that app's **… VCam · HDMI GAME** entry (e.g. `OBS VCam · HDMI GAME`, `StreamCenter VCam · HDMI GAME`)
   (or *OBS Virtual Camera*).
3. PIPE shows `VIRTUAL_CAM`, FEED shows `60 fps`. Both apps now run together.

A virtual camera is a **software feed at its host app's rate**. Streaming
Center's lists every size at 30 or 60 only (`ffmpeg -list_options` on the
development machine), so FEED cannot read above 60 on it whatever the HDMI
signal is doing. While the gaming PC sends 60 Hz that costs nothing; to analyse
a 144 Hz signal, read the card itself (§7). The choice is saved; next launch
CheatVision goes straight to it.

If you pick a virtual camera and its host app has the output switched **off**,
CheatVision says so after 4 s instead of showing a frozen picture.

---

## 6. Profiles — telling the analyser what to ignore

The **profile** half of the SOURCE selector chooses which parts of the screen are *not* gameplay:

| profile | pick it when | ignores |
|---|---|---|
| `HDMI GAME` | capture card straight from the game | facecam corner (bottom-right by default), player's own weapon |
| `STREAM WINDOW` | watching a Twitch/Kick/YouTube stream in a browser | top and bottom stream chrome, chat column on the right, facecam |
| `VOD FILE` | a recorded stream with overlays baked in | same as STREAM WINDOW |

Picking a different entry while live restarts capture once (a couple of seconds), even if only the profile changed. Importing a VOD switches the profile to `VOD FILE` automatically.

The **game profile** (`config/settings.json` → `game_profile`: `warzone` or
`generic`) sets where the HUD is (minimap, ammo) so it is masked out, and where
the player's own gun is drawn so YOLO never mistakes it for an enemy. Add a new
game by copying `config/game_profiles/warzone.json` and editing the fractions.

---

## 7. Reading the numbers honestly (144 Hz, 60 fps, and all that)

- **MODE** is what was *requested* and accepted: on AUTO the entry shows what
  calibration negotiated (`AUTO · 2560×1440 @ 144`); a pinned entry is the mode
  you chose. Neither is the HDMI signal's own refresh rate — a capture card
  does not expose that to Windows, it only lists the capture modes it can
  output, and it repeats or skips frames to fit. The picker offers every
  common refresh step inside what the device advertises (24 … 144, 165, 240,
  360) and nothing it does not.
- **FEED** is what *arrives*. Every card has its own delivery ceiling per
  resolution, and FEED shows yours. Example from the development card (GC573)
  at 1440p: the driver hands over ~70 frames/s no matter what you request, and
  with a 60 Hz HDMI signal FEED reads `71 fps (60 new)`. That is not a bug in
  CheatVision; it is the card. Duplicated frames are detected and thrown away
  so the analyser only ever sees new pictures.
- **FEED stuck at 60 on a 144 Hz setup?** Check these in order:
  1. **Source.** A *virtual camera* (Streaming Center, OBS, Streamlabs) is
     capped by its host app — Streaming Center publishes 30/60 only. Pick the
     card's own entry in SOURCE; PIPE must read `CAP_FFMPEG`, not `VIRTUAL_CAM`.
  2. **The gaming PC.** New pictures a second can never exceed the refresh
     rate Windows *on the gaming PC* gives the display the capture card
     presents itself as (Settings → Display → Advanced display → choose the
     capture-card display → refresh rate). If that display **duplicates** the
     main monitor, Windows uses the lowest rate both accept — extend instead,
     or run the game on the card's display. The game's own frame cap counts too.
  3. **The card's driver.** A delivered rate below the mode (~70 of 144 on the
     GC573 in the default format) is a per-format driver ceiling. Close every
     app that holds the card and run `python tools/probe_capture_rate.py`: it
     streams the mode in `auto`, `bgr24`, `nv12` and `yuyv422` for a few
     seconds each and prints delivered and unique fps per format. If one
     clearly beats `auto`, set it as `capture_pixel_format` in
     `config/settings.local.json` and press RESCAN.
- **ANALYSIS** is how often the aim rules run, and it is deliberately *not*
  the feed rate. The rules were tuned at ~20 analysed samples a second (60 new
  pictures, every third one). Whatever FEED reads, the pipeline picks the
  stride that lands nearest that cadence — 1 in 3 at 60, 1 in 7 at 144, 1 in
  12 at 240 — and tells the analyser the exact rate, so its windows are
  measured in seconds and its per-step thresholds are rescaled (a 144 Hz feed
  analysed every frame would otherwise read as slower *and* steadier than a
  60 Hz one, which is what a bot looks like). The stride only moves when the
  cadence drifts more than ~30 % from target, so 59–71 fps jitter never flaps
  it. To score more samples a second set `analysis_rate_hz` (e.g. `48`); the
  thresholds follow, but the tuning evidence is at 20.
- The development card's own maximum at 1440p is 144 (advertised) / ~70
  (delivered in the default format); it advertises 240 at 1080p and 60 at 4K,
  and no 360 mode at any size. 1080p at 360 would also exceed the raw-pipe
  budget (2.2 GB/s in bgr24), so it needs a card that advertises it *and* a
  compact pipe format — not supported yet. Your card's numbers will differ:
  the MODE picker lists what it advertises, FEED shows what it delivers.

---

## 8. Warnings you may see, and what to do

| text | meaning | fix |
|---|---|---|
| `Capture card is in use by another application…` | OBS/Streamlabs/RECentral/a browser tab owns the card | close it and press **RESCAN**, or use the virtual camera (§5) |
| `CAPTURE CARD DELIVERING ONLY N FPS` (amber) | another app grabbed the card mid-session | same as above |
| FEED never above `60 fps` on a 144 Hz setup | the source is a virtual camera, the gaming PC sends the card 60 Hz, or the card's default pixel format caps delivery | the three checks in §7 |
| `NO PIXEL CHANGE DETECTED` / SIGNAL **FREEZE** | picture identical for 1.5 s | pause menu, alt-tab, or no signal. Clears by itself when motion returns |
| `Waiting for capture device` | device opened but sends nothing | check HDMI cable / source power |
| `… is registered but not sending frames` | virtual camera picked but its host app's output is off | turn on Virtual Camera in OBS / Streaming Center, press **RESCAN** |
| `⚠ manual … not supported, auto-calibrated instead` | you pinned a MODE (or VOD RES/FPS) the device can't do | pick **AUTO** in the SOURCE card's MODE picker |
| `No video devices found` in the SOURCE selector | Windows has no DirectShow video device right now | plug the card in / install its driver, press **RESCAN** |
| `ffmpeg not found on PATH` in the SOURCE selector | ffmpeg is missing (§0) | install it, open a new terminal, press **RESCAN** |
| card refuses to open with *nothing* else running | an earlier ffmpeg got killed mid-stream and wedged the driver (older builds did this) | reboot once. Current builds stop ffmpeg gracefully and can't cause it |

The console window (where you ran `python main.py`) prints the same events with
more detail, e.g. `[CAPTURE] [CALIBRATE] …`, `[EXPORT] baseline saved …`.

---

## 9. Recording

Two buttons in the left rail, under the brand. Both write GPU-encoded mp4s
(`h264_nvenc` → `h264_qsv` → `h264_amf`, falling back to `libx264`), stamp the
file with the real unique-picture rate so it plays at true speed, and finalise
in the background the instant you stop (closing the app waits up to 10 s for
that). They are independent — you can run both at once.

### Recording a session

**RECORD SESSION** saves the live feed to
`data/recordings/session_<time>.mp4` — plain evidence of the sitting you're
monitoring, with no clean/suspicious meaning for training. Live only (a VOD
already is a file). Press again to stop. Each flag still gets its own proof
folder under `data/incidents/` regardless (see the INCIDENTS card in §3).

### Recording a clean baseline

**RECORD CLEAN BASELINE** saves the live feed to
`data/clean/baseline_session_<time>.mp4`. Use it to build a library of gameplay
you *know* is legit — that is what the thresholds are tuned against, and what a
classifier would train on. On a VOD the same button reads **MARK VOD AS
CLEAN**. Zero dropped frames at 1440p in testing; ~90 MB of memory.

---

## 10. Where things are saved

| what | where |
|---|---|
| **proof for each flag** (snapshot.png, clip.mp4, event.json) | `data/incidents/<date-time>_<class>_fr<frame>/` |
| flag events (one JSON line each, all sessions) | `logs/session_<time>.jsonl` |
| plain-text app log | `logs/events.log` |
| session recordings (RECORD SESSION) | `data/recordings/` |
| baseline recordings | `data/clean/` |
| your device / MODE / profile choices | `config/settings.local.json` (written by the app; git-ignored, so your picks never ship with the repo) |
| YOLO weights | `data/models/yolov8n.onnx` |

`data/recordings`, `data/clean`, `data/suspicious`, `data/incidents`, `logs` and the weights are
**not** committed to git.

---

## 11. Settings files (`config/settings.json`, `config/settings.local.json`)

`settings.json` is the shipped defaults. Everything the app saves for you — capture device, MODE pin — goes to `settings.local.json` beside it, which is git-ignored and layered on top at startup, so nobody's hardware choices ship to anyone else. You rarely need to touch either. For reference:

| key | meaning |
|---|---|
| `capture_device_name` / `capture_device_kind` | device chosen in the SOURCE dropdown (saved automatically) |
| `capture_mode` | `camera` (devices) or `screen` (a monitor region) |
| `source_profile` | `hdmi_game` / `stream_window` / `vod_file` |
| `game_profile` | `warzone` (default) or `generic` |
| `capture_width` / `capture_height` / `capture_fps` | mode to request; the app verifies it and falls back if the device can't do it |
| `capture_pixel_format` | `auto` (driver's choice, default) or a DirectShow format to request from a capture card (`nv12`, `yuyv422`, `bgr24`); measure first with `tools/probe_capture_rate.py` (§7) |
| `playback_fps` | force a VOD's rate; `0` = trust the file (accepted range 12–480, else 30) |
| `capture_resolution_override` | the MODE pin as `{"width", "height", "fps"}`; absent = AUTO (saved automatically, local file) |
| `player_detector_model_path` | ONNX weights, default `data/models/yolov8n.onnx` |
| `detection_fps` | how often YOLO runs (default 30) |
| `analysis_rate_hz` | analysed aim samples a second to aim for (default 20, the cadence the rules were tuned at); the stride follows the feed's real rate (§7) |
| `analysis_stride` | starting stride (default 3); adapts automatically once the feed rate is known |
| `detection_confidence_threshold` / `detection_nms_threshold` | YOLO thresholds |
| `detection_player_class_ids` | `[0]` = COCO "person" |
| `facecam_roi` | `[]` = default bottom-right box; or `[x0, y0, x1, y1]` as fractions or pixels |
| `stream_chat_ignore` | ignore the right-hand chat column on stream/VOD profiles |
| `screen_monitor_index` / `screen_region` | only for `capture_mode: "screen"` |
| `window_title` | `CheatVision` |

---

## 12. Testing that everything works

```powershell
python -m unittest discover -s tests -v
```

27 tests: scene gate, aim tracker, coordinate scaling, game profiles, and
full-pipeline recall (a human flick must **not** flag; a ruler-straight pan, a
tremor-free lock on a curving target, and a one-frame snap onto a head **must**).

To score the detector on your own footage:

```powershell
python tools/import_dataset.py --input <folder of clips> --labels <labels.csv> --output data --analyze --report report.json
```

`labels.csv` has `filename,label,cheat_type,notes` with `label` = `clean` or
`suspicious` (see `data/labels_template.csv`). The report lists true/false
positives per clip. On the author's legit 1440p144 Warzone highlights the
current rules produce **0 false positives**; recall on real cheats still needs
labelled cheat footage — if you have some, this is the tool to run it through.

The grey `clean_*.mp4` / `suspicious_*.mp4` clips you may find under `data/` are
**synthetic test fixtures** from `tools/make_synthetic_eval.py` (noise texture
+ fake HUD + a dot), not real captures.

---

## 13. Everyday checklist

1. Plug in / power the source. Start OBS / Streaming Center **before** CheatVision if you want to record.
2. `python main.py`.
3. SOURCE → pick your capture card's `… · HDMI GAME` entry, or its `… VCam · …` entry if you're recording.
4. Confirm: top bar `LIVE · …`, GATE `live` during play, FEED shows as many new pictures as your HDMI signal carries (60 on a 60 Hz signal, 144 on 144), ANALYSIS about 20 Hz.
5. Play. Watch INCIDENTS. Double-check anything flagged by eye.
6. Optional: RECORD CLEAN BASELINE during matches you know are legit.
7. Close the window normally (it shuts the capture down cleanly).

---
---

# Technical reference

Everything below is for people changing the code.

## Architecture

Five worker threads plus the UI thread. No stage queues a backlog of live
frames: each keeps **one latest** `FrameContext` and drops the rest.

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

The preview is independent of detection: hiding the canvas changes nothing
about what gets flagged.

- **CaptureWorker** — owns `FrameSource`. Drops frames the driver merely
  repeated; reports feed rate (delivered / new) once a second and flags
  starvation by another client.
- **PlaybackWorker** — VOD. `cv2.CAP_FFMPEG` first. Clamps reported FPS to
  12–480 (else 30). Absolute-schedule pacing (no drift/catch-up bursts).
- **AnalysisWorker** — `AntiCheatPipeline.process_frame`; telemetry throttled.
- **DetectionWorker** — YOLO via ONNX Runtime (4 intra-op threads). Idles when
  disabled.
- **RenderWorker** — builds the display frame off the UI thread; the UI blits.
- **MainWindow** — composition root; never blocks on capture or analysis.

## Capture (`src/core/frame_source.py`)

Capture cards go through **ffmpeg dshow**, not OpenCV's camera API.

1. `ffmpeg -list_options` → parse the modes of the pixel format we will request (bgr24 unless `capture_pixel_format` says otherwise).
2. Ladder: **requested mode first**, then the requested resolution's other
   rates, then sizes nearest to the request (never up to 4K when 1440p was
   asked), sub-720 last. 144.0 and 144.001 are the same mode and probed once.
3. Each candidate streams for ~2.5 s after a 1 s warm-up; pass = no `too full`
   overflow, device not busy, frames arriving steadily (≥ 92 % of target or
   ≥ 50 fps). **The passing probe is kept as the live capture** — closing it and
   re-opening a moment later was a race that starved the real capture.
4. Height < 720 is never AUTO success while an HD mode exists (`[LOW MODE]`).

`FFmpegRawVideoCapture`: `-fps_mode passthrough` (CFR output was padding to the
requested rate with duplicates), raw BGR24 into a **64 MB kernel pipe** read
with unbuffered `readinto` straight into the frame array (the 32 KB pipe +
`BufferedReader` maxed out ~83 fps and made ffmpeg drop frames). Requests a
pixel format for virtual cameras (the one they publish) and for capture cards
only when `capture_pixel_format` is set; `tools/probe_capture_rate.py` measures
delivered/unique fps per format so that setting rests on evidence.

**Shutdown:** ffmpeg gets `q` on stdin and is waited for before anything else;
hard kill is the fallback. Killing a streaming dshow graph wedges the development card's AVerMedia
driver (unkillable zombie owning the card until reboot). All ffmpeg children
sit in a Windows job object with kill-on-close.

**Virtual cameras** (`infer_device_kind` → "Virtual Camera"): open in the
published pixel format, largest landscape mode ≤ requested, fixed rate, no
ladder; never fall back to opening the real card by index.

Freeze latch: 320×180 nearest-neighbour gray, mean absdiff; frozen after the
greater of 90 frames or 1.5 s below 1.5.

Measured on this hardware: 2560×1440 bgr24 at 144 fps (~1.59 GB/s) sustains
with zero drops; PCIe Gen2 ×4 is not the limit; the driver's delivery cap at
1440p is ~70.6 fps and the HDMI content is 60 Hz.

## Scene gate (`src/core/scene_gate.py`)

Raw skip reasons from the analysis frame:

| reason | meaning |
|---|---|
| `live` | HUD energy present, not black, not letterbox, not frozen |
| `black` | mean luma on a 32×18 shrink < 8 |
| `letterbox` | cinematic bars in the content region |
| `no_hud` | no minimap/ammo/stance energy |
| `frozen` | capture freeze latch |

Published state is **Live** or **Held** with a third of a second of hysteresis
each way (20 frames at 60 fps, scaled to the feed rate). While Held: no overlays, `GATE:<reason>` chip, analyser and head trackers
reset, STR/TREMOR = 0. A short raw skip while still Live only idles telemetry.
The canvas always shows the live picture (never a held frame).

## Aim scoring (`src/core/anomaly_detector.py`, `anti_cheat_pipeline.py`)

FPS reticles sit at screen centre; cheats move the **camera**.

1. HUD-mask the analysis frame; zero facecam / chrome / chat pixels.
2. Centre ROI (~22 %). Reticle = geometric centre unless a clearly isolated
   bright mark sits within 12 px (the old refinement chased specks ±20 px and
   injected fake tremor).
3. Phase correlation vs the previous ROI → scene translation; aim delta is the
   negation. Fine estimate from four edge bands, coarse whole-ROI estimate takes
   over when they disagree (bands read a 60 px snap as ~7 px).
4. 0.9 s window (18 samples at the 20 Hz reference cadence, re-sized to the
   real cadence): straightness = displacement / path length; tremor =
   max(residual variance off a line, step variance); **jerk** = variance of the
   second difference (a bot tracking a *curving* target reads as tremor 10–16 on
   a line fit while the hand does nothing).
5. `UNNATURAL_GEOMETRIC_LINE`: velocity ≥ 16 px, straightness ≥ 0.985 **and
   jerk ≤ 4**, held **≥ 0.25 s**. The jerk condition came from two live false
   positives: fast whips with straightness 0.99 but jerk 134 and 568 — a hand
   shaking hard along a straight-ish path is not a scripted line (which
   measures < 1). `MECHANICAL_LOCK_NO_TREMOR`: fast with tremor *or* jerk
   ≤ 0.45 held ≥ 0.12 s. Hold clocks survive brief measurement dropouts.

**Why time-based:** on ~5,600 analysed frames of legit 1440p144 Warzone,
straightness ≥ 0.985 occurs 7–48× per clip (every flick is briefly straight),
single steps reach 47 px, but fast + tremor ≤ 0.45 occurred **zero** times.
Frame-count persistence also behaved differently at 60 vs 144 Hz.

**Cadence:** the rules above are tuned for ~20 analysed samples a second.
`AntiCheatPipeline.set_feed_rate()` (fed by the measured unique-picture rate,
or a VOD's fps) picks the stride that lands nearest `analysis_rate_hz` and only
changes it when the cadence leaves a 0.7–1.4× band, then calls
`CrosshairKinematicsAnalyzer.set_sample_rate()`: velocity-like thresholds
(16 px/step, snap 12, flick 26, sticky camera move 4) scale with the step time,
variance-like ones (tremor 0.45, jerk 4) with its square, step counts (lock
streak 3, sticky hits 6) inversely, the window covers 0.9 s and the scene-gate
hysteresis a third of a second. At the reference cadence every number is
exactly the tuned one.

Replica-aim with YOLO boxes (detections inside the profile's
`detection_ignore_frac` — the player's own weapon — are discarded first):

| event | idea |
|---|---|
| `SNAP_TO_TARGET` | an **instant** step ≥ 12 px (the frame before it ≤ 15 % of the step — humans ramp up, measured 0.6→14→37 px on a live false positive) landing ≤ 34 px from a head, moving toward where the head was, and then **held on that head ≥ 0.20 s** before it is reported (an assist lands and stays; a whipped hand overshoots or drifts) |
| `STICKY_AIM` | reticle ≤ 22 px from a head while the *camera* moves, ≥ 6 hits (a perfect lock keeps the head still on screen) |
| `FLICK_SNAP` | one instant step ≥ 26 px and ≫ mean velocity landing near the nearest head; same ramp test and hold requirement as a snap |

Kinematic flags without a replica event still need a YOLO box under the reticle
when the detector is ready. A verdict must persist 0.05 s (target-corroborated)
or 0.20 s (free-space) and is reported once per streak. Events are JSONL lines
in `logs/`.

## Profiles

`source_profile`: `hdmi_game` (facecam ignore `(0.62, 0.42, 0.99, 0.82)` unless
`facecam_roi`), `stream_window` / `vod_file` (top 0–0.10, bottom 0.88–1.0, chat
0.80–1.0, facecam). `game_profile` JSON (`config/game_profiles/`): HUD mask
fractions, HUD-energy requirement, `detection_ignore_frac` (viewmodel).

## Baseline recorder (`src/core/dataset_exporter.py`)

Frames → 8-deep queue → ffmpeg encoder process (`h264_nvenc` → `h264_qsv` →
`h264_amf` → `libx264`, probed once at startup; OpenCV writers last resort at
~23 fps). Encoder opened before the first frame; 64 MB stdin pipe absorbs
start-up. Stamped with the measured unique-picture rate. `stop()` is instant;
the trailer is written in the background; the app waits ≤ 10 s on exit.

## UI

| piece | job |
|---|---|
| `ControlBar` | IMPORT, RESCAN, TOOLS, elided status text, RES/FPS pins (VOD only) |
| `LeftRail` | brand lockup (mark + wordmark), RECORD CLEAN BASELINE, SOURCE (device·profile selector, MODE/FEED/PIPE, IGNORE/BASE), DETECT, SIGNAL, INCIDENTS (centred title) |
| `VideoCanvas` | paints the latest rendered frame; emits `viewportResized` so the render target always matches the real canvas (a 320×180 placeholder used to be upscaled ~4× until the first window resize) |

## Layout

| path | role |
|---|---|
| `main.py` | entry |
| `src/app.py` | Qt bootstrap, OpenCV thread cap, crash log |
| `src/core/frame_source.py` | dshow/ffmpeg/MSS, AUTO ladder, virtual cameras, freeze, graceful ffmpeg lifecycle + job object |
| `src/core/scene_gate.py` | Live/Held hysteresis |
| `src/core/anti_cheat_pipeline.py` | skip reasons, gate, kinematics, replica-aim, events |
| `src/core/anomaly_detector.py` | phase-correlation aim |
| `src/core/hud_masker.py` | HUD mask, letterbox, HUD energy, viewmodel exclusion |
| `src/core/game_profiles.py` | JSON game profiles |
| `src/core/object_detector.py` | YOLO (ONNX Runtime) + IOU tracker |
| `src/core/dataset_exporter.py` | baseline recorder |
| `src/core/evidence.py` | per-incident proof: annotated snapshot, pre/post clip, event.json (`data/incidents/`) |
| `src/core/train_workflow.py` | optional classifier (needs torch) |
| `src/ui/main_window.py` | composition root, worker wiring, settings persistence |
| `src/ui/workers.py` | capture / playback / analysis / detection / render threads |
| `src/ui/control_bar.py`, `left_rail.py`, `video_canvas.py`, `incident_queue.py`, `playback_controls.py` | widgets |
| `src/ui/theme.py`, `src/ui/branding.py` | palette + stylesheet, brand-asset loader and brand type (see `assets/brand/BRAND.md`) |
| `assets/brand/` | the crosshair mark (`cheatvision_mark.png`, `cheatvision.ico`) and `BRAND.md` |
| `tools/` | `export_player_model.py`, `import_dataset.py`, `make_synthetic_eval.py`, `fetch_anticheatpt.py`, `probe_capture_rate.py` (delivered/unique fps per pixel format) |
| `tests/` | 72 unit tests |
