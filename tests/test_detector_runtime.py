"""The detector picks its runtime sensibly: GPU when DirectML is present, CPU
otherwise, a forced GPU that is unavailable falls back rather than failing,
the CPU pool leaves half the machine to the video path, and the letterbox
follows whatever input size the model was exported with."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.object_detector import PixelVisionObjectDetector, auto_threads

MODEL = ROOT / "data" / "models" / "yolov8n.onnx"


class ThreadPolicyTests(unittest.TestCase):
    def test_half_the_machine_minus_a_margin_between_2_and_8(self) -> None:
        self.assertEqual(auto_threads(20), 8)
        self.assertEqual(auto_threads(16), 6)
        self.assertEqual(auto_threads(12), 4)
        self.assertEqual(auto_threads(8), 2)
        self.assertEqual(auto_threads(2), 2)
        self.assertEqual(auto_threads(None), 2)

    def test_explicit_threads_win(self) -> None:
        det = PixelVisionObjectDetector(model_path="does/not/exist.onnx", player_class_ids=[0], threads=3)
        self.assertEqual(det.threads, 3)
        det.ort_session = None  # a missing path falls back to the default weights when they exist
        self.assertEqual(det.describe(), "")  # no model, no label


@unittest.skipUnless(MODEL.is_file(), "player detector not fetched on this machine")
class DetectorRuntimeTests(unittest.TestCase):
    def test_cpu_can_always_be_forced(self) -> None:
        det = PixelVisionObjectDetector(model_path=str(MODEL), player_class_ids=[0], provider="cpu")
        self.assertIsNotNone(det.ort_session)
        self.assertEqual(det.provider_name, "CPU")
        self.assertTrue(det.describe().endswith("CPU"))

    def test_auto_never_leaves_the_model_unloaded(self) -> None:
        det = PixelVisionObjectDetector(model_path=str(MODEL), player_class_ids=[0], provider="auto")
        self.assertIsNotNone(det.ort_session)
        self.assertIn(det.provider_name, ("GPU", "CPU"))

    def test_forced_gpu_falls_back_when_directml_is_absent(self) -> None:
        import onnxruntime as ort

        det = PixelVisionObjectDetector(model_path=str(MODEL), player_class_ids=[0], provider="directml")
        self.assertIsNotNone(det.ort_session)
        expected = "GPU" if "DmlExecutionProvider" in ort.get_available_providers() else "CPU"
        self.assertEqual(det.provider_name, expected)

    def test_letterbox_follows_the_models_input(self) -> None:
        det = PixelVisionObjectDetector(model_path=str(MODEL), player_class_ids=[0], provider="cpu")
        padded, scale, pad_x, pad_y = det._apply_letterbox(np.zeros((540, 960, 3), dtype=np.uint8))
        self.assertEqual(padded.shape[:2], (det.input_h, det.input_w))
        self.assertAlmostEqual(scale, min(det.input_w / 960, det.input_h / 540))
        self.assertEqual(pad_x, (det.input_w - int(960 * scale)) // 2)
        self.assertEqual(pad_y, (det.input_h - int(540 * scale)) // 2)


if __name__ == "__main__":
    unittest.main()
