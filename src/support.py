"""CheatVision local-first support telemetry and diagnostics.

This module handles only application logging, redaction, health snapshots,
support bundle creation, and explicit support uploads.
"""

from __future__ import annotations

import atexit
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
import zipfile
from datetime import datetime, timezone
from pathlib import Path

APP_NAME = "CheatVision"
REPOSITORY_URL = "https://github.com/SensoredRooster/CheatVision"
ISSUES_URL = REPOSITORY_URL + "/issues/new"
DEFAULT_UPLOAD_URL = "https://cheatvision-support.sensoredrooster-com.workers.dev/upload"
SESSION_ID = uuid.uuid4().hex[:12]
STARTED_AT = datetime.now(timezone.utc).isoformat()
MAX_LOG_BYTES = 10 * 1024 * 1024
MAX_BACKUPS = 6
_LOCK = threading.Lock()
_HEARTBEAT_THREAD: threading.Thread | None = None

_SENSITIVE_KEY = re.compile(r"(?i)(token|secret|password|passwd|authorization|cookie|credential|api[_-]?key)")
_BEARER = re.compile(r"(?i)Bearer\s+[A-Za-z0-9._~+/-]+=*")
_QUERY_SECRET = re.compile(r"(?i)([?&](?:code|token|access_token|refresh_token|client_secret|state|password)=)[^&#\s]+")
_LONG_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_-]{56,}(?![A-Za-z0-9])")


def support_root() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    path = base / APP_NAME / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def redact_text(value: str) -> str:
    value = _BEARER.sub("Bearer [REDACTED]", value)
    value = _QUERY_SECRET.sub(r"\1[REDACTED]", value)
    value = _LONG_TOKEN.sub("[REDACTED]", value)
    return value


def _redact(value):
    if isinstance(value, dict):
        return {
            str(key): ("[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _rotate(path: Path) -> None:
    if not path.exists() or path.stat().st_size < MAX_LOG_BYTES:
        return
    path.with_name(path.name + f".{MAX_BACKUPS}").unlink(missing_ok=True)
    for index in range(MAX_BACKUPS - 1, 0, -1):
        src = path.with_name(path.name + f".{index}")
        dst = path.with_name(path.name + f".{index + 1}")
        if src.exists():
            src.replace(dst)
    path.replace(path.with_name(path.name + ".1"))


def log_event(event: str, *, level: str = "INFO", **fields) -> None:
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": SESSION_ID,
        "level": level,
        "event": event,
        **_redact(fields),
    }
    target = support_root() / ("errors.jsonl" if level in {"ERROR", "CRITICAL"} else "cheatvision.jsonl")
    with _LOCK:
        _rotate(target)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(redact_text(json.dumps(record, ensure_ascii=False, default=str)) + "\n")


def _module_version(name: str) -> str | None:
    try:
        module = __import__(name)
        return str(getattr(module, "__version__", "installed"))
    except Exception:
        return None


def _gpu_snapshot() -> dict:
    result = {"nvidia_smi_available": bool(shutil.which("nvidia-smi"))}
    if not result["nvidia_smi_available"]:
        return result
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if completed.returncode == 0:
            result["nvidia_gpus"] = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    except Exception:
        pass
    return result


def health_snapshot(project_root: str | Path | None = None) -> dict:
    root = Path(project_root).resolve() if project_root else Path(__file__).resolve().parent.parent
    return {
        "app": APP_NAME,
        "session_id": SESSION_ID,
        "started_at": STARTED_AT,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "opencv": _module_version("cv2"),
        "numpy": _module_version("numpy"),
        "pyside6": _module_version("PySide6"),
        "onnxruntime": _module_version("onnxruntime"),
        "free_disk_bytes": shutil.disk_usage(root).free,
        "settings_present": (root / "config" / "settings.json").is_file(),
        "local_settings_present": (root / "config" / "settings.local.json").is_file(),
        "model_directory_present": (root / "data" / "models").is_dir(),
        "upload_configured": bool(os.environ.get("CHEATVISION_SUPPORT_UPLOAD_URL", "").strip() or DEFAULT_UPLOAD_URL),
        "repository": REPOSITORY_URL,
        **_gpu_snapshot(),
    }


def create_support_bundle(project_root: str | Path | None = None) -> Path:
    root = Path(project_root).resolve() if project_root else Path(__file__).resolve().parent.parent
    destination = Path(tempfile.gettempdir()) / f"CheatVision-Support-{SESSION_ID}.zip"
    destination.unlink(missing_ok=True)

    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("diagnostics/manifest.json", json.dumps(health_snapshot(root), indent=2))
        archive.writestr(
            "README.txt",
            "CheatVision support bundle. Created locally after explicit user action. "
            "No gameplay video, frames, evidence clips, model files, or gameplay telemetry rolls are included.\n",
        )
        for path in support_root().glob("*"):
            if not path.is_file():
                continue
            if not (
                path.name.startswith("cheatvision.jsonl")
                or path.name.startswith("errors.jsonl")
                or path.suffix.lower() == ".log"
            ):
                continue
            archive.writestr(
                f"logs/{path.name}",
                redact_text(path.read_text(encoding="utf-8", errors="replace")),
            )

        for relative in ("config/settings.json", "config/settings.local.json"):
            path = root / relative
            if not path.is_file():
                continue
            try:
                payload = _redact(json.loads(path.read_text(encoding="utf-8")))
                if isinstance(payload, dict):
                    for key in list(payload):
                        if "path" in key.lower() or "directory" in key.lower():
                            payload[key] = "[LOCAL PATH OMITTED]"
                archive.writestr(
                    f"diagnostics/{Path(relative).name}",
                    json.dumps(payload, indent=2, default=str),
                )
            except Exception as exc:
                log_event("support_config_snapshot_failed", level="ERROR", file=relative, error=str(exc))

    log_event("support_bundle_created", path=str(destination), size_bytes=destination.stat().st_size)
    return destination


def upload_support_bundle(project_root: str | Path | None = None, url: str | None = None) -> dict:
    endpoint = (url or os.environ.get("CHEATVISION_SUPPORT_UPLOAD_URL", "").strip() or DEFAULT_UPLOAD_URL).strip()
    if not endpoint:
        raise RuntimeError("CheatVision support upload endpoint is not configured.")
    bundle = create_support_bundle(project_root)
    request = urllib.request.Request(endpoint, data=bundle.read_bytes(), method="POST")
    request.add_header("Content-Type", "application/zip")
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", "CheatVision/1.0 (+https://github.com/SensoredRooster/CheatVision)")
    request.add_header("X-CheatVision-Session", SESSION_ID)
    request.add_header("X-CheatVision-Filename", bundle.name)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8", errors="replace")
            log_event("support_bundle_uploaded", status=response.status)
            return {"status": response.status, "body": body}
    except Exception as exc:
        log_event("support_upload_failed", level="ERROR", error=str(exc))
        raise


def open_logs_folder() -> None:
    path = support_root()
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def open_repository() -> None:
    webbrowser.open(REPOSITORY_URL)


def report_issue() -> None:
    title = urllib.parse.quote("CheatVision support issue")
    body = urllib.parse.quote(f"Session ID: {SESSION_ID}\nStarted: {STARTED_AT}\n\nDescribe the issue here.")
    webbrowser.open(f"{ISSUES_URL}?title={title}&body={body}")


def install_exception_hooks() -> None:
    original_sys = sys.excepthook
    def sys_hook(exc_type, exc_value, exc_tb):
        try:
            log_event("uncaught_exception", level="ERROR", exception_type=getattr(exc_type, "__name__", str(exc_type)), error=str(exc_value))
        finally:
            original_sys(exc_type, exc_value, exc_tb)
    sys.excepthook = sys_hook

    original_thread = getattr(threading, "excepthook", None)
    if original_thread is not None:
        def thread_hook(args):
            try:
                log_event(
                    "thread_uncaught_exception",
                    level="ERROR",
                    thread=getattr(getattr(args, "thread", None), "name", None),
                    exception_type=getattr(getattr(args, "exc_type", None), "__name__", "unknown"),
                    error=str(getattr(args, "exc_value", "")),
                )
            finally:
                original_thread(args)
        threading.excepthook = thread_hook


def start_heartbeat(interval: float = 1.0) -> None:
    global _HEARTBEAT_THREAD
    if _HEARTBEAT_THREAD and _HEARTBEAT_THREAD.is_alive():
        return
    def worker() -> None:
        while True:
            try:
                log_event("heartbeat")
            except Exception:
                pass
            time.sleep(max(1.0, interval))
    _HEARTBEAT_THREAD = threading.Thread(target=worker, name="CheatVisionSupportHeartbeat", daemon=True)
    _HEARTBEAT_THREAD.start()


def _log_shutdown() -> None:
    try:
        log_event("app_stop")
    except Exception:
        pass


install_exception_hooks()
atexit.register(_log_shutdown)
log_event("app_support_initialized", health=health_snapshot())
