"""Measure what a capture card really delivers, per mode and pixel format.

CheatVision's FEED readout shows the delivered and unique frame rates of the
mode it negotiated. This tool answers the next question -- is the card's
delivery ceiling a property of the mode, or of the pixel format the driver
was asked for? -- by streaming each combination for a few seconds and
counting frames exactly the way the app reads them.

    python tools/probe_capture_rate.py
    python tools/probe_capture_rate.py --mode 2560x1440@144 --mode 1920x1080@240
    python tools/probe_capture_rate.py --device "AVerMedia HD Capture GC573 1" --pixel-format nv12 --seconds 6

The card must be free: close OBS / Streaming Center / CheatVision first (a
busy device is reported, not measured). Nothing is written anywhere. If one
format clearly delivers more new pictures a second than "auto", put it in
config/settings.local.json as "capture_pixel_format" and press RESCAN.

The unique count can never exceed the refresh rate the gaming PC sends over
HDMI: that is set in Windows display settings on the *gaming* PC, for the
display that the capture card presents itself as.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.app import load_settings  # noqa: E402
from src.core.frame_source import (  # noqa: E402
    FFmpegRawVideoCapture,
    FrameSource,
    _run_ffmpeg_query,
    discover_directshow_devices,
    ffmpeg_on_path,
    normalize_pixel_format,
)

DEFAULT_FORMATS = ("auto", "bgr24", "nv12", "yuyv422")
WARMUP_SEC = 1.0
# The driver needs a moment to release the device between opens; rushing it
# makes the next open report "busy" or starve.
SETTLE_SEC = 0.75


def parse_mode(text: str) -> tuple[int, int, int]:
    size, _, rate = text.partition("@")
    width, _, height = size.lower().partition("x")
    try:
        mode = (int(width), int(height), int(round(float(rate or 0))))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"mode must look like 2560x1440@144, got {text!r}") from exc
    if min(mode) <= 0:
        raise argparse.ArgumentTypeError(f"mode must look like 2560x1440@144, got {text!r}")
    return mode


def pick_device(settings: dict, requested: str | None) -> str:
    if requested:
        return requested
    devices = [device for device in discover_directshow_devices() if str(device.get("name", ""))]
    cards = [device for device in devices if str(device.get("kind", "")) == "Capture Card"]
    if cards:
        return str(cards[0]["name"])
    saved = str(settings.get("capture_device_name", "")).strip()
    if saved and str(settings.get("capture_device_kind", "")).lower() != "virtual camera":
        return saved
    names = ", ".join(str(device["name"]) for device in devices) or "none"
    raise SystemExit(f"No capture card found (devices: {names}). Pass --device.")


def measure(device: str, mode: tuple[int, int, int], pixel_format: str | None, seconds: float) -> dict:
    width, height, fps = mode
    result = {
        "mode": mode,
        "format": pixel_format or "auto",
        "delivered": 0.0,
        "unique": 0.0,
        "error": "",
        "overflow": False,
    }
    try:
        capture = FFmpegRawVideoCapture(device, width, height, fps, pixel_format=pixel_format)
    except Exception as exc:  # ffmpeg missing or unusable arguments
        result["error"] = repr(exc)
        return result
    try:
        deadline = time.perf_counter() + WARMUP_SEC
        while time.perf_counter() < deadline and capture.isOpened():
            capture.read()
        if not capture.isOpened():
            result["error"] = capture.get_last_error() or "ffmpeg exited before streaming"
            return result
        start = time.perf_counter()
        delivered = unique = 0
        while time.perf_counter() - start < seconds and capture.isOpened():
            ok, _frame = capture.read()
            if not ok:
                continue
            delivered += 1
            if not capture.last_read_duplicate:
                unique += 1
        elapsed = max(time.perf_counter() - start, 1e-6)
        result["delivered"] = delivered / elapsed
        result["unique"] = unique / elapsed
        result["overflow"] = "too full" in capture.get_last_error()
        if delivered == 0:
            result["error"] = capture.get_last_error() or "no frames (no signal, or device busy)"
        elif capture.device_busy():
            result["error"] = "device busy"
    finally:
        capture.release()
        time.sleep(SETTLE_SEC)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", help="DirectShow device name (default: first capture card found)")
    parser.add_argument(
        "--mode", action="append", type=parse_mode, help="WxH@FPS, repeatable (default: the configured capture mode)"
    )
    parser.add_argument(
        "--pixel-format", action="append", dest="formats", help=f"repeatable (default: {' '.join(DEFAULT_FORMATS)})"
    )
    parser.add_argument("--seconds", type=float, default=5.0, help="measurement window per combination (default 5)")
    args = parser.parse_args()

    if not ffmpeg_on_path():
        print("ffmpeg is not on PATH; install it first.")
        return 2
    settings = load_settings(ROOT)
    device = pick_device(settings, args.device)
    modes = args.mode or [
        (
            int(settings.get("capture_width", 2560) or 2560),
            int(settings.get("capture_height", 1440) or 1440),
            int(settings.get("capture_fps", 144) or 144),
        )
    ]
    formats = [normalize_pixel_format(fmt) for fmt in (args.formats or DEFAULT_FORMATS)]

    listing = _run_ffmpeg_query(
        ["ffmpeg", "-hide_banner", "-list_options", "true", "-f", "dshow", "-i", f"video={device}"]
    )
    lowered = listing.lower()
    if "already in use" in lowered or "resource busy" in lowered:
        print(f"{device} is in use by another program (OBS / Streaming Center / CheatVision). Close it and retry.")
        return 1
    parser_source = FrameSource(
        {**settings, "capture_device_name": device, "capture_device_kind": "Capture Card", "capture_mode": "camera"}
    )

    print(f"Device: {device}")
    print(
        f"Each combination streams {WARMUP_SEC:.0f} s warm-up + {args.seconds:.0f} s measured, "
        "reading frames exactly as the app does.\n"
    )
    print(f"{'mode':>16} {'format':>8} {'advertised':>11} {'delivered':>10} {'unique':>8}  note")
    rows: list[dict] = []
    for mode in modes:
        for fmt in formats:
            ranges = parser_source._parse_device_modes(listing, preferred_format=fmt) if listing.strip() else {}
            low, high = ranges.get((mode[0], mode[1]), (0.0, 0.0))
            advertised = f"{low:.0f}-{high:.0f}" if high > 0 else "no"
            row = measure(device, mode, fmt, args.seconds)
            rows.append(row)
            note = row["error"] or ("OVERFLOW" if row["overflow"] else "")
            label = f"{mode[0]}x{mode[1]}@{mode[2]}"
            print(f"{label:>16} {row['format']:>8} {advertised:>11} {row['delivered']:>10.1f} {row['unique']:>8.1f}  {note}")
            if row["error"] and ("in use" in row["error"].lower() or "busy" in row["error"].lower()):
                print("\nDevice is busy; stopping.")
                return 1

    best = max((row for row in rows if not row["error"]), key=lambda row: row["unique"], default=None)
    if best is None:
        print("\nNothing streamed. Check the HDMI signal and that no other program holds the card.")
        return 1
    mode = best["mode"]
    print(
        f"\nMost new pictures/s: {mode[0]}x{mode[1]}@{mode[2]} {best['format']}: "
        f"{best['unique']:.1f} unique of {best['delivered']:.1f} delivered."
    )
    print("Unique pictures cannot exceed the refresh rate the gaming PC sends over HDMI to the card.")
    if best["format"] != "auto":
        print(f'To use it in the app: add "capture_pixel_format": "{best["format"]}" to config/settings.local.json, then RESCAN.')
    return 0


if __name__ == "__main__":
    sys.exit(main())
