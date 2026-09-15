"""The setup checker runs on a bare interpreter and the requirements split keeps
the heavy training extras out of the app's install."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import setup_check  # noqa: E402


class SetupCheckTests(unittest.TestCase):
    def test_every_check_runs_and_reports(self) -> None:
        results = setup_check.run_checks()
        names = [check.name for check in results]
        for expected in ("Windows", "Python", "Environment", "Packages", "ffmpeg", "Capture devices", "Player detector", "Settings", "Folders"):
            self.assertIn(expected, names)
        for check in results:
            self.assertTrue(check.detail, check.name)
            if not check.ok:
                self.assertTrue(check.fix, f"{check.name} failed without a fix")

    def test_python_and_packages_pass_here(self) -> None:
        # The suite itself needs these, so they must read as present.
        self.assertTrue(setup_check.check_python().ok)
        self.assertTrue(setup_check.check_packages().ok)
        self.assertTrue(setup_check.check_settings().ok)
        self.assertTrue(setup_check.check_folders().ok)

    def test_model_check_matches_disk(self) -> None:
        path = setup_check.model_path()
        self.assertEqual(setup_check.check_model().ok, path.is_file() and path.stat().st_size >= setup_check.MIN_MODEL_BYTES)
        self.assertFalse(setup_check.check_model().required)  # the app runs without it

    def test_tags(self) -> None:
        self.assertEqual(setup_check.Check("x", True, "d").tag, "[OK]")
        self.assertEqual(setup_check.Check("x", False, "d").tag, "[!!]")
        self.assertEqual(setup_check.Check("x", False, "d", required=False).tag, "[--]")


class RequirementsSplitTests(unittest.TestCase):
    def test_runtime_file_has_no_training_packages(self) -> None:
        runtime = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        for heavy in ("torch", "ultralytics"):
            self.assertNotRegex(runtime, rf"(?m)^{heavy}\b", heavy)
        for needed in ("opencv-python", "numpy", "PySide6", "mss", "onnxruntime"):
            self.assertRegex(runtime, rf"(?m)^{needed}\b", needed)

    def test_training_file_has_them(self) -> None:
        train = (ROOT / "requirements-train.txt").read_text(encoding="utf-8")
        self.assertRegex(train, r"(?m)^torch\b")
        self.assertRegex(train, r"(?m)^ultralytics\b")

    def test_setup_scripts_exist(self) -> None:
        self.assertTrue((ROOT / "setup.bat").is_file())
        self.assertTrue((ROOT / "run.bat").is_file())


if __name__ == "__main__":
    unittest.main()
