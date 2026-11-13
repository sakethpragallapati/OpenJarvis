"""Desktop-assistant plumbing for ``jarvis chat --voice`` / ``jarvis listen``.

Kept out of ``chat_cmd`` so the REPL stays readable: laptop power policy,
model warm-up, reminder delivery, and the wake-word input loop.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import threading
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
LISTEN_DEFAULT_AGENT = "orchestrator"
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
            config.speech.device = "cpu"
            config.speech.compute_type = "int8"
            notes.append("speech on CPU")
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
            config.speech.device = "cpu"
            config.speech.compute_type = "int8"
            console.print("[dim]Speech-to-text will run on CPU.[/dim]")
    return model


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
    build = getattr(agent, "_build_messages", None)
    generate = getattr(agent, "_generate", None)
    if build is None or generate is None:
        return
    messages = build(
        "Hello", None, system_prompt=getattr(agent, "_system_prompt", None)
    )
    kwargs: dict[str, Any] = {"max_tokens": 1}
    executor = getattr(agent, "_executor", None)
    if executor is not None and getattr(agent, "_tools", None):
        kwargs["tools"] = executor.get_openai_tools()
    generate(messages, **kwargs)


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
        from openjarvis.pc.notify import notify

        notify("Reminder", reminder.message)
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

        while True:
            announcer.speak_pending(console, session)
            try:
                audio = detector.wait(
                    should_abort=lambda: not announcer.pending.empty(),
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
        return request
    return result


__all__ = [
    "LISTEN_DEFAULT_AGENT",
    "LISTEN_DEFAULT_TOOLS",
    "ReminderAnnouncer",
    "apply_power_policy",
    "is_exit_phrase",
    "preload_model",
    "strip_wake_phrase",
    "voice_keep_alive",
    "voice_system_template",
    "wait_for_wake_command",
    "warm_up",
]
