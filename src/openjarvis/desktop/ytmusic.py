"""Your own YouTube Music playlists, played from the app's side panel.

``play_youtube`` searches YouTube, which finds public mixes: "play my anime
music playlist" once played "ANIME SONGS MIX" instead of the user's own
"Anime music" playlist. The YouTube Music app (installed from Brave/Chrome)
lists your playlists in its side panel, each with its own Play button, and
UI Automation can press that button without moving the mouse.
"""

from __future__ import annotations

import difflib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, List, Optional

from openjarvis.desktop.winapi import COM, DesktopError

logger = logging.getLogger(__name__)

APP_NAME = "YouTube Music"
_BROWSERS = ("brave", "chrome", "msedge")

# "my anime music playlist", "my liked songs": clearly one of yours.
_OWN = re.compile(
    r"\bmy\b.*\b(?:playlists?|liked(?: songs| music)?|favou?rites?)\b"
    r"|\b(?:playlists?|liked songs|liked music)\b.*\bof mine\b",
    re.IGNORECASE,
)
_MY = re.compile(r"\bmy\b", re.IGNORECASE)
_FILLER = re.compile(
    r"\b(?:my|the|playlists?|in|on|from|youtube music|youtube|app|called|named|"
    r"of mine|please)\b",
    re.IGNORECASE,
)
_LIKED = re.compile(
    r"^(?:liked|liked songs|liked music|favou?rites?|favou?rite songs)$"
)


class NoSuchPlaylist(DesktopError):
    """None of your playlists has that name."""


@dataclass(slots=True)
class Playlist:
    title: str
    owner: str = ""


def asks_for_own(query: str) -> bool:
    """ "my anime music playlist" / "my liked songs" (not "play my heart will
    go on", which is a song)."""
    return bool(_OWN.search(query or ""))


def mentions_my(query: str) -> bool:
    return bool(_MY.search(query or ""))


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower()).split())


def wanted_name(query: str) -> str:
    """'my anime music playlist' -> 'anime music'."""
    return " ".join(_FILLER.sub(" ", _norm(query)).split())


def match(playlists: List[Playlist], query: str) -> Optional[Playlist]:
    """The playlist ``query`` names, or None."""
    wanted = wanted_name(query)
    if not wanted or not playlists:
        return None
    titles = [_norm(p.title) for p in playlists]
    if _LIKED.match(wanted):
        wanted = "liked music"
    for rule in (
        lambda t: t == wanted,
        lambda t: t.startswith(wanted + " ") or wanted.startswith(t + " "),
        lambda t: all(w in t.split() for w in wanted.split()),
        lambda t: all(w in wanted.split() for w in t.split()),
    ):
        hits = [p for p, t in zip(playlists, titles) if t and rule(t)]
        if len(hits) == 1:
            return hits[0]
        if hits:
            return min(hits, key=lambda p: len(p.title))  # the closest fit
    scored = sorted(
        (
            (difflib.SequenceMatcher(None, wanted, t).ratio(), i)
            for i, t in enumerate(titles)
        ),
        reverse=True,
    )
    if scored and scored[0][0] >= 0.8:
        if len(scored) == 1 or scored[1][0] < scored[0][0] - 0.05:
            return playlists[scored[0][1]]
    return None


# ---------------------------------------------------------------------------
# The app window (UI Automation on the COM thread)
# ---------------------------------------------------------------------------


def find_window() -> Any:
    from openjarvis.desktop.winapi import list_windows

    for window in list_windows():
        if window.title.lower().endswith(APP_NAME.lower()) and (
            window.process.lower().startswith(_BROWSERS)
        ):
            return window
    return None


_DESCENDANTS = 4  # TreeScope_Descendants


def _find(root: Any, kind: str, name: Optional[str] = None) -> List[Any]:
    """Every ``kind`` control (e.g. "HyperlinkControl") under ``root``.

    Asks Brave to search its own tree in one call: walking a page with a
    long playlist open element by element took 14 seconds, this takes ~1.
    """
    import uiautomation as auto

    type_id = getattr(auto.ControlType, kind)
    try:
        from uiautomation.uiautomation import _AutomationClient

        client = _AutomationClient.instance().IUIAutomation
        condition = client.CreatePropertyCondition(
            auto.PropertyId.ControlTypeProperty, type_id
        )
        if name is not None:
            condition = client.CreateAndCondition(
                condition,
                client.CreatePropertyCondition(auto.PropertyId.NameProperty, name),
            )
        found = root.Element.FindAll(_DESCENDANTS, condition)
        return [
            auto.Control.CreateControlFromElement(found.GetElement(i))
            for i in range(found.Length)
        ]
    except Exception:
        logger.debug("UIA FindAll failed; walking the tree", exc_info=True)
    return [
        control
        for control, _depth in auto.WalkControl(root, maxDepth=60)
        if control.ControlTypeName == kind
        and (name is None or (control.Name or "").strip() == name)
    ]


def _children(control: Any, depth: int = 3) -> List[Any]:
    """Everything a few levels under ``control`` (the titles inside a side
    panel link don't show up in a FindAll search, but do as children)."""
    found: List[Any] = []
    if depth <= 0:
        return found
    try:
        kids = control.GetChildren()
    except Exception:
        return found
    for kid in kids:
        found.append(kid)
        found.extend(_children(kid, depth - 1))
    return found


def _side_links(hwnd: int) -> List[Any]:
    """Side-panel playlist links: their names end in their button's name
    ("Anime music SASRPLAYS Play"); song links in the page don't."""
    import uiautomation as auto

    return [
        link
        for link in _find(auto.ControlFromHandle(hwnd), "HyperlinkControl")
        if (link.Name or "").strip().endswith(("Play", "Pause"))
    ]


def _entries(hwnd: int) -> List[tuple[Playlist, Any]]:
    """Side-panel playlists with their buttons: a link (no URL of its own)
    holding the title, the owner and a "Play" (or, while it plays, "Pause")
    button."""
    out: List[tuple[Playlist, Any]] = []
    for link in _side_links(hwnd):
        inside = _children(link)
        button = next(
            (
                c
                for c in inside
                if c.ControlTypeName == "ButtonControl"
                and (c.Name or "").strip() in ("Play", "Pause")
            ),
            None,
        )
        texts = [
            (c.Name or "").strip()
            for c in inside
            if c.ControlTypeName == "TextControl" and (c.Name or "").strip()
        ]
        if button is None or not texts:
            continue
        owner = texts[1] if len(texts) > 1 else ""
        out.append((Playlist(texts[0], owner), button))
    return out


def _open_side_panel(hwnd: int) -> bool:
    """In a narrow window the side panel is folded away behind "Guide"."""
    import uiautomation as auto

    for button in _find(auto.ControlFromHandle(hwnd), "ButtonControl", "Guide"):
        try:
            button.GetInvokePattern().Invoke()
            return True
        except Exception:
            return False
    return False


def _button_state(hwnd: int, title: str) -> str:
    """ "Play", "Pause" (that playlist is playing) or "" (not found)."""
    for playlist, button in _entries(hwnd):
        if playlist.title == title:
            return (button.Name or "").strip()
    return ""


def _button_point(hwnd: int, title: str) -> Optional[tuple[int, int]]:
    """Centre of ``title``'s Play button in the side panel."""
    for playlist, button in _entries(hwnd):
        if playlist.title == title:
            rect = button.BoundingRectangle
            if rect.right > rect.left and rect.bottom > rect.top:
                return ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
    return None


def _click_play(window: Any, title: str) -> bool:
    """Hover the playlist (its Play button only reacts to a real pointer;
    an accessibility "press" was ignored) and click the button."""
    from openjarvis.desktop import winapi

    point = COM.run(lambda: _button_point(window.hwnd, title), timeout=40)
    if point is None:
        return False
    winapi.focus_window(window.hwnd)
    winapi.move_mouse(*point)
    time.sleep(0.3)  # let the hover show the button
    winapi.click_in(window.hwnd, *point)
    return True


def _is_playing(hwnd: int) -> bool:
    """The player bar shows "Pause" while something plays."""
    import uiautomation as auto

    return bool(_find(auto.ControlFromHandle(hwnd), "ButtonControl", "Pause"))


def _app_window(timeout: float = 15.0) -> Any:
    window = find_window()
    if window is not None:
        return window
    from openjarvis.desktop import web
    from openjarvis.desktop.winapi import start_detached

    command = web.web_app(APP_NAME)
    if command is None:
        raise DesktopError(
            "Your playlists live in the YouTube Music app, and it isn't installed "
            "(install it from music.youtube.com in Brave: menu, Install YouTube "
            "Music)."
        )
    start_detached(command)
    deadline = time.monotonic() + timeout
    while window is None and time.monotonic() < deadline:
        time.sleep(0.5)
        window = find_window()
    if window is None:
        raise DesktopError("YouTube Music didn't open.")
    return window


def playlists(window: Any, timeout: float = 10.0) -> List[Playlist]:
    """Your playlists from the side panel, waiting for the page to be
    readable (Brave builds it lazily) and unfolding the panel if needed."""
    deadline = time.monotonic() + timeout
    unfolded = False
    while True:
        found = COM.run(lambda: _entries(window.hwnd), timeout=40)
        if found:
            return [p for p, _button in found]
        if not unfolded and time.monotonic() > deadline - timeout / 2:
            # "Guide" is a toggle: press it only when the panel really is
            # folded away (no playlist links at all), never to "retry".
            unfolded = True
            if not COM.run(lambda: _side_links(window.hwnd), timeout=40):
                COM.run(lambda: _open_side_panel(window.hwnd), timeout=40)
        if time.monotonic() >= deadline:
            return []
        time.sleep(0.5)


def play(query: str) -> tuple[str, str]:
    """Play your own playlist named in ``query``: (reply, playlist title)."""
    window = _app_window()
    mine = playlists(window)
    if not mine:
        raise DesktopError(
            "I can't see your playlists in YouTube Music's side panel, so I didn't "
            "play anything. Make the window wider so the panel shows."
        )
    chosen = match(mine, query)
    if chosen is None:
        names = [p.title for p in mine]
        listed = ", ".join(names[:-1]) + (f" or {names[-1]}" if len(names) > 1 else "")
        raise NoSuchPlaylist(
            f"You don't have a playlist called {wanted_name(query)!r}. "
            f"Did you mean {listed or names[0]}? Say which."
        )
    hwnd = window.hwnd
    if COM.run(lambda: _button_state(hwnd, chosen.title), timeout=40) == "Pause":
        return f"Your {chosen.title} playlist is already playing.", chosen.title
    was_playing = COM.run(lambda: _is_playing(hwnd), timeout=40)

    def started() -> bool:
        state = COM.run(lambda: _button_state(hwnd, chosen.title), timeout=40)
        if state == "Pause":
            return True
        return not was_playing and COM.run(lambda: _is_playing(hwnd), timeout=40)

    playing = (f"Playing your {chosen.title} playlist in YouTube Music.", chosen.title)
    # Right after the app opens, the panel can show before the page reacts
    # to clicks, so a first click can be lost: check, then click once more.
    for wait in (3.0, 5.0):
        if wait > 3.0 and started():
            return playing  # it did start (never click a playing one again)
        if not _click_play(window, chosen.title):
            raise DesktopError(
                f"I found your {chosen.title} playlist but couldn't press its "
                "Play button."
            )
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            time.sleep(0.5)
            if started():
                return playing
    return (
        f"I pressed Play on your {chosen.title} playlist in YouTube Music, but I "
        "can't tell whether it started.",
        chosen.title,
    )


__all__ = [
    "NoSuchPlaylist",
    "Playlist",
    "asks_for_own",
    "match",
    "mentions_my",
    "play",
    "playlists",
    "wanted_name",
]
