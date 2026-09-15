"""SOURCE lists every input once; MASK is a separate live picker; the browser
window is an input of its own rather than a profile that hijacks a device."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

_APP = QApplication.instance() or QApplication([])

from src.core.frame_source import BROWSER_WINDOW_DEVICE, FrameSource, is_browser_window_device  # noqa: E402
from src.ui.left_rail import LeftRail  # noqa: E402

_DEVICES = [
    {"label": "AVerMedia HD Capture GC573 1 (DirectShow 1)", "name": "AVerMedia HD Capture GC573 1", "index": 1, "kind": "Capture Card"},
    {"label": "Streaming Center Virtual Camera (DirectShow 2)", "name": "Streaming Center Virtual Camera", "index": 2, "kind": "Virtual Camera"},
    {"label": "Twitch Virtual Cam (DirectShow 3)", "name": "Twitch Virtual Cam", "index": 3, "kind": "Virtual Camera"},
    {"label": "Logitech BRIO (DirectShow 0)", "name": "Logitech BRIO", "index": 0, "kind": "Webcam"},
]


def _items(combo) -> list[tuple[str, object]]:
    return [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())]


class SourceSelectorTests(unittest.TestCase):
    def test_one_entry_per_input_plus_browser_window(self) -> None:
        rail = LeftRail()
        rail.set_devices(_DEVICES + [dict(BROWSER_WINDOW_DEVICE)], "AVerMedia HD Capture GC573 1")
        items = _items(rail.source_combo)
        self.assertEqual(len(items), 5)  # was 11: two or three profile copies of every device
        self.assertEqual([data for _, data in items], [d["name"] for d in _DEVICES] + ["Browser window"])
        self.assertEqual(len({data for _, data in items}), 5)
        self.assertEqual(items[0][0], "GC573 1")
        self.assertEqual(items[1][0], "StreamCenter VCam")
        self.assertEqual(items[3][0], "BRIO")  # a webcam is an input too; listed once
        self.assertEqual(rail.source_combo.currentData(), "AVerMedia HD Capture GC573 1")

    def test_browser_window_listed_last_even_if_given_first(self) -> None:
        rail = LeftRail()
        rail.set_devices([dict(BROWSER_WINDOW_DEVICE)] + _DEVICES, "Browser window")
        items = _items(rail.source_combo)
        self.assertEqual(items[-1][1], "Browser window")
        self.assertEqual(rail.source_combo.currentData(), "Browser window")

    def test_no_devices_shows_reason_unselectable_and_still_offers_browser(self) -> None:
        rail = LeftRail()
        placeholder = {"label": "ffmpeg not found on PATH (install it, then RESCAN)", "name": "", "index": 0, "kind": "Input"}
        rail.set_devices([placeholder, dict(BROWSER_WINDOW_DEVICE)], "")
        items = _items(rail.source_combo)
        self.assertEqual(len(items), 2)
        self.assertIn("ffmpeg", items[0][0])
        self.assertIsNone(items[0][1])
        self.assertFalse(rail.source_combo.model().item(0).isEnabled())
        self.assertEqual(items[1][1], "Browser window")

    def test_selecting_an_entry_emits_the_device_name_only(self) -> None:
        rail = LeftRail()
        rail.set_devices(_DEVICES, "AVerMedia HD Capture GC573 1")
        got: list[str] = []
        rail.sourceSelected.connect(lambda name: got.append(name))
        rail.source_combo.setCurrentIndex(3)
        self.assertEqual(got, ["Logitech BRIO"])


class MaskPickerTests(unittest.TestCase):
    def test_two_masks_mapping_to_source_profiles(self) -> None:
        rail = LeftRail()
        self.assertEqual(_items(rail.mask_combo), [("GAME", "hdmi_game"), ("STREAM", "stream_window")])

    def test_set_mask_profile_reflects_without_emitting_and_folds_vod_into_stream(self) -> None:
        rail = LeftRail()
        got: list[str] = []
        rail.maskSelected.connect(lambda p: got.append(p))
        rail.set_mask_profile("vod_file")
        self.assertEqual(rail.mask_combo.currentData(), "stream_window")
        rail.set_mask_profile("hdmi_game")
        self.assertEqual(rail.mask_combo.currentData(), "hdmi_game")
        self.assertEqual(got, [])

    def test_user_pick_emits_and_lock_disables(self) -> None:
        rail = LeftRail()
        got: list[str] = []
        rail.maskSelected.connect(lambda p: got.append(p))
        rail.mask_combo.setCurrentIndex(1)
        self.assertEqual(got, ["stream_window"])
        rail.set_mask_profile("stream_window", locked=True)
        self.assertFalse(rail.mask_combo.isEnabled())
        rail.set_mask_profile("hdmi_game")
        self.assertTrue(rail.mask_combo.isEnabled())

    def test_ignore_count_lives_in_the_mask_tooltip(self) -> None:
        rail = LeftRail()
        rail.set_profile("hdmi_game", 1, "idle")
        self.assertIn("Ignoring 1 region right now", rail.mask_combo.toolTip())
        rail.set_profile("stream_window", 4, "rec")
        self.assertIn("Ignoring 4 regions right now", rail.mask_combo.toolTip())
        self.assertEqual(rail.mask_combo.currentData(), "stream_window")


class BrowserWindowInputTests(unittest.TestCase):
    def test_browser_input_is_chosen_by_kind_not_by_profile(self) -> None:
        browser = FrameSource({"capture_mode": "camera", "capture_device_name": "Browser window", "capture_device_kind": "Browser Window"})
        self.assertTrue(browser._follow_browser)
        self.assertEqual(browser.mode, "screen")
        browser.close()  # releases the mss screen handle it opened
        # A capture card with the STREAM mask still reads the card: the mask
        # no longer hijacks the device into a screen grab.
        card = FrameSource(
            {"capture_mode": "camera", "capture_device_name": "AVerMedia HD Capture GC573 1", "capture_device_kind": "Capture Card", "source_profile": "stream_window"}
        )
        self.assertFalse(card._follow_browser)
        self.assertEqual(card.mode, "camera")

    def test_kind_predicate(self) -> None:
        self.assertTrue(is_browser_window_device(dict(BROWSER_WINDOW_DEVICE)))
        self.assertFalse(is_browser_window_device(_DEVICES[0]))
        self.assertFalse(is_browser_window_device(None))


if __name__ == "__main__":
    unittest.main()
