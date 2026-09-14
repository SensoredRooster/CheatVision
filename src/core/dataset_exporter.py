from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Tuple

import cv2
import numpy as np

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")

# Frames waiting for the encoder. Each 2560x1440 frame is ~11 MB, so this is
# the memory ceiling of a recording (~90 MB); the old 60-deep queue reached
# 660 MB the moment the encoder fell behind. With a hardware encoder the
# queue never fills; with a slow software fallback frames are dropped and
# counted instead of hoarded.
_WRITE_QUEUE_DEPTH = 8
# ffmpeg encoders tried in order for the baseline recorder. OpenCV's own
# writers max out around 23 fps at 1440p (measured: mp4v 23, XVID 28, MJPG 12)
# so they only remain as the last resort when ffmpeg is missing.
_FFMPEG_ENCODERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("h264_nvenc", ("-preset", "p4", "-tune", "ll", "-rc", "vbr", "-cq", "23", "-b:v", "0")),
    ("h264_qsv", ("-preset", "veryfast", "-global_quality", "23")),
    ("h264_amf", ("-quality", "speed", "-rc", "cqp", "-qp_i", "22", "-qp_p", "24")),
    ("libx264", ("-preset", "ultrafast", "-crf", "23")),
)


def _even(value: int) -> int:
    value = max(2, int(value))
    return value if value % 2 == 0 else value - 1


def _no_window_flags() -> int:
    if sys.platform != "win32":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _h264_available() -> bool:
    names = (
        "openh264-2.5.0-win64.dll",
        "openh264-1.8.0-win64.dll",
        "openh264.dll",
    )
    search = [Path.cwd(), Path(os.environ.get("SystemRoot", "C:\\Windows")) / "System32"]
    for folder in search:
        for name in names:
            if (folder / name).is_file():
                return True
    return False


def _writer_candidates() -> tuple[tuple[str, str], ...]:
    codecs: list[tuple[str, str]] = []
    if _h264_available():
        codecs.extend([(".mp4", "avc1"), (".mp4", "H264")])
    codecs.extend([(".mp4", "mp4v"), (".avi", "XVID"), (".avi", "MJPG")])
    return tuple(codecs)


def _open_video_writer(path_stem: Path, fps: float, width: int, height: int) -> tuple[cv2.VideoWriter | None, Path | None]:
    width = _even(width)
    height = _even(height)
    fps = max(1.0, float(fps))
    for suffix, codec in _writer_candidates():
        output_path = path_stem.with_suffix(suffix)
        fourcc = cv2.VideoWriter_fourcc(*codec)
        writer = cv2.VideoWriter(str(output_path), fourcc, fps, (width, height))
        if writer is not None and writer.isOpened():
            return writer, output_path
        if writer is not None:
            writer.release()
    return None, None


_ENCODER_LOCK = threading.Lock()
_WORKING_ENCODER: str | None = None
_ENCODER_PROBED = False


def _encoder_works(name: str, options: tuple[str, ...]) -> bool:
    """One tiny real encode: the only reliable test that a hardware encoder
    is actually usable on this machine (the driver may be absent even though
    ffmpeg lists the encoder)."""
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "lavfi", "-i", "color=c=black:s=256x256:r=30:d=0.2",
        "-c:v", name, *options, "-pix_fmt", "yuv420p", "-f", "null", "-",
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, timeout=15, check=False, creationflags=_no_window_flags()
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _pick_ffmpeg_encoder() -> tuple[str, tuple[str, ...]] | None:
    global _WORKING_ENCODER, _ENCODER_PROBED
    with _ENCODER_LOCK:
        if not _ENCODER_PROBED:
            _ENCODER_PROBED = True
            if shutil.which("ffmpeg") is not None:
                for name, options in _FFMPEG_ENCODERS:
                    if _encoder_works(name, options):
                        _WORKING_ENCODER = name
                        print(f"[EXPORT] baseline encoder: {name}")
                        break
                else:
                    print("[EXPORT] [WARN] no ffmpeg encoder works; falling back to OpenCV writer (slow)")
        if _WORKING_ENCODER is None:
            return None
        for name, options in _FFMPEG_ENCODERS:
            if name == _WORKING_ENCODER:
                return name, options
        return None


class _FfmpegFrameSink:
    """Raw BGR frames -> ffmpeg stdin -> encoded mp4."""

    # ffmpeg does not start reading stdin until the encoder is initialised
    # (~200ms for NVENC). A default-sized pipe makes the very first write block
    # for that long and the queue behind it overflow; a pipe that holds several
    # frames absorbs the start-up instead.
    _STDIN_PIPE_BYTES = 64 * 1024 * 1024

    def __init__(self, output_path: Path, fps: float, width: int, height: int, encoder: str, options: tuple[str, ...]):
        self.output_path = output_path
        self.width = width
        self.height = height
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}", "-r", f"{fps:.3f}",
            "-i", "-", "-an", "-c:v", encoder, *options, "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(output_path),
        ]
        child_stdin, self._stdin = self._open_stdin_pipe(width * height * 3)
        try:
            self._process = subprocess.Popen(
                command,
                stdin=child_stdin,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=_no_window_flags(),
            )
        finally:
            if isinstance(child_stdin, int):
                os.close(child_stdin)
        if self._stdin is None:
            self._stdin = self._process.stdin

    @classmethod
    def _open_stdin_pipe(cls, frame_size: int):
        if sys.platform != "win32":
            return subprocess.PIPE, None
        import _winapi
        import msvcrt

        read_handle, write_handle = _winapi.CreatePipe(None, max(cls._STDIN_PIPE_BYTES, frame_size * 4))
        child_fd = msvcrt.open_osfhandle(read_handle, os.O_RDONLY | os.O_BINARY)
        write_fd = msvcrt.open_osfhandle(write_handle, 0)
        return child_fd, os.fdopen(write_fd, "wb", buffering=0)

    def isOpened(self) -> bool:
        return self._process.poll() is None and self._stdin is not None

    def write(self, frame: np.ndarray) -> None:
        if not frame.flags["C_CONTIGUOUS"]:
            frame = np.ascontiguousarray(frame)
        view = memoryview(frame).cast("B")
        written = 0
        while written < len(view):
            written += self._stdin.write(view[written:])

    def release(self) -> str:
        """Close the input so ffmpeg writes the trailer; returns stderr text."""
        try:
            if self._stdin is not None:
                self._stdin.close()
        except Exception:
            pass
        self._stdin = None
        try:
            self._process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self._process.kill()
        try:
            return (self._process.stderr.read() or b"").decode("utf-8", errors="replace").strip()
        except Exception:
            return ""


class _BaselineSession:
    """One recording: its own queue, writer thread and encoder, so stopping is
    instant for the UI while the file is finalised in the background and a new
    recording can start immediately without sharing any state."""

    def __init__(self, output_stem: Path, fps: float, size: tuple[int, int], label: str = "baseline"):
        self.label = label
        self.output_stem = output_stem
        self.output_path: Path = output_stem.with_suffix(".mp4")
        self.fps = max(1.0, float(fps))
        self.size = (_even(size[0]), _even(size[1]))
        self.queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=_WRITE_QUEUE_DEPTH)
        self.stop_event = threading.Event()
        self.frames_written = 0
        self.frames_dropped = 0
        self.error = ""
        # Open the encoder before accepting any frame: doing it lazily stalled
        # long enough (probe + process spawn, ~200ms) to overflow the queue
        # and lose the first frames of every recording.
        self._sink = self._open_sink()
        self.ok = self._sink is not None
        if not self.ok:
            self.error = "no usable video encoder"
            print(f"[EXPORT] [WARN] {self.label} recording aborted: {self.error}")
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit(self, frame: np.ndarray) -> None:
        try:
            self.queue.put_nowait(frame.copy())
        except queue.Full:
            self.frames_dropped += 1

    def stop(self) -> None:
        self.stop_event.set()

    def is_alive(self) -> bool:
        return self.ok and self._thread.is_alive()

    def join(self, timeout: float) -> None:
        if self.ok:
            self._thread.join(timeout=timeout)

    def _open_sink(self):
        width, height = self.size
        chosen = _pick_ffmpeg_encoder()
        if chosen is not None:
            name, options = chosen
            sink = _FfmpegFrameSink(self.output_path, self.fps, width, height, name, options)
            if sink.isOpened():
                return sink
        writer, path = _open_video_writer(self.output_stem, self.fps, width, height)
        if writer is not None and path is not None:
            self.output_path = path
            return writer
        return None

    def _run(self) -> None:
        sink = self._sink
        size = self.size
        try:
            while not self.stop_event.is_set() or not self.queue.empty():
                try:
                    frame = self.queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if frame.shape[1] != size[0] or frame.shape[0] != size[1]:
                    frame = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
                try:
                    sink.write(frame)
                except (OSError, ValueError, cv2.error) as exc:
                    self.error = f"encoder write failed: {exc!r}"
                    return
                self.frames_written += 1
        finally:
            try:
                detail = sink.release()
                if isinstance(detail, str) and detail and not self.error:
                    self.error = detail[-200:]
            except Exception:
                pass
            summary = f"[EXPORT] {self.label} saved {self.output_path.name}: {self.frames_written} frames"
            if self.frames_dropped:
                summary += f", {self.frames_dropped} dropped (encoder too slow)"
            if self.error:
                summary += f" [{self.error}]"
            print(summary)


class PixelVisionDatasetExporter:
    def __init__(self, target_resolution: Tuple[int, int] = (2560, 1440), project_root: str | None = None):
        self.width, self.height = target_resolution
        self.project_root = Path(project_root) if project_root else Path.cwd()
        self.is_recording_baseline = False
        self.is_recording_session = False
        self.clean_dir = self.project_root / "data" / "clean"
        self.suspicious_dir = self.project_root / "data" / "suspicious"
        self.recordings_dir = self.project_root / "data" / "recordings"
        self.clean_dir.mkdir(parents=True, exist_ok=True)
        self.suspicious_dir.mkdir(parents=True, exist_ok=True)
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.baseline_fps = 60.0
        self._session: _BaselineSession | None = None
        # Plain session recording (data/recordings/): same writer machinery,
        # completely independent slot so it can run alongside a baseline.
        self._session_rec: _BaselineSession | None = None
        self._finishing: list[_BaselineSession] = []
        # Find a working encoder now (takes ~1s) so the first recording does
        # not pay for it while frames are already arriving.
        threading.Thread(target=_pick_ffmpeg_encoder, daemon=True).start()

    def set_baseline_fps(self, fps: float) -> None:
        """Rate the recorded file is stamped with; should match the unique
        picture rate of the source so playback speed is right."""
        if fps > 0:
            self.baseline_fps = float(fps)

    def start_clean_baseline_mode(self) -> str:
        if self.is_recording_baseline:
            return ""

        timestamp = int(time.time())
        session = _BaselineSession(
            self.clean_dir / f"baseline_session_{timestamp}", self.baseline_fps, (self.width, self.height)
        )
        if not session.ok:
            return ""
        self._session = session
        self.is_recording_baseline = True
        return str(session.output_path)

    def start_session_recording(self) -> str:
        """Record the watched feed to data/recordings/ — evidence of the whole
        sitting, carrying no clean/suspicious meaning for training."""
        if self.is_recording_session:
            return ""

        timestamp = int(time.time())
        session = _BaselineSession(
            self.recordings_dir / f"session_{timestamp}",
            self.baseline_fps,
            (self.width, self.height),
            label="session recording",
        )
        if not session.ok:
            return ""
        self._session_rec = session
        self.is_recording_session = True
        return str(session.output_path)

    def stop_session_recording(self) -> None:
        self.is_recording_session = False
        session = self._session_rec
        self._session_rec = None
        if session is not None:
            session.stop()
            self._finishing = [s for s in self._finishing if s.is_alive()]
            self._finishing.append(session)

    def set_target_resolution(self, width: int, height: int) -> None:
        self.width = _even(width)
        self.height = _even(height)

    def stop_clean_baseline_mode(self) -> None:
        self.is_recording_baseline = False
        session = self._session
        self._session = None
        if session is not None:
            session.stop()
            # Finalising the mp4 continues in the session's own thread; keep a
            # reference so it can be reported/joined at shutdown.
            self._finishing = [s for s in self._finishing if s.is_alive()]
            self._finishing.append(session)

    def wait_for_pending_writes(self, timeout: float = 10.0) -> None:
        deadline = time.time() + timeout
        for session in list(self._finishing):
            remaining = max(0.0, deadline - time.time())
            session.join(remaining)
        self._finishing = [s for s in self._finishing if s.is_alive()]

    @property
    def baseline_frames_dropped(self) -> int:
        return self._session.frames_dropped if self._session is not None else 0

    def write_frame(self, frame: np.ndarray) -> None:
        session = self._session
        if session is not None and self.is_recording_baseline:
            session.submit(frame)
        recording = self._session_rec
        if recording is not None and self.is_recording_session:
            recording.submit(frame)

    def export_suspicious_incident_clip(self, historical_frames_buffer: list[np.ndarray], incident_id: int) -> str:
        if not historical_frames_buffer:
            return ""

        first = historical_frames_buffer[0]
        height, width = first.shape[:2]
        stem = self.suspicious_dir / f"flagged_incident_fr_{incident_id}_{int(time.time())}"
        writer, output_path = _open_video_writer(stem, 60.0, width, height)
        if writer is None or output_path is None:
            return ""

        out_w, out_h = _even(width), _even(height)
        for frame in historical_frames_buffer:
            if frame.shape[:2] != (out_h, out_w):
                frame = cv2.resize(frame, (out_w, out_h), interpolation=cv2.INTER_AREA)
            writer.write(frame)

        writer.release()
        return str(output_path)
