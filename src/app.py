import json
import sys
import traceback
from pathlib import Path

import cv2
from PySide6.QtWidgets import QApplication

from src.ui.main_window import MainWindow

# OpenCV defaults to one pool thread per logical core (20 here). For the small
# per-frame resizes this app does, the extra threads mostly spin-wait: capping
# the pool measured ~25% less total CPU at identical frame rates.
_OPENCV_THREADS = 4


LOCAL_SETTINGS_NAME = "settings.local.json"


def load_settings(project_root: Path | None = None) -> dict:
    """Shipped defaults from config/settings.json, then this machine's own
    saved choices from config/settings.local.json layered on top.

    The local file is git-ignored. It holds whatever the app remembers for
    *this* PC (capture device, MODE pin), so a developer's hardware never
    ends up as the defaults that ship to everyone else."""
    root = Path(project_root) if project_root is not None else Path(__file__).resolve().parent.parent
    settings_path = root / "config" / "settings.json"

    with settings_path.open("r", encoding="utf-8") as handle:
        settings = json.load(handle)

    local_path = root / "config" / LOCAL_SETTINGS_NAME
    if local_path.is_file():
        try:
            with local_path.open("r", encoding="utf-8") as handle:
                local = json.load(handle)
            if isinstance(local, dict):
                settings.update(local)
            else:
                print(f"[SETTINGS] [WARN] {local_path.name} is not a JSON object; ignoring it")
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[SETTINGS] [WARN] could not read {local_path.name}: {exc}")

    settings["project_root"] = str(root)
    return settings


def main() -> None:
    cv2.setNumThreads(_OPENCV_THREADS)
    settings = load_settings()
    app = QApplication(sys.argv)
    window = MainWindow(settings)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    try:
        main()
    except Exception:
        crash_log_path = Path(__file__).resolve().parent.parent / "startup_crash.log"
        with crash_log_path.open("w", encoding="utf-8") as handle:
            traceback.print_exc(file=handle)
        traceback.print_exc()
        print(f"\n[FATAL] Startup failed. Full traceback written to {crash_log_path}", file=sys.stderr)
        sys.exit(1)
