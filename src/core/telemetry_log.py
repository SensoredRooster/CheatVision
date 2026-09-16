from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any


class RollingTelemetryLog:
    """Append-only JSONL of live telemetry under ``data/telemetry/``.

    Runs in the analysis worker thread: each ``write`` is a short lock + append.
    On ``close``, the ``.partial.jsonl`` file is renamed to the finished name so
    you can tell in-progress rolls from complete ones when sifting.
    """

    def __init__(self, project_root: str | Path, *, sample_hz: float = 20.0):
        self.root = Path(project_root) / "data" / "telemetry"
        self.root.mkdir(parents=True, exist_ok=True)
        self._min_interval = 1.0 / max(1.0, float(sample_hz))
        self._lock = threading.Lock()
        self._last_write = 0.0
        self._handle = None
        self._partial_path: Path | None = None
        self._started_at = datetime.now()
        stamp = self._started_at.strftime("%Y%m%d-%H%M%S")
        self._partial_path = self.root / f"roll_{stamp}.partial.jsonl"
        self._handle = self._partial_path.open("a", encoding="utf-8")

    @property
    def path(self) -> Path | None:
        return self._partial_path

    def write(self, row: dict[str, Any], *, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            if self._handle is None:
                return
            if not force and (now - self._last_write) < self._min_interval:
                return
            self._last_write = now
            payload = dict(row)
            payload.setdefault("ts", datetime.now().isoformat(timespec="milliseconds"))
            # Drop bulky fields that drown a sift pass.
            payload.pop("path", None)
            payload.pop("residuals", None)
            self._handle.write(json.dumps(payload, separators=(",", ":"), default=_json_default))
            self._handle.write("\n")
            self._handle.flush()

    def close(self) -> Path | None:
        """Flush and rename ``.partial.jsonl`` -> ``.jsonl``. Returns finished path."""
        with self._lock:
            if self._handle is None or self._partial_path is None:
                return None
            try:
                self._handle.flush()
            finally:
                self._handle.close()
                self._handle = None
            finished = self._partial_path.with_name(self._partial_path.name.replace(".partial.jsonl", ".jsonl"))
            if self._partial_path.exists():
                self._partial_path.replace(finished)
            self._partial_path = None
            return finished


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return str(value)
