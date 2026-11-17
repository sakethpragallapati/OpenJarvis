"""Thin ctypes layer over user32: windows, keyboard, mouse, launching.

Everything here is Windows-only and raises :class:`DesktopError` with a
message that is safe to read out to the user.
"""

from __future__ import annotations

import concurrent.futures
import functools
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Sequence


class DesktopError(RuntimeError):
    """An action could not be performed; the message is user-facing."""


def require_windows(action: str = "This") -> None:
    if sys.platform != "win32":
        raise DesktopError(f"{action} is only supported on Windows.")


_DPI_DONE = False


def ensure_dpi_aware() -> None:
    """Use physical pixels so screenshots, UIA rects and clicks agree."""
    global _DPI_DONE
    if _DPI_DONE or sys.platform != "win32":
        return
    _DPI_DONE = True
    import ctypes

    try:
        # PER_MONITOR_AWARE_V2
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass


@functools.lru_cache(maxsize=1)
def _user32() -> Any:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsZoomed.argtypes = [wintypes.HWND]
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.PostMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
    user32.VkKeyScanW.restype = ctypes.c_short
    return user32


class _ComWorker:
    """One thread for all COM work (UI Automation, Windows Search).

    COM objects belong to the thread that created them, and tool calls run
    on arbitrary worker threads, so every COM call is funnelled through here.
    """

    def __init__(self) -> None:
        self._pool: Optional[concurrent.futures.ThreadPoolExecutor] = None
        self._lock = threading.Lock()

    @staticmethod
    def _init() -> None:
        try:
            import comtypes

            comtypes.CoInitialize()
        except Exception:  # pragma: no cover - non-Windows / no comtypes
            pass

    def run(self, fn: Callable[[], Any], timeout: float = 25) -> Any:
        with self._lock:
            if self._pool is None:
                self._pool = concurrent.futures.ThreadPoolExecutor(
                    1, thread_name_prefix="openjarvis-com", initializer=self._init
                )
        if threading.current_thread().name.startswith("openjarvis-com"):
            return fn()  # already on the COM thread
        return self._pool.submit(fn).result(timeout=timeout)


COM = _ComWorker()


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

_GW_OWNER = 4
_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080
_WM_CLOSE = 0x0010
_SW = {"minimize": 6, "maximize": 3, "restore": 9, "show": 5}
_IGNORED_TITLES = {"program manager", "windows input experience"}


@dataclass(slots=True)
class WindowInfo:
    hwnd: int
    title: str
    process: str
    pid: int
    minimized: bool = False
    maximized: bool = False

    def describe(self) -> str:
        state = " (minimized)" if self.minimized else ""
        return f"{self.title} [{self.process}]{state}"


def _window_title(user32: Any, hwnd: int) -> str:
    import ctypes

    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _is_cloaked(hwnd: int) -> bool:
    import ctypes
    from ctypes import wintypes

    cloaked = wintypes.DWORD(0)
    try:
        ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd), 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
        )
    except (AttributeError, OSError):
        return False
    return bool(cloaked.value)


def _process_of(user32: Any, hwnd: int) -> tuple[int, str]:
    import ctypes
    from ctypes import wintypes

    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    name = ""
    try:
        import psutil

        name = psutil.Process(pid.value).name()
    except Exception:
        pass
    return pid.value, name


def _info(user32: Any, hwnd: int) -> WindowInfo:
    pid, process = _process_of(user32, hwnd)
    return WindowInfo(
        hwnd=int(hwnd),
        title=_window_title(user32, hwnd),
        process=process,
        pid=pid,
        minimized=bool(user32.IsIconic(hwnd)),
        maximized=bool(user32.IsZoomed(hwnd)),
    )


def list_windows() -> List[WindowInfo]:
    """Visible top-level app windows, front-most first (Alt-Tab order)."""
    require_windows("Window control")
    import ctypes
    from ctypes import wintypes

    user32 = _user32()
    handles: List[int] = []
    proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def collect(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, _GW_OWNER):
            return True
        if user32.GetWindowLongW(hwnd, _GWL_EXSTYLE) & _WS_EX_TOOLWINDOW:
            return True
        if user32.GetWindowTextLengthW(hwnd) <= 0 or _is_cloaked(hwnd):
            return True
        handles.append(hwnd)
        return True

    user32.EnumWindows(proc_type(collect), 0)
    windows = [_info(user32, h) for h in handles]
    return [w for w in windows if w.title.strip().lower() not in _IGNORED_TITLES]


def foreground_window() -> Optional[WindowInfo]:
    if sys.platform != "win32":
        return None
    user32 = _user32()
    hwnd = user32.GetForegroundWindow()
    return _info(user32, hwnd) if hwnd else None


TERMINAL_PROCESSES = frozenset(
    {
        "windowsterminal.exe",
        "cmd.exe",
        "conhost.exe",
        "openconsole.exe",
        "powershell.exe",
        "pwsh.exe",
    }
)


JARVIS_WINDOW_TITLE = "J.A.R.V.I.S."


def is_terminal(window: WindowInfo) -> bool:
    """Jarvis itself: its console, or its own window (the tools run inside the
    Jarvis app, so "click Send" must never mean Jarvis's own Send button)."""
    if window.process.lower() in TERMINAL_PROCESSES:
        return True
    return window.pid == os.getpid() or window.title == JARVIS_WINDOW_TITLE


def app_window() -> Optional[WindowInfo]:
    """The front-most window that isn't Jarvis itself (console or window)."""
    for window in list_windows():
        if not is_terminal(window) and not window.minimized:
            return window
    return None


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def find_window(query: str) -> Optional[WindowInfo]:
    """Best window for a spoken name: title words, then process name."""
    wanted = _norm(query)
    wanted = re.sub(r"\b(the|window|app|application)\b", " ", wanted)
    wanted = " ".join(wanted.split())
    if not wanted:
        return foreground_window()
    aliases = {
        "chrome": "chrome",
        "google chrome": "chrome",
        "edge": "msedge",
        "vs code": "code",
        "vscode": "code",
        "visual studio code": "code",
        "file explorer": "explorer",
        "explorer": "explorer",
        "files": "explorer",
        "task manager": "taskmgr",
        "terminal": "windowsterminal",
        "word": "winword",
        "excel": "excel",
        "powerpoint": "powerpnt",
        "spotify": "spotify",
    }
    windows = list_windows()
    proc_alias = aliases.get(wanted)
    for window in windows:  # exact process alias first
        stem = window.process.lower().removesuffix(".exe")
        if proc_alias and stem == proc_alias:
            return window
    for window in windows:
        if wanted in _norm(window.title):
            return window
    for window in windows:
        if wanted.replace(" ", "") in window.process.lower():
            return window
    words = wanted.split()
    for window in windows:
        title = _norm(window.title)
        if all(word in title for word in words):
            return window
    return None


def find_windows(query: str) -> List[WindowInfo]:
    """Every window matching ``query`` ("youtube" -> all YouTube tabs' windows)."""
    wanted = " ".join(
        re.sub(r"\b(the|window|windows|app|apps|tab|tabs)\b", " ", _norm(query)).split()
    )
    if not wanted:
        return []
    words = wanted.split()
    found = []
    for window_info in list_windows():
        title = _norm(window_info.title)
        process = window_info.process.lower().removesuffix(".exe")
        if all(w in title for w in words) or wanted.replace(" ", "") == process:
            found.append(window_info)
    return found


def _is_front(user32: Any, hwnd: int) -> bool:
    front = user32.GetForegroundWindow()
    return bool(front) and int(front) == int(hwnd)


def focus_window(hwnd: int, timeout: float = 1.5) -> None:
    """Bring a window to the front and make sure it actually got there.

    Windows only lets the process that sent the last input change the
    foreground window, and otherwise just flashes the taskbar button. Try
    the Alt-key trick, then attach to the foreground thread's input queue;
    raise if the window still isn't in front (so nothing gets typed or
    clicked into whatever else is there).
    """
    import ctypes

    user32 = _user32()
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, _SW["restore"])
    if _is_front(user32, hwnd):
        return
    user32.keybd_event(0x12, 0, 0, 0)
    user32.keybd_event(0x12, 0, 0x0002, 0)
    user32.SetForegroundWindow(hwnd)
    deadline = time.monotonic() + timeout / 2
    while time.monotonic() < deadline:
        if _is_front(user32, hwnd):
            return
        time.sleep(0.05)
    kernel32 = ctypes.windll.kernel32
    front = user32.GetForegroundWindow()
    front_thread = user32.GetWindowThreadProcessId(front, None) if front else 0
    ours = kernel32.GetCurrentThreadId()
    attached = bool(front_thread) and bool(
        user32.AttachThreadInput(ours, front_thread, True)
    )
    try:
        user32.BringWindowToTop(hwnd)
        user32.ShowWindow(hwnd, _SW["show"])
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(ours, front_thread, False)
    deadline = time.monotonic() + timeout / 2
    while time.monotonic() < deadline:
        if _is_front(user32, hwnd):
            return
        time.sleep(0.05)
    raise DesktopError(
        "Windows kept that window in the background, so I didn't act on it. "
        "Click it once and ask again."
    )


def window_at(x: int, y: int) -> int:
    """Top-level window under a screen point (physical pixels)."""
    from ctypes import wintypes

    user32 = _user32()
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    child = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    root = user32.GetAncestor(child, 2) if child else None  # GA_ROOT
    return int(root or 0)


def click_in(hwnd: int, x: int, y: int, **kwargs: Any) -> None:
    """Click only if (x, y) really belongs to ``hwnd``; never click blind."""
    ensure_dpi_aware()
    target = window_at(x, y)
    if target != int(hwnd):
        same_process = False
        if target:
            user32 = _user32()
            import ctypes
            from ctypes import wintypes

            a, b = wintypes.DWORD(0), wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(target, ctypes.byref(a))
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(b))
            same_process = a.value == b.value
        if not same_process:
            raise DesktopError(
                "Another window is covering that spot, so I didn't click."
            )
    click(x, y, **kwargs)


def show_window(hwnd: int, how: str) -> None:
    _user32().ShowWindow(hwnd, _SW[how])


def close_window(hwnd: int) -> None:
    """Ask a window to close, like clicking its X (apps may ask to save)."""
    import ctypes

    if not _user32().PostMessageW(hwnd, _WM_CLOSE, 0, 0):
        if ctypes.get_last_error() == 5:  # ERROR_ACCESS_DENIED (UIPI)
            raise DesktopError(
                "That window runs as administrator, so Windows won't let me "
                "close it. Close it yourself or run Jarvis as administrator."
            )
        raise DesktopError("That window refused to close.")


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    import ctypes
    from ctypes import wintypes

    rect = wintypes.RECT()
    _user32().GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def screen_size() -> tuple[int, int]:
    ensure_dpi_aware()
    user32 = _user32()
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


# ---------------------------------------------------------------------------
# Keyboard and mouse (SendInput)
# ---------------------------------------------------------------------------

_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1
_KEYEVENTF_EXTENDEDKEY = 0x0001
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_MOUSE = {
    "left": (0x0002, 0x0004),
    "right": (0x0008, 0x0010),
    "middle": (0x0020, 0x0040),
}
_MOUSEEVENTF_WHEEL = 0x0800

VK_CODES = {
    "ctrl": 0x11,
    "control": 0x11,
    "shift": 0x10,
    "alt": 0x12,
    "win": 0x5B,
    "windows": 0x5B,
    "cmd": 0x5B,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "tab": 0x09,
    "space": 0x20,
    "spacebar": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "del": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "page up": 0x21,
    "pagedown": 0x22,
    "page down": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "printscreen": 0x2C,
    "prtsc": 0x2C,
    "capslock": 0x14,
    "menu": 0x5D,
    "apps": 0x5D,
    "plus": 0xBB,
    "minus": 0xBD,
    "volumemute": 0xAD,
    "volumedown": 0xAE,
    "volumeup": 0xAF,
    "playpause": 0xB3,
    "nexttrack": 0xB0,
    "prevtrack": 0xB1,
}
VK_CODES.update({f"f{i}": 0x6F + i for i in range(1, 25)})
_EXTENDED = {0x2D, 0x2E, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0x5B, 0x5D}


def _input_structs() -> Any:
    import ctypes
    from ctypes import wintypes

    ulong_ptr = ctypes.c_size_t

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ulong_ptr),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ulong_ptr),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class _UNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _UNION)]

    return INPUT


def _send(events: Sequence[tuple[str, dict[str, int]]]) -> None:
    import ctypes

    INPUT = _input_structs()
    array = (INPUT * len(events))()
    for slot, (kind, fields) in zip(array, events):
        if kind == "key":
            slot.type = _INPUT_KEYBOARD
            for name, value in fields.items():
                setattr(slot.ki, name, value)
        else:
            slot.type = _INPUT_MOUSE
            for name, value in fields.items():
                setattr(slot.mi, name, value)
    sent = ctypes.windll.user32.SendInput(
        len(events), ctypes.byref(array), ctypes.sizeof(INPUT)
    )
    if sent != len(events):
        raise DesktopError(
            "Windows blocked the simulated input (an admin window may be in "
            "front, or the screen is locked)."
        )


def _key_event(vk: int, up: bool) -> tuple[str, dict[str, int]]:
    flags = _KEYEVENTF_KEYUP if up else 0
    if vk in _EXTENDED:
        flags |= _KEYEVENTF_EXTENDEDKEY
    return ("key", {"wVk": vk, "dwFlags": flags})


def _vk_for(name: str) -> int:
    key = name.strip().lower()
    if key in VK_CODES:
        return VK_CODES[key]
    if len(key) == 1:
        if key.isalnum():
            return ord(key.upper())
        code = _user32().VkKeyScanW(key)
        if code != -1:
            return code & 0xFF
    raise DesktopError(f"I don't know the key {name!r}.")


def parse_hotkey(combo: str) -> List[int]:
    """'ctrl+shift+esc' -> [VK_CONTROL, VK_SHIFT, VK_ESCAPE]."""
    combo = combo.strip().lower()
    if not combo:
        raise DesktopError("No keys given.")
    combo = re.sub(r"\+\+$", "+plus", combo)  # "ctrl++" = zoom in
    parts = [p for p in re.split(r"\s*\+\s*", combo) if p]
    return [_vk_for(p) for p in parts]


def press_hotkey(combo: str, times: int = 1) -> None:
    require_windows("Keyboard control")
    keys = parse_hotkey(combo)
    events: List[tuple[str, dict[str, int]]] = []
    for _ in range(max(1, min(int(times), 50))):
        events += [_key_event(vk, False) for vk in keys]
        events += [_key_event(vk, True) for vk in reversed(keys)]
    _send(events)


def paste_text(text: str) -> bool:
    """Paste ``text`` through the clipboard (restoring what was there)."""
    try:
        from openjarvis.pc import control
    except ImportError:
        return False
    previous: Optional[str] = None
    try:
        previous = control.read_clipboard()
    except Exception:
        previous = None
    try:
        control.write_clipboard(text)
    except Exception:
        return False
    press_hotkey("ctrl+v")
    time.sleep(0.35)
    if previous is not None:
        try:
            control.write_clipboard(previous)
        except Exception:
            pass
    return True


def type_text(text: str, *, paste_over: int = 160) -> None:
    """Type any Unicode text into the focused control.

    Long texts (a drafted message, a poem) are pasted instead: typing a
    thousand key events takes seconds and can drop characters.
    """
    require_windows("Keyboard control")
    if len(text) > paste_over and paste_text(text):
        return
    events: List[tuple[str, dict[str, int]]] = []
    for char in text:
        if char == "\n":
            events += [_key_event(0x0D, False), _key_event(0x0D, True)]
            continue
        units = char.encode("utf-16-le")
        for i in range(0, len(units), 2):
            code = int.from_bytes(units[i : i + 2], "little")
            events.append(("key", {"wScan": code, "dwFlags": _KEYEVENTF_UNICODE}))
            events.append(
                (
                    "key",
                    {"wScan": code, "dwFlags": _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP},
                )
            )
    # Chunk so a long dictation doesn't overflow the input queue.
    for start in range(0, len(events), 200):
        _send(events[start : start + 200])
        time.sleep(0.01)


def move_mouse(x: int, y: int) -> None:
    require_windows("Mouse control")
    ensure_dpi_aware()
    if not _user32().SetCursorPos(int(x), int(y)):
        raise DesktopError("Could not move the mouse there.")


def click(x: int, y: int, *, button: str = "left", double: bool = False) -> None:
    move_mouse(x, y)
    down, up = _MOUSE.get(button, _MOUSE["left"])
    clicks = 2 if double else 1
    events: List[tuple[str, dict[str, int]]] = []
    for _ in range(clicks):
        events += [("mouse", {"dwFlags": down}), ("mouse", {"dwFlags": up})]
    _send(events)


def scroll(clicks: int) -> None:
    """Scroll the wheel; positive = up, negative = down (120 units/click)."""
    require_windows("Mouse control")
    import ctypes

    amount = ctypes.c_uint32(int(clicks) * 120 & 0xFFFFFFFF).value
    _send([("mouse", {"dwFlags": _MOUSEEVENTF_WHEEL, "mouseData": amount})])


# ---------------------------------------------------------------------------
# Launching
# ---------------------------------------------------------------------------

_CREATE_NO_WINDOW = 0x08000000
_DETACHED_PROCESS = 0x00000008


def shell_open(target: str) -> None:
    """Open a path, URL, shell: URI or .lnk the way double-clicking would.

    Routed through explorer.exe so the new app is a child of the shell, not
    of this console: apps like VS Code otherwise attach to our console and
    print their logs into the assistant window.
    """
    require_windows("Opening things")
    explorer = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "explorer.exe")
    try:
        subprocess.Popen(
            [explorer, target],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
            close_fds=True,
        )
    except OSError as exc:
        raise DesktopError(f"Windows couldn't open {target}: {exc}") from exc


def start_detached(command: Sequence[str]) -> None:
    """Start a program with arguments, detached from this console."""
    require_windows("Starting programs")
    exe = shutil.which(command[0]) or command[0]
    try:
        subprocess.Popen(
            [exe, *command[1:]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_DETACHED_PROCESS | 0x00000200,  # NEW_PROCESS_GROUP
            close_fds=True,
        )
    except OSError as exc:
        raise DesktopError(f"Couldn't start {command[0]}: {exc}") from exc


__all__ = [
    "COM",
    "TERMINAL_PROCESSES",
    "app_window",
    "is_terminal",
    "DesktopError",
    "VK_CODES",
    "WindowInfo",
    "click",
    "close_window",
    "ensure_dpi_aware",
    "find_window",
    "focus_window",
    "foreground_window",
    "list_windows",
    "move_mouse",
    "parse_hotkey",
    "press_hotkey",
    "require_windows",
    "screen_size",
    "scroll",
    "shell_open",
    "show_window",
    "start_detached",
    "type_text",
    "window_rect",
]
