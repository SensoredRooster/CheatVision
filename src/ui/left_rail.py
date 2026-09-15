from __future__ import annotations

from collections import deque

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.core.frame_source import is_browser_window_device
from src.ui.branding import WORDMARK_LEFT, WORDMARK_RIGHT, brand_font, brand_pixmap
from src.ui.incident_queue import IncidentQueueTable
from src.ui.theme import ACCENT, ALERT, HAIRLINE, PANEL, TEXT_PRIMARY, TRACE, WARNING

# Shared with the control bar so its buttons sit exactly over the rail's cards.
RAIL_WIDTH = 268
RAIL_SIDE_MARGIN = 12

# MASK picker: which regions of the picture the analyser ignores. GAME is a
# bare game feed (facecam corner, own weapon); STREAM is a stream page (top
# and bottom chrome, chat column, facecam). vod_file has the STREAM masks.
_MASK_ITEMS: tuple[tuple[str, str], ...] = (("GAME", "hdmi_game"), ("STREAM", "stream_window"))


def _short_device_name(name: str) -> str:
    """Trim vendor boilerplate so 'device · profile' fits the 268px rail."""
    short = name
    # Vendor boilerplate only; the model name that identifies *their* card stays.
    for prefix in (
        "AVerMedia HD Capture ",
        "AVerMedia ",
        "Elgato Game Capture ",
        "Elgato ",
        "Magewell ",
        "Blackmagic ",
        "Razer ",
        "Hauppauge ",
        "Logitech ",
    ):
        if short.startswith(prefix):
            short = short[len(prefix):]
            break
    short = short.replace("Streaming Center", "StreamCenter")
    short = short.replace(" Virtual Camera", " VCam").replace(" Virtual Cam", " VCam").replace(" Virtual Webcam", " VCam")
    return short.strip() or name


class AimGraph(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Sparkline")
        self.setFixedHeight(78)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._straightness: deque[float] = deque(maxlen=48)
        self._tremor: deque[float] = deque(maxlen=48)

    def push(self, straightness: float, tremor: float) -> None:
        self._straightness.append(min(1.0, max(0.0, float(straightness))))
        self._tremor.append(max(0.0, float(tremor)))
        self.update()

    def clear(self) -> None:
        self._straightness.clear()
        self._tremor.clear()
        self.update()

    def _series_points(self, values: deque[float], rect, peak: float) -> list[QPointF]:
        width = max(1, rect.width())
        height = max(1, rect.height() - 16)
        step = width / max(1, values.maxlen - 1)
        points = []
        for index, value in enumerate(values):
            x = rect.left() + index * step
            y = rect.bottom() - (value / peak) * height
            points.append(QPointF(x, y))
        return points

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(1, 1, -1, -1)
        painter.setPen(QPen(QColor(HAIRLINE), 1.0))
        painter.setBrush(QColor(PANEL))
        painter.drawRoundedRect(rect, 4, 4)
        painter.setBrush(Qt.NoBrush)
        font = painter.font()
        font.setPixelSize(9)
        font.setBold(True)
        painter.setFont(font)
        # Legend in the series' own colours: red straightness (the thing to
        # watch), neutral tremor (the human evidence).
        painter.setPen(QColor(ACCENT))
        painter.drawText(rect.adjusted(6, 3, -6, 0), Qt.AlignTop | Qt.AlignLeft, "STR")
        painter.setPen(QColor(TRACE))
        painter.drawText(rect.adjusted(6, 3, -6, 0), Qt.AlignTop | Qt.AlignRight, "TREMOR")
        if len(self._tremor) < 2:
            painter.end()
            return
        plot = rect.adjusted(2, 16, -2, -2)
        tremor_peak = max(max(self._tremor), 1.0)
        tremor_points = self._series_points(self._tremor, plot, tremor_peak)
        fill = QPainterPath()
        fill.moveTo(tremor_points[0].x(), plot.bottom())
        for point in tremor_points:
            fill.lineTo(point)
        fill.lineTo(tremor_points[-1].x(), plot.bottom())
        fill.closeSubpath()
        gradient = QLinearGradient(plot.topLeft(), plot.bottomLeft())
        top = QColor(TRACE)
        top.setAlpha(70)
        bottom = QColor(TRACE)
        bottom.setAlpha(8)
        gradient.setColorAt(0.0, top)
        gradient.setColorAt(1.0, bottom)
        painter.fillPath(fill, gradient)
        painter.setPen(QPen(QColor(TRACE), 1.2))
        for index in range(len(tremor_points) - 1):
            painter.drawLine(tremor_points[index], tremor_points[index + 1])
        if len(self._straightness) >= 2:
            str_points = self._series_points(self._straightness, plot, 1.0)
            painter.setPen(QPen(QColor(ACCENT), 1.6))
            for index in range(len(str_points) - 1):
                painter.drawLine(str_points[index], str_points[index + 1])
        painter.end()


class MetricRow(QWidget):
    def __init__(self, key: str, parent=None):
        super().__init__(parent)
        # An inset strip on the card (QWidget#RailMetricRow in the stylesheet).
        self.setObjectName("RailMetricRow")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(8)
        self._key = QLabel(key.upper())
        self._key.setObjectName("RailMetricKey")
        self._value = QLabel("—")
        self._value.setObjectName("RailMetricVal")
        self._value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self._key, 0)
        layout.addWidget(self._value, 1)

    def set_value(self, text: str, *, alert: bool = False, warn: bool = False) -> None:
        self._value.setText(text)
        if alert:
            self._value.setStyleSheet(f"color: {ALERT};")
        elif warn:
            self._value.setStyleSheet(f"color: {WARNING};")
        else:
            self._value.setStyleSheet("")


class RailSection(QFrame):
    def __init__(self, title: str, parent=None, *, centered_title: bool = False):
        super().__init__(parent)
        self.setObjectName("RailCard")
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(12, 10, 12, 10)
        self.layout.setSpacing(6)
        self._heading = QLabel(title.upper())
        self._heading.setObjectName("RailCardTitle")
        self._heading.setFont(brand_font(10, 2.0, QFont.Weight.Bold))
        if centered_title:
            self._heading.setAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
        self.layout.addWidget(self._heading)

    def set_title(self, title: str) -> None:
        self._heading.setText(title.upper())

    def add_row(self, widget: QWidget) -> None:
        self.layout.addWidget(widget)


class LeftRail(QWidget):
    analyzeToggled = Signal(bool)
    recordBaselineToggled = Signal()
    recordSessionToggled = Signal()
    # Device name chosen in the SOURCE selector (one entry per input).
    sourceSelected = Signal(str)
    # Source profile chosen in the MASK picker: "hdmi_game" or "stream_window".
    maskSelected = Signal(str)
    # (width, height, fps) pinned in the MODE picker; (0, 0, 0) means AUTO.
    captureModeSelected = Signal(int, int, int)

    _MODE_HELP = (
        "Capture mode. AUTO tests the device's modes and keeps the fastest one that streams "
        "cleanly, then shows what it negotiated. The other entries are only the modes this "
        "device advertised at the last scan. Picking one restarts capture on it; if the device "
        "rejects it, capture falls back to AUTO and the status text says so."
    )
    _SELECTOR_HELP = (
        "Where the picture comes from: one entry per input, by its own name. Capture cards, "
        "virtual cameras (OBS / Streaming Center / Streamlabs, so that app can record while "
        "CheatVision analyses) and webcams as Windows lists them, plus Browser window (a screen "
        "grab of a Twitch / Kick / YouTube tab). What to ignore on the picture is the MASK below."
    )
    _MASK_HELP = (
        "Which regions of the picture the analyser ignores. GAME: a bare game feed (facecam "
        "corner, the player's own weapon). STREAM: a stream page (top and bottom chrome, chat "
        "column, facecam). Applies immediately, no capture restart. Browser window is always STREAM."
    )
    _ANALYSIS_HELP = (
        "How many new pictures a second the aim analyser scores, and what fraction of the feed "
        "that is. The stride follows the feed's real rate, so 60, 144 and 240 Hz sources are "
        "judged at the same cadence and the aim thresholds keep their real-world meaning "
        "(analysis_rate_hz in settings, default 20)."
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("LeftRail")
        self.setMinimumWidth(RAIL_WIDTH)
        self.setMaximumWidth(RAIL_WIDTH)

        root = QVBoxLayout(self)
        root.setContentsMargins(RAIL_SIDE_MARGIN, 10, RAIL_SIDE_MARGIN, 12)
        root.setSpacing(10)

        # Brand lockup: the crosshair mark beside the wordmark (CHEAT white,
        # VISION red, echoing the letters on the disc).
        lockup = QWidget()
        lockup_layout = QHBoxLayout(lockup)
        lockup_layout.setContentsMargins(0, 2, 0, 0)
        lockup_layout.setSpacing(10)
        lockup_layout.addStretch(1)
        mark = QLabel()
        mark.setObjectName("RailBrandMark")
        mark.setFixedSize(34, 34)
        mark.setAlignment(Qt.AlignCenter)
        pixmap = brand_pixmap(34, self.devicePixelRatioF())
        if not pixmap.isNull():
            mark.setPixmap(pixmap)
        lockup_layout.addWidget(mark, 0)
        brand = QLabel(
            f'<span style="color:{TEXT_PRIMARY}">{WORDMARK_LEFT}</span>'
            f'<span style="color:{ACCENT}">{WORDMARK_RIGHT}</span>'
        )
        brand.setObjectName("RailBrand")
        brand.setTextFormat(Qt.RichText)
        brand.setFont(brand_font(17, 3.5))
        brand.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        lockup_layout.addWidget(brand, 0)
        lockup_layout.addStretch(1)
        root.addWidget(lockup)

        # Recording control lives in the rail, right under the brand.
        self.record_baseline_btn = QPushButton("RECORD CLEAN BASELINE")
        self.record_baseline_btn.setObjectName("BaselineButton")
        self.record_baseline_btn.setFixedHeight(28)
        self.record_baseline_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.record_baseline_btn.clicked.connect(self.recordBaselineToggled.emit)
        root.addWidget(self.record_baseline_btn)

        # Plain recording of the watched feed (data/recordings/). Unlike the
        # baseline button it carries no clean/suspicious meaning — it is the
        # session's own evidence file. Live only: a VOD already is a file.
        self.record_session_btn = QPushButton("RECORD SESSION")
        self.record_session_btn.setObjectName("BaselineButton")
        self.record_session_btn.setFixedHeight(28)
        self.record_session_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.record_session_btn.setToolTip(
            "Save the live feed to data/recordings/session_<time>.mp4 while you monitor. "
            "Independent of the clean-baseline recorder; both can run at once."
        )
        self.record_session_btn.clicked.connect(self.recordSessionToggled.emit)
        root.addWidget(self.record_session_btn)

        # One card for everything about where the picture comes from: device,
        # negotiated mode, real feed rate, pipe, source profile (selectable),
        # and the profile's ignore-rect count / baseline recording state.
        self.source = RailSection("Source")
        # One selector for *where the picture comes from and how to read it*:
        # each entry is a device (capture card, or another app's virtual
        # camera so that app can record while CheatVision analyses) paired
        # with a source profile (which screen regions to ignore). Picking an
        # entry sets both, so there is no separate PROFILE row.
        self.source_combo = QComboBox()
        self.source_combo.setObjectName("SourceCombo")
        self.source_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.source_combo.setMinimumContentsLength(18)
        self.source_combo.setToolTip(self._SELECTOR_HELP)
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        # MASK is the other half of "what am I looking at": the ignore-rect
        # set for the picture. It used to be multiplied into the selector as
        # a profile suffix on every device (three entries per input); now it
        # is one picker that applies live.
        self._mask_row = QWidget()
        mask_layout = QHBoxLayout(self._mask_row)
        mask_layout.setContentsMargins(0, 0, 0, 0)
        mask_layout.setSpacing(8)
        mask_key = QLabel("MASK")
        mask_key.setObjectName("RailMetricKey")
        self.mask_combo = QComboBox()
        self.mask_combo.setObjectName("SourceCombo")
        self.mask_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.mask_combo.setMinimumContentsLength(14)
        self.mask_combo.setToolTip(self._MASK_HELP)
        for label, profile in _MASK_ITEMS:
            self.mask_combo.addItem(label, profile)
        self.mask_combo.currentIndexChanged.connect(self._on_mask_changed)
        mask_layout.addWidget(mask_key, 0)
        mask_layout.addWidget(self.mask_combo, 1)
        # MODE is a picker, not a readout: AUTO (labelled with whatever
        # calibration negotiated) plus only the modes this device advertised
        # when it was opened. Nothing generic is ever offered.
        self._mode_row = QWidget()
        mode_layout = QHBoxLayout(self._mode_row)
        mode_layout.setContentsMargins(0, 0, 0, 0)
        mode_layout.setSpacing(8)
        mode_key = QLabel("MODE")
        mode_key.setObjectName("RailMetricKey")
        self.mode_combo = QComboBox()
        self.mode_combo.setObjectName("SourceCombo")
        self.mode_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.mode_combo.setMinimumContentsLength(14)
        self.mode_combo.setToolTip(self._MODE_HELP)
        self.mode_combo.addItem("AUTO", (0, 0, 0))
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_layout.addWidget(mode_key, 0)
        mode_layout.addWidget(self.mode_combo, 1)
        self._negotiated_mode_text = ""
        self._negotiated_low_mode = False
        self._pinned_mode: tuple[int, int, int] = (0, 0, 0)
        self._source_feed = MetricRow("Feed")
        # Analysed samples a second and the stride producing them: the aim
        # rules run at one cadence whatever the feed's rate (see
        # AntiCheatPipeline.set_feed_rate), and this row shows that choice.
        self._source_analysis = MetricRow("Analysis")
        self._source_analysis.setToolTip(self._ANALYSIS_HELP)
        self._source_backend = MetricRow("Pipe")
        self._baseline_row = MetricRow("Base")
        self.source.add_row(self.source_combo)
        self.source.add_row(self._mode_row)
        self.source.add_row(self._mask_row)
        self.source.add_row(self._source_feed)
        self.source.add_row(self._source_analysis)
        self.source.add_row(self._source_backend)
        self.source.add_row(self._baseline_row)
        root.addWidget(self.source)
        self._devices: list[dict] = []
        self._current_device_name = ""
        self._mask_profile = "hdmi_game"

        self.detect = RailSection("Detect")
        self._yolo_row = MetricRow("Yolo")
        self._tracks_row = MetricRow("Tracks")
        self._gate_row = MetricRow("Gate")
        self.analyze_checkbox = QCheckBox("ANALYZE LIVE")
        self.analyze_checkbox.setObjectName("RailCheck")
        self.analyze_checkbox.setToolTip(
            "Live capture does not run YOLO by default. Check to force player boxes."
        )
        self.analyze_checkbox.toggled.connect(self.analyzeToggled.emit)
        self.detect.add_row(self._yolo_row)
        self.detect.add_row(self._tracks_row)
        self.detect.add_row(self._gate_row)
        self.detect.add_row(self.analyze_checkbox)
        root.addWidget(self.detect)

        self.signal = RailSection("Signal")
        self._signal_status = QLabel("ok")
        self._signal_status.setObjectName("RailStatusOk")
        self._signal_status.setFont(brand_font(12, 2.0, QFont.Weight.Bold))
        self._signal_str = MetricRow("Str")
        self._signal_tremor = MetricRow("Tremor")
        self._sparkline = AimGraph(self)
        self.signal.add_row(self._signal_status)
        self.signal.add_row(self._signal_str)
        self.signal.add_row(self._signal_tremor)
        self.signal.add_row(self._sparkline)
        root.addWidget(self.signal)

        # Flagged events live here (the only place), filling the rest of the
        # rail; double-click a row to seek a mounted VOD to that frame.
        self.incidents = RailSection("Incidents · 0", centered_title=True)
        self.incident_table = IncidentQueueTable(self)
        self.incident_table.setMinimumHeight(90)
        self.incident_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.incidents.add_row(self.incident_table)
        root.addWidget(self.incidents, 1)
        self._incident_count = 0

    def _on_source_changed(self, index: int) -> None:
        name = self.source_combo.itemData(index)
        if not name:
            return
        self._current_device_name = str(name)
        self.sourceSelected.emit(str(name))

    def _on_mask_changed(self, index: int) -> None:
        profile = self.mask_combo.itemData(index)
        if not profile:
            return
        self._mask_profile = str(profile)
        self.maskSelected.emit(str(profile))

    def _on_mode_changed(self, index: int) -> None:
        data = self.mode_combo.itemData(index)
        if not data:
            return
        width, height, fps = (int(v) for v in data)
        self._pinned_mode = (width, height, fps)
        self.captureModeSelected.emit(width, height, fps)

    def _auto_label(self) -> str:
        if self._negotiated_low_mode:
            return "AUTO · low mode"
        if self._negotiated_mode_text:
            return f"AUTO · {self._negotiated_mode_text}"
        return "AUTO"

    @staticmethod
    def _mode_label(mode: tuple[int, int, int]) -> str:
        return f"{mode[0]}×{mode[1]} @ {mode[2]}"

    def set_capture_modes(self, modes, pinned=None) -> None:
        """Rebuild the MODE picker from what the device advertised at this open.
        `pinned` is the user's saved override or None for AUTO. A pin the device
        no longer advertises is still listed (marked) so it can be seen and cleared."""
        self._pinned_mode = tuple(int(v) for v in pinned) if pinned else (0, 0, 0)
        self.mode_combo.blockSignals(True)
        self.mode_combo.clear()
        self.mode_combo.addItem(self._auto_label(), (0, 0, 0))
        listed: set[tuple[int, int, int]] = set()
        for width, height, fps in modes:
            mode = (int(width), int(height), int(fps))
            if mode in listed or min(mode) <= 0:
                continue
            listed.add(mode)
            self.mode_combo.addItem(self._mode_label(mode), mode)
        if self._pinned_mode != (0, 0, 0) and self._pinned_mode not in listed:
            self.mode_combo.addItem(f"{self._mode_label(self._pinned_mode)} · not advertised", self._pinned_mode)
        self._select_pinned_mode()
        self.mode_combo.blockSignals(False)

    def set_pinned_mode(self, pinned) -> None:
        """Reflect an override chosen elsewhere (top-bar RES/FPS) without re-emitting."""
        self._pinned_mode = tuple(int(v) for v in pinned) if pinned else (0, 0, 0)
        self.mode_combo.blockSignals(True)
        self._select_pinned_mode()
        self.mode_combo.blockSignals(False)

    def _select_pinned_mode(self) -> None:
        for i in range(self.mode_combo.count()):
            data = self.mode_combo.itemData(i)
            if data and tuple(int(v) for v in data) == self._pinned_mode:
                self.mode_combo.setCurrentIndex(i)
                return
        self.mode_combo.setCurrentIndex(0)

    def set_mode_picker_enabled(self, enabled: bool) -> None:
        """Pinning a capture mode only means something while capturing live."""
        self.mode_combo.setEnabled(bool(enabled))

    def set_devices(self, devices: list[dict], current_name: str) -> None:
        """Rebuild the selector: one entry per input, by its own name. Real
        DirectShow devices first (capture cards, virtual cameras, webcams --
        whatever Windows lists, nothing invented), then the Browser window
        pseudo-input. What to ignore on the picture is the MASK picker, so no
        input is ever listed more than once."""
        real = [d for d in devices if str(d.get("name", "")) and not is_browser_window_device(d)]
        browser = [d for d in devices if is_browser_window_device(d)]
        self._devices = real + browser
        self._current_device_name = current_name
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        if not real:
            # No device at all: say why (discovery puts the reason in the
            # label of its single empty-named entry) as an unselectable line.
            reason = next((str(d.get("label", "")) for d in devices if not str(d.get("name", ""))), "")
            self.source_combo.addItem(reason or "No video devices found", None)
            placeholder = self.source_combo.model().item(self.source_combo.count() - 1)
            if placeholder is not None:
                placeholder.setEnabled(False)
        for device in self._devices:
            name = str(device["name"])
            self.source_combo.addItem(_short_device_name(name), name)
        self._select_current()
        self.source_combo.blockSignals(False)

    def _select_current(self) -> None:
        for i in range(self.source_combo.count()):
            if self.source_combo.itemData(i) == self._current_device_name:
                self.source_combo.setCurrentIndex(i)
                return

    def set_mask_profile(self, profile: str, *, locked: bool = False) -> None:
        """Reflect the mask in force (set by the window: live pick, VOD import,
        or forced STREAM for the browser input) without re-emitting."""
        wanted = "hdmi_game" if profile == "hdmi_game" else "stream_window"
        self._mask_profile = wanted
        self.mask_combo.blockSignals(True)
        for i in range(self.mask_combo.count()):
            if self.mask_combo.itemData(i) == wanted:
                self.mask_combo.setCurrentIndex(i)
                break
        self.mask_combo.blockSignals(False)
        self.mask_combo.setEnabled(not locked)

    def set_recording_baseline(self, active: bool, stream_mode: str = "live") -> None:
        if stream_mode == "vod":
            self.record_baseline_btn.setText("STOP MARKING CLEAN" if active else "MARK VOD AS CLEAN")
        else:
            self.record_baseline_btn.setText("STOP BASELINE" if active else "RECORD CLEAN BASELINE")

    def set_recording_session(self, active: bool, stream_mode: str = "live") -> None:
        self.record_session_btn.setText("STOP RECORDING" if active else "RECORD SESSION")
        # Recording a VOD would only duplicate a file that already exists.
        self.record_session_btn.setEnabled(stream_mode == "live")

    def add_incident(self, event) -> None:
        self._incident_count += 1
        self.incident_table.add_event(event)
        self.incidents.set_title(f"Incidents · {self._incident_count}")

    def clear_incidents(self) -> None:
        self._incident_count = 0
        self.incident_table.clear()
        self.incidents.set_title("Incidents · 0")

    def set_source(self, name: str, mode: str, backend: str, *, low_mode: bool = False) -> None:
        # The selector shows a shortened device name; keep the full one as its tooltip.
        if name:
            self.source_combo.setToolTip(f"{name}\n\n{self._SELECTOR_HELP}")
        self._negotiated_mode_text = mode or ""
        self._negotiated_low_mode = bool(low_mode)
        self.mode_combo.setItemText(0, self._auto_label())
        self._source_backend.set_value(backend or "—")
        self._source_feed.set_value("—")
        self._source_analysis.set_value("—")

    def set_feed_rate(self, delivered_fps: float, unique_fps: float, *, starved: bool = False) -> None:
        """Real frame rate arriving from the device (vs. the requested mode)."""
        if delivered_fps <= 0:
            self._source_feed.set_value("—")
            return
        text = f"{delivered_fps:.0f} fps"
        if unique_fps > 0 and unique_fps < delivered_fps - 2:
            text += f" ({unique_fps:.0f} new)"
        self._source_feed.set_value(text, alert=starved)

    def set_analysis_rate(self, cadence_hz: float, stride: int) -> None:
        """Analysed samples a second and the stride that produces them."""
        if cadence_hz <= 0:
            self._source_analysis.set_value("—")
            return
        stride = max(1, int(stride))
        every = "every frame" if stride == 1 else f"1 in {stride}"
        self._source_analysis.set_value(f"{cadence_hz:.0f} Hz · {every}")

    def set_signal(self, *, frozen: bool, straightness: float, tremor: float) -> None:
        if frozen:
            self._signal_status.setText("FREEZE")
            self._signal_status.setObjectName("RailStatusAlert")
        else:
            self._signal_status.setText("LIVE AIM")
            self._signal_status.setObjectName("RailStatusOk")
        self._signal_status.style().unpolish(self._signal_status)
        self._signal_status.style().polish(self._signal_status)
        self._signal_str.set_value(f"{straightness:.2f}", warn=straightness >= 0.92)
        self._signal_tremor.set_value(f"{tremor:.2f}", alert=tremor <= 0.05 and straightness >= 0.90)
        self._sparkline.push(straightness, tremor)

    def set_detect(self, *, yolo_on: bool, tracks: int, gate: str, detector: str = "") -> None:
        # e.g. "ON · 960 GPU": the model's input size and where it runs.
        label = f"ON · {detector}" if (yolo_on and detector) else ("ON" if yolo_on else "OFF")
        self._yolo_row.set_value(label, warn=yolo_on)
        self._tracks_row.set_value(str(int(tracks)))
        skipped = gate not in ("", "live", "ok")
        self._gate_row.set_value(gate or "live", warn=skipped)

    def set_profile(self, profile: str, ignore_count: int, baseline: str) -> None:
        """Derived state: the MASK picker's tooltip carries how many regions
        the current mask is ignoring; BASE shows the baseline recorder."""
        if profile and profile != self._mask_profile:
            self.set_mask_profile(profile, locked=not self.mask_combo.isEnabled())
        count = int(ignore_count)
        self.mask_combo.setToolTip(f"{self._MASK_HELP}\n\nIgnoring {count} region{'s' if count != 1 else ''} right now.")
        self._baseline_row.set_value(baseline or "idle", warn=(baseline == "rec"))

    def set_analyze_checked(self, checked: bool) -> None:
        if self.analyze_checkbox.isChecked() == checked:
            return
        self.analyze_checkbox.blockSignals(True)
        self.analyze_checkbox.setChecked(checked)
        self.analyze_checkbox.blockSignals(False)
