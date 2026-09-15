"""CheatVision look: the crosshair mark's crimson on charcoal.

Sampled from the brand mark (assets/brand/cheatvision_mark.png): the ring and
the CV letterforms are #CB2B30, the disc behind them #1E2327. Everything else
is derived from those two -- neutral charcoals for surfaces (no blue or green
cast), one red for chrome and the straightness trace, a hotter red kept for
alerts only, amber for warnings, white for what is fine. Red is the brand, so
it is spent sparingly: edges and titles, not fills, until something is
flagged. The full sheet is assets/brand/BRAND.md.
"""
from __future__ import annotations

# Charcoal scale (neutral, no tint)
BACKGROUND = "#0B0C0E"  # window, canvas idle
SURFACE = "#111315"  # top and bottom bars
PANEL = "#15181B"  # rail, inset metric rows, table
PANEL_ALT = "#1E2327"  # cards and controls -- the disc of the mark
HAIRLINE = "#2A3036"
BORDER = "#3B434A"

# Brand red and its states
ACCENT = "#CB2B30"  # the mark's ring and letters
ACCENT_BRIGHT = "#E63946"  # hover on a checked button
ACCENT_EDGE = "#8E1F24"  # resting border of a control
ACCENT_DIM = "#4A1417"  # hover / selection fill
ACCENT_DEEP = "#2E0D0F"  # pressed

# Text
TEXT_PRIMARY = "#EDEFF1"
TEXT_MUTED = "#8C949C"
TEXT_ON_ACCENT = "#FFFFFF"

# States. "Fine" is white, not green: red is reserved for a flag, so the
# healthy state needs no colour of its own.
OK = TEXT_PRIMARY
ALERT = "#FF4D57"
WARNING = "#F2A93B"
VOD_ACCENT = "#D4A64A"
# The tremor trace on the SIGNAL graph: the human evidence, kept neutral so
# the red straightness line is the only thing that reads as "watch this".
TRACE = "#7F8A94"

CANVAS_IDLE_COLOR = BACKGROUND

# Bahnschrift ships with Windows 10/11; Qt falls through the list otherwise.
BRAND_FONT_FAMILY = "Bahnschrift"
BRAND_FONT = f'"{BRAND_FONT_FAMILY}", "Segoe UI", Arial, sans-serif'
MONO_FONT = '"Cascadia Mono", "Consolas", monospace'

# Containers get no background from the generic rule: a plain QWidget with a
# stylesheet background paints it, which used to turn every layout container
# (metric rows, the button block in the top bar) into a darker rectangle.
# Surfaces that should have a colour name themselves below.
APP_STYLESHEET = f"""
QMainWindow, QDialog {{
    background-color: {BACKGROUND};
}}

QWidget {{
    color: {TEXT_PRIMARY};
    font-family: "Segoe UI", Arial, sans-serif;
}}

QToolTip {{
    background-color: {PANEL_ALT};
    color: {TEXT_PRIMARY};
    border: 1px solid {ACCENT_EDGE};
    padding: 4px 6px;
}}

QWidget#ControlBar {{
    background-color: {SURFACE};
    border-bottom: 1px solid {HAIRLINE};
}}

QWidget#PlaybackControlsBar {{
    background-color: {SURFACE};
    border-top: 1px solid {HAIRLINE};
}}

QLabel#StatusLabel {{
    color: {TEXT_PRIMARY};
    font-size: 11px;
    font-weight: bold;
    padding: 0 8px;
}}

QPushButton {{
    background-color: {PANEL_ALT};
    color: {TEXT_PRIMARY};
    border: 1px solid {ACCENT_EDGE};
    border-radius: 3px;
    padding: 4px 12px;
    font-size: 11px;
    font-weight: bold;
}}

QPushButton:hover {{
    background-color: {ACCENT_DIM};
    border-color: {ACCENT};
}}

QPushButton:pressed {{
    background-color: {ACCENT_DEEP};
    border-color: {ACCENT};
}}

QPushButton:checked {{
    background-color: {ACCENT};
    color: {TEXT_ON_ACCENT};
    border-color: {ACCENT};
}}

QPushButton:checked:hover {{
    background-color: {ACCENT_BRIGHT};
    border-color: {ACCENT_BRIGHT};
}}

QPushButton:disabled {{
    color: {TEXT_MUTED};
    background-color: {PANEL};
    border-color: {HAIRLINE};
}}

/* The three buttons over the tool rail share its 268px: tight padding so all
   three labels fit at equal width, matching the RECORD CLEAN BASELINE button. */
QPushButton#MountButton, QPushButton#RailButton {{
    padding: 3px 4px;
    font-size: 10px;
    min-width: 0px;
}}

QPushButton#BaselineButton {{
    padding: 4px 12px;
    font-size: 10px;
}}

QFrame#ControlGroup {{
    background-color: {PANEL_ALT};
    border: 1px solid {HAIRLINE};
    border-radius: 4px;
}}

QLabel#ControlGroupLabel {{
    color: {TEXT_MUTED};
    font-size: 10px;
    font-weight: bold;
}}

QSplitter::handle {{
    background-color: {HAIRLINE};
}}

QSplitter::handle:horizontal {{
    width: 1px;
}}

QWidget#LeftRail {{
    background-color: {PANEL};
    border-right: 1px solid {HAIRLINE};
}}

/* Brand lockup, card titles and the SIGNAL state carry their fonts in code
   (Bahnschrift with letter tracking, which stylesheets cannot express); the
   sheet only colours them. */
QLabel#RailBrand {{
    color: {TEXT_PRIMARY};
}}

QFrame#RailCard {{
    background-color: {PANEL_ALT};
    border: 1px solid {HAIRLINE};
    border-radius: 6px;
}}

QLabel#RailCardTitle {{
    color: {ACCENT};
}}

QWidget#RailMetricRow {{
    background-color: {PANEL};
    border-radius: 3px;
}}

QLabel#RailMetricKey {{
    color: {TEXT_MUTED};
    font-size: 10px;
    font-weight: bold;
}}

QLabel#RailMetricVal {{
    color: {TEXT_PRIMARY};
    font-size: 11px;
    font-weight: bold;
    font-family: {MONO_FONT};
}}

QLabel#RailStatusOk {{
    color: {OK};
}}

QLabel#RailStatusAlert {{
    color: {ALERT};
}}

QCheckBox#RailCheck {{
    color: {TEXT_PRIMARY};
    font-size: 10px;
    font-weight: bold;
    spacing: 8px;
}}

QCheckBox#RailCheck::indicator {{
    width: 12px;
    height: 12px;
    border: 1px solid {ACCENT_EDGE};
    border-radius: 2px;
    background: {PANEL};
}}

QCheckBox#RailCheck::indicator:hover {{
    border-color: {ACCENT};
}}

QCheckBox#RailCheck::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}

QComboBox {{
    background-color: {PANEL_ALT};
    color: {TEXT_PRIMARY};
    border: 1px solid {ACCENT_EDGE};
    border-radius: 3px;
    padding: 3px 8px;
    font-size: 11px;
    font-weight: bold;
    min-width: 64px;
}}

QComboBox:hover {{
    border-color: {ACCENT};
}}

QComboBox:on {{
    border-color: {ACCENT};
    background-color: {ACCENT_DIM};
}}

QComboBox#SourceCombo {{
    font-size: 10px;
    padding: 2px 6px;
    min-width: 0px;
}}

QComboBox::drop-down {{
    border: none;
    width: 14px;
}}

QComboBox QAbstractItemView {{
    background-color: {PANEL_ALT};
    color: {TEXT_PRIMARY};
    border: 1px solid {ACCENT_EDGE};
    selection-background-color: {ACCENT_DIM};
    selection-color: {TEXT_PRIMARY};
    outline: none;
}}

QSlider::groove:horizontal {{
    background: {HAIRLINE};
    height: 4px;
    border-radius: 2px;
}}

QSlider::sub-page:horizontal {{
    background: {ACCENT};
    border-radius: 2px;
}}

QSlider::handle:horizontal {{
    background: {TEXT_PRIMARY};
    border: 2px solid {ACCENT};
    width: 10px;
    margin: -5px 0;
    border-radius: 7px;
}}

QSlider::handle:horizontal:hover {{
    border-color: {ACCENT_BRIGHT};
}}

QTableView {{
    background-color: {PANEL};
    alternate-background-color: {PANEL_ALT};
    gridline-color: {PANEL};
    border: 1px solid {HAIRLINE};
    selection-background-color: {ACCENT_DIM};
    selection-color: {TEXT_PRIMARY};
    font-size: 11px;
}}

QHeaderView::section {{
    background-color: {PANEL};
    color: {TEXT_MUTED};
    padding: 4px;
    border: none;
    border-bottom: 1px solid {HAIRLINE};
    font-size: 10px;
    font-weight: bold;
}}

QScrollBar:vertical {{
    background: {PANEL};
    width: 8px;
    margin: 0px;
}}

QScrollBar::handle:vertical {{
    background: {BORDER};
    border-radius: 4px;
    min-height: 24px;
}}

QScrollBar::handle:vertical:hover {{
    background: {ACCENT_EDGE};
}}

QScrollBar:horizontal {{
    background: {PANEL};
    height: 8px;
    margin: 0px;
}}

QScrollBar::handle:horizontal {{
    background: {BORDER};
    border-radius: 4px;
    min-width: 24px;
}}

QScrollBar::add-line, QScrollBar::sub-line {{
    width: 0px;
    height: 0px;
}}

QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent;
}}
"""
