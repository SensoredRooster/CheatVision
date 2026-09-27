#!/usr/bin/env python3
"""Watch labeled VOD folders and process new videos with a saved gamer tag."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.process_vod_inbox import (  # noqa: E402
    VIDEO_EXTENSIONS,
    is_short_clip,
    load_player_tag,
    load_player_tag_history,
    parse_args,
    probe_video,
    process_vod,
    save_player_tag,
    train_candidate,
)

PROCESSED_FILENAME = "processed.json"


def label_for_path(inbox: Path, path: Path) -> str:
    relative_parts = path.relative_to(inbox).parts
    if not relative_parts or relative_parts[0].lower() not in {"clean", "cheating"}:
        raise ValueError(f"Video must be under clean or cheating: {path}")
    return "clean" if relative_parts[0].lower() == "clean" else "suspicious"


def processed_path(inbox: Path) -> Path:
    return inbox / PROCESSED_FILENAME


def processed_key(path: Path, size: int, mtime_ns: int) -> str:
    return f"{size}:{mtime_ns}:{path.resolve()}"


def load_processed(inbox: Path) -> set[str]:
    path = processed_path(inbox)
    if not path.is_file():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if isinstance(payload, list):
        return {str(item) for item in payload}
    return set()


def save_processed(inbox: Path, keys: set[str]) -> None:
    processed_path(inbox).write_text(json.dumps(sorted(keys), indent=2), encoding="utf-8")


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
                training_message = (
                    "No kill clips were found in this long VOD. "
                    "Short clips are copied as-is; long VODs still need a visible kill feed or toast."
                )
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
        self.setMinimumWidth(520)

        self.tag_box = QComboBox()
        self.tag_box.setEditable(True)
        self.tag_box.setInsertPolicy(QComboBox.InsertPolicy.InsertAtTop)
        self._reload_tag_box()

        self.status = QLabel(
            "Watching these folders. Keep original filenames.\n"
            f"{self.inbox / 'clean'}\n"
            f"{self.inbox / 'cheating'}\n\n"
            "Each video asks for the player tag, pre-filled with the last name. "
            "Press OK to keep it, or type/select a different tag. Names are saved in a list."
        )
        self.status.setWordWrap(True)
        self.status.setMargin(12)

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.addWidget(QLabel("Saved player tags"))
        layout.addWidget(self.tag_box)
        layout.addWidget(self.status)
        self.setCentralWidget(root)

        self._processed = load_processed(self.inbox)
        self._failed: set[str] = set()
        self._stability: dict[Path, tuple[int, int, int]] = {}
        self._queued: set[Path] = set()
        self._prompt_open = False
        self._worker: VideoWorker | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(1500)
        self._timer.timeout.connect(self._poll)
        self._timer.start()
        QTimer.singleShot(0, self._poll)

    def _reload_tag_box(self) -> None:
        history = load_player_tag_history(self.inbox)
        current = self.tag_box.currentText().strip() if hasattr(self, "tag_box") else ""
        self.tag_box.blockSignals(True)
        self.tag_box.clear()
        self.tag_box.addItems(history)
        if current:
            index = self.tag_box.findText(current)
            if index >= 0:
                self.tag_box.setCurrentIndex(index)
            else:
                self.tag_box.setEditText(current)
        elif history:
            self.tag_box.setCurrentIndex(0)
        self.tag_box.blockSignals(False)

    def _current_tag(self) -> str:
        return self.tag_box.currentText().strip()

    def _remember_tag(self, tag: str) -> None:
        cleaned = tag.strip()
        if not cleaned:
            return
        save_player_tag(self.inbox, cleaned)
        self._reload_tag_box()
        index = self.tag_box.findText(cleaned)
        if index >= 0:
            self.tag_box.setCurrentIndex(index)
        else:
            self.tag_box.setEditText(cleaned)

    def _find_videos(self) -> list[Path]:
        videos: list[Path] = []
        for folder in (self.inbox / "clean", self.inbox / "cheating"):
            videos.extend(
                path for path in folder.rglob("*")
                if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
            )
        return videos

    def _key_for(self, path: Path) -> str | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return processed_key(path, stat.st_size, stat.st_mtime_ns)

    def _poll(self) -> None:
        for path in self._find_videos():
            if path in self._queued:
                continue
            key = self._key_for(path)
            if key is None or key in self._processed or key in self._failed:
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
                self._queued.add(path)

        if self._worker is None and not self._prompt_open:
            next_video = next((path for path in self._find_videos() if path in self._queued), None)
            if next_video is not None:
                self._start_video(next_video)

    def _label_for(self, path: Path) -> str:
        return label_for_path(self.inbox, path)

    def _start_video(self, path: Path) -> None:
        if self._worker is not None or self._prompt_open:
            return
        try:
            _fps, duration = probe_video(path)
            short = is_short_clip(duration, self.processing_args.before, self.processing_args.after)
        except RuntimeError as exc:
            self._queued.discard(path)
            self.status.setText(f"Could not read {path.name}: {exc}")
            return

        history = load_player_tag_history(self.inbox)
        current = self._current_tag() or load_player_tag(self.inbox)
        items = history or ([current] if current else [""])
        self._prompt_open = True
        try:
            tag, accepted = QInputDialog.getItem(
                self,
                "Player for this video",
                f"{path.name}\n\nPress OK to keep this name, or type/select a different tag:",
                items,
                0,
                True,
            )
        finally:
            self._prompt_open = False
        tag = tag.strip()
        if not accepted:
            self._queued.discard(path)
            key = self._key_for(path)
            if key is not None:
                self._failed.add(key)
            self.status.setText(f"Skipped {path.name}. Replace the file to retry.")
            return
        if tag:
            self._remember_tag(tag)
        if not tag and not short:
            self.status.setText(
                f"Waiting on {path.name}. Enter or select a player tag for long VODs. "
                "Short misfire clips can continue without a tag."
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

    def _mark_processed(self, video: Path) -> None:
        key = self._key_for(video)
        if key is None:
            return
        self._processed.add(key)
        save_processed(self.inbox, self._processed)

    def _on_complete(self, video: Path, message: str, count: int) -> None:
        self._queued.discard(video)
        self._mark_processed(video)
        self.status.setText(
            f"Finished {video.name}: {count} clip(s).\n{message}\n\nStill watching for new videos."
        )
        if count:
            QMessageBox.information(self, "VOD processing complete", f"{video.name}\n\n{count} clip(s) created.\n{message}")
        else:
            QMessageBox.warning(self, "No kill clips found", f"{video.name}\n\n{message}")

    def _on_failure(self, video: Path, message: str) -> None:
        self._queued.discard(video)
        key = self._key_for(video)
        if key is not None:
            self._failed.add(key)
        self.status.setText(
            f"Could not process {video.name}: {message}\n"
            "Keep the original filename. Replace the file to retry."
        )
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
