"""The JARVIS window: live events, typed commands, the bridge and the icon."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from openjarvis.core import live


class _Console:
    def print(self, *args, **kwargs) -> None:
        pass


@pytest.fixture
def events():
    seen: List[Tuple[str, Dict[str, Any]]] = []
    unsubscribe = live.subscribe(lambda kind, data: seen.append((kind, data)))
    while live.take_typed() is not None:
        pass
    live.take_pause_request()
    live._LAST_LEVEL[0] = 0.0  # a level from an earlier test must not throttle
    yield seen
    unsubscribe()
    while live.take_typed() is not None:
        pass
    live.take_pause_request()
    live.clear_stop_speaking()


def test_events_reach_the_window_and_steps_keep_their_kind(events) -> None:
    import rich.console

    from openjarvis.cli._assistant import progress_printer

    show = progress_printer(rich.console.Console(file=io.StringIO()))
    show("tool_start", {"tool": "whatsapp", "args": {"action": "open_chat"}})
    show("tool_end", {"tool": "whatsapp", "ok": False, "text": "not found"})
    start = {"kind": "tool_start", "tool": "whatsapp", "args": {"action": "open_chat"}}
    end = {"kind": "tool_end", "tool": "whatsapp", "ok": False, "text": "not found"}
    assert events == [("step", start), ("step", end)]


def test_nothing_happens_without_a_window() -> None:
    assert not live.active()
    live.emit("step", kind="tool_start")  # no error, no work
    live.level("mic", 0.5)


def test_typed_commands_and_pause_end_a_wait(events) -> None:
    assert not live.needs_attention()
    live.type_command("  open   notepad ")
    assert live.needs_attention() and live.take_typed() == "open notepad"
    live.want_pause(True)
    assert live.needs_attention() and live.take_pause_request() is True
    assert live.take_pause_request() is None and not live.needs_attention()


def test_levels_are_clamped(events) -> None:
    live.level("mic", 7.0, every=0)
    live.level("voice", -1.0, every=0)
    assert events == [("mic", {"level": 1.0}), ("voice", {"level": 0.0})]


def test_a_recording_stops_waiting_when_a_command_is_typed(events) -> None:
    from openjarvis.speech.voice_io import record_from_stream

    class Room:
        reads = 0

        def read(self, n: int):
            Room.reads += 1
            if Room.reads == 3:
                live.type_command("open notepad")
            return b"\x01\x00" * n, False

    audio = record_from_stream(
        Room(), silence_threshold=500, startup_silence_seconds=10
    )
    assert audio == b"" and Room.reads == 3  # not ten seconds of silence
    assert events and all(kind == "mic" for kind, _ in events)


def test_always_listening_hands_a_typed_command_back(events, monkeypatch) -> None:
    from openjarvis.cli import _assistant

    recorded: List[int] = []
    monkeypatch.setattr(_assistant, "record_voice", lambda *a, **k: recorded.append(1))
    monkeypatch.setattr(_assistant.time, "sleep", lambda s: None)
    announcer = type("A", (), {"speak_pending": lambda self, c, s: None})()
    listener = _assistant.AlwaysListener()
    live.type_command("open notepad")
    assert _assistant.listen_always(_Console(), None, None, announcer, listener) is None
    assert recorded == []  # the chat loop picks the typed command up next
    assert live.take_typed() == "open notepad"


def test_the_pause_button_switches_to_the_wake_word(events, monkeypatch) -> None:
    from openjarvis.cli import _assistant

    waited: List[int] = []
    monkeypatch.setattr(
        _assistant, "wait_for_wake_command", lambda *a, **k: waited.append(1)
    )
    announcer = type("A", (), {"speak_pending": lambda self, c, s: None})()
    listener = _assistant.AlwaysListener()
    live.want_pause(True)
    _assistant.listen_always(_Console(), None, None, announcer, listener)
    assert listener.paused and waited == [1]
    assert ("paused", {"wake_word": "hey jarvis"}) in events
    live.want_pause(False)
    monkeypatch.setattr(_assistant, "record_voice", lambda *a, **k: None)
    monkeypatch.setattr(_assistant.time, "sleep", lambda s: None)
    live.type_command("x")  # ends the listening wait at once
    _assistant.listen_always(_Console(), None, None, announcer, listener)
    assert not listener.paused


# ---------------------------------------------------------------------------
# The window side
# ---------------------------------------------------------------------------


def test_the_bridge_batches_and_tracks_a_running_request() -> None:
    from openjarvis.hud.app import Bridge

    bridge = Bridge()
    bridge.on_event("thinking", {"text": "open notepad"})
    assert bridge.busy
    bridge.on_event("mic", {"level": 0.2})
    bridge.on_event("mic", {"level": 0.6})  # only the latest level is sent
    bridge.on_event("reply", {"text": "Opened Notepad.", "path": Path("x")})
    assert not bridge.busy
    assert [k for k, _ in bridge.history] == ["thinking", "reply"]
    assert bridge._levels == {"mic": {"level": 0.6}}
    assert bridge.history[-1][1]["path"] == "x"  # made JSON-safe


def test_console_output_goes_to_the_log_and_the_boot_lines(events) -> None:
    from openjarvis.hud.app import _Sink

    file = io.StringIO()
    sink = _Sink(file)
    sink.write("\x1b[2mLoading wake word model…\x1b[0m\nWarming")
    sink.write(" up speech and model…\n")
    assert "Loading wake word model" in file.getvalue()
    assert events == [
        ("log", {"text": "Loading wake word model…"}),
        ("log", {"text": "Warming up speech and model…"}),
    ]
    assert not sink.isatty()


def test_the_icon_is_a_ring_on_a_dark_tile() -> None:
    pytest.importorskip("PIL")
    from openjarvis.hud.install import draw_icon

    image = draw_icon(64)
    assert image.size == (64, 64)
    assert image.getpixel((1, 1))[3] == 0  # rounded corner: transparent
    red, green, blue, _ = image.getpixel((32, 10))  # on the ring
    assert green > 100 and blue > 80 and red < green / 2  # teal


def test_shortcuts_run_the_windowless_launcher(tmp_path: Path, monkeypatch) -> None:
    import sys

    from openjarvis.hud import install

    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    monkeypatch.setattr(sys, "executable", str(scripts / "python.exe"))
    (scripts / "pythonw.exe").write_bytes(b"")
    assert install.launcher() == [str(scripts / "pythonw.exe"), "-m", "openjarvis.hud"]
    (scripts / "jarvis-app.exe").write_bytes(b"")
    assert install.launcher() == [str(scripts / "jarvis-app.exe")]


def test_the_page_has_what_the_bridge_sends() -> None:
    from openjarvis.hud import app

    page = app._WEB.read_text(encoding="utf-8")
    for kind in (
        "ready",
        "heard",
        "thinking",
        "step",
        "reply",
        "speaking",
        "spoken",
        "paused",
        "mic",
        "voice",
        "confirm",
        "fatal",
        "notice",
        "log",
    ):
        assert f"{kind}(" in page or f"{kind}:" in page, kind
    assert "window.__jarvis" in page


def test_jarvis_waits_for_ollama_instead_of_falling_back(monkeypatch) -> None:
    from types import SimpleNamespace

    from openjarvis.cli import _assistant
    from openjarvis.engine import _discovery

    answers = iter([False, False, False, True])  # Ollama comes up on try 4
    engine = SimpleNamespace(health=lambda: next(answers))
    monkeypatch.setattr(_discovery, "_make_engine", lambda key, config: engine)
    started: List[int] = []
    monkeypatch.setattr(_assistant, "_start_ollama", lambda: started.append(1) or True)
    monkeypatch.setattr(_assistant.time, "sleep", lambda s: None)
    config = SimpleNamespace(engine=SimpleNamespace(default="ollama"))
    assert _assistant.ensure_local_engine(config, _Console()) is True
    assert started == [1]
    # Not Ollama: nothing to start.
    other = SimpleNamespace(engine=SimpleNamespace(default="vllm"))
    assert _assistant.ensure_local_engine(other) is True and started == [1]


def test_the_voice_spectrum_follows_the_sound(events) -> None:
    np = pytest.importorskip("numpy")

    rate = 24000
    t = np.arange(1200) / rate
    voice = sum(0.08 / k * np.sin(2 * np.pi * 180 * k * t) for k in range(1, 15))
    loud = live.spectrum(voice, rate)
    quiet = live.spectrum(voice * 0.05, rate)
    silent = live.spectrum(np.zeros(1200), rate)
    assert len(loud) == 12 and max(loud) > 0.5
    assert sum(quiet) < sum(loud) and max(silent) == 0.0
    # int16 microphone samples work too.
    mic = live.spectrum(
        memoryview((voice * 8000).astype("int16").tobytes()).cast("h"), 16000
    )
    assert max(mic) > 0.3
    live.level("voice", 0.4, every=0, bands=loud)
    kind, data = events[-1]
    assert kind == "voice" and len(data["bands"]) == 12
    assert all(isinstance(b, float) and 0.0 <= b <= 1.0 for b in data["bands"])


def test_the_ring_loop_survives_a_window_with_no_size() -> None:
    from openjarvis.hud import app

    page = app._WEB.read_text(encoding="utf-8")
    loop = page[page.index("function frame(ms)") : page.index("function draw(ms)")]
    # The next frame is booked before drawing, and drawing errors are caught.
    assert loop.index("requestAnimationFrame(frame)") < loop.index("draw(ms)")
    assert "catch" in loop


def test_the_panel_choice_is_remembered(tmp_path: Path, monkeypatch) -> None:
    from openjarvis.hud import app

    monkeypatch.setattr(app, "app_dir", lambda: tmp_path)
    api = app.Api(app.JarvisApp())
    assert api.prefs() == {}
    api.set_pref("panel_open", True)
    api.set_pref("anything_else", "x")  # only known keys are kept
    assert api.prefs() == {"panel_open": True}
    page = app._WEB.read_text(encoding="utf-8")
    assert 'id="toggle-panel"' in page and 'id="close-panel"' in page
    assert "Always listening" not in page and 'id="chip-mode"' not in page


def test_closing_the_window_keeps_jarvis_running(events, monkeypatch) -> None:
    from types import SimpleNamespace

    from openjarvis.hud import app

    jarvis = app.JarvisApp()
    hidden: List[int] = []
    jarvis.window = SimpleNamespace(hide=lambda: hidden.append(1))
    monkeypatch.setattr(
        app.threading, "Thread", lambda target, daemon: SimpleNamespace(start=target)
    )
    assert jarvis._closing() is False  # cancel the close...
    assert hidden == [1]  # ...and hide instead
    jarvis.quitting = True
    assert jarvis._closing() is True  # "Quit Jarvis" really closes


def test_closed_means_muted_and_a_request_opens_it(events, monkeypatch) -> None:
    from types import SimpleNamespace

    from openjarvis.hud import app

    jarvis = app.JarvisApp()
    shown: List[int] = []
    jarvis.window = SimpleNamespace(
        hide=lambda: None, show=lambda: shown.append(1), restore=lambda: None
    )
    monkeypatch.setattr(
        app.threading, "Thread", lambda target, daemon: SimpleNamespace(start=target)
    )
    monkeypatch.setattr(app.sys, "platform", "test")
    jarvis.hide()
    assert jarvis.hidden and live.take_pause_request() is True  # wake word only
    jarvis.bridge.on_event("paused", {})
    # "Hey Jarvis, ..." while closed: the window opens by itself.
    jarvis._on_live("heard", {"text": "open notepad"})
    assert shown == [1] and not jarvis.hidden
    assert live.take_pause_request() is False  # always-listening again
    # Paused by you before closing: opening doesn't unpause it.
    jarvis.bridge.on_event("paused", {})
    jarvis.hide()
    assert live.take_pause_request() is None
    jarvis.show()
    assert live.take_pause_request() is None
    jarvis._on_live("heard", {"text": "x"})  # visible: nothing to do
    assert shown == [1, 1]


def test_a_wake_word_request_is_reported(events, monkeypatch) -> None:
    from openjarvis.cli import _assistant

    monkeypatch.setattr(
        _assistant, "wait_for_wake_command", lambda *a, **k: "open notepad"
    )
    announcer = type("A", (), {"speak_pending": lambda self, c, s: None})()
    listener = _assistant.AlwaysListener()
    listener.paused = True
    assert _assistant.listen_always(_Console(), None, None, announcer, listener) == (
        "open notepad"
    )
    assert ("heard", {"text": "open notepad"}) in events and not listener.paused


def test_speech_timing_is_not_replayed_and_pause_is_tracked() -> None:
    from openjarvis.hud.app import Bridge

    bridge = Bridge()
    bridge.on_event("say", {"text": "Hello.", "seconds": 0.5})
    bridge.on_event("paused", {"wake_word": "hey jarvis"})
    assert bridge.paused and [k for k, _ in bridge.history] == ["paused"]
    bridge.on_event("listening", {})
    assert not bridge.paused


def test_sign_in_start_goes_straight_to_the_tray(monkeypatch) -> None:
    from openjarvis.hud import app, install

    made: List[Any] = []
    monkeypatch.setattr(app, "_already_running", lambda: True)
    focused: List[int] = []
    monkeypatch.setattr(app, "_focus_existing", lambda: focused.append(1))
    assert app.main(["--background"]) == 0 and focused == []  # no window popping up
    assert app.main([]) == 0 and focused == [1]  # the shortcut shows the running one
    monkeypatch.setattr(install, "_folders", lambda: {"startup": Path("S")})
    monkeypatch.setattr(install, "ensure_icon", lambda: Path("i.ico"))
    monkeypatch.setattr(install, "launcher", lambda: ["jarvis-app.exe"])
    monkeypatch.setattr(
        install, "_write_shortcut", lambda p, cmd, icon: made.append(cmd)
    )
    monkeypatch.setattr(install.sys, "platform", "win32")
    install.set_autostart(True)
    assert made == [["jarvis-app.exe", "--background"]]


def test_the_page_streams_what_jarvis_says() -> None:
    from openjarvis.hud import app

    page = app._WEB.read_text(encoding="utf-8")
    assert "say(d)" in page and "streamTick" in page and "finishStream" in page


class _FakeDetector:
    """Hears the name after ``delay`` seconds, then hands over its stream."""

    label = "hey jarvis"
    threshold = 0.5

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay

    def wait(self, *, should_abort, threshold, then):
        import time

        time.sleep(self.delay)
        if should_abort():
            return False
        return then(object())


def _session():
    from types import SimpleNamespace

    return SimpleNamespace(record_kwargs=lambda **kw: kw)


def test_saying_jarvis_stops_the_running_request(events, monkeypatch) -> None:
    import time

    from openjarvis.cli import _assistant
    from openjarvis.speech import voice_io

    monkeypatch.setattr(
        voice_io, "record_from_stream", lambda stream, **kw: b"RIFFaudio"
    )
    interrupter = _assistant.Interrupter(_FakeDetector(), _session())
    interrupter.working(True)
    interrupter.start()
    stopped = False
    try:
        for _ in range(300):  # the agent "working"
            time.sleep(0.01)
    except KeyboardInterrupt:
        stopped = True
    interrupter.close()
    assert stopped and interrupter.heard.is_set()
    assert interrupter.audio == b"RIFFaudio"  # what came after the name
    assert ("woken", {}) in events


def test_while_speaking_the_name_only_stops_the_voice(events, monkeypatch) -> None:
    import time

    from openjarvis.cli import _assistant
    from openjarvis.speech import voice_io

    monkeypatch.setattr(voice_io, "record_from_stream", lambda stream, **kw: b"")
    interrupter = _assistant.Interrupter(_FakeDetector(), _session()).start()
    for _ in range(100):  # not "working": no KeyboardInterrupt
        if interrupter.heard.is_set():
            break
        time.sleep(0.01)
    interrupter.close()
    assert interrupter.heard.is_set()  # speak()'s should_stop sees this


@pytest.mark.parametrize(
    "heard, expected",
    [
        ("Jarvis, open notepad instead.", "open notepad instead."),
        ("Hey Jarvis, play some music", "play some music"),
        ("stop", None),  # "Jarvis, stop": nothing more
        ("Never mind.", None),
    ],
)
def test_what_was_said_after_the_name(monkeypatch, heard: str, expected) -> None:
    from openjarvis.cli import _assistant

    interrupter = _assistant.Interrupter(_FakeDetector(), _session())
    interrupter.heard.set()
    interrupter.audio = b"RIFF"
    monkeypatch.setattr(_assistant, "record_voice", lambda *a, **k: heard)
    assert interrupter.request(_Console()) == expected
    interrupter.audio = b""  # just "Jarvis": listen for the request next
    assert interrupter.request(_Console()) == ""


def test_the_page_types_letters_and_listens_on_its_name() -> None:
    from openjarvis.hud import app

    page = app._WEB.read_text(encoding="utf-8")
    assert 'ch.className = "ch pending"' in page and "@keyframes ch-in" in page
    assert "woken()" in page
