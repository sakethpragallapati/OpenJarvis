"""Videos listed on the YouTube page open in the browser.

"Open the third video on the right side" means the third suggestion next to
the video that's playing (or the third result on a results page), as the
user sees it. Searching YouTube again picks a different list; clicking at
guessed coordinates once paused the trailer four times. The page's links
carry their real URLs and positions in the accessibility tree, so the Nth
one can be found and clicked exactly.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, List, Tuple
from urllib.parse import parse_qs, urlparse

from openjarvis.desktop.winapi import COM, DesktopError

logger = logging.getLogger(__name__)

_BROWSERS = ("brave", "chrome", "msedge", "firefox")
_ORDINAL = {1: "1st", 2: "2nd", 3: "3rd"}
# Link names end with the length: "… Marvel Rivals 17 minutes, 7 seconds".
_DURATION = re.compile(r"(?:,?\s+\d+\s+(?:hours?|minutes?|seconds?))+,?\s*$", re.I)


@dataclass(slots=True)
class PageVideo:
    title: str
    video_id: str
    url: str
    center: Tuple[int, int]
    offscreen: bool = False


def video_id(url: str) -> str:
    """The ``v=`` id of a YouTube watch link ('' otherwise)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    if "youtube.com" not in parsed.netloc or not parsed.path.startswith("/watch"):
        return ""
    found = parse_qs(parsed.query).get("v", [""])[0]
    return found if re.fullmatch(r"[\w-]{11}", found) else ""


def find_window() -> Any:
    """The browser window showing YouTube (not YouTube Music), front first."""
    from openjarvis.desktop.winapi import list_windows

    for window in list_windows():
        title = window.title.lower()
        if (
            window.process.lower().startswith(_BROWSERS)
            and "youtube" in title
            and "youtube music" not in title
        ):
            return window
    return None


def pick(
    links: List[Tuple[str, str, Tuple[int, int, int, int], bool]],
    current: str,
    width: int,
    side: str = "",
) -> List[PageVideo]:
    """Videos in the order the user sees them.

    ``links`` are (name, url, (left, top, right, bottom), offscreen). Links to
    the video that's playing (comment timestamps, chapters) are dropped, one
    entry per video; ``side`` "right" keeps the suggestions column, "left"
    the main column.
    """
    seen: set = set()
    videos: List[Tuple[Tuple[int, int], PageVideo]] = []
    for name, url, (left, top, right, bottom), offscreen in links:
        vid = video_id(url)
        if not vid or vid == current or vid in seen:
            continue
        centre_x = (left + right) // 2
        if side == "right" and centre_x < width * 0.55:
            continue
        if side == "left" and centre_x >= width * 0.55:
            continue
        seen.add(vid)
        # Rows first (a grid reads left to right), then across.
        order = (top // 60, left)
        centre = (centre_x, (top + bottom) // 2)
        title = _DURATION.sub("", " ".join(name.split()))
        videos.append((order, PageVideo(title, vid, url, centre, offscreen)))
    videos.sort(key=lambda item: item[0])
    return [video for _order, video in videos]


def _read(
    hwnd: int,
) -> Tuple[str, int, List[Tuple[str, str, Tuple[int, int, int, int], bool]]]:
    """(current video id, window width, watch links) from the page."""
    import uiautomation as auto

    from openjarvis.desktop.ytmusic import _find

    root = auto.ControlFromHandle(hwnd)
    rect = root.BoundingRectangle
    current = ""
    for edit in _find(root, "EditControl"):
        if "address" in (edit.Name or "").lower():
            try:
                address = edit.GetValuePattern().Value or ""
            except Exception:
                address = ""
            if address and "://" not in address:
                address = "https://" + address
            current = video_id(address)
            break
    links = []
    for link in _find(root, "HyperlinkControl"):
        try:
            url = link.GetValuePattern().Value or ""
        except Exception:
            continue
        if "/watch" not in url:
            continue
        box = link.BoundingRectangle
        if box.right <= box.left or box.bottom <= box.top:
            continue
        links.append(
            (
                link.Name or "",
                url,
                (box.left, box.top, box.right, box.bottom),
                bool(link.IsOffscreen),
            )
        )
    return current, rect.right - rect.left, links


def _scroll_to(hwnd: int, vid: str) -> bool:
    import uiautomation as auto

    from openjarvis.desktop.ytmusic import _find

    for link in _find(auto.ControlFromHandle(hwnd), "HyperlinkControl"):
        try:
            if video_id(link.GetValuePattern().Value or "") != vid:
                continue
            link.GetScrollItemPattern().ScrollIntoView()
            return True
        except Exception:
            continue
    return False


def describe_place(which: int, side: str) -> str:
    place = _ORDINAL.get(which, f"{which}th")
    if side == "right":
        return f"the {place} video on the right"
    return f"the {place} video on the page"


def play(which: int, side: str = "") -> Tuple[str, List[PageVideo], int]:
    """Click the ``which``-th video on the YouTube page; (reply, list, index)."""
    from openjarvis.desktop import winapi

    window = find_window()
    if window is None:
        raise DesktopError("There's no YouTube page open in the browser.")
    winapi.focus_window(window.hwnd)
    current, width, links = COM.run(lambda: _read(window.hwnd), timeout=40)
    videos = pick(links, current, width, side)
    if not videos and side:
        videos = pick(links, current, width, "")  # narrow window: one column
    if not videos:
        raise DesktopError("I can't see any videos listed on that YouTube page.")
    index = max(int(which or 1), 1) - 1
    if index >= len(videos):
        raise DesktopError(
            f"I can only see {len(videos)} videos there, so there's no "
            f"{_ORDINAL.get(index + 1, str(index + 1) + 'th')} one."
        )
    chosen = videos[index]
    if chosen.offscreen:
        COM.run(lambda: _scroll_to(window.hwnd, chosen.video_id), timeout=40)
        time.sleep(0.5)
        _current, _width, links = COM.run(lambda: _read(window.hwnd), timeout=40)
        fresh = next(
            (
                v
                for v in pick(links, current, width, "")
                if v.video_id == chosen.video_id
            ),
            None,
        )
        if fresh is None or fresh.offscreen:
            raise DesktopError(f"I couldn't scroll to {chosen.title!r} on the page.")
        chosen = fresh
    before = window.title
    winapi.click_in(window.hwnd, *chosen.center)
    place = describe_place(index + 1, side)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        time.sleep(0.4)
        now = find_window()
        if now is not None and now.title != before:
            return f"Playing {chosen.title} ({place}).", videos, index
    return (
        f"I clicked {chosen.title} ({place}), but the page didn't change.",
        videos,
        index,
    )


__all__ = ["PageVideo", "describe_place", "find_window", "pick", "play", "video_id"]
