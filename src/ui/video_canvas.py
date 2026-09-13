from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QSizePolicy, QWidget

from src.core.anti_cheat_pipeline import FrameContext
from src.ui.theme import ACCENT, CANVAS_IDLE_COLOR


class VideoCanvas(QWidget):
    """Letterboxed frame display.

    Holds references to both the full FrameContext and the rendered numpy frame
    because QImage(Format_BGR888) wraps the backing numpy buffer without
    copying it.

    Emits ``viewportResized(w, h)`` whenever its own geometry changes so the
    render worker can fit frames to the real canvas. The main window's
    resizeEvent is not enough: the first layout after show(), splitter drags
    and the TOOLS toggle all change this widget without resizing the window,
    which left the worker rendering at its 320x180 placeholder size and the
    canvas upscaling that ~4x (blurry until the user happened to resize).
    """

    viewportResized = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._ctx: FrameContext | None = None
        self._frame_buffer = None
        self._image: QImage | None = None
        self._idle_text = "No capture device detected — Mount a gameplay recording to begin"

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        size = event.size()
        if size.width() > 0 and size.height() > 0:
            self.viewportResized.emit(size.width(), size.height())

    def set_idle_text(self, text: str) -> None:
        self._idle_text = text
        if self._image is None:
            self.update()

    def set_frame(self, ctx: FrameContext, frame) -> None:
        self._ctx = ctx
        self._frame_buffer = frame
        height, width = frame.shape[:2]
        self._image = QImage(frame.data, width, height, frame.strides[0], QImage.Format_BGR888)
        self.update()

    def clear_frame(self) -> None:
        self._ctx = None
        self._frame_buffer = None
        self._image = None
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.fillRect(self.rect(), QColor(CANVAS_IDLE_COLOR))

        if self._image is None:
            painter.setPen(QColor(ACCENT))
            painter.drawText(self.rect(), Qt.AlignCenter, self._idle_text)
            painter.end()
            return

        painter.drawImage(self._letterbox_rect(self._image.size(), self.size()), self._image)
        painter.end()

    def _letterbox_rect(self, image_size: QSize, widget_size: QSize) -> QRect:
        if image_size.width() <= 0 or image_size.height() <= 0:
            return QRect(0, 0, widget_size.width(), widget_size.height())

        scale = min(widget_size.width() / image_size.width(), widget_size.height() / image_size.height())
        render_w = max(1, int(image_size.width() * scale))
        render_h = max(1, int(image_size.height() * scale))
        x = (widget_size.width() - render_w) // 2
        y = (widget_size.height() - render_h) // 2
        return QRect(x, y, render_w, render_h)
