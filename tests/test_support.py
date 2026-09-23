from __future__ import annotations

import json
import os
import tempfile
import zipfile
from unittest import mock

import pytest

from src import support


def test_redact_text_hides_common_secret_shapes():
    value = (
        "Bearer abcdefghijklmnopqrstuvwxyz0123456789 "
        "https://example.test/callback?access_token=super-secret&state=keep-private "
        + ("A" * 70)
    )
    redacted = support.redact_text(value)
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in redacted
    assert "super-secret" not in redacted
    assert "keep-private" not in redacted
    assert ("A" * 70) not in redacted
    assert "[REDACTED]" in redacted


def test_support_bundle_excludes_gameplay_data():
    with tempfile.TemporaryDirectory() as temp:
        root = support.Path(temp)
        (root / "config").mkdir()
        (root / "data" / "telemetry").mkdir(parents=True)
        (root / "data" / "models").mkdir(parents=True)
        (root / "config" / "settings.json").write_text(
            json.dumps({"api_key": "secret-value", "capture_path": "C:/private/gameplay.mp4"}),
            encoding="utf-8",
        )
        (root / "data" / "telemetry" / "roll_private.jsonl").write_text("private gameplay telemetry", encoding="utf-8")
        (root / "data" / "models" / "model.onnx").write_bytes(b"model")

        with mock.patch.dict(os.environ, {"LOCALAPPDATA": temp}, clear=False):
            support.log_event("unit_test", authorization="Bearer never-leak")
            bundle = support.create_support_bundle(root)
            with zipfile.ZipFile(bundle) as archive:
                names = set(archive.namelist())
                assert "diagnostics/manifest.json" in names
                assert "README.txt" in names
                assert not any("telemetry" in name for name in names)
                assert not any(name.endswith(".onnx") for name in names)
                merged = "\n".join(
                    archive.read(name).decode("utf-8", errors="replace")
                    for name in names
                    if name.startswith("logs/") or name.startswith("diagnostics/")
                )
                assert "never-leak" not in merged
                assert "secret-value" not in merged
                assert "C:/private/gameplay.mp4" not in merged


def test_default_support_endpoint_is_wired():
    with mock.patch.dict(os.environ, {"CHEATVISION_SUPPORT_UPLOAD_URL": ""}, clear=False):
        assert support.DEFAULT_UPLOAD_URL == "https://cheatvision-support.sensoredrooster-com.workers.dev/upload"
        assert support.health_snapshot()["upload_configured"] is True
