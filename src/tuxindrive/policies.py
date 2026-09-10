from __future__ import annotations

import ctypes
import platform
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .models import AppSettings


@dataclass(frozen=True, slots=True)
class PowerState:
    battery_percent: int | None = None
    on_ac_power: bool = True


_CACHE_LOCK = threading.Lock()
_POWER_CACHE: tuple[float, PowerState] | None = None
_METERED_CACHE: tuple[float, bool] | None = None
_CACHE_SECONDS = 30.0


def _read_power_state() -> PowerState:
    system = platform.system()
    if system == "Linux":
        values: list[int] = []
        for path in Path("/sys/class/power_supply").glob("BAT*/capacity"):
            try:
                values.append(int(path.read_text(encoding="utf-8").strip()))
            except (OSError, ValueError):
                continue
        online = False
        found_ac = False
        for path in Path("/sys/class/power_supply").glob("*/online"):
            try:
                found_ac = True
                online = online or path.read_text(encoding="utf-8").strip() == "1"
            except OSError:
                continue
        return PowerState(min(values) if values else None, online if found_ac else not values)
    if system == "Windows":
        class Status(ctypes.Structure):
            _fields_ = [
                ("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong),
            ]
        status = Status()
        try:
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
                percent = int(status.BatteryLifePercent)
                return PowerState(None if percent == 255 else percent, status.ACLineStatus == 1)
        except (AttributeError, OSError):
            pass
    if system == "Darwin":
        try:
            result = subprocess.run(
                ["pmset", "-g", "batt"], capture_output=True, text=True,
                timeout=3, check=False,
            )
            match = re.search(r"(\d+)%", result.stdout)
            return PowerState(
                int(match.group(1)) if match else None,
                "AC Power" in result.stdout,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    return PowerState()


def power_state() -> PowerState:
    """Return a short-lived power snapshot without repeatedly waking helpers."""
    global _POWER_CACHE
    now = time.monotonic()
    with _CACHE_LOCK:
        if _POWER_CACHE and now - _POWER_CACHE[0] < _CACHE_SECONDS:
            return _POWER_CACHE[1]
    value = _read_power_state()
    with _CACHE_LOCK:
        _POWER_CACHE = (now, value)
    return value


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    reason: str = "Maximum transfer usage"


class TransferPolicy:
    """Evaluate optional network, battery and schedule transfer limits."""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    def evaluate(self, now: datetime | None = None) -> PolicyDecision:
        if self.settings.network_policy == "maximum":
            return PolicyDecision(True)
        if not self.settings.allow_metered_networks and self._metered():
            return PolicyDecision(False, "Paused on a metered network")
        threshold = max(0, min(100, self.settings.pause_below_battery_percent))
        battery = self._battery_percent()
        if threshold and battery is not None and battery < threshold and not self._on_ac_power():
            return PolicyDecision(False, f"Paused below {threshold}% battery")
        if self.settings.schedule_start and self.settings.schedule_end:
            current = (now or datetime.now()).strftime("%H:%M")
            start, end = self.settings.schedule_start, self.settings.schedule_end
            inside = start <= current < end if start <= end else current >= start or current < end
            if not inside:
                return PolicyDecision(False, f"Paused outside schedule {start}–{end}")
        return PolicyDecision(True, "Transfer policy allows synchronization")

    @staticmethod
    def _battery_percent() -> int | None:
        return power_state().battery_percent

    @staticmethod
    def _on_ac_power() -> bool:
        return power_state().on_ac_power

    @staticmethod
    def _metered() -> bool:
        global _METERED_CACHE
        now = time.monotonic()
        with _CACHE_LOCK:
            if _METERED_CACHE and now - _METERED_CACHE[0] < _CACHE_SECONDS:
                return _METERED_CACHE[1]
        try:
            result = subprocess.run(
                ["nmcli", "-t", "-f", "GENERAL.METERED", "device", "show"],
                capture_output=True, text=True, timeout=5, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            value = False
        else:
            value = any(
                line.rsplit(":", 1)[-1].lower() in {"yes", "guess-yes"}
                for line in result.stdout.splitlines()
            )
        with _CACHE_LOCK:
            _METERED_CACHE = (now, value)
        return value
