"""Battery state and free GPU memory, for laptop-aware voice mode."""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BatteryStatus:
    """Snapshot of the battery; fields are None when the OS doesn't say."""

    percent: Optional[int]
    plugged_in: Optional[bool]
    seconds_left: Optional[int] = None

    def describe(self) -> str:
        parts = []
        if self.percent is not None:
            parts.append(f"{self.percent}%")
        if self.plugged_in is True:
            parts.append("plugged in")
        elif self.plugged_in is False:
            parts.append("on battery")
        if self.seconds_left:
            hours, rem = divmod(self.seconds_left, 3600)
            parts.append(f"about {hours}h {rem // 60}m left")
        return ", ".join(parts) or "unknown"


def _windows_battery() -> Optional[BatteryStatus]:
    import ctypes
    from ctypes import wintypes

    class SYSTEM_POWER_STATUS(ctypes.Structure):
        _fields_ = [
            ("ACLineStatus", wintypes.BYTE),
            ("BatteryFlag", wintypes.BYTE),
            ("BatteryLifePercent", wintypes.BYTE),
            ("SystemStatusFlag", wintypes.BYTE),
            ("BatteryLifeTime", wintypes.DWORD),
            ("BatteryFullLifeTime", wintypes.DWORD),
        ]

    status = SYSTEM_POWER_STATUS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):  # type: ignore[attr-defined]
        return None
    flag = status.BatteryFlag & 0xFF
    if flag == 128:  # "no system battery" -- a desktop PC
        return None
    percent = status.BatteryLifePercent & 0xFF
    line = status.ACLineStatus & 0xFF
    life = status.BatteryLifeTime
    return BatteryStatus(
        percent=None if percent == 255 else percent,
        plugged_in=None if line == 255 else line == 1,
        seconds_left=None if life == 0xFFFFFFFF else int(life),
    )


def _linux_battery() -> Optional[BatteryStatus]:
    supplies = Path("/sys/class/power_supply")
    batteries = sorted(supplies.glob("BAT*"))
    if not batteries:
        return None
    try:
        percent = int((batteries[0] / "capacity").read_text().strip())
        state = (batteries[0] / "status").read_text().strip().lower()
    except (OSError, ValueError):
        return None
    plugged: Optional[bool] = state in ("charging", "full", "not charging")
    for ac in supplies.glob("A[CD]*"):
        try:
            plugged = (ac / "online").read_text().strip() == "1"
            break
        except OSError:
            continue
    return BatteryStatus(percent=percent, plugged_in=plugged)


def _macos_battery() -> Optional[BatteryStatus]:
    try:
        out = subprocess.run(
            ["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    if "InternalBattery" not in out:
        return None
    percent = None
    for token in out.replace(";", " ").split():
        if token.endswith("%") and token[:-1].isdigit():
            percent = int(token[:-1])
            break
    return BatteryStatus(percent=percent, plugged_in="AC Power" in out)


def battery_status() -> Optional[BatteryStatus]:
    """Return the battery state, or None on machines without a battery."""
    try:
        if sys.platform == "win32":
            return _windows_battery()
        if sys.platform == "darwin":
            return _macos_battery()
        return _linux_battery()
    except Exception as exc:  # never let a power probe break the caller
        logger.debug("Battery probe failed: %s", exc)
        return None


def on_battery() -> bool:
    """True only when we know the laptop is running unplugged."""
    status = battery_status()
    return status is not None and status.plugged_in is False


def free_vram_mb() -> Optional[int]:
    """Free memory on the first NVIDIA GPU in MiB, or None if unknown."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
        return int(float(out.splitlines()[0].strip()))
    except (OSError, subprocess.TimeoutExpired, IndexError, ValueError):
        return None


__all__ = ["BatteryStatus", "battery_status", "free_vram_mb", "on_battery"]
