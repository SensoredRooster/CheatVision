from __future__ import annotations

import threading
import time
from collections import deque

import cv2
import numpy as np
from PySide6.QtCore import QObject, Signal, Slot

from src.core.anti_cheat_pipeline import AntiCheatPipeline, CheatEvent, FrameContext
from src.core.dataset_exporter import PixelVisionDatasetExporter
from src.core.frame_source import FFmpegRawVideoCapture, FrameSource

_GATE_CHIP_FONT = cv2.FONT_HERSHEY_SIMPLEX
_GATE_CHIP_FONT_SCALE = 0.45
_GATE_CHIP_THICKNESS = 1
_GATE_CHIP_PAD = 6
_GATE_CHIP_TEXT_COLOR = (90, 220, 120)
_GATE_CHIP_BG_COLOR = (10, 16, 12)


def _downscale(frame: np.ndarray, max_w: int, max_h: int) -> np.ndarray:
    source_h, source_w = frame.shape[:2]
    if source_w <= 0 or source_h <= 0:
        return frame

    scale = min(max_w / source_w, max_h / source_h, 1.0)
    if scale >= 1.0:
        return frame

    target_w = max(1, int(source_w * scale))
    target_h = max(1, int(source_h * scale))
    return cv2.resize(frame, (target_w, target_h), interpolation=cv2.INTER_AREA)


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
    cv2.rectangle(canvas, (x1, y1), (x2, y2), (35, 55, 40), 1)
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

        if not opened:
            if getattr(self._frame_source, "_follow_browser", False):
                self.captureError.emit("No browser window found. Open Chrome/Edge with the stream visible.")
            else:
                self.captureError.emit("Failed to open capture source")
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
            backend = "CAP_FFMPEG"
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

        while not self._stop_event.is_set():
            try:
                success, frame = self._frame_source.read() if self._frame_source is not None else (False, None)
            except Exception:
                success, frame = False, None

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

            try:
                self._maybe_emit_negotiated_from_frame(frame)
                if self._ffmpeg_capture is not None:
                    is_frozen_now = self._ffmpeg_capture.is_stream_frozen()
                    if is_frozen_now != is_frozen_reported:
                        is_frozen_reported = is_frozen_now
                        self.streamFrozen.emit(is_frozen_now)

                self._frame_sequence += 1
                timestamp = time.time()
                context = FrameContext(
                    frame=frame,
                    timestamp=timestamp,
                    frame_id=self._frame_sequence,
                    source=str(self._settings.get("capture_mode", "camera")),
                    is_duplicate=False,
                )
                self._live_frame_timestamps.append(timestamp)
                self._publish_latest(context)
                self._dataset_exporter.write_frame(frame)
            except Exception as exc:
                self.captureError.emit(f"capture pipeline error: {exc!r}")
                return

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
        fps = reported if 12.0 <= reported <= 120.0 else 30.0
        self.total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self.sourceOpened.emit(width, height, fps, self.total_frames)

        frame_delay = 1.0 / fps
        finished_naturally = False

        while not self._stop_event.is_set():
            with self._seek_lock:
                seek_target = self._seek_target
                self._seek_target = None
            if seek_target is not None:
                cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, seek_target - 1))

            if self._pause_event.is_set():
                time.sleep(0.03)
                continue

            start_time = time.perf_counter()
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

            elapsed = time.perf_counter() - start_time
            time.sleep(max(0.0, frame_delay - elapsed))

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

    def __init__(self, pipeline: AntiCheatPipeline, dataset_exporter: PixelVisionDatasetExporter, capture_worker: CaptureWorker):
        super().__init__()
        self._pipeline = pipeline
        self._dataset_exporter = dataset_exporter
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

    @Slot()
    def start(self) -> None:
        self._stop_event.clear()
        while not self._stop_event.is_set():
            with self._source_lock:
                source = self._source
                capture_worker = self._capture_worker

            ctx = source.wait_for_frame(self._last_analyzed_frame_id, timeout=0.05)
            if ctx is None or ctx.frame_id == self._last_analyzed_frame_id:
                continue

            if ctx.analysis_frame is None and self._pipeline.should_analyze_frame(ctx.frame_id):
                ctx.analysis_frame = _downscale(ctx.frame, 960, 540)

            event = self._pipeline.process_frame(frame_context=ctx)
            with self._source_lock:
                self._last_analyzed_frame_id = ctx.frame_id

            telemetry = dict(self._pipeline.last_telemetry_snapshot or {})
            telemetry["flagged"] = event is not None
            self.telemetryUpdated.emit(telemetry)

            if event is not None:
                self.cheatEventDetected.emit(event)
                if source is capture_worker and self._pipeline.should_export_suspicious_clip():
                    frames = capture_worker.get_recent_frame_cache()
                    self._dataset_exporter.export_suspicious_incident_clip(frames, event.frame_id)

    def stop(self) -> None:
        self._stop_event.set()


class RenderWorker(QObject):
    """Builds display frames off the UI thread from the latest source frame."""

    frameReady = Signal(object)

    def __init__(
        self,
        pipeline: AntiCheatPipeline,
        live_overlay,
        advanced_overlay,
        source: CaptureWorker | PlaybackWorker,
        target_fps: int = 60,
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

            timeout = max(0.0, next_render_at - time.monotonic()) if next_render_at > 0.0 else frame_interval
            ctx = source.wait_for_frame(last_seen_frame_id, timeout=timeout)
            if ctx is None:
                ctx = source.get_latest_context()
            if ctx is None:
                continue
            last_seen_frame_id = ctx.frame_id

            gate_reason = self._pipeline.gate_reason()
            is_gate_live = self._pipeline.is_gate_live()
            signature = (ctx.frame_id, is_gate_live, gate_reason, revision)
            now = time.monotonic()
            if signature == last_render_signature and now < next_render_at:
                continue
            if now < next_render_at and last_render_signature is not None and revision == last_render_signature[3]:
                continue

            with self._source_lock:
                flagged_event = self._pending_flagged_event
                self._pending_flagged_event = None
                view_mode = self._view_mode
                flagged_track_ids = set(self._flagged_track_ids)
                target_w, target_h = self._target_size

            display_frame = self._pipeline.get_display_frame(ctx.frame)
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
                source_h, source_w = display_frame.shape[:2]
                if source_w > target_w or source_h > target_h:
                    scale = min(target_w / source_w, target_h / source_h, 1.0)
                    display_frame = cv2.resize(
                        display_frame,
                        (max(1, int(source_w * scale)), max(1, int(source_h * scale))),
                        interpolation=cv2.INTER_AREA,
                    )
                if not is_gate_live:
                    display_frame = _with_gate_chip(display_frame, gate_reason)
                if not display_frame.flags["C_CONTIGUOUS"]:
                    display_frame = display_frame.copy()
            except Exception as exc:
                render_error = repr(exc)
                display_frame = self._pipeline.get_display_frame(ctx.frame)
                if not is_gate_live:
                    display_frame = _with_gate_chip(display_frame, gate_reason)
                if not display_frame.flags["C_CONTIGUOUS"]:
                    display_frame = display_frame.copy()

            self.frameReady.emit(
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

            ctx = source.wait_for_frame(self._last_frame_id, timeout=frame_interval)
            if ctx is None or ctx.frame_id == self._last_frame_id:
                continue

            self._last_frame_id = ctx.frame_id

            if ctx.analysis_frame is None:
                ctx.analysis_frame = _downscale(ctx.frame, 960, 540)

            with self._source_lock:
                detection_enabled = self._detection_enabled
            if not detection_enabled:
                time.sleep(frame_interval)
                continue

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
