from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QWidget,
)

from src.ui.left_rail import RAIL_SIDE_MARGIN, RAIL_WIDTH


class StatusLabel(QLabel):
    """Single-line status text that takes whatever width the bar has left and
    elides instead of pushing the buttons off-screen; the full text is kept
    for `text()` and shown as a tooltip."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full_text = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(80)
        self.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 (Qt API)
        self._full_text = str(text)
        self.setToolTip(self._full_text)
        self._apply_elide()

    def text(self) -> str:  # noqa: N802 (Qt API)
        return self._full_text

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self) -> None:
        available = max(0, self.width() - 4)
        super().setText(self.fontMetrics().elidedText(self._full_text, Qt.ElideRight, available))

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
    """Top bar: import, rescan, source combo, optional RES/FPS, baseline."""

    mountVodRequested = Signal()
    viewModeChanged = Signal(str)
    rescanDevicesRequested = Signal()
    analyzeDisplayToggled = Signal(bool)
    captureModeChanged = Signal(int, int, int)
    toolsPanelToggled = Signal(bool)
    supportRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ControlBar")
        self.setFixedHeight(44)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 12, 0)
        layout.setSpacing(0)

        # The three buttons live in a block exactly as wide as the tool rail
        # below them, with the rail's own side margins, so they line up with
        # its cards and split its width evenly.
        self.rail_block = QWidget(self)
        self.rail_block.setFixedWidth(RAIL_WIDTH)
        rail_layout = QHBoxLayout(self.rail_block)
        rail_layout.setContentsMargins(RAIL_SIDE_MARGIN, 0, RAIL_SIDE_MARGIN, 0)
        rail_layout.setSpacing(6)

        self.mount_vod_btn = QPushButton("IMPORT")
        self.mount_vod_btn.setObjectName("MountButton")
        self.mount_vod_btn.setToolTip("Import a gameplay VOD for review.")
        self.mount_vod_btn.clicked.connect(self.mountVodRequested.emit)

        self.rescan_btn = QPushButton("RESCAN")
        self.rescan_btn.setToolTip("Re-list video devices and reconnect.")
        self.rescan_btn.clicked.connect(self.rescanDevicesRequested.emit)

        self.tools_panel_btn = QPushButton("TOOLS")
        self.tools_panel_btn.setCheckable(True)
        self.tools_panel_btn.setChecked(True)
        self.tools_panel_btn.setToolTip("Show or hide the left tools panel.")
        self.tools_panel_btn.toggled.connect(self._on_tools_panel_toggled)

        for button in (self.mount_vod_btn, self.rescan_btn, self.tools_panel_btn):
            button.setObjectName(button.objectName() or "RailButton")
            button.setFixedHeight(26)
            button.setMinimumWidth(0)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            rail_layout.addWidget(button, 1)
        layout.addWidget(self.rail_block, 0)

        # Live status (mode, profile, warnings) lives here instead of a bottom
        # status bar, so the video reaches the window edge. It starts where the
        # video does, with the same inset the rail's cards use.
        self.status_label = StatusLabel("Initializing...")
        self.status_label.setObjectName("StatusLabel")
        layout.addSpacing(RAIL_SIDE_MARGIN)
        layout.addWidget(self.status_label, 1)
        layout.addSpacing(8)

        self.capture_mode_group = self._build_capture_mode_group()
        layout.addWidget(self.capture_mode_group)
        layout.addSpacing(8)

        self.support_btn = QPushButton("SUPPORT")
        self.support_btn.setFixedHeight(26)
        self.support_btn.setToolTip("Open Support & Diagnostics.")
        self.support_btn.clicked.connect(self.supportRequested.emit)
        layout.addWidget(self.support_btn)

        self.analyze_display_checkbox = QCheckBox("Analyze this display anyway")
        self.analyze_display_checkbox.setToolTip(
            "Live capture does not run YOLO player detection by default."
        )
        self.analyze_display_checkbox.toggled.connect(self.analyzeDisplayToggled.emit)
        self.analyze_display_checkbox.hide()

        self.view_buttons: dict[str, QPushButton] = {}

    def _on_tools_panel_toggled(self, visible: bool) -> None:
        self.tools_panel_btn.setText("TOOLS" if visible else "TOOLS OFF")
        self.toolsPanelToggled.emit(visible)

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
        self.capture_mode_group.setVisible(mode != "live")

    def set_active_view_mode(self, mode_id: str) -> None:
        button = self.view_buttons.get(mode_id)
        if button is not None:
            button.setChecked(True)

    def set_tools_panel_visible(self, visible: bool) -> None:
        if self.tools_panel_btn.isChecked() == visible:
            self.tools_panel_btn.setText("TOOLS" if visible else "TOOLS OFF")
            return
        self.tools_panel_btn.blockSignals(True)
        self.tools_panel_btn.setChecked(visible)
        self.tools_panel_btn.blockSignals(False)
        self.tools_panel_btn.setText("TOOLS" if visible else "TOOLS OFF")
