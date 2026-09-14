from __future__ import annotations

from collections import deque

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
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

from src.ui.incident_queue import IncidentQueueTable
from src.ui.theme import ACCENT, ACCENT_DIM, ALERT, HAIRLINE, TEXT_MUTED, VOD_ACCENT, WARNING

# Shared with the control bar so its buttons sit exactly over the rail's cards.
RAIL_WIDTH = 268
RAIL_SIDE_MARGIN = 12

_PROFILE_LABELS = {"hdmi_game": "HDMI GAME", "stream_window": "STREAM WIN", "vod_file": "VOD FILE"}


def _short_device_name(name: str) -> str:
    """Trim vendor boilerplate so 'device · profile' fits the 268px rail."""
    short = name
    for prefix in ("AVerMedia HD Capture ", "AVerMedia ", "Elgato ", "Logitech "):
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
        painter.fillRect(rect, QColor(HAIRLINE))
        painter.setPen(QColor(TEXT_MUTED))
        font = painter.font()
        font.setPixelSize(9)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect.adjusted(6, 3, -6, 0), Qt.AlignTop | Qt.AlignLeft, "STR")
        painter.setPen(QColor(ACCENT))
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
        accent = QColor(ACCENT)
        accent.setAlpha(90)
        dim = QColor(ACCENT_DIM)
        dim.setAlpha(20)
        gradient.setColorAt(0.0, accent)
        gradient.setColorAt(1.0, dim)
        painter.fillPath(fill, gradient)
        painter.setPen(QPen(QColor(ACCENT), 1.4))
        for index in range(len(tremor_points) - 1):
            painter.drawLine(tremor_points[index], tremor_points[index + 1])
        if len(self._straightness) >= 2:
            str_points = self._series_points(self._straightness, plot, 1.0)
            painter.setPen(QPen(QColor(VOD_ACCENT), 1.4))
            for index in range(len(str_points) - 1):
                painter.drawLine(str_points[index], str_points[index + 1])
        painter.end()


class MetricRow(QWidget):
    def __init__(self, key: str, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
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
    # (device_name, source_profile) chosen in the SOURCE selector.
    sourceSelected = Signal(str, str)
    # (width, height, fps) pinned in the MODE picker; (0, 0, 0) means AUTO.
    captureModeSelected = Signal(int, int, int)

    _MODE_HELP = (
        "Capture mode. AUTO tests the device's modes and keeps the fastest one that streams "
        "cleanly, then shows what it negotiated. The other entries are only the modes this "
        "device advertised at the last scan. Picking one restarts capture on it; if the device "
        "rejects it, capture falls back to AUTO and the status text says so."
    )
    _SELECTOR_HELP = (
        "Video device + profile. The capture card serves one app at a time; pick a Virtual "
        "Camera entry (Streaming Center / OBS) to analyse while that app records. The profile "
        "part (HDMI GAME / STREAM WINDOW / VOD FILE) decides which screen regions are ignored."
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("LeftRail")
        self.setMinimumWidth(RAIL_WIDTH)
        self.setMaximumWidth(RAIL_WIDTH)

        root = QVBoxLayout(self)
        root.setContentsMargins(RAIL_SIDE_MARGIN, 10, RAIL_SIDE_MARGIN, 12)
        root.setSpacing(10)

        brand = QLabel("CHEATVISION")
        brand.setObjectName("RailBrand")
        brand.setAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
        root.addWidget(brand)

        # Recording control lives in the rail, right under the brand.
        self.record_baseline_btn = QPushButton("RECORD CLEAN BASELINE")
        self.record_baseline_btn.setObjectName("BaselineButton")
        self.record_baseline_btn.setFixedHeight(28)
        self.record_baseline_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.record_baseline_btn.clicked.connect(self.recordBaselineToggled.emit)
        root.addWidget(self.record_baseline_btn)

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
        self._source_backend = MetricRow("Pipe")
        self._ignore_row = MetricRow("Ignore")
        self._baseline_row = MetricRow("Base")
        self.source.add_row(self.source_combo)
        self.source.add_row(self._mode_row)
        self.source.add_row(self._source_feed)
        self.source.add_row(self._source_backend)
        self.source.add_row(self._ignore_row)
        self.source.add_row(self._baseline_row)
        root.addWidget(self.source)
        self._devices: list[dict] = []
        self._current_device_name = ""
        self._current_profile = "hdmi_game"

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
        data = self.source_combo.itemData(index)
        if not data:
            return
        device_name, profile = data
        self._current_device_name = device_name
        self._current_profile = profile
        self.sourceSelected.emit(str(device_name), str(profile))

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

    def set_devices(self, devices: list[dict], current_name: str, current_profile: str | None = None) -> None:
        """Rebuild the selector: one entry per (device, profile) pair. The
        capture card gets HDMI GAME and STREAM WINDOW (some people capture a
        browser through the card); virtual cameras and webcams get all three."""
        self._devices = [d for d in devices if str(d.get("name", ""))]
        if current_profile:
            self._current_profile = current_profile
        self._current_device_name = current_name
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        for device in self._devices:
            name = str(device["name"])
            kind = str(device.get("kind", "Input")).lower()
            profiles = ("hdmi_game", "stream_window") if kind == "capture card" else ("hdmi_game", "stream_window", "vod_file")
            for profile in profiles:
                label = f"{_short_device_name(name)} · {_PROFILE_LABELS[profile]}"
                self.source_combo.addItem(label, (name, profile))
        if self.source_combo.count() == 0:
            # Nothing real to offer: say why (discovery puts the reason in the
            # label of its single empty-named entry) instead of inventing devices.
            reason = next((str(d.get("label", "")) for d in devices if not str(d.get("name", ""))), "")
            self.source_combo.addItem(reason or "No video devices found", None)
        self._select_current()
        self.source_combo.blockSignals(False)

    def set_source_profile(self, profile: str) -> None:
        """Reflect a profile change made elsewhere (e.g. importing a VOD)."""
        if profile == self._current_profile:
            return
        self._current_profile = profile
        self.source_combo.blockSignals(True)
        self._select_current()
        self.source_combo.blockSignals(False)

    def _select_current(self) -> None:
        wanted = (self._current_device_name, self._current_profile)
        for i in range(self.source_combo.count()):
            if self.source_combo.itemData(i) == wanted:
                self.source_combo.setCurrentIndex(i)
                return
        # Device present but this profile isn't offered for it: fall back to
        # the device's first entry rather than showing a wrong device. The
        # "no devices" placeholder carries no data and is skipped.
        for i in range(self.source_combo.count()):
            data = self.source_combo.itemData(i)
            if data and data[0] == self._current_device_name:
                self.source_combo.setCurrentIndex(i)
                self._current_profile = data[1]
                return

    def set_recording_baseline(self, active: bool, stream_mode: str = "live") -> None:
        if stream_mode == "vod":
            self.record_baseline_btn.setText("STOP MARKING CLEAN" if active else "MARK VOD AS CLEAN")
        else:
            self.record_baseline_btn.setText("STOP BASELINE" if active else "RECORD CLEAN BASELINE")

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

    def set_feed_rate(self, delivered_fps: float, unique_fps: float, *, starved: bool = False) -> None:
        """Real frame rate arriving from the device (vs. the requested mode)."""
        if delivered_fps <= 0:
            self._source_feed.set_value("—")
            return
        text = f"{delivered_fps:.0f} fps"
        if unique_fps > 0 and unique_fps < delivered_fps - 2:
            text += f" ({unique_fps:.0f} new)"
        self._source_feed.set_value(text, alert=starved)

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

    def set_detect(self, *, yolo_on: bool, tracks: int, gate: str) -> None:
        self._yolo_row.set_value("ON" if yolo_on else "OFF", warn=yolo_on)
        self._tracks_row.set_value(str(int(tracks)))
        skipped = gate not in ("", "live", "ok")
        self._gate_row.set_value(gate or "live", warn=skipped)

    def set_profile(self, name: str, ignore_count: int, baseline: str) -> None:
        # The profile itself is shown by the combo; only the derived state here.
        self._ignore_row.set_value(str(int(ignore_count)))
        self._baseline_row.set_value(baseline or "idle", warn=(baseline == "rec"))

    def set_analyze_checked(self, checked: bool) -> None:
        if self.analyze_checkbox.isChecked() == checked:
            return
        self.analyze_checkbox.blockSignals(True)
        self.analyze_checkbox.setChecked(checked)
        self.analyze_checkbox.blockSignals(False)
