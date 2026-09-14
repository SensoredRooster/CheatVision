from __future__ import annotations

import copy
import json
import subprocess
import time
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl, Slot
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog,
    QMainWindow,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from src.core.anti_cheat_pipeline import SOURCE_PROFILE_LABELS, AntiCheatPipeline, CheatEvent
from src.core.dataset_exporter import PixelVisionDatasetExporter
from src.core.event_logger import EventLogger
from src.core.evidence import EvidenceRecorder
from src.core.frame_source import discover_directshow_devices, pick_preferred_capture_device
from src.core.live_overlay import PixelVisionLiveOverlay
from src.ui.advanced_overlay import PixelVisionAdvancedOverlayEngine
from src.ui.control_bar import ControlBar
from src.ui.left_rail import LeftRail
from src.ui.playback_controls import PlaybackControlsBar
from src.ui.theme import APP_STYLESHEET, WARNING
from src.ui.video_canvas import VideoCanvas
from src.ui.workers import AnalysisWorker, CaptureWorker, DetectionWorker, PlaybackWorker, RenderWorker

# Cap on retained flagged-track markers so a continuous LIVE session can't grow
# this without bound. Oldest flagged track is forgotten first (FIFO).
_MAX_FLAGGED_TRACK_IDS = 500


class MainWindow(QMainWindow):
    """Composition root: wires the control bar, tool rail, canvas and playback
    controls together with the capture/analysis/playback worker threads."""

    def __init__(self, settings: dict):
        super().__init__()
        self.settings = settings
        self.setWindowTitle(str(settings.get("window_title", "CheatVision")))
        self.resize(1600, 950)
        self.setStyleSheet(APP_STYLESHEET)

        project_root = settings.get("project_root", str(Path.cwd()))
        log_dir = str(settings.get("log_directory", "logs"))
        target_resolution = (
            int(settings.get("capture_width", 2560) or 2560),
            int(settings.get("capture_height", 1440) or 1440),
        )

        self.event_logger = EventLogger(log_dir)
        self.dataset_exporter = PixelVisionDatasetExporter(target_resolution=target_resolution, project_root=project_root)
        self.evidence = EvidenceRecorder(project_root)
        facecam_roi = settings.get("facecam_roi") or None

        self.pipeline = AntiCheatPipeline(
            log_dir=log_dir,
            analysis_stride=int(settings.get("analysis_stride", 3)),
            dataset_exporter=self.dataset_exporter,
            target_resolution=target_resolution,
            player_detector_model_path=str(settings.get("player_detector_model_path", "data/models/yolov8n.onnx")),
            detection_confidence_threshold=float(settings.get("detection_confidence_threshold", 0.45)),
            detection_nms_threshold=float(settings.get("detection_nms_threshold", 0.45)),
            detection_player_class_ids=settings.get("detection_player_class_ids", []),
            detection_corroboration_margin_px=int(settings.get("detection_corroboration_margin_px", 12)),
            facecam_roi=facecam_roi,
            source_profile=str(settings.get("source_profile", "hdmi_game")),
            game_profile=str(settings.get("game_profile", "warzone")),
            stream_chat_ignore=bool(settings.get("stream_chat_ignore", True)),
        )
        self.live_overlay = PixelVisionLiveOverlay()
        self.advanced_overlay = PixelVisionAdvancedOverlayEngine(target_resolution=target_resolution)

        self._view_mode = "standard"
        self._stream_mode = "live"
        self._last_signal_paint_time = 0.0
        self._last_slider_paint_time = 0.0
        self._latest_telemetry: dict = {}
        self._event_count = 0
        self._flagged_track_ids: OrderedDict[int, None] = OrderedDict()
        self._analyze_display_anyway = False
        self._tools_panel_visible = True
        self._tools_panel_width = 268
        self._is_stream_frozen = False
        self._feed_starved = False
        self._current_device_name = ""
        self._current_device: dict | None = None
        self._known_devices: list[dict] = []

        self._capture_thread: QThread | None = None
        self._capture_worker: CaptureWorker | None = None
        self._playback_thread: QThread | None = None
        self._playback_worker: PlaybackWorker | None = None
        self._analysis_thread: QThread | None = None
        self._analysis_worker: AnalysisWorker | None = None
        self._detection_thread: QThread | None = None
        self._detection_worker: DetectionWorker | None = None
        self._render_thread: QThread | None = None
        self._render_worker: RenderWorker | None = None

        self._build_ui()
        self._auto_start_capture()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.control_bar = ControlBar(self)
        self.control_bar.mountVodRequested.connect(self._on_mount_vod_requested)
        self.control_bar.viewModeChanged.connect(self._on_view_mode_changed)
        self.control_bar.rescanDevicesRequested.connect(self._on_rescan_devices_requested)
        self.control_bar.captureModeChanged.connect(self._on_capture_mode_changed)
        self.control_bar.toolsPanelToggled.connect(self._on_tools_panel_toggled)
        layout.addWidget(self.control_bar, 0)
        self.status_label = self.control_bar.status_label

        self.body_splitter = QSplitter(Qt.Horizontal, self)
        self.body_splitter.setHandleWidth(1)

        self.left_rail = LeftRail(self)
        self.left_rail.analyzeToggled.connect(self._on_analyze_display_toggled)
        self.left_rail.recordBaselineToggled.connect(self._on_record_baseline_toggled)
        self.left_rail.sourceSelected.connect(self._on_source_selected)
        self.left_rail.captureModeSelected.connect(self._on_capture_mode_changed)
        self.left_rail.incident_table.seekRequested.connect(self._on_seek_requested)
        self.left_rail.incident_table.incidentActivated.connect(self._on_incident_activated)
        self.body_splitter.addWidget(self.left_rail)

        self.video_canvas = VideoCanvas(self)
        self.body_splitter.addWidget(self.video_canvas)

        self.body_splitter.setCollapsible(0, True)
        self.body_splitter.setCollapsible(1, False)
        self.body_splitter.setStretchFactor(0, 0)
        self.body_splitter.setStretchFactor(1, 1)
        self.body_splitter.setSizes([268, 1300])

        layout.addWidget(self.body_splitter, 1)

        self.playback_controls = PlaybackControlsBar(self)
        self.playback_controls.playPauseToggled.connect(self._on_play_pause_toggled)
        self.playback_controls.seekRequested.connect(self._on_seek_requested)
        self.playback_controls.hide()
        layout.addWidget(self.playback_controls, 0)

        self.setCentralWidget(central)

        self.control_bar.set_stream_mode(self._stream_mode)
        self.left_rail.set_mode_picker_enabled(self._stream_mode == "live")
        self.left_rail.set_source_profile(str(self.settings.get("source_profile", "hdmi_game")))
        self._update_signal_card()

    # ------------------------------------------------------------------
    # Startup / device lifecycle
    # ------------------------------------------------------------------
    def _auto_start_capture(self) -> None:
        devices = discover_directshow_devices()
        preferred = self._choose_startup_device(devices)
        self._known_devices = devices
        self.left_rail.set_devices(devices, str(preferred.get("name", "")) if preferred else "", str(self.settings.get("source_profile", "hdmi_game")))

        self._capture_worker, self._capture_thread = self._make_capture_worker(preferred)

        self._analysis_worker = AnalysisWorker(self.pipeline, self.dataset_exporter, self._capture_worker, evidence=self.evidence)
        self._analysis_thread = QThread(self)
        self._analysis_worker.moveToThread(self._analysis_thread)
        self._analysis_worker.cheatEventDetected.connect(self._on_cheat_event_detected)
        self._analysis_worker.telemetryUpdated.connect(self._on_telemetry_updated)
        self._analysis_thread.started.connect(self._analysis_worker.start)
        self._analysis_thread.start()

        detection_fps = int(self.settings.get("detection_fps", 30))
        self._detection_worker = DetectionWorker(self.pipeline, self._capture_worker, target_fps=detection_fps)
        self._detection_thread = QThread(self)
        self._detection_worker.moveToThread(self._detection_thread)
        self._detection_thread.started.connect(self._detection_worker.start)
        self._detection_thread.start()
        self._update_detection_enabled()

        self._render_worker = RenderWorker(
            self.pipeline,
            self.live_overlay,
            self.advanced_overlay,
            self._capture_worker,
        )
        self._render_thread = QThread(self)
        self._render_worker.moveToThread(self._render_thread)
        self._render_worker.frameReady.connect(self._on_rendered_frame)
        self._render_thread.started.connect(self._render_worker.start)
        self._render_worker.set_view_mode(self._view_mode)
        self._render_worker.set_flagged_track_ids(())
        # The canvas has no real geometry yet (we're still in __init__, before
        # show()); it reports its size as soon as it is laid out.
        self._render_worker.set_target_size(self.video_canvas.width(), self.video_canvas.height())
        self.video_canvas.viewportResized.connect(self._on_canvas_resized)
        self._render_thread.start()

        self._wire_capture_worker()
        self._launch_capture(preferred)

    def _make_capture_worker(self, device: dict | None) -> tuple[CaptureWorker, QThread]:
        settings = copy.deepcopy(self.settings)
        settings["capture_device_name"] = str(device.get("name", "")) if device else ""
        settings["capture_device_kind"] = str(device.get("kind", "")) if device else ""
        settings["camera_index"] = int(device.get("index", 0)) if device else 0
        self._current_device_name = settings["capture_device_name"]
        self._current_device = device

        worker = CaptureWorker(settings, self.dataset_exporter)
        thread = QThread(self)
        worker.moveToThread(thread)
        return worker, thread

    def _wire_capture_worker(self) -> None:
        self._capture_worker.sourceOpened.connect(self._on_capture_source_opened)
        self._capture_worker.captureError.connect(self._on_capture_error)
        self._capture_worker.streamFrozen.connect(self._on_capture_stream_frozen)
        self._capture_worker.waitingForDevice.connect(self._on_waiting_for_capture)
        self._capture_worker.feedRateMeasured.connect(self._on_feed_rate_measured)
        self._capture_worker.deviceModesListed.connect(self._on_device_modes_listed)
        self._capture_thread.started.connect(self._capture_worker.start)

    def _launch_capture(self, device: dict | None) -> None:
        browser = str(self.settings.get("source_profile", "hdmi_game")) == "stream_window"
        if browser:
            self.video_canvas.set_idle_text("Waiting for browser window...")
            self.status_label.setText("Starting capture: browser window")
            self._capture_thread.start()
        elif device:
            self.video_canvas.set_idle_text("Waiting for capture device...")
            self.status_label.setText(f"Starting capture: {device.get('label', 'device')}")
            self._capture_thread.start()
        else:
            self.video_canvas.set_idle_text("No capture device detected — Mount a gameplay recording to begin")
            self.status_label.setText("No capture device detected")

    def _stop_baseline_safe(self) -> None:
        try:
            self.dataset_exporter.stop_clean_baseline_mode()
        except Exception:
            pass
        self.left_rail.set_recording_baseline(False, self._stream_mode)
        self.control_bar.mount_vod_btn.setEnabled(True)

    def _teardown_capture(self) -> None:
        self._stop_baseline_safe()
        if self._capture_worker is not None:
            self._capture_worker.stop()
        if self._capture_thread is not None:
            self._capture_thread.quit()
            self._capture_thread.wait(5000)
        self._capture_worker = None
        self._capture_thread = None
        self.video_canvas.clear_frame()

    def _teardown_playback(self) -> None:
        if self._playback_worker is not None:
            self._playback_worker.stop()
        if self._playback_thread is not None:
            self._playback_thread.quit()
            self._playback_thread.wait(2000)
        self._playback_worker = None
        self._playback_thread = None
        self.playback_controls.hide()

    # ------------------------------------------------------------------
    # Control bar actions
    # ------------------------------------------------------------------
    def _on_mount_vod_requested(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Mount Gameplay VOD", "", "Video Files (*.mp4 *.mkv *.avi)")
        if not path:
            return

        self._teardown_capture()
        self._teardown_playback()

        fps_override = float(self.settings.get("playback_fps", 0) or 0)
        self._playback_worker = PlaybackWorker(path, fps_override=fps_override)
        self._playback_thread = QThread(self)
        self._playback_worker.moveToThread(self._playback_thread)
        self._playback_worker.sourceOpened.connect(self._on_playback_source_opened)
        self._playback_worker.playbackFinished.connect(self._on_playback_finished)
        self._playback_worker.playbackError.connect(self._on_playback_error)
        self._playback_thread.started.connect(self._playback_worker.start)

        self._analysis_worker.set_source(self._playback_worker)
        self.pipeline.reset_stream_state()
        self._detection_worker.set_source(self._playback_worker)
        if self._render_worker is not None:
            self._render_worker.set_source(self._playback_worker)
        self._stream_mode = "vod"
        self._is_stream_frozen = False
        self.pipeline.set_stream_frozen(False)
        self.control_bar.set_stream_mode(self._stream_mode)
        self.left_rail.set_mode_picker_enabled(self._stream_mode == "live")
        if str(self.settings.get("source_profile", "hdmi_game")) == "hdmi_game":
            self._apply_source_profile("stream_window")
        self._update_signal_card()

        self._mounted_vod_name = Path(path).name
        self._flagged_track_ids = OrderedDict()
        if self._render_worker is not None:
            self._render_worker.set_flagged_track_ids(())
        self._update_detection_enabled()
        self.left_rail.set_recording_baseline(self.dataset_exporter.is_recording_baseline, "vod")

        self.playback_controls.show()
        self.playback_controls.reset_play_state()
        self.video_canvas.set_idle_text("Loading VOD...")
        self.status_label.setText(f"Mounted VOD: {self._mounted_vod_name}")
        self._playback_thread.start()

    def _on_view_mode_changed(self, mode: str) -> None:
        self._view_mode = mode
        if self._render_worker is not None:
            self._render_worker.set_view_mode(mode)
        self.status_label.setText(self._status_text_with_mode())

    def _on_record_baseline_toggled(self) -> None:
        if self.dataset_exporter.is_recording_baseline:
            self.dataset_exporter.stop_clean_baseline_mode()
            self.left_rail.set_recording_baseline(False, self._stream_mode)
            self.control_bar.mount_vod_btn.setEnabled(True)
            self.status_label.setText(self._status_text_with_mode())
            self._update_signal_card()
        else:
            output_path = self.dataset_exporter.start_clean_baseline_mode()
            self.left_rail.set_recording_baseline(True, self._stream_mode)
            self.control_bar.mount_vod_btn.setEnabled(False)
            self.status_label.setText(f"SAMPLING BASELINE · {output_path}")
            self._update_signal_card()

    def _restart_capture(self, device: dict | None) -> None:
        self._teardown_playback()
        self._teardown_capture()

        self._capture_worker, self._capture_thread = self._make_capture_worker(device)
        self._wire_capture_worker()
        self.pipeline.reset_stream_state()
        self._analysis_worker.set_source(self._capture_worker)
        self._detection_worker.set_source(self._capture_worker)
        if self._render_worker is not None:
            self._render_worker.set_source(self._capture_worker)
        self._stream_mode = "live"
        self._is_stream_frozen = False
        self.pipeline.set_stream_frozen(False)
        self.control_bar.set_stream_mode(self._stream_mode)
        self.left_rail.set_mode_picker_enabled(self._stream_mode == "live")
        self._update_signal_card()
        self._flagged_track_ids = OrderedDict()
        if self._render_worker is not None:
            self._render_worker.set_flagged_track_ids(())
        self._update_detection_enabled()
        self.left_rail.set_recording_baseline(self.dataset_exporter.is_recording_baseline, "live")
        self._launch_capture(device)

    def _on_rescan_devices_requested(self) -> None:
        devices = discover_directshow_devices()
        self._known_devices = devices
        preferred = self._choose_startup_device(devices)
        preferred_name = str(preferred.get("name", "")) if preferred else ""
        self.left_rail.set_devices(devices, preferred_name, str(self.settings.get("source_profile", "hdmi_game")))

        capture_alive = self._capture_thread is not None and self._capture_thread.isRunning()
        if preferred_name == self._current_device_name and capture_alive:
            self.status_label.setText("RESCAN: no device change detected")
            return

        self._restart_capture(preferred)

    def _choose_startup_device(self, devices: list[dict]) -> dict | None:
        """The user's saved pick if it is present, else the best available."""
        wanted = str(self.settings.get("capture_device_name", "") or "").strip().lower()
        if wanted:
            for device in devices:
                if str(device.get("name", "")).strip().lower() == wanted:
                    return device
        return pick_preferred_capture_device(devices)

    def _on_source_selected(self, device_name: str, profile: str) -> None:
        """SOURCE selector: a (device, profile) pair. Apply both, restart capture once."""
        device = next((d for d in self._known_devices if str(d.get("name", "")) == device_name), None)
        if device is None:
            return
        previous_profile = str(self.settings.get("source_profile", "hdmi_game"))
        if profile != previous_profile:
            self.settings["source_profile"] = profile
            self.pipeline.set_source_profile(profile)
            self._persist_setting("source_profile", profile)
            self._update_signal_card()
        self.settings["capture_device_name"] = device_name
        self.settings["capture_device_kind"] = str(device.get("kind", ""))
        self._persist_setting("capture_device_name", device_name)
        self._persist_setting("capture_device_kind", str(device.get("kind", "")))
        capture_alive = self._capture_thread is not None and self._capture_thread.isRunning()
        if device_name == self._current_device_name and profile == previous_profile and capture_alive:
            return
        if self._stream_mode != "live" and device_name == self._current_device_name:
            # Reviewing a VOD: the profile change applies to the pipeline; no capture restart.
            self.status_label.setText(self._status_text_with_mode())
            return
        self.status_label.setText(f"Switching source: {device.get('label', device_name)} · {SOURCE_PROFILE_LABELS.get(profile, profile)}")
        self._restart_capture(device)

    def _persist_setting(self, key: str, value) -> None:
        """Write one key back to config/settings.json so the choice survives restarts."""
        try:
            path = Path(self.settings.get("project_root", str(Path.cwd()))) / "config" / "settings.json"
            data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except Exception as exc:
            print(f"[SETTINGS] [WARN] could not save {key}: {exc}")

    def _on_capture_mode_changed(self, width: int, height: int, fps: int) -> None:
        if width > 0 and height > 0 and fps > 0:
            override = {"width": int(width), "height": int(height), "fps": int(fps)}
            self.settings["capture_resolution_override"] = override
            self._persist_setting("capture_resolution_override", override)
            message = f"Manual capture mode selected: {width}x{height} @ {fps}fps"
        else:
            self.settings.pop("capture_resolution_override", None)
            self._persist_setting("capture_resolution_override", None)
            message = "Capture mode set to AUTO (auto-calibration)"
        # Both pickers (rail MODE live, top-bar RES/FPS on VOD) write the same pin.
        self.left_rail.set_pinned_mode(self._pinned_capture_mode())

        capture_alive = self._capture_thread is not None and self._capture_thread.isRunning()
        if not capture_alive:
            self.status_label.setText(message)
            return

        self.status_label.setText(f"{message} -- restarting capture")
        self._restart_capture(self._current_device)

    def _on_play_pause_toggled(self) -> None:
        if self._playback_worker is None:
            return
        if self.playback_controls.is_paused():
            self._playback_worker.pause()
        else:
            self._playback_worker.resume()

    def _on_seek_requested(self, frame_id: int) -> None:
        if self._playback_worker is not None:
            self._playback_worker.seek(frame_id)

    def _on_incident_activated(self, event: CheatEvent) -> None:
        """Open the incident's proof folder in Explorer (snapshot.png, clip.mp4, event.json)."""
        folder = (event.telemetry_data or {}).get("evidence_dir")
        if not folder or not Path(folder).is_dir():
            self.status_label.setText("No evidence folder recorded for this incident")
            return
        snapshot = Path(folder) / "snapshot.png"
        # Select the snapshot if it has been written yet; otherwise just open the folder.
        if snapshot.is_file():
            subprocess.Popen(["explorer", "/select,", str(snapshot)])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        self.status_label.setText(f"Opened evidence: {Path(folder).name}")

    def _on_analyze_display_toggled(self, checked: bool) -> None:
        self._analyze_display_anyway = checked
        self._update_detection_enabled()
        self._update_signal_card()

    def _on_tools_panel_toggled(self, visible: bool) -> None:
        self._tools_panel_visible = bool(visible)
        if self.left_rail is None:
            return
        if visible:
            self.left_rail.show()
            self.left_rail.setMinimumWidth(self._tools_panel_width)
            self.left_rail.setMaximumWidth(self._tools_panel_width)
        else:
            self._tools_panel_width = max(268, self.left_rail.width(), self._tools_panel_width)
            self.left_rail.setMinimumWidth(0)
            self.left_rail.setMaximumWidth(16777215)
            self.left_rail.hide()
        sizes = self.body_splitter.sizes()
        if len(sizes) != 2:
            return
        total = max(sum(sizes), self.width())
        if visible:
            self.body_splitter.setSizes([self._tools_panel_width, max(320, total - self._tools_panel_width)])
        else:
            self.body_splitter.setSizes([0, max(320, total)])

    def _apply_source_profile(self, profile: str) -> None:
        self.settings["source_profile"] = profile
        self.pipeline.set_source_profile(profile)
        self.left_rail.set_source_profile(profile)
        self.status_label.setText(self._status_text_with_mode())
        self._update_signal_card()

    def _update_detection_enabled(self) -> None:
        if self._detection_worker is None:
            return
        enabled = (self._stream_mode == "vod") or self._analyze_display_anyway
        self._detection_worker.set_detection_enabled(enabled)

    # ------------------------------------------------------------------
    # Worker signal handlers
    # ------------------------------------------------------------------
    @Slot(int, int, float, str)
    def _on_capture_source_opened(self, width: int, height: int, fps: float, backend: str) -> None:
        self._stream_mode = "live"
        self._is_stream_frozen = False
        if self._feed_starved:
            self._feed_starved = False
            self.status_label.setStyleSheet("")
        self.pipeline.set_stream_frozen(False)
        self.control_bar.set_stream_mode(self._stream_mode)
        self.left_rail.set_mode_picker_enabled(self._stream_mode == "live")
        self._update_signal_card()
        # The requested mode (settings.json / manual pick) is deliberately NOT
        # overwritten with what was negotiated: a browser-window or degraded
        # session used to leak its geometry back into the "requested" values,
        # so the next HDMI calibration asked for e.g. 3456x1408@30, found it
        # unadvertised, and fell through to the largest mode on the card (4K).
        self.pipeline.update_target_resolution(width, height)
        self.dataset_exporter.set_target_resolution(width, height)
        input_mode = self._capture_worker.input_mode() if self._capture_worker is not None else None
        override = self.settings.get("capture_resolution_override")
        override_rejected = False
        if override is not None:
            wanted = (int(override.get("width", 0)), int(override.get("height", 0)), int(override.get("fps", 0)))
            # Compare against the mode actually negotiated with the device;
            # the preview may legitimately be a downscale of it (4K -> 1440p).
            got = input_mode if input_mode is not None else (int(width), int(height), int(round(fps)))
            override_rejected = wanted != got
        self._live_status_text = f"LIVE · {width}×{height} @ {fps:.0f} · {backend}"
        if input_mode is not None and (input_mode[0], input_mode[1]) != (int(width), int(height)):
            self._live_status_text = (
                f"LIVE · {input_mode[0]}×{input_mode[1]} @ {fps:.0f} → {width}×{height} preview · {backend}"
            )
        if override_rejected:
            self._live_status_text += (
                f" · ⚠ manual {override['width']}×{override['height']}@{override['fps']} not supported, auto-calibrated instead"
            )
        self.status_label.setText(self._status_text_with_mode())
        if str(self.settings.get("source_profile", "hdmi_game")) == "stream_window":
            device_label = "Browser window"
        else:
            device_label = (
                self._current_device.get("label", self._current_device_name) if self._current_device else self._current_device_name
            )
        low_mode = int(height) < 720
        self.left_rail.set_source(
            device_label or "Capture device",
            f"{width}×{height} @ {fps:.0f}",
            backend,
            low_mode=low_mode,
        )
        self._update_signal_card()
        if low_mode:
            self._live_status_text += " · ⚠ low mode"
            self.status_label.setText(self._status_text_with_mode())
            print(f"[CAPTURE] [LOW MODE] Source opened at {width}x{height} @ {fps:.0f}fps ({backend})")
        else:
            print(f"[CAPTURE] [SUCCESS] Source opened at {width}x{height} @ {fps:.0f}fps ({backend})")

    @Slot(str)
    def _on_capture_error(self, message: str) -> None:
        self.video_canvas.clear_frame()
        self.status_label.setText(f"CAPTURE ERROR: {message}")
        self.video_canvas.set_idle_text(f"Capture error: {message}")

    @Slot(bool)
    def _on_capture_stream_frozen(self, is_frozen: bool) -> None:
        self._is_stream_frozen = is_frozen
        self.pipeline.set_stream_frozen(is_frozen)
        if is_frozen:
            self.status_label.setText(
                f"{self._current_base_status_text()} · ⚠ NO PIXEL CHANGE DETECTED (signal or source may be frozen)"
            )
            self.status_label.setStyleSheet(f"color: {WARNING};")
        else:
            self.status_label.setStyleSheet("")
            self.status_label.setText(self._status_text_with_mode())
        self._update_signal_card()

    @Slot()
    def _on_waiting_for_capture(self) -> None:
        self.video_canvas.clear_frame()
        self.video_canvas.set_idle_text("Waiting for capture device")
        self.status_label.setText("Waiting for capture device")

    @Slot(float, float, bool)
    @Slot(object)
    def _on_device_modes_listed(self, modes) -> None:
        """Fresh from this open: only what the device advertised goes in the MODE picker."""
        self.left_rail.set_capture_modes(list(modes or []), self._pinned_capture_mode())

    def _pinned_capture_mode(self) -> tuple[int, int, int] | None:
        override = self.settings.get("capture_resolution_override")
        if not isinstance(override, dict):
            return None
        mode = (
            int(override.get("width", 0) or 0),
            int(override.get("height", 0) or 0),
            int(override.get("fps", 0) or 0),
        )
        return mode if all(value > 0 for value in mode) else None

    def _on_feed_rate_measured(self, delivered_fps: float, unique_fps: float, starved: bool) -> None:
        self.left_rail.set_feed_rate(delivered_fps, unique_fps, starved=starved)
        if unique_fps > 0 and not self.dataset_exporter.is_recording_baseline:
            # Only new pictures are recorded, so the file must be stamped with
            # the unique-picture rate or it plays back too fast/slow.
            self.dataset_exporter.set_baseline_fps(round(unique_fps))
        if starved and not self._feed_starved:
            self._feed_starved = True
            requested = int(self.settings.get("capture_fps", 0) or 0)
            self.status_label.setText(
                f"{self._current_base_status_text()} · ⚠ CAPTURE CARD DELIVERING ONLY {delivered_fps:.0f} FPS"
                f"{f' OF {requested}' if requested else ''} — another app (OBS/Streamlabs/RECentral) is probably using it"
            )
            self.status_label.setStyleSheet(f"color: {WARNING};")
        elif not starved and self._feed_starved:
            self._feed_starved = False
            self.status_label.setStyleSheet("")
            self.status_label.setText(self._status_text_with_mode())

    @Slot(int, int, float, int)
    def _on_playback_source_opened(self, width: int, height: int, fps: float, total_frames: int) -> None:
        self.pipeline.update_target_resolution(width, height)
        self.playback_controls.set_total_frames(total_frames)
        filename = getattr(self, "_mounted_vod_name", "VOD")
        self._vod_status_text = f"VOD · {filename} · {width}×{height} @ {fps:.0f}"
        self.status_label.setText(self._status_text_with_mode())
        self.left_rail.set_source(
            filename,
            f"{width}×{height} @ {fps:.0f}",
            "VOD",
            low_mode=int(height) < 720,
        )
        self._update_signal_card()

    @Slot()
    def _on_playback_finished(self) -> None:
        filename = getattr(self, "_mounted_vod_name", "VOD")
        self._vod_status_text = f"VOD · {filename} · finished"
        self.status_label.setText(self._status_text_with_mode())

    @Slot(str)
    def _on_playback_error(self, message: str) -> None:
        self.status_label.setText(f"PLAYBACK ERROR: {message}")

    def _on_cheat_event_detected(self, event: CheatEvent) -> None:
        self._event_count += 1
        self.left_rail.add_incident(event)

        associated_track_id = event.telemetry_data.get("associated_track_id")
        if associated_track_id is not None:
            self._flagged_track_ids[associated_track_id] = None
            self._flagged_track_ids.move_to_end(associated_track_id)
            if len(self._flagged_track_ids) > _MAX_FLAGGED_TRACK_IDS:
                self._flagged_track_ids.popitem(last=False)
            if self._render_worker is not None:
                self._render_worker.set_flagged_track_ids(self._flagged_track_ids.keys())
        if self._render_worker is not None:
            self._render_worker.push_flagged_event(event)

    def _on_telemetry_updated(self, telemetry: dict) -> None:
        self._latest_telemetry = telemetry
        now = time.monotonic()
        if now - self._last_signal_paint_time < (1.0 / 4.0):
            return
        self._last_signal_paint_time = now
        self._update_signal_card()

    def _update_signal_card(self) -> None:
        telemetry = self._latest_telemetry
        straightness = float(telemetry.get("straightness") or 0.0)
        tremor = float(telemetry.get("tremor_variance") or 0.0)
        self.left_rail.set_signal(frozen=self._is_stream_frozen, straightness=straightness, tremor=tremor)
        yolo_on = (self._stream_mode == "vod") or self._analyze_display_anyway
        tracks = 0
        try:
            tracks = len(self.pipeline.last_tracked_entities)
        except Exception:
            tracks = 0
        gate = self.pipeline.gate_reason()
        self.left_rail.set_detect(yolo_on=yolo_on, tracks=tracks, gate=gate)
        profile = SOURCE_PROFILE_LABELS.get(
            str(self.settings.get("source_profile", "hdmi_game")),
            "HDMI GAME",
        )
        ignore_count = int(telemetry.get("ignore_rect_count") or getattr(self.pipeline, "ignore_rect_count", 0) or 0)
        baseline = "rec" if self.dataset_exporter.is_recording_baseline else "idle"
        self.left_rail.set_profile(profile, ignore_count, baseline)

    # ------------------------------------------------------------------
    # Status text helpers
    # ------------------------------------------------------------------
    def _current_base_status_text(self) -> str:
        if self._stream_mode == "vod":
            return getattr(self, "_vod_status_text", "VOD")
        return getattr(self, "_live_status_text", "LIVE")

    def _status_mode_suffix(self) -> str:
        return {
            "standard": "STANDARD",
            "heatmap": "HEATMAP",
            "flagged_only": "FLAGGED",
        }.get(self._view_mode, self._view_mode.upper())

    def _profile_status_name(self) -> str:
        profile = str(self.settings.get("source_profile", "hdmi_game"))
        return SOURCE_PROFILE_LABELS.get(profile, profile.upper())

    def _status_text_with_mode(self) -> str:
        return f"{self._current_base_status_text()} · {self._profile_status_name()} · {self._status_mode_suffix()}"

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    @Slot()
    def _on_rendered_frame(self) -> None:
        if self._render_worker is None:
            return
        payload = self._render_worker.take_latest()
        if payload is None:
            return
        ctx = payload.get("context")
        display_frame = payload.get("frame")
        render_error = payload.get("render_error")
        if ctx is None or display_frame is None:
            return
        if self._stream_mode == "vod":
            now = time.monotonic()
            if now - self._last_slider_paint_time >= (1.0 / 30.0):
                self._last_slider_paint_time = now
                self.playback_controls.set_current_frame(ctx.frame_id)
        try:
            self.video_canvas.set_frame(ctx, display_frame)
        except Exception as exc:
            render_error = f"canvas update failed: {exc!r}"
        base_text = self._current_base_status_text()
        mode_text = self._status_text_with_mode()

        if render_error is not None:
            self.status_label.setText(f"{base_text} -- ⚠ render error (frame {ctx.frame_id}): {render_error}")
        elif (
            self._stream_mode == "live"
            and not self._feed_starved
            and self.status_label.text() != mode_text
            and "NO PIXEL CHANGE" not in self.status_label.text()
        ):
            self.status_label.setText(mode_text)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    def closeEvent(self, event: QCloseEvent) -> None:
        if self._render_worker is not None:
            self._render_worker.stop()
        if self._render_thread is not None:
            self._render_thread.quit()
            self._render_thread.wait(2000)
        self._stop_baseline_safe()
        self._teardown_capture()
        self._teardown_playback()
        # Let an in-flight recording write its mp4 trailer before exit.
        self.dataset_exporter.wait_for_pending_writes(10.0)
        if self._detection_worker is not None:
            self._detection_worker.stop()
        if self._detection_thread is not None:
            self._detection_thread.quit()
            self._detection_thread.wait(2000)
        if self._analysis_thread is not None:
            if self._analysis_worker is not None:
                self._analysis_worker.stop()
            self._analysis_thread.quit()
            self._analysis_thread.wait(2000)
        self.event_logger.log("Application closed")
        try:
            self.evidence.close()
        except Exception:
            pass
        super().closeEvent(event)

    @Slot(int, int)
    def _on_canvas_resized(self, width: int, height: int) -> None:
        if self._render_worker is not None:
            self._render_worker.set_target_size(width, height)
