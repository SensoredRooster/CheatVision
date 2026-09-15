"""Detections survive everywhere a player can stand. The filters behind YOLO
only remove HUD art, the player's own weapon, and (under STREAM masks) the
streamer's facecam; none of them may swallow a region a real enemy uses."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.anti_cheat_pipeline import AntiCheatPipeline
from src.core.hud_masker import HUDMasker, box_coverage

W, H = 960, 540


def _pipe(source_profile: str, game_profile: str = "warzone", **extra) -> AntiCheatPipeline:
    return AntiCheatPipeline(
        log_dir=tempfile.mkdtemp(),
        target_resolution=(W, H),
        player_detector_model_path="does/not/exist.onnx",
        detection_player_class_ids=[0],
        source_profile=source_profile,
        game_profile=game_profile,
        **extra,
    )


def _survivors(pipe: AntiCheatPipeline, boxes) -> int:
    entities = [
        {"track_id": i + 1, "bbox": box, "center": ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2), "confidence": 0.7, "class_id": 0}
        for i, box in enumerate(boxes)
    ]
    pipe.update_detected_entities(entities, (H, W), (H, W))
    return len(pipe.get_analysis_entities())


class CoverageTests(unittest.TestCase):
    def test_box_coverage_values(self) -> None:
        minimap = [(0.0, 0.0, 0.18, 0.285)]  # 0..172 x 0..153 px at 960x540
        self.assertAlmostEqual(box_coverage((20, 20, 80, 120), minimap, W, H), 1.0)
        self.assertAlmostEqual(box_coverage((500, 300, 560, 420), minimap, W, H), 0.0)
        # Half behind the minimap: 32.8 of 60 px wide, 53.9 of 120 px tall inside.
        partial = box_coverage((140, 100, 200, 220), minimap, W, H)
        self.assertGreater(partial, 0.2)
        self.assertLess(partial, 0.3)
        self.assertEqual(box_coverage((10, 10, 10, 50), minimap, W, H), 0.0)  # degenerate box


class HudFilterTests(unittest.TestCase):
    def test_portrait_inside_hud_is_dropped_but_player_half_behind_minimap_is_kept(self) -> None:
        pipe = _pipe("hdmi_game")
        self.assertEqual(_survivors(pipe, [(20, 20, 80, 120)]), 0)  # squad portrait / minimap icon
        self.assertEqual(_survivors(pipe, [(140, 100, 200, 220)]), 1)  # enemy peeking past the minimap

    def test_generic_profile_has_no_hud_regions(self) -> None:
        pipe = _pipe("hdmi_game", game_profile="generic")
        self.assertEqual(_survivors(pipe, [(20, 20, 80, 120)]), 1)


class FacecamTests(unittest.TestCase):
    # Default facecam box is x 0.62..0.99, y 0.42..0.82 -> 595..950 x 227..443 px.
    _IN_FACECAM = (700, 260, 760, 400)

    def test_game_feed_has_no_facecam_so_right_middle_players_survive(self) -> None:
        pipe = _pipe("hdmi_game")
        self.assertEqual(pipe.ignore_rect_count, 0)
        self.assertEqual(_survivors(pipe, [self._IN_FACECAM]), 1)

    def test_stream_masks_drop_the_streamers_face(self) -> None:
        for profile in ("stream_window", "vod_file"):
            pipe = _pipe(profile)
            self.assertEqual(_survivors(pipe, [self._IN_FACECAM]), 0, profile)

    def test_explicit_facecam_roi_applies_on_a_game_feed_too(self) -> None:
        pipe = _pipe("hdmi_game", facecam_roi=[0.62, 0.42, 0.99, 0.82])
        self.assertEqual(pipe.ignore_rect_count, 1)
        self.assertEqual(_survivors(pipe, [self._IN_FACECAM]), 0)

    def test_switching_masks_live_changes_the_verdict(self) -> None:
        pipe = _pipe("hdmi_game")
        self.assertEqual(_survivors(pipe, [self._IN_FACECAM]), 1)
        pipe.set_source_profile("stream_window")
        self.assertEqual(_survivors(pipe, [self._IN_FACECAM]), 0)
        pipe.set_source_profile("hdmi_game")
        self.assertEqual(_survivors(pipe, [self._IN_FACECAM]), 1)


class ViewmodelTests(unittest.TestCase):
    def test_weapon_box_is_dropped_only_when_it_hugs_the_bottom_inside_the_zone(self) -> None:
        masker = HUDMasker(target_resolution=(W, H), game_profile="warzone")
        # Zone x 0.30..0.72, y 0.55..1.0 -> 288..691 x 297..540 px.
        self.assertTrue(masker.is_viewmodel_detection(380, 330, 650, 540, width=W, height=H))  # arms + gun
        self.assertFalse(masker.is_viewmodel_detection(380, 200, 650, 540, width=W, height=H))  # reaches above: enemy
        self.assertFalse(masker.is_viewmodel_detection(380, 330, 650, 480, width=W, height=H))  # off the bottom edge
        # Hands thrown wide during a pistol / melee animation still hug the
        # bottom edge inside the lower band: weapon, whatever the x.
        self.assertTrue(masker.is_viewmodel_detection(50, 330, 210, 540, width=W, height=H))
        self.assertTrue(masker.is_viewmodel_detection(420, 300, 780, 540, width=W, height=H))
        # A far enemy low on the right never touches the bottom edge: kept.
        self.assertFalse(masker.is_viewmodel_detection(860, 330, 900, 420, width=W, height=H))


class WholeScreenSurvivalTests(unittest.TestCase):
    def test_a_player_sized_box_survives_almost_everywhere_on_a_game_feed(self) -> None:
        """Sweep a 40x90 px box (a mid-range enemy at 960x540) across the
        screen: on a bare game feed it must survive in the overwhelming
        majority of positions, including the whole right-middle and the low
        centre, which used to be dead zones."""
        pipe = _pipe("hdmi_game")
        boxes = [(x, y, x + 40, y + 90) for x in range(0, W - 40, 40) for y in range(0, H - 90, 30)]
        kept = sum(_survivors(pipe, [box]) for box in boxes)
        self.assertGreater(kept / len(boxes), 0.85)
        # Right-middle band specifically (the old facecam dead zone), stopping
        # short of the bottom-right ammo HUD, where a box mostly behind the
        # HUD is still (correctly) dropped.
        band = [(x, y, x + 40, y + 90) for x in range(600, W - 40, 40) for y in range(240, 390, 30)]
        self.assertEqual(sum(_survivors(pipe, [box]) for box in band), len(band))


if __name__ == "__main__":
    unittest.main()
