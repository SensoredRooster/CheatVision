from __future__ import annotations

import os
import shutil
import time
import subprocess
import threading
import re
import sys
import ctypes
from collections import deque
from ctypes import wintypes
from typing import Any

import cv2
import numpy as np
from mss import mss

_BROWSER_TITLE_HINTS = (
    "chrome",
    "edge",
    "firefox",
    "brave",
    "opera",
    "kick",
    "twitch",
    "youtube",
)
_BROWSER_CLASSES = ("Chrome_WidgetWin_1", "MozillaWindowClass", "ApplicationFrameWindow")
_BROWSER_SKIP = ("pixelvision", "cheatvision")
_BROWSER_MIN_WIDTH = 400
_BROWSER_MIN_HEIGHT = 300


def _find_browser_window() -> tuple[int, dict[str, int]] | None:
    if sys.platform != "win32":
        return None
    user32 = ctypes.windll.user32
    found: list[tuple[int, int, dict[str, int]]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = int(user32.GetWindowTextLengthW(hwnd))
        if length <= 0:
            return True
        title_buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title_buf, length + 1)
        title = title_buf.value.lower()
        if any(skip in title for skip in _BROWSER_SKIP):
            return True
        class_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buf, 256)
        class_name = class_buf.value
        if class_name not in _BROWSER_CLASSES and not any(hint in title for hint in _BROWSER_TITLE_HINTS):
            return True
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width < _BROWSER_MIN_WIDTH or height < _BROWSER_MIN_HEIGHT:
            return True
        region = {"left": int(rect.left), "top": int(rect.top), "width": width, "height": height}
        found.append((width * height, int(hwnd), region))
        return True

    user32.EnumWindows(_enum, 0)
    if not found:
        return None
    found.sort(key=lambda item: item[0], reverse=True)
    _area, hwnd, region = found[0]
    return hwnd, region


def _window_region(hwnd: int) -> dict[str, int] | None:
    if sys.platform != "win32" or not hwnd:
        return None
    user32 = ctypes.windll.user32
    if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
        return None
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if width < _BROWSER_MIN_WIDTH or height < _BROWSER_MIN_HEIGHT:
        return None
    return {"left": int(rect.left), "top": int(rect.top), "width": width, "height": height}

_RANGE_MODE_PATTERN = re.compile(
    r"(?:pixel_format|vcodec)=(\S+)\s+min s=(\d+)x(\d+) fps=([0-9.]+) max s=(\d+)x(\d+) fps=([0-9.]+)"
)
_DISCRETE_MODE_PATTERN = re.compile(r"(?:pixel_format|vcodec)=(\S+)\s+s=(\d+)x(\d+) fps=([0-9.]+)")

# A device can *advertise* a resolution/fps combination while the real
# ffmpeg(dshow) -> OS pipe -> Python read loop still can't sustain the raw
# BGR24 byte-rate it implies -- ffmpeg's own internal capture buffer overflows
# and silently drops the vast majority of frames ("real-time buffer ... too
# full ... frame dropped!"), which looks to the app like a frozen video feed.
# Picking "largest advertised resolution x fps" is therefore not safe; the
# mode actually used has to be measured against the live device at startup.
#
# The candidate ladder itself must only contain resolution/fps pairs the
# device's own `-list_options` output actually advertises for the pixel
# format we request (bgr24) -- requesting a combination the driver doesn't
# support fails with "Could not set video options ... I/O error", a hard
# mode-rejection (not a bandwidth/overflow signal, and not fixable by
# retrying or adding delays). Confirmed on real hardware: different
# resolutions have very different valid fps ranges on the same device (e.g.
# the development card's (AVerMedia GC573) bgr24 block only accepts 1280x720 at 50-60.0002fps,
# not 30fps as a generic ladder might guess), so candidates are generated
# per-resolution from the real advertised range rather than assumed values.
_FPS_STEP_LADDER = (144.0, 120.0, 90.0, 85.0, 75.0, 60.0, 50.0, 30.0, 24.0)
# Try every standard step within a resolution's supported range, not just the
# fastest few -- the top fps values at a large resolution (144/120/90) tend
# to be exactly the ones that overflow the capture buffer, and stopping the
# per-resolution search too early skips right past slower-but-clean options
# (measured on real hardware: 1920x1080@90 overflows but @75-85 is clean) in
# favor of dropping to a much smaller resolution unnecessarily.
_MAX_FPS_CANDIDATES_PER_RESOLUTION = 2
_MAX_CALIBRATION_CANDIDATES = 8
_CALIBRATION_FPS_TOLERANCE = 0.5
# A short test window is unreliable near the real bandwidth cliff: ffmpeg's
# 256M rtbufsize buffer takes time to visibly overflow, and a borderline
# candidate can measure as "clean" by pure timing luck in under a second,
# only to actually start dropping frames a few seconds into a real session --
# reproducing the exact intermittent-freeze bug this calibration exists to
# prevent. 2.5s plus a strict near-100% fps ratio gives the buffer enough
# time to reveal marginal overflow and rejects modes that only barely keep up.
_CALIBRATION_TEST_DURATION_SEC = 2.5
_CALIBRATION_SETTLE_SEC = 0.35
_CALIBRATION_MIN_FPS_RATIO = 0.92
# With -fps_mode passthrough the pipe carries only frames the driver really
# produced, so a candidate is judged on steady delivery rather than on hitting
# the nominal rate: anything at or above this is a healthy live feed.
_CALIBRATION_MIN_ABSOLUTE_FPS = 50.0
# A virtual camera whose host app has it switched off opens but never sends a
# frame; how long to wait for the first one before reporting that.
_VIRTUAL_CAMERA_FIRST_FRAME_SEC = 4.0

# The ffmpeg(dshow) process + reader-thread/event handoff needs real time
# after isOpened() to reach steady-state frame delivery -- the first stretch
# of frames trickles in well below the target rate while the driver locks
# onto the capture cadence and the reader thread ramps up. Starting the
# strict timing window immediately (0s warmup) bakes that startup lag into
# the average and fails candidates that are otherwise trivially achievable:
# measured on real hardware, 640x480@60 (~55MB/s, nowhere near any real
# bandwidth limit) scores only 0.67x target fps with no warmup but a clean
# 1.00x with a 0.75s warmup discarded first. A full 1.0s gives an extra
# margin over that measured minimum so borderline-slow driver startups on
# other machines/devices don't reintroduce the same false-fail.
_CALIBRATION_WARMUP_SEC = 1.0

# Measured on the development card (AVerMedia GC573, ffmpeg dshow -> raw pipe
# -> unbuffered readinto): 2560x1440 bgr24 at 144fps (~1.59GB/s raw) sustains
# for the full test window with zero "real-time buffer too full" drops. The
# old ~545MB/s cliff was an artifact of reading the pipe through Python's
# BufferedReader, not a device or OS limit. Any candidate whose raw bgr24 byte
# rate clears this ceiling is skipped before spending a real hardware probe on
# it; everything under it still goes through the full strict hardware test.
_BANDWIDTH_CEILING_BYTES_PER_SEC = 1_700_000_000


def selectable_modes_from_ranges(
    resolution_fps_ranges: dict[tuple[int, int], tuple[float, float]],
    *,
    apply_bandwidth_ceiling: bool = True,
) -> list[tuple[int, int, int]]:
    """Turn the advertised {(w, h): (min_fps, max_fps)} map into the discrete
    modes a user can pin: every standard rate step inside each resolution's
    advertised span, plus the span's own ceiling when no step lands on it.
    Mirrors the rate filter the calibration ladder applies (bandwidth ceiling
    included) so the picker only offers modes the app would ever attempt.
    Sorted largest resolution first, fastest rate first."""
    modes: list[tuple[int, int, int]] = []
    for (width, height), (low, high) in resolution_fps_ranges.items():
        if width <= 0 or height <= 0 or high <= 0:
            continue
        rates: list[float] = [
            fps
            for fps in _FPS_STEP_LADDER
            if low - _CALIBRATION_FPS_TOLERANCE <= fps <= high + _CALIBRATION_FPS_TOLERANCE
        ]
        if not any(abs(high - fps) <= _CALIBRATION_FPS_TOLERANCE for fps in rates):
            rates.append(high)
        for fps in rates:
            if apply_bandwidth_ceiling and width * height * fps * 3 > _BANDWIDTH_CEILING_BYTES_PER_SEC:
                continue
            mode = (int(width), int(height), int(round(fps)))
            if mode[2] > 0 and mode not in modes:
                modes.append(mode)
    modes.sort(key=lambda m: (m[0] * m[1], m[2]), reverse=True)
    return modes

# Freeze detection: a stream is only "frozen" once consecutive downscaled
# grayscale samples stay near-identical (mean absdiff below threshold) for a
# sustained run of frames -- a single duplicated/dropped frame must never
# trip this, only a real stuck signal.
_FREEZE_ABSDIFF_THRESHOLD = 1.5
_FREEZE_HOLD_FRAMES = 90
# Frame-based hold alone shrinks to ~0.6s at 144fps, which trips on ordinary
# menus/loading screens; hold for at least this long regardless of rate.
_FREEZE_HOLD_SECONDS = 1.5
_FREEZE_SAMPLE_SIZE = (320, 180)
_PREVIEW_MAX_WIDTH = 2560
_MIN_AUTO_HEIGHT = 720
# Frames used to estimate the driver's real delivery rate.
_RATE_WINDOW_FRAMES = 120
# How long read() blocks waiting for the reader thread to publish a new frame
# before reporting "no frame yet" back to the capture loop.
_READ_WAIT_TIMEOUT_SEC = 0.02


def _preview_geometry(width: int, height: int, fps: int) -> tuple[int, int, int]:
    # The pipe is delivered at the device's real rate; never cap the reported fps.
    out_fps = max(1, int(fps))
    if width <= _PREVIEW_MAX_WIDTH:
        out_w, out_h = max(2, int(width)), max(2, int(height))
    else:
        out_w = _PREVIEW_MAX_WIDTH
        out_h = max(2, int(round(height * (out_w / float(width)))))
    if out_h < _MIN_AUTO_HEIGHT and height >= _MIN_AUTO_HEIGHT:
        out_w, out_h = max(2, int(width)), max(2, int(height))
    if out_w % 2:
        out_w -= 1
    if out_h % 2:
        out_h -= 1
    return max(2, out_w), max(2, out_h), out_fps


def _freeze_sample(frame: np.ndarray) -> np.ndarray:
    # Nearest-neighbour sampling is ~50x cheaper than an area filter and is
    # sufficient here: a genuinely frozen raw signal gives identical samples,
    # while live gameplay differs on almost every sampled pixel.
    small = cv2.resize(frame, _FREEZE_SAMPLE_SIZE, interpolation=cv2.INTER_NEAREST)
    return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)


def _subprocess_no_window() -> int:
    if sys.platform != "win32":
        return 0
    flags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags |= subprocess.CREATE_NO_WINDOW
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        flags |= subprocess.CREATE_NEW_PROCESS_GROUP
    return flags


# Kernel-side pipe buffer between ffmpeg and the reader. The default
# subprocess pipe holds ~32KB, so ffmpeg blocks after every 32KB write and a
# 2560x1440 frame takes ~340 ReadFile calls (each one re-taking the GIL); the
# moment the reader is briefly busy, ffmpeg stalls and its real-time buffer
# starts dropping frames. With several frames of kernel buffering ffmpeg never
# blocks, and a reader that fell behind drains a whole frame per call.
_PIPE_BUFFER_BYTES = 64 * 1024 * 1024


def _open_frame_pipe(frame_size: int):
    """Return (child_stdout, reader) for ffmpeg's stdout.

    On Windows this builds a pipe with a large kernel buffer; elsewhere the
    caller falls back to a normal subprocess.PIPE.
    """
    if sys.platform != "win32":
        return subprocess.PIPE, None
    import _winapi
    import msvcrt

    buffer_bytes = max(_PIPE_BUFFER_BYTES, frame_size * 4)
    read_handle, write_handle = _winapi.CreatePipe(None, buffer_bytes)
    child_fd = msvcrt.open_osfhandle(write_handle, 0)
    read_fd = msvcrt.open_osfhandle(read_handle, os.O_RDONLY | os.O_BINARY)
    reader = os.fdopen(read_fd, "rb", buffering=0)
    return child_fd, reader


def _stop_ffmpeg_process(process: subprocess.Popen[bytes] | None, graceful_timeout: float = 3.0) -> bool:
    """Stop ffmpeg, politely first. Returns True once the process has exited.

    Terminating ffmpeg while its DirectShow graph is streaming can leave the
    AVerMedia driver's close routine hung in the kernel: the process becomes
    an unkillable zombie that still owns the device, and every later open
    (ours or OBS's) fails with "device already in use" until a reboot. ffmpeg
    stops the graph cleanly when it reads 'q' on stdin, so that always goes
    first; a hard kill is the fallback only.
    """
    if process is None:
        return True
    if process.poll() is None and process.stdin is not None:
        try:
            process.stdin.write(b"q\n")
            process.stdin.flush()
        except Exception:
            pass
        try:
            process.wait(timeout=graceful_timeout)
        except Exception:
            pass
    if process.poll() is None:
        try:
            process.kill()
        except Exception:
            pass
        if sys.platform == "win32" and process.pid:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    capture_output=True,
                    check=False,
                    timeout=3,
                    creationflags=_subprocess_no_window(),
                )
            except Exception:
                pass
        try:
            process.wait(timeout=2.0)
        except Exception:
            pass
    exited = process.poll() is not None
    if not exited:
        _remember_lingering(process)
    return exited


# ffmpeg processes that refused to exit (see _stop_ffmpeg_process). A new
# capture must not be opened while one of these still holds the device.
_LINGERING_LOCK = threading.Lock()
_LINGERING: list[subprocess.Popen[bytes]] = []
_LINGERING_WAIT_SEC = 8.0


def _run_ffmpeg_query(command: list[str], timeout: float = 8.0) -> str:
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_subprocess_no_window(),
        )
    except OSError:
        return ""
    stdout = stderr = b""
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop_ffmpeg_process(process, graceful_timeout=2.0)
        try:
            stdout, stderr = process.communicate(timeout=1.0)
        except Exception:
            stdout = stderr = b""
    return (stdout or b"").decode("utf-8", errors="replace") + "\n" + (stderr or b"").decode("utf-8", errors="replace")


def _remember_lingering(process: subprocess.Popen[bytes]) -> None:
    with _LINGERING_LOCK:
        if process not in _LINGERING:
            _LINGERING.append(process)


def _wait_for_lingering_ffmpeg(timeout: float = _LINGERING_WAIT_SEC) -> int:
    """Block (bounded) until earlier ffmpeg instances have really exited.

    Returns the number still alive afterwards.
    """
    deadline = time.time() + timeout
    while True:
        with _LINGERING_LOCK:
            _LINGERING[:] = [proc for proc in _LINGERING if proc.poll() is None]
            remaining = len(_LINGERING)
        if remaining == 0 or time.time() >= deadline:
            if remaining:
                print(f"[CAPTURE] [WARN] {remaining} previous ffmpeg process(es) still shutting down; device may be busy")
            return remaining
        time.sleep(0.05)


def _create_kill_on_close_job():
    """Windows job object that kills every assigned process when the app dies.

    Without it, a crash or Task-Manager kill of the app leaves ffmpeg running
    invisibly with the capture card open, and the next launch is starved.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
        )
        if not ok:
            kernel32.CloseHandle(job)
            return None
        return job
    except Exception:
        return None


def _assign_to_job(job, process: subprocess.Popen[bytes]) -> None:
    if job is None or sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.AssignProcessToJobObject(job, int(process._handle))
    except Exception:
        pass


_FFMPEG_JOB = _create_kill_on_close_job()


def _close_ffmpeg_pipes(process: subprocess.Popen[bytes] | None) -> None:
    if process is None:
        return
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is None:
            continue
        try:
            pipe.close()
        except Exception:
            pass


class FFmpegRawVideoCapture:
    def __init__(self, device_name: str, width: int, height: int, fps: int, pixel_format: str | None = None):
        self.device_name = device_name
        self.input_width = max(1, int(width))
        self.input_height = max(1, int(height))
        self.input_fps = max(1, int(fps))
        # DirectShow input pixel format to request (e.g. "bgr0" for virtual
        # cameras that only publish that); None lets the driver choose.
        self.input_pixel_format = pixel_format
        self.width, self.height, self.fps = _preview_geometry(self.input_width, self.input_height, self.input_fps)
        self._frame_size = self.width * self.height * 3
        self._process: subprocess.Popen[bytes] | None = None
        self._stdout = None
        self._reader_thread: threading.Thread | None = None
        self._reader_stop_event = threading.Event()
        self._lock = threading.Lock()
        self._frame_condition = threading.Condition(self._lock)
        self._latest_frame: np.ndarray | None = None
        self._latest_frame_seq = 0
        self._last_read_seq = 0
        self._latest_is_duplicate = False
        self.last_read_duplicate = False
        self._arrival_times: deque[float] = deque(maxlen=_RATE_WINDOW_FRAMES)
        self._stderr_thread: threading.Thread | None = None
        self._last_error_lines: deque[str] = deque(maxlen=20)
        self._freeze_sample: np.ndarray | None = None
        self._freeze_hold_count = 0
        self._freeze_hold_frames = max(_FREEZE_HOLD_FRAMES, int(round(_FREEZE_HOLD_SECONDS * self.fps)))
        self._stream_frozen = False
        self._open()

    def _open(self) -> None:
        _wait_for_lingering_ffmpeg()
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostats",
            "-fflags",
            "nobuffer+igndts",
            "-flags",
            "low_delay",
            *self._input_args(),
            "-an",
            "-vf",
            self._video_filter(),
            "-pix_fmt",
            "bgr24",
            # passthrough: emit exactly the frames the driver produced. The
            # default constant-frame-rate mode pads the output up to the
            # requested rate by repeating frames (measured: the GC573 driver
            # yields ~70 frames/s at 2560x1440 and ffmpeg was duplicating
            # every one of them to reach "144"), which doubles the work in
            # every stage downstream without adding a single new picture.
            "-fps_mode",
            "passthrough",
            "-f",
            "rawvideo",
            "-",
        ]
        # bufsize=0 is essential: Python's BufferedReader on top of the pipe
        # tops out well under the ~1.6 GB/s a 2560x1440@144 bgr24 stream
        # needs, which makes ffmpeg's real-time buffer fill up (latency) and
        # then drop frames (skips). Reading straight from the raw pipe into a
        # preallocated frame sustains the full rate with zero drops.
        child_stdout, reader = _open_frame_pipe(self._frame_size)
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=child_stdout,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=_subprocess_no_window(),
            )
        finally:
            if reader is not None:
                # The child now owns its copy of the write end; ours must go
                # or the reader would never see EOF when ffmpeg exits.
                os.close(child_stdout)
        _assign_to_job(_FFMPEG_JOB, self._process)
        self._stdout = reader if reader is not None else self._process.stdout
        self._last_error_lines.clear()
        self._reader_stop_event.clear()
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(target=self._stderr_drain_loop, daemon=True)
        self._stderr_thread.start()

    def device_busy(self) -> bool:
        """True when ffmpeg reported the DirectShow device is held by another client."""
        if self.isOpened():
            return False
        text = self.get_last_error().lower()
        return "already in use" in text or "resource busy" in text

    def measured_fps(self) -> float:
        """Rate at which the driver is actually delivering frames (0 until known)."""
        with self._lock:
            times = list(self._arrival_times)
        if len(times) < 2:
            return 0.0
        elapsed = times[-1] - times[0]
        if elapsed <= 0:
            return 0.0
        return (len(times) - 1) / elapsed

    def _input_args(self) -> list[str]:
        """ffmpeg input section; subclasses may substitute a non-device source."""
        args = [
            "-thread_queue_size",
            "512",
            "-rtbufsize",
            "256M",
            "-f",
            "dshow",
        ]
        if self.input_pixel_format:
            args += ["-pixel_format", self.input_pixel_format]
        args += [
            "-framerate",
            str(self.input_fps),
            "-video_size",
            f"{self.input_width}x{self.input_height}",
            "-i",
            f"video={self.device_name}",
        ]
        return args

    def _video_filter(self) -> str:
        filters: list[str] = []
        if self.width != self.input_width or self.height != self.input_height:
            filters.append(
                f"scale={self.width}:{self.height}:flags=lanczos+accurate_rnd+full_chroma_int"
            )
        filters.append("format=bgr24")
        return ",".join(filters)

    def _stderr_drain_loop(self) -> None:
        stderr = None
        try:
            process = self._process
            if process is not None:
                stderr = process.stderr
        except Exception:
            return
        if stderr is None:
            return
        try:
            while not self._reader_stop_event.is_set():
                try:
                    line = stderr.readline()
                except (ValueError, OSError):
                    break
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    self._last_error_lines.append(text)
        except (ValueError, OSError):
            return

    def get_last_error(self) -> str:
        if self._last_error_lines:
            return " | ".join(self._last_error_lines)
        return ""

    def _reader_loop(self) -> None:
        process = self._process
        stdout = self._stdout
        if process is None or stdout is None:
            return

        while not self._reader_stop_event.is_set() and process.poll() is None:
            # A fresh buffer per frame: the pipe writes straight into the
            # array that gets published, so no memcpy is needed and consumers
            # can hold the frame as long as they like.
            frame = np.empty((self.height, self.width, 3), dtype=np.uint8)
            view = memoryview(frame).cast("B")
            filled = 0
            while filled < self._frame_size and not self._reader_stop_event.is_set():
                try:
                    count = stdout.readinto(view[filled:])
                except (ValueError, OSError):
                    return
                if not count:
                    if process.poll() is not None:
                        break
                    time.sleep(0.001)
                    continue
                filled += count

            if filled != self._frame_size:
                break

            sample = _freeze_sample(frame)
            arrived_at = time.perf_counter()
            with self._frame_condition:
                previous_sample = self._freeze_sample
                is_duplicate = False
                if previous_sample is not None and previous_sample.shape == sample.shape:
                    mean_diff = float(cv2.absdiff(sample, previous_sample).mean())
                    # The driver repeats the previous picture when it has no
                    # new one (a 60Hz source sampled at ~70Hz); an identical
                    # sample grid means an identical frame.
                    is_duplicate = mean_diff == 0.0
                    if mean_diff < _FREEZE_ABSDIFF_THRESHOLD:
                        self._freeze_hold_count += 1
                    else:
                        self._freeze_hold_count = 0
                else:
                    self._freeze_hold_count = 0
                self._stream_frozen = self._freeze_hold_count >= self._freeze_hold_frames
                self._freeze_sample = sample
                self._latest_frame = frame
                self._latest_is_duplicate = is_duplicate
                self._latest_frame_seq += 1
                self._arrival_times.append(arrived_at)
                self._frame_condition.notify_all()

        with self._frame_condition:
            self._frame_condition.notify_all()

    def is_stream_frozen(self) -> bool:
        with self._lock:
            return self._stream_frozen

    def isOpened(self) -> bool:
        return self._process is not None and self._process.poll() is None and self._stdout is not None

    def read(self):
        if not self.isOpened():
            return False, None

        with self._frame_condition:
            if self._latest_frame is None or self._last_read_seq >= self._latest_frame_seq:
                self._frame_condition.wait_for(
                    lambda: self._latest_frame is not None and self._last_read_seq < self._latest_frame_seq,
                    timeout=_READ_WAIT_TIMEOUT_SEC,
                )
            if self._latest_frame is None or self._last_read_seq >= self._latest_frame_seq:
                return False, None

            self._last_read_seq = self._latest_frame_seq
            self.last_read_duplicate = self._latest_is_duplicate
            return True, self._latest_frame

    def grab(self) -> bool:
        return self.isOpened()

    def retrieve(self):
        return self.read()

    def get(self, prop_id: int) -> float:
        if prop_id == cv2.CAP_PROP_FRAME_WIDTH:
            return float(self.width)
        if prop_id == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(self.height)
        if prop_id == cv2.CAP_PROP_FPS:
            return float(self.fps)
        return 0.0

    def set(self, prop_id: int, value: float) -> bool:
        return False

    def release(self) -> None:
        process = self._process
        self._process = None
        # Ask ffmpeg to quit while the reader thread is still draining the
        # pipe: if the reader stopped first, ffmpeg would block on a full pipe
        # and never get to process the 'q', forcing the hard kill that wedges
        # the driver. The reader exits on its own when it sees EOF.
        exited = _stop_ffmpeg_process(process)
        self._reader_stop_event.set()
        if self._reader_thread is not None and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=1.0)
        self._reader_thread = None
        if self._stderr_thread is not None and self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=1.0)
        self._stderr_thread = None
        _close_ffmpeg_pipes(process)
        stdout = self._stdout
        self._stdout = None
        if stdout is not None:
            try:
                stdout.close()
            except Exception:
                pass
        with self._frame_condition:
            self._latest_frame = None
            self._frame_condition.notify_all()
        if not exited and process is not None:
            print(f"[CAPTURE] [WARN] ffmpeg pid {process.pid} did not exit; the capture device may stay busy until it does")

    def __del__(self) -> None:
        try:
            self.release()
        except Exception:
            pass


class FrameSource:
    def __init__(self, settings: dict[str, Any]):
        self.mode = settings.get("capture_mode", "camera").lower()
        self.capture = None
        self.screen = None
        self.capture_width = 0.0
        self.capture_height = 0.0
        self.capture_fps = 0.0
        self._capture_kind = str(settings.get("capture_device_kind", "")).lower()
        self._capture_device_name = str(settings.get("capture_device_name", "")).strip()
        self.camera_index = int(settings.get("camera_index", 0))
        self.settings = settings.copy()
        self._follow_browser = str(settings.get("source_profile", "hdmi_game")) == "stream_window"
        self._browser_hwnd = 0
        self.monitor_index = int(settings.get("screen_monitor_index", 1))
        self.region = settings.get("screen_region")
        self.last_open_error = ""
        self.last_rejected_override: tuple[int, int, int] | None = None
        # {(w, h): (min_fps, max_fps)} the device advertised at the last open,
        # so the UI can offer exactly those modes and nothing invented.
        self.advertised_modes: dict[tuple[int, int], tuple[float, float]] = {}

        if self._follow_browser:
            self.mode = "screen"
            self.region = None
        if self.mode == "screen":
            self.screen = mss()
            if self.region is not None and not isinstance(self.region, dict):
                x0, y0, x1, y1 = (int(value) for value in self.region[:4])
                self.region = {"left": x0, "top": y0, "width": max(1, x1 - x0), "height": max(1, y1 - y0)}

    def selectable_modes(self) -> list[tuple[int, int, int]]:
        """Discrete (w, h, fps) modes the current device advertised, for the
        MODE picker. A virtual camera serves a fixed rate per size, so no
        bandwidth filter applies to it."""
        return selectable_modes_from_ranges(
            self.advertised_modes, apply_bandwidth_ceiling=not self._is_virtual_camera_device()
        )

    def _is_capture_card_device(self) -> bool:
        if self._is_virtual_camera_device():
            return False
        # One classifier for every vendor: whatever infer_device_kind calls a
        # capture card goes through the ffmpeg dshow calibration path.
        if "capture" in self._capture_kind.lower():
            return True
        return infer_device_kind(self._capture_device_name) == "Capture Card"

    def _is_virtual_camera_device(self) -> bool:
        return "virtual" in self._capture_kind.lower() or infer_device_kind(self._capture_device_name) == "Virtual Camera"

    def _open_virtual_camera(self, device_name: str, requested_width: int, requested_height: int) -> "FFmpegRawVideoCapture | None":
        """Open another app's virtual-camera output.

        No bandwidth calibration ladder here: it is a software feed at a fixed
        rate (60 on Streaming Center/OBS), and the modes it lists are exactly
        what it will serve. Pick the largest advertised size that is not
        larger than the requested one, in the pixel format it publishes.
        """
        output = _run_ffmpeg_query(
            ["ffmpeg", "-hide_banner", "-list_options", "true", "-f", "dshow", "-i", f"video={device_name}"],
            timeout=8.0,
        )
        pixel_format: str | None = None
        modes: dict[tuple[int, int], float] = {}
        for line in output.splitlines():
            match = _RANGE_MODE_PATTERN.search(line) or _DISCRETE_MODE_PATTERN.search(line)
            if match is None:
                continue
            fmt = match.group(1)
            if pixel_format is None and not fmt.startswith("0x"):
                pixel_format = fmt
            if fmt != pixel_format:
                continue
            groups = match.groups()
            if len(groups) >= 7:
                w, h, fps = int(groups[4]), int(groups[5]), float(groups[6])
            else:
                w, h, fps = int(groups[1]), int(groups[2]), float(groups[3])
            # Virtual cameras list portrait variants too (1440x2560); keep landscape.
            if h > w:
                continue
            modes[(w, h)] = max(modes.get((w, h), 0.0), fps)
        self.advertised_modes = {resolution: (fps, fps) for resolution, fps in modes.items()}

        requested_area = max(1, requested_width * requested_height)
        candidates = sorted(
            (m for m in modes if m[0] * m[1] <= requested_area * 1.05), key=lambda m: m[0] * m[1], reverse=True
        ) or sorted(modes, key=lambda m: m[0] * m[1])
        if not candidates:
            candidates = [(requested_width, requested_height)]
            modes[candidates[0]] = 60.0
        width, height = candidates[0]
        fps = int(round(modes.get((width, height), 60.0))) or 60

        try:
            capture = FFmpegRawVideoCapture(device_name, width, height, fps, pixel_format=pixel_format)
        except Exception:
            return None
        # A virtual camera that is registered but switched off in its host app
        # opens fine and then never delivers a frame; give it a moment and
        # report that clearly instead of a generic stall.
        deadline = time.time() + _VIRTUAL_CAMERA_FIRST_FRAME_SEC
        while time.time() < deadline and capture.isOpened():
            ok, _frame = capture.read()
            if ok:
                self.settings["capture_width"] = width
                self.settings["capture_height"] = height
                self.settings["capture_fps"] = fps
                print(f"[CAPTURE] [VIRTUAL] {device_name}: {width}x{height}@{fps} {pixel_format or ''}")
                return capture
        self.last_open_error = capture.get_last_error() or (
            f"{device_name} is registered but not sending frames. Turn on the Virtual Camera "
            "output in its host app (OBS / Streaming Center / Streamlabs), then press RESCAN."
        )
        capture.release()
        return None

    def _probe_capture_card_mode(
        self, device_name: str, width: int, height: int, fallback_fps: int
    ) -> tuple[int, int, float, "FFmpegRawVideoCapture | None"]:
        """Pick a working mode from what the device advertises, requested
        mode first. When calibration verifies one, the verified capture is
        returned still streaming so the caller keeps using it (never
        probe-and-kill: see _stop_ffmpeg_process)."""
        output = _run_ffmpeg_query(
            ["ffmpeg", "-hide_banner", "-list_options", "true", "-f", "dshow", "-i", f"video={device_name}"],
            timeout=8.0,
        )
        resolution_fps_ranges = self._parse_device_modes(output) if output.strip() else {}
        self.advertised_modes = dict(resolution_fps_ranges)
        if not resolution_fps_ranges:
            # Could not enumerate modes: try the requested one directly.
            ladder = [(width, height, float(fallback_fps))]
        else:
            ladder = self._build_calibration_ladder(width, height, float(fallback_fps), resolution_fps_ranges)
        return self._calibrate_capture_mode(device_name, ladder)

    def _parse_device_modes(self, output: str) -> dict[tuple[int, int], tuple[float, float]]:
        """Parse every `pixel_format=... min/max s=...` and `s=... fps=...`
        line into {(width, height): (min_fps, max_fps)}, scoped to the bgr24
        block specifically (that's the pixel format we always request) since
        a device can advertise a narrower fps ceiling for bgr24 than for its
        other formats at the same resolution. A device can also report
        several lines for the *same* (format, resolution) pair -- e.g. a
        range line capping at 120fps plus a separate discrete line at
        144.001fps for bgr24 at 2560x1440 on the development card -- so the
        parsed range is a union across every line seen, not just the first."""
        by_format: dict[str, dict[tuple[int, int], tuple[float, float]]] = {}

        def merge(fmt: str, resolution: tuple[int, int], low_fps: float, high_fps: float) -> None:
            ranges = by_format.setdefault(fmt, {})
            existing = ranges.get(resolution)
            if existing is not None:
                low_fps, high_fps = min(existing[0], low_fps), max(existing[1], high_fps)
            ranges[resolution] = (low_fps, high_fps)

        for line in output.splitlines():
            range_match = _RANGE_MODE_PATTERN.search(line)
            if range_match is not None:
                fmt = range_match.group(1)
                min_fps = float(range_match.group(4))
                max_w, max_h = int(range_match.group(5)), int(range_match.group(6))
                max_fps = float(range_match.group(7))
                # Devices report identical min/max *resolution* per line with
                # the *frame rate* varying across the range; key on the
                # resolution and union the fps span it advertises.
                merge(fmt, (max_w, max_h), min(min_fps, max_fps), max(min_fps, max_fps))
                continue

            discrete_match = _DISCRETE_MODE_PATTERN.search(line)
            if discrete_match is not None:
                fmt = discrete_match.group(1)
                mode_w, mode_h = int(discrete_match.group(2)), int(discrete_match.group(3))
                mode_fps = float(discrete_match.group(4))
                merge(fmt, (mode_w, mode_h), mode_fps, mode_fps)

        if "bgr24" in by_format:
            return by_format["bgr24"]
        if by_format:
            # No explicit bgr24 block advertised -- fall back to whichever
            # pixel format block the device did report so calibration still
            # gets a real, device-derived candidate list, not an empty one.
            return next(iter(by_format.values()))
        return {}

    def _build_calibration_ladder(
        self,
        requested_width: int,
        requested_height: int,
        requested_fps: float,
        resolution_fps_ranges: dict[tuple[int, int], tuple[float, float]],
    ) -> list[tuple[int, int, float]]:
        ordered: list[tuple[int, int, float]] = []
        seen: set[tuple[int, int, float]] = set()

        def add(mode: tuple[int, int, float]) -> bool:
            # 144.0 and 144.001 are the same hardware mode: never probe both.
            for seen_w, seen_h, seen_fps in ordered:
                if (seen_w, seen_h) == mode[:2] and abs(seen_fps - mode[2]) <= _CALIBRATION_FPS_TOLERANCE:
                    return False
            ordered.append(mode)
            return True

        def fps_supported(resolution: tuple[int, int], fps: float) -> bool:
            low, high = resolution_fps_ranges.get(resolution, (0.0, -1.0))
            return low - _CALIBRATION_FPS_TOLERANCE <= fps <= high + _CALIBRATION_FPS_TOLERANCE

        def within_bandwidth_ceiling(resolution: tuple[int, int], fps: float) -> bool:
            raw_bytes_per_sec = resolution[0] * resolution[1] * fps * 3
            return raw_bytes_per_sec <= _BANDWIDTH_CEILING_BYTES_PER_SEC

        def rates_for(resolution: tuple[int, int]) -> list[float]:
            low, high = resolution_fps_ranges[resolution]
            rates = [
                fps
                for fps in _FPS_STEP_LADDER
                if low - _CALIBRATION_FPS_TOLERANCE <= fps <= high + _CALIBRATION_FPS_TOLERANCE
                and within_bandwidth_ceiling(resolution, fps)
            ]
            if within_bandwidth_ceiling(resolution, high) and not any(
                abs(high - fps) <= _CALIBRATION_FPS_TOLERANCE for fps in rates
            ):
                rates.append(high)
            return sorted(rates, reverse=True)

        # The configured mode is the user's intent: it goes first whenever the
        # device actually advertises it. Then the requested resolution's next
        # two rates, then other sizes so a failing native mode still degrades
        # gracefully instead of stalling startup for minutes.
        requested_resolution = (requested_width, requested_height)
        if fps_supported(requested_resolution, requested_fps) and within_bandwidth_ceiling(
            requested_resolution, requested_fps
        ):
            add((requested_width, requested_height, requested_fps))

        if requested_resolution in resolution_fps_ranges:
            added_for_requested = 0
            for fps in rates_for(requested_resolution):
                if added_for_requested >= _MAX_FPS_CANDIDATES_PER_RESOLUTION:
                    break
                if add((requested_width, requested_height, fps)):
                    added_for_requested += 1

        # Fallback sizes ordered by closeness to the requested one: sizes at or
        # below the requested area first (largest of those first), then bigger
        # ones. Requesting 2560x1440 must not fall through to 3840x2160 --
        # that only makes the card upscale and the app downscale again.
        requested_area = requested_width * requested_height

        def closeness(res: tuple[int, int]) -> tuple[int, int]:
            area = res[0] * res[1]
            return (1 if area > requested_area else 0, abs(area - requested_area))

        hd_resolutions = sorted(
            (res for res in resolution_fps_ranges if res[1] >= _MIN_AUTO_HEIGHT and res != requested_resolution),
            key=closeness,
        )
        low_resolutions = sorted(
            (res for res in resolution_fps_ranges if res[1] < _MIN_AUTO_HEIGHT and res != requested_resolution),
            key=lambda r: r[0] * r[1],
            reverse=True,
        )
        for res_w, res_h in hd_resolutions + low_resolutions:
            if len(ordered) >= _MAX_CALIBRATION_CANDIDATES:
                break

            added_for_resolution = 0
            for fps in rates_for((res_w, res_h)):
                if added_for_resolution >= _MAX_FPS_CANDIDATES_PER_RESOLUTION:
                    break
                if add((res_w, res_h, fps)):
                    added_for_resolution += 1

        return ordered

    def _calibrate_capture_mode(
        self, device_name: str, ladder: list[tuple[int, int, float]]
    ) -> tuple[int, int, float, "FFmpegRawVideoCapture | None"]:
        hd_ladder = [mode for mode in ladder if mode[1] >= _MIN_AUTO_HEIGHT]
        low_ladder = [mode for mode in ladder if mode[1] < _MIN_AUTO_HEIGHT]
        last_resort = (hd_ladder[0] if hd_ladder else None) or (ladder[-1] if ladder else (1920, 1080, 60.0))

        # Every candidate gets one strict, real hardware test, and the first
        # one that passes is handed back *still running* as the live capture.
        # Closing the verified probe and re-opening the same mode a moment
        # later was the startup race that produced starved sessions: if the
        # driver had not finished tearing the probe down, the real capture
        # became a second client and received a trickle of frames.
        for width, height, fps in hd_ladder:
            passed, frame_count, probe = self._measure_candidate(device_name, width, height, fps)
            if passed:
                return width, height, fps, probe
            if self.last_open_error and self._error_means_busy(self.last_open_error):
                # Another client owns the card. Trying more modes only stacks
                # more failed opens on it; wait once for a previous ffmpeg to
                # let go, retry the same mode, then report busy.
                _wait_for_lingering_ffmpeg()
                time.sleep(2.0)
                passed, frame_count, probe = self._measure_candidate(device_name, width, height, fps)
                if passed:
                    return width, height, fps, probe
                return (*last_resort, None)
            if frame_count == 0:
                # Opened but produced nothing at all: no signal on this mode.
                # Probing every remaining mode would stall startup for
                # minutes; hand back the preferred mode and let the capture
                # loop report the stall.
                return (*last_resort, None)

        if hd_ladder:
            return (*last_resort, None)

        for width, height, fps in low_ladder:
            passed, frame_count, probe = self._measure_candidate(device_name, width, height, fps)
            if passed:
                return width, height, fps, probe
            if frame_count == 0 or (self.last_open_error and self._error_means_busy(self.last_open_error)):
                break

        return (*last_resort, None)

    def _verify_candidate_strict(
        self, device_name: str, width: int, height: int, fps: float
    ) -> "FFmpegRawVideoCapture | None":
        passed, _, probe = self._measure_candidate(device_name, width, height, fps)
        return probe if passed else None

    def _measure_candidate(
        self, device_name: str, width: int, height: int, fps: float
    ) -> tuple[bool, int, "FFmpegRawVideoCapture | None"]:
        """Returns (passed, frames_seen, capture). The capture is only
        returned (still streaming) when the candidate passed."""
        try:
            probe = FFmpegRawVideoCapture(device_name, width, height, int(round(fps)))
        except Exception:
            return False, 0, None

        if not probe.isOpened():
            self.last_open_error = probe.get_last_error()
            probe.release()
            time.sleep(_CALIBRATION_SETTLE_SEC)
            return False, 0, None

        warmup_deadline = time.time() + _CALIBRATION_WARMUP_SEC
        warmup_frames = 0
        while time.time() < warmup_deadline:
            ok, _ = probe.read()
            if ok:
                warmup_frames += 1
            if not probe.isOpened():
                break

        start = time.time()
        frame_count = 0
        while time.time() - start < _CALIBRATION_TEST_DURATION_SEC and probe.isOpened():
            ok, frame = probe.read()
            if ok and frame is not None:
                frame_count += 1
            else:
                time.sleep(0.001)

        elapsed = max(time.time() - start, 0.001)
        achieved_fps = frame_count / elapsed
        overflowed = "too full" in probe.get_last_error()
        busy = probe.device_busy()
        # The driver may legitimately deliver fewer frames than the mode's
        # nominal rate (this card tops out near 70fps at 2560x1440 whatever
        # is requested). With passthrough output that is not a fault: the
        # test is that frames flow steadily and nothing overflowed.
        passed = probe.isOpened() and not overflowed and not busy and frame_count > 0 and achieved_fps >= min(
            fps * _CALIBRATION_MIN_FPS_RATIO, _CALIBRATION_MIN_ABSOLUTE_FPS
        )
        print(
            f"[CAPTURE] [CALIBRATE] {width}x{height}@{fps:.0f}: {achieved_fps:.1f} fps"
            f"{' OVERFLOW' if overflowed else ''}{' BUSY' if busy else ''} -> {'ok' if passed else 'reject'}"
        )
        if passed:
            return True, frame_count + warmup_frames, probe

        if busy:
            self.last_open_error = probe.get_last_error()
        probe.release()
        time.sleep(_CALIBRATION_SETTLE_SEC)
        return False, frame_count + warmup_frames, None

    def _resolve_browser_region(self) -> dict[str, int] | None:
        region = _window_region(self._browser_hwnd)
        if region is not None:
            return region
        found = _find_browser_window()
        if found is None:
            self._browser_hwnd = 0
            return None
        hwnd, region = found
        self._browser_hwnd = hwnd
        return region

    def open(self) -> bool:
        if self.mode == "screen":
            if self.screen is None:
                self.screen = mss()
            if self._follow_browser:
                region = self._resolve_browser_region()
                if region is None:
                    return False
                self.region = region
                self.capture_width = float(region["width"])
                self.capture_height = float(region["height"])
                self.capture_fps = 30.0
            elif isinstance(self.region, dict):
                self.capture_width = float(self.region.get("width", 0) or 0)
                self.capture_height = float(self.region.get("height", 0) or 0)
                self.capture_fps = max(1.0, float(self.settings.get("capture_fps", 30) or 30))
            return True

        if self.capture is not None:
            return self.capture.isOpened()

        camera_index = int(self.settings.get("camera_index", self.camera_index))
        self.capture = self._open_camera_capture(camera_index)
        if self.capture is None or not self.capture.isOpened():
            self.capture = None
            return False

        self._configure_camera_capture(self.settings)
        return True

    def _open_camera_capture(self, camera_index: int) -> cv2.VideoCapture:
        if self._is_virtual_camera_device() and self._capture_device_name:
            requested_width = int(self.settings.get("capture_width", 2560) or 2560)
            requested_height = int(self.settings.get("capture_height", 1440) or 1440)
            # A pinned MODE picks the size; the rate is whatever the virtual
            # camera publishes for that size (it serves one fixed rate).
            manual_override = self._normalize_resolution_override(self.settings.get("capture_resolution_override"))
            if manual_override is not None:
                requested_width, requested_height = manual_override[0], manual_override[1]
            capture = self._open_virtual_camera(self._capture_device_name, requested_width, requested_height)
            # Never fall through to OpenCV here: it would open the *real* card
            # by index and starve the host app the user is recording with.
            return capture

        if self._is_capture_card_device() and self._capture_device_name:
            requested_width = int(self.settings.get("capture_width", 2560) or 2560)
            requested_height = int(self.settings.get("capture_height", 1440) or 1440)
            requested_fps = int(self.settings.get("capture_fps", 144) or 144)

            manual_override = self._normalize_resolution_override(self.settings.get("capture_resolution_override"))
            if manual_override is not None:
                override_width, override_height, override_fps = manual_override
                # A manually pinned mode still has to prove itself on real
                # hardware before being trusted -- handing an unsupported
                # combination straight to ffmpeg fails several seconds into
                # the session with a raw dshow I/O error and no recovery
                # (the capture thread just dies). Verifying up front means an
                # unsupported override degrades to auto-calibration instead
                # of killing the feed outright.
                live = self._verify_candidate_strict(
                    self._capture_device_name, override_width, override_height, override_fps
                )
                if live is not None:
                    width, height, fps = override_width, override_height, max(1, override_fps)
                else:
                    self.last_rejected_override = (override_width, override_height, override_fps)
                    width, height, probed_fps, live = self._probe_capture_card_mode(
                        self._capture_device_name, requested_width, requested_height, requested_fps
                    )
                    fps = max(1, int(round(probed_fps)))
            else:
                width, height, probed_fps, live = self._probe_capture_card_mode(
                    self._capture_device_name, requested_width, requested_height, requested_fps
                )
                fps = max(1, int(round(probed_fps)))

            self.settings["capture_width"] = width
            self.settings["capture_height"] = height
            self.settings["capture_fps"] = fps
            if live is not None and live.isOpened():
                return live
            return None

        for backend in (cv2.CAP_ANY, cv2.CAP_DSHOW):
            cap = cv2.VideoCapture(camera_index, backend)
            if not cap.isOpened():
                cap.release()
                continue

            return cap

        return cv2.VideoCapture(camera_index)

    @staticmethod
    def _error_means_busy(text: str) -> bool:
        lowered = text.lower()
        return "already in use" in lowered or "resource busy" in lowered

    def open_error_message(self) -> str:
        """Human-readable reason for the last failed open, if known."""
        if self.last_open_error and self._error_means_busy(self.last_open_error):
            return (
                "Capture card is exclusive and did not open. Close OBS/Streamlabs/RECentral if they have "
                "this device, wait a second, then RESCAN. If CheatVision just restarted, the previous "
                "ffmpeg may still be releasing the card."
            )
        return self.last_open_error or ""

    def _normalize_resolution_override(self, override: Any) -> tuple[int, int, int] | None:
        if override is None:
            return None
        if isinstance(override, dict):
            width = int(override.get("width", 0) or 0)
            height = int(override.get("height", 0) or 0)
            fps = int(override.get("fps", 0) or 0)
            return (width, height, fps) if width > 0 and height > 0 and fps > 0 else None
        if isinstance(override, (list, tuple)) and len(override) >= 3:
            width = int(override[0])
            height = int(override[1])
            fps = int(override[2])
            return (width, height, fps) if width > 0 and height > 0 and fps > 0 else None
        return None

    def _configure_camera_capture(self, settings: dict[str, Any]) -> None:
        if self.capture is None:
            return

        try:
            self.capture.set(cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY)
        except AttributeError:
            pass

        requested_fps = int(settings.get("capture_fps", 144) or 144)
        override = self._normalize_resolution_override(settings.get("capture_resolution_override"))
        if override is not None:
            requested_width, requested_height, requested_fps = override
        else:
            requested_width = int(settings.get("capture_width", 2560))
            requested_height = int(settings.get("capture_height", 1440))

        # No ffmpeg probing here: this path only runs when OpenCV already
        # holds the device, and a second DirectShow client starves both.

        if requested_width > 0 and requested_height > 0:
            self.capture.set(cv2.CAP_PROP_FRAME_WIDTH, requested_width)
            self.capture.set(cv2.CAP_PROP_FRAME_HEIGHT, requested_height)
        if requested_fps > 0:
            self.capture.set(cv2.CAP_PROP_FPS, requested_fps)

        try:
            self.capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except AttributeError:
            pass

        prop_width = float(self.capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0.0)
        prop_height = float(self.capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0.0)
        prop_fps = float(self.capture.get(cv2.CAP_PROP_FPS) or 0.0)

        frame_width = 0.0
        frame_height = 0.0
        probe_times: list[float] = []
        for _ in range(8):
            ok, frame = self.capture.read()
            if not ok or frame is None:
                break
            frame_height = float(frame.shape[0])
            frame_width = float(frame.shape[1])
            probe_times.append(time.perf_counter())

        self.capture_width = frame_width or prop_width
        self.capture_height = frame_height or prop_height
        self.capture_fps = prop_fps
        if self.capture_fps <= 0.0 and len(probe_times) >= 2:
            deltas = [probe_times[i] - probe_times[i - 1] for i in range(1, len(probe_times))]
            average_delta = sum(deltas) / max(len(deltas), 1)
            if average_delta > 0:
                self.capture_fps = 1.0 / average_delta
        if self.capture_fps <= 0.0:
            self.capture_fps = max(1.0, float(settings.get("capture_fps", 144) or 144))

        if self.capture_width > 0 and self.capture_height > 0:
            self.settings["capture_width"] = int(self.capture_width)
            self.settings["capture_height"] = int(self.capture_height)
        if self.capture_fps > 0:
            self.settings["capture_fps"] = int(round(self.capture_fps))

    def read(self):
        if self.mode == "screen":
            if self.screen is None:
                self.screen = mss()
            if self.screen is None:
                return False, None

            if self._follow_browser:
                region = self._resolve_browser_region()
                if region is None:
                    return False, None
                self.region = region
                self.capture_width = float(region["width"])
                self.capture_height = float(region["height"])
                shot = self.screen.grab(region)
            elif self.region is not None:
                shot = self.screen.grab(self.region)
            else:
                monitor = self.screen.monitors[self.monitor_index]
                shot = self.screen.grab(monitor)

            frame = np.array(shot)
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
            return True, frame

        if self.capture is None:
            if not self.open():
                return False, None

        try:
            if isinstance(self.capture, FFmpegRawVideoCapture):
                return self.capture.read()
            if not self.capture.grab():
                return False, None
            return self.capture.retrieve()
        except cv2.error:
            return False, None

    def close(self) -> None:
        capture = self.capture
        self.capture = None
        if capture is not None:
            try:
                capture.release()
            except cv2.error:
                pass
            except Exception:
                pass
        self.screen = None


def infer_device_kind(label: str) -> str:
    lowered = label.lower()
    # A virtual camera is another app's *output* (Streaming Center / OBS /
    # Streamlabs re-publishing the capture card). Reading it lets that app
    # record or stream while CheatVision analyses the same picture -- the
    # only way around the card's one-client-at-a-time limit. Checked first:
    # "Streaming Center Virtual Camera" also matches "camera" below.
    virtual_keywords = ("virtual cam", "virtual camera", "virtualcam", "obs virtual", "streamlabs desktop virtual")
    # Vendor / model words that only ever appear on capture hardware. This
    # list decides which capture path a device takes; the UI always shows the
    # device's own name exactly as Windows reports it, whatever the vendor.
    capture_keywords = (
        "capture card",
        "capture",
        "grabber",
        "hdmi",
        "avermedia",
        "live gamer",
        "live streamer",
        "elgato",
        "cam link",
        "game capture",
        "hd60",
        "hd 60",
        "4k60",
        "magewell",
        "blackmagic",
        "decklink",
        "intensity pro",
        "ultrastudio",
        "ripsaw",
        "hauppauge",
        "shadowcast",
        "mirabox",
        "ezcap",
        "startech",
    )
    webcam_keywords = (
        "webcam",
        "camera",
        "c920",
        "c922",
        "brio",
        "facecam",
        "logitech",
        "razer kiyo",
        "integrated",
    )
    if any(keyword in lowered for keyword in virtual_keywords):
        return "Virtual Camera"
    if any(keyword in lowered for keyword in capture_keywords):
        return "Capture Card"
    if any(keyword in lowered for keyword in webcam_keywords):
        return "Webcam"
    return "Input"


def _list_directshow_video_names() -> list[str]:
    output = _run_ffmpeg_query(
        ["ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
        timeout=8.0,
    )
    if not output.strip():
        return []

    names: list[str] = []
    for line in output.splitlines():
        if '"' not in line:
            continue
        if "(video)" not in line and "(audio, video)" not in line:
            continue
        parts = line.split('"')
        if len(parts) < 2:
            continue
        name = parts[1].strip()
        if name and name not in names:
            names.append(name)
    return names


def ffmpeg_on_path() -> bool:
    return shutil.which("ffmpeg") is not None


def discover_directshow_devices() -> list[dict[str, str | int]]:
    """Every DirectShow video device present right now, in enumeration order.
    Nothing is invented: when ffmpeg finds no device (or is not installed)
    the single returned entry has an empty name and a label saying why, and
    the UI shows that instead of a device."""
    device_names = _list_directshow_video_names()
    devices: list[dict[str, str | int]] = []

    for index, device_name in enumerate(device_names):
        devices.append(
            {
                "label": f"{device_name} (DirectShow {index})",
                "name": device_name,
                "index": index,
                "kind": infer_device_kind(device_name),
            }
        )

    if not devices:
        reason = "No video devices found" if ffmpeg_on_path() else "ffmpeg not found on PATH (install it, then RESCAN)"
        devices.append({"label": reason, "name": "", "index": 0, "kind": "Input"})

    return devices


def pick_preferred_capture_device(devices: list[dict[str, str | int]]) -> dict[str, str | int] | None:
    # The "no devices" placeholder has no name and must never be opened.
    devices = [device for device in devices if str(device.get("name", ""))]
    capture_cards = [device for device in devices if str(device.get("kind", "")).lower() == "capture card"]
    webcams = [device for device in devices if str(device.get("kind", "")).lower() == "webcam"]

    if capture_cards:
        return capture_cards[0]
    if webcams:
        return webcams[0]
    if devices:
        return devices[0]
    return None
