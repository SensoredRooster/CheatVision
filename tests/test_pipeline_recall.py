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

from src.core.anti_cheat_pipeline import AntiCheatPipeline, FrameContext

W, H, FPS = 960, 540, 72.0


def _world(seed: int) -> np.ndarray:
    """Sharp blocky texture: real footage gives a phase-correlation response of
    ~0.8, a Gaussian-blurred noise field only ~0.1 (looks like measurement
    failure to the analyser)."""
    rng = np.random.default_rng(seed)
    coarse = rng.integers(30, 230, size=(H * 3 // 12, W * 3 // 12), dtype=np.uint8)
    field = cv2.resize(coarse, (W * 3, H * 3), interpolation=cv2.INTER_NEAREST)
    fine = rng.integers(0, 40, size=(H * 3, W * 3), dtype=np.uint8)
    return cv2.add(field, fine)


def _render(world: np.ndarray, cam: tuple[float, float]) -> np.ndarray:
    cx, cy = int(cam[0]), int(cam[1])
    view = world[H + cy : 2 * H + cy, W + cx : 2 * W + cx]
    frame = cv2.cvtColor(view, cv2.COLOR_GRAY2BGR).copy()
    cv2.circle(frame, (W // 2, H // 2), 2, (255, 255, 255), -1)
    return frame


class _Scenario:
    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.pipe = AntiCheatPipeline(
            log_dir=self.tmp,
            analysis_stride=1,
            target_resolution=(W, H),
            player_detector_model_path="does/not/exist.onnx",
            detection_player_class_ids=[0],
            source_profile="vod_file",
            game_profile="generic",
        )
        self.world = _world(1)

    def run(self, cam_path, target_path=None) -> list[str]:
        events: list[str] = []
        for i, cam in enumerate(cam_path):
            frame = _render(self.world, cam)
            if target_path is not None:
                tx, ty = target_path[i]
                bx = int(tx - cam[0]) + W // 2
                by = int(ty - cam[1]) + H // 2
                box = (bx - 16, by - 32, bx + 16, by + 32)
                self.pipe.update_detected_entities(
                    [{"track_id": 1, "bbox": box, "center": (bx, by), "confidence": 0.8, "class_id": 0}],
                    (H, W),
                    (H, W),
                )
            ctx = FrameContext(frame=frame, timestamp=i / FPS, frame_id=i + 1, source="vod", is_duplicate=False)
            ctx.analysis_frame = frame
            event = self.pipe.process_frame(ctx)
            if event is not None:
                events.append(event.cheat_category)
        return events


class PipelineRecallTest(unittest.TestCase):
    """Full-pipeline behaviour on synthetic aim patterns. Thresholds were set
    against real legit Warzone footage; these fixtures guard the logic that
    footage cannot (no labelled cheat clips yet)."""

    def test_human_flick_with_tremor_is_not_flagged(self) -> None:
        rng = np.random.default_rng(3)
        path = [(0.0, 0.0)]
        for i in range(1, 90):
            if i < 22:
                step = (14.0 + rng.normal(0, 2.5), 0.9 * np.sin(i * 0.9) + rng.normal(0, 1.4))
            else:
                step = (rng.normal(0, 1.2), rng.normal(0, 1.2))
            path.append((path[-1][0] + step[0], path[-1][1] + step[1]))
        self.assertEqual(_Scenario().run(path), [])

    def test_sustained_ruler_straight_pan_is_flagged(self) -> None:
        path = [(i * 20.0, i * 4.0) for i in range(90)]
        events = _Scenario().run(path)
        self.assertTrue(set(events) & {"UNNATURAL_GEOMETRIC_LINE", "MECHANICAL_LOCK_NO_TREMOR"}, events)

    def test_tremor_free_tracking_of_curving_target_is_flagged(self) -> None:
        target = [(120 + i * 12.0, 40 + 25 * np.sin(i * 0.12)) for i in range(90)]
        events = _Scenario().run(list(target), target_path=target)
        self.assertTrue(set(events) & {"MECHANICAL_LOCK_NO_TREMOR", "STICKY_AIM"}, events)

    def test_one_frame_snap_onto_head_is_flagged(self) -> None:
        rng = np.random.default_rng(5)
        target = [(60.0, -30.0)] * 90
        path = [
            (rng.normal(0, 0.6), rng.normal(0, 0.6)) if i < 30 else (60.0 + rng.normal(0, 0.5), -30.0 + rng.normal(0, 0.5))
            for i in range(90)
        ]
        events = _Scenario().run(path, target_path=target)
        self.assertIn("SNAP_TO_TARGET", events)

    def test_human_ramp_onto_head_is_not_flagged(self) -> None:
        """A hand accelerating onto a target over ~3 frames (as in two real
        false positives: 0.6 -> 14 -> 37 px) must not read as a snap even
        though it lands on the head and holds there."""
        rng = np.random.default_rng(7)
        target = [(60.0, -30.0)] * 90
        ramp = [(0.0, 0.0), (0.6, 0.0), (15.0, -7.0), (52.0, -26.0), (60.0, -30.0)]
        path = []
        for i in range(90):
            if i < 30:
                path.append((rng.normal(0, 0.5), rng.normal(0, 0.5)))
            elif i - 30 < len(ramp):
                path.append(ramp[i - 30])
            else:
                path.append((60.0 + rng.normal(0, 0.5), -30.0 + rng.normal(0, 0.5)))
        events = _Scenario().run(path, target_path=target)
        self.assertNotIn("SNAP_TO_TARGET", events)
        self.assertNotIn("FLICK_SNAP", events)

    def test_snap_that_does_not_hold_is_not_flagged(self) -> None:
        """Instant move onto the head, then straight off it: no hold, no flag."""
        rng = np.random.default_rng(9)
        target = [(60.0, -30.0)] * 90
        path = []
        for i in range(90):
            if i < 30:
                path.append((rng.normal(0, 0.5), rng.normal(0, 0.5)))
            elif i < 33:
                path.append((60.0, -30.0))
            else:
                path.append((60.0 + (i - 32) * 9.0, -30.0 + (i - 32) * 4.0))
        events = _Scenario().run(path, target_path=target)
        self.assertNotIn("SNAP_TO_TARGET", events)

    def test_head_point_is_inside_the_box(self) -> None:
        head = AntiCheatPipeline._head_point((100, 200, 140, 280))
        self.assertIsNotNone(head)
        self.assertEqual(head, (120.0, 216.0))

    def test_viewmodel_detection_is_discarded(self) -> None:
        scenario = _Scenario()
        # Box hugging the bottom edge inside the weapon zone: the player's own arms.
        scenario.pipe.update_detected_entities(
            [{"track_id": 7, "bbox": (400, 360, 640, 540), "center": (520, 450), "confidence": 0.6, "class_id": 0}],
            (H, W),
            (H, W),
        )
        self.assertEqual(scenario.pipe.get_analysis_entities(), [])
        # Same box moved up into the play area is kept.
        scenario.pipe.update_detected_entities(
            [{"track_id": 8, "bbox": (400, 60, 440, 140), "center": (420, 100), "confidence": 0.6, "class_id": 0}],
            (H, W),
            (H, W),
        )
        self.assertEqual(len(scenario.pipe.get_analysis_entities()), 1)

    def test_close_enemy_low_centre_is_kept(self) -> None:
        """An enemy right in front of the player: the box centre is inside the
        weapon zone, but the box reaches well above it. Real target, kept."""
        scenario = _Scenario()
        scenario.pipe.update_detected_entities(
            [{"track_id": 9, "bbox": (400, 200, 640, 540), "center": (520, 370), "confidence": 0.7, "class_id": 0}],
            (H, W),
            (H, W),
        )
        self.assertEqual(len(scenario.pipe.get_analysis_entities()), 1)

    def test_point_blank_enemy_filling_half_the_screen_is_kept(self) -> None:
        scenario = _Scenario()
        scenario.pipe.update_detected_entities(
            [{"track_id": 10, "bbox": (200, 20, 760, 500), "center": (480, 260), "confidence": 0.8, "class_id": 0}],
            (H, W),
            (H, W),
        )
        self.assertEqual(len(scenario.pipe.get_analysis_entities()), 1)


if __name__ == "__main__":
    unittest.main()
