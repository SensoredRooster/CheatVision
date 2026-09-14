"""Devices are shown by their own Windows names; the vendor only decides the capture path.
Per-machine choices live in a git-ignored local settings file layered over the shipped one."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.app import load_settings
from src.core.frame_source import infer_device_kind
from src.ui.left_rail import _short_device_name


class DeviceKindTests(unittest.TestCase):
    def test_capture_cards_from_several_vendors(self) -> None:
        for name in (
            "AVerMedia GC573 1",
            "Elgato Game Capture HD60 X",
            "Magewell USB Capture HDMI Gen 2",
            "Razer Ripsaw HD",
            "Hauppauge HD PVR 2",
            "Blackmagic WDM Capture",
            "USB3.0 HD Video Capture",
            "Cam Link 4K",
            "Live Streamer CAP 4K",
        ):
            self.assertEqual(infer_device_kind(name), "Capture Card", name)

    def test_virtual_cameras_win_over_everything(self) -> None:
        for name in ("OBS Virtual Camera", "Streaming Center Virtual Camera", "Streamlabs Desktop Virtual Webcam"):
            self.assertEqual(infer_device_kind(name), "Virtual Camera", name)

    def test_webcams_and_unknowns(self) -> None:
        self.assertEqual(infer_device_kind("Logitech BRIO"), "Webcam")
        self.assertEqual(infer_device_kind("Razer Kiyo Pro"), "Webcam")
        self.assertEqual(infer_device_kind("USB Video Device"), "Input")


class ShortNameTests(unittest.TestCase):
    def test_vendor_boilerplate_trimmed_but_model_kept(self) -> None:
        self.assertEqual(_short_device_name("AVerMedia GC573 1"), "GC573 1")
        self.assertEqual(_short_device_name("Elgato Game Capture HD60 X"), "HD60 X")
        self.assertEqual(_short_device_name("Magewell USB Capture HDMI"), "USB Capture HDMI")
        self.assertEqual(_short_device_name("Razer Ripsaw HD"), "Ripsaw HD")
        self.assertEqual(_short_device_name("Streaming Center Virtual Camera"), "StreamCenter VCam")
        self.assertEqual(_short_device_name("OBS Virtual Camera"), "OBS VCam")


class LocalSettingsOverlayTests(unittest.TestCase):
    def _write(self, root: Path, name: str, payload) -> None:
        (root / "config").mkdir(exist_ok=True)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        (root / "config" / name).write_text(text, encoding="utf-8")

    def test_local_file_layers_over_shipped_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "settings.json", {"capture_fps": 144, "game_profile": "warzone"})
            self._write(root, "settings.local.json", {"capture_device_name": "My Card", "capture_fps": 120})
            settings = load_settings(root)
        self.assertEqual(settings["capture_device_name"], "My Card")
        self.assertEqual(settings["capture_fps"], 120)
        self.assertEqual(settings["game_profile"], "warzone")
        self.assertEqual(settings["project_root"], str(root))

    def test_missing_or_broken_local_file_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "settings.json", {"capture_fps": 144})
            self.assertEqual(load_settings(root)["capture_fps"], 144)
            self._write(root, "settings.local.json", "{not json")
            self.assertEqual(load_settings(root)["capture_fps"], 144)
            self._write(root, "settings.local.json", [1, 2, 3])
            self.assertEqual(load_settings(root)["capture_fps"], 144)


if __name__ == "__main__":
    unittest.main()
