from __future__ import annotations

import threading
import time
from collections import deque

import cv2
import numpy as np
from PySide6.QtCore import QObject, Signal, Slot

from src.core.anti_cheat_pipeline import AntiCheatPipeline, CheatEvent, FrameContext
from src.core.dataset_exporter import PixelVisionDatasetExporter
from src.core.evidence import EvidenceRecorder
from src.core.telemetry_log import RollingTelemetryLog
from src.core.frame_source import FFmpegRawVideoCapture, FrameSource

_GATE_CHIP_FONT = cv2.FONT_HERSHEY_SIMPLEX
_GATE_CHIP_FONT_SCALE = 0.45
_GATE_CHIP_THICKNESS = 1
_GATE_CHIP_PAD = 6
# BGR, matching the rail: charcoal chip, hairline edge, muted text.
_GATE_CHIP_TEXT_COLOR = (212, 206, 200)
_GATE_CHIP_BG_COLOR = (27, 24, 21)
_GATE_CHIP_BORDER_COLOR = (54, 48, 42)


def _downscale(frame: np.ndarray, max_w: int, max_h: int) -> np.ndarray:
    source_h, source_w = frame.shape[:2]
    if source_w <= 0 or source_h <= 0:
        return frame

    scale = min(max_w / source_w, max_h / source_h, 1.0)
    if scale >= 1.0:
        return frame

    target_w = max(1, int(source_w * scale))
    target_h = max(1, int(source_h * scale))
    # INTER_LINEAR is ~6x cheaper than INTER_AREA for these 2-3x reductions
    # (measured 1.1ms vs 7ms for 1440p->960x540), and the analysers/detector
    # are insensitive to the slight extra aliasing.
    return cv2.resize(frame, (target_w, target_h), interpolation=cv2.INTER_LINEAR)


# Analysis-side telemetry only needs to reach the UI a few times a second; the
# panel itself repaints at 4Hz. Flag transitions still bypass this throttle.
_TELEMETRY_EMIT_INTERVAL_SEC = 1.0 / 20.0
# Upper bound on how often the render worker will build a display frame. The
# worker always renders the *latest* source frame, so a slower machine simply
# skips frames rather than falling behind. Rendering faster than the normal
# 60 Hz display refresh only floods the Qt event loop with paint callbacks.
_RENDER_MAX_FPS = 60
# Live feed-rate reporting and starvation detection (see CaptureWorker).
_FEED_RATE_REPORT_INTERVAL_SEC = 1.0
_STARVED_RATIO = 0.35
_STARVED_HOLD_SEC = 3.0


def _with_gate_chip(frame: np.ndarray, reason: str) -> np.ndarray:
    if frame.size == 0:
        return frame.copy()
    canvas = frame.copy()
    text = f"GATE:{reason}"
    (text_w, text_h), baseline = cv2.getTextSize(
        text,
        _GATE_CHIP_FONT,
        _GATE_CHIP_FONT_SCALE,
        _GATE_CHIP_THICKNESS,
    )
    height, width = canvas.shape[:2]
    x1 = min(_GATE_CHIP_PAD, max(0, width - 1))
    y1 = min(_GATE_CHIP_PAD, max(0, height - 1))
    x2 = min(width - 1, max(x1, x1 + text_w + _GATE_CHIP_PAD * 2))
    y2 = min(height - 1, max(y1, y1 + text_h + baseline + _GATE_CHIP_PAD * 2))
    cv2.rectangle(canvas, (x1, y1), (x2, y2), _GATE_CHIP_BG_COLOR, -1)
    cv2.rectangle(canvas, (x1, y1), (x2, y2), _GATE_CHIP_BORDER_COLOR, 1)
    text_x = min(max(x1 + _GATE_CHIP_PAD, 0), max(0, width - 1))
    text_y = min(max(y1 + _GATE_CHIP_PAD + text_h, 0), max(0, height - 1))
    cv2.putText(
        canvas,
        text,
        (text_x, text_y),
        _GATE_CHIP_FONT,
        _GATE_CHIP_FONT_SCALE,
        _GATE_CHIP_TEXT_COLOR,
        _GATE_CHIP_THICKNESS,
        cv2.LINE_AA,
    )
    return canvas


class CaptureWorker(QObject):
    """Owns the live FrameSource read loop on a dedicated thread.

    Runs a tight blocking loop, so it must live on its own QThread with no
    other queued slot calls expected while running; stop() only flips a
    threading.Event, which is safe to call from any thread.
    """

    sourceOpened = Signal(int, int, float, str)
    captureError = Signal(str)
    streamFrozen = Signal(bool)
    waitingForDevice = Signal()
    # (delivered_fps, unique_fps, starved): what the driver is really handing
    # over versus the requested mode, refreshed about once a second.
    feedRateMeasured = Signal(float, float, bool)
    # list[(width, height, fps)] the device advertised when it was opened, so
    # the MODE picker offers exactly what this device can do right now.
    deviceModesListed = Signal(object)

    def __init__(self, settings: dict, dataset_exporter: PixelVisionDatasetExporter):
        super().__init__()
        self._settings = settings
        self._dataset_exporter = dataset_exporter
        self._frame_source: FrameSource | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._frame_condition = threading.Condition(self._lock)
        self._latest_context: FrameContext | None = None
        self._frame_sequence = 0
        self._recent_frame_cache: deque[np.ndarray] = deque(maxlen=1)
        self._live_frame_timestamps: deque[float] = deque(maxlen=180)
        self._delivered_timestamps: deque[float] = deque(maxlen=240)
        self._ffmpeg_capture: FFmpegRawVideoCapture | None = None
        self._negotiated = (0, 0, 0.0)
        self._backend = ""

    def get_latest_context(self) -> FrameContext | None:
        with self._lock:
            return self._latest_context

    def get_recent_frame_cache(self) -> list[np.ndarray]:
        with self._lock:
            return list(self._recent_frame_cache)

    def wait_for_frame(self, last_frame_id: int, timeout: float = 0.05) -> FrameContext | None:
        with self._frame_condition:
            self._frame_condition.wait_for(
                lambda: (
                    self._latest_context is not None and self._latest_context.frame_id != last_frame_id
                ) or self._stop_event.is_set(),
                timeout=timeout,
            )
            return self._latest_context

    def input_mode(self) -> tuple[int, int, int] | None:
        """Mode negotiated with the device (may be larger than the preview frames)."""
        capture = self._ffmpeg_capture
        if capture is None:
            return None
        return int(capture.input_width), int(capture.input_height), int(capture.input_fps)

    def estimate_live_fps(self) -> float:
        timestamps = list(self._live_frame_timestamps)
        if len(timestamps) < 2:
            return 0.0
        elapsed = timestamps[-1] - timestamps[0]
        if elapsed <= 0:
            return 0.0
        return float(len(timestamps) - 1) / elapsed

    @Slot()
    def start(self) -> None:
        self._stop_event.clear()
        self._frame_source = FrameSource(self._settings)

        try:
            opened = self._frame_source.open()
        except Exception as exc:
            self.captureError.emit(str(exc))
            return

        # Even a failed open (card busy, pinned mode rejected) has usually
        # enumerated the device, so the picker can still show its real modes.
        try:
            self.deviceModesListed.emit(list(self._frame_source.selectable_modes()))
        except Exception:
            pass

        if not opened:
            if getattr(self._frame_source, "_follow_browser", False):
                self.captureError.emit("No browser window found. Open Chrome/Edge with the stream visible.")
            else:
                reason = ""
                try:
                    reason = self._frame_source.open_error_message()
                except Exception:
                    reason = ""
                self.captureError.emit(reason or "Failed to open capture source")
            self._frame_source = None
            return

        capture = getattr(self._frame_source, "capture", None)
        self._ffmpeg_capture = capture if isinstance(capture, FFmpegRawVideoCapture) else None
        width, height, fps = self._read_negotiated_properties(capture)
        if getattr(self._frame_source, "_follow_browser", False):
            backend = "GDI_BROWSER"
        elif getattr(self._frame_source, "mode", "") == "screen":
            backend = "MSS"
        elif isinstance(capture, FFmpegRawVideoCapture):
            is_virtual = False
            try:
                is_virtual = bool(self._frame_source._is_virtual_camera_device())
            except Exception:
                is_virtual = False
            backend = "VIRTUAL_CAM" if is_virtual else "CAP_FFMPEG"
        else:
            backend = "CAP_DSHOW"
        self._backend = backend
        self._negotiated = (width, height, fps)
        self.sourceOpened.emit(width, height, fps, backend)

        try:
            self._read_loop()
        except Exception as exc:
            self.captureError.emit(f"capture worker crashed: {exc!r}")
        finally:
            self._close_frame_source()

    def _close_frame_source(self) -> None:
        source = self._frame_source
        self._frame_source = None
        self._ffmpeg_capture = None
        if source is None:
            return
        try:
            source.close()
        except Exception:
            pass

    def _read_negotiated_properties(self, capture: object | None) -> tuple[int, int, float]:
        width = height = 0
        fps = 0.0
        if capture is not None:
            try:
                width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
            except Exception:
                pass
        source = self._frame_source
        if source is not None:
            if width <= 0:
                width = int(getattr(source, "capture_width", 0) or 0)
            if height <= 0:
                height = int(getattr(source, "capture_height", 0) or 0)
            if fps <= 0.0:
                fps = float(getattr(source, "capture_fps", 0.0) or 0.0)
        return width, height, fps

    def _publish_latest(self, context: FrameContext) -> None:
        with self._frame_condition:
            self._latest_context = context
            self._recent_frame_cache.append(context.frame)
            self._frame_condition.notify_all()

    def _clear_latest_frame(self) -> None:
        with self._frame_condition:
            self._latest_context = None
            self._recent_frame_cache.clear()
            self._frame_condition.notify_all()

    def _maybe_emit_negotiated_from_frame(self, frame: np.ndarray) -> None:
        height, width = frame.shape[:2]
        prev_w, prev_h, fps = self._negotiated
        if width <= 0 or height <= 0 or (width == prev_w and height == prev_h):
            return
        if self._frame_source is not None:
            self._frame_source.capture_width = float(width)
            self._frame_source.capture_height = float(height)
            self._frame_source.settings["capture_width"] = width
            self._frame_source.settings["capture_height"] = height
        self._negotiated = (width, height, fps)
        self.sourceOpened.emit(width, height, fps, self._backend)

    def _describe_capture_stall(self) -> str:
        capture = getattr(self._frame_source, "capture", None)
        if isinstance(capture, FFmpegRawVideoCapture):
            detail = capture.get_last_error()
            if detail:
                return f"ffmpeg capture stalled: {detail}"
            if not capture.isOpened():
                return "ffmpeg capture stalled: capture process exited unexpectedly (device may not support the negotiated resolution/fps)"
            return "ffmpeg capture stalled: no frames received from device"
        return "capture stalled: no frames received"

    def _read_loop(self) -> None:
        stall_started_at: float | None = None
        stall_timeout_seconds = 3.0
        empty_clear_seconds = 1.0
        waiting_reported = False
        is_frozen_reported = False
        next_rate_report = time.monotonic() + _FEED_RATE_REPORT_INTERVAL_SEC
        starved_since: float | None = None
        starved_reported = False

        while not self._stop_event.is_set():
            try:
                success, frame = self._frame_source.read() if self._frame_source is not None else (False, None)
            except Exception:
                success, frame = False, None

            now_mono = time.monotonic()
            if now_mono >= next_rate_report:
                next_rate_report = now_mono + _FEED_RATE_REPORT_INTERVAL_SEC
                self._report_feed_rate(now_mono)
                delivered = self._rate(self._delivered_timestamps, now_mono)
                requested = float(self._negotiated[2] or 0.0)
                # A healthy card yields at least half the requested rate (this
                # one gives ~70 of a requested 144). Far less for several
                # seconds means another client owns the device.
                is_starved = requested > 0 and 0.0 < delivered < requested * _STARVED_RATIO
                if is_starved:
                    starved_since = starved_since or now_mono
                else:
                    starved_since = None
                should_report = starved_since is not None and now_mono - starved_since >= _STARVED_HOLD_SEC
                if should_report != starved_reported:
                    starved_reported = should_report
                    self.feedRateMeasured.emit(delivered, self._rate(self._live_frame_timestamps, now_mono), should_report)

            if not success or frame is None:
                device_gone = self._ffmpeg_capture is not None and not self._ffmpeg_capture.isOpened()
                if stall_started_at is None:
                    stall_started_at = time.time()
                elapsed = time.time() - stall_started_at
                if elapsed >= empty_clear_seconds and not waiting_reported:
                    self._clear_latest_frame()
                    self.waitingForDevice.emit()
                    waiting_reported = True
                if elapsed > stall_timeout_seconds and (device_gone or self._ffmpeg_capture is None):
                    self.captureError.emit(self._describe_capture_stall())
                    return
                time.sleep(0.001)
                continue

            stall_started_at = None
            waiting_reported = False
            self._delivered_timestamps.append(now_mono)

            try:
                self._maybe_emit_negotiated_from_frame(frame)
                if self._ffmpeg_capture is not None:
                    is_frozen_now = self._ffmpeg_capture.is_stream_frozen()
                    if is_frozen_now != is_frozen_reported:
                        is_frozen_reported = is_frozen_now
                        self.streamFrozen.emit(is_frozen_now)
                    if self._ffmpeg_capture.last_read_duplicate:
                        # Driver repeated the previous picture: nothing new to
                        # analyse, render or record.
                        continue

                self._frame_sequence += 1
                timestamp = time.time()
                context = FrameContext(
                    frame=frame,
                    timestamp=timestamp,
                    frame_id=self._frame_sequence,
                    source=str(self._settings.get("capture_mode", "camera")),
                    is_duplicate=False,
                )
                self._live_frame_timestamps.append(now_mono)
                self._publish_latest(context)
                self._dataset_exporter.write_frame(frame)
            except Exception as exc:
                self.captureError.emit(f"capture pipeline error: {exc!r}")
                return

    @staticmethod
    def _rate(timestamps: deque[float], now: float, window: float = 2.0) -> float:
        recent = [t for t in timestamps if now - t <= window]
        if len(recent) < 2:
            return 0.0
        span = recent[-1] - recent[0]
        return (len(recent) - 1) / span if span > 0 else 0.0

    def _report_feed_rate(self, now: float) -> None:
        delivered = self._rate(self._delivered_timestamps, now)
        unique = self._rate(self._live_frame_timestamps, now)
        if delivered > 0:
            self.feedRateMeasured.emit(delivered, unique, False)

    def stop(self) -> None:
        self._stop_event.set()
        with self._frame_condition:
            self._frame_condition.notify_all()
        self._close_frame_source()


class PlaybackWorker(QObject):
    """Owns a mounted-VOD read loop with pause/resume/seek support."""

    sourceOpened = Signal(int, int, float, int)
    playbackFinished = Signal()
    playbackError = Signal(str)

    def __init__(self, video_path: str, fps_override: float = 0.0):
        super().__init__()
        self._video_path = video_path
        self._fps_override = fps_override
        self._lock = threading.Lock()
        self._frame_condition = threading.Condition(self._lock)
        self._latest_context: FrameContext | None = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._seek_lock = threading.Lock()
        self._seek_target: int | None = None
        self._capture: cv2.VideoCapture | None = None
        self.total_frames = 0

    def get_latest_context(self) -> FrameContext | None:
        with self._lock:
            return self._latest_context

    def wait_for_frame(self, last_frame_id: int, timeout: float = 0.05) -> FrameContext | None:
        with self._frame_condition:
            self._frame_condition.wait_for(
                lambda: (
                    self._latest_context is not None and self._latest_context.frame_id != last_frame_id
                ) or self._stop_event.is_set(),
                timeout=timeout,
            )
            return self._latest_context

    @Slot()
    def start(self) -> None:
        cap = cv2.VideoCapture(self._video_path, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(self._video_path)
        if not cap.isOpened():
            self.playbackError.emit(f"Failed to open {self._video_path}")
            return

        self._capture = cap
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        reported = self._fps_override if self._fps_override > 0 else float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        fps = reported if 12.0 <= reported <= 480.0 else 30.0
        self.total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self.sourceOpened.emit(width, height, fps, self.total_frames)

        frame_delay = 1.0 / fps
        finished_naturally = False
        # Absolute schedule: sleeping "delay minus decode time" per frame lets
        # timer granularity accumulate into drift and periodic catch-up bursts.
        next_due = time.perf_counter()

        while not self._stop_event.is_set():
            with self._seek_lock:
                seek_target = self._seek_target
                self._seek_target = None
            if seek_target is not None:
                cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, seek_target - 1))
                next_due = time.perf_counter()

            if self._pause_event.is_set():
                time.sleep(0.03)
                next_due = time.perf_counter()
                continue

            ret, frame = cap.read()
            if not ret or frame is None:
                finished_naturally = True
                break

            frame_id = int(cap.get(cv2.CAP_PROP_POS_FRAMES))
            context = FrameContext(
                frame=frame,
                timestamp=time.time(),
                frame_id=frame_id,
                source="video_playback_stream",
                is_duplicate=False,
            )
            with self._frame_condition:
                self._latest_context = context
                self._frame_condition.notify_all()

            next_due += frame_delay
            now = time.perf_counter()
            if next_due < now - frame_delay * 4:
                # Decoding fell far behind (e.g. seek or stall): resync the
                # schedule instead of racing through frames to catch up.
                next_due = now
            remaining = next_due - now
            if remaining > 0:
                time.sleep(remaining)

        cap.release()
        self._capture = None
        if finished_naturally:
            self.playbackFinished.emit()

    @Slot()
    def pause(self) -> None:
        self._pause_event.set()

    @Slot()
    def resume(self) -> None:
        self._pause_event.clear()

    @Slot(int)
    def seek(self, frame_id: int) -> None:
        target = max(0, frame_id)
        with self._seek_lock:
            self._seek_target = target
        if self._pause_event.is_set():
            self._seek_and_emit_immediate(target)

    def _seek_and_emit_immediate(self, frame_id: int) -> None:
        cap = self._capture
        if cap is None:
            return

        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, frame_id - 1))
        ret, frame = cap.read()
        if not ret or frame is None:
            return

        context = FrameContext(
            frame=frame,
            timestamp=time.time(),
            frame_id=frame_id,
            source="timeline_seek_review",
            is_duplicate=False,
        )
        with self._frame_condition:
            self._latest_context = context
            self._frame_condition.notify_all()
        with self._seek_lock:
            self._seek_target = None

    def stop(self) -> None:
        self._stop_event.set()
        with self._frame_condition:
            self._frame_condition.notify_all()
        capture = self._capture
        self._capture = None
        if capture is not None:
            try:
                capture.release()
            except Exception:
                pass


class AnalysisWorker(QObject):
    """Runs the anti-cheat pipeline against whichever source is currently bound."""

    cheatEventDetected = Signal(object)
    telemetryUpdated = Signal(dict)

    def __init__(
        self,
        pipeline: AntiCheatPipeline,
        dataset_exporter: PixelVisionDatasetExporter,
        capture_worker: CaptureWorker,
        evidence: EvidenceRecorder | None = None,
        telemetry_log: RollingTelemetryLog | None = None,
    ):
        super().__init__()
        self._pipeline = pipeline
        self._dataset_exporter = dataset_exporter
        self._evidence = evidence
        self._telemetry_log = telemetry_log
        self._source_lock = threading.Lock()
        self._capture_worker = capture_worker
        self._source: CaptureWorker | PlaybackWorker = capture_worker
        self._last_analyzed_frame_id = -1
        self._stop_event = threading.Event()

    def set_source(self, source: CaptureWorker | "PlaybackWorker") -> None:
        with self._source_lock:
            self._source = source
            if isinstance(source, CaptureWorker):
                self._capture_worker = source
            self._last_analyzed_frame_id = -1
        if self._evidence is not None:
            self._evidence.clear()

    @Slot()
    def start(self) -> None:
        self._stop_event.clear()
        last_telemetry_emit = 0.0
        last_flagged = False
        while not self._stop_event.is_set():
            with self._source_lock:
                source = self._source
                capture_worker = self._capture_worker

            ctx = source.wait_for_frame(self._last_analyzed_frame_id, timeout=0.05)
            if ctx is None or ctx.frame_id == self._last_analyzed_frame_id:
                continue

            # The scene-gate checks run on every frame, so always hand the
            # pipeline a downscaled frame -- otherwise 2 of every 3 frames get
            # three full-resolution colour conversions each.
            if ctx.analysis_frame is None:
                ctx.analysis_frame = _downscale(ctx.frame, 960, 540)
            # The analysis frame is already 960x540: the evidence buffer keeps
            # a reference to it (no extra copy or resize per frame).
            if self._evidence is not None:
                self._evidence.push_frame(ctx.analysis_frame, ctx.timestamp)

            event = self._pipeline.process_frame(frame_context=ctx)
            with self._source_lock:
                self._last_analyzed_frame_id = ctx.frame_id

            flagged = event is not None
            now = time.monotonic()
            if flagged != last_flagged or now - last_telemetry_emit >= _TELEMETRY_EMIT_INTERVAL_SEC:
                telemetry = dict(self._pipeline.last_telemetry_snapshot or {})
                telemetry["flagged"] = flagged
                telemetry["frame_id"] = int(ctx.frame_id)
                telemetry["gate"] = self._pipeline.gate_reason()
                with self._pipeline._entities_lock:
                    tracks = list(self._pipeline.last_tracked_entities)
                telemetry["tracks"] = len(tracks)
                telemetry["detector_ready"] = bool(self._pipeline.detector_ready)
                telemetry["detector_has_result"] = bool(self._pipeline.detector_has_result)
                if self._telemetry_log is not None:
                    self._telemetry_log.write(telemetry, force=flagged)
                self.telemetryUpdated.emit(telemetry)
                last_telemetry_emit = now
                last_flagged = flagged

            if event is not None:
                # Proof first, so the folder exists by the time the UI row appears.
                if self._evidence is not None:
                    try:
                        folder = self._evidence.capture(event, source_label=getattr(ctx, "source", ""))
                        event.telemetry_data["evidence_dir"] = str(folder)
                    except Exception as exc:
                        print(f"[EVIDENCE] [WARN] capture failed: {exc!r}")
                self.cheatEventDetected.emit(event)

    def stop(self) -> None:
        self._stop_event.set()
        if self._telemetry_log is not None:
            finished = self._telemetry_log.close()
            if finished is not None:
                print(f"[TELEMETRY] finished {finished.name}")
            self._telemetry_log = None


class RenderWorker(QObject):
    """Builds display frames off the UI thread from the latest source frame.

    Hand-off to the UI is a single-slot mailbox: the worker overwrites the
    latest payload and only posts ``frameReady`` when the UI has not yet
    picked up the previous one, so a busy UI thread can never accumulate a
    backlog of stale frames (which shows up as growing latency followed by a
    visible skip).
    """

    frameReady = Signal()

    def __init__(
        self,
        pipeline: AntiCheatPipeline,
        live_overlay,
        advanced_overlay,
        source: CaptureWorker | PlaybackWorker,
        target_fps: int = _RENDER_MAX_FPS,
    ):
        super().__init__()
        self._pipeline = pipeline
        self._live_overlay = live_overlay
        self._advanced_overlay = advanced_overlay
        self._source_lock = threading.Lock()
        self._source = source
        self._target_fps = max(1, int(target_fps))
        self._view_mode = "standard"
        self._flagged_track_ids: tuple[int, ...] = ()
        self._pending_flagged_event: CheatEvent | None = None
        self._target_size = (320, 180)
        self._render_revision = 0
        self._stop_event = threading.Event()
        self._mailbox_lock = threading.Lock()
        self._latest_payload: dict | None = None
        self._notify_pending = False

    def set_source(self, source: CaptureWorker | PlaybackWorker) -> None:
        with self._source_lock:
            self._source = source
            self._render_revision += 1

    def set_view_mode(self, mode: str) -> None:
        with self._source_lock:
            self._view_mode = mode
            self._render_revision += 1

    def set_flagged_track_ids(self, track_ids) -> None:
        with self._source_lock:
            self._flagged_track_ids = tuple(int(track_id) for track_id in track_ids)
            self._render_revision += 1

    def push_flagged_event(self, event: CheatEvent) -> None:
        with self._source_lock:
            self._pending_flagged_event = event
            self._render_revision += 1

    def set_target_size(self, width: int, height: int) -> None:
        with self._source_lock:
            self._target_size = (max(320, int(width)), max(180, int(height)))
            self._render_revision += 1

    def take_latest(self) -> dict | None:
        """Called on the UI thread: returns the newest payload and re-arms notification."""
        with self._mailbox_lock:
            payload = self._latest_payload
            self._latest_payload = None
            self._notify_pending = False
            return payload

    def _publish(self, payload: dict) -> None:
        with self._mailbox_lock:
            self._latest_payload = payload
            should_notify = not self._notify_pending
            self._notify_pending = True
        if should_notify:
            self.frameReady.emit()

    @Slot()
    def start(self) -> None:
        self._stop_event.clear()
        frame_interval = 1.0 / self._target_fps
        last_seen_frame_id = -1
        last_render_signature: tuple[int, bool, str, int] | None = None
        next_render_at = 0.0

        while not self._stop_event.is_set():
            with self._source_lock:
                source = self._source
                revision = self._render_revision

            now = time.monotonic()
            wait_timeout = frame_interval if now >= next_render_at else max(0.001, next_render_at - now)
            ctx = source.wait_for_frame(last_seen_frame_id, timeout=wait_timeout)
            if ctx is None:
                ctx = source.get_latest_context()
            if ctx is None:
                continue
            now = time.monotonic()
            if now < next_render_at:
                # Frame arrived ahead of the render cap: wait it out, then
                # render whatever is newest rather than this older frame.
                time.sleep(next_render_at - now)
                ctx = source.get_latest_context() or ctx
            last_seen_frame_id = ctx.frame_id

            gate_reason = self._pipeline.gate_reason()
            is_gate_live = self._pipeline.is_gate_live()
            signature = (ctx.frame_id, is_gate_live, gate_reason, revision)
            if signature == last_render_signature:
                continue

            with self._source_lock:
                flagged_event = self._pending_flagged_event
                self._pending_flagged_event = None
                view_mode = self._view_mode
                flagged_track_ids = set(self._flagged_track_ids)
                target_w, target_h = self._target_size

            # Always show the live frame: substituting the gate's held frame
            # here froze the picture and then snapped forward when the gate
            # re-opened. The gate still drives analysis and the status chip.
            display_frame = ctx.frame
            render_error: str | None = None
            try:
                if is_gate_live:
                    entities = self._pipeline.get_tracked_entities()
                    needs_overlay = flagged_event is not None or bool(entities) or view_mode != "standard"
                    if needs_overlay:
                        display_frame = self._advanced_overlay.compile_display_frame(
                            display_frame,
                            entities,
                            flagged_event,
                            mode=view_mode,
                            flagged_track_ids=flagged_track_ids,
                        )
                        flagged_id = flagged_event.telemetry_data.get("associated_track_id") if flagged_event else None
                        if entities:
                            display_frame = self._live_overlay.render_overlays(display_frame, entities, flagged_id=flagged_id)
                display_frame = self._fit_to_target(display_frame, target_w, target_h)
                if not is_gate_live:
                    display_frame = _with_gate_chip(display_frame, gate_reason)
                if not display_frame.flags["C_CONTIGUOUS"]:
                    display_frame = np.ascontiguousarray(display_frame)
            except Exception as exc:
                render_error = repr(exc)
                display_frame = self._fit_to_target(ctx.frame, target_w, target_h)
                if not is_gate_live:
                    display_frame = _with_gate_chip(display_frame, gate_reason)
                if not display_frame.flags["C_CONTIGUOUS"]:
                    display_frame = np.ascontiguousarray(display_frame)

            self._publish(
                {
                    "context": ctx,
                    "frame": display_frame,
                    "render_error": render_error,
                    "is_gate_live": is_gate_live,
                    "gate_reason": gate_reason,
                }
            )
            last_render_signature = signature
            next_render_at = time.monotonic() + frame_interval

    @staticmethod
    def _fit_to_target(frame: np.ndarray, target_w: int, target_h: int) -> np.ndarray:
        source_h, source_w = frame.shape[:2]
        if source_w <= target_w and source_h <= target_h:
            return frame
        scale = min(target_w / source_w, target_h / source_h, 1.0)
        size = (max(1, int(source_w * scale)), max(1, int(source_h * scale)))
        # The canvas is normally ~half the source width, where a bilinear
        # resample is visually equivalent to an area filter at ~1/5 the cost.
        interpolation = cv2.INTER_LINEAR if scale >= 0.4 else cv2.INTER_AREA
        return cv2.resize(frame, size, interpolation=interpolation)

    def stop(self) -> None:
        self._stop_event.set()


class DetectionWorker(QObject):
    """Runs the YOLO object detector asynchronously at a configurable frame rate."""

    def __init__(self, pipeline: AntiCheatPipeline, source: CaptureWorker | PlaybackWorker, target_fps: int = 30):
        super().__init__()
        self._pipeline = pipeline
        self._source_lock = threading.Lock()
        self._source = source
        self._target_fps = max(1, target_fps)
        self._stop_event = threading.Event()
        self._last_frame_id = -1
        self._detection_enabled = False

    def set_source(self, source: CaptureWorker | PlaybackWorker) -> None:
        with self._source_lock:
            self._source = source
            self._last_frame_id = -1

    def set_detection_enabled(self, enabled: bool) -> None:
        with self._source_lock:
            self._detection_enabled = enabled

    @Slot()
    def start(self) -> None:
        if not self._pipeline.detector_ready:
            return

        self._stop_event.clear()
        frame_interval = 1.0 / self._target_fps

        while not self._stop_event.is_set():
            with self._source_lock:
                source = self._source
                detection_enabled = self._detection_enabled

            if not detection_enabled:
                # Nothing to do in this mode; poll cheaply instead of waking
                # (and downscaling) on every captured frame.
                time.sleep(0.1)
                continue

            ctx = source.wait_for_frame(self._last_frame_id, timeout=frame_interval)
            if ctx is None or ctx.frame_id == self._last_frame_id:
                continue

            self._last_frame_id = ctx.frame_id

            if ctx.analysis_frame is None:
                ctx.analysis_frame = _downscale(ctx.frame, 960, 540)

            try:
                entities = self._pipeline.player_detector.detect_and_track(ctx.analysis_frame)
                with self._source_lock:
                    if source is not self._source:
                        continue
                self._pipeline.update_detected_entities(
                    entities, ctx.analysis_frame.shape, ctx.frame.shape
                )
            except Exception as exc:
                print(f"[DETECTION] [WARN] Detection worker error: {exc}")

            time.sleep(frame_interval)

    def stop(self) -> None:
        self._stop_event.set()
