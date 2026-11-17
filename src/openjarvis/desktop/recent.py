"""What Jarvis just did, so the next command can refer to it.

"Open his chat", "play the next one", "now compare it with Google", "it's
Anand" only make sense against the last contact, video, search or failure.
Every tool call updates this record (``DesktopGraph`` calls ``note``); the
fast path resolves follow-ups against it, and its ``summary`` rides into
the model's context on every request.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

_PATH = re.compile(r"[A-Za-z]:\\[^\n\"']+")
_OPENED = re.compile(r"^Opened (.+?)(?: \((.+)\))?\.?$")
_CHAT = (
    re.compile(r"chat with (.+?)(?: is already open)?\.(?:\s|$)"),
    re.compile(r"^Sent to (.+?): "),
    re.compile(r"^Latest in (.+?):"),
)
# A failure older than this no longer shapes how the next sentence is read.
STALE_SECONDS = 180.0
# Words that must never become a learned name for someone.
_COMMON_WORDS = {
    "and", "the", "chat", "open", "this", "that", "him", "her", "them", "his",
    "she", "they", "you", "your", "mine", "one", "some", "what", "who", "call",
    "send", "message", "whatsapp", "please", "with", "from", "for", "into",
    "onto", "then", "next", "first", "second", "group", "contact", "contacts",
}  # fmt: skip


_TITLED_LINK = re.compile(r"^#{1,4}\s*(.+?)\s*\n\s*Source:\s*(https?://\S+)", re.M)
_BARE_LINK = re.compile(r"Source:\s*(https?://\S+)")


def listing_paths(folder: str, text: str) -> List[str]:
    """Full paths from an fs_list_directory result ("[FILE] a.pdf")."""
    from openjarvis.desktop.files_mcp import listing

    return [path for path, _ in listing(folder, text)]


# fs_* tools whose argument names the file or folder they worked on.
_FILE_ARG = {
    "fs_read_text_file": "path",
    "fs_get_file_info": "path",
    "fs_write_file": "path",
    "fs_edit_file": "path",
    "fs_append_file": "path",
    "fs_create_directory": "path",
    "fs_move_file": "destination",
    "fs_copy_file": "destination",
}


def links_in(text: str) -> List[Tuple[str, str]]:
    """(title, url) pairs from web_search results ("### Title / Source: url")."""
    found = [(t.strip(), u.rstrip(").,")) for t, u in _TITLED_LINK.findall(text or "")]
    if not found:
        found = [("", u.rstrip(").,")) for u in _BARE_LINK.findall(text or "")]
    seen, out = set(), []
    for title, url in found:
        if url not in seen:
            seen.add(url)
            out.append((title, url))
    return out


@dataclass
class Recent:
    app: str = ""
    contact: str = ""
    message: str = ""
    query: str = ""
    site: str = ""
    youtube_query: str = ""
    youtube_results: List[Tuple[str, str]] = field(default_factory=list)
    youtube_index: int = -1
    youtube_where: str = "youtube"  # or "youtube_music" (the app)
    path: str = ""
    # The last folder listing / file search, for "open the second one".
    paths: List[str] = field(default_factory=list)
    # (title, url) of the last web results, for "open the first three".
    links: List[Tuple[str, str]] = field(default_factory=list)
    code_file: str = ""
    error: Optional[Dict[str, Any]] = None
    last: Optional[Dict[str, Any]] = None
    at: float = 0.0

    # -- recording -------------------------------------------------------------

    def note(self, name: str, args: Dict[str, Any], text: str, ok: bool) -> None:
        """Record one tool call (called for fast-path and agent steps alike)."""
        args = dict(args or {})
        action = str(args.get("action", ""))
        now = time.time()
        self.last = {"tool": name, "args": args, "text": text, "ok": ok, "at": now}
        self.at = now
        if not ok:
            self.error = {"tool": name, "args": args, "text": text, "at": now}
            return
        previous_error = self.recent_error()
        self.error = None
        text = text or ""
        if name == "whatsapp" and previous_error is not None:
            self._learn_correction(previous_error, args, text)
        if name == "open":
            match = _OPENED.match(text.strip())
            display = match.group(1) if match else str(args.get("target", ""))
            location = match.group(2) if match and match.group(2) else ""
            if location:
                self.path = location
            elif display:
                self.app = display
        elif name == "window":
            wanted = str(args.get("name", "")).strip()
            if action in ("focus", "maximize", "restore", "minimize") and wanted:
                self.app = wanted
            elif action == "close" and wanted and wanted.lower() in self.app.lower():
                self.app = ""
        elif name == "browser_search":
            self.query = str(args.get("query", "")).strip()
            self.site = str(args.get("site", "") or "google")
            self.links = []  # a new search: older links no longer apply
        elif name == "web_search":
            self.query = str(args.get("query", "")).strip()
            self.site = "web_search"
            self.links = links_in(text)
        elif name in ("fs_list_directory", "fs_list_directory_with_sizes"):
            # "open CC mid1 notes" next means a file from this listing.
            folder = str(args.get("path", ""))
            listed = listing_paths(folder, text)
            if listed:
                self.paths = listed
            self.path = folder or self.path
        elif name == "fs_search_files":
            found = [p.strip() for p in _PATH.findall(text)]
            if found:
                self.paths = found
                self.path = found[0]
        elif name in _FILE_ARG:
            self.path = str(args.get(_FILE_ARG[name], "")) or self.path
        elif name == "recycle":
            self.path = ""
        elif name == "whatsapp":
            for pattern in _CHAT:
                match = pattern.search(text)
                if match:
                    self.contact = match.group(1).strip()
                    break
            if action == "send":
                self.message = str(args.get("message", ""))
        elif name in ("email", "gmail_send", "gmail_draft"):
            if args.get("to"):
                self.contact = str(args["to"])
            if args.get("body"):
                self.message = str(args["body"])
        elif name == "code":
            match = _PATH.search(text)
            if match:
                self.code_file = match.group(0).strip().rstrip(".")

    def _learn_correction(
        self, error: Dict[str, Any], args: Dict[str, Any], text: str
    ) -> None:
        """'Arnold' wasn't found, then 'it's Anand' opened Anand ECE-2: next
        time "Arnold" means Anand ECE-2 (that's how you say it, or how speech
        recognition hears it)."""
        if error.get("tool") != "whatsapp":
            return
        heard = str(error.get("args", {}).get("contact", "")).strip()
        opened = ""
        for pattern in _CHAT:
            match = pattern.search(text)
            if match:
                opened = match.group(1).strip()
                break
        if (
            not heard
            or not opened
            or heard.lower() == str(args.get("contact", "")).lower()
        ):
            return
        if len(heard) < 3 or heard.lower() in _COMMON_WORDS:
            return  # "on", "the": too common to stand for one person
        try:
            from openjarvis.desktop.contacts import directory

            directory().learn(heard, opened)
        except Exception:
            pass

    def played(
        self,
        query: str,
        results: List[Tuple[str, str]],
        index: int,
        *,
        where: str = "youtube",
    ) -> None:
        self.youtube_query = query
        self.youtube_results = list(results)
        self.youtube_index = index
        self.youtube_where = where

    # -- reading -----------------------------------------------------------------

    def recent_error(self) -> Optional[Dict[str, Any]]:
        if self.error and time.time() - self.error.get("at", 0) < STALE_SECONDS:
            return self.error
        return None

    def next_video(self) -> Optional[int]:
        """Index of the next YouTube result after the one playing, or None."""
        if not self.youtube_results:
            return None
        index = self.youtube_index + 1
        return index if index < len(self.youtube_results) else None

    def summary(self) -> str:
        lines = []
        if self.app:
            lines.append(f"Last app opened: {self.app}")
        if self.contact:
            lines.append(f"Last contact (him/her/them): {self.contact}")
        if self.query and self.site != "web_search":
            lines.append(
                f"Last browser search: '{self.query}' on {self.site} ('open the "
                "first results' = open_page with no query)"
            )
        if self.links:
            shown = "; ".join(
                f"{i}. {title or url}"
                for i, (title, url) in enumerate(self.links[:3], 1)
            )
            lines.append(
                f"Last web results for '{self.query}': {shown} ('open the first "
                "N' = open_page count N, no query)"
            )
        if self.youtube_results:
            index = max(self.youtube_index, 0)
            total = len(self.youtube_results)
            title = self.youtube_results[min(index, total - 1)][1]
            if self.youtube_where == "youtube_music":
                line = (
                    f"Playing in the YouTube Music app: {title} (next song / "
                    "pause = system next_track / play_pause)"
                )
            else:
                line = (
                    f"Playing YouTube result {index + 1} of {total} for "
                    f"'{self.youtube_query}': {title}"
                )
                if index + 1 < total:
                    line += " ('the next one' = play_youtube with which=0)"
            lines.append(line)
        if self.path:
            lines.append(f"Last file/folder: {self.path}")
        if self.code_file:
            lines.append(f"Last program written: {self.code_file} (code run runs it)")
        error = self.recent_error()
        if error:
            shown = ", ".join(f"{k}={v!r}" for k, v in error["args"].items() if v)
            lines.append(
                f"Your previous step FAILED: {error['tool']}({shown}) -> "
                f"{error['text'][:160]}. If the user is correcting you (a name, "
                "a path, 'the second one'), retry that step with the correction."
            )
        if not lines:
            return ""
        return "Recent (for follow-ups like 'it', 'him', 'the next one'):\n- " + (
            "\n- ".join(lines)
        )


__all__ = ["Recent", "STALE_SECONDS"]
