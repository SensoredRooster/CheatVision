"""The analysis cadence follows the feed: 60, 144 and 240 new pictures a second
are scored at the same rate, and the per-step aim thresholds keep their
real-world meaning at whatever cadence results."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.anomaly_detector import REFERENCE_SAMPLE_HZ, CrosshairKinematicsAnalyzer
from src.core.anti_cheat_pipeline import AntiCheatPipeline, FrameContext
from test_pipeline_recall import H, W, _render, _world


def _pipeline(**extra) -> AntiCheatPipeline:
    return AntiCheatPipeline(
        log_dir=tempfile.mkdtemp(),
        analysis_stride=3,
        target_resolution=(960, 540),
        player_detector_model_path="does/not/exist.onnx",
        game_profile="generic",
        **extra,
    )


def _textured_field(height: int, width: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    noise = rng.integers(40, 220, size=(height, width), dtype=np.uint8)
    return cv2.GaussianBlur(noise, (31, 31), 0)


def _pan(analyzer: CrosshairKinematicsAnalyzer, hz: float, px_per_step: int, seconds: float) -> dict | None:
    """A ruler-straight constant-speed pan sampled at `hz`; the first flagged metrics, if any."""
    base = _textured_field(540, 960, seed=2)
    for i in range(int(round(seconds * hz))):
        shifted = np.roll(base, shift=px_per_step * i, axis=1)
        result = analyzer.update(cv2.cvtColor(shifted, cv2.COLOR_GRAY2BGR), timestamp=i / hz)
        if result and result.get("flagged"):
            return result
    return None


class StrideFollowsFeedTests(unittest.TestCase):
    def test_reference_feed_keeps_the_configured_stride(self) -> None:
        pipe = _pipeline()
        pipe.set_feed_rate(60.0)
        self.assertEqual(pipe.analysis_stride, 3)
        self.assertAlmostEqual(pipe.analysis_cadence_hz, 20.0)
        self.assertAlmostEqual(pipe.crosshair_analyzer.rate_scale, 1.0)

    def test_high_refresh_feeds_land_near_the_target_cadence(self) -> None:
        for fps, stride in ((120.0, 6), (144.0, 7), (240.0, 12), (360.0, 18)):
            pipe = _pipeline()
            pipe.set_feed_rate(fps)
            self.assertEqual(pipe.analysis_stride, stride, fps)
            self.assertTrue(14.0 <= pipe.analysis_cadence_hz <= 28.0, (fps, pipe.analysis_cadence_hz))

    def test_slow_vod_analyses_more_of_its_frames(self) -> None:
        pipe = _pipeline()
        pipe.set_feed_rate(30.0)
        self.assertEqual(pipe.analysis_stride, 2)

    def test_jitter_inside_the_band_does_not_flap(self) -> None:
        pipe = _pipeline()
        for fps in (60.0, 66.0, 71.0, 59.0, 70.6):
            pipe.set_feed_rate(fps)
            self.assertEqual(pipe.analysis_stride, 3, fps)
        pipe.set_feed_rate(144.0)
        self.assertEqual(pipe.analysis_stride, 7)
        pipe.set_feed_rate(130.0)  # 18.6 Hz with stride 7: still inside the band
        self.assertEqual(pipe.analysis_stride, 7)

    def test_target_cadence_is_configurable(self) -> None:
        pipe = _pipeline(analysis_rate_hz=48.0)
        pipe.set_feed_rate(144.0)
        self.assertEqual(pipe.analysis_stride, 3)
        self.assertAlmostEqual(pipe.crosshair_analyzer.rate_scale, REFERENCE_SAMPLE_HZ / 48.0)

    def test_unknown_rate_changes_nothing(self) -> None:
        pipe = _pipeline()
        pipe.set_feed_rate(0.0)
        self.assertEqual(pipe.analysis_stride, 3)
        self.assertEqual(pipe.analysis_cadence_hz, 0.0)

    def test_gate_hysteresis_is_a_third_of_a_second(self) -> None:
        pipe = _pipeline()
        pipe.set_feed_rate(144.0)
        self.assertEqual(pipe.scene_gate.N, 48)
        pipe.set_feed_rate(30.0)
        self.assertEqual(pipe.scene_gate.N, 10)

    def test_stride_change_clears_the_analyser_window(self) -> None:
        pipe = _pipeline()
        pipe.crosshair_analyzer.delta_history.extend([(1.0, 0.0)] * 6)
        pipe.set_feed_rate(144.0)
        self.assertEqual(len(pipe.crosshair_analyzer.delta_history), 0)


class AnalyserSampleRateTests(unittest.TestCase):
    def test_reference_rate_leaves_the_tuned_numbers_alone(self) -> None:
        analyzer = CrosshairKinematicsAnalyzer()
        self.assertEqual(analyzer.set_sample_rate(REFERENCE_SAMPLE_HZ), 18)
        self.assertAlmostEqual(analyzer.rate_scale, 1.0)

    def test_window_covers_the_same_time_at_any_rate(self) -> None:
        analyzer = CrosshairKinematicsAnalyzer()
        self.assertEqual(analyzer.set_sample_rate(48.0), 43)
        self.assertEqual(analyzer.set_sample_rate(10.0), 9)
        self.assertEqual(analyzer.set_sample_rate(5.0), 8)  # floor
        self.assertEqual(analyzer.set_sample_rate(480.0), 120)  # cap

    def test_resizing_keeps_the_most_recent_samples(self) -> None:
        analyzer = CrosshairKinematicsAnalyzer()
        analyzer.delta_history.extend((float(i), 0.0) for i in range(18))
        analyzer.set_sample_rate(10.0)  # 9 samples
        self.assertEqual([d[0] for d in analyzer.delta_history], [float(i) for i in range(9, 18)])

    def test_scale_is_clamped(self) -> None:
        analyzer = CrosshairKinematicsAnalyzer()
        analyzer.set_sample_rate(480.0)
        self.assertAlmostEqual(analyzer.rate_scale, 0.25)
        analyzer.set_sample_rate(5.0)
        self.assertAlmostEqual(analyzer.rate_scale, 4.0)

    def test_same_pan_flags_at_20_and_at_60_samples_a_second(self) -> None:
        # 420 px/s at analysis size: 21 px per step at 20 Hz, 7 px per step at 60 Hz.
        slow = CrosshairKinematicsAnalyzer()
        slow.set_sample_rate(20.0)
        fast = CrosshairKinematicsAnalyzer()
        fast.set_sample_rate(60.0)
        for flagged in (_pan(slow, 20.0, 21, 1.5), _pan(fast, 60.0, 7, 1.5)):
            self.assertIsNotNone(flagged)
            self.assertIn(flagged["event_type"], {"UNNATURAL_GEOMETRIC_LINE", "MECHANICAL_LOCK_NO_TREMOR"})

    def test_without_rate_correction_the_fast_feed_misses_the_same_pan(self) -> None:
        # Still believes it sees 20 samples/s. Use 6 px/step (360 px/s physical): after the
        # threshold loosen, 7 px/step already trips MECHANICAL_LOCK when under-read, but 6 px
        # still reads as too slow to flag. The corrected sibling above keeps 7/21 px at 420 px/s.
        untold = CrosshairKinematicsAnalyzer()
        self.assertIsNone(_pan(untold, 60.0, 6, 1.5))


class HighRefreshPipelineTests(unittest.TestCase):
    def test_one_frame_snap_is_caught_on_a_144_fps_feed(self) -> None:
        fps = 144.0
        pipe = AntiCheatPipeline(
            log_dir=tempfile.mkdtemp(),
            analysis_stride=3,
            target_resolution=(W, H),
            player_detector_model_path="does/not/exist.onnx",
            detection_player_class_ids=[0],
            source_profile="vod_file",
            game_profile="generic",
        )
        pipe.set_feed_rate(fps)
        self.assertEqual(pipe.analysis_stride, 7)

        world = _world(1)
        rng = np.random.default_rng(5)
        target = (60.0, -30.0)
        snap_at = int(fps * 0.5)
        events: list[str] = []
        for i in range(int(fps * 1.5)):
            if i < snap_at:
                cam = (rng.normal(0, 0.6), rng.normal(0, 0.6))
            else:
                cam = (target[0] + rng.normal(0, 0.5), target[1] + rng.normal(0, 0.5))
            frame = _render(world, cam)
            bx = int(target[0] - cam[0]) + W // 2
            by = int(target[1] - cam[1]) + H // 2
            pipe.update_detected_entities(
                [{"track_id": 1, "bbox": (bx - 16, by - 32, bx + 16, by + 32), "center": (bx, by), "confidence": 0.8, "class_id": 0}],
                (H, W),
                (H, W),
            )
            ctx = FrameContext(frame=frame, timestamp=i / fps, frame_id=i + 1, source="vod", is_duplicate=False)
            ctx.analysis_frame = frame
            event = pipe.process_frame(ctx)
            if event is not None:
                events.append(event.cheat_category)
        self.assertIn("SNAP_TO_TARGET", events)


if __name__ == "__main__":
    unittest.main()
