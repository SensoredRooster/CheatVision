#!/usr/bin/env python3
"""Clip Warzone kill-feed events from labeled VODs and train an offline candidate."""

from __future__ import annotations

import argparse
import difflib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

VIDEO_EXTENSIONS = {".mp4", ".avi", ".mkv", ".mov"}
DEFAULT_KILL_FEED_ROI = (0.0, 0.43, 0.24, 0.60)
DEFAULT_ELIMINATION_ROI = (0.55, 0.42, 0.82, 0.57)
DEFAULT_PLAYER_HUD_ROI = (0.0, 0.89, 0.20, 0.98)


@dataclass(frozen=True)
class OcrText:
    x: float
    y: float
    height: float
    text: str
    confidence: float


@dataclass(frozen=True)
class KillEvent:
    timestamp: float
    row_text: str


def normalize_name(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _ocr_texts(result: Any, min_confidence: float) -> list[OcrText]:
    if not result:
        return []
    if isinstance(result, tuple):
        result = result[0]

    detections: list[OcrText] = []
    for item in result or []:
        try:
            box, text, confidence = item
            confidence_value = float(confidence)
            points = [(float(point[0]), float(point[1])) for point in box]
            if not points:
                continue
            y_values = [point[1] for point in points]
            detections.append(
                OcrText(
                    x=min(point[0] for point in points),
                    y=(min(y_values) + max(y_values)) / 2,
                    height=max(y_values) - min(y_values),
                    text=str(text).strip(),
                    confidence=confidence_value,
                )
            )
        except (TypeError, ValueError, IndexError):
            continue

    return [item for item in detections if item.text and item.confidence >= min_confidence]


def _rows(items: Iterable[OcrText]) -> list[list[OcrText]]:
    ordered = sorted(items, key=lambda item: (item.y, item.x))
    rows: list[list[OcrText]] = []
    for item in ordered:
        destination = next(
            (
                row
                for row in reversed(rows)
                if abs(item.y - sum(entry.y for entry in row) / len(row))
                <= max(8.0, item.height, max(entry.height for entry in row))
            ),
            None,
        )
        if destination is None:
            rows.append([item])
        else:
            destination.append(item)
    return [sorted(row, key=lambda item: item.x) for row in rows]


def _name_matches(text: str, player_tag: str, threshold: float) -> bool:
    normalized_text = normalize_name(text)
    normalized_tag = normalize_name(player_tag)
    if not normalized_tag or len(normalized_tag) < 3 or len(normalized_text) < len(normalized_tag) + 3:
        return False

    # Warzone kill-feed rows read killer first, followed by the victim. Match
    # only at the beginning so the tagged player as a victim is not clipped.
    return _tag_span(normalized_text, normalized_tag, threshold) is not None


def _tag_span(text: str, tag: str, threshold: float) -> tuple[int, int] | None:
    for start in range(min(5, len(text))):
        for offset in (-1, 0, 1):
            width = len(tag) + offset
            if width < 1 or start + width > len(text):
                continue
            candidate = text[start : start + width]
            if difflib.SequenceMatcher(None, tag, candidate).ratio() >= threshold:
                return start, width
    return None


def _kill_row_key(text: str, player_tag: str, threshold: float) -> str:
    normalized_text = normalize_name(text)
    normalized_tag = normalize_name(player_tag)
    span = _tag_span(normalized_text, normalized_tag, threshold)
    if span is None:
        return normalized_text
    start, width = span
    return normalized_text[start + width :]


def player_tag_is_visible(items: Iterable[OcrText], player_tag: str, threshold: float = 0.88) -> bool:
    normalized_tag = normalize_name(player_tag)
    if len(normalized_tag) < 4:
        return False

    for item in items:
        normalized_text = normalize_name(item.text)
        if _tag_span(normalized_text, normalized_tag, threshold) is not None:
            return True
    return False


def find_player_kill_rows(
    items: Iterable[OcrText], player_tag: str, min_confidence: float = 0.60, match_threshold: float = 0.90
) -> list[str]:
    matches: list[str] = []
    for row in _rows(item for item in items if item.confidence >= min_confidence):
        row_text = " ".join(item.text for item in row)
        if _name_matches(row_text, player_tag, match_threshold):
            matches.append(row_text)
    return matches


def find_elimination_toasts(items: Iterable[OcrText]) -> list[str]:
    matches: list[str] = []
    for row in _rows(items):
        row_text = " ".join(item.text for item in row)
        normalized = normalize_name(row_text)
        words = [normalize_name(word) for word in row_text.split()]
        is_elimination = any(
            word.startswith("eliminat") or difflib.SequenceMatcher(None, word, "elimination").ratio() >= 0.82
            for word in words
        )
        if is_elimination or "killconfirmed" in normalized:
            matches.append(row_text)
    return matches


def parse_roi(value: str) -> tuple[float, float, float, float]:
    try:
        parts = tuple(float(part.strip()) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("ROI must be x0,y0,x1,y1 in normalized 0..1 coordinates") from exc
    if len(parts) != 4 or not (0 <= parts[0] < parts[2] <= 1 and 0 <= parts[1] < parts[3] <= 1):
        raise argparse.ArgumentTypeError("ROI must satisfy 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1")
    return (parts[0], parts[1], parts[2], parts[3])


def scan_vod(
    vod_path: Path,
    player_tag: str,
    ocr: Any,
    sample_seconds: float,
    roi: tuple[float, float, float, float],
    min_confidence: float,
    match_threshold: float,
    elimination_roi: tuple[float, float, float, float] = DEFAULT_ELIMINATION_ROI,
    player_hud_roi: tuple[float, float, float, float] = DEFAULT_PLAYER_HUD_ROI,
) -> tuple[float, list[KillEvent]]:
    capture = cv2.VideoCapture(str(vod_path))
    if not capture.isOpened():
        raise RuntimeError(f"Could not open VOD: {vod_path}")

    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if fps <= 0 or frame_count <= 0:
        capture.release()
        raise RuntimeError(f"VOD has no usable frame-rate or duration metadata: {vod_path}")

    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    def read_roi(target_roi: tuple[float, float, float, float]) -> list[OcrText]:
        x0, y0, x1, y1 = target_roi
        left, top = int(width * x0), int(height * y0)
        right = max(left + 1, int(width * x1))
        bottom = max(top + 1, int(height * y1))
        result, _ = ocr(frame[top:bottom, left:right])
        return _ocr_texts(result, min_confidence)

    events: list[KillEvent] = []
    last_seen_kill_rows: list[tuple[str, float]] = []
    last_feed_seen_at: float | None = None
    last_fallback_timestamp: float | None = None
    player_verified = False
    step = max(1, int(round(fps * sample_seconds)))

    try:
        for frame_number in range(frame_count):
            ok, frame = capture.read()
            if not ok or frame is None:
                continue
            if frame_number % step:
                continue
            timestamp = frame_number / fps
            hud_detections = read_roi(player_hud_roi)
            player_verified = player_verified or player_tag_is_visible(
                hud_detections, player_tag, threshold=match_threshold
            )
            if not player_verified:
                continue

            kill_feed_detections = read_roi(roi)
            kill_rows = find_player_kill_rows(
                kill_feed_detections,
                player_tag,
                min_confidence=min_confidence,
                match_threshold=match_threshold,
            )
            new_kill_rows: list[str] = []
            for row_text in kill_rows:
                key = _kill_row_key(row_text, player_tag, match_threshold)
                repeated_row = any(
                    timestamp - seen <= 8.0
                    and difflib.SequenceMatcher(None, key, previous_key).ratio() >= 0.72
                    for previous_key, seen in last_seen_kill_rows
                )
                if not repeated_row:
                    new_kill_rows.append(row_text)
                last_seen_kill_rows.append((key, timestamp))
            last_seen_kill_rows = [
                (key, seen) for key, seen in last_seen_kill_rows if timestamp - seen <= 8.0
            ]

            if kill_rows:
                last_feed_seen_at = timestamp
            if new_kill_rows:
                events.append(KillEvent(timestamp=timestamp, row_text=" | ".join(new_kill_rows)))
                continue

            toast_detections = read_roi(elimination_roi)
            toast_texts = find_elimination_toasts(toast_detections)
            if (
                toast_texts
                and (last_feed_seen_at is None or timestamp - last_feed_seen_at > 3.0)
                and (last_fallback_timestamp is None or timestamp - last_fallback_timestamp > 3.0)
            ):
                events.append(KillEvent(timestamp=timestamp, row_text=" | ".join(toast_texts)))
                last_fallback_timestamp = timestamp
    finally:
        capture.release()

    if not player_verified:
        raise RuntimeError(
            f"Could not verify gamer tag {player_tag!r} in the bottom-left player HUD of {vod_path.name}; "
            "check the sidecar tag or --player-hud-roi."
        )
    return fps, events


def write_clip(
    vod_path: Path,
    destination: Path,
    start_seconds: float,
    end_seconds: float,
    fps: float,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg was not found on PATH; run tools\\setup_check.py and install its suggested ffmpeg package")
    duration = end_seconds - start_seconds
    if start_seconds < 0 or duration <= 0:
        raise ValueError("Clip start must be non-negative and end must be after start")
    if fps <= 0:
        raise ValueError("Source video frame rate must be positive")

    destination.parent.mkdir(parents=True, exist_ok=True)
    output_fps = min(60.0, fps)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-ss",
        f"{start_seconds:.3f}",
        "-i",
        str(vod_path),
        "-t",
        f"{duration:.3f}",
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "27",
        "-vf",
        f"scale=960:540:force_original_aspect_ratio=decrease:force_divisible_by=2,fps={output_fps:g}",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(destination),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=max(120.0, duration * 20.0),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg timed out while creating clip: {destination}") from exc
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"Could not start ffmpeg to create clip {destination}: {exc}") from exc
    if result.returncode != 0:
        destination.unlink(missing_ok=True)
        detail = result.stderr.strip() or f"ffmpeg exited with status {result.returncode}"
        raise RuntimeError(f"Could not encode clip {destination}: {detail}")

    if not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError(f"Clip writer produced no usable output: {destination}")
    verification = cv2.VideoCapture(str(destination))
    try:
        if not verification.isOpened() or int(verification.get(cv2.CAP_PROP_FRAME_COUNT)) < 1:
            destination.unlink(missing_ok=True)
            raise RuntimeError(f"ffmpeg output is not a readable video: {destination}")
    finally:
        verification.release()


def _find_vods(inbox: Path) -> list[tuple[str, Path]]:
    vods: list[tuple[str, Path]] = []
    for folder_name, label in (("clean", "clean"), ("cheating", "suspicious")):
        folder = inbox / folder_name
        if not folder.is_dir():
            continue
        vods.extend(
            (label, path)
            for path in sorted(folder.rglob("*"))
            if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
        )
    return vods


def _output_name(vod_path: Path, event: KillEvent) -> str:
    return f"{vod_path.stem}_t{int(event.timestamp * 1000):09d}ms.mp4"


def create_ocr_engine() -> Any:
    try:
        from importlib import import_module

        rapidocr_module = import_module("rapidocr_onnxruntime")
        rapid_ocr = rapidocr_module.RapidOCR
    except (ImportError, AttributeError):
        print(
            "[ERROR] OCR is not installed. Run: "
            ".venv\\Scripts\\python.exe -m pip install --no-deps -r requirements-vod-ocr.txt"
        )
        raise RuntimeError("OCR is not installed; see the install command above.") from None
    return rapid_ocr()


def process_vod(
    vod_path: Path,
    label: str,
    player_tag: str,
    args: argparse.Namespace,
    ocr: Any | None = None,
) -> list[Path]:
    if label not in {"clean", "suspicious"}:
        raise ValueError(f"Unsupported dataset label: {label}")
    if not player_tag.strip():
        raise ValueError("The gamer tag cannot be empty.")

    engine = ocr if ocr is not None else create_ocr_engine()
    print(f"[SCAN] {vod_path.name} | {label} | player={player_tag}")
    fps, events = scan_vod(
        vod_path,
        player_tag,
        engine,
        args.sample_seconds,
        args.roi,
        args.min_confidence,
        args.match_threshold,
        args.elimination_roi,
        args.player_hud_roi,
    )
    print(f"[SCAN] Found {len(events)} player kill moments.")
    if not events:
        print(f"[WARN] No kill moments found for {vod_path.name}.")
        return []

    clips: list[Path] = []
    for index, event in enumerate(events, start=1):
        start = max(0.0, event.timestamp - args.before)
        end = event.timestamp + args.after
        destination = args.dataset / label / _output_name(vod_path, event)
        write_clip(vod_path, destination, start, end, fps)
        clips.append(destination)
        print(f"  [{index}/{len(events)}] {start:.1f}-{end:.1f}s -> {destination}")
    return clips


def train_candidate(args: argparse.Namespace) -> str:
    clean_clips = [
        path for path in (args.dataset / "clean").glob("*")
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
    ]
    suspicious_clips = [
        path for path in (args.dataset / "suspicious").glob("*")
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
    ]
    if not clean_clips or not suspicious_clips:
        return "Training skipped: add processed clips to both clean and cheating folders first."
    if not args.skip_training:
        try:
            from src.core.train_workflow import PixelVisionTrainingWorkflow
        except ImportError as exc:
            if getattr(exc, "name", None) == "torch":
                return "Training skipped: training packages are missing; install requirements-train.txt."
            raise RuntimeError(f"Cannot load candidate trainer: {exc}") from exc
        candidate_dir = args.candidates / datetime.now().strftime("candidate_%Y%m%d_%H%M%S")
        workflow = PixelVisionTrainingWorkflow(data_root=str(args.dataset), models_dir=str(candidate_dir))
        if not workflow.execute_pipeline_retraining(epochs=args.epochs, batch_size=args.batch_size):
            raise RuntimeError("Candidate training failed; check the dataset and training output.")
        return (
            f"Offline candidate saved to {candidate_dir}. "
            "The live CheatVision pipeline was not changed and does not load this candidate."
        )
    return "Clip processing finished; candidate training was disabled."


def process_inbox(args: argparse.Namespace) -> int:
    (args.inbox / "clean").mkdir(parents=True, exist_ok=True)
    (args.inbox / "cheating").mkdir(parents=True, exist_ok=True)
    vods = _find_vods(args.inbox)
    if not vods:
        print(f"[ERROR] No VODs found under {args.inbox}\\clean or {args.inbox}\\cheating")
        return 1

    clip_count = 0
    for label, vod_path in vods:
        player_tag = input(f"Enter the watched player's exact gamer tag for {vod_path.name}: ").strip()
        if not player_tag:
            print(f"[ERROR] No gamer tag entered for {vod_path.name}; stopping.")
            return 1
        clip_count += len(process_vod(vod_path, label, player_tag, args))

    if clip_count == 0:
        print("[ERROR] No clips were created; candidate training was not started.")
        return 1
    print(f"[DONE] Created {clip_count} labeled clips under {args.dataset}")
    if not args.skip_training:
        print(f"[CANDIDATE] {train_candidate(args)}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inbox", type=Path, default=ROOT / "data" / "vod_inbox")
    parser.add_argument("--dataset", type=Path, default=ROOT / "data" / "vod_dataset")
    parser.add_argument("--candidates", type=Path, default=ROOT / "data" / "models" / "candidates")
    parser.add_argument(
        "--roi",
        type=parse_roi,
        default=DEFAULT_KILL_FEED_ROI,
        metavar="X0,Y0,X1,Y1",
        help="Normalized Warzone kill-feed crop; default is the middle-left area.",
    )
    parser.add_argument(
        "--elimination-roi",
        type=parse_roi,
        default=DEFAULT_ELIMINATION_ROI,
        metavar="X0,Y0,X1,Y1",
        help="Normalized Warzone elimination-toast crop; default is mid-right.",
    )
    parser.add_argument(
        "--player-hud-roi",
        type=parse_roi,
        default=DEFAULT_PLAYER_HUD_ROI,
        metavar="X0,Y0,X1,Y1",
        help="Normalized bottom-left player-name HUD crop used to verify VOD POV identity.",
    )
    parser.add_argument("--sample-seconds", type=float, default=0.25, help="OCR interval in seconds.")
    parser.add_argument("--before", type=float, default=10.0, help="Seconds before the detected kill to include.")
    parser.add_argument("--after", type=float, default=4.0, help="Seconds after the detected kill to include.")
    parser.add_argument("--min-confidence", type=float, default=0.60)
    parser.add_argument("--match-threshold", type=float, default=0.90)
    parser.add_argument("--epochs", type=int, default=25)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--skip-training", action="store_true", help="Only create labeled clips; do not train a candidate.")
    args = parser.parse_args(argv)
    if args.sample_seconds <= 0 or args.before < 0 or args.after <= 0:
        parser.error("sample interval and after duration must be positive; before duration cannot be negative")
    if not 0 < args.min_confidence <= 1 or not 0 < args.match_threshold <= 1:
        parser.error("OCR confidence and name match threshold must be in (0, 1]")
    if args.epochs < 1 or args.batch_size < 1:
        parser.error("epochs and batch size must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        return process_inbox(parse_args(argv))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"[ERROR] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
