#!/usr/bin/env python3
"""Watch labeled VOD folders and process new videos with a gamer-tag popup."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QInputDialog, QLabel, QMainWindow, QMessageBox

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.process_vod_inbox import VIDEO_EXTENSIONS, parse_args, process_vod, train_candidate  # noqa: E402


def label_for_path(inbox: Path, path: Path) -> str:
    relative_parts = path.relative_to(inbox).parts
    if not relative_parts or relative_parts[0].lower() not in {"clean", "cheating"}:
        raise ValueError(f"Video must be under clean or cheating: {path}")
    return "clean" if relative_parts[0].lower() == "clean" else "suspicious"


class VideoWorker(QThread):
    completed = Signal(str, int)
    failed = Signal(object, str)

    def __init__(
        self,
        video_path: Path,
        label: str,
        player_tag: str,
        processing_args: argparse.Namespace,
    ) -> None:
        super().__init__()
        self.video_path = video_path
        self.label = label
        self.player_tag = player_tag
        self.processing_args = processing_args

    def run(self) -> None:
        try:
            clips = process_vod(
                self.video_path,
                self.label,
                self.player_tag,
                self.processing_args,
            )
            if clips and not self.processing_args.skip_training:
                training_message = train_candidate(self.processing_args)
            elif clips:
                training_message = "Candidate training is disabled."
            else:
                training_message = "No kill clips were found; training was not started."
            self.completed.emit(training_message, len(clips))
        except Exception as exc:
            self.failed.emit(self.video_path, str(exc))


class InboxWindow(QMainWindow):
    def __init__(self, processing_args: argparse.Namespace) -> None:
        super().__init__()
        self.processing_args = processing_args
        self.inbox = processing_args.inbox
        self.inbox.joinpath("clean").mkdir(parents=True, exist_ok=True)
        self.inbox.joinpath("cheating").mkdir(parents=True, exist_ok=True)
        self.setWindowTitle("CheatVision VOD Inbox")
        self.setMinimumWidth(440)
        self.status = QLabel(
            "Watching these folders for new videos:\n"
            f"{self.inbox / 'clean'}\n"
            f"{self.inbox / 'cheating'}\n\n"
            "Drop a VOD into either folder. A popup will ask for the watched player's tag."
        )
        self.status.setMargin(18)
        self.setCentralWidget(self.status)

        self._known_videos = set(self._find_videos())
        self._stability: dict[Path, tuple[int, int, int]] = {}
        self._queued: set[Path] = set()
        self._prompt_open = False
        self._worker: VideoWorker | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(1500)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

    def _find_videos(self) -> list[Path]:
        videos: list[Path] = []
        for folder in (self.inbox / "clean", self.inbox / "cheating"):
            videos.extend(
                path for path in folder.rglob("*")
                if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
            )
        return videos

    def _poll(self) -> None:
        for path in self._find_videos():
            if path in self._known_videos or path in self._queued:
                continue
            try:
                stat = path.stat()
            except OSError as exc:
                self.status.setText(f"Could not inspect {path.name}: {exc}")
                continue
            previous = self._stability.get(path)
            if previous and previous[:2] == (stat.st_size, stat.st_mtime_ns):
                stable_count = previous[2] + 1
            else:
                stable_count = 1
            self._stability[path] = (stat.st_size, stat.st_mtime_ns, stable_count)
            if stable_count >= 3:
                self._known_videos.add(path)
                self._queued.add(path)
                self._prompt_for_tag(path)
                if self._worker is not None:
                    break

        if self._worker is None and not self._prompt_open:
            next_video = next((path for path in self._find_videos() if path in self._queued), None)
            if next_video is not None:
                self._prompt_for_tag(next_video)

    def _label_for(self, path: Path) -> str:
        return label_for_path(self.inbox, path)

    def _prompt_for_tag(self, path: Path) -> None:
        if self._prompt_open or self._worker is not None:
            return
        self._prompt_open = True
        try:
            tag, accepted = QInputDialog.getText(
                self,
                "Enter watched player",
                f"Enter the exact in-game tag visible in the bottom-left HUD:\n{path.name}",
            )
        finally:
            self._prompt_open = False
        tag = tag.strip()
        if not accepted or not tag:
            self._queued.discard(path)
            self.status.setText(
                f"Skipped {path.name}. Copy it back with a new filename to retry."
            )
            return

        try:
            label = self._label_for(path)
        except ValueError as exc:
            self._queued.discard(path)
            QMessageBox.critical(self, "Invalid inbox location", str(exc))
            return

        self.status.setText(f"Processing {path.name} ({label})…\nThe window stays responsive.")
        self._worker = VideoWorker(path, label, tag, self.processing_args)
        self._worker.completed.connect(lambda message, count, video=path: self._on_complete(video, message, count))
        self._worker.failed.connect(self._on_failure)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()

    def _on_complete(self, video: Path, message: str, count: int) -> None:
        self._queued.discard(video)
        self.status.setText(
            f"Finished {video.name}: {count} clip(s).\n{message}\n\nStill watching for new videos."
        )
        if count:
            QMessageBox.information(self, "VOD processing complete", f"{video.name}\n\n{count} clip(s) created.\n{message}")
        else:
            QMessageBox.warning(self, "No kill clips found", f"{video.name}\n\n{message}")

    def _on_failure(self, video: Path, message: str) -> None:
        self._queued.discard(video)
        self.status.setText(f"Could not process {video.name}: {message}\n\nStill watching for new videos.")
        QMessageBox.critical(self, "VOD processing failed", f"{video.name}\n\n{message}")

    def _on_worker_finished(self) -> None:
        self._worker = None
        QTimer.singleShot(0, self._poll)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.warning(
                self,
                "Video still processing",
                "Keep the inbox window open until the current video finishes.",
            )
            event.ignore()
            return
        event.accept()


def main(argv: list[str] | None = None) -> int:
    processing_args = parse_args(argv)
    app = QApplication(sys.argv[:1])
    window = InboxWindow(processing_args)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
