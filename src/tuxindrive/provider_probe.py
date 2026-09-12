"""Bounded, conservative runtime probes for rclone-backed providers."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import Callable

from .capabilities import ProviderCapabilities, capabilities_for
from .error_details import redact_error_text
from .models import Account


@dataclass(frozen=True, slots=True)
class LiveCapabilities:
    provider: str
    verified: bool
    hashes: bool
    server_move: bool
    server_copy: bool
    change_notify: bool
    detail: str = ""

    def to_dict(self) -> dict[str, str | bool]:
        return asdict(self)


Runner = Callable[..., subprocess.CompletedProcess[str]]


class ProviderCapabilityProbe:
    def __init__(
        self,
        executable: str = "rclone",
        *,
        ttl_seconds: float = 900.0,
        runner: Runner = subprocess.run,
    ) -> None:
        self.executable = executable
        self.ttl_seconds = max(30.0, ttl_seconds)
        self.runner = runner
        self._cache: dict[str, tuple[float, LiveCapabilities]] = {}

    @staticmethod
    def _fallback(account: Account, static: ProviderCapabilities, detail: str) -> LiveCapabilities:
        return LiveCapabilities(
            account.provider.value, False, static.hashes, static.server_move,
            False, False, detail,
        )

    def probe(self, account: Account, *, now: float | None = None) -> LiveCapabilities:
        current = time.monotonic() if now is None else now
        cached = self._cache.get(account.remote)
        if cached and current - cached[0] < self.ttl_seconds:
            return cached[1]
        static = capabilities_for(account.provider)
        if account.backend != "rclone":
            result = self._fallback(account, static, "Native adapter; conservative declared capabilities")
            self._cache[account.remote] = (current, result)
            return result
        try:
            completed = self.runner(
                [self.executable, "backend", "features", f"{account.remote}:"],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            if completed.returncode:
                detail = redact_error_text((completed.stderr or "probe failed").strip())[:240]
                result = self._fallback(account, static, detail)
            else:
                payload = json.loads(completed.stdout)
                features = payload.get("Features") if isinstance(payload, dict) else None
                features = features if isinstance(features, dict) else {}
                hashes = payload.get("Hashes") if isinstance(payload, dict) else []
                result = LiveCapabilities(
                    account.provider.value,
                    True,
                    static.hashes and isinstance(hashes, list) and bool(hashes),
                    static.server_move and bool(features.get("Move") or features.get("DirMove")),
                    bool(features.get("Copy")),
                    static.polling and bool(features.get("ChangeNotify")),
                    "Runtime capabilities verified; undeclared features remain disabled",
                )
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, json.JSONDecodeError) as exc:
            result = self._fallback(account, static, redact_error_text(str(exc))[:240])
        self._cache[account.remote] = (current, result)
        return result

    def invalidate(self, remote: str | None = None) -> None:
        if remote is None:
            self._cache.clear()
        else:
            self._cache.pop(remote, None)
