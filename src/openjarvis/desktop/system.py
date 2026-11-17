"""System actions: power, screenshots, processes, files, PowerShell."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List

from openjarvis.desktop.winapi import DesktopError, require_windows

_CREATE_NO_WINDOW = 0x08000000


def _run(cmd: List[str], timeout: float = 15) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DesktopError(f"{cmd[0]} failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Power and session
# ---------------------------------------------------------------------------


def sleep_pc() -> str:
    require_windows("Sleep")
    import ctypes

    # hibernate=False, force=False, wakeup events allowed
    ctypes.windll.powrprof.SetSuspendState(False, False, False)
    return "Going to sleep."


def shutdown(kind: str, delay: int = 10) -> str:
    require_windows("Shutting down")
    flag = {"shutdown": "/s", "restart": "/r", "sign_out": "/l"}[kind]
    cmd = (
        ["shutdown", flag]
        if kind == "sign_out"
        else ["shutdown", flag, "/t", str(delay)]
    )
    result = _run(cmd)
    if result.returncode != 0:
        raise DesktopError(
            (result.stderr or result.stdout).strip() or "Windows refused."
        )
    if kind == "sign_out":
        return "Signing out."
    word = "Restarting" if kind == "restart" else "Shutting down"
    return f"{word} in {delay} seconds. Say 'cancel shutdown' to stop it."


def cancel_shutdown() -> str:
    require_windows("Cancelling shutdown")
    result = _run(["shutdown", "/a"])
    if result.returncode != 0:
        return "There was no shutdown to cancel."
    return "Shutdown cancelled."


def empty_recycle_bin() -> str:
    require_windows("Emptying the Recycle Bin")
    import ctypes

    # SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
    result = ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x7)
    if result not in (0, -2147418113):  # S_OK, or E_UNEXPECTED when already empty
        raise DesktopError("Windows couldn't empty the Recycle Bin.")
    return "Emptied the Recycle Bin."


def take_screenshot() -> str:
    from PIL import ImageGrab

    from openjarvis.desktop.winapi import ensure_dpi_aware

    ensure_dpi_aware()
    folder = (
        Path(os.environ.get("USERPROFILE") or Path.home()) / "Pictures" / "Screenshots"
    )
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"Jarvis {datetime.now():%Y-%m-%d %H%M%S}.png"
    ImageGrab.grab(all_screens=True).save(path)
    return f"Saved a screenshot to {path}."


def system_status() -> str:
    import psutil

    parts = [
        f"CPU {psutil.cpu_percent(interval=0.3):.0f}%",
        f"memory {psutil.virtual_memory().percent:.0f}% used",
    ]
    try:
        disk = psutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\")
        parts.append(f"{disk.free / 2**30:.0f} GB free on the system drive")
    except OSError:
        pass
    from openjarvis.pc.power import battery_status

    battery = battery_status()
    if battery is not None:
        parts.append(f"battery {battery.describe()}")
    return ", ".join(parts) + "."


def now_text() -> str:
    now = datetime.now()
    return f"It's {now:%I:%M %p} on {now:%A, %d %B %Y}.".replace(" 0", " ")


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------

_PROTECTED = {
    "system",
    "registry",
    "smss.exe",
    "csrss.exe",
    "wininit.exe",
    "winlogon.exe",
    "services.exe",
    "lsass.exe",
    "svchost.exe",
    "dwm.exe",
    "fontdrvhost.exe",
    "explorer.exe",
    "python.exe",
    "pythonw.exe",
    "ollama.exe",
    "ollama app.exe",
}


def list_processes(limit: int = 15) -> str:
    import psutil

    rows = []
    for proc in psutil.process_iter(["name", "memory_info"]):
        try:
            mem = proc.info["memory_info"].rss if proc.info["memory_info"] else 0
            rows.append((mem, proc.info["name"] or "?"))
        except (psutil.Error, AttributeError):
            continue
    totals: dict[str, int] = {}
    for mem, name in rows:
        totals[name] = totals.get(name, 0) + mem
    top = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return "Top apps by memory: " + ", ".join(
        f"{name.removesuffix('.exe')} {mem / 2**20:.0f} MB" for name, mem in top
    )


def _matching_processes(name: str) -> list:
    import psutil

    wanted = name.strip().lower().removesuffix(".exe")
    aliases = {
        "chrome": "chrome",
        "google chrome": "chrome",
        "edge": "msedge",
        "vs code": "code",
        "vscode": "code",
        "visual studio code": "code",
        "word": "winword",
        "powerpoint": "powerpnt",
        "task manager": "taskmgr",
        "file explorer": "explorer",
    }
    wanted = aliases.get(wanted, wanted)
    procs = []
    for proc in psutil.process_iter(["name"]):
        pname = (proc.info["name"] or "").lower()
        if pname.removesuffix(".exe") == wanted or (
            len(wanted) > 3 and pname.startswith(wanted)
        ):
            procs.append(proc)
    return procs


def kill_process(name: str) -> str:
    import psutil

    procs = _matching_processes(name)
    if not procs:
        return f"No running program called {name}."
    pname = (procs[0].info["name"] or name).lower()
    if pname in _PROTECTED:
        raise DesktopError(f"I won't force-close {pname}; Windows or Jarvis needs it.")
    for proc in procs:
        try:
            proc.kill()
        except psutil.Error:
            pass
    return f"Closed {len(procs)} {pname.removesuffix('.exe')} process" + (
        "es." if len(procs) != 1 else "."
    )


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def _path(text: str, *, must_exist: bool = True) -> Path:
    raw = os.path.expandvars(os.path.expanduser(text.strip().strip('"')))
    if not raw:
        raise DesktopError("No path given.")
    path = Path(raw)
    if not path.is_absolute():
        path = Path(os.environ.get("USERPROFILE") or Path.home()) / path
    if must_exist and not path.exists():
        raise DesktopError(f"{path} doesn't exist.")
    return path


def recycle(path: str) -> str:
    """Send to the Recycle Bin (undoable), never a permanent delete."""
    require_windows("Deleting files")
    import ctypes
    from ctypes import wintypes

    target = _path(path)

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_ushort),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    op = SHFILEOPSTRUCTW()
    op.wFunc = 3  # FO_DELETE
    op.pFrom = str(target) + "\0"  # ctypes adds the second terminator
    # FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    op.fFlags = 0x40 | 0x10 | 0x4 | 0x400
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if result != 0 or op.fAnyOperationsAborted:
        raise DesktopError(f"Windows couldn't delete {target.name} (code {result}).")
    return f"Moved {target.name} to the Recycle Bin."


# ---------------------------------------------------------------------------
# PowerShell
# ---------------------------------------------------------------------------

_SAFE_COMMANDS = re.compile(
    r"^(?:get-\w+|select-\w+|where-object|where|sort-object|sort|format-\w+|"
    r"measure-object|group-object|test-path|test-connection|resolve-dnsname|"
    r"out-string|convertto-json|ipconfig|ping|systeminfo|tasklist|whoami|"
    r"hostname|dir|ls|gci|echo|write-output|nslookup|ver|"
    r"\$psversiontable|\[environment\]::\w+)\b",
    re.IGNORECASE,
)
_UNSAFE_SYNTAX = re.compile(r"[>`]|\$\(|&\s*[\w\"'$]|\b(?:iex|invoke-)", re.IGNORECASE)


def is_read_only_command(command: str) -> bool:
    """True for Get-*-style queries that can't change anything."""
    if not command.strip() or _UNSAFE_SYNTAX.search(command):
        return False
    segments = [s.strip() for s in re.split(r"[|;\n]", command) if s.strip()]
    return all(_SAFE_COMMANDS.match(s) for s in segments)


def run_powershell(command: str, timeout: float = 60) -> str:
    require_windows("Running commands")
    result = _run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        timeout=timeout,
    )
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    text = out or err or "(no output)"
    if len(text) > 3000:
        text = text[:3000] + "\n[... output truncated]"
    status = "" if result.returncode == 0 else f"(exit code {result.returncode})\n"
    return status + text


__all__ = [
    "cancel_shutdown",
    "empty_recycle_bin",
    "is_read_only_command",
    "kill_process",
    "list_processes",
    "now_text",
    "recycle",
    "run_powershell",
    "shutdown",
    "sleep_pc",
    "system_status",
    "take_screenshot",
]
