"""The session recorder is an independent slot beside the baseline recorder:
both receive frames, each stops without touching the other."""

from __future__ import annotations

import tempfile
import unittest
from unittest import mock

import numpy as np

from src.core import dataset_exporter
from src.core.dataset_exporter import PixelVisionDatasetExporter


class _FakeSession:
    """Stands in for _BaselineSession: no encoder, no thread."""

    def __init__(self, output_stem, fps, size, label="baseline"):
        self.output_path = output_stem.with_suffix(".mp4")
        self.label = label
        self.ok = True
        self.frames = 0
        self.frames_dropped = 0
        self.stopped = False

    def submit(self, frame) -> None:
        self.frames += 1

    def stop(self) -> None:
        self.stopped = True

    def is_alive(self) -> bool:
        return False

    def join(self, timeout: float) -> None:
        pass


class SessionRecordingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(dataset_exporter, "_BaselineSession", _FakeSession)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.exporter = PixelVisionDatasetExporter(target_resolution=(64, 64), project_root=self._tmp.name)
        self.frame = np.zeros((64, 64, 3), dtype=np.uint8)

    def test_paths_and_flags(self) -> None:
        path = self.exporter.start_session_recording()
        self.assertIn("recordings", path)
        self.assertIn("session_", path)
        self.assertTrue(self.exporter.is_recording_session)
        self.assertFalse(self.exporter.is_recording_baseline)
        self.assertEqual(self.exporter.start_session_recording(), "")  # already running
        self.exporter.stop_session_recording()
        self.assertFalse(self.exporter.is_recording_session)

    def test_both_recorders_receive_frames_independently(self) -> None:
        self.exporter.start_clean_baseline_mode()
        self.exporter.start_session_recording()
        baseline, session = self.exporter._session, self.exporter._session_rec
        self.exporter.write_frame(self.frame)
        self.exporter.write_frame(self.frame)
        self.assertEqual((baseline.frames, session.frames), (2, 2))

        self.exporter.stop_clean_baseline_mode()
        self.exporter.write_frame(self.frame)
        self.assertEqual(baseline.frames, 2)  # stopped: no more frames
        self.assertEqual(session.frames, 3)  # still recording
        self.assertTrue(baseline.stopped)
        self.assertFalse(session.stopped)
        self.exporter.stop_session_recording()
        self.assertTrue(session.stopped)

    def test_stop_without_start_is_a_no_op(self) -> None:
        self.exporter.stop_session_recording()
        self.exporter.write_frame(self.frame)  # must not raise
        self.assertFalse(self.exporter.is_recording_session)


if __name__ == "__main__":
    unittest.main()
