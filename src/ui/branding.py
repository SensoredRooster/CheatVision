"""Brand assets: the crosshair mark, the window icon, and the brand type.

The files live in assets/brand/ (see BRAND.md there). Loading is cached, and
a missing file degrades to an empty pixmap or icon rather than an exception so
the UI still comes up without its artwork.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QIcon, QPixmap

from src.ui.theme import BRAND_FONT_FAMILY

BRAND_DIR = Path(__file__).resolve().parents[2] / "assets" / "brand"
MARK_PATH = BRAND_DIR / "cheatvision_mark.png"
ICON_PATH = BRAND_DIR / "cheatvision.ico"
# The wordmark is set in type beside the mark; the two halves are coloured to
# echo the red letters on the charcoal disc.
WORDMARK_LEFT = "CHEAT"
WORDMARK_RIGHT = "VISION"
TAGLINE = "AIM REVIEW CONSOLE"


@lru_cache(maxsize=1)
def _mark_source() -> QPixmap:
    # isNull() when the asset is missing; callers treat that as "no mark".
    return QPixmap(str(MARK_PATH))


@lru_cache(maxsize=32)
def brand_pixmap(size: int, device_pixel_ratio: float = 1.0) -> QPixmap:
    """The mark at `size` logical px square, rendered for the given screen
    ratio so it stays crisp under Windows display scaling."""
    source = _mark_source()
    if source.isNull() or size <= 0:
        return QPixmap()
    ratio = max(1.0, float(device_pixel_ratio))
    physical = max(1, int(round(size * ratio)))
    pixmap = source.scaled(physical, physical, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


def brand_icon() -> QIcon:
    """Window and taskbar icon: the multi-size .ico, else the PNG mark."""
    if ICON_PATH.is_file():
        icon = QIcon(str(ICON_PATH))
        if not icon.isNull():
            return icon
    source = _mark_source()
    return QIcon(source) if not source.isNull() else QIcon()


def brand_font(pixel_size: int, tracking: float, weight: QFont.Weight = QFont.Weight.DemiBold) -> QFont:
    """Bahnschrift with letter tracking, for the wordmark, card titles and the
    SIGNAL state. Stylesheets cannot express tracking, so these labels carry
    their font in code and the sheet only colours them."""
    font = QFont(BRAND_FONT_FAMILY)
    font.setPixelSize(int(pixel_size))
    font.setWeight(weight)
    font.setLetterSpacing(QFont.AbsoluteSpacing, float(tracking))
    return font
