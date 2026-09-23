# CheatVision Support & Diagnostics

CheatVision includes a local-first support system for tester and developer diagnostics.

## Included functions

- Rotating JSONL support logs under `%LOCALAPPDATA%\CheatVision\logs`
- One-second session heartbeat and session IDs
- Uncaught Python/thread exception capture
- Startup crash logging
- Runtime health snapshot with Python, Windows, OpenCV, NumPy, PySide6, ONNX Runtime, disk, and NVIDIA GPU information when available
- Main-window **SUPPORT** button
- Native **Support & Diagnostics** dialog
- **Create Support Bundle**
- **Send Diagnostics to Developer** with explicit confirmation
- **Open Logs**
- **Report Issue**
- **Repository**
- Secret/Bearer/OAuth-query redaction
- Dedicated Cloudflare Worker and dedicated R2 bucket
- Per-IP upload rate limiting and ZIP validation
- Windows CI for compilation/tests

## Privacy

Nothing uploads automatically.

Support bundles intentionally exclude gameplay video, captured frames, evidence clips, gameplay telemetry rolls, and model files. Settings snapshots are sanitized and local path/directory values are omitted.

## Cloudflare isolation

- Worker: `cheatvision-support`
- R2 bucket: `cheatvision-support-logs`

This storage is separate from SubScript, Universal AI Studio, RCM Tool, and SonicScout2.0.

The GitHub deployment workflow uses the repository secret `CLOUDFLARE_API_TOKEN`.
## Tester Share

CheatVision's tester file portal is separate from support diagnostics:

- Portal: `https://cheatvision-share.sensoredrooster-com.workers.dev`
- Private R2 bucket: `cheatvision-share`
- Open from **SUPPORT → TESTER SHARE**

Folders are `Releases`, `Tester Uploads`, `Screenshots`, `Bug Reports`, `Logs`, and `Archived`. Tester access can browse/download and upload to tester-facing folders; admin access also manages releases, Latest, deletes, and archives.

