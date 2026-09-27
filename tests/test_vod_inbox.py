from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from tools.process_vod_inbox import (
    DEFAULT_KILL_FEED_ROI,
    DEFAULT_PLAYER_HUD_ROI,
    KillEvent,
    OcrText,
    _name_matches,
    _ocr_texts,
    find_elimination_toasts,
    find_player_kill_rows,
    is_short_clip,
    load_player_tag,
    load_player_tag_history,
    normalize_name,
    parse_roi,
    player_tag_is_visible,
    process_vod,
    save_player_tag,
    scan_vod,
    train_candidate,
    write_clip,
)
from tools.watch_vod_inbox import label_for_path


class KillFeedMatchingTests(unittest.TestCase):
    def test_normalizes_gamer_tags_without_case_or_punctuation(self) -> None:
        self.assertEqual(normalize_name("Tester#1234"), "tester1234")

    def test_matches_player_at_start_of_killer_then_victim_row(self) -> None:
        detections = [
            OcrText(10, 20, 16, "Tester#1234", 0.98),
            OcrText(150, 20, 16, "TargetPlayer", 0.94),
        ]

        self.assertEqual(find_player_kill_rows(detections, "Tester#1234"), ["Tester#1234 TargetPlayer"])

    def test_does_not_match_player_when_name_is_victim(self) -> None:
        detections = [
            OcrText(10, 20, 16, "OtherPlayer", 0.98),
            OcrText(150, 20, 16, "Tester#1234", 0.94),
        ]

        self.assertEqual(find_player_kill_rows(detections, "Tester#1234"), [])

    def test_requires_text_after_tag_to_avoid_non_kill_hud_name(self) -> None:
        self.assertFalse(_name_matches("Tester#1234", "Tester#1234", 0.9))

    def test_combined_ocr_line_is_supported(self) -> None:
        detections = [OcrText(10, 20, 16, "Tester#1234 TargetPlayer", 0.98)]

        self.assertEqual(find_player_kill_rows(detections, "Tester#1234"), ["Tester#1234 TargetPlayer"])

    def test_low_confidence_text_is_ignored(self) -> None:
        detections = [
            OcrText(10, 20, 16, "Tester#1234", 0.98),
            OcrText(150, 20, 16, "TargetPlayer", 0.20),
        ]

        self.assertEqual(find_player_kill_rows(detections, "Tester#1234"), [])

    def test_rapidocr_string_confidence_is_converted(self) -> None:
        detection = [[[0, 0], [20, 0], [20, 10], [0, 10]], "Tester1234", "0.95"]
        raw = ([detection], [0.01, 0.01, 0.01])

        detections = _ocr_texts(raw, min_confidence=0.6)

        self.assertEqual(len(detections), 1)
        self.assertEqual(detections[0].confidence, 0.95)

    def test_bottom_left_player_name_can_be_verified_with_hud_prefix(self) -> None:
        detections = [OcrText(0, 10, 18, "1 [TwTcHjSensoredRooster", 0.90)]

        self.assertTrue(player_tag_is_visible(detections, "[TwTcH]SensoredRooster"))

    def test_unrelated_bottom_left_name_does_not_verify_player(self) -> None:
        detections = [OcrText(0, 10, 18, "1 OtherPlayer", 0.99)]

        self.assertFalse(player_tag_is_visible(detections, "[TwTcH]SensoredRooster"))

    def test_detects_elimination_toast(self) -> None:
        detections = [OcrText(10, 20, 16, "+100 Elimination", 0.98)]

        self.assertEqual(find_elimination_toasts(detections), ["+100 Elimination"])

    def test_detects_kill_confirmed_toast(self) -> None:
        detections = [OcrText(10, 20, 16, "+50 Kill Confirmed", 0.98)]

        self.assertEqual(find_elimination_toasts(detections), ["+50 Kill Confirmed"])

    def test_does_not_treat_enemy_downed_as_a_kill(self) -> None:
        detections = [OcrText(10, 20, 16, "+100 Enemy Downed", 0.98)]

        self.assertEqual(find_elimination_toasts(detections), [])


class RoiParsingTests(unittest.TestCase):
    def test_warzone_defaults_use_middle_left_feed_and_bottom_left_hud(self) -> None:
        self.assertEqual(DEFAULT_KILL_FEED_ROI, (0.0, 0.43, 0.24, 0.60))
        self.assertEqual(DEFAULT_PLAYER_HUD_ROI, (0.0, 0.89, 0.20, 0.98))

    def test_parses_normalized_roi(self) -> None:
        self.assertEqual(parse_roi("0.5,0.1,0.99,0.4"), (0.5, 0.1, 0.99, 0.4))

    def test_rejects_out_of_bounds_roi(self) -> None:
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_roi("-0.1,0,1,1")

    def test_rejects_wrong_number_of_coordinates(self) -> None:
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_roi("0,0,1")


class InboxLabelTests(unittest.TestCase):
    def test_clean_folder_maps_to_clean_dataset(self) -> None:
        inbox = Path("data/vod_inbox")

        self.assertEqual(label_for_path(inbox, inbox / "clean" / "match.mp4"), "clean")

    def test_cheating_folder_maps_to_suspicious_dataset(self) -> None:
        inbox = Path("data/vod_inbox")

        self.assertEqual(label_for_path(inbox, inbox / "cheating" / "match.mp4"), "suspicious")

    def test_rejects_video_outside_labeled_folders(self) -> None:
        inbox = Path("data/vod_inbox")

        with self.assertRaisesRegex(ValueError, "must be under clean or cheating"):
            label_for_path(inbox, inbox / "other" / "match.mp4")


class ProcessVodTests(unittest.TestCase):
    def test_processes_detected_short_clip_into_its_labeled_class(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vod = Path(directory) / "short.mp4"
            dataset = Path(directory) / "dataset"
            args = argparse.Namespace(
                dataset=dataset,
                sample_seconds=0.25,
                roi=(0, 0, 1, 1),
                min_confidence=0.6,
                match_threshold=0.9,
                elimination_roi=(0, 0, 1, 1),
                player_hud_roi=(0, 0, 1, 1),
                before=10.0,
                after=4.0,
            )
            event = KillEvent(timestamp=2.0, row_text="Tester1234 Enemy5678")
            expected = dataset / "suspicious" / "short_t000002000ms.mp4"

            with patch("tools.process_vod_inbox.probe_video", return_value=(60.0, 8.0)), patch(
                "tools.process_vod_inbox.scan_vod", return_value=(60.0, [event])
            ), patch("tools.process_vod_inbox.write_clip") as write_clip_mock:
                clips = process_vod(vod, "suspicious", "Tester1234", args, ocr=object())

        self.assertEqual(clips, [expected])
        write_clip_mock.assert_called_once_with(vod, expected, 0.0, 6.0, 60.0)

    def test_short_clip_without_kills_is_copied_whole(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vod = Path(directory) / "misfire_sticky.mp4"
            dataset = Path(directory) / "dataset"
            args = argparse.Namespace(
                dataset=dataset,
                sample_seconds=0.25,
                roi=(0, 0, 1, 1),
                min_confidence=0.6,
                match_threshold=0.9,
                elimination_roi=(0, 0, 1, 1),
                player_hud_roi=(0, 0, 1, 1),
                before=10.0,
                after=4.0,
            )
            expected = dataset / "clean" / "misfire_sticky_full.mp4"

            with patch("tools.process_vod_inbox.probe_video", return_value=(60.0, 5.0)), patch(
                "tools.process_vod_inbox.scan_vod", return_value=(60.0, [])
            ), patch("tools.process_vod_inbox.write_clip") as write_clip_mock:
                clips = process_vod(vod, "clean", "Tester1234", args, ocr=object())

        self.assertEqual(clips, [expected])
        write_clip_mock.assert_called_once_with(vod, expected, 0.0, 5.0, 60.0)

    def test_short_clip_does_not_need_a_gamer_tag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vod = Path(directory) / "misfire.mp4"
            dataset = Path(directory) / "dataset"
            args = argparse.Namespace(dataset=dataset, before=10.0, after=4.0)
            expected = dataset / "clean" / "misfire_full.mp4"

            with patch("tools.process_vod_inbox.probe_video", return_value=(30.0, 4.0)), patch(
                "tools.process_vod_inbox.write_clip"
            ) as write_clip_mock:
                clips = process_vod(vod, "clean", " ", args, ocr=object())

        self.assertEqual(clips, [expected])
        write_clip_mock.assert_called_once_with(vod, expected, 0.0, 4.0, 30.0)

    def test_long_vod_still_requires_a_gamer_tag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(before=10.0, after=4.0)

            with patch("tools.process_vod_inbox.probe_video", return_value=(60.0, 180.0)):
                with self.assertRaisesRegex(ValueError, "gamer tag cannot be empty"):
                    process_vod(Path(directory) / "input.mp4", "clean", " ", args, ocr=object())

    def test_saves_player_tag_once_for_later_videos(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inbox = Path(directory)

            save_player_tag(inbox, " [TwTcH]SensoredRooster ")
            save_player_tag(inbox, "OtherPlayer")

            self.assertEqual(load_player_tag(inbox), "OtherPlayer")
            self.assertEqual(
                load_player_tag_history(inbox),
                ["OtherPlayer", "[TwTcH]SensoredRooster"],
            )
            self.assertTrue(is_short_clip(5.0, 10.0, 4.0))
            self.assertFalse(is_short_clip(180.0, 10.0, 4.0))

    def test_candidate_training_waits_until_both_classes_exist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dataset = Path(directory) / "dataset"
            (dataset / "clean").mkdir(parents=True)
            (dataset / "suspicious").mkdir()
            args = argparse.Namespace(dataset=dataset)

            message = train_candidate(args)

        self.assertIn("both clean and cheating", message)


class VodScanTests(unittest.TestCase):
    def _make_vod(self, path: Path, seconds: int = 2) -> None:
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*"mp4v"), 10.0, (160, 90))
        self.assertTrue(writer.isOpened())
        for index in range(seconds * 10):
            writer.write(np.full((90, 160, 3), index, dtype=np.uint8))
        writer.release()

    def test_scan_deduplicates_a_persistent_kill_feed_row(self) -> None:
        class FakeOcr:
            def __call__(self, image):
                detection = [[[2, 2], [60, 2], [60, 15], [2, 15]], "Tester1234 Enemy5678", 0.99]
                return ([detection], 0.01)

        with tempfile.TemporaryDirectory() as directory:
            vod = Path(directory) / "input.mp4"
            self._make_vod(vod)

            fps, events = scan_vod(
                vod,
                "Tester1234",
                FakeOcr(),
                sample_seconds=0.25,
                roi=(0, 0, 1, 1),
                min_confidence=0.60,
                match_threshold=0.90,
                elimination_roi=(0, 0, 1, 1),
            )

        self.assertEqual(fps, 10.0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].timestamp, 0.0)

    def test_scan_refuses_to_label_a_vod_when_player_tag_is_not_visible(self) -> None:
        class NoTagOcr:
            def __call__(self, _image):
                return ([], 0.01)

        with tempfile.TemporaryDirectory() as directory:
            vod = Path(directory) / "input.mp4"
            self._make_vod(vod)

            with self.assertRaisesRegex(RuntimeError, "Could not verify gamer tag"):
                scan_vod(
                    vod,
                    "Tester1234",
                    NoTagOcr(),
                    sample_seconds=0.25,
                    roi=(0, 0, 1, 1),
                    min_confidence=0.60,
                    match_threshold=0.90,
                )

    def test_write_clip_creates_readable_video_for_requested_interval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vod = root / "input.mp4"
            clip = root / "output" / "clip.mp4"
            self._make_vod(vod, seconds=2)

            write_clip(vod, clip, start_seconds=0.5, end_seconds=1.5, fps=10.0)

            captured = cv2.VideoCapture(str(clip))
            try:
                self.assertTrue(captured.isOpened())
                self.assertEqual(int(captured.get(cv2.CAP_PROP_FRAME_COUNT)), 10)
            finally:
                captured.release()


if __name__ == "__main__":
    unittest.main()
