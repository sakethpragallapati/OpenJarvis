"""Operate the local PC: launch apps, volume and media keys, brightness, lock.

Windows is implemented with user32 / Start Menu shortcuts / CIM, so nothing
extra needs installing. Every function raises :class:`PCControlError` with a
user-facing message instead of failing silently.
"""

from __future__ import annotations

import difflib
import os
import re
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse


class PCControlError(RuntimeError):
    """An action could not be performed; the message is safe to show."""


def _require_windows(action: str) -> None:
    if sys.platform != "win32":
        raise PCControlError(f"{action} is only supported on Windows for now.")


# ---------------------------------------------------------------------------
# Apps and websites
# ---------------------------------------------------------------------------

# Spoken names that don't match a Start Menu shortcut, mapped to what
# Windows can launch directly (exe on PATH or a protocol handler).
_WINDOWS_BUILTINS: Dict[str, str] = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "paint": "mspaint.exe",
    "file explorer": "explorer.exe",
    "explorer": "explorer.exe",
    "files": "explorer.exe",
    "task manager": "taskmgr.exe",
    "command prompt": "cmd.exe",
    "cmd": "cmd.exe",
    "powershell": "powershell.exe",
    "terminal": "wt.exe",
    "settings": "ms-settings:",
    "bluetooth settings": "ms-settings:bluetooth",
    "wifi settings": "ms-settings:network-wifi",
    "display settings": "ms-settings:display",
    "sound settings": "ms-settings:sound",
    "camera": "microsoft.windows.camera:",
    "store": "ms-windows-store:",
    "microsoft store": "ms-windows-store:",
    "clock": "ms-clock:",
    "alarms": "ms-clock:",
    "mail": "outlookmail:",
    "calendar": "outlookcal:",
}

_ALIASES: Dict[str, str] = {
    "vs code": "visual studio code",
    "vscode": "visual studio code",
    "code": "visual studio code",
    "chrome": "google chrome",
    "edge": "microsoft edge",
    "word": "word",
    "excel": "excel",
    "powerpoint": "powerpoint",
}


def _start_menu_dirs() -> List[Path]:
    dirs = []
    for env in ("ProgramData", "APPDATA"):
        base = os.environ.get(env)
        if base:
            dirs.append(
                Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            )
    return [d for d in dirs if d.is_dir()]


def _start_menu_shortcuts() -> Dict[str, Path]:
    shortcuts: Dict[str, Path] = {}
    for root in _start_menu_dirs():
        for pattern in ("*.lnk", "*.url", "*.appref-ms"):
            for path in root.rglob(pattern):
                name = path.stem.lower()
                # Skip uninstallers and docs that share an app's folder.
                if "uninstall" in name or name.endswith((" help", " readme")):
                    continue
                shortcuts.setdefault(name, path)
    return shortcuts


_START_APPS_CACHE: Optional[Dict[str, str]] = None


def _start_apps() -> Dict[str, str]:
    """Name -> AppUserModelID for every app in Start, including Store apps.

    Store (UWP/MSIX) apps such as Spotify have no .lnk file, so the
    shortcut scan misses them; ``Get-StartApps`` lists everything.
    """
    global _START_APPS_CACHE
    if _START_APPS_CACHE is None:
        apps: Dict[str, str] = {}
        try:
            out = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    'Get-StartApps | ForEach-Object { $_.Name + "`t" + $_.AppID }',
                ],
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        for line in out.splitlines():
            name, _, app_id = line.partition("\t")
            if name.strip() and app_id.strip():
                apps.setdefault(name.strip().lower(), app_id.strip())
        _START_APPS_CACHE = apps
    return _START_APPS_CACHE


def _best_match(wanted: str, names: List[str]) -> Optional[str]:
    if wanted in names:
        return wanted
    # "spotify" -> "Spotify", "visual studio code" -> "Visual Studio Code"
    starts = sorted(n for n in names if n.startswith(wanted))
    if starts:
        return min(starts, key=len)
    close = difflib.get_close_matches(wanted, names, n=1, cutoff=0.75)
    return close[0] if close else None


def _normalize(name: str) -> str:
    name = re.sub(r"[^\w\s+.#-]", " ", name.lower())
    name = re.sub(r"^(the|open|launch|start)\s+", "", name.strip())
    name = re.sub(r"\s+(app|application|program)$", "", name)
    return " ".join(name.split())


def find_app(name: str) -> Optional[tuple[str, str]]:
    """Return (display name, launch target) for a spoken app name, or None."""
    wanted = _normalize(name)
    if not wanted:
        return None
    if wanted in _WINDOWS_BUILTINS:
        return wanted, _WINDOWS_BUILTINS[wanted]
    wanted = _ALIASES.get(wanted, wanted)
    shortcuts = _start_menu_shortcuts()
    match = _best_match(wanted, list(shortcuts))
    if match is not None:
        return shortcuts[match].stem, str(shortcuts[match])
    if sys.platform == "win32":
        apps = _start_apps()
        match = _best_match(wanted, list(apps))
        if match is not None:
            return match.title(), "shell:AppsFolder\\" + apps[match]
    return None


def open_app(name: str) -> str:
    """Launch an installed app by (fuzzy) name; returns what was opened."""
    _require_windows("Opening apps")
    found = find_app(name)
    if found is None:
        raise PCControlError(
            f"I couldn't find an app called {name!r} in the Start Menu."
        )
    display, target = found
    try:
        if target.startswith("shell:AppsFolder"):
            subprocess.Popen(["explorer.exe", target])
        else:
            os.startfile(target)  # type: ignore[attr-defined]
    except OSError as exc:
        raise PCControlError(f"Windows couldn't open {display}: {exc}") from exc
    return display


_URL_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:(?!\d)", re.IGNORECASE)


def open_website(url: str) -> str:
    """Open an http(s) URL (or bare domain) in the default browser."""
    url = url.strip()
    # Only a bare domain ("example.com", "localhost:3000") gets https://;
    # anything with its own scheme ("javascript:", "file:") keeps it and is
    # rejected below.
    if url and not _URL_SCHEME.match(url):
        url = f"https://{url}"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise PCControlError("Only http and https web addresses can be opened.")
    if not webbrowser.open(url):
        raise PCControlError("No web browser is available.")
    return url


# ---------------------------------------------------------------------------
# Volume and media keys
# ---------------------------------------------------------------------------

_VK = {
    "mute": 0xAD,
    "volume_down": 0xAE,
    "volume_up": 0xAF,
    "next": 0xB0,
    "previous": 0xB1,
    "stop": 0xB2,
    "play_pause": 0xB3,
}
_KEYEVENTF_KEYUP = 0x0002
# Each volume key press moves the Windows mixer by 2 points.
_VOLUME_STEP = 2


def _press(key: str, times: int = 1) -> None:
    _require_windows("Volume and media control")
    import ctypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    vk = _VK[key]
    for _ in range(times):
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)
        time.sleep(0.005)


def change_volume(direction: str, amount: int = 10) -> str:
    if direction not in ("up", "down"):
        raise PCControlError("Volume direction must be 'up' or 'down'.")
    amount = max(_VOLUME_STEP, min(100, int(amount)))
    _press(f"volume_{direction}", max(1, amount // _VOLUME_STEP))
    return f"Volume {direction} by about {amount}%."


def set_volume(level: int) -> str:
    """Set the master volume to ``level`` percent (0-100)."""
    level = max(0, min(100, int(level)))
    _press("volume_down", 100 // _VOLUME_STEP)  # bottom out from any start
    if level:
        _press("volume_up", round(level / _VOLUME_STEP))
    return f"Volume set to {level}%."


def toggle_mute() -> str:
    _press("mute")
    return "Toggled mute."


def media_key(action: str) -> str:
    names = {
        "play_pause": "Play/pause",
        "next": "Next track",
        "previous": "Previous track",
        "stop": "Stopped playback",
    }
    if action not in names:
        raise PCControlError(f"Unknown media action {action!r}.")
    _press(action)
    return f"{names[action]}."


# ---------------------------------------------------------------------------
# Brightness, lock
# ---------------------------------------------------------------------------


def _powershell(script: str) -> str:
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PCControlError(f"PowerShell failed: {exc}") from exc
    if result.returncode != 0:
        raise PCControlError(
            "This display doesn't support software brightness control "
            "(external monitors usually don't)."
        )
    return result.stdout.strip()


def get_brightness() -> int:
    _require_windows("Brightness control")
    out = _powershell(
        "(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness "
        "-ErrorAction Stop | Select-Object -First 1).CurrentBrightness"
    )
    try:
        return int(out.splitlines()[-1])
    except (IndexError, ValueError):
        raise PCControlError("Could not read the screen brightness.") from None


def set_brightness(level: int) -> str:
    _require_windows("Brightness control")
    level = max(0, min(100, int(level)))
    _powershell(
        "$m = Get-CimInstance -Namespace root/WMI -ClassName "
        "WmiMonitorBrightnessMethods -ErrorAction Stop | Select-Object -First 1; "
        f"Invoke-CimMethod -InputObject $m -MethodName WmiSetBrightness "
        f"-Arguments @{{Timeout=1; Brightness={level}}} -ErrorAction Stop | Out-Null"
    )
    return f"Brightness set to {level}%."


def lock_screen() -> str:
    _require_windows("Locking the screen")
    import ctypes

    if not ctypes.windll.user32.LockWorkStation():  # type: ignore[attr-defined]
        raise PCControlError("Windows refused to lock the workstation.")
    return "Screen locked."


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------

_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002


def _win32_clipboard_api():  # type: ignore[no-untyped-def]
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    # Explicit signatures: handles are pointer-sized, and ctypes' default
    # int return type would truncate them on 64-bit Windows.
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    return ctypes, user32, kernel32


def _open_clipboard(user32) -> None:  # type: ignore[no-untyped-def]
    # Another app may hold the clipboard for a moment; retry briefly.
    for _ in range(20):
        if user32.OpenClipboard(None):
            return
        time.sleep(0.05)
    raise PCControlError("The clipboard is busy; try again.")


def read_clipboard() -> str:
    if sys.platform == "win32":
        ctypes, user32, kernel32 = _win32_clipboard_api()
        _open_clipboard(user32)
        try:
            handle = user32.GetClipboardData(_CF_UNICODETEXT)
            if not handle:
                return ""
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                return ""
            try:
                return ctypes.wstring_at(pointer)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()
    for cmd in (
        ["pbpaste"],
        ["wl-paste", "--no-newline"],
        ["xclip", "-o", "-sel", "c"],
    ):
        try:
            return subprocess.run(
                cmd, capture_output=True, text=True, timeout=5, check=True
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
    raise PCControlError("No clipboard tool found (install wl-clipboard or xclip).")


def write_clipboard(text: str) -> None:
    if sys.platform == "win32":
        ctypes, user32, kernel32 = _win32_clipboard_api()
        data = text.encode("utf-16-le") + b"\x00\x00"
        handle = kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(data))
        if not handle:
            raise PCControlError("Out of memory copying to the clipboard.")
        pointer = kernel32.GlobalLock(handle)
        ctypes.memmove(pointer, data, len(data))
        kernel32.GlobalUnlock(handle)
        _open_clipboard(user32)
        try:
            user32.EmptyClipboard()
            if not user32.SetClipboardData(_CF_UNICODETEXT, handle):
                kernel32.GlobalFree(handle)
                raise PCControlError("Windows rejected the clipboard update.")
        finally:
            user32.CloseClipboard()
        return
    for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-i", "-sel", "c"]):
        try:
            subprocess.run(cmd, input=text, text=True, timeout=5, check=True)
            return
        except (OSError, subprocess.SubprocessError):
            continue
    raise PCControlError("No clipboard tool found (install wl-clipboard or xclip).")


__all__ = [
    "PCControlError",
    "change_volume",
    "find_app",
    "get_brightness",
    "lock_screen",
    "media_key",
    "open_app",
    "open_website",
    "read_clipboard",
    "set_brightness",
    "set_volume",
    "toggle_mute",
    "write_clipboard",
]
