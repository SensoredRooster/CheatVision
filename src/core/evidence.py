from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from src.core.anti_cheat_pipeline import CheatEvent

# Frames kept for evidence, downscaled so the buffer stays small: 960x540 BGR is
# ~1.5 MB, so 3 s at 60 unique frames/s is ~270 MB worst case; we keep fewer.
_EVIDENCE_WIDTH = 960
_EVIDENCE_HEIGHT = 540
_PRE_SECONDS = 1.5
_POST_SECONDS = 1.0
_MAX_BUFFER_FRAMES = 120
_CLIP_FPS_FALLBACK = 60.0


def _no_window_flags() -> int:
    if sys.platform != "win32":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _safe_name(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in text)


class EvidenceRecorder:
    """Keeps the last few seconds of frames and, when a flag fires, saves proof
    for it: an annotated snapshot of the flagged frame, a short clip around it,
    and the event's numbers as JSON -- one folder per incident under
    ``<project>/data/incidents/``.

    The buffer is fed by the capture/playback path at display resolution and
    downscaled here; the writer runs on its own thread so the UI and analysis
    never wait on disk or the encoder.
    """

    def __init__(self, project_root: str | Path):
        self.root = Path(project_root) / "data" / "incidents"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._frames: deque[tuple[float, np.ndarray]] = deque(maxlen=_MAX_BUFFER_FRAMES)
        self._pending: list[dict[str, Any]] = []
        self._fps_estimate = _CLIP_FPS_FALLBACK
        self._ffmpeg = shutil.which("ffmpeg")
        self._encoder: str | None = None
        self._worker = threading.Thread(target=self._writer_loop, daemon=True)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._worker.start()

    # ------------------------------------------------------------------
    # feeding
    # ------------------------------------------------------------------
    def push_frame(self, frame: np.ndarray, timestamp: float) -> None:
        if frame is None or frame.size == 0:
            return
        h, w = frame.shape[:2]
        if w > _EVIDENCE_WIDTH or h > _EVIDENCE_HEIGHT:
            scale = min(_EVIDENCE_WIDTH / w, _EVIDENCE_HEIGHT / h)
            small = cv2.resize(frame, (max(2, int(w * scale)), max(2, int(h * scale))), interpolation=cv2.INTER_LINEAR)
        else:
            # Already evidence-sized (the pipeline's analysis frame): keep the
            # reference. The pipeline never writes into it, and the capture
            # path allocates a fresh buffer per frame, so it is stable.
            small = frame
        with self._lock:
            self._frames.append((float(timestamp), small))
            if len(self._frames) >= 10:
                span = self._frames[-1][0] - self._frames[0][0]
                if span > 0:
                    self._fps_estimate = max(10.0, min(240.0, (len(self._frames) - 1) / span))

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()

    # ------------------------------------------------------------------
    # capturing an incident
    # ------------------------------------------------------------------
    def capture(self, event: CheatEvent, source_label: str = "") -> Path:
        """Snapshot the buffer now and schedule the files. Returns the folder
        the evidence will land in (created immediately so the UI can link it)."""
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        folder = self.root / f"{stamp}_{_safe_name(event.cheat_category.lower())}_fr{event.frame_id}"
        folder.mkdir(parents=True, exist_ok=True)
        with self._lock:
            pre = [(t, f) for (t, f) in self._frames if f is not None]
            fps = self._fps_estimate
        job = {
            "folder": folder,
            "event": event,
            "pre": pre,
            "post": [],
            "post_until": time.time() + _POST_SECONDS,
            "fps": fps,
            "source": source_label,
        }
        with self._lock:
            self._pending.append(job)
        self._wake.set()
        return folder

    # ------------------------------------------------------------------
    # writer thread
    # ------------------------------------------------------------------
    def _writer_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=0.25)
            self._wake.clear()
            now = time.time()
            ready: list[dict[str, Any]] = []
            with self._lock:
                for job in list(self._pending):
                    # Collect frames that arrived after the flag until the post window closes.
                    last_t = job["pre"][-1][0] if job["pre"] else 0.0
                    if job["post"]:
                        last_t = job["post"][-1][0]
                    job["post"].extend((t, f) for (t, f) in self._frames if t > last_t)
                    if now >= job["post_until"]:
                        ready.append(job)
                        self._pending.remove(job)
            for job in ready:
                try:
                    self._write_job(job)
                except Exception as exc:
                    print(f"[EVIDENCE] [WARN] could not write incident files: {exc!r}")

    def _write_job(self, job: dict[str, Any]) -> None:
        folder: Path = job["folder"]
        event: CheatEvent = job["event"]
        pre_window = job["pre"]
        cutoff = (pre_window[-1][0] - _PRE_SECONDS) if pre_window else 0.0
        frames = [f for (t, f) in pre_window if t >= cutoff] + [f for (_t, f) in job["post"]]
        flagged = pre_window[-1][1] if pre_window else (frames[-1] if frames else None)

        if flagged is not None:
            cv2.imwrite(str(folder / "snapshot.png"), self._annotate(flagged.copy(), event))

        if frames:
            self._write_clip(folder / "clip.mp4", frames, job["fps"])

        details = asdict(event)
        details["telemetry_data"] = {
            k: v for k, v in (event.telemetry_data or {}).items() if k not in ("path", "residuals")
        }
        details["telemetry_data"]["path"] = (event.telemetry_data or {}).get("path", [])
        details["source"] = job["source"]
        details["files"] = {
            "snapshot": "snapshot.png" if flagged is not None else None,
            "clip": "clip.mp4" if frames else None,
            "frames_in_clip": len(frames),
            "clip_fps": round(job["fps"], 1),
        }
        (folder / "event.json").write_text(json.dumps(details, indent=2, default=str), encoding="utf-8")
        print(f"[EVIDENCE] saved {folder.name}: {len(frames)} frames, snapshot={'yes' if flagged is not None else 'no'}")

    # ------------------------------------------------------------------
    # drawing
    # ------------------------------------------------------------------
    @staticmethod
    def _annotate(frame: np.ndarray, event: CheatEvent) -> np.ndarray:
        h, w = frame.shape[:2]
        tele = event.telemetry_data or {}
        # Path is in analysis space (analysis_size); scale it onto this frame.
        asize = tele.get("analysis_size") or [w, h]
        sx = w / max(1, int(asize[0]))
        sy = h / max(1, int(asize[1]))
        point = tele.get("crosshair_point") or [w // 2, h // 2]
        cx, cy = int(point[0] * sx), int(point[1] * sy)
        # BGR, following the app palette: steel aim path, alert-red reticle,
        # charcoal text chips (assets/brand/BRAND.md).
        path = tele.get("path") or []
        if len(path) >= 2:
            # Path is relative displacement; anchor its end at the reticle.
            end = path[-1]
            pts = [(int(cx + (p[0] - end[0]) * sx), int(cy + (p[1] - end[1]) * sy)) for p in path]
            for a, b in zip(pts, pts[1:]):
                cv2.line(frame, a, b, (227, 222, 217), 2, cv2.LINE_AA)
            cv2.circle(frame, pts[0], 5, (227, 222, 217), -1, cv2.LINE_AA)
        cv2.circle(frame, (cx, cy), 10, (87, 77, 255), 2, cv2.LINE_AA)
        cv2.line(frame, (cx - 16, cy), (cx + 16, cy), (87, 77, 255), 1, cv2.LINE_AA)
        cv2.line(frame, (cx, cy - 16), (cx, cy + 16), (87, 77, 255), 1, cv2.LINE_AA)

        lines = [
            f"{event.cheat_category}  conf {event.confidence_score:.2f}",
            f"{event.timestamp}   frame {event.frame_id}",
            f"velocity {float(tele.get('velocity', 0)):.1f}px  straightness {float(tele.get('straightness', 0)):.3f}  "
            f"tremor {float(tele.get('tremor_variance', 0)):.2f}",
        ]
        track = tele.get("associated_track_id")
        if track is not None:
            lines.append(f"target track #{track}")
        y = 26
        for text in lines:
            (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
            cv2.rectangle(frame, (10, y - th - 8), (16 + tw, y + 6), (27, 24, 21), -1)
            cv2.putText(frame, text, (13, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (212, 206, 200), 1, cv2.LINE_AA)
            y += th + 16
        return frame

    # ------------------------------------------------------------------
    # clip writing
    # ------------------------------------------------------------------
    def _write_clip(self, path: Path, frames: list[np.ndarray], fps: float) -> None:
        h, w = frames[0].shape[:2]
        w -= w % 2
        h -= h % 2
        if self._ffmpeg:
            for encoder, opts in (
                ("h264_nvenc", ["-preset", "p4", "-cq", "23", "-b:v", "0"]),
                ("libx264", ["-preset", "veryfast", "-crf", "20"]),
            ):
                if self._encoder is not None and encoder != self._encoder:
                    continue
                cmd = [
                    self._ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", f"{fps:.2f}", "-i", "-",
                    "-an", "-c:v", encoder, *opts, "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path),
                ]
                try:
                    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.PIPE, creationflags=_no_window_flags())
                    for f in frames:
                        if f.shape[1] != w or f.shape[0] != h:
                            f = cv2.resize(f, (w, h), interpolation=cv2.INTER_LINEAR)
                        proc.stdin.write(np.ascontiguousarray(f).tobytes())
                    proc.stdin.close()
                    proc.wait(timeout=30)
                    if proc.returncode == 0 and path.is_file() and path.stat().st_size > 0:
                        self._encoder = encoder
                        return
                except Exception:
                    pass
        # Last resort: OpenCV's mp4v writer.
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
        if writer.isOpened():
            for f in frames:
                if f.shape[1] != w or f.shape[0] != h:
                    f = cv2.resize(f, (w, h), interpolation=cv2.INTER_LINEAR)
                writer.write(f)
            writer.release()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        self._worker.join(timeout=5.0)
