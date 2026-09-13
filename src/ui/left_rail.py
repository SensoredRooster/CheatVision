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


class WidgetRow(QWidget):
    """Metric-style row whose value is an interactive widget (e.g. a combo)."""

    def __init__(self, key: str, widget: QWidget, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self._key = QLabel(key.upper())
        self._key.setObjectName("RailMetricKey")
        layout.addWidget(self._key, 0)
        layout.addStretch(1)
        layout.addWidget(widget, 0, Qt.AlignRight)


class RailSection(QFrame):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("RailCard")
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(12, 10, 12, 10)
        self.layout.setSpacing(6)
        self._heading = QLabel(title.upper())
        self._heading.setObjectName("RailCardTitle")
        self.layout.addWidget(self._heading)

    def set_title(self, title: str) -> None:
        self._heading.setText(title.upper())

    def add_row(self, widget: QWidget) -> None:
        self.layout.addWidget(widget)


class LeftRail(QWidget):
    analyzeToggled = Signal(bool)
    sourceProfileChanged = Signal(str)
    recordBaselineToggled = Signal()
    deviceSelected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("LeftRail")
        self.setMinimumWidth(268)
        self.setMaximumWidth(268)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 14, 12, 12)
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
        # Device picker: the capture card, or another app's virtual camera so
        # that app can record/stream the card while CheatVision analyses it.
        self.device_combo = QComboBox()
        self.device_combo.setObjectName("SourceCombo")
        self.device_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.device_combo.setMinimumContentsLength(18)
        self.device_combo.setToolTip(
            "Video device. The capture card serves one app at a time; pick a Virtual Camera "
            "(Streaming Center / OBS) to analyse while that app records."
        )
        self.device_combo.currentIndexChanged.connect(self._on_device_changed)
        self._source_mode = MetricRow("Mode")
        self._source_feed = MetricRow("Feed")
        self._source_backend = MetricRow("Pipe")
        self.profile_combo = QComboBox()
        self.profile_combo.setObjectName("SourceCombo")
        self.profile_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.profile_combo.setToolTip(
            "Source profile: ignore stream chrome / facecam and scene-gate kinematics."
        )
        for text, profile in (("HDMI GAME", "hdmi_game"), ("STREAM WINDOW", "stream_window"), ("VOD FILE", "vod_file")):
            self.profile_combo.addItem(text, profile)
        self.profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        self._profile_row = WidgetRow("Profile", self.profile_combo)
        self._ignore_row = MetricRow("Ignore")
        self._baseline_row = MetricRow("Base")
        self.source.add_row(self.device_combo)
        self.source.add_row(self._source_mode)
        self.source.add_row(self._source_feed)
        self.source.add_row(self._source_backend)
        self.source.add_row(self._profile_row)
        self.source.add_row(self._ignore_row)
        self.source.add_row(self._baseline_row)
        root.addWidget(self.source)

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
        self.incidents = RailSection("Incidents · 0")
        self.incident_table = IncidentQueueTable(self)
        self.incident_table.setMinimumHeight(90)
        self.incident_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.incidents.add_row(self.incident_table)
        root.addWidget(self.incidents, 1)
        self._incident_count = 0

    def _on_profile_changed(self, index: int) -> None:
        profile = self.profile_combo.itemData(index)
        if profile:
            self.sourceProfileChanged.emit(str(profile))

    def _on_device_changed(self, index: int) -> None:
        name = self.device_combo.itemData(index)
        if name:
            self.deviceSelected.emit(str(name))

    def set_devices(self, devices: list[dict], current_name: str) -> None:
        """Populate the picker; entries carry the DirectShow device name."""
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for device in devices:
            name = str(device.get("name", ""))
            if not name:
                continue
            kind = str(device.get("kind", "Input"))
            self.device_combo.addItem(f"{name}  ·  {kind.upper()}", name)
        index = self.device_combo.findData(current_name)
        if index >= 0:
            self.device_combo.setCurrentIndex(index)
        self.device_combo.blockSignals(False)

    def set_source_profile(self, profile: str) -> None:
        index = self.profile_combo.findData(profile)
        if index < 0 or index == self.profile_combo.currentIndex():
            return
        self.profile_combo.blockSignals(True)
        self.profile_combo.setCurrentIndex(index)
        self.profile_combo.blockSignals(False)

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
        # The device name is shown by the dropdown; keep it as a tooltip only.
        self.device_combo.setToolTip(name or self.device_combo.toolTip())
        if low_mode:
            self._source_mode.set_value("low mode", warn=True)
        else:
            self._source_mode.set_value(mode or "—")
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
