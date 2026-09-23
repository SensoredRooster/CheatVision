from __future__ import annotations

import json
import webbrowser
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QVBoxLayout,
)

from src.support import (
    SESSION_ID,
    create_support_bundle,
    health_snapshot,
    log_event,
    open_logs_folder,
    open_repository,
    report_issue,
    upload_support_bundle,
)


class _UploadWorker(QThread):
    completed = Signal(dict)
    failed = Signal(str)

    def __init__(self, project_root: str | Path):
        super().__init__()
        self.project_root = project_root

    def run(self) -> None:
        try:
            self.completed.emit(upload_support_bundle(self.project_root))
        except Exception as exc:
            self.failed.emit(str(exc))


class SupportDialog(QDialog):
    def __init__(self, project_root: str | Path, parent=None):
        super().__init__(parent)
        self.project_root = Path(project_root)
        self._upload_worker: _UploadWorker | None = None
        self.setWindowTitle("CheatVision — Support & Diagnostics")
        self.resize(760, 560)
        self.setMinimumSize(680, 500)
        self.setModal(True)
        self._build_ui()
        log_event("support_window_opened")

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        title = QLabel("SUPPORT & DIAGNOSTICS")
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        layout.addWidget(title)

        privacy = QLabel(
            "Diagnostics stay local unless you explicitly choose Send Diagnostics to Developer. "
            "Support bundles exclude gameplay video, frames, evidence clips, model files, and gameplay telemetry rolls."
        )
        privacy.setWordWrap(True)
        layout.addWidget(privacy)

        session = QLabel(f"Session ID: {SESSION_ID}")
        session.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(session)

        self.diagnostics = QPlainTextEdit(self)
        self.diagnostics.setReadOnly(True)
        self.diagnostics.setPlainText(json.dumps(health_snapshot(self.project_root), indent=2, default=str))
        layout.addWidget(self.diagnostics, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)

        create_btn = QPushButton("CREATE SUPPORT BUNDLE")
        create_btn.clicked.connect(self._create_bundle)
        buttons.addWidget(create_btn)

        self.send_btn = QPushButton("SEND DIAGNOSTICS TO DEVELOPER")
        self.send_btn.clicked.connect(self._send_bundle)
        buttons.addWidget(self.send_btn)

        logs_btn = QPushButton("OPEN LOGS")
        logs_btn.clicked.connect(open_logs_folder)
        buttons.addWidget(logs_btn)

        issue_btn = QPushButton("REPORT ISSUE")
        issue_btn.clicked.connect(report_issue)
        buttons.addWidget(issue_btn)

        repo_btn = QPushButton("REPOSITORY")
        repo_btn.clicked.connect(open_repository)
        buttons.addWidget(repo_btn)

        share_btn = QPushButton("TESTER SHARE")
        share_btn.clicked.connect(
            lambda: webbrowser.open("https://cheatvision-share.sensoredrooster-com.workers.dev")
        )
        buttons.addWidget(share_btn)

        layout.addLayout(buttons)

        close_btn = QPushButton("CLOSE")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, 0, Qt.AlignRight)

    def _create_bundle(self) -> None:
        try:
            path = create_support_bundle(self.project_root)
            QMessageBox.information(self, "Support bundle created", f"Created:\n{path}")
        except Exception as exc:
            log_event("support_bundle_failed", level="ERROR", error=str(exc))
            QMessageBox.critical(self, "Support bundle failed", str(exc))

    def _send_bundle(self) -> None:
        answer = QMessageBox.question(
            self,
            "Send diagnostics?",
            "Create and send a redacted diagnostic bundle to the CheatVision developer now?\n\n"
            "Nothing is uploaded unless you confirm.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        self.send_btn.setEnabled(False)
        self.send_btn.setText("SENDING…")
        self._upload_worker = _UploadWorker(self.project_root)
        self._upload_worker.completed.connect(self._upload_complete)
        self._upload_worker.failed.connect(self._upload_failed)
        self._upload_worker.finished.connect(self._upload_worker.deleteLater)
        self._upload_worker.start()

    def _upload_complete(self, result: dict) -> None:
        self.send_btn.setEnabled(True)
        self.send_btn.setText("SEND DIAGNOSTICS TO DEVELOPER")
        QMessageBox.information(
            self,
            "Diagnostics sent",
            f"Upload completed successfully.\nHTTP status: {result.get('status')}",
        )

    def _upload_failed(self, error: str) -> None:
        self.send_btn.setEnabled(True)
        self.send_btn.setText("SEND DIAGNOSTICS TO DEVELOPER")
        QMessageBox.critical(
            self,
            "Upload failed",
            error + "\n\nYou can still create a local support bundle and attach it manually.",
        )
