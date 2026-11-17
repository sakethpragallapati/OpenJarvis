"""Desktop-assistant plumbing for ``jarvis chat --voice`` / ``jarvis listen``.

Kept out of ``chat_cmd`` so the REPL stays readable: laptop power policy,
model warm-up, reminder delivery, and the wake-word input loop.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from openjarvis.cli._voice_chat import (
    VOICE_EXIT,
    VoiceSession,
    _terminal_safe_text,
    record_voice,
    speak,
)

logger = logging.getLogger(__name__)

# Voice sessions idle between commands; without a longer keep-alive Ollama
# unloads the model after 5 minutes and the next command waits for a reload.
VOICE_KEEP_ALIVE = "30m"
# Default toolset for `jarvis listen` when none is configured.
LISTEN_DEFAULT_TOOLS = (
    "pc_control,lock_screen,clipboard,set_reminder,list_reminders,"
    "cancel_reminder,calculator,web_search,get_weather"
)
# The agentic desktop agent (LangGraph + FastMCP, `agent` extra); without
# those packages `jarvis listen` falls back to the orchestrator.
LISTEN_DEFAULT_AGENT = "desktop"
LISTEN_FALLBACK_AGENT = "orchestrator"
# OpenJarvis tools bridged into the desktop agent next to its own PC tools.
LISTEN_DESKTOP_TOOLS = "web_search,get_weather,calculator"
_EXIT_PHRASES = {
    "goodbye",
    "goodbye jarvis",
    "stop listening",
    "exit",
    "quit",
    "shut down",
}


def apply_power_policy(
    console: Any, config: Any, model: str, *, explicit_model: bool
) -> str:
    """Adapt the session to battery and GPU-memory state; returns the model.

    On battery (``power.battery_saver``): switch to ``power.battery_model``
    unless the user chose a model with ``-m``, and run speech-to-text on CPU
    so the GPU can stay in its low-power state. With little free VRAM,
    speech-to-text also moves to CPU so it can't crowd out the LLM.
    """
    from openjarvis.pc.power import battery_status, free_vram_mb

    power = getattr(config, "power", None)
    if power is None:
        return model

    status = battery_status() if power.battery_saver else None
    if status is not None and status.plugged_in is False:
        notes = []
        if power.battery_model and not explicit_model and power.battery_model != model:
            model = power.battery_model
            notes.append(f"model {model}")
        if config.speech.device != "cpu":
            notes.append(_speech_to_cpu(config))
        if notes:
            console.print(
                f"[dim yellow]On battery ({status.describe()}) — "
                f"saving power: {', '.join(notes)}.[/dim yellow]"
            )

    free = free_vram_mb()
    if free is not None and free < power.low_vram_warning_mb:
        console.print(
            f"[yellow]Only {free} MB of GPU memory is free. Close other GPU "
            "apps or pick a smaller model if replies are slow or fail.[/yellow]"
        )
        if config.speech.device != "cpu":
            note = _speech_to_cpu(config)
            console.print(f"[dim]Speech-to-text will run on CPU: {note}.[/dim]")
    return model


# Whisper sizes that still transcribe a spoken command in about a second on a
# laptop CPU; small and up take 2-3x longer there.
_CPU_SPEECH_MODELS = ("tiny", "tiny.en", "base", "base.en")


def _speech_to_cpu(config: Any) -> str:
    """Move speech-to-text to the CPU (int8), with a model that stays quick."""
    speech = config.speech
    speech.device = "cpu"
    speech.compute_type = "int8"
    model = str(getattr(speech, "model", "") or "")
    if not model or model in _CPU_SPEECH_MODELS or "/" in model or "\\" in model:
        return "speech on CPU"
    english = model.endswith(".en") or str(getattr(speech, "language", "")).startswith(
        "en"
    )
    speech.model = "base.en" if english else "base"
    return f"speech on CPU with Whisper {speech.model}"


VOICE_STYLE = (
    "You are speaking out loud through the user's speakers. Answer in one or "
    "two short spoken sentences. Never use Markdown, lists, code blocks, "
    "emoji or URLs. When asked to do something on the computer, call the "
    "matching tool, then confirm in a few words."
)


def voice_system_template(template: str) -> str:
    """Prefix the agent template with the spoken-reply style."""
    return f"{VOICE_STYLE}\n\n{template}" if template else VOICE_STYLE


def _prime_prompt_cache(agent: Any) -> None:
    """Send the agent's exact system prompt + tool list once, generating 1 token.

    Ollama reuses the KV cache for a matching prompt prefix, so the first
    real command skips evaluating ~2k prompt tokens (9.6 s -> ~2 s on an
    RTX 5050 laptop). No tool can run: nothing is executed from the reply.
    """
    warm = getattr(agent, "warm_up", None)
    if callable(warm):  # the desktop agent primes its own prompt + indexes
        warm()
        return
    build = getattr(agent, "_build_messages", None)
    engine = getattr(agent, "_engine", None)
    if build is None or engine is None:
        return
    from openjarvis.agents._stubs import _ALLOWED_ENGINE_OPTION_KEYS

    messages = build(
        "Hello", None, system_prompt=getattr(agent, "_system_prompt", None)
    )
    # Straight to the engine: BaseAgent._generate always passes its own
    # max_tokens, so a max_tokens=1 override there raised a TypeError (and
    # the warm-up silently did nothing). Same num_ctx as real calls, or
    # Ollama would reload the model.
    kwargs: dict[str, Any] = {
        k: v
        for k, v in getattr(agent, "_engine_options", {}).items()
        if k in _ALLOWED_ENGINE_OPTION_KEYS
    }
    executor = getattr(agent, "_executor", None)
    if executor is not None and getattr(agent, "_tools", None):
        kwargs["tools"] = executor.get_openai_tools()
    engine.generate(
        messages,
        model=getattr(agent, "_model", ""),
        temperature=getattr(agent, "_temperature", 0.7),
        max_tokens=1,
        **kwargs,
    )


def warm_up(console: Any, session: VoiceSession, agent: Any) -> None:
    """Load speech models and prime the model's prompt cache in parallel."""
    import warnings

    # torch/Kokoro emit deprecation chatter on load that means nothing to
    # someone talking to their assistant.
    for category in (UserWarning, FutureWarning):
        warnings.filterwarnings("ignore", category=category, module=r"torch\b.*")

    console.print("[dim]Warming up speech and model…[/dim]")
    problems: list[str] = []

    def speech() -> None:
        problems.extend(session.warm_up())

    def model() -> None:
        if agent is None:
            return
        try:
            _prime_prompt_cache(agent)
        except Exception as exc:
            logger.debug("Prompt-cache warm-up failed: %s", exc)

    workers = [
        threading.Thread(target=target, name=f"openjarvis-warmup-{target.__name__}")
        for target in (speech, model)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    for problem in problems:
        console.print(f"[yellow]Voice: {_terminal_safe_text(problem)}[/yellow]")


_YES = re.compile(
    r"^\W*(?:yes|yeah|yep|yup|sure|ok|okay|do it|go ahead|confirm(?:ed)?|"
    r"please do|affirmative|correct|absolutely|of course)\b",
    re.IGNORECASE,
)


def is_affirmative(text: str) -> bool:
    return bool(_YES.match(text or ""))


def make_voice_confirm(console: Any, session: VoiceSession) -> Any:
    """Ask a yes/no question aloud and listen for the answer."""

    def confirm(question: str) -> bool:
        from openjarvis.core import live

        live.emit("confirm", question=question)
        console.print(
            f"[yellow]Confirm:[/yellow] {_terminal_safe_text(question)} "
            "[dim](say yes or no)[/dim]"
        )
        speak(f"{question} Say yes or no.", console, session)
        answer = record_voice(console, session, follow_up_seconds=6.0)
        if not isinstance(answer, str):
            answer = live.take_typed() or answer  # Yes/No in the window
        if not isinstance(answer, str):
            console.print("[dim]No answer — not doing it.[/dim]")
            return False
        console.print(f"[dim]You: {_terminal_safe_text(answer)}[/dim]")
        return is_affirmative(answer)

    return confirm


def progress_printer(console: Any) -> Any:
    """Show the agent's tool calls as they happen (dim, one line each)."""

    def show(kind: str, data: dict) -> None:
        from openjarvis.core import live

        live.emit("step", kind=kind, **{k: v for k, v in data.items() if k != "kind"})
        if kind == "tool_start":
            args = data.get("args") or {}
            shown = ", ".join(
                f"{k}={v!r}" for k, v in args.items() if v not in ("", None)
            )
            if len(shown) > 90:
                shown = shown[:87] + "..."
            console.print(
                f"[dim]  → {data.get('tool')}({_terminal_safe_text(shown)})[/dim]"
            )
        elif kind == "tool_end" and not data.get("ok"):
            text = _terminal_safe_text(str(data.get("text", "")))
            console.print(f"[dim red]    ✗ {text}[/dim red]")
        elif kind == "declined":
            console.print("[dim]    (not allowed)[/dim]")
        elif kind == "plan" and data.get("steps"):
            for step in data["steps"]:
                console.print(
                    f"[dim cyan]  plan {_terminal_safe_text(step)}[/dim cyan]"
                )

    return show


def _start_ollama() -> bool:
    """Start Ollama the way its Startup shortcut does (the tray app), else
    ``ollama serve``. False if it isn't installed."""
    import shutil
    import subprocess

    try:
        import psutil

        for proc in psutil.process_iter(["name"]):
            if (proc.info.get("name") or "").lower().startswith("ollama"):
                return True  # already starting: just wait for it
    except Exception:
        pass
    detached = 0x00000008 | 0x00000200 if sys.platform == "win32" else 0
    tray = (
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Programs"
        / "Ollama"
        / "ollama app.exe"
    )
    if sys.platform == "win32" and tray.exists():
        subprocess.Popen([str(tray)], creationflags=detached, close_fds=True)
        return True
    exe = shutil.which("ollama")
    if exe:
        subprocess.Popen(
            [exe, "serve"],
            creationflags=detached,
            close_fds=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    return False


def ensure_local_engine(config: Any, console: Any = None, wait: float = 45.0) -> bool:
    """Make sure Ollama answers before the engine is chosen.

    Opened from the Jarvis shortcut (or at sign-in) Jarvis can start before
    Ollama does; the engine check then found it down and quietly fell back to
    another engine. Now Ollama is started if needed and waited for.
    """
    if (getattr(config.engine, "default", "") or "") != "ollama":
        return True
    try:
        from openjarvis.engine._discovery import _make_engine

        engine = _make_engine("ollama", config)
    except Exception as exc:
        logger.debug("Ollama engine unavailable: %s", exc)
        return False
    if engine.health():
        return True
    from openjarvis.core import live

    if not _start_ollama():
        return False
    if console is not None:
        console.print("[dim]Starting Ollama…[/dim]")
    live.emit("log", text="Starting Ollama…")
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        time.sleep(1.0)
        if engine.health():
            return True
    return False


# "Jarvis, stop" just stops; it isn't a new request.
_JUST_STOP = re.compile(
    r"^\W*(?:stop|stop it|stop talking|cancel|never ?mind|forget it|leave it|"
    r"shut up|quiet|be quiet|hold on|wait|enough|that's enough|ok(?:ay)?|"
    r"thanks|thank you)\W*$",
    re.IGNORECASE,
)


def is_just_stop(text: str) -> bool:
    return bool(_JUST_STOP.match(text or "")) or not (text or "").strip()


class Interrupter:
    """Calling Jarvis by name interrupts what it's doing.

    The wake-word detector runs while Jarvis works on a request and while it
    speaks. "Jarvis" stops the speech at once (or the request, after its
    current step, the way Ctrl-C does in the terminal); anything said right
    after the name ("Jarvis, open Notepad instead") is recorded on the same
    microphone stream, so none of it is lost, and becomes the next request.
    """

    def __init__(self, detector: Any, session: VoiceSession) -> None:
        self._detector = detector
        self._session = session
        # A little above the usual threshold: Jarvis's own voice is in the mic.
        self._threshold = min(0.9, float(detector.threshold) + 0.15)
        self._loop = threading.get_ident()
        self._lock = threading.Lock()
        self._working = False
        self._stop = threading.Event()
        self._done = threading.Event()
        self.heard = threading.Event()
        self.audio = b""
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "Interrupter":
        self._thread = threading.Thread(
            target=self._run, name="openjarvis-interrupt", daemon=True
        )
        self._thread.start()
        return self

    def working(self, on: bool) -> None:
        """Mark the stretch where a KeyboardInterrupt may stop the request."""
        with self._lock:
            self._working = on

    def _run(self) -> None:
        try:
            self._detector.wait(
                should_abort=self._stop.is_set,
                threshold=self._threshold,
                then=self._after_name,
            )
        except Exception as exc:  # mic busy / device lost: no interruptions
            logger.debug("Interrupt listener stopped: %s", exc)
        finally:
            self._done.set()

    def _after_name(self, stream: Any) -> bytes:
        from openjarvis.core import live
        from openjarvis.speech.voice_io import record_from_stream

        self.heard.set()
        live.emit("woken")
        with self._lock:
            if self._working:
                import ctypes

                ctypes.pythonapi.PyThreadState_SetAsyncExc(
                    ctypes.c_ulong(self._loop), ctypes.py_object(KeyboardInterrupt)
                )
        kwargs = self._session.record_kwargs(
            startup_silence_seconds=3.0, require_speech=True
        )
        try:
            self.audio = record_from_stream(stream, **kwargs) or b""
        except Exception as exc:
            logger.debug("Recording after the name failed: %s", exc)
        return self.audio

    def close(self) -> None:
        """Stop listening; after "Jarvis", wait for the request to be said."""
        self.working(False)
        if self.heard.is_set():
            self._done.wait(timeout=35.0)
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def request(self, console: Any) -> Optional[str]:
        """What was said after the name: the next request, "" for just the
        name (listen for it next), or None when it was "stop"."""
        if not self.heard.is_set():
            return None
        if not self.audio:
            return ""
        text = record_voice(console, self._session, audio=self.audio)
        if not isinstance(text, str):
            return ""
        text = strip_wake_phrase(text, getattr(self._detector, "label", "hey jarvis"))
        from openjarvis.desktop.fastpath import normalize

        if is_just_stop(text) or is_just_stop(normalize(text)):
            return None
        return text


def voice_keep_alive(config: Any) -> Optional[str]:
    """Keep-alive to request in voice mode, or None if the user set one."""
    if config.engine.ollama.keep_alive or os.environ.get("JARVIS_OLLAMA_KEEP_ALIVE"):
        return None
    return VOICE_KEEP_ALIVE


def preload_model(engine: Any, model: str, keep_alive: Optional[str]) -> None:
    """Warm the model on a background thread so the first command is fast."""
    preload = getattr(engine, "preload", None)
    if preload is None:
        return

    def run() -> None:
        try:
            preload(model, keep_alive=keep_alive)
        except Exception as exc:
            logger.debug("Model preload failed: %s", exc)

    threading.Thread(target=run, name="openjarvis-preload", daemon=True).start()


class ReminderAnnouncer:
    """Deliver due reminders: notification + console line (+ speech if voice)."""

    def __init__(self, console: Any, *, speak_aloud: bool) -> None:
        self._console = console
        self._speak_aloud = speak_aloud
        self.pending: queue.Queue[str] = queue.Queue()
        self._watcher: Any = None

    def _deliver(self, reminder: Any) -> None:
        from openjarvis.core import live
        from openjarvis.pc.notify import notify

        notify("Reminder", reminder.message)
        live.emit("reminder", text=reminder.message)
        self._console.print(
            f"\n[bold magenta]⏰ Reminder:[/bold magenta] "
            f"{_terminal_safe_text(reminder.message)}"
        )
        if self._speak_aloud:
            self.pending.put(f"Reminder: {reminder.message}")

    def start(self) -> "ReminderAnnouncer":
        from openjarvis.pc.reminders import ReminderWatcher

        try:
            self._watcher = ReminderWatcher(self._deliver).start()
        except Exception as exc:
            logger.debug("Reminder watcher unavailable: %s", exc)
        return self

    def stop(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()

    def speak_pending(self, console: Any, session: Optional[VoiceSession]) -> None:
        while True:
            try:
                text = self.pending.get_nowait()
            except queue.Empty:
                return
            speak(text, console, session)


def is_exit_phrase(text: str) -> bool:
    cleaned = re.sub(r"[^\w\s]", "", text.lower()).strip()
    return cleaned in _EXIT_PHRASES


_WAKE_LEAD_INS = r"(?:hey|hi|hello|ok|okay|yo)"


def strip_wake_phrase(text: str, label: str) -> str:
    """Drop a leading wake phrase ("Hey, Jarvis.") from a transcript.

    The recorder starts on the wake word, so Whisper often hears it again;
    sending "Hey, Jarvis." to the model as the request wastes a whole turn.
    """
    words = label.split()
    if not words:
        return text.strip()
    name = re.escape(words[-1])
    pattern = rf"^\W*(?:{_WAKE_LEAD_INS}\W+)?{name}\b\W*"
    return re.sub(pattern, "", text, count=1, flags=re.IGNORECASE).strip()


def _chime() -> None:
    from openjarvis.speech.voice_io import play_chime

    try:
        play_chime(blocking=False)
    except Exception as exc:  # a missing output device must not block input
        logger.debug("Chime failed: %s", exc)


def wait_for_wake_command(
    console: Any,
    session: VoiceSession,
    detector: Any,
    announcer: ReminderAnnouncer,
    *,
    skip_wake: bool = False,
    follow_up_seconds: float = 0.0,
) -> Optional[str] | object:
    """Get the next spoken request: a follow-up, or wake word + command.

    ``skip_wake`` (after a barge-in) records straight away. With
    ``follow_up_seconds`` it first listens that long for a follow-up without
    the wake word, then falls back to waiting for the wake word. A command
    said in the same breath as the wake word is recorded on the same mic
    stream, and a bare "Hey Jarvis" just keeps listening for the request.

    Returns the transcript, None if nothing was heard, or ``VOICE_EXIT`` on
    Ctrl-C / an exit phrase. Due reminders are spoken while waiting.
    """
    from openjarvis.speech.voice_io import record_from_stream

    result: Optional[str] | object = None
    if skip_wake:
        _chime()
        result = record_voice(console, session)
    elif follow_up_seconds > 0:
        console.print(
            "[dim cyan]Listening for a follow-up… (or say "
            f"'{detector.label}' later)[/dim cyan]"
        )
        result = record_voice(console, session, follow_up_seconds=follow_up_seconds)

    if result is None and not skip_wake:
        console.print(
            f"[dim cyan]Waiting for '{detector.label}'… (Ctrl-C to quit)[/dim cyan]"
        )

        def record_command(stream: Any) -> bytes:
            _chime()
            return record_from_stream(stream, **session.record_kwargs())

        from openjarvis.core import live

        while True:
            announcer.speak_pending(console, session)
            if live.needs_attention():
                return None  # a typed command or resume from the window
            try:
                audio = detector.wait(
                    should_abort=lambda: (
                        not announcer.pending.empty() or live.needs_attention()
                    ),
                    then=record_command,
                )
            except KeyboardInterrupt:
                return VOICE_EXIT
            except Exception as exc:
                console.print(f"[red]Mic error: {_terminal_safe_text(exc)}[/red]")
                return VOICE_EXIT
            if audio is not False:
                break
        result = record_voice(console, session, audio=audio)

    if isinstance(result, str):
        request = strip_wake_phrase(result, detector.label)
        if not request:
            # Only the wake word was heard: the request is coming next.
            _chime()
            result = record_voice(console, session)
            request = (
                strip_wake_phrase(result, detector.label)
                if isinstance(result, str)
                else ""
            )
            if not isinstance(result, str):
                return result
        if not request:
            return None
        if is_exit_phrase(request):
            return VOICE_EXIT
        return complete_sentence(console, session, request, detector.label)
    return result


# ---------------------------------------------------------------------------
# Always listening (no wake word)
# ---------------------------------------------------------------------------

# Verbs that start a command to a computer. Everyday conversation starters
# ("let's", "tell", "give", "wait") are left out: they'd make the TV or
# people nearby trigger Jarvis. Inside a conversation everything counts.
_COMMAND_VERBS = (
    "open|close|quit|exit|launch|start|run|execute|stop|play|pause|resume|skip|"
    "next|previous|set|turn|switch|search|google|find|look|show|read|send|"
    "email|e-mail|mail|message|whatsapp|text|reply|call|remind|create|make|"
    "take|lock|shut|restart|reboot|mute|unmute|volume|brightness|"
    "minimi[sz]e|maximi[sz]e|snap|copy|paste|save|delete|remove|move|rename|"
    "download|upload|install|update|cancel|remember|forget|check|write|type|"
    "press|hit|scroll|go|bring|put|add|increase|decrease|lower|raise|empty|"
    "translate|summari[sz]e|describe|navigate|new tab|refresh|reload|"
    "wake me|compare|draft|compose|retry|undo|redo|click|select|debug|"
    "snooze|dismiss|silence|schedule|calculate|convert|zoom|enable|disable|"
    "toggle|screenshot|capture|edit|insert|replace|generate|kill|rerun|re-run"
)
_COMMAND = re.compile(rf"^(?:{_COMMAND_VERBS})\b", re.IGNORECASE)
_QUESTION = re.compile(
    r"^(?:what|what's|whats|who|who's|where|when|why|how|which|is|are|can|"
    r"could|do|does|did|will|would|should|tell me|give me|explain)\b",
    re.IGNORECASE,
)
# Said to an assistant, not to the room: "…please", "can you…", "…for me".
_ASKING = re.compile(
    r"\b(?:please|kindly)\b|^(?:can|could|would|will) you\b|\bfor me\b",
    re.IGNORECASE,
)
# About this PC: a question like "can you see my screen?" is for Jarvis even
# without the name or a conversation.
_MINE = re.compile(
    r"\b(?:my|the|this) (?:screen|laptop|pc|computer|desktop|browser|window|"
    r"tab|volume|brightness|alarm|timer|clipboard|inbox|whats\s?app|file|folder)"
    r"\b",
    re.IGNORECASE,
)
# Acknowledgements that need no answer ("okay", "nice").
_FILLER = re.compile(
    r"^(?:ok(?:ay)?|yeah|yep|yup|no|nope|nah|hmm+|uh+|um+|right|cool|nice|good|"
    r"great|thanks|thank you|fine|sure|alright|all right|wow|oh|ah|oops|haha|"
    r"lol|yes)\W*$",
    re.IGNORECASE,
)
# Thanks is never an answer or a correction, even right after a failure (a
# "Thank you." once re-ran the failed WhatsApp step). Whisper also writes
# "Thank you." for background noise.
_THANKS = re.compile(
    r"^(?:(?:ok(?:ay)?|great|cool|nice|perfect|alright|all right|good)[,.!\s]+)?"
    r"(?:thanks|thank you|thank u|thx)(?:\s+(?:so much|a lot|very much|jarvis|"
    r"buddy|bro|man))*\W*$",
    re.IGNORECASE,
)
# Stop always-listening. A bare "pause" is the music unless Jarvis is named.
_PAUSE = re.compile(
    r"^(?:pause listening|stop listening for now|go to sleep|sleep|be quiet|"
    r"take a break|don'?t listen)\b",
    re.IGNORECASE,
)
_NAMED_PAUSE = re.compile(r"^(?:pause|stop|quiet|wait)\W*$", re.IGNORECASE)
# A reply that ended in a question or a failure: the next thing the user
# says is almost certainly the answer or the correction.
_ASKS_BACK = re.compile(
    r"\?\s*$|\b(?:couldn'?t|can'?t|cannot|didn'?t|wasn'?t able|not sure|"
    r"which one|who do you mean|say which|don'?t know|not found|no (?:such|"
    r"match)|unable|failed)\b",
    re.IGNORECASE,
)
CONVERSATION_SECONDS = 45.0
# After Jarvis asked something or failed, wait longer for the follow-up.
FOLLOW_UP_SECONDS = 90.0


def classify_utterance(
    text: str, *, in_conversation: bool, expecting: bool = False
) -> str:
    """'command', 'question', 'pause' or 'ignore' for always-listening mode.

    Commands are acted on any time. Inside a conversation (within
    ``CONVERSATION_SECONDS`` of a reply) everything is for Jarvis - "now
    compare it with Google", "it's Anand" - because people don't say the
    name twice in a row. Outside one, questions need the name, so a video
    or people talking nearby ("what are you doing?") don't get answered.
    ``expecting`` means Jarvis just asked something or failed, so even a
    one-word answer ("yes", "Anand") counts.
    """
    from openjarvis.desktop.fastpath import WAKE_NAMES, normalize

    # "Jarvis" anywhere, or a near-miss of it opening the sentence ("Darv, …").
    opening = re.match(rf"^\W*(?:hey\s+)?{WAKE_NAMES}\b", text or "", re.IGNORECASE)
    named = bool(opening) or re.search(r"\bjarvis\b", text or "", re.I) is not None
    # ``normalize`` strips "can you" / "please"; judge those on the raw words.
    raw = re.sub(r"\bjarvis\b[,\s]*", " ", text or "", flags=re.IGNORECASE)
    if opening:
        raw = (text or "")[opening.end() :].lstrip(" ,.!")
    raw = " ".join(raw.split()).strip(" .!?,")
    cleaned = normalize(text)
    if not cleaned:
        return "ignore"
    if (_PAUSE.match(cleaned) and len(cleaned.split()) <= 5) or (
        named and _NAMED_PAUSE.match(cleaned)
    ):
        return "pause"
    words = cleaned.split()
    if _THANKS.match(cleaned) or _THANKS.match(raw):
        return "ignore"
    if cleaned.lower().strip(" .!") in ("pause", "resume", "mute", "unmute"):
        return "command"
    if _COMMAND.match(cleaned) and (len(words) >= 2 or named):
        return "command"
    if named and len(words) >= 1:
        return "question"
    if _FILLER.match(cleaned) and not expecting:
        return "ignore"
    if in_conversation or expecting:
        return "question" if _QUESTION.match(raw) else "command"
    if _ASKING.search(raw) and len(words) >= 2:
        return "command"
    if _QUESTION.match(raw) and _MINE.search(raw) and len(raw.split()) >= 3:
        return "question"
    return "ignore"


class AlwaysListener:
    """State for wake-word-free listening: paused?, last reply time."""

    def __init__(self) -> None:
        self.paused = False
        self.last_reply = 0.0
        self.announced = False
        self.expecting = False  # Jarvis asked something or failed

    def replied(self, reply: str = "", ok: bool = True) -> None:
        self.last_reply = time.monotonic()
        self.expecting = (not ok) or _ASKS_BACK.search(reply or "") is not None

    def in_conversation(self) -> bool:
        limit = FOLLOW_UP_SECONDS if self.expecting else CONVERSATION_SECONDS
        return time.monotonic() - self.last_reply < limit


def listen_always(
    console: Any,
    session: VoiceSession,
    detector: Any,
    announcer: ReminderAnnouncer,
    listener: AlwaysListener,
) -> Optional[str] | object:
    """Next request in always-listening mode (no wake word needed).

    Speech that isn't for Jarvis is shown dimmed and ignored. "Jarvis,
    pause" switches to wake-word mode until "Hey Jarvis". The window's
    pause/resume button and typed commands end a wait early (None).
    """
    from openjarvis.core import live

    label = getattr(detector, "label", "hey jarvis") if detector else "hey jarvis"
    wanted = live.take_pause_request()
    if wanted is not None and wanted != listener.paused:
        listener.paused = wanted
        listener.announced = False
        if not wanted:
            listener.replied()
    if listener.paused:
        live.emit("paused", wake_word=label)
        result = wait_for_wake_command(console, session, detector, announcer)
        if isinstance(result, str):
            listener.paused = False
            listener.announced = False
            listener.replied()
            live.emit("heard", text=result)  # woken by the wake word
        return result
    if not listener.announced:
        console.print(
            "[dim cyan]Listening… just talk (say 'Jarvis, pause' to switch "
            "to the wake word; Ctrl-C to quit)[/dim cyan]"
        )
        listener.announced = True
    time.sleep(0.25)  # let the room settle after Jarvis spoke
    while True:
        announcer.speak_pending(console, session)
        if live.needs_attention():
            return None  # a typed command or pause from the window
        live.emit("listening")
        heard = record_voice(console, session, follow_up_seconds=15.0, quiet=True)
        if heard is VOICE_EXIT or heard is None:
            if heard is VOICE_EXIT:
                return VOICE_EXIT
            continue
        text = str(heard)
        talking = listener.in_conversation()
        verdict = classify_utterance(
            text, in_conversation=talking, expecting=listener.expecting and talking
        )
        if verdict == "ignore":
            live.emit("ignored", text=text)
            console.print(
                f"[dim]  (heard, not for me: {_terminal_safe_text(text)})[/dim]"
            )
            continue
        live.emit("heard", text=text)
        console.print(f"[bold]You (voice):[/bold] {_terminal_safe_text(text)}")
        if verdict == "pause":
            listener.paused = True
            live.emit("paused", wake_word=label)
            speak(f"Okay. Say {label} when you need me.", console, session)
            return None
        request = strip_wake_phrase(text, label)
        if not request:
            continue
        if is_exit_phrase(request):
            return VOICE_EXIT
        return complete_sentence(console, session, request, label)


# A request ending like this was cut off by a pause ("Open a chat in…").
_DANGLING = re.compile(
    r"\b(?:in|on|to|with|and|the|a|an|of|at|from|my|about|called|named|into|"
    r"or|but|for|is|saying|that)\W*$",
    re.IGNORECASE,
)


def complete_sentence(
    console: Any, session: VoiceSession, request: str, label: str = ""
) -> str:
    """If the user paused mid-sentence, listen briefly for the rest."""
    if not _DANGLING.search(request):
        return request
    console.print("[dim cyan]…[/dim cyan]")
    more = record_voice(console, session, follow_up_seconds=2.5)
    if isinstance(more, str):
        rest = strip_wake_phrase(more, label) if label else more.strip()
        if rest:
            return f"{request.rstrip(' .,')} {rest}"
    return request


__all__ = [
    "LISTEN_DEFAULT_AGENT",
    "complete_sentence",
    "LISTEN_DEFAULT_TOOLS",
    "LISTEN_DESKTOP_TOOLS",
    "LISTEN_FALLBACK_AGENT",
    "ReminderAnnouncer",
    "apply_power_policy",
    "AlwaysListener",
    "classify_utterance",
    "is_affirmative",
    "listen_always",
    "is_exit_phrase",
    "make_voice_confirm",
    "preload_model",
    "progress_printer",
    "strip_wake_phrase",
    "voice_keep_alive",
    "voice_system_template",
    "wait_for_wake_command",
    "warm_up",
]
