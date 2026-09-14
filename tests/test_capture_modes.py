"""MODE picker sources: only what the device advertised, only devices that exist."""

from __future__ import annotations

import unittest
from unittest import mock

from src.core import frame_source
from src.core.frame_source import discover_directshow_devices, selectable_modes_from_ranges


class SelectableModesTests(unittest.TestCase):
    def test_only_rates_inside_each_advertised_span(self) -> None:
        ranges = {(2560, 1440): (23.976, 144.001), (1280, 720): (50.0, 60.0002)}
        modes = selectable_modes_from_ranges(ranges)
        self.assertIn((2560, 1440, 144), modes)
        self.assertIn((2560, 1440, 120), modes)
        self.assertIn((1280, 720, 60), modes)
        self.assertIn((1280, 720, 50), modes)
        self.assertNotIn((1280, 720, 30), modes)  # below the advertised floor
        self.assertNotIn((1920, 1080, 60), modes)  # never advertised at all

    def test_span_ceiling_kept_when_no_standard_step_lands_on_it(self) -> None:
        modes = selectable_modes_from_ranges({(1920, 1080): (30.0, 100.0)})
        self.assertIn((1920, 1080, 100), modes)

    def test_bandwidth_ceiling_filters_capture_card_modes(self) -> None:
        # 3840*2160*3 bytes * 120 fps ~= 2.99 GB/s: over the raw-pipe ceiling; 60 fps is under it.
        ranges = {(3840, 2160): (24.0, 120.0)}
        modes = selectable_modes_from_ranges(ranges)
        self.assertNotIn((3840, 2160, 120), modes)
        self.assertIn((3840, 2160, 60), modes)
        unfiltered = selectable_modes_from_ranges(ranges, apply_bandwidth_ceiling=False)
        self.assertIn((3840, 2160, 120), unfiltered)

    def test_sorted_largest_resolution_then_fastest(self) -> None:
        modes = selectable_modes_from_ranges({(1280, 720): (60.0, 60.0), (2560, 1440): (60.0, 144.0)})
        self.assertEqual(modes[0], (2560, 1440, 144))
        self.assertEqual(modes[-1], (1280, 720, 60))

    def test_nothing_advertised_offers_nothing(self) -> None:
        self.assertEqual(selectable_modes_from_ranges({}), [])


class DeviceDiscoveryTests(unittest.TestCase):
    def test_lists_every_present_device_with_no_cap(self) -> None:
        names = [f"Device {i}" for i in range(8)]
        with mock.patch.object(frame_source, "_list_directshow_video_names", return_value=names):
            devices = discover_directshow_devices()
        self.assertEqual([d["name"] for d in devices], names)

    def test_no_devices_gives_one_empty_placeholder_not_fakes(self) -> None:
        with mock.patch.object(frame_source, "_list_directshow_video_names", return_value=[]), mock.patch.object(
            frame_source, "ffmpeg_on_path", return_value=True
        ):
            devices = discover_directshow_devices()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["name"], "")
        self.assertIn("No video devices", str(devices[0]["label"]))

    def test_placeholder_is_never_picked_as_a_device(self) -> None:
        placeholder = [{"label": "No video devices found", "name": "", "index": 0, "kind": "Input"}]
        self.assertIsNone(frame_source.pick_preferred_capture_device(placeholder))
        real = placeholder + [{"label": "Cam (DirectShow 1)", "name": "Cam", "index": 1, "kind": "Webcam"}]
        self.assertEqual(frame_source.pick_preferred_capture_device(real)["name"], "Cam")

    def test_missing_ffmpeg_is_named_as_the_reason(self) -> None:
        with mock.patch.object(frame_source, "_list_directshow_video_names", return_value=[]), mock.patch.object(
            frame_source, "ffmpeg_on_path", return_value=False
        ):
            devices = discover_directshow_devices()
        self.assertEqual(devices[0]["name"], "")
        self.assertIn("ffmpeg", str(devices[0]["label"]))


if __name__ == "__main__":
    unittest.main()
