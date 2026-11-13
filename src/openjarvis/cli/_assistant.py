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


def wait_for_wake_command(
    console: Any,
    session: VoiceSession,
    detector: Any,
    announcer: ReminderAnnouncer,
    *,
    skip_wake: bool = False,
) -> Optional[str] | object:
    """Wait for the wake word, then record and transcribe one command.

    Returns the transcript, None if nothing was heard, or ``VOICE_EXIT`` on
    Ctrl-C / an exit phrase. Due reminders are spoken while waiting.
    """
    from openjarvis.speech.voice_io import play_chime

    if not skip_wake:
        console.print(
            f"[dim cyan]Waiting for '{detector.label}'… (Ctrl-C to quit)[/dim cyan]"
        )
        while True:
            announcer.speak_pending(console, session)
            try:
                heard = detector.wait(
                    should_abort=lambda: not announcer.pending.empty()
                )
            except KeyboardInterrupt:
                return VOICE_EXIT
            except Exception as exc:
                console.print(f"[red]Mic error: {_terminal_safe_text(exc)}[/red]")
                return VOICE_EXIT
            if heard:
                break
    try:
        play_chime()
    except Exception as exc:  # a missing output device must not block input
        logger.debug("Chime failed: %s", exc)
    result = record_voice(console, session)
    if isinstance(result, str) and is_exit_phrase(result):
        return VOICE_EXIT
    return result


__all__ = [
    "LISTEN_DEFAULT_AGENT",
    "LISTEN_DEFAULT_TOOLS",
    "ReminderAnnouncer",
    "apply_power_policy",
    "is_exit_phrase",
    "preload_model",
    "voice_keep_alive",
    "wait_for_wake_command",
]
