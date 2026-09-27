"""Safe local-data connectors for automatic AI-tool backups.

These connectors deliberately read documented/local application directories;
they do not impersonate a tool or scrape a web session. Known credential files
and generic private-key patterns are excluded from generated jobs.
The resulting jobs use the ordinary TuxInDrive scheduler and upload mirror.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import platform
import re
import socket
from pathlib import Path
from typing import Mapping

from .models import ConflictPolicy, SyncJob, SyncMode


SECRET_EXCLUDES = (
    "auth.json",
    "**/auth.json",
    "credentials.json",
    "**/credentials.json",
    "*.pem",
    "**/*.pem",
    "*.key",
    "**/*.key",
    "*.p12",
    "**/*.p12",
    "*.pfx",
    "**/*.pfx",
    "id_rsa",
    "**/id_rsa",
    ".env",
    "**/.env",
    "**/.env.*",
    "cache/**",
    "**/cache/**",
    "Cache/**",
    "**/Cache/**",
    "CachedData/**",
    "logs/**",
    "**/logs/**",
    "Logs/**",
    "**/Logs/**",
    "tmp/**",
    "**/tmp/**",
    "**/*.sock",
    "**/*.lock",
    ".git/**",
)


@dataclass(frozen=True, slots=True)
class AIBackupConnector:
    key: str
    name: str
    description: str
    paths: tuple[Path, ...]
    excludes: tuple[str, ...] = ()

    @property
    def available_paths(self) -> tuple[Path, ...]:
        result: list[Path] = []
        for path in self.paths:
            expanded = path.expanduser()
            try:
                if expanded.is_dir() and not expanded.is_symlink():
                    result.append(expanded.resolve())
            except OSError:
                continue
        return tuple(dict.fromkeys(result))

    @property
    def detected(self) -> bool:
        return bool(self.available_paths)


def _environment_path(environment: Mapping[str, str], key: str, fallback: Path) -> Path:
    value = environment.get(key, "").strip()
    return Path(value).expanduser() if value else fallback


def connectors(
    *,
    home: Path | None = None,
    system: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> tuple[AIBackupConnector, ...]:
    """Return platform paths without probing or contacting any AI service."""
    home = (home or Path.home()).expanduser()
    system = system or platform.system()
    environment = os.environ if environment is None else environment
    codex = _environment_path(environment, "CODEX_HOME", home / ".codex")

    if system == "Windows":
        roaming = _environment_path(environment, "APPDATA", home / "AppData" / "Roaming")
        cursor = roaming / "Cursor" / "User"
    elif system == "Darwin":
        cursor = home / "Library" / "Application Support" / "Cursor" / "User"
    else:
        config = _environment_path(environment, "XDG_CONFIG_HOME", home / ".config")
        cursor = config / "Cursor" / "User"

    return (
        AIBackupConnector(
            "codex", "Codex", "Chats, memories, skills and workspace metadata",
            (codex,), ("config.toml",),
        ),
        AIBackupConnector(
            "claude", "Claude Code", "Projects, conversations, commands and settings",
            (home / ".claude",), (".credentials.json", "statsig/**", "telemetry/**"),
        ),
        AIBackupConnector(
            "gemini", "Gemini CLI", "Conversation history and local tool settings",
            (home / ".gemini",), ("oauth_creds.json", "google_accounts.json"),
        ),
        AIBackupConnector(
            "cursor", "Cursor", "Editor AI settings, prompts, snippets and history",
            (cursor,), ("globalStorage/**", "workspaceStorage/**"),
        ),
        AIBackupConnector(
            "continue", "Continue", "Local sessions, prompts and indexing metadata",
            (home / ".continue",),
            ("config.json", "config.yaml", "config.yml", "**/index/**"),
        ),
    )


def connector_by_key(key: str, **kwargs) -> AIBackupConnector:
    for connector in connectors(**kwargs):
        if connector.key == key:
            return connector
    raise ValueError(f"Unknown AI backup connector: {key}")


def safe_remote_component(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-")
    return cleaned[:80] or fallback


def build_backup_jobs(
    selected: list[AIBackupConnector] | tuple[AIBackupConnector, ...],
    *,
    account_remote: str,
    remote_base: str = "AI Backups",
    interval_minutes: int = 60,
    hostname: str | None = None,
) -> list[SyncJob]:
    if not account_remote.strip():
        raise ValueError("Choose a cloud account for AI backups")
    interval = min(1440, max(5, int(interval_minutes)))
    base = "/".join(
        safe_remote_component(part, "AI-Backups")
        for part in remote_base.replace("\\", "/").split("/") if part.strip()
    ) or "AI-Backups"
    machine = safe_remote_component(hostname or socket.gethostname(), "computer")
    jobs: list[SyncJob] = []
    for connector in selected:
        for index, source in enumerate(connector.available_paths, start=1):
            suffix = "" if len(connector.available_paths) == 1 else f"-{index}"
            remote = f"{base}/{machine}/{connector.key}{suffix}"
            jobs.append(SyncJob(
                name=f"{connector.name} automatic backup",
                account_remote=account_remote.strip(),
                local_path=str(source),
                remote_path=remote,
                mode=SyncMode.UPLOAD_ONLY,
                interval_minutes=interval,
                conflict_policy=ConflictPolicy.LOCAL_WINS,
                exclude_patterns=list(dict.fromkeys((*SECRET_EXCLUDES, *connector.excludes))),
                realtime_sync=False,
                version_history=True,
                version_retention_days=7,
                ransomware_protection=True,
                max_delete=25,
                ai_connector=connector.key,
            ))
    return jobs
