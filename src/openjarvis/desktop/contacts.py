"""Who the user means: spoken names -> the exact WhatsApp / email names.

Three sources, in order:

1. ``~/.openjarvis/contacts.txt`` - the user's own list, one per line:
   ``Trisha = Thrisha ECE2`` (how it's said = the exact chat name), or just
   a name Jarvis should know.
2. Aliases Jarvis learned: whenever a fuzzy or corrected lookup opened a
   chat, the spoken form is remembered for next time.
3. Every chat name Jarvis has ever seen in the WhatsApp list.

Matching is forgiving: prefixes ("Anand" -> "Anand ECE-2"), sound-alikes
("Trisha" -> "Thrisha ECE2") and spelled-out names ("T-H-R-I-S-H-A").
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

HEADER = """# Jarvis contacts: the people Jarvis can find on WhatsApp and email.
#
# Names are matched by sound and spelling, so you don't list how you say
# them: "Trisha" finds "Thrisha ECE-2", "Anand" finds "Anand ECE-2", "Dad"
# finds "Daddy". Under [nicknames], add only what you call someone when it
# sounds nothing like their saved name:
#     Mom = Amma
#     Bro = Sai Teja (Tammudu)
# Say "open my contacts list" to edit this file.
"""
_NO_IMPORT_NOTE = (
    "Your phone contacts go here: jarvis desktop contacts import contacts.csv"
)
# Single letters separated by spaces, dots or dashes ("T-H-R-I-S-H-A"); a
# letter right after an apostrophe ("it's") is not one of them.
_SPELLED = re.compile(r"(?<![\w'])(?:[A-Za-z][\s.\-]+){2,}[A-Za-z](?!\w)")
_SPELLED_WORD = re.compile(r"^(?:[A-Za-z][\s.\-]+)+[A-Za-z]$")


def despell(text: str) -> str:
    """'T-H-R-I-S-H-A ECE2' / 'A N A N D' -> 'Thrisha ECE2' / 'Anand'."""

    def join(match: re.Match) -> str:
        word = re.sub(r"[\s.\-]", "", match.group(0))
        return word.capitalize() if word.isupper() or len(word) > 3 else word

    return _SPELLED.sub(join, text or "").strip()


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower()).split())


_SOUNDS = (
    ("ph", "f"),
    ("th", "t"),
    ("sh", "s"),
    ("ch", "c"),
    ("ck", "k"),
    ("gh", "g"),
    ("wh", "w"),
    ("kn", "n"),
    ("wr", "r"),
    ("qu", "k"),
    ("x", "ks"),
    ("z", "s"),
    ("v", "w"),
    ("c", "k"),
    ("q", "k"),
    ("y", "i"),
    ("j", "g"),
)


def phonetic(word: str) -> str:
    """A rough sound key: 'Trisha' and 'Thrisha' -> 'trs', 'Saketh'/'Saket' -> 'skt'."""
    text = re.sub(r"[^a-z]", "", (word or "").lower())
    if not text:
        return ""
    for sound, simple in _SOUNDS:
        text = text.replace(sound, simple)
    key = text[0] + re.sub(r"[aeiouh]", "", text[1:])
    return re.sub(r"(.)\1+", r"\1", key)


def contacts_file() -> Path:
    from openjarvis.core.paths import get_config_dir

    return get_config_dir() / "contacts.txt"


def _cache_file() -> Path:
    from openjarvis.core.paths import get_config_dir

    return get_config_dir() / "desktop" / "contacts.json"


_IMPORT_START = "# --- imported from"
_IMPORT_END = "# --- end of import ---"
_NAME_COLUMNS = ("First Name", "Middle Name", "Last Name")


def read_contact_names(source: Path) -> Tuple[List[str], Dict[str, str]]:
    """``(names, nicknames)`` from a Google/Outlook contacts CSV or a vCard.

    Only names are read (as the phone - and so WhatsApp - shows them:
    first, middle and last joined); phone numbers and the rest stay put.
    """
    names, nicknames, _starred = read_contact_details(source)
    return names, nicknames


def read_contact_details(
    source: Path,
) -> Tuple[List[str], Dict[str, str], List[str]]:
    """``(names, nicknames, starred names)`` from a contacts export."""
    import csv

    source = Path(source)
    text = source.read_text(encoding="utf-8-sig", errors="replace")
    names: List[str] = []
    nicknames: Dict[str, str] = {}
    starred: List[str] = []
    if source.suffix.lower() in (".vcf", ".vcard") or text.lstrip().startswith(
        "BEGIN:VCARD"
    ):
        for line in text.splitlines():
            key, _, value = line.partition(":")
            field = key.split(";")[0].upper()
            if field == "FN" and value.strip():
                names.append(value.strip().replace("\\,", ","))
        return names, nicknames, starred
    reader = csv.DictReader(text.splitlines())
    columns = reader.fieldnames or []
    single = next(
        (c for c in ("Name", "Display Name", "Full Name") if c in columns), ""
    )
    for row in reader:
        parts = [(row.get(c) or "").strip() for c in _NAME_COLUMNS]
        name = " ".join(p for p in parts if p) or (row.get(single) or "").strip()
        if not name:
            continue
        names.append(name)
        nickname = (row.get("Nickname") or "").strip()
        if nickname:
            nicknames[nickname] = name
        if "starred" in (row.get("Labels") or "").lower():
            starred.append(" ".join(name.split()))
    return names, nicknames, starred


# Tags people add after a saved name ("Anand ECE-2", "Krishna IT-1 Jr",
# "Keerthi SRT Deloitte"): not part of how the name is said.
_ACRONYM = re.compile(r"^[A-Z]{2,}[\w-]*$")  # ECE-2, AIDS, SRT, IOT (case matters)
_TAG = re.compile(
    r"^(?:\w*\d[\w-]*|-\d+|jr|sr|\(.*|mam|sir|uncle|aunty|anna|akka|mavayya|"
    r"atta|deloitte|mentor|friend)$",
    re.IGNORECASE,
)


def spoken_name(name: str) -> str:
    """How a saved name is said: 'Anand ECE-2' -> 'Anand', 'P Prathik ECE-2'
    -> 'Prathik', 'Chinna Ammamma' -> 'Chinna Ammamma'."""
    name = re.sub(
        r"^[A-Za-z]\.\s*(?=[A-Za-z]{2})", "", (name or "").strip()
    )  # M.Neeraj
    words = [w for w in re.split(r"\s+", name) if w]
    kept: List[str] = []
    for word in words:
        if (_TAG.match(word) or _ACRONYM.match(word)) and kept:
            break
        if len(word.strip(".")) == 1 and not kept:
            continue  # "P Prathik", "M Srikar"
        kept.append(word.strip(".,"))
        if len(kept) == 2:
            break
    return " ".join(w for w in kept if re.search(r"[A-Za-z]", w))


_SECTION = re.compile(r"^\[(.+)\]$")


@dataclass
class ContactsFile:
    """contacts.txt, by section."""

    nicknames: List[Tuple[str, str]] = field(default_factory=list)
    contacts: List[str] = field(default_factory=list)  # imported from the phone
    other: List[str] = field(default_factory=list)  # names typed in elsewhere
    note: str = ""  # the comment under [contacts] (where they came from)


def read_sections(text: str) -> ContactsFile:
    """Split contacts.txt into nicknames, imported contacts and other names.

    Also reads the earlier layout (an "imported from" comment block).
    """
    book = ContactsFile()
    section = ""
    old_import = False
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith(_IMPORT_START):
            old_import = True
            continue
        if line == _IMPORT_END:
            old_import = False
            continue
        header = _SECTION.match(line)
        if header:
            section = header.group(1).strip().lower()
            continue
        if not line:
            continue
        if line.startswith("#"):
            if section == "contacts" and not book.note and not book.contacts:
                book.note = line.lstrip("# ").strip()
            continue
        if "=" in line:
            spoken, _, name = line.partition("=")
            if spoken.strip() and name.strip():
                book.nicknames.append((spoken.strip(), name.strip()))
        elif section == "contacts" or old_import:
            book.contacts.append(line)
        else:
            book.other.append(line)
    return book


def render(book: ContactsFile) -> str:
    """contacts.txt text for ``book`` (header, [nicknames], [contacts])."""
    lines = [HEADER.rstrip("\n"), "", "[nicknames]"]
    if book.nicknames:
        width = max(len(spoken) for spoken, _ in book.nicknames)
        lines += [f"{spoken.ljust(width)} = {name}" for spoken, name in book.nicknames]
    else:
        lines.append("# (none yet)")
    if book.other:
        lines += ["", "[other names]", *book.other]
    note = book.note or (
        f"{len(book.contacts)} names. Importing your phone contacts again "
        "replaces this list."
        if book.contacts
        else _NO_IMPORT_NOTE
    )
    lines += ["", "[contacts]", f"# {note}", *book.contacts]
    return "\n".join(lines) + "\n"


def parse_contacts(text: str) -> Tuple[Dict[str, str], List[str]]:
    """``(aliases, names)`` from the contacts file's text."""
    aliases: Dict[str, str] = {}
    names: List[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or _SECTION.match(line):
            continue
        if "=" in line:
            spoken, _, name = line.partition("=")
            spoken, name = spoken.strip(), name.strip()
            if spoken and name:
                aliases[_norm(spoken)] = name
                names.append(name)
        else:
            names.append(line)
    return aliases, names


class Directory:
    """The known names and how the user says them."""

    def __init__(
        self, path: Optional[Path] = None, cache: Optional[Path] = None
    ) -> None:
        self.path = path or contacts_file()
        self.cache = cache or _cache_file()
        self._lock = threading.Lock()
        self._mtime: Optional[float] = None
        self.file_aliases: Dict[str, str] = {}
        self.file_names: List[str] = []
        self.learned: Dict[str, str] = {}
        self.seen: Dict[str, float] = {}
        self._load_cache()
        self.refresh()

    # -- storage -----------------------------------------------------------------

    def refresh(self) -> None:
        """Re-read contacts.txt if it changed (and create it the first time)."""
        try:
            if not self.path.exists():
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(render(ContactsFile()), encoding="utf-8")
            mtime = self.path.stat().st_mtime
        except OSError:
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            text = self.path.read_text(encoding="utf-8-sig")
        except OSError:
            return
        self.file_aliases, self.file_names = parse_contacts(text)

    def _load_cache(self) -> None:
        self.starred: List[str] = []
        try:
            data = json.loads(self.cache.read_text(encoding="utf-8"))
            self.learned = {
                str(k): str(v) for k, v in (data.get("aliases") or {}).items()
            }
            self.seen = {str(k): float(v) for k, v in (data.get("seen") or {}).items()}
            self.starred = [str(n) for n in data.get("starred") or []]
        except (OSError, ValueError, AttributeError):
            self.learned, self.seen = {}, {}

    def _save_cache(self) -> None:
        try:
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache.with_suffix(".tmp")
            data = {"aliases": self.learned, "seen": self.seen, "starred": self.starred}
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, self.cache)
        except OSError as exc:
            logger.debug("Could not save contacts cache: %s", exc)

    def set_starred(self, names: Iterable[str]) -> None:
        with self._lock:
            self.starred = list(dict.fromkeys(" ".join(n.split()) for n in names))
            self._save_cache()

    def hint_names(self, limit_chars: int = 450) -> List[str]:
        """Names to prime speech recognition with, most useful first:
        nicknames, starred contacts, chats seen in WhatsApp, then the rest.
        Said forms only ("Anand", not "Anand ECE-2")."""
        self.refresh()
        contacts = set(self.file_names)
        ordered: List[str] = [k.title() for k in self.file_aliases]
        ordered += [spoken_name(n) for n in self.starred]
        ordered += [
            spoken_name(n)
            for n in sorted(self.seen, key=self.seen.get, reverse=True)
            if n in contacts
        ]
        ordered += [spoken_name(n) for n in sorted(contacts, key=str.lower)]
        hints: List[str] = []
        seen_lower: set = set()
        used = 0
        for name in ordered:
            key = name.lower()
            if not name or key in seen_lower or len(name) < 3:
                continue
            if used + len(name) + 2 > limit_chars:
                break
            seen_lower.add(key)
            hints.append(name)
            used += len(name) + 2
        return hints

    # -- knowledge -----------------------------------------------------------------

    def names(self) -> List[str]:
        self.refresh()
        ordered: Dict[str, None] = {}
        for name in (*self.file_names, *self.learned.values(), *self.seen):
            if name:
                ordered.setdefault(name, None)
        return list(ordered)

    def alias(self, spoken: str) -> Optional[str]:
        self.refresh()
        key = _norm(despell(spoken))
        if key in self.file_aliases:
            return self.file_aliases[key]  # your own [nicknames] lines win
        if key in {_norm(n) for n in self.file_names}:
            return None  # a saved contact's own name is never someone else
        return self.learned.get(key)

    def saw(self, names: Iterable[str]) -> None:
        """Remember chat names seen in the list (so they can be matched later)."""
        now = time.time()
        changed = False
        with self._lock:
            for name in names:
                name = " ".join((name or "").split())
                if name and name not in self.seen:
                    self.seen[name] = now
                    changed = True
            if len(self.seen) > 2000:
                for name in sorted(self.seen, key=self.seen.get)[:-1500]:
                    del self.seen[name]
            if changed:
                self._save_cache()

    def learn(self, spoken: str, name: str) -> None:
        """'Arnold' turned out to be 'Anand ECE-2': next time go straight there.

        Nothing is stored when the name already matches by sound or spelling
        ("Trisha" -> "Thrisha ECE-2" needs no memory).
        """
        key = _norm(despell(spoken))
        name = " ".join((name or "").split())
        if not key or not name or key == _norm(name):
            return
        if key in {_norm(n) for n in self.file_names}:
            return  # "Sarika ECE-2" is a different person from "Siri ECE-2"
        if _norm(self.resolve(spoken) or "") == _norm(name):
            return
        with self._lock:
            if self.learned.get(key) == name:
                return
            self.learned[key] = name
            self.seen.setdefault(name, time.time())
            self._save_cache()

    def forget(self, spoken: str) -> bool:
        key = _norm(despell(spoken))
        with self._lock:
            if key in self.learned:
                del self.learned[key]
                self._save_cache()
                return True
        return False

    # -- matching --------------------------------------------------------------------

    def candidates(self, spoken: str, limit: int = 3) -> List[Tuple[str, float]]:
        """Known names that could be ``spoken``, best first, with a 0-1 score."""
        wanted = _norm(despell(spoken))
        if not wanted:
            return []
        scored: Dict[str, float] = {}
        alias = self.alias(spoken)
        if alias:
            scored[alias] = 1.0
        for name in self.names():
            score = score_name(wanted, name)
            if score >= 0.6:
                scored[name] = max(scored.get(name, 0.0), score)
        # Ties in alphabetical order, so "the second one" is predictable.
        ranked = sorted(scored.items(), key=lambda item: (-item[1], item[0].lower()))
        return ranked[:limit]

    def resolve(self, spoken: str) -> Optional[str]:
        """The one name clearly meant by ``spoken``, or None."""
        ranked = self.candidates(spoken, 5)
        if not ranked or ranked[0][1] < 0.8:
            return None
        tied = [name for name, score in ranked if score >= ranked[0][1] - 0.02]
        if len(tied) > 1:
            # "Amma" vs "Amma (Office Number)": the plain name is meant.
            base = min(tied, key=len)
            if all(_norm(name).startswith(_norm(base)) for name in tied):
                return base
            return None  # a real tie: let the caller ask
        return ranked[0][0]

    def ambiguous(self, spoken: str, limit: int = 4) -> List[str]:
        """Names ``spoken`` fits equally well ("Krishna" -> three Krishnas).

        Empty when one name is clearly meant, including an exact match
        ("Amma" over "Amma (office phone number)").
        """
        ranked = self.candidates(spoken, limit + 1)
        if len(ranked) < 2 or ranked[0][1] >= 1.0 or ranked[0][1] < 0.9:
            return []
        top = ranked[0][1]
        tied = [name for name, score in ranked if score >= top - 0.02]
        return tied[:limit] if len(tied) > 1 else []

    # -- editing contacts.txt ------------------------------------------------------

    def _read_file(self) -> ContactsFile:
        self.refresh()
        try:
            return read_sections(self.path.read_text(encoding="utf-8-sig"))
        except OSError:
            return ContactsFile()

    def _write_file(self, book: ContactsFile) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(render(book), encoding="utf-8")
        self._mtime = None
        self.refresh()

    def import_names(
        self,
        names: Iterable[str],
        source: str,
        nicknames: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, int]:
        """Replace the [contacts] section with ``names`` (nicknames and other
        names are kept). Returns ``(names, nicknames added)``."""
        unique: Dict[str, str] = {}
        for name in names:
            name = " ".join((name or "").split())
            if name and re.search(r"\w", name):
                unique.setdefault(_norm(name) or name.lower(), name)
        book = self._read_file()
        book.contacts = sorted(unique.values(), key=str.lower)
        book.other = [n for n in book.other if _norm(n) not in unique]
        book.note = (
            f"{len(book.contacts)} names from {source}, imported "
            f"{time.strftime('%d %b %Y')}. Importing again replaces this list."
        )
        have = {_norm(spoken) for spoken, _ in book.nicknames}
        added = 0
        for spoken, name in (nicknames or {}).items():
            if _norm(spoken) not in have:
                book.nicknames.append((spoken, name))
                have.add(_norm(spoken))
                added += 1
        self._write_file(book)
        return len(book.contacts), added

    def add_nickname(self, spoken: str, name: str) -> None:
        """'Mom = Amma' into [nicknames] (replacing an earlier 'Mom')."""
        spoken, name = " ".join(spoken.split()), " ".join(name.split())
        if not spoken or not name:
            raise ValueError("Need both what you say and the saved name.")
        book = self._read_file()
        key = _norm(spoken)
        book.nicknames = [(s, n) for s, n in book.nicknames if _norm(s) != key]
        book.nicknames.append((spoken, name))
        self._write_file(book)

    def tidy(self) -> ContactsFile:
        """Rewrite contacts.txt in the current layout (same contents)."""
        book = self._read_file()
        self._write_file(book)
        return book


def score_name(wanted: str, name: str) -> float:
    """How well the spoken ``wanted`` (normalised) matches a known ``name``."""
    known = _norm(name)
    if not known or not wanted:
        return 0.0
    if known == wanted:
        return 1.0
    if known.startswith(wanted + " "):
        return 0.95
    known_words = known.split()
    wanted_words = wanted.split()
    if all(w in known_words for w in wanted_words):
        return 0.9
    score = 0.0
    first_wanted, first_known = wanted_words[0], known_words[0]
    ratio = difflib.SequenceMatcher(None, first_wanted, first_known).ratio()
    if ratio >= 0.6 and phonetic(first_wanted) == phonetic(first_known):
        score = 0.85
    if min(len(first_wanted), len(first_known)) >= 4 and (
        first_wanted[:-1] == first_known or first_known[:-1] == first_wanted
    ):
        score = max(score, 0.88)  # a letter more/less at the end: "Amman" = Amma
    if ratio >= 0.75:
        score = max(score, ratio * 0.9)
    # Compare only the words that differ: a shared tag ("ECE-2", "Chemical")
    # made "Thrisha ECE-2" look like "Harsha ECE-2" and "Sarika" like "Siri".
    wanted_rest = " ".join(w for w in wanted_words if w not in known_words)
    known_rest = " ".join(w for w in known_words if w not in wanted_words)
    full = difflib.SequenceMatcher(None, wanted_rest, known_rest).ratio()
    if full >= 0.75:
        score = max(score, full * 0.9)
    return round(score, 3)


_DIRECTORY: Optional[Directory] = None
_DIRECTORY_LOCK = threading.Lock()


def directory() -> Directory:
    """The process-wide directory (contacts.txt + what Jarvis learned)."""
    global _DIRECTORY
    with _DIRECTORY_LOCK:
        if _DIRECTORY is None:
            _DIRECTORY = Directory()
        return _DIRECTORY


def import_file(source: Path, book: Optional[Directory] = None) -> Tuple[int, int]:
    """Import names (and nicknames as aliases) from a contacts export.

    Returns ``(names imported, nicknames added)``.
    """
    book = book or directory()
    names, nicknames, starred = read_contact_details(Path(source))
    if starred:
        book.set_starred(starred)  # favourites first in speech hints
    return book.import_names(names, Path(source).name, nicknames)


# "Ammatak" / "Anandchat": Whisper runs "<name> chat" into one word.
_FUSED_CHAT = re.compile(
    r"^(?P<stem>[A-Za-z]{3,}?)(?:chat|chaat|chet|chut|tak|thak|tuck|jat|jar|char|"
    r"chart|chad|chats)$",
    re.IGNORECASE,
)


def unfuse_chat(spoken: str) -> str:
    """'Ammatak' -> 'Amma' (else '')."""
    words = " ".join((spoken or "").split()).split(" ")
    match = _FUSED_CHAT.match(words[-1]) if words and words[-1] else None
    if not match:
        return ""
    return " ".join([*words[:-1], match.group("stem")]).strip()


# "Anand.jar" / "Anand.chat" (how Whisper writes "Anand chat").
_DOTTED_CHAT = re.compile(r"\b([A-Za-z][\w'-]*)\.(?:jar|chat|char|chart|jat)\b")
# Contexts where a garbled phrase is probably a person's name.
_PERSON_CONTEXT = re.compile(
    r"\b(?:whats\s?app|chat|message|text|call|send|tell|ask|reply|ping)\b", re.I
)


def repair_names(text: str, book: Optional["Directory"] = None) -> str:
    """Fix how speech recognition mangles contact names in commands.

    "open Anand.jar" -> "open Anand chat"; "open on and in WhatsApp" ->
    "open Anand in WhatsApp" (a name split into short words). Only in
    sentences about chats, calls or messages, and only to a known contact.
    """
    if not text:
        return text
    try:
        book = book or directory()
        said = {}
        for name in book.names():
            short = spoken_name(name)
            if len(short) >= 4:
                said.setdefault(_squash_letters(short), short)
    except Exception:
        return text

    def dotted(match: re.Match) -> str:
        word = match.group(1)
        if _squash_letters(word) in said or book.resolve(word):
            return f"{word} chat"
        return match.group(0)

    text = _DOTTED_CHAT.sub(dotted, text)
    if not _PERSON_CONTEXT.search(text):
        return text
    words = text.split()
    out: List[str] = []
    i = 0
    while i < len(words):
        joined = None
        for span in (3, 2):
            window = words[i : i + span]
            if len(window) < span or any(len(w.strip(",.")) > 4 for w in window):
                continue
            squashed = _squash_letters("".join(window))
            match = _closest_said(squashed, said)
            if match:
                joined = (span, match)
                break
        if joined:
            out.append(joined[1])
            i += joined[0]
        else:
            out.append(words[i])
            i += 1
    return " ".join(out)


def _squash_letters(text: str) -> str:
    return re.sub(r"[^a-z]", "", (text or "").lower())


def _closest_said(squashed: str, said: Dict[str, str]) -> Optional[str]:
    """The contact whose said name ``squashed`` (split words joined) is."""
    if len(squashed) < 4 or squashed in _NOT_SPLIT_NAMES:
        return None
    if squashed in said:
        return said[squashed]
    best, best_ratio = None, 0.0
    for key, name in said.items():
        if abs(len(key) - len(squashed)) > 1 or key[-2:] != squashed[-2:]:
            continue
        ratio = difflib.SequenceMatcher(None, squashed, key).ratio()
        if ratio > best_ratio:
            best, best_ratio = name, ratio
    return best if best_ratio >= 0.8 else None


# Ordinary word pairs that must never be read as a split name.
_NOT_SPLIT_NAMES = {
    "andthe", "onthe", "inthe", "tothe", "isthe", "itis", "ofthe", "forthe",
    "withhim", "withher", "tohim", "toher", "onit", "init", "andi", "andme",
    "callme", "textme", "tellme", "askhim", "askher",
}  # fmt: skip


__all__ = [
    "Directory",
    "HEADER",
    "contacts_file",
    "despell",
    "directory",
    "import_file",
    "parse_contacts",
    "read_sections",
    "render",
    "phonetic",
    "read_contact_names",
    "score_name",
]
