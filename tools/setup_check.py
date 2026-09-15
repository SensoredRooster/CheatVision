#!/usr/bin/env python3
"""Does this PC have everything CheatVision needs? Says what is missing and how to fix it.

    python tools/setup_check.py               report only
    python tools/setup_check.py --get-model   also fetch the YOLO player detector
                                              (installs requirements-train.txt if needed,
                                              then runs tools/export_player_model.py)

Written to run with a bare Python install: nothing outside the standard
library is imported at module level, so it works before `pip install` too.
Exit code 0 when everything required is present, 1 otherwise.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIN_PYTHON = (3, 11)
# (import name, pip name) for everything the review console needs at runtime.
RUNTIME_PACKAGES = (
    ("cv2", "opencv-python"),
    ("numpy", "numpy"),
    ("PySide6", "PySide6"),
    ("mss", "mss"),
    ("onnxruntime", "onnxruntime"),
)
TRAIN_PACKAGES = (("torch", "torch"), ("ultralytics", "ultralytics"))
MIN_MODEL_BYTES = 1_000_000
WINGET_FFMPEG = "winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""
    required: bool = True

    @property
    def tag(self) -> str:
        if self.ok:
            return "[OK]"
        return "[!!]" if self.required else "[--]"


# ---------------------------------------------------------------- helpers
def _venv_python() -> Path:
    return ROOT / ".venv" / "Scripts" / "python.exe"


def _pip_hint(requirements: str) -> str:
    if sys.prefix != sys.base_prefix:
        return f"pip install -r {requirements}"
    if _venv_python().is_file():
        return f".venv\\Scripts\\python.exe -m pip install -r {requirements}   (or run setup.bat)"
    return f"python -m venv .venv  then  .venv\\Scripts\\python.exe -m pip install -r {requirements}   (or run setup.bat)"


def _load_settings() -> dict:
    settings: dict = {}
    shipped = ROOT / "config" / "settings.json"
    if shipped.is_file():
        settings = json.loads(shipped.read_text(encoding="utf-8"))
    local = ROOT / "config" / "settings.local.json"
    if local.is_file():
        try:
            overlay = json.loads(local.read_text(encoding="utf-8"))
            if isinstance(overlay, dict):
                settings.update(overlay)
        except (OSError, json.JSONDecodeError):
            pass
    return settings


def model_path() -> Path:
    raw = str(_load_settings().get("player_detector_model_path", "data/models/yolov8n.onnx") or "data/models/yolov8n.onnx")
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def _ffmpeg_exe() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    # winget installs a link here; a terminal opened before the install does
    # not see it on PATH yet.
    links = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"
    return str(links) if links.is_file() else None


# ---------------------------------------------------------------- checks
def check_windows() -> Check:
    system = platform.system()
    return Check(
        "Windows",
        system == "Windows",
        f"{system} {platform.release()}",
        "" if system == "Windows" else "CheatVision captures through DirectShow and only runs on Windows 10/11.",
    )


def check_python() -> Check:
    version = sys.version_info[:3]
    ok = version >= MIN_PYTHON
    return Check(
        "Python",
        ok,
        f"{version[0]}.{version[1]}.{version[2]} at {sys.executable}",
        "" if ok else f"Install Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer from https://www.python.org/downloads/ and tick 'Add python.exe to PATH'.",
    )


def check_venv() -> Check:
    inside = sys.prefix != sys.base_prefix
    if inside:
        return Check("Environment", True, f"running inside {sys.prefix}", required=False)
    exists = _venv_python().is_file()
    detail = ".venv exists but this check is running outside it" if exists else "no .venv yet"
    fix = (
        "Start the app with run.bat (it uses .venv automatically), or activate it: .\\.venv\\Scripts\\Activate.ps1"
        if exists
        else "Run setup.bat once, or: python -m venv .venv"
    )
    return Check("Environment", False, detail, fix, required=False)


def check_packages() -> Check:
    present: list[str] = []
    missing: list[str] = []
    for import_name, pip_name in RUNTIME_PACKAGES:
        try:
            module = importlib.import_module(import_name)
            version = getattr(module, "__version__", None) or getattr(module, "__version_info__", "")
            present.append(f"{pip_name} {version}".strip())
        except Exception:
            missing.append(pip_name)
    if missing:
        return Check("Packages", False, "missing: " + ", ".join(missing), _pip_hint("requirements.txt"))
    return Check("Packages", True, ", ".join(present))


def check_ffmpeg() -> Check:
    exe = _ffmpeg_exe()
    if exe is None:
        return Check(
            "ffmpeg",
            False,
            "not installed",
            f"{WINGET_FFMPEG}   (or download from https://www.gyan.dev/ffmpeg/builds/ and add its bin folder to PATH). Open a NEW terminal afterwards.",
        )
    on_path = shutil.which("ffmpeg") is not None
    version = ""
    try:
        out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=10)
        version = (out.stdout or out.stderr).splitlines()[0].strip() if (out.stdout or out.stderr) else ""
    except Exception as exc:  # broken install
        return Check("ffmpeg", False, f"{exe} did not run: {exc}", "Reinstall ffmpeg: " + WINGET_FFMPEG)
    if not on_path:
        return Check(
            "ffmpeg",
            False,
            f"installed ({version}) but not on PATH in this window",
            "Close this terminal and open a new one; PATH picks the install up on the next launch.",
        )
    return Check("ffmpeg", True, version)


def check_devices() -> Check:
    exe = _ffmpeg_exe()
    if exe is None:
        return Check("Capture devices", False, "cannot list without ffmpeg", "Install ffmpeg first (see above).", required=False)
    try:
        out = subprocess.run(
            [exe, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        text = (out.stdout or "") + "\n" + (out.stderr or "")
    except Exception as exc:
        return Check("Capture devices", False, f"could not enumerate: {exc}", required=False)
    names: list[str] = []
    for line in text.splitlines():
        if '"' in line and ("(video)" in line or "(audio, video)" in line):
            name = line.split('"')[1].strip()
            if name and name not in names:
                names.append(name)
    kinds: dict[str, str] = {}
    try:
        sys.path.insert(0, str(ROOT))
        from src.core.frame_source import infer_device_kind  # needs cv2/mss installed

        kinds = {name: infer_device_kind(name) for name in names}
    except Exception:
        pass
    if not names:
        return Check(
            "Capture devices",
            False,
            "Windows lists no video device",
            "Plug the capture card in and install its vendor driver (AVerMedia / Elgato / ...); it must appear in the Windows Camera app. A webcam or a virtual camera also counts. Without one, IMPORT a video file.",
            required=False,
        )
    labelled = [f"{name} [{kinds.get(name, 'input')}]" for name in names]
    return Check("Capture devices", True, "; ".join(labelled), required=False)


def check_model() -> Check:
    path = model_path()
    if path.is_file() and path.stat().st_size >= MIN_MODEL_BYTES:
        return Check("Player detector", True, f"{path} ({path.stat().st_size // 1_000_000} MB)", required=False)
    detail = "missing" if not path.is_file() else f"file is too small to be a model ({path.stat().st_size} bytes)"
    return Check(
        "Player detector",
        False,
        f"{path}: {detail}. The app still runs; aim motion is scored, but snap / sticky rules need player boxes.",
        "python tools/setup_check.py --get-model   (downloads about 300 MB once)",
        required=False,
    )


def check_settings() -> Check:
    shipped = ROOT / "config" / "settings.json"
    if not shipped.is_file():
        return Check("Settings", False, "config/settings.json is missing", "Re-download the repo; that file ships with it.")
    try:
        json.loads(shipped.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return Check("Settings", False, f"config/settings.json is not valid JSON: {exc}", "Restore it from git: git checkout config/settings.json")
    local = ROOT / "config" / "settings.local.json"
    if local.is_file():
        try:
            data = json.loads(local.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except Exception as exc:
            return Check(
                "Settings",
                True,
                f"settings.json ok; settings.local.json ignored ({exc})",
                "Delete config/settings.local.json; the app rewrites it from your next pick.",
                required=False,
            )
        return Check("Settings", True, "settings.json ok; settings.local.json ok (your saved picks)")
    return Check("Settings", True, "settings.json ok; no local picks saved yet (normal on a fresh clone)")


def check_folders() -> Check:
    problems: list[str] = []
    for rel in ("data", "logs", "data/models", "data/incidents", "data/recordings"):
        path = ROOT / rel
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except Exception as exc:
            problems.append(f"{rel}: {exc}")
    if problems:
        return Check("Folders", False, "; ".join(problems), "Run from a folder you own (not Program Files), or fix permissions.")
    return Check("Folders", True, "data/, logs/ and their subfolders are writable")


def check_train_extras() -> Check:
    present = []
    for import_name, pip_name in TRAIN_PACKAGES:
        try:
            module = importlib.import_module(import_name)
            present.append(f"{pip_name} {getattr(module, '__version__', '')}".strip())
        except Exception:
            pass
    if present:
        return Check("Training extras", True, ", ".join(present) + " (only needed to fetch the model or retrain)", required=False)
    return Check(
        "Training extras",
        True,
        "not installed (fine: only needed to fetch the model or retrain)",
        required=False,
    )


def run_checks() -> list[Check]:
    return [
        check_windows(),
        check_python(),
        check_venv(),
        check_packages(),
        check_ffmpeg(),
        check_devices(),
        check_model(),
        check_settings(),
        check_folders(),
        check_train_extras(),
    ]


# ---------------------------------------------------------------- model fetch
def get_model() -> bool:
    """Install the training extras if needed, then export the YOLO detector."""
    try:
        importlib.import_module("ultralytics")
    except Exception:
        print("[..] Installing requirements-train.txt (torch + ultralytics, about 300 MB, once) ...")
        result = subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements-train.txt")])
        if result.returncode != 0:
            print("[!!] pip install failed. Check the internet connection and run again.")
            return False
    print("[..] Exporting data/models/yolov8n.onnx (downloads the weights the first time) ...")
    result = subprocess.run([sys.executable, str(ROOT / "tools" / "export_player_model.py")], cwd=str(ROOT))
    if result.returncode != 0:
        print("[!!] Export failed; see the messages above.")
        return False
    return check_model().ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--get-model", action="store_true", help="fetch the YOLO player detector if it is missing")
    args = parser.parse_args()

    if args.get_model and not check_model().ok:
        get_model()

    results = run_checks()
    width = max(len(check.name) for check in results)
    print()
    print("CheatVision setup check")
    print("=" * 60)
    for check in results:
        print(f"{check.tag} {check.name.ljust(width)}  {check.detail}")
        if not check.ok and check.fix:
            print(f"     fix: {check.fix}")
    print("=" * 60)
    required_missing = [check for check in results if check.required and not check.ok]
    optional_missing = [check for check in results if not check.required and not check.ok]
    if required_missing:
        print(f"NOT READY: {len(required_missing)} required item(s) missing (marked [!!]). Apply the fixes above and run this again.")
        return 1
    if optional_missing:
        print("READY to run. Items marked [--] are optional; the fixes above add them.")
    else:
        print("READY: everything is in place. Double-click run.bat or: python main.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
