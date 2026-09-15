"""Brand assets ship with the app and the palette is what the brand sheet says."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.ui import theme
from src.ui.branding import BRAND_DIR, ICON_PATH, MARK_PATH

_HEX = re.compile(r"^#[0-9A-F]{6}$")


class BrandAssetTests(unittest.TestCase):
    def test_mark_and_icon_are_present(self) -> None:
        self.assertTrue(MARK_PATH.is_file(), MARK_PATH)
        self.assertTrue(ICON_PATH.is_file(), ICON_PATH)
        self.assertGreater(MARK_PATH.stat().st_size, 10_000)
        self.assertGreater(ICON_PATH.stat().st_size, 5_000)
        self.assertTrue((BRAND_DIR / "BRAND.md").is_file())

    def test_mark_is_square_with_transparent_corners(self) -> None:
        mark = cv2.imread(str(MARK_PATH), cv2.IMREAD_UNCHANGED)
        self.assertIsNotNone(mark)
        height, width, channels = mark.shape
        self.assertEqual((height, width, channels), (512, 512, 4))
        alpha = mark[:, :, 3]
        self.assertEqual(int(alpha[0, 0]), 0)
        self.assertEqual(int(alpha[-1, -1]), 0)
        self.assertGreater(int(alpha[256, 256]), 200)  # the disc is opaque at the centre


class PaletteTests(unittest.TestCase):
    def test_every_colour_constant_is_a_six_digit_hex(self) -> None:
        colours = {
            name: value
            for name, value in vars(theme).items()
            if name.isupper() and isinstance(value, str) and value.startswith("#")
        }
        self.assertGreaterEqual(len(colours), 15)
        for name, value in colours.items():
            self.assertRegex(value, _HEX, name)

    def test_brand_red_and_disc_match_the_artwork(self) -> None:
        self.assertEqual(theme.ACCENT, "#CB2B30")
        self.assertEqual(theme.PANEL_ALT, "#1E2327")
        # The healthy state is white, never green, and alerts are not the brand red.
        self.assertEqual(theme.OK, theme.TEXT_PRIMARY)
        self.assertNotEqual(theme.ALERT, theme.ACCENT)

    def test_stylesheet_uses_the_palette_only(self) -> None:
        sheet = theme.APP_STYLESHEET
        self.assertNotIn("#2EE6C7", sheet)  # the retired teal accent
        self.assertNotIn("letter-spacing", sheet)  # not a stylesheet property; done in code
        palette = {v.upper() for k, v in vars(theme).items() if k.isupper() and isinstance(v, str) and v.startswith("#")}
        for colour in set(re.findall(r"#[0-9A-Fa-f]{6}", sheet)):
            self.assertIn(colour.upper(), palette, colour)


if __name__ == "__main__":
    unittest.main()
