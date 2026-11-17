"""Help speech-to-text with the names people say to a desktop assistant.

Whisper doesn't know "VS Code" is a thing and writes "V's code" or "VS
Core". Two layers fix that:

1. ``hotwords()`` - a vocabulary hint given to Whisper (``hotwords``) so it
   prefers these spellings. On a noisy test set of spoken commands this took
   base.en from 52/60 to 60/60 correct at no extra latency.
2. ``correct_transcript()`` - a light post-pass that snaps near-misses of
   multi-word names back ("V's code" -> "VS Code", "Whats app" -> "WhatsApp")
   and drops Whisper's well-known phantom phrases on noise ("Thanks for
   watching", "See you in the next one").
"""

from __future__ import annotations

import difflib
import re
from typing import Iterable, List, Optional

DEFAULT_VOCABULARY = (
    "Jarvis",
    "VS Code",
    "Visual Studio Code",
    "YouTube",
    "WhatsApp",
    "Netflix",
    "Spotify",
    "Chrome",
    "Brave",
    "Edge",
    "Gmail",
    "GitHub",
    "ChatGPT",
    "Task Manager",
    "File Explorer",
    "Notepad",
    "PowerShell",
    "Command Prompt",
    "Minecraft",
    "Discord",
    "Telegram",
    "Instagram",
    "Microsoft Teams",
    "PowerPoint",
    "Excel",
    "OpenJarvis",
    "Wi-Fi",
    "Bluetooth",
    "lofi",
    "screenshot",
    "alarm",
    "timer",
    "reminder",
)

# Whole-phrase mishearings of common commands (heard -> meant), collected
# from real transcripts and from every Whisper size on noisy test commands.
_PHRASE_FIXES = (
    # Bigger Whisper models given a vocabulary hint glue the command word to
    # the next one ("OpenAsk Manager", "OpenVase code"); real names stay.
    (
        re.compile(
            r"\bOpen(?=(?!AI\b|Jarvis\b|Office\b|Street|Source\b|Shot\b|SSH\b|"
            r"SSL\b|CV\b|GL\b|API\b|Router\b|Table\b)[A-Z])"
        ),
        "Open ",
    ),
    (
        re.compile(
            r"\bset (?:and|an|a|in|them) "
            r"(?:allow|aloud|a loan|alarum|along|a long|alarms?)\b",
            re.I,
        ),
        "set an alarm",
    ),
    (re.compile(r"\b(?:an|and) allow (?=for |at )", re.I), "an alarm "),
    (re.compile(r"\bset a time (?=for \d)", re.I), "set a timer "),
    # "VS" said quickly: "vase code", "BS code", "V.S. code", "visa code",
    # and "Jarvis code" (the wake-word hint pulls "VS" toward "Jarvis").
    (
        re.compile(
            r"\b(?:vase|vaze|vays|vas|bs|v\s?s|visa|vees|jarvis)\.? "
            r"(?:code|core|cold|coat)\b",
            re.I,
        ),
        "VS Code",
    ),
    (
        re.compile(
            r"\b(?:snoose|snooz|snooth|smooth|snews)\b"
            r"(?= (?:the |my )?(?:alarm|timer|reminder|it\b|for\b))",
            re.I,
        ),
        "snooze",
    ),
    (re.compile(r"\bsnoose\b", re.I), "snooze"),
    (re.compile(r"\b(?:loafy|lo-fi|low-fi|low fi)\b", re.I), "lofi"),
)
# "11.43 pm", "11 43 p.m." -> "11:43 pm" (how Whisper writes clock times it
# isn't sure about), and "1143 pm" -> "11:43 pm".
_CLOCK_DOT = re.compile(
    r"\b(\d{1,2})[. ](\d{2})(?=\s*(?:a\.?m|p\.?m|o'?clock)\b)", re.I
)
_CLOCK_RUN = re.compile(r"\b(1[0-2]|[1-9])([0-5]\d)(?=\s*(?:a\.?m|p\.?m)\b)", re.I)

# Whisper "hears" these in silence, music or background video audio.
_PHANTOMS = re.compile(
    r"^\W*(?:(?:thanks|thank you)(?: so much)? for watching|"
    r"(?:we'?ll |i'?ll )?see you (?:in|on) the next (?:one|video|episode)|"
    r"(?:please )?(?:like and )?subscribe(?: to (?:my|the|our) channel)?|"
    r"subtitles by .*|transcribed by .*|you)\W*$",
    re.IGNORECASE,
)


def vocabulary(extra: str | Iterable[str] = "") -> List[str]:
    """Default names plus the user's own (comma-separated or a list)."""
    words = list(DEFAULT_VOCABULARY)
    if isinstance(extra, str):
        extra = [w for w in extra.split(",")]
    for word in extra:
        word = " ".join(str(word).split())
        if word and word.lower() not in {w.lower() for w in words}:
            words.append(word)
    return words


def hotwords(extra: str | Iterable[str] = "", *, limit_chars: int = 600) -> str:
    """The Whisper hint string (kept well under its ~224-token prompt cap)."""
    text = ""
    for word in vocabulary(extra):
        candidate = f"{text}, {word}" if text else word
        if len(candidate) > limit_chars:
            break
        text = candidate
    return text


# Command words never merge into the name after them.
_VERBS = frozenset(
    ("open", "close", "start", "launch", "play", "run", "show", "stop", "kill")
)


def _squash(text: str) -> str:
    text = re.sub(r"'s\b", "s", text.lower())
    return re.sub(r"[^a-z0-9]", "", text)


def _one_word_off(window: List[str], name: str) -> bool:
    """'VS core' ~ 'VS Code': all words equal but one, and that one close."""
    said = [_squash(w) for w in window]
    want = [_squash(w) for w in name.split()]
    diffs = [(a, b) for a, b in zip(said, want) if a != b]
    if len(diffs) != 1:
        return False
    a, b = diffs[0]
    return len(b) >= 3 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.75


def is_phantom(text: str) -> bool:
    """True when the whole transcript is a known Whisper hallucination."""
    return bool(_PHANTOMS.match(text or ""))


def correct_transcript(text: str, words: Optional[Iterable[str]] = None) -> str:
    """Snap mis-spellings of known names back; '' for phantom phrases."""
    if not text or is_phantom(text):
        return ""
    for pattern, replacement in _PHRASE_FIXES:
        text = pattern.sub(replacement, text)
    text = _CLOCK_DOT.sub(r"\1:\2", text)
    text = _CLOCK_RUN.sub(r"\1:\2", text)
    names = [w for w in (words or DEFAULT_VOCABULARY) if _squash(w)]
    tokens = text.split()
    out: List[str] = []
    i = 0
    while i < len(tokens):
        replaced = False
        for name in sorted(names, key=lambda n: -len(n.split())):
            size = len(name.split())
            target = _squash(name)
            for span in (size, size + 1):
                if i + span > len(tokens):
                    continue
                window = tokens[i : i + span]
                if span > size and _squash(window[0]) in _VERBS:
                    continue  # "open Jarvis" is a command, not "OpenJarvis"
                core = " ".join(window)
                lead = re.match(r"^\W*", core).group(0)
                trail = re.search(r"\W*$", core).group(0)
                squashed = _squash(core)
                exact = squashed == target
                close = (
                    len(target) >= 6
                    and (size > 1 or span > 1)
                    and difflib.SequenceMatcher(None, squashed, target).ratio() >= 0.9
                ) or (span == size > 1 and _one_word_off(window, name))
                bare = core[len(lead) : len(core) - len(trail) or None]
                if exact and span == size:
                    # Already this name ("V's code" still gets respelled);
                    # never let it swallow the next word ("GitHub on").
                    if size > 1 and bare.lower() != name.lower():
                        out.append(f"{lead}{name}{trail}")
                        i += span
                        replaced = True
                    break
                if (exact or close) and bare.lower() != name.lower():
                    out.append(f"{lead}{name}{trail}")
                    i += span
                    replaced = True
                    break
            if replaced:
                break
        if not replaced:
            out.append(tokens[i])
            i += 1
    return " ".join(out)


__all__ = [
    "DEFAULT_VOCABULARY",
    "correct_transcript",
    "hotwords",
    "is_phantom",
    "vocabulary",
]
