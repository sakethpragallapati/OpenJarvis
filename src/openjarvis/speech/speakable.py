"""Turn a written reply into what should be *said*.

Replies are shown on screen as Markdown (bullets, bold, numbers with
currency signs) and read aloud by the TTS voice. Read literally, that's
"dash Samsung Galaxy A17 dash rupee 29,200 open bracket 8GB RAM close
bracket". This module rewrites the text for the ear: list items become
sentences, symbols become words ("$4.01T" -> "4.01 trillion dollars",
"P/E" -> "P E", "vs" -> "versus", "~5" -> "about 5"), and leftover marks
that mean nothing out loud are dropped.
"""

from __future__ import annotations

import re
from typing import List

_CODE_BLOCK = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_BARE_URL = re.compile(r"https?://\S+|www\.\S+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_LIST_MARK = re.compile(r"^\s*(?:[-*+•·▪●]\s+|\d{1,2}[.)]\s+|>\s?)", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*\*|__|\*|~~|(?<!\w)_(?=\w)|(?<=\w)_(?!\w))")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}.*$", re.MULTILINE)
_EMOJI = re.compile(
    "[\U0001f000-\U0001faff\U00002600-\U000027bf\U0001f900-\U0001f9ff"
    "\U0000fe0f\U0000200d]"
)

_SCALE = {"k": "thousand", "m": "million", "b": "billion", "t": "trillion"}
_SCALE_WORDS = {
    "thousand": "thousand",
    "million": "million",
    "mn": "million",
    "billion": "billion",
    "bn": "billion",
    "trillion": "trillion",
    "lakh": "lakh",
    "lakhs": "lakh",
    "crore": "crore",
    "crores": "crore",
    "cr": "crore",
}
_CURRENCY = {
    "$": "dollars",
    "us$": "dollars",
    "₹": "rupees",
    "rs.": "rupees",
    "rs": "rupees",
    "inr": "rupees",
    "€": "euros",
    "£": "pounds",
    "¥": "yen",
}
_MONEY = re.compile(
    r"(?P<cur>US\$|\$|₹|€|£|¥|\bRs\.?\s?|\bINR\s?)"
    r"(?P<num>\d[\d,]*(?:\.\d+)?)"
    r"(?:\s?(?P<scale>[KkMmBbTt](?![a-z])|thousand|million|mn|billion|bn|trillion|"
    r"lakhs?|crores?|cr)\b)?",
)
_WORDS = (
    (re.compile(r"\be\.g\.?(?=\W)", re.I), "for example"),
    (re.compile(r"\bi\.e\.?(?=\W)", re.I), "that is"),
    (re.compile(r"\betc\.?(?=\W|$)", re.I), "and so on"),
    (re.compile(r"\bvs\.?(?=\W)", re.I), "versus"),
    (re.compile(r"\bapprox\.?(?=\W)", re.I), "approximately"),
    (re.compile(r"\bw/o\b", re.I), "without"),
    (re.compile(r"\bw/(?=\s)", re.I), "with"),
    (re.compile(r"\bYoY\b"), "year over year"),
    (re.compile(r"\bQoQ\b"), "quarter over quarter"),
    (re.compile(r"\bP/E\b"), "P E"),
)
_SYMBOLS = (
    (re.compile(r"\s*(?:->|→|=>)\s*"), " to "),
    (re.compile(r"\s+[-–—]\s+|\s*[–—]\s*"), ", "),  # a dash used as a pause
    (re.compile(r"(?<=\d)\s*-\s*(?=\d)"), " to "),  # 10-20 -> 10 to 20
    (re.compile(r"\s*&\s*"), " and "),
    (re.compile(r"(?<=\d)\s*%"), " percent"),
    (re.compile(r"~\s*(?=\d)|≈\s*"), "about "),
    (re.compile(r"(?<![\w.])\+(?=\d)"), "plus "),
    (re.compile(r"(?<=\d)\s*[x×]\s*(?=\d)"), " by "),
    (re.compile(r"(?<=[A-Za-z0-9])/(?=[A-Za-z])"), " "),  # F17/F36, AI/ML
    (re.compile(r"\s*@\s*"), " at "),
    (re.compile(r"[(\[{]\s*"), ", "),
    (re.compile(r"\s*[)\]}]"), ","),
    (re.compile(r"[|#*_^=<>\\\"“”«»•·▪●`]"), " "),
)


def _money(match: re.Match) -> str:
    unit = _CURRENCY.get(match.group("cur").strip().lower(), "")
    number = match.group("num")
    scale = (match.group("scale") or "").strip()
    if scale:
        scale = _SCALE.get(scale.lower(), _SCALE_WORDS.get(scale.lower(), scale))
    words = " ".join(part for part in (number, scale, unit) if part)
    return words


def _sentence(line: str) -> str:
    """A line as a sentence: list items and headings get a full stop."""
    line = line.strip()
    if not line:
        return ""
    if line[-1] not in ".!?…;,:":  # "…are:" leads into the list; keep it
        line += "."
    return line


def to_speech(text: str, *, max_chars: int = 0) -> str:
    """What to say for ``text``. ``max_chars`` > 0 stops after about that many
    characters (at a sentence end) and adds "The rest is on the screen."."""
    text = _CODE_BLOCK.sub(" (code shown on screen) ", text or "")
    text = _INLINE_CODE.sub(r"\1", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _BARE_URL.sub("a link", text)
    text = _TABLE_RULE.sub("", text)
    text = _HEADING.sub("", text)
    text = _LIST_MARK.sub("", text)
    text = _EMPHASIS.sub("", text)
    text = _EMOJI.sub("", text)
    text = _MONEY.sub(_money, text)
    for pattern, words in _WORDS:
        text = pattern.sub(words, text)
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.count("|") >= 2:  # a table row
            line = ", ".join(cell.strip() for cell in line.strip("|").split("|"))
        for pattern, words in _SYMBOLS:
            line = pattern.sub(words, line)
        line = " ".join(line.split())
        line = re.sub(r"\s+([,.;:!?])", r"\1", line)
        line = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", line)
        line = re.sub(r",\s*([.!?])", r"\1", line)
        line = line.strip(" ,;")
        if line:
            lines.append(_sentence(line))
    spoken = " ".join(lines)
    spoken = re.sub(r"\.\s*\.", ".", spoken)
    if max_chars and len(spoken) > max_chars:
        cut = _cut_at_sentence(spoken, max_chars)
        if cut < len(spoken):
            spoken = spoken[:cut].rstrip() + " The rest is on the screen."
    return spoken.strip()


def _cut_at_sentence(text: str, limit: int) -> int:
    ends = [m.end() for m in re.finditer(r"[.!?…](?=\s|$)", text[: limit + 1])]
    return ends[-1] if ends else limit


def split_long(sentence: str, limit: int = 200) -> List[str]:
    """Break a very long sentence at commas/semicolons/'and' so each piece
    stays short enough for the voice model to say in one go."""
    sentence = sentence.strip()
    if len(sentence) <= limit:
        return [sentence] if sentence else []
    parts = re.split(
        r"(?<=[,;:])\s+|\s+(?=(?:and|but|or|while|because|which)\s)", sentence
    )
    pieces: List[str] = []
    current = ""
    for part in parts:
        if not part:
            continue
        candidate = f"{current} {part}".strip()
        if current and len(candidate) > limit:
            pieces.append(current)
            current = part
        else:
            current = candidate
    if current:
        pieces.append(current)
    out: List[str] = []
    for piece in pieces:  # still too long (no commas at all): cut at spaces
        while len(piece) > limit:
            cut = piece.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            out.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if piece:
            out.append(piece)
    return out


__all__ = ["split_long", "to_speech"]
