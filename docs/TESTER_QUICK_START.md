# CheatVision VOD Tester Quick Start

## One-time setup

1. Get the CheatVision project folder from the project owner and run `setup.bat`.
2. Open PowerShell in that folder. Install OCR support:

   ```powershell
   .\.venv\Scripts\python.exe -m pip install --no-deps -r requirements-vod-ocr.txt
   ```

3. To let the tool train an offline candidate after processing, install training
   packages too:

   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements-train.txt
   ```

   The OCR install intentionally uses `--no-deps` to preserve CheatVision's
   ONNX Runtime/DirectML installation. Run
   `.venv\Scripts\python.exe tools\setup_check.py` after training-package
   installation. If the detector is reported as CPU-only but should use
   DirectML, restore it with
   `.venv\Scripts\python.exe -m pip uninstall -y onnxruntime` and then
   `.venv\Scripts\python.exe -m pip install onnxruntime-directml`. The normal
   app requires `ffmpeg` on PATH.

## Process a video

1. Double-click **`watch_vod_inbox.bat`**. Leave the CheatVision VOD Inbox
   window open. Videos already in the folders when the watcher starts are
   ignored; drop new files after it is running.
2. Drop the video into exactly one folder:
   - `data\vod_inbox\clean\` — verified-clean footage.
   - `data\vod_inbox\cheating\` — footage submitted as confirmed cheating.
3. When the file has finished copying, enter the watched player's exact
   in-game tag in the popup. The tag must be visible in the bottom-left HUD.
4. Wait for the completion popup. Extracted clips are saved under:
   - `data\vod_dataset\clean\`
   - `data\vod_dataset\suspicious\`

The scanner checks the **middle-left kill feed** for the watched player as the
killer and verifies player identity against the **bottom-left HUD**. The
mid-right elimination toast is a fallback. The input video stays in the inbox.
No `.player.txt` file is needed.

## Long VODs and short-form clips

Drop both long recordings and short clips into the same labeled folders. The
tool scans the whole video and exports context around each distinct kill it
detects. Longer footage includes 10 seconds before and 4 seconds after each
kill by default. A short clip is trimmed to the footage it actually contains.

Short clips still need a visible kill-feed row or elimination toast and the
player tag in the HUD. If the kill indicator is no longer on screen, no clip is
created; the original stays in the inbox so it can be reviewed and re-exported.

## Training and safety

After processing creates clips, the tool attempts training only when both
clean and suspicious examples exist and training packages are installed. It
saves a timestamped **offline candidate** under
`data\models\candidates\candidate_...`. If training cannot run, the window
reports why; labeled clips remain available for a later run.

This candidate is not loaded into CheatVision. The live detection pipeline and
main model are unchanged. Keep each input video entirely consistent with its
folder label, review the extracted clips before using them for future work, and
send the resulting dataset to the project owner through the agreed secure
channel.
