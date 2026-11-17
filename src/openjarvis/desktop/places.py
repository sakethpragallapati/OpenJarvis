"""Resolve "open X" targets and find files/folders by name.

``resolve()`` turns a spoken target into something Windows can open: a
website, a known folder ("downloads"), a remembered place, an installed app
or a folder or file found by name ("the Minecraft server folder"). Files
are looked up through the MCP filesystem server (``files_mcp``); this
module only decides which of the names it returns was meant.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional

logger = logging.getLogger(__name__)

# Spoken site names -> address. An installed app with the same name (e.g.
# the Netflix or Spotify app) wins over the website.
KNOWN_SITES = {
    "youtube": "https://www.youtube.com",
    "youtube music": "https://music.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "google drive": "https://drive.google.com",
    "drive": "https://drive.google.com",
    "google maps": "https://maps.google.com",
    "maps": "https://maps.google.com",
    "google docs": "https://docs.google.com",
    "google calendar": "https://calendar.google.com",
    "netflix": "https://www.netflix.com",
    "prime video": "https://www.primevideo.com",
    "amazon prime": "https://www.primevideo.com",
    "hotstar": "https://www.hotstar.com",
    "jiohotstar": "https://www.hotstar.com",
    "amazon": "https://www.amazon.in",
    "flipkart": "https://www.flipkart.com",
    "github": "https://github.com",
    "chatgpt": "https://chatgpt.com",
    "claude": "https://claude.ai",
    "wikipedia": "https://www.wikipedia.org",
    "instagram": "https://www.instagram.com",
    "facebook": "https://www.facebook.com",
    "twitter": "https://x.com",
    "x": "https://x.com",
    "linkedin": "https://www.linkedin.com",
    "reddit": "https://www.reddit.com",
    "whatsapp": "https://web.whatsapp.com",
    "whatsapp web": "https://web.whatsapp.com",
    "spotify": "https://open.spotify.com",
    "outlook": "https://outlook.live.com",
    "stack overflow": "https://stackoverflow.com",
}


# Other words people use for things Windows calls something else.
_SYNONYMS = {
    "file manager": "file explorer",
    "my files": "file explorer",
    "windows explorer": "file explorer",
    "this computer": "this pc",
    "system settings": "settings",
    "control center": "settings",
    "web browser": "browser",
    "the browser": "browser",
    "internet": "browser",
}
_BROWSER_HOME = "https://www.google.com"


def _fuzzy_name(spoken: str) -> Optional[str]:
    """'vs core' -> 'vs code': fix a misheard well-known app/site name."""
    from openjarvis.pc.control import _ALIASES, _WINDOWS_BUILTINS

    known = list(_ALIASES) + list(_WINDOWS_BUILTINS) + list(KNOWN_SITES)
    if spoken in known or len(spoken) < 4:
        return None
    close = difflib.get_close_matches(spoken, known, n=1, cutoff=0.8)
    return close[0] if close else None


def _home() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home())


def _known_folders() -> dict[str, str]:
    home = _home()
    folders = {
        "desktop": "shell:Desktop",
        "downloads": "shell:Downloads",
        "download": "shell:Downloads",
        "documents": "shell:Personal",
        "my documents": "shell:Personal",
        "pictures": "shell:My Pictures",
        "photos": "shell:My Pictures",
        "music": "shell:My Music",
        "videos": "shell:My Video",
        "screenshots": str(home / "Pictures" / "Screenshots"),
        "recycle bin": "shell:RecycleBinFolder",
        "trash": "shell:RecycleBinFolder",
        "this pc": "shell:MyComputerFolder",
        "my computer": "shell:MyComputerFolder",
        "computer": "shell:MyComputerFolder",
        "control panel": "shell:ControlPanelFolder",
        "startup": "shell:Startup",
        "startup folder": "shell:Startup",
        "home": str(home),
        "home folder": str(home),
        "user folder": str(home),
        "appdata": os.environ.get("APPDATA", ""),
        "temp": os.environ.get("TEMP", ""),
        "onedrive": os.environ.get("OneDrive", ""),
        "c drive": "C:\\",
        "d drive": "D:\\",
        "e drive": "E:\\",
    }
    return {k: v for k, v in folders.items() if v}


@dataclass(slots=True)
class Resolution:
    """Something Windows can open, and how sure we are it's what was meant."""

    kind: str  # "website", "folder", "file", "app", "place", "shell"
    display: str
    target: str
    confident: bool = True
    learned: bool = False  # found by searching -> worth remembering

    def describe(self) -> str:
        return f"{self.display} ({self.kind}: {self.target})"


_FILLER = re.compile(
    r"^(?:please\s+|can you\s+|could you\s+|jarvis\s+)*"
    r"(?:open|launch|start|run|show|go to|goto|navigate to|take me to)?\s*"
    r"(?:up\s+)?(?:the\s+|my\s+|a\s+|an\s+)?",
    re.IGNORECASE,
)
_KIND_WORDS = re.compile(
    r"\s+(folder|directory|dir|file|document|app|application|program|"
    r"website|web site|site|page)$",
    re.IGNORECASE,
)
_TAIL = re.compile(r"\s+(?:for me|please|now|in (?:the )?file explorer)$", re.I)
_URLISH = re.compile(
    r"^(?:https?://)?(?:[\w-]+\.)+(?:com|org|net|io|ai|dev|in|co|edu|gov|app|"
    r"me|tv|uk|us|info|xyz|gg|so|ly)(?:[/?#:]\S*)?$",
    re.IGNORECASE,
)


def clean_target(text: str) -> tuple[str, str]:
    """('the Minecraft server folder.') -> ('minecraft server', 'folder')."""
    text = text.strip().strip(".!?,;:\"'").strip()
    text = _FILLER.sub("", text, count=1).strip()
    text = _TAIL.sub("", text).strip()
    kind = ""
    match = _KIND_WORDS.search(text)
    if match:
        word = match.group(1).lower()
        kind = {
            "directory": "folder",
            "dir": "folder",
            "document": "file",
            "application": "app",
            "program": "app",
            "web site": "website",
            "site": "website",
            "page": "website",
        }.get(word, word)
        text = text[: match.start()].strip()
    text = re.sub(r"^(?:the|my)\s+", "", text, flags=re.I)
    return text, kind


# ---------------------------------------------------------------------------
# Searching for files and folders
# ---------------------------------------------------------------------------

_SKIP_PARTS = {
    "appdata",
    "node_modules",
    ".git",
    "site-packages",
    "__pycache__",
    ".venv",
    "venv",
    "$recycle.bin",
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    ".cache",
    "cache",
}


@dataclass(slots=True)
class Found:
    path: str
    is_dir: bool
    score: float


def _words(text: str) -> List[str]:
    """Words of a name, letters and numbers apart: "Mid-1", "mid1" and
    "mid 1" are all ["mid", "1"]."""
    return re.findall(r"[a-z]+|[0-9]+", (text or "").lower())


_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
}  # fmt: skip


def _word_hit(word: str, name_words: List[str]) -> bool:
    """A spoken word starts a word of the name, or (4+ letters) is a near
    spelling of one: speech recognition writes "alamar" for "Alammar".
    "One" also counts as "1"."""
    for form in {word, _NUMBER_WORDS.get(word, word)}:
        if any(nw.startswith(form) for nw in name_words):
            return True
    if len(word) < 4 or word.isdigit():
        return False
    return any(
        len(nw) >= 4 and difflib.SequenceMatcher(None, word, nw).ratio() >= 0.8
        for nw in name_words
    )


def _hits(words: List[str], name_words: List[str]) -> int:
    """How many spoken words the name accounts for. Two or three spoken words
    may spell one word of the name together ("byte byte go" ->
    "Bytebytego", even misheard as "bite bite go")."""
    hit = [_word_hit(w, name_words) for w in words]
    for size in (2, 3):
        for i in range(len(words) - size + 1):
            run = words[i : i + size]
            if any(w.isdigit() for w in run):
                continue  # numbers match exactly ("1notes" is not "notes")
            joined = "".join(run)
            if len(joined) < 5:
                continue
            if any(
                nw.startswith(joined)
                or (
                    len(nw) >= 5
                    and difflib.SequenceMatcher(None, joined, nw).ratio() >= 0.8
                )
                for nw in name_words
            ):
                hit[i : i + size] = [True] * size
    return sum(hit)


def _score(query: str, path: str, is_dir: bool) -> float:
    """How well a path's name matches the spoken words (>= 0.9 = confident).

    Every spoken word must start (or nearly spell) a word of the name
    ("minecraft server" -> "Minecraft Server", "cc mid1 notes" -> "CC Mid-1
    Notes.docx.pdf"); "vs core" does not match "vscore.dll". Numbers must
    match exactly, so "mid 2" is never "Mid-1". Hidden/tool folders and deep
    paths rank lower; things under the user's home rank higher.
    """
    p = Path(path)
    name = p.name.lower()
    stem = name if is_dir else p.stem.lower()
    q = " ".join(query.lower().split())
    words = _words(q)
    name_words = _words(name)
    if not words:
        return 0.0
    coverage = _hits(words, name_words) / len(words)
    ratio = max(
        difflib.SequenceMatcher(None, q, name).ratio(),
        difflib.SequenceMatcher(None, q, stem).ratio(),
    )
    score = 0.5 * ratio + 0.8 * coverage
    if words == name_words or words == _words(stem):
        score += 0.4
    parents = [part.lower() for part in p.parts[:-1]]
    if any(part in _SKIP_PARTS or part.startswith(".") for part in parents):
        score -= 0.6
    try:
        if p.is_relative_to(_home()):
            score += 0.1
    except (TypeError, ValueError):
        pass
    score -= 0.01 * len(p.parts)  # prefer shallower matches
    if coverage < 1:
        score = min(score, 0.85)  # never confident unless every word matched
    return score


_SCOPE = re.compile(
    r"\s*,?\s+(?:in|on|from|inside|at|under)\s+(?:my\s+|the\s+)?"
    r"(desktop|documents|downloads|pictures|music|videos)(?:\s+folder)?\W*$",
    re.IGNORECASE,
)
_NUMBER_NAMES = {d: w for w, d in _NUMBER_WORDS.items()}


def scope_folder(target: str) -> tuple[str, Optional[Path]]:
    """'CC mid 1 notes in desktop' -> ('CC mid 1 notes', ~/Desktop)."""
    match = _SCOPE.search(target or "")
    if not match:
        return target, None
    name = match.group(1).capitalize()
    for base in (_home(), _home() / "OneDrive"):
        if (base / name).is_dir():
            return target[: match.start()].strip(" ,"), base / name
    return target[: match.start()].strip(" ,"), None


def _spoken_words(text: str) -> List[str]:
    """Name words with extensions dropped and numbers as words ("1" -> "one")."""
    stem = re.sub(r"(\.[a-z0-9]{1,5})+$", "", (text or "").lower())
    return [_NUMBER_NAMES.get(w, w) for w in _words(stem)]


def sound_score(spoken: str, name: str) -> float:
    """How alike a misheard name sounds and reads to a file name (0-1).

    Speech recognition turns "CC Mid-1 notes" into "CC mid-1 codes" or "CC
    Midland Hodes": compared by spelling *and* by consonant sound, over the
    stretch of the name that lines up best with what was said.
    """
    from openjarvis.desktop.contacts import phonetic

    said, words = _spoken_words(spoken), _spoken_words(os.path.basename(name))
    if not said or not words:
        return 0.0

    def sound(ws: List[str]) -> str:
        return "".join(phonetic(w) or w for w in ws)

    best = 0.0
    for size in range(max(1, len(said) - 1), len(said) + 2):
        for start in range(0, max(1, len(words) - size + 1)):
            part = words[start : start + size]
            spelled = difflib.SequenceMatcher(
                None, "".join(said), "".join(part)
            ).ratio()
            sounded = difflib.SequenceMatcher(None, sound(said), sound(part)).ratio()
            best = max(best, 0.5 * spelled + 0.5 * sounded)
    return best


def sound_pick(spoken: str, paths: List[str]) -> tuple[Optional[str], List[str]]:
    """(the path ``spoken`` most likely means, or None; the close candidates
    to ask about when it isn't clear)."""
    ranked = sorted(((sound_score(spoken, p), p) for p in paths), reverse=True)
    if not ranked or ranked[0][0] < 0.55:
        return None, []
    top = ranked[0][0]
    close = [p for s, p in ranked[:3] if s >= top - 0.08]
    if top >= 0.72 and len(close) == 1:
        return ranked[0][1], []
    return None, close


def best_listed(query: str, paths: List[str]) -> Optional[str]:
    """The one path among ``paths`` (e.g. the folder Jarvis just listed) that
    ``query`` clearly names, or None."""
    scored = sorted(
        ((_score(query, p, os.path.isdir(p)), p) for p in paths), reverse=True
    )
    if not scored or scored[0][0] < 0.9:
        return None
    if len(scored) > 1 and scored[1][0] >= scored[0][0] - 0.05:
        return None  # two equally good: let the normal search decide / ask
    return scored[0][1]


# Where files are looked up: the filesystem MCP server (files_mcp.FileStore),
# set when the desktop tool server is built.
_FILES: Optional[Any] = None


def use_files(store: Optional[Any]) -> None:
    """Look files up through ``store`` (a ``files_mcp.FileStore``)."""
    global _FILES
    _FILES = store


def files() -> Optional[Any]:
    return _FILES


def search(query: str, kind: str = "any", limit: int = 8) -> List[Found]:
    """Find folders/files whose name matches ``query``, best first (through
    the filesystem MCP server; nothing when it isn't running)."""
    if _FILES is None:
        return []
    try:
        return list(_FILES.search(query, kind, limit))
    except Exception as exc:
        logger.debug("File search failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Resolving "open X"
# ---------------------------------------------------------------------------


def _expand(text: str) -> Optional[Path]:
    raw = os.path.expandvars(os.path.expanduser(text.strip().strip('"')))
    if re.match(r"^[a-zA-Z]:[\\/]|^\\\\", raw):
        path = Path(raw)
        return path if path.exists() else None
    return None


def resolve(
    text: str,
    kind: str = "auto",
    *,
    recall_place: Optional[Callable[[str], Optional[str]]] = None,
    find_app: Optional[Callable[[str], Optional[tuple[str, str]]]] = None,
    search_fn: Optional[Callable[..., List[Found]]] = None,
) -> Optional[Resolution]:
    """Best guess at what ``text`` refers to, or None."""
    target, spoken_kind = clean_target(text)
    kind = (kind or "auto").lower()
    if kind == "auto":
        kind = spoken_kind or "auto"
    if not target:
        return None
    lowered = _SYNONYMS.get(target.lower(), target.lower())
    if lowered == "browser" or (lowered in ("chrome", "edge") and kind == "website"):
        return Resolution("website", "the browser", _BROWSER_HOME)
    corrected = _fuzzy_name(lowered)
    if corrected:
        lowered = corrected
    if lowered != target.lower():
        target = lowered

    path = _expand(target)
    if path is not None:
        return Resolution(
            "folder" if path.is_dir() else "file", path.name or str(path), str(path)
        )

    if kind in ("auto", "website") and (
        lowered.startswith(("http://", "https://")) or _URLISH.match(lowered)
    ):
        url = lowered if lowered.startswith("http") else f"https://{target}"
        return Resolution("website", target, url)

    if kind in ("auto", "folder"):
        known = _known_folders().get(lowered)
        if known:
            return Resolution("shell", target.title(), known)

    if recall_place is not None and kind in ("auto", "folder", "file"):
        remembered = recall_place(lowered)
        if remembered and (
            remembered.startswith("shell:") or Path(remembered).exists()
        ):
            return Resolution("place", target, remembered)

    if kind in ("auto", "app"):
        if find_app is None:
            from openjarvis.pc.control import find_app as default_find_app

            find_app = default_find_app
        app = find_app(target)
        if app is not None:
            display, launch = app
            exact = display.lower() == lowered or display.lower().startswith(lowered)
            site = KNOWN_SITES.get(lowered)
            if site and not exact:
                return Resolution("website", target.title(), site)
            return Resolution(
                "app", display, launch, confident=exact or len(lowered) > 3
            )

    if kind in ("auto", "website"):
        site = KNOWN_SITES.get(lowered) or KNOWN_SITES.get(
            re.sub(r"\.(?:com|in|org|net)$", "", lowered)
        )
        if site:
            return Resolution("website", target.title(), site)
        if kind == "website":
            return None

    if kind in ("auto", "folder", "file"):
        search_fn = search_fn or search
        want = "folder" if kind == "folder" else "file" if kind == "file" else "any"
        hits = search_fn(target, want, 5)
        if hits:
            best = hits[0]
            confident = best.score >= 0.9
            return Resolution(
                "folder" if best.is_dir else "file",
                Path(best.path).name,
                best.path,
                confident=confident,
                learned=True,
            )
    return None


__all__ = [
    "Found",
    "KNOWN_SITES",
    "Resolution",
    "best_listed",
    "clean_target",
    "files",
    "resolve",
    "scope_folder",
    "search",
    "sound_pick",
    "use_files",
]
