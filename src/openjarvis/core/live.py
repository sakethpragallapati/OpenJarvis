"""Live status of the voice assistant for a front-end (the JARVIS window).

The voice loop calls :func:`emit` at the moments a window would show:
listening, the microphone level, a transcript, thinking, each tool step, the
reply, speaking and its loudness. With nobody subscribed (``jarvis listen``
in a terminal) every call returns at once.

The window talks back through the same module: a typed command
(:func:`type_command`), "stop speaking" / "stop" and pause/resume. The voice
loop polls these between and during recordings, so a typed command doesn't
wait for the microphone to time out.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

Listener = Callable[[str, Dict[str, Any]], None]

_LOCK = threading.Lock()
_LISTENERS: List[Listener] = []
_TYPED: "queue.Queue[str]" = queue.Queue()
_STOP_SPEAKING = threading.Event()
_PAUSE_WANTED: List[Optional[bool]] = [None]  # True = pause, False = resume
_LAST_LEVEL = [0.0]


def subscribe(listener: Listener) -> Callable[[], None]:
    """Receive every event; returns a function that unsubscribes."""
    with _LOCK:
        _LISTENERS.append(listener)

    def unsubscribe() -> None:
        with _LOCK:
            if listener in _LISTENERS:
                _LISTENERS.remove(listener)

    return unsubscribe


def active() -> bool:
    """Is a front-end listening? (Skip work that only feeds it.)"""
    return bool(_LISTENERS)


def emit(kind: str, /, **data: Any) -> None:
    # Positional-only: tool steps carry their own ``kind`` ("tool_start").
    if not _LISTENERS:
        return
    with _LOCK:
        listeners = list(_LISTENERS)
    for listener in listeners:
        try:
            listener(kind, data)
        except Exception:
            logger.debug("live listener failed on %s", kind, exc_info=True)


def level(
    kind: str,
    value: float,
    *,
    every: float = 0.05,
    bands: Optional[List[float]] = None,
) -> None:
    """A loudness update ("mic" or "voice", 0-1), at most every ``every`` s.

    ``bands`` (optional, 0-1 each) is a coarse spectrum, low to high, that
    the window's ring dances to.
    """
    if not _LISTENERS:
        return
    now = time.monotonic()
    if now - _LAST_LEVEL[0] < every:
        return
    _LAST_LEVEL[0] = now
    data: Dict[str, Any] = {"level": max(0.0, min(1.0, float(value)))}
    if bands:
        data["bands"] = [round(max(0.0, min(1.0, float(b))), 3) for b in bands]
    emit(kind, **data)


def spectrum(samples: Any, rate: int, count: int = 12) -> List[float]:
    """``count`` loudness bands (0-1, log-spaced 80 Hz - 7 kHz) of a short
    mono float/int16 piece of audio, for the window's visualiser."""
    try:
        import numpy as np
    except ImportError:
        return []
    piece = np.asarray(samples, dtype="float32").reshape(-1)
    if piece.size < 64:
        return []
    if np.abs(piece).max() > 2.0:  # int16 samples
        piece = piece / 32768.0
    window = piece * np.hanning(piece.size)
    power = np.abs(np.fft.rfft(window))
    freqs = np.fft.rfftfreq(piece.size, 1.0 / rate)
    edges = np.geomspace(80.0, min(7000.0, rate / 2 - 1), count + 1)
    out = []
    for low, high in zip(edges[:-1], edges[1:]):
        mask = (freqs >= low) & (freqs < high)
        value = float(power[mask].mean()) if mask.any() else 0.0
        # Roughly 0-1 for speech: log scale, -50 dB .. 0 dB.
        out.append(max(0.0, min(1.0, (20 * np.log10(value + 1e-6) + 20) / 50)))
    return out


# -- from the window to the voice loop ------------------------------------------


def type_command(text: str) -> None:
    text = " ".join((text or "").split())
    if text:
        _TYPED.put(text)


def take_typed() -> Optional[str]:
    try:
        return _TYPED.get_nowait()
    except queue.Empty:
        return None


def has_typed() -> bool:
    return not _TYPED.empty()


def stop_speaking() -> None:
    _STOP_SPEAKING.set()


def stop_speaking_requested() -> bool:
    return _STOP_SPEAKING.is_set()


def clear_stop_speaking() -> None:
    _STOP_SPEAKING.clear()


def want_pause(paused: bool) -> None:
    _PAUSE_WANTED[0] = bool(paused)


def take_pause_request() -> Optional[bool]:
    wanted, _PAUSE_WANTED[0] = _PAUSE_WANTED[0], None
    return wanted


def pause_requested() -> bool:
    return _PAUSE_WANTED[0] is not None


def needs_attention() -> bool:
    """Should a recording or wake-word wait end early? (typed command or a
    pause/resume from the window)."""
    return has_typed() or pause_requested()


__all__ = [
    "active",
    "clear_stop_speaking",
    "emit",
    "has_typed",
    "level",
    "needs_attention",
    "pause_requested",
    "stop_speaking",
    "stop_speaking_requested",
    "subscribe",
    "take_pause_request",
    "take_typed",
    "type_command",
    "want_pause",
]
