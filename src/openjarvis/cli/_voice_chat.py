"""Voice input/output helpers for the interactive chat session."""

from __future__ import annotations

import queue
import re
import sys
import threading
from typing import Any, Callable, Optional

from rich.markup import escape

VOICE_EXIT = object()
_SYNTH_DONE = object()
_NO_TTS_BACKEND = object()

# Backend selection is shared with the API server and lives in
# openjarvis.speech._tts_discovery. Importing that at module level would pull
# the whole speech stack -- numpy included -- into every `jarvis` invocation,
# which tests/cli/test_cli.py guards against, so these aliases resolve lazily.
_LAZY_TTS_NAMES = {
    "_TTS_BACKEND_ORDER": "TTS_BACKEND_ORDER",
    "_BACKEND_DEFAULT_VOICE": "BACKEND_DEFAULT_VOICE",
}


def __getattr__(name: str) -> Any:
    target = _LAZY_TTS_NAMES.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from openjarvis.speech import _tts_discovery

    return getattr(_tts_discovery, target)


def _terminal_safe_text(value: object) -> str:
    """Remove terminal controls and escape Rich markup from dynamic text."""
    printable = "".join(
        char for char in str(value) if char in ("\n", "\t") or char.isprintable()
    )
    return escape(printable)


class VoiceSession:
    """Cache healthy speech backends for one interactive chat session."""

    def __init__(self, config: object | None = None) -> None:
        self._config = config
        self._stt_resolved = False
        self._stt_backend: Any = None
        self._tts_backend: Any = None
        self._tts_attempted: set[str] = set()
        self._voice_prefs: tuple[str, str, float] | None = None
        self._voice_warned: set[str] = set()
        self._silence_threshold: int | None = None
        self.last_stt_seconds = 0.0

    @property
    def silence_threshold(self) -> int | None:
        """Mic level that counts as speech; None = voice_io's fixed default."""
        return self._silence_threshold

    def record_kwargs(self, **overrides: Any) -> dict[str, Any]:
        """Keyword arguments for voice_io's recorders from this session."""
        from openjarvis.core.config import load_config

        config = self._config if self._config is not None else load_config()
        kwargs: dict[str, Any] = {}
        if self._silence_threshold is not None:
            kwargs["silence_threshold"] = self._silence_threshold
        end_silence = float(getattr(config.speech, "end_silence_seconds", 0) or 0)
        if end_silence > 0:
            kwargs["silence_seconds"] = end_silence
        kwargs.update(overrides)
        return kwargs

    def warm_up(self) -> list[str]:
        """Load speech-to-text and the voice now instead of on first use.

        Loading the voice model takes 10-40 s and Whisper a few seconds; doing it lazily
        made the first reply take that long to be heard. Returns problems.
        """
        problems: list[str] = []
        try:
            stt = self.get_stt_backend()
            if stt is None:
                problems.append("no speech-to-text backend")
            elif getattr(stt, "cloud", False):
                pass  # nothing to load, and a test decode would be a paid call
            else:
                from openjarvis.speech.voice_io import _frames_to_wav

                # A first decode initializes the CTranslate2 kernels too.
                silence = _frames_to_wav([bytes(3200)], 16000)
                stt.transcribe(silence, format="wav")
                if self.stt_options(stt):  # also load the VAD model now
                    stt.transcribe(silence, format="wav", **self.stt_options(stt))
        except Exception as exc:
            problems.append(f"speech-to-text: {exc}")
        try:
            tts = self.get_tts_backend()
            if tts is None:
                problems.append("no text-to-speech backend")
            else:
                voice_id, speed = self.voice_for_backend(tts)
                kwargs: dict[str, Any] = {"output_format": "wav", "speed": speed}
                if voice_id:
                    kwargs["voice_id"] = voice_id
                tts.synthesize("Ready.", **kwargs)
        except Exception as exc:
            problems.append(f"text-to-speech: {exc}")
        return problems

    def calibrate_microphone(self, console: Any = None) -> int | None:
        """Pick the speech threshold for this session from the room's noise.

        ``speech.silence_threshold`` > 0 pins it; otherwise the background
        level is measured once, while nobody is talking yet.
        """
        from openjarvis.core.config import load_config

        config = self._config if self._config is not None else load_config()
        fixed = int(getattr(config.speech, "silence_threshold", 0) or 0)
        if fixed > 0:
            self._silence_threshold = fixed
            return fixed

        from openjarvis.speech.voice_io import measure_noise_floor, threshold_from_noise

        try:
            noise = measure_noise_floor()
        except Exception as exc:
            if console is not None:
                console.print(
                    "[dim yellow]Mic calibration skipped: "
                    f"{_terminal_safe_text(exc)}[/dim yellow]"
                )
            return None
        self._silence_threshold = threshold_from_noise(noise)
        if console is not None:
            console.print(
                f"[dim]Mic calibrated (background {noise:.0f}, "
                f"speech threshold {self._silence_threshold}).[/dim]"
            )
        return self._silence_threshold

    def get_stt_backend(self) -> Any:
        """Resolve and health-check STT once, then reuse the loaded backend."""
        if not self._stt_resolved:
            from openjarvis.core.config import load_config
            from openjarvis.speech._discovery import get_speech_backend

            config = self._config if self._config is not None else load_config()
            self._stt_backend = get_speech_backend(config)
            self._stt_resolved = True
        return self._stt_backend

    def _speech_config(self) -> Any:
        from openjarvis.core.config import load_config

        config = self._config if self._config is not None else load_config()
        return config.speech

    def vocabulary(self) -> list[str]:
        from openjarvis.speech.vocabulary import vocabulary

        return vocabulary(getattr(self._speech_config(), "vocabulary", "") or "")

    def stt_options(self, backend: Any) -> dict[str, Any]:
        """Command-tuned decoding + name hints, for backends that take them."""
        backend_id = getattr(backend, "backend_id", "")
        if backend_id not in ("faster-whisper", "groq"):
            return {}
        from openjarvis.speech.vocabulary import hotwords

        speech = self._speech_config()
        # Your own words first, then the built-in app names, then contacts.
        own = [
            w.strip()
            for w in str(getattr(speech, "vocabulary", "") or "").split(",")
            if w.strip()
        ]
        room = max(0, 320 - len(", ".join(own)))
        base = hotwords("", limit_chars=room) if room > 40 else ""
        hints = ", ".join([*own, base] if base else own)
        people = self.contact_hints()
        if people:
            hints = f"{hints}, {', '.join(people)}" if hints else ", ".join(people)
        if backend_id == "groq":
            return {"hotwords": hints}  # sent as Whisper's spelling prompt
        return {
            "hotwords": hints,
            "short_utterance": True,
            "vad_filter": bool(getattr(speech, "vad_filter", True)),
        }

    def contact_hints(self) -> list[str]:
        """Contact names to prime recognition with (Indian names like
        "Anand", "Thrisha" come out as "armed", "on and" without them)."""
        if not bool(getattr(self._speech_config(), "contact_hints", True)):
            return []
        try:
            from openjarvis.desktop.contacts import directory

            return directory().hint_names(limit_chars=300)
        except Exception:
            return []

    def correct(self, text: str) -> str:
        """Fix near-misses of known names; '' for Whisper phantom phrases."""
        from openjarvis.speech.vocabulary import correct_transcript

        text = correct_transcript(text, self.vocabulary())
        if text and bool(getattr(self._speech_config(), "contact_hints", True)):
            try:
                from openjarvis.desktop.contacts import repair_names

                text = repair_names(text)
            except Exception:
                pass
        return text

    def get_stt_language(self) -> Optional[str]:
        """Return the configured ``speech.language``, or None to auto-detect."""
        from openjarvis.core.config import load_config

        config = self._config if self._config is not None else load_config()
        return (getattr(config.speech, "language", "") or "").strip() or None

    def get_tts_backend(self) -> Any:
        """Return a cached healthy TTS backend, falling through once per key."""
        if self._tts_backend is not None:
            return self._tts_backend

        from openjarvis.speech._tts_discovery import get_tts_backend

        preferred, _, _ = self.get_voice_preferences()
        self._tts_backend = get_tts_backend(preferred, attempted=self._tts_attempted)
        return self._tts_backend

    def get_voice_preferences(self) -> tuple[str, str, float]:
        """Resolve configured (tts_backend, voice_id, speed), cached per session."""
        if self._voice_prefs is None:
            from openjarvis.core.config import load_config
            from openjarvis.speech._tts_discovery import voice_preferences

            config = self._config if self._config is not None else load_config()
            self._voice_prefs = voice_preferences(config)
        return self._voice_prefs

    def voice_for_backend(self, backend: Any, console: Any = None) -> tuple[str, float]:
        """Return the voice ID valid for ``backend``, plus the configured speed.

        Voice IDs are not portable across backends, so the configured
        ``speech.voice_id`` is honored only when ``backend`` is the configured
        ``speech.tts_backend``. Otherwise the fallback backend's own default is
        used and the substitution is reported once per session.
        """
        want_backend, voice_id, speed = self.get_voice_preferences()
        active = getattr(backend, "backend_id", "") or ""
        if active == want_backend:
            return voice_id, speed

        from openjarvis.speech._tts_discovery import default_voice_for

        substitute = default_voice_for(active)
        if console is not None and active not in self._voice_warned:
            self._voice_warned.add(active)
            console.print(
                f"[dim yellow]Voice: {want_backend!r} unavailable — using "
                f"{active!r} with its default voice "
                f"({substitute or 'backend default'}).[/dim yellow]"
            )
        return substitute, speed

    def discard_tts_backend(self) -> None:
        """Forget a backend that failed synthesis and allow the next fallback."""
        self._tts_backend = None


def read_voice_input(console: Any, session: VoiceSession) -> Optional[str] | object:
    """Accept a typed command/message, or record after an empty submission."""
    try:
        typed = input("You> [type, or press Enter to speak] ")
    except (EOFError, KeyboardInterrupt):
        return VOICE_EXIT
    typed = typed.strip()
    return typed if typed else record_voice(console, session)


def record_voice(
    console: Any,
    session: VoiceSession | None = None,
    *,
    audio: bytes | None = None,
    follow_up_seconds: float | None = None,
    quiet: bool = False,
) -> Optional[str] | object:
    """Record from mic, transcribe, and return text or a loop sentinel.

    ``quiet`` prints nothing about the utterance (always-listening mode
    decides afterwards whether it was meant for Jarvis).

    ``audio`` transcribes an utterance already recorded (e.g. on the
    wake-word stream). ``follow_up_seconds`` listens quietly for that long
    and returns None without a message if nobody starts speaking.
    """
    import time

    from openjarvis.speech.voice_io import record_until_silence

    active_session = session or VoiceSession()
    backend = active_session.get_stt_backend()
    if backend is None:
        console.print(
            "[red]No speech-to-text backend available. "
            "Install the voice dependencies with: "
            "pip install 'OpenJarvis[speech,voice]' "
            "(or: uv sync --extra speech --extra voice), or configure a "
            "healthy OpenAI/Deepgram backend.[/red]"
        )
        return VOICE_EXIT

    if audio is not None:
        audio_bytes = audio
    else:
        if follow_up_seconds is None:
            console.print(
                "[dim cyan]Listening… (speak now, stops on silence)[/dim cyan]"
            )
            record_kwargs = active_session.record_kwargs()
        else:
            record_kwargs = active_session.record_kwargs(
                startup_silence_seconds=follow_up_seconds, require_speech=True
            )
        try:
            audio_bytes = record_until_silence(**record_kwargs)
        except KeyboardInterrupt:
            return VOICE_EXIT
        except Exception as exc:
            # PortAudio failures are regular Exceptions (and can surface as
            # OSError), so report them without letting terminal control
            # sequences through. SystemExit deliberately continues to
            # propagate, while Ctrl-C maps to the chat loop's graceful exit.
            console.print(f"[red]Mic error: {_terminal_safe_text(exc)}[/red]")
            return VOICE_EXIT
    if not audio_bytes:
        return None  # nobody started speaking

    from openjarvis.core import live

    live.emit("transcribing")
    if not quiet:
        console.print("[dim]Transcribing…[/dim]")
    started = time.perf_counter()
    try:
        result = backend.transcribe(
            audio_bytes,
            format="wav",
            language=active_session.get_stt_language(),
            **active_session.stt_options(backend),
        )
        active_session.last_stt_seconds = time.perf_counter() - started
        text = active_session.correct(result.text.strip())
        if text:
            if not quiet:
                console.print(f"[bold]You (voice):[/bold] {_terminal_safe_text(text)}")
            return text
        if follow_up_seconds is None and not quiet:
            console.print("[dim]Nothing heard — try again.[/dim]")
        return None
    except Exception as exc:
        console.print(f"[red]Transcription error: {_terminal_safe_text(exc)}[/red]")
        return None


_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|\n+")
# Later chunks are merged up to this size: fewer, longer synth calls sound
# more natural, while the first chunk stays short so playback starts fast.
_MIN_CHUNK_CHARS = 80
# No chunk longer than this (about 12 s of speech).
_MAX_CHUNK_CHARS = 200
# Speak at most about this much of a reply (~80 s); the rest stays on screen.
_MAX_SPOKEN_CHARS = 1200


def speakable_text(text: str) -> str:
    """What to say for a reply: Markdown, symbols, currency and list marks
    rewritten for the ear (see ``openjarvis.speech.speakable``). Very long
    replies stop after about a minute with "The rest is on the screen."."""
    from openjarvis.speech.speakable import to_speech

    return to_speech(text, max_chars=_MAX_SPOKEN_CHARS)


def split_for_speech(text: str) -> list[str]:
    """Split text into sentence chunks for pipelined synthesis.

    A run-on sentence is cut at commas so no chunk is too long for the voice
    model (it once disabled fast voice for the rest of the session).
    """
    from openjarvis.speech.speakable import split_long

    pieces = [
        part
        for p in _SENTENCE_END.split(text)
        if p and p.strip()
        for part in split_long(p.strip(), _MAX_CHUNK_CHARS)
    ]
    chunks: list[str] = []
    for piece in pieces:
        if (
            len(chunks) > 1
            and len(chunks[-1]) < _MIN_CHUNK_CHARS
            and len(chunks[-1]) + len(piece) < _MAX_CHUNK_CHARS
        ):
            chunks[-1] = f"{chunks[-1]} {piece}"
        else:
            chunks.append(piece)
    return chunks


def _key_pressed() -> bool:
    """True if a key was pressed in the console (Windows only; drains input)."""
    if sys.platform != "win32":
        return False
    try:
        import msvcrt
    except ImportError:
        return False
    if not sys.stdin.isatty():
        return False
    pressed = False
    while msvcrt.kbhit():  # type: ignore[attr-defined]
        msvcrt.getwch()  # type: ignore[attr-defined]
        pressed = True
    return pressed


def _wav_seconds(audio: bytes, sample_rate: int) -> float:
    """How long a synthesized chunk plays."""
    import io
    import wave

    try:
        with wave.open(io.BytesIO(audio)) as clip:
            return clip.getnframes() / float(clip.getframerate() or sample_rate)
    except Exception:
        return len(audio) / 2.0 / float(sample_rate or 24000)


def _synthesize_chunk(
    chunk: str, console: Any, session: VoiceSession
) -> tuple[bytes, int] | None:
    """Synthesize one chunk, falling through unhealthy backends once each."""
    while (backend := session.get_tts_backend()) is not None:
        try:
            voice_id, speed = session.voice_for_backend(backend, console)
            synth_kwargs: dict[str, Any] = {"output_format": "wav", "speed": speed}
            if voice_id:
                synth_kwargs["voice_id"] = voice_id
            result = backend.synthesize(chunk, **synth_kwargs)
            return result.audio, result.sample_rate
        except Exception as exc:
            console.print(
                f"[dim yellow]Voice backend "
                f"{getattr(backend, 'backend_id', '?')!r} failed: "
                f"{_terminal_safe_text(exc)}[/dim yellow]"
            )
            session.discard_tts_backend()
    return None


def speak(
    text: str,
    console: Any,
    session: VoiceSession | None = None,
    *,
    should_stop: Optional[Callable[[], bool]] = None,
) -> bool:
    """Speak ``text``, starting as soon as the first sentence is synthesized.

    Sentences are synthesized on a worker thread while earlier ones play, so
    a long reply no longer waits for the whole text to be converted. Ctrl-C,
    any key (Windows console), or ``should_stop()`` returning True cuts the
    reply short. Returns False when playback was interrupted.
    """
    from openjarvis.speech import voice_io

    active_session = session or VoiceSession()

    # Resolved before the loop: a bad config value must surface as a config
    # error, not be swallowed by the per-backend fallback handler below.
    try:
        active_session.get_voice_preferences()
    except Exception as exc:
        console.print(f"[red]Invalid speech config: {_terminal_safe_text(exc)}[/red]")
        return True

    chunks = split_for_speech(speakable_text(text))
    if not chunks:
        return True

    from openjarvis.core import live

    stop = threading.Event()
    live.clear_stop_speaking()
    offsets = [sum(len(c) for c in chunks[:i]) for i in range(len(chunks))]
    total_chars = sum(len(c) for c in chunks)
    live.emit("speaking", text=text, total=total_chars)

    def stop_requested() -> bool:
        if not stop.is_set() and (
            _key_pressed()
            or live.stop_speaking_requested()  # the window's Stop button
            or (should_stop is not None and should_stop())
        ):
            stop.set()
        return stop.is_set()

    audio_queue: queue.Queue[object] = queue.Queue(maxsize=2)

    def put(item: object) -> None:
        while not stop.is_set():
            try:
                audio_queue.put(item, timeout=0.1)
                return
            except queue.Full:
                continue

    def produce() -> None:
        try:
            for index, chunk in enumerate(chunks):
                if stop.is_set():
                    return
                audio = _synthesize_chunk(chunk, console, active_session)
                if audio is None:
                    put(_NO_TTS_BACKEND)
                    return
                put((*audio, index))
        finally:
            put(_SYNTH_DONE)

    worker = threading.Thread(target=produce, name="openjarvis-tts", daemon=True)
    worker.start()
    interrupted = False
    try:
        while True:
            try:
                item = audio_queue.get(timeout=0.1)
            except queue.Empty:
                if stop_requested():
                    interrupted = True
                    break
                continue
            if item is _SYNTH_DONE:
                break
            if item is _NO_TTS_BACKEND:
                console.print(
                    "[dim yellow]No TTS backend available — install the voice: "
                    "uv sync --extra voice[/dim yellow]"
                )
                break
            audio, sample_rate, index = item  # type: ignore[misc]
            if not audio:
                continue
            # What's being said right now and for how long: the window shows
            # it word by word in time with the voice.
            live.emit(
                "say",
                text=chunks[index],
                index=index,
                count=len(chunks),
                start=offsets[index],
                total=total_chars,
                seconds=round(_wav_seconds(audio, sample_rate), 2),
            )
            finished = voice_io.play_wav(
                audio, sample_rate=sample_rate, should_stop=stop_requested
            )
            if finished is False:
                interrupted = True
                break
    except KeyboardInterrupt:
        voice_io.stop_playback()
        interrupted = True
    finally:
        stop.set()
        worker.join(timeout=2.0)
        live.clear_stop_speaking()
        live.emit("spoken", interrupted=interrupted)
    if interrupted:
        console.print("[dim]Stopped speaking.[/dim]")
    return not interrupted


__all__ = [
    "VOICE_EXIT",
    "VoiceSession",
    "read_voice_input",
    "record_voice",
    "speak",
    "speakable_text",
    "split_for_speech",
]
