from __future__ import annotations

import faulthandler
import hashlib
import json
import logging
import os
import platform
import re
import sys
import tempfile
import threading
import traceback
import zipfile
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import branded_root
from .error_details import redact_error_text
from .models import AppConfig


LOGGER_NAME = "tuxindrive"
_MAX_BUNDLE_LOG_BYTES = 128 * 1024
_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9])(?:/[A-Za-z0-9._~@%+,:=-]+){2,}")
_WINDOWS_PATH_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:|\\\\[^\\\s]+\\[^\\\s]+)(?:\\[^\\\s:;,'\"<>|?*]+)+"
)


def state_home() -> Path:
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Logs"
    if system == "Darwin":
        return Path.home() / "Library" / "Logs"
    configured = os.environ.get("XDG_STATE_HOME")
    return Path(configured) if configured else Path.home() / ".local" / "state"


def log_directory() -> Path:
    return branded_root(state_home())


def crash_log_path() -> Path:
    return log_directory() / "crash.log"


def application_log_path() -> Path:
    return log_directory() / "tuxindrive.log"


def configure_logging(version: str) -> logging.Logger:
    directory = log_directory()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(
            application_log_path(), maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s")
        )
        logger.addHandler(handler)
        os.chmod(application_log_path(), 0o600)
    logger.info(
        "Starting TuxInDrive %s; Python=%s; platform=%s; display=%s; desktop=%s",
        version,
        platform.python_version(),
        platform.platform(),
        os.environ.get("XDG_SESSION_TYPE", "unknown"),
        os.environ.get("XDG_CURRENT_DESKTOP", "unknown"),
    )
    return logger


def install_crash_handlers(logger: logging.Logger) -> None:
    crash_log_path().touch(mode=0o600, exist_ok=True)
    os.chmod(crash_log_path(), 0o600)
    crash_file = crash_log_path().open("a", encoding="utf-8", buffering=1)
    crash_file.write(
        f"\n=== TuxInDrive process started {datetime.now(timezone.utc).isoformat()} ===\n"
    )
    try:
        faulthandler.enable(crash_file, all_threads=True)
    except (RuntimeError, OSError):
        logger.exception("Could not enable faulthandler")

    def handle_exception(exc_type, exc_value, exc_traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        formatted = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        logger.critical("Unhandled exception\n%s", formatted)
        crash_file.write(formatted)
        crash_file.flush()

    def handle_thread(args: threading.ExceptHookArgs) -> None:
        handle_exception(args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = handle_exception
    threading.excepthook = handle_thread


def log_boot_failure(message: str) -> None:
    directory = log_directory()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    crash_log_path().touch(mode=0o600, exist_ok=True)
    os.chmod(crash_log_path(), 0o600)
    with crash_log_path().open("a", encoding="utf-8") as handle:
        handle.write(
            f"[{datetime.now(timezone.utc).isoformat()}] STARTUP FAILURE\n{message}\n"
        )


def _diagnostic_text(value: str, *, include_paths: bool) -> str:
    """Redact secrets and host paths from support material by default."""
    cleaned = redact_error_text(value)
    home = str(Path.home())
    if home:
        cleaned = cleaned.replace(home, "[home]")
    if not include_paths:
        cleaned = _PATH_PATTERN.sub("[path]", cleaned)
        cleaned = _WINDOWS_PATH_PATTERN.sub("[path]", cleaned)
    return cleaned


def _bounded_regular_tail(path: Path) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            return ""
        size = path.stat(follow_symlinks=False).st_size
        with path.open("rb") as handle:
            if size > _MAX_BUNDLE_LOG_BYTES:
                handle.seek(-_MAX_BUNDLE_LOG_BYTES, os.SEEK_END)
            value = handle.read(_MAX_BUNDLE_LOG_BYTES)
        return value.decode("utf-8", errors="replace")
    except OSError:
        return ""


def create_diagnostic_bundle(
    config: AppConfig,
    destination: Path,
    *,
    include_paths: bool = False,
    runtime: dict[str, object] | None = None,
) -> Path:
    """Create a private, bounded and redacted support archive.

    The default archive intentionally omits account names, remote/local paths,
    credentials, peer identities and file contents.  Path inclusion requires a
    separate caller opt-in and still applies credential redaction.
    """
    from . import __version__
    from .capabilities import capabilities_for
    from .config import cache_root
    from .recovery_advisor import advice_for_error

    target = Path(destination).expanduser()
    if target.suffix.lower() != ".zip":
        target = target.with_suffix(".zip")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    accounts_by_remote = {item.remote: item for item in config.accounts}
    jobs = []
    for job in config.jobs:
        account = accounts_by_remote.get(job.account_remote)
        error_code = advice_for_error(job.last_error).code if job.last_error else ""
        item: dict[str, object] = {
            "id": hashlib.sha256(job.id.encode("utf-8")).hexdigest()[:12],
            "provider": account.provider.value if account else "unknown",
            "mode": job.mode.value,
            "enabled": job.enabled,
            "initialized": job.initialized,
            "has_error": bool(job.last_error),
            "error_category": error_code,
            "last_run": job.last_run,
            "selection": {
                "extensions": sorted(set(job.selective_extensions)),
                "max_size_mb": job.selective_max_size_mb,
                "max_age_days": job.selective_max_age_days,
            },
        }
        if include_paths:
            item["local_path"] = _diagnostic_text(job.local_path, include_paths=True)
            item["remote_path"] = _diagnostic_text(job.remote_path, include_paths=True)
        jobs.append(item)
    providers = sorted({item.provider for item in config.accounts}, key=lambda value: value.value)
    summary = {
        "schema": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tuxindrive_version": __version__,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "session": {
            "type": os.environ.get("XDG_SESSION_TYPE", "unknown"),
            "desktop": os.environ.get("XDG_CURRENT_DESKTOP", "unknown"),
        },
        "privacy": {
            "paths_included": include_paths,
            "credentials_included": False,
            "file_contents_included": False,
        },
        "providers": [
            {
                "provider": provider.value,
                "capabilities": {
                    "streaming": capabilities_for(provider).streaming,
                    "polling": capabilities_for(provider).polling,
                    "hashes": capabilities_for(provider).hashes,
                    "server_move": capabilities_for(provider).server_move,
                },
            }
            for provider in providers
        ],
        "jobs": jobs,
        "runtime": runtime or {},
    }
    log_candidates = [application_log_path(), crash_log_path()]
    transfer_root = cache_root() / "logs"
    try:
        transfer_logs = sorted(
            (item for item in transfer_root.glob("*.log") if not item.is_symlink()),
            key=lambda item: item.stat().st_mtime_ns,
        )[-3:]
    except OSError:
        transfer_logs = []
    log_candidates.extend(transfer_logs)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="diagnostic-", suffix=".zip", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "summary.json", json.dumps(summary, indent=2, sort_keys=True) + "\n"
            )
            for index, source in enumerate(log_candidates):
                content = _bounded_regular_tail(source)
                if not content:
                    continue
                archive.writestr(
                    f"logs/log-{index + 1}.txt",
                    _diagnostic_text(content, include_paths=include_paths),
                )
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return target
