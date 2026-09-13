from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QWidget,
)

RESOLUTION_PRESETS: tuple[tuple[str, int, int], ...] = (
    ("AUTO", 0, 0),
    ("3840x2160", 3840, 2160),
    ("2560x1440", 2560, 1440),
    ("1920x1080", 1920, 1080),
    ("1600x900", 1600, 900),
    ("1280x720", 1280, 720),
    ("854x480", 854, 480),
    ("640x480", 640, 480),
)

FPS_PRESETS: tuple[tuple[str, int], ...] = (
    ("AUTO", 0),
    ("144", 144),
    ("120", 120),
    ("90", 90),
    ("75", 75),
    ("60", 60),
    ("50", 50),
    ("30", 30),
    ("24", 24),
)


class ControlBar(QWidget):
    """Top bar: import, rescan, source combo, view modes, session (LIVE + baseline)."""

    mountVodRequested = Signal()
    viewModeChanged = Signal(str)
    recordBaselineToggled = Signal()
    rescanDevicesRequested = Signal()
    analyzeDisplayToggled = Signal(bool)
    sourceProfileChanged = Signal(str)
    captureModeChanged = Signal(int, int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ControlBar")
        self.setFixedHeight(44)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(8)

        self.mount_vod_btn = QPushButton("IMPORT")
        self.mount_vod_btn.setObjectName("MountButton")
        self.mount_vod_btn.setToolTip("Import a gameplay VOD for review.")
        self.mount_vod_btn.clicked.connect(self.mountVodRequested.emit)
        layout.addWidget(self.mount_vod_btn)

        self.rescan_btn = QPushButton("RESCAN")
        self.rescan_btn.clicked.connect(self.rescanDevicesRequested.emit)
        layout.addWidget(self.rescan_btn)

        self.profile_combo = QComboBox()
        self.profile_combo.setObjectName("SourceCombo")
        self.profile_combo.setToolTip(
            "Source profile: ignore stream chrome / facecam and scene-gate kinematics."
        )
        for text, profile in (("HDMI", "hdmi_game"), ("STREAM", "stream_window"), ("VOD", "vod_file")):
            self.profile_combo.addItem(text, profile)
        self.profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        layout.addWidget(self.profile_combo)

        layout.addStretch(1)
        layout.addWidget(self._build_view_mode_group())
        layout.addStretch(1)

        self.capture_mode_group = self._build_capture_mode_group()
        layout.addWidget(self.capture_mode_group)

        self.session_group = self._build_session_group()
        layout.addWidget(self.session_group)

        self.analyze_display_checkbox = QCheckBox("Analyze this display anyway")
        self.analyze_display_checkbox.setToolTip(
            "Live capture does not run YOLO player detection by default."
        )
        self.analyze_display_checkbox.toggled.connect(self.analyzeDisplayToggled.emit)
        self.analyze_display_checkbox.hide()

    def _build_session_group(self) -> QFrame:
        group = QFrame(self)
        group.setObjectName("SessionGroup")
        group_layout = QHBoxLayout(group)
        group_layout.setContentsMargins(4, 4, 6, 4)
        group_layout.setSpacing(8)

        self.mode_pill = QLabel("LIVE")
        self.mode_pill.setObjectName("ModePill")
        self.mode_pill.setProperty("mode", "live")
        group_layout.addWidget(self.mode_pill)

        self.record_baseline_btn = QPushButton("RECORD CLEAN BASELINE")
        self.record_baseline_btn.setObjectName("BaselineButton")
        self.record_baseline_btn.clicked.connect(self.recordBaselineToggled.emit)
        group_layout.addWidget(self.record_baseline_btn)
        return group

    def _build_view_mode_group(self) -> QFrame:
        group = QFrame(self)
        group.setObjectName("ChipGroup")
        group_layout = QHBoxLayout(group)
        group_layout.setContentsMargins(4, 4, 4, 4)
        group_layout.setSpacing(2)

        self.view_mode_group = QButtonGroup(self)
        self.view_mode_group.setExclusive(True)
        self.view_buttons: dict[str, QPushButton] = {}
        for text, mode_id in (("STANDARD", "standard"), ("HEATMAP", "heatmap"), ("FLAGGED", "flagged_only")):
            button = QPushButton(text)
            button.setCheckable(True)
            button.clicked.connect(lambda _checked, mode=mode_id: self.viewModeChanged.emit(mode))
            self.view_mode_group.addButton(button)
            self.view_buttons[mode_id] = button
            group_layout.addWidget(button)
        self.view_buttons["standard"].setChecked(True)
        return group

    def _on_profile_changed(self, index: int) -> None:
        profile = self.profile_combo.itemData(index)
        if profile:
            self.sourceProfileChanged.emit(str(profile))

    def set_source_profile(self, profile: str) -> None:
        index = self.profile_combo.findData(profile)
        if index < 0 or index == self.profile_combo.currentIndex():
            return
        self.profile_combo.blockSignals(True)
        self.profile_combo.setCurrentIndex(index)
        self.profile_combo.blockSignals(False)

    def _build_capture_mode_group(self) -> QFrame:
        group = QFrame(self)
        group.setObjectName("ControlGroup")
        group_layout = QHBoxLayout(group)
        group_layout.setContentsMargins(4, 4, 4, 4)
        group_layout.setSpacing(6)

        res_label = QLabel("RES")
        res_label.setObjectName("ControlGroupLabel")
        group_layout.addWidget(res_label)

        self.resolution_combo = QComboBox()
        self.resolution_combo.setToolTip("Manually pin the capture resolution.")
        for label, _width, _height in RESOLUTION_PRESETS:
            self.resolution_combo.addItem(label)
        self.resolution_combo.currentIndexChanged.connect(self._on_capture_mode_changed)
        group_layout.addWidget(self.resolution_combo)

        fps_label = QLabel("FPS")
        fps_label.setObjectName("ControlGroupLabel")
        group_layout.addWidget(fps_label)

        self.fps_combo = QComboBox()
        self.fps_combo.setToolTip("Manually pin the capture frame rate.")
        for label, _fps in FPS_PRESETS:
            self.fps_combo.addItem(label)
        self.fps_combo.currentIndexChanged.connect(self._on_capture_mode_changed)
        group_layout.addWidget(self.fps_combo)
        return group

    def _on_capture_mode_changed(self, _index: int) -> None:
        _, width, height = RESOLUTION_PRESETS[self.resolution_combo.currentIndex()]
        _, fps = FPS_PRESETS[self.fps_combo.currentIndex()]
        if width > 0 and height > 0 and fps > 0:
            self.captureModeChanged.emit(width, height, fps)
        else:
            self.captureModeChanged.emit(0, 0, 0)

    def set_stream_mode(self, mode: str) -> None:
        self.mode_pill.setText(mode.upper())
        self.mode_pill.setProperty("mode", mode)
        self.mode_pill.style().unpolish(self.mode_pill)
        self.mode_pill.style().polish(self.mode_pill)
        self.capture_mode_group.setVisible(mode != "live")

    def set_active_view_mode(self, mode_id: str) -> None:
        button = self.view_buttons.get(mode_id)
        if button is not None:
            button.setChecked(True)

    def set_recording_baseline(self, active: bool, stream_mode: str = "live") -> None:
        if stream_mode == "vod":
            self.record_baseline_btn.setText("STOP MARKING CLEAN" if active else "MARK VOD AS CLEAN")
        else:
            self.record_baseline_btn.setText("STOP BASELINE" if active else "RECORD CLEAN BASELINE")
