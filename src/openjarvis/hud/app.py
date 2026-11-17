"""The JARVIS window: the voice assistant in its own window, no console.

``jarvis-app.exe`` (or ``jarvis app``) opens a window with the animated ring,
the conversation and a command box, and runs the same voice loop as
``jarvis listen`` behind it. The loop reports what it is doing through
:mod:`openjarvis.core.live`; this module forwards those events to the page
and the page's buttons back to the loop.
"""

from __future__ import annotations

import collections
import ctypes
import io
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from openjarvis.core import live

logger = logging.getLogger(__name__)

TITLE = "J.A.R.V.I.S."
_MUTEX_NAME = "Local\\OpenJarvisHUD"
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_WEB = Path(__file__).resolve().parent / "web" / "index.html"


def app_dir() -> Path:
    path = Path.home() / ".openjarvis" / "app"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_path() -> Path:
    path = Path.home() / ".openjarvis" / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path / "jarvis-app.log"


# ---------------------------------------------------------------------------
# Output: what `jarvis listen` prints goes to a log file and the boot lines
# ---------------------------------------------------------------------------


class _Sink(io.TextIOBase):
    """Stands in for stdout/stderr (a windowless app has neither)."""

    def __init__(self, file: Any) -> None:
        super().__init__()
        self._file = file
        self._buffer = ""
        self._lock = threading.Lock()

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"

    @property
    def errors(self) -> str:  # type: ignore[override]
        return "replace"

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            text = str(text)
        with self._lock:
            try:
                self._file.write(text)
                self._file.flush()
            except Exception:
                pass
            self._buffer += text
            lines = self._buffer.split("\n")
            self._buffer = lines.pop()
        for line in lines:
            line = _ANSI.sub("", line).strip()
            if line:
                live.emit("log", text=line[:300])
        return len(text)

    def flush(self) -> None:
        try:
            self._file.flush()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Events to the page
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        if isinstance(value, dict):
            return {str(k): _jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_jsonable(v) for v in value]
        return str(value)


class Bridge:
    """Batches live events and hands them to the page ~25 times a second;
    loudness updates are merged to the latest value."""

    def __init__(self) -> None:
        self.window: Any = None
        self.history: Deque[List[Any]] = collections.deque(maxlen=500)
        self._pending: List[List[Any]] = []
        self._levels: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self.busy = False  # between "thinking" and the reply
        self.paused = False  # always-listening paused (wake word only)
        self._thread = threading.Thread(
            target=self._pump, name="jarvis-hud-pump", daemon=True
        )

    def start(self) -> "Bridge":
        live.subscribe(self.on_event)
        self._thread.start()
        return self

    def on_event(self, kind: str, data: Dict[str, Any]) -> None:
        if kind in ("mic", "voice"):
            with self._lock:
                self._levels[kind] = dict(data)
            return
        if kind == "thinking":
            self.busy = True
        elif kind in ("reply", "error", "interrupted", "fatal"):
            self.busy = False
        if kind == "paused":
            self.paused = True
        elif kind in ("listening", "heard"):
            self.paused = False
        event = [kind, _jsonable(data)]
        with self._lock:
            self._pending.append(event)
            if kind != "say":  # speech timing is live only
                self.history.append(event)

    def _pump(self) -> None:
        while True:
            time.sleep(0.04)
            with self._lock:
                batch = self._pending + [[k, v] for k, v in self._levels.items()]
                self._pending, self._levels = [], {}
            if not batch or self.window is None:
                continue
            try:
                self.window.evaluate_js(
                    "window.__jarvis && window.__jarvis(%s)" % json.dumps(batch)
                )
            except Exception:
                logger.debug("Could not reach the page", exc_info=True)


class Api:
    """What the page can call (``window.pywebview.api``)."""

    def __init__(self, app: "JarvisApp") -> None:
        self._app = app

    def send(self, text: str) -> None:
        live.type_command(str(text or ""))

    def stop(self) -> None:
        live.stop_speaking()
        self._app.interrupt_request()

    def pause(self, paused: bool) -> None:
        live.want_pause(bool(paused))

    def history(self) -> List[List[Any]]:
        return list(self._app.bridge.history)

    def open_log(self) -> None:
        os.startfile(str(log_path()))  # type: ignore[attr-defined]

    def open_url(self, url: str) -> None:
        if str(url).startswith(("http://", "https://")):
            import webbrowser

            webbrowser.open(str(url))

    def restart(self) -> None:
        self._app.restart()

    def prefs(self) -> Dict[str, Any]:
        """Window preferences (is the conversation panel open?)."""
        return _load_prefs()

    def set_pref(self, key: str, value: Any) -> None:
        if key in _PREF_KEYS:
            prefs = _load_prefs()
            prefs[key] = value
            _prefs_path().write_text(json.dumps(prefs, indent=2), encoding="utf-8")

    # -- settings: local model or Groq -------------------------------------------

    def settings(self) -> Dict[str, Any]:
        from openjarvis.hud import settings

        data = settings.current()
        data["problem"] = settings.engine_problem()
        return data

    def save_groq_key(self, key: str) -> Dict[str, Any]:
        """Check the key with Groq, then keep it in the Credential Manager.
        The key itself is never sent back to the page."""
        from openjarvis.hud import settings

        ok, message, models = settings.save_groq_key(str(key or ""))
        return {"ok": ok, "message": message, "models": models}

    def remove_groq_key(self) -> None:
        from openjarvis.hud import settings

        settings.remove_groq_key()

    def groq_models(self) -> List[str]:
        from openjarvis.hud import settings

        return settings.groq_models()

    def save_settings(self, choice: Dict[str, Any]) -> Dict[str, Any]:
        """Save the choices and restart Jarvis with them."""
        from openjarvis.hud import settings

        ok, message = settings.save(dict(choice or {}))
        if ok:
            threading.Timer(0.8, self._app.restart).start()
        return {"ok": ok, "message": message}


_PREF_KEYS = {"panel_open"}


def _prefs_path() -> Path:
    return app_dir() / "hud.json"


def _load_prefs() -> Dict[str, Any]:
    try:
        data = json.loads(_prefs_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# Windows details
# ---------------------------------------------------------------------------


def _already_running() -> bool:
    """One Jarvis at a time (two would both answer every command)."""
    if sys.platform != "win32":
        return False
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    JarvisApp.mutex = handle
    return kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


_SHOW_EVENT = "Local\\OpenJarvisHUD.Show"


def _focus_existing() -> None:
    """Jarvis is already running (maybe hidden in the tray): show it."""
    kernel32, user32 = ctypes.windll.kernel32, ctypes.windll.user32
    user32.AllowSetForegroundWindow(-1)  # ASFW_ANY: let it come to the front
    event = kernel32.OpenEventW(0x0002, False, _SHOW_EVENT)  # EVENT_MODIFY_STATE
    if event:
        kernel32.SetEvent(event)
        kernel32.CloseHandle(event)
        return
    hwnd = user32.FindWindowW(None, TITLE)
    if hwnd:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)


def _no_console_windows() -> None:
    """Console programs Jarvis starts (PowerShell, schtasks, ...) would each
    flash a black window from a windowless app; run them hidden."""
    if sys.platform != "win32":
        return
    no_window = 0x08000000  # CREATE_NO_WINDOW
    new_console = 0x00000010
    original = subprocess.Popen.__init__

    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        flags = int(kwargs.get("creationflags", 0) or 0)
        if not flags & new_console:
            kwargs["creationflags"] = flags | no_window
        original(self, *args, **kwargs)

    subprocess.Popen.__init__ = init  # type: ignore[method-assign]


def _style_window(icon: Optional[Path]) -> None:
    """Dark title bar in the ring's colours, and the JARVIS icon."""
    if sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    hwnd = 0
    for _ in range(50):
        hwnd = user32.FindWindowW(None, TITLE)
        if hwnd:
            break
        time.sleep(0.1)
    if not hwnd:
        return
    dwm = ctypes.windll.dwmapi

    def attribute(name: int, value: int) -> None:
        data = ctypes.c_int(value)
        dwm.DwmSetWindowAttribute(hwnd, name, ctypes.byref(data), ctypes.sizeof(data))

    attribute(20, 1)  # DWMWA_USE_IMMERSIVE_DARK_MODE
    attribute(35, 0x00060501)  # DWMWA_CAPTION_COLOR (0x00BBGGRR): near black
    attribute(36, 0x00CCE500)  # DWMWA_TEXT_COLOR: teal
    attribute(34, 0x00403A00)  # DWMWA_BORDER_COLOR: dim teal
    if icon is not None and icon.exists():
        load = user32.LoadImageW
        load.restype = ctypes.c_void_p
        for which, size in ((0, 16), (1, 32)):  # ICON_SMALL, ICON_BIG
            handle = load(
                None, str(icon), 1, size, size, 0x10
            )  # IMAGE_ICON, LR_LOADFROMFILE
            if handle:
                user32.SendMessageW(hwnd, 0x80, which, ctypes.c_void_p(handle))


# ---------------------------------------------------------------------------
# The tray icon (by the clock)
# ---------------------------------------------------------------------------


class Tray:
    """Jarvis's icon by the clock: click to open, right-click for Pause /
    Quit. It's how you know Jarvis is still running with its window shut."""

    def __init__(self, app: "JarvisApp", icon: Optional[Path]) -> None:
        self._app = app
        self._icon_path = icon
        self._icon: Any = None
        self._form: Any = None
        self._hinted = False

    def _on_gui(self, fn: Any) -> None:
        from System import Action  # type: ignore[import-not-found]

        if self._form is not None:
            self._form.Invoke(Action(fn))

    def add(self, window: Any) -> None:
        import clr  # type: ignore[import-not-found]

        clr.AddReference("System.Windows.Forms")
        clr.AddReference("System.Drawing")
        self._form = window.native
        for _ in range(50):  # the form exists once the window is created
            if self._form is not None:
                break
            time.sleep(0.1)
            self._form = window.native
        self._on_gui(self._create)

    def _create(self) -> None:
        from System import EventHandler  # type: ignore[import-not-found]
        from System.Drawing import Icon, SystemIcons  # type: ignore[import-not-found]
        from System.Windows.Forms import (  # type: ignore[import-not-found]
            ContextMenuStrip,
            MouseButtons,
            MouseEventHandler,
            NotifyIcon,
            ToolStripMenuItem,
        )

        def later(fn: Any) -> Any:
            # Menu clicks run on the window's thread; do the work elsewhere.
            return lambda *_: threading.Thread(target=fn, daemon=True).start()

        icon = NotifyIcon()
        path = self._icon_path
        icon.Icon = (
            Icon(str(path)) if path and path.exists() else SystemIcons.Application
        )
        icon.Text = "Jarvis"
        menu = ContextMenuStrip()
        open_item = ToolStripMenuItem("Open Jarvis")
        open_item.Click += EventHandler(later(self._app.show))
        pause_item = ToolStripMenuItem("Pause listening")
        pause_item.Click += EventHandler(later(self._app.toggle_pause))
        quit_item = ToolStripMenuItem("Quit Jarvis")
        quit_item.Click += EventHandler(later(self._app.quit))
        menu.Items.Add(open_item)
        menu.Items.Add(pause_item)
        menu.Items.Add(quit_item)

        def opening(*_: Any) -> None:
            pause_item.Text = (
                "Resume listening" if self._app.bridge.paused else "Pause listening"
            )

        from System.ComponentModel import (  # type: ignore[import-not-found]
            CancelEventHandler,
        )

        menu.Opening += CancelEventHandler(opening)
        icon.ContextMenuStrip = menu

        def clicked(_sender: Any, args: Any) -> None:
            if args.Button == MouseButtons.Left:
                threading.Thread(target=self._app.show, daemon=True).start()

        icon.MouseClick += MouseEventHandler(clicked)
        icon.Visible = True
        self._icon = icon

    def hint_once(self) -> None:
        """The first time the window is closed, say where Jarvis went."""
        if self._hinted or self._icon is None:
            return
        self._hinted = True

        def tip() -> None:
            from System.Windows.Forms import ToolTipIcon  # type: ignore[import-not-found]

            self._icon.ShowBalloonTip(
                4000,
                "Jarvis is muted in the tray",
                "Say “Hey Jarvis” and it opens. Everything stays loaded; "
                "click this icon (or the "
                "shortcut) to open it; right-click to quit.",
                ToolTipIcon.Info,
            )

        self._on_gui(tip)

    def remove(self) -> None:
        def gone() -> None:
            if self._icon is not None:
                self._icon.Visible = False
                self._icon.Dispose()

        try:
            self._on_gui(gone)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------


class JarvisApp:
    mutex: Any = None

    def __init__(self, *, background: bool = False) -> None:
        self.bridge = Bridge()
        self.window: Any = None
        self._runner: Optional[threading.Thread] = None
        self.background = background
        self.quitting = False
        self.tray: Optional[Tray] = None
        self.hidden = background
        self._muted_by_hide = False

    # -- the window comes and goes; Jarvis keeps running -------------------------

    def _closing(self) -> bool:
        """Closing the window hides it: the models stay loaded, so opening
        Jarvis again is instant. "Quit Jarvis" in the tray really quits."""
        if self.quitting:
            return True
        threading.Thread(target=self.hide, daemon=True).start()
        return False

    def hide(self) -> None:
        if self.window is not None:
            self.window.hide()
        self._mute()
        if self.tray is not None:
            self.tray.hint_once()

    def _mute(self) -> None:
        """Closed means muted: only "Hey Jarvis" wakes it (and opens the window)."""
        self.hidden = True
        if not self.bridge.paused and not self._muted_by_hide:
            self._muted_by_hide = True
            live.want_pause(True)

    def show(self) -> None:
        if self.window is None:
            return
        self.window.show()
        self.window.restore()
        self.hidden = False
        if self._muted_by_hide:
            # Open again: always-listening again (unless you paused it yourself).
            self._muted_by_hide = False
            if self.bridge.paused:
                live.want_pause(False)
        if sys.platform == "win32":
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, TITLE)
            if hwnd:
                # Above other windows even when Windows won't hand over focus.
                flags = 0x0001 | 0x0002 | 0x0040  # NOSIZE | NOMOVE | SHOWWINDOW
                user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, flags)  # HWND_TOPMOST
                user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, flags)  # HWND_NOTOPMOST
                user32.SetForegroundWindow(hwnd)

    def _on_live(self, kind: str, data: Dict[str, Any]) -> None:
        """A request (or a question Jarvis asks) while closed opens the window."""
        if self.hidden and kind in ("heard", "thinking", "confirm", "reminder"):
            threading.Thread(target=self.show, daemon=True).start()

    def quit(self) -> None:
        self.quitting = True
        logger.warning("Jarvis quit from the tray.")
        if self.tray is not None:
            self.tray.remove()
        if self.window is not None:
            self.window.destroy()
        os._exit(0)

    def toggle_pause(self) -> None:
        live.want_pause(not self.bridge.paused)

    def _watch_show_requests(self) -> None:
        """A second launch (the shortcut again) signals this one to show."""
        if sys.platform != "win32":
            return
        kernel32 = ctypes.windll.kernel32
        event = kernel32.CreateEventW(None, False, False, _SHOW_EVENT)
        while event:
            kernel32.WaitForSingleObject(event, 0xFFFFFFFF)
            try:
                self.show()
            except Exception:
                logger.debug("Show failed", exc_info=True)

    def interrupt_request(self) -> None:
        """Stop a request that's being worked on, like Ctrl-C in the terminal
        (the current step finishes first)."""
        runner = self._runner
        if not self.bridge.busy or runner is None or runner.ident is None:
            return
        ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_ulong(runner.ident), ctypes.py_object(KeyboardInterrupt)
        )
        live.emit("log", text="Stopping…")

    def restart(self) -> None:
        if sys.platform == "win32" and JarvisApp.mutex:
            ctypes.windll.kernel32.CloseHandle(JarvisApp.mutex)
            JarvisApp.mutex = None
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        exe = str(pythonw if pythonw.exists() else sys.executable)
        subprocess.Popen([exe, "-m", "openjarvis.hud"], close_fds=True)
        os._exit(0)

    def _run_assistant(self) -> None:
        """`jarvis listen`, in this window instead of a console."""
        live.emit("log", text="Starting Jarvis…")
        try:
            from openjarvis.hud.settings import engine_problem

            problem = engine_problem()
        except Exception:
            logger.debug("Settings check failed", exc_info=True)
            problem = None
        if problem:
            # First run (or Ollama gone): choose a brain before starting.
            if self.hidden:
                threading.Thread(target=self.show, daemon=True).start()
            live.emit("setup", text=problem)
            return
        try:
            from openjarvis.cli.listen_cmd import listen

            listen.main(args=[], prog_name="jarvis listen", standalone_mode=False)
        except SystemExit as exc:
            if exc.code not in (0, None):
                live.emit(
                    "fatal",
                    text=f"Jarvis couldn't start (exit {exc.code}). See the log.",
                )
                return
        except BaseException:
            text = traceback.format_exc()
            logger.error("Jarvis stopped:\n%s", text)
            live.emit("fatal", text=text[-2500:])
            return
        # "Stop listening" ends the session: quit, not just hide.
        time.sleep(1.0)
        self.quit()

    def _started(self, window: Any) -> None:
        self.bridge.window = window
        try:
            from openjarvis.hud.install import ensure_icon

            icon: Optional[Path] = ensure_icon()
        except Exception:
            logger.debug("No icon", exc_info=True)
            icon = None
        try:
            _style_window(icon)
        except Exception:
            logger.debug("Window styling failed", exc_info=True)
        try:
            self.tray = Tray(self, icon)
            self.tray.add(window)
        except Exception:
            logger.warning("No tray icon", exc_info=True)
            self.tray = None
        threading.Thread(
            target=self._watch_show_requests, name="jarvis-show", daemon=True
        ).start()
        demo = bool(os.environ.get("JARVIS_HUD_DEMO"))
        if not demo:
            _warn_if_listen_running()
        self._runner = threading.Thread(
            target=_demo if demo else self._run_assistant,
            name="jarvis-assistant",
            daemon=True,
        )
        self._runner.start()

    def _closed(self) -> None:
        logger.warning("The Jarvis window was closed.")
        os._exit(0)

    def run(self) -> int:
        import webview

        self.bridge.start()
        live.subscribe(self._on_live)
        if self.background:
            self._mute()  # started at sign-in: in the tray, muted
        self.window = webview.create_window(
            TITLE,
            url=_WEB.as_uri(),
            js_api=Api(self),
            width=1240,
            height=780,
            min_size=(760, 560),
            background_color="#010506",
            text_select=True,
            hidden=self.background,  # at sign-in: straight to the tray
        )
        self.window.events.closing += self._closing
        self.window.events.closed += self._closed
        webview.start(
            self._started,
            self.window,
            gui="edgechromium",
            storage_path=str(app_dir() / "webview"),
        )
        os._exit(0)


def _demo() -> None:
    """``jarvis app --demo``: the window with scripted events, no microphone
    or models (to check how it looks)."""
    import math

    script = [
        ("log", {"text": "Loading wake word model…"}),
        ("log", {"text": "Warming up speech and model…"}),
        ("ready", {"engine": "ollama", "model": "qwen3.5:4b", "always": True,
                   "wake_word": "hey jarvis"}),
        ("listening", {}),
        ("heard", {"text": "Jarvis, open WhatsApp and open Anand's chat"}),
        ("thinking", {"text": "Jarvis, open WhatsApp and open Anand's chat"}),
        ("step", {"kind": "tool_start", "tool": "whatsapp",
                  "args": {"action": "open_chat", "contact": "Anand"}}),
        ("step", {"kind": "tool_end", "tool": "whatsapp", "ok": True}),
        ("reply", {"text": "Opened your WhatsApp chat with **Anand ECE-2**.",
                   "seconds": 4.1, "heard_seconds": 0.8, "ok": True}),
        ("speaking", {"text": "Opened your WhatsApp chat with Anand ECE-2."}),
    ]  # fmt: skip
    for kind, data in script:
        live.emit(kind, **data)
        time.sleep(0.6)
    for i in range(60):
        live.level("voice", 0.3 + 0.3 * abs(math.sin(i / 3)), every=0)
        time.sleep(0.05)
    live.emit("spoken", interrupted=False)
    live.emit("listening")
    while True:
        for i in range(200):
            live.level("mic", 0.15 + 0.25 * abs(math.sin(i / 5)), every=0)
            time.sleep(0.06)
        text = live.take_typed()
        if text:
            live.emit("heard", text=text, typed=True)
            live.emit("reply", text=f"(demo) You typed: {text}", seconds=0.1, ok=True)
            live.emit("listening")


def _warn_if_listen_running() -> None:
    """A `jarvis listen` still open in a terminal would answer too."""
    try:
        import psutil
    except ImportError:
        return
    me = os.getpid()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            words = [w.lower() for w in (proc.info.get("cmdline") or [])]
        except Exception:
            continue
        if proc.info.get("pid") == me or not words:
            continue
        if (
            any(w.endswith(("jarvis", "jarvis.exe")) for w in words)
            and "listen" in words
        ):
            live.emit(
                "notice",
                text="`jarvis listen` is also running in a terminal; close it, or "
                "both will answer.",
            )
            return


def _setup_output() -> None:
    log = open(log_path(), "a", encoding="utf-8", errors="replace", buffering=1)
    log.write(
        f"\n===== Jarvis app started {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n"
    )
    import faulthandler

    faulthandler.enable(file=log)  # a native crash still leaves a trace
    sink = _Sink(log)
    sys.stdout = sink  # type: ignore[assignment]
    sys.stderr = sink  # type: ignore[assignment]
    if sys.stdin is None:
        # Windowless: code that asks "is this a terminal?" (key-to-stop while
        # speaking, the runtime picker) gets "no" instead of crashing.
        sys.stdin = io.StringIO()  # type: ignore[assignment]
    logging.basicConfig(
        stream=sink,
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    background = "--background" in args
    if _already_running():
        if not background:
            _focus_existing()
        return 0
    _setup_output()
    if sys.platform == "win32":
        try:  # its own taskbar button and icon, not Python's
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "OpenJarvis.JARVIS"
            )
        except Exception:
            pass
    _no_console_windows()
    try:
        return JarvisApp(background=background).run()
    except Exception:
        text = traceback.format_exc()
        logger.error("The Jarvis window failed:\n%s", text)
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(
                None,
                "The Jarvis window couldn't open.\n\n"
                + text[-1200:]
                + f"\n\nLog: {log_path()}",
                TITLE,
                0x10,
            )
        return 1


__all__ = ["Api", "Bridge", "JarvisApp", "TITLE", "main"]
