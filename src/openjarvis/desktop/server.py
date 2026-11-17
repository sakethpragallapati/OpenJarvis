"""The desktop tool server (FastMCP).

Every PC capability is an MCP tool on one FastMCP server. The ``desktop``
agent calls it in-process; ``jarvis desktop mcp`` serves the same tools to
other MCP clients (Claude Desktop, Cursor, ...). External MCP servers from
config are mounted on it, and OpenJarvis tools (web search, weather, ...)
are bridged in, so the agent sees one tool list.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, Dict, List, Literal, Optional
from urllib.parse import quote_plus

from pydantic import Field

from openjarvis.desktop import places, system, web
from openjarvis.desktop.recent import Recent
from openjarvis.desktop.winapi import DesktopError

logger = logging.getLogger(__name__)

SERVER_NAME = "jarvis-desktop"
INSTRUCTIONS = (
    "Tools that operate the user's Windows PC: open apps/folders/websites, "
    "find files, manage windows and processes, type and click, look at the "
    "screen, set system alarms, and keep long-term memory."
)


@dataclass
class DesktopContext:
    """Process-wide state the tools need (set by the agent or the CLI)."""

    memory: Any = None  # MemoryStore
    vision: Optional[Callable[[str, str, int], str]] = None  # VisionAsk
    last_open: float = 0.0
    front_before_open: tuple = (0, "")
    email_client: str = "gmail"  # or "default" (mailto: your mail app)
    alarm_voice: str = "male"  # voice that announces alarms: male/female/none
    alarm_advance: int = 0  # default minutes of early warning for alarms
    code_dir: str = ""  # where `code write` saves programs; "" = Desktop\Jarvis Code
    # What the last tool calls did, for follow-ups ("him", "the next one").
    recent: Recent = field(default_factory=Recent)
    # The user's words for the request being handled (set per request).
    request: str = ""
    # query, max_results -> web_search text with "Source: <url>" lines (the
    # agent hands in its configured web_search; None = a default one).
    link_search: Optional[Callable[[str, int], str]] = None


CONTEXT = DesktopContext()


def _memory() -> Any:
    if CONTEXT.memory is None:
        from openjarvis.desktop.memory import MemoryStore

        CONTEXT.memory = MemoryStore()
    return CONTEXT.memory


def _vision() -> Callable[[str, str, int], str]:
    if CONTEXT.vision is None:
        raise DesktopError(
            "Screen vision isn't available (no vision model is configured)."
        )
    return CONTEXT.vision


def _recall_place(name: str) -> Optional[str]:
    try:
        return _memory().recall_place(name)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Opening and finding
# ---------------------------------------------------------------------------


def _mark_opening() -> None:
    from openjarvis.desktop.winapi import foreground_window

    front = foreground_window()
    CONTEXT.front_before_open = (front.hwnd, front.title) if front else (0, "")
    CONTEXT.last_open = time.monotonic()


def _launch(resolution: places.Resolution) -> None:
    from openjarvis.desktop.winapi import shell_open

    _mark_opening()

    target = resolution.target
    if resolution.kind == "website":
        os.startfile(target)  # type: ignore[attr-defined]
        return
    if resolution.kind == "app" and not (
        target.startswith(("shell:", "ms-")) or ":" in target[:3] or "\\" in target
    ):
        import shutil

        target = shutil.which(target) or target  # "taskmgr.exe" -> full path
    shell_open(target)


def _fresh_notepad_tab() -> None:
    """Windows 11 Notepad reopens your previous (unsaved) tabs; anything
    typed next would land in one of them. Start a new blank tab instead."""
    from openjarvis.desktop import winapi

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        front = winapi.foreground_window()
        if front is not None and front.process.lower() == "notepad.exe":
            if not front.title.startswith("Untitled"):
                time.sleep(0.3)
                winapi.press_hotkey("ctrl+n")
            CONTEXT.last_open = 0.0  # already settled
            return
        time.sleep(0.1)


def _open_path(path: str) -> str:
    _mark_opening()
    os.startfile(path)  # type: ignore[attr-defined]
    return f"Opened {os.path.basename(path)} ({path})."


def _in_folder(folder: Any) -> List[str]:
    """What's in ``folder``, through the filesystem MCP server."""
    store = places.files()
    if store is None or folder is None:
        return []
    try:
        return store.list(str(folder))
    except DesktopError as exc:
        logger.debug("Listing %s failed: %s", folder, exc)
        return []


def _near() -> List[str]:
    """The top of Desktop, Documents and Downloads (through the server)."""
    store = places.files()
    if store is None:
        return []
    return [path for path, _ in store.nearby("any", depth=1)]


def _which_one(said: str, paths: List[str]) -> DesktopError:
    """A question for the user, not the model ("Say which" ends the turn)."""
    names = list(dict.fromkeys(os.path.basename(p) for p in paths))[:3]
    listed = ", ".join(names[:-1]) + (f" or {names[-1]}" if len(names) > 1 else "")
    return DesktopError(
        f"I couldn't find {said!r} exactly. Did you mean {listed or names[0]}? "
        "Say which."
    )


def open_target(
    target: Annotated[
        str,
        Field(
            description="What to open: app name, folder/file name or path, "
            "website, or place, e.g. 'task manager', 'minecraft server folder', "
            "'downloads', 'github.com'."
        ),
    ],
    kind: Literal["auto", "app", "folder", "file", "website"] = "auto",
) -> str:
    """Open an app, folder, file, website or known place in its app (finds
    files by partial or misheard names)."""
    name, folder = places.scope_folder(target)  # "... in desktop"
    if kind in ("auto", "file", "folder"):
        pool = _in_folder(folder)
        listed = places.best_listed(name, pool) or places.best_listed(
            name, CONTEXT.recent.paths
        )
        if listed:
            # "open CC mid1 notes" after listing the Desktop: that file.
            return _open_path(listed)
        if folder is not None:
            # Misheard ("CC mid-1 codes"): the one that sounds right, or ask.
            best, close = places.sound_pick(name, pool)
            if best:
                return _open_path(best)
            if close:
                raise _which_one(name, close)
            raise DesktopError(f"There's nothing like {name!r} in your {folder.name}.")
    target = name or target
    found = places.resolve(target, kind, recall_place=_recall_place)
    if found is None and kind != "auto":
        # The model often guesses the kind wrong ("gmail" as an app).
        found = places.resolve(target, "auto", recall_place=_recall_place)
    if found is None and kind == "website":
        url = web.search_url(places.clean_target(target)[0], "google")
        os.startfile(url)  # type: ignore[attr-defined]
        return f"I didn't know that site, so I searched Google for {target}."
    if found is None and kind in ("auto", "file", "folder"):
        # Maybe misheard: by sound, among what was just listed or the usual
        # places (Desktop, Documents, Downloads).
        pool = CONTEXT.recent.paths or _near()
        best, close = places.sound_pick(target, pool)
        if best:
            return _open_path(best)
        if close:
            raise _which_one(target, close)
    if found is None:
        raise DesktopError(f"I couldn't find anything called {target!r}.")
    if found.learned and not found.confident:
        others = places.search(places.clean_target(target)[0], "any", 4)
        # Misheard words ("codes" for "notes"): decide by sound if one clearly fits.
        pool = list(
            dict.fromkeys([f.path for f in others] + CONTEXT.recent.paths + _near())
        )
        best, close = places.sound_pick(target, pool)
        if best:
            return _open_path(best)
        raise _which_one(target, close or [f.path for f in others] or [found.target])
    _launch(found)
    if found.kind == "app" and found.target.lower().endswith("notepad.exe"):
        _fresh_notepad_tab()
    if found.learned:
        name, _ = places.clean_target(target)
        try:
            _memory().remember_place(name, found.target)
        except Exception as exc:
            logger.debug("Could not remember place %r: %s", name, exc)
    if found.kind in ("folder", "file", "place") and found.target[:1].isalpha():
        return f"Opened {found.display} ({found.target})."
    return f"Opened {found.display}."


def browser_search(
    query: str,
    site: Annotated[
        str,
        Field(
            description="google, youtube, images, maps, amazon, flipkart, "
            "github, wikipedia, spotify, netflix, reddit, news, or a domain."
        ),
    ] = "google",
) -> str:
    """Open a search for the query in the web browser."""
    url = web.search_url(query, site)
    _mark_opening()
    os.startfile(url)  # type: ignore[attr-defined]
    where = site.strip() or "Google"
    return f"Searching {where} for {query}."


# Spoken shop/site names -> the domain their pages live on.
_SITE_DOMAINS = {
    "amazon": "amazon.in",
    "amazon india": "amazon.in",
    "flipkart": "flipkart.com",
    "youtube": "youtube.com",
    "wikipedia": "wikipedia.org",
    "reddit": "reddit.com",
    "github": "github.com",
    "myntra": "myntra.com",
    "croma": "croma.com",
    "reliance digital": "reliancedigital.in",
    "gsmarena": "gsmarena.com",
    "91mobiles": "91mobiles.com",
    "imdb": "imdb.com",
    "google": "",
    "web": "",
    "": "",
}
# Search/listing pages are what open_page exists to get past.
_LISTING = re.compile(
    r"google\.[a-z.]+/search|bing\.com/search|duckduckgo\.com|/s\?k=|[?&]k=|"
    r"/search[/?]|/results\?|/b\?ie=|/b/\?",
    re.IGNORECASE,
)
_PRODUCT = re.compile(r"/dp/|/gp/product/|flipkart\.com/.+/p/|/product[s]?/", re.I)


def _site_domain(site: str) -> str:
    key = " ".join((site or "").lower().replace("www.", "").split())
    key = re.sub(r"^https?://", "", key).strip("/ ")
    if key in _SITE_DOMAINS:
        return _SITE_DOMAINS[key]
    return key if "." in key else f"{key}.com"


def _link_search(query: str, count: int) -> List[tuple]:
    if CONTEXT.link_search is not None:
        text = CONTEXT.link_search(query, count)
    else:
        from openjarvis.tools.web_search import WebSearchTool

        text = WebSearchTool().execute(query=query, max_results=count).content
    from openjarvis.desktop.recent import links_in

    return links_in(text)


def _pick_links(links: List[tuple], domain: str, count: int) -> List[tuple]:
    """The real pages among search hits: on ``domain`` if one was named,
    never a search/listing page, product pages first."""
    from urllib.parse import urlparse

    kept = []
    for title, url in links:
        host = urlparse(url).netloc.lower().removeprefix("www.")
        if domain and not (host == domain or host.endswith("." + domain)):
            continue
        if _LISTING.search(url):
            continue
        kept.append((title, url))
    if domain:
        kept.sort(key=lambda link: not _PRODUCT.search(link[1]))  # stable
    return kept[:count]


def open_page(
    query: Annotated[
        str,
        Field(
            description="What the page is about, e.g. 'OnePlus Nord CE6 Lite'. "
            "Empty = the results of the last search."
        ),
    ] = "",
    site: Annotated[
        str,
        Field(description="Where the page is: amazon, flipkart, a domain, or empty."),
    ] = "",
    count: Annotated[
        int, Field(description="How many of the top pages to open (1-5).")
    ] = 1,
) -> str:
    """Open the actual web page(s) - a product's Amazon page, the first N
    search results - in the browser (not a search page)."""
    recent = CONTEXT.recent
    count = min(max(int(count or 1), 1), 5)
    domain = _site_domain(site)
    query = (query or "").strip()
    if query:
        searched = f"{query} site:{domain}" if domain else query
        try:
            links = _link_search(searched, max(count * 3, 6))
        except Exception as exc:
            raise DesktopError(f"I couldn't search for {query}: {exc}") from exc
    elif recent.links:
        links = list(recent.links)
    elif recent.query:
        query = recent.query
        if not domain and recent.site not in ("", "google", "web_search"):
            domain = _site_domain(recent.site)
        searched = f"{query} site:{domain}" if domain else query
        links = _link_search(searched, max(count * 3, 6))
    else:
        raise DesktopError("Which page should I open?")
    chosen = _pick_links(links, domain, count)
    if not chosen:
        where = f" on {domain}" if domain else ""
        raise DesktopError(f"I couldn't find a page{where} for {query or 'that'}.")
    _mark_opening()
    for _title, url in chosen:
        os.startfile(url)  # type: ignore[attr-defined]
    recent.links = links
    names = [title or url for title, url in chosen]
    if len(names) == 1:
        return f"Opened {names[0]} ({chosen[0][1]})."
    listed = "; ".join(f"{i}. {n}" for i, n in enumerate(names, 1))
    return f"Opened {len(names)} pages: {listed}."


_MUSIC = re.compile(
    r"\b(?:songs?|music|playlists?|albums?|tracks?|lo-?fi|remix(?:es)?|bgm|ost|"
    r"soundtracks?|lyrics?|jukebox|singers?|melod(?:y|ies)|bhajans?|ghazals?|"
    r"qawwalis?|beats|hits)\b",
    re.IGNORECASE,
)
_VIDEO = re.compile(
    r"\b(?:videos?|trailers?|reviews?|unboxing|vlogs?|tutorials?|episodes?|movies?|"
    r"films?|highlights|interviews?|podcasts?|gameplay|news|explained|how to|"
    r"comparison|teaser|shorts?|clips?)\b",
    re.IGNORECASE,
)
# "…in YouTube Music, not in YouTube website": the negated part doesn't count.
_NOT_YOUTUBE = re.compile(
    r"\bnot\s+(?:in|on|from|using)?\s*(?:the\s+)?(?:youtube|you tube)"
    r"(?:\s+(?:website|site|web|app|music))?",
    re.IGNORECASE,
)
_WEBSITE = re.compile(
    r"\b(?:youtube|you tube)\s+(?:website|site|web(?:site)?|dot com)\b|"
    r"\byoutube\.com\b|\bin the browser\b",
    re.IGNORECASE,
)


def _destination(query: str, where: str) -> str:
    """Where to play: songs and music go to the YouTube Music app (the
    user's standing rule), videos and anything said to be "on the YouTube
    website" to YouTube."""
    said = _NOT_YOUTUBE.sub(" ", f"{CONTEXT.request} {query}")
    if _WEBSITE.search(said):
        return "youtube"
    if where == "youtube_music":
        return where
    if _MUSIC.search(said) and not _VIDEO.search(said):
        return "youtube_music"
    return where


def _play_own_playlist(query: str, where: str) -> Optional[str]:
    """ "my anime music playlist" -> that playlist from your YouTube Music
    library (a YouTube search found a public mix instead). None = not one of
    yours: search as usual."""
    from openjarvis.desktop import ytmusic

    clearly_mine = ytmusic.asks_for_own(query)
    if not query or not (
        clearly_mine or (where == "youtube_music" and ytmusic.mentions_my(query))
    ):
        return None
    _mark_opening()
    try:
        reply, title = ytmusic.play(query)
    except DesktopError:
        if clearly_mine:
            raise
        return None  # "play my heart will go on": a song, not a playlist
    # Remembered as a playlist: "next" goes to its next song (media key).
    CONTEXT.recent.played(title, [("", title)], 0, where="youtube_music")
    return reply


def _page_side(request: str) -> str:
    """'…on the right side' -> 'right' ("in the left side, right side" was a
    self-correction: right wins)."""
    if re.search(r"\bright\b", request or "", re.I):
        return "right"
    if re.search(r"\bleft\b", request or "", re.I):
        return "left"
    return ""


def _play_from_page(which: int) -> str:
    """The which-th video listed on the YouTube page in the browser."""
    from openjarvis.desktop import youtube_page

    _mark_opening()
    reply, videos, index = youtube_page.play(which, _page_side(CONTEXT.request))
    results = [(v.video_id, v.title) for v in videos]
    CONTEXT.recent.played("the videos on the YouTube page", results, index)
    return reply


def play_youtube(
    query: Annotated[
        str,
        Field(description="Video to play, e.g. 'lofi hip hop'. Empty = same search."),
    ] = "",
    which: Annotated[
        int,
        Field(
            description="1 = top result, 2 = second...; 0 = the next result after "
            "the one playing ('play the next one')."
        ),
    ] = 1,
    where: Annotated[
        Literal["youtube", "youtube_music"],
        Field(
            description="youtube_music = the YouTube Music app: every song or "
            "music request. youtube = videos (reviews, trailers...)."
        ),
    ] = "youtube",
    on_page: Annotated[
        bool,
        Field(
            description="true = the which-th video listed on the YouTube page "
            "open in the browser ('the third video on the right side'), not a "
            "new search."
        ),
    ] = False,
) -> str:
    """Play a YouTube video, or a song in the YouTube Music app (top result,
    the Nth, or the next one after what's playing). Songs and music always
    go to YouTube Music. Your own playlists: keep 'my' in the query ('my
    anime music playlist', 'my liked songs')."""
    recent = CONTEXT.recent
    query = (query or "").strip()
    if on_page:
        return _play_from_page(which)
    where = _destination(query, where)
    own = _play_own_playlist(query, where)
    if own is not None:
        return own
    if (
        where != "youtube_music"
        and not query
        and recent.youtube_where == ("youtube_music")
    ):
        where = "youtube_music"  # "play the next one" while in YouTube Music
    playlist_playing = recent.youtube_results[:1] == [("", recent.youtube_query)]
    if not query and playlist_playing and (which == 0 or which > 1):
        from openjarvis.pc import control

        return control.media_key("next")  # the playlist's own next song
    same_search = bool(recent.youtube_results) and (
        not query or query.lower() == recent.youtube_query.lower()
    )
    if same_search and (which == 0 or which > 1 or not query):
        results = recent.youtube_results
        query = recent.youtube_query
        index = recent.youtube_index + 1 if which == 0 else max(which - 1, 0)
        if index >= len(results):
            raise DesktopError(
                f"That was the last result I had for {query}; say what to play next."
            )
    else:
        if not query:
            raise DesktopError("What should I play?")
        try:
            results = web.youtube_results(query, 8)
        except Exception as exc:
            raise DesktopError(f"YouTube search failed: {exc}") from exc
        if not results:
            if where == "youtube_music":
                url = "https://music.youtube.com/search?q=" + quote_plus(query)
                if not web.open_in_web_app("YouTube Music", url):
                    os.startfile(url)  # type: ignore[attr-defined]
                return f"No direct match, so I searched YouTube Music for {query}."
            url = web.search_url(query, "youtube")
            os.startfile(url)  # type: ignore[attr-defined]
            return f"No direct match, so I opened YouTube results for {query}."
        index = min(max(which - 1, 0), len(results) - 1)
    video_id, title = results[index]
    recent.played(query, results, index, where=where)
    _mark_opening()
    if where == "youtube_music":
        url = web.youtube_music_url(video_id)
        if web.open_in_web_app("YouTube Music", url):
            return f"Playing {title or query} in YouTube Music."
        os.startfile(url)  # type: ignore[attr-defined]
        return f"Playing {title or query} on YouTube Music in the browser."
    os.startfile(web.youtube_watch_url(video_id))  # type: ignore[attr-defined]
    return f"Playing {title or query} on YouTube."


# ---------------------------------------------------------------------------
# Windows and processes
# ---------------------------------------------------------------------------


def window(
    action: Literal[
        "list",
        "focus",
        "minimize",
        "maximize",
        "restore",
        "close",
        "snap_left",
        "snap_right",
        "show_desktop",
    ],
    name: Annotated[
        str, Field(description="App or window title; empty = the active window.")
    ] = "",
) -> str:
    """List, switch to, minimize, maximize, snap or close app windows."""
    from openjarvis.desktop import winapi

    if action == "list":
        windows = winapi.list_windows()
        if not windows:
            return "No app windows are open."
        return "Open windows:\n" + "\n".join(
            f"{i}. {w.describe()}" for i, w in enumerate(windows[:25], 1)
        )
    if action == "show_desktop":
        winapi.press_hotkey("win+d")
        return "Showing the desktop."
    plural = re.match(
        r"^\s*(?:all|every)\s+(?:of\s+)?(?:the\s+|my\s+)?(.+)$", name, re.I
    )
    if plural and action in ("close", "minimize"):
        matches = winapi.find_windows(plural.group(1))
        if not matches:
            raise DesktopError(f"No open windows match {plural.group(1)!r}.")
        for window_info in matches:
            if action == "close":
                winapi.close_window(window_info.hwnd)
            else:
                winapi.show_window(window_info.hwnd, "minimize")
        verb = "Closed" if action == "close" else "Minimized"
        return f"{verb} {len(matches)} window" + ("s." if len(matches) != 1 else ".")
    target = (
        winapi.foreground_window()
        if name.strip().lower() in ("", "this", "it", "current", "active")
        else winapi.find_window(name)
    )
    if target is None:
        raise DesktopError(f"No open window matches {name!r}.")
    label = target.title or target.process
    if action == "focus":
        winapi.focus_window(target.hwnd)
        return f"Switched to {label}."
    if action in ("minimize", "maximize", "restore"):
        winapi.show_window(target.hwnd, action)
        return f"{action.title().rstrip('e')}ed {label}."
    if action == "close":
        winapi.close_window(target.hwnd)
        return f"Closed {label}."
    winapi.focus_window(target.hwnd)
    winapi.press_hotkey("win+left" if action == "snap_left" else "win+right")
    side = "left" if action == "snap_left" else "right"
    return f"Snapped {label} to the {side}."


def process(
    action: Literal["list", "kill"],
    name: Annotated[str, Field(description="Program name for kill.")] = "",
) -> str:
    """List memory-heavy programs, or force-close one by name."""
    if action == "list":
        return system.list_processes()
    if not name.strip():
        raise DesktopError("Which program should I close?")
    return system.kill_process(name)


SYSTEM_ACTIONS = (
    "volume_up",
    "volume_down",
    "set_volume",
    "mute",
    "play_pause",
    "next_track",
    "previous_track",
    "set_brightness",
    "brightness_up",
    "brightness_down",
    "battery",
    "status",
    "time",
    "lock",
    "sleep",
    "shutdown",
    "restart",
    "sign_out",
    "cancel_shutdown",
    "screenshot",
    "empty_recycle_bin",
)


def system_control(
    action: Literal[SYSTEM_ACTIONS],  # type: ignore[valid-type]
    level: Annotated[
        Optional[int],
        Field(description="0-100 for set_volume/set_brightness, or step size."),
    ] = None,
) -> str:
    """Volume, media keys, brightness, battery/time/status, lock, sleep,
    shutdown/restart, screenshot, empty recycle bin."""
    from openjarvis.pc import control

    step = int(level) if level is not None else 10
    if action == "volume_up":
        return control.change_volume("up", step)
    if action == "volume_down":
        return control.change_volume("down", step)
    if action == "set_volume":
        if level is None:
            raise DesktopError("Give a volume level from 0 to 100.")
        return control.set_volume(int(level))
    if action == "mute":
        return control.toggle_mute()
    if action in ("play_pause", "next_track", "previous_track"):
        key = {"play_pause": "play_pause", "next_track": "next"}.get(action, "previous")
        return control.media_key(key)
    if action == "set_brightness":
        if level is None:
            raise DesktopError("Give a brightness level from 0 to 100.")
        return control.set_brightness(int(level))
    if action in ("brightness_up", "brightness_down"):
        current = control.get_brightness()
        delta = step if action == "brightness_up" else -step
        return control.set_brightness(max(0, min(100, current + delta)))
    if action == "battery":
        from openjarvis.pc.power import battery_status

        status = battery_status()
        return (
            "No battery (desktop PC)."
            if status is None
            else f"Battery {status.describe()}."
        )
    if action == "status":
        return system.system_status()
    if action == "time":
        return system.now_text()
    if action == "lock":
        return control.lock_screen()
    if action == "sleep":
        return system.sleep_pc()
    if action in ("shutdown", "restart", "sign_out"):
        return system.shutdown(action)
    if action == "cancel_shutdown":
        return system.cancel_shutdown()
    if action == "screenshot":
        return system.take_screenshot()
    if action == "empty_recycle_bin":
        return system.empty_recycle_bin()
    raise DesktopError(f"Unknown system action {action!r}.")


# ---------------------------------------------------------------------------
# Keyboard, mouse, screen, UI
# ---------------------------------------------------------------------------


def _bring_app_forward(require: bool = False) -> None:
    """If Jarvis's own terminal is in front, switch to the app behind it.

    People start `jarvis listen` in a terminal and talk while it's focused;
    "look at my screen" / "type hello" mean the app they were using, not the
    console that's printing Jarvis's replies.
    """
    from openjarvis.desktop import winapi

    front = winapi.foreground_window()
    if front is None or not winapi.is_terminal(front):
        return
    app = winapi.app_window()
    if app is not None:
        try:
            winapi.focus_window(app.hwnd)
            time.sleep(0.3)
            return
        except DesktopError:
            pass
    if require:
        # Only Jarvis is open: never click/type into Jarvis's own window
        # (it once clicked its own Send button).
        raise DesktopError(
            "No app is open to do that in; only Jarvis's own window is in front. "
            "To open a file or app, use open."
        )


def _settle() -> None:
    """Before typing/clicking, let a just-opened app come to the front.

    Waits (up to ~5 s after the open) until the foreground window is no
    longer the one that was in front when ``open`` ran, then a beat more.
    """
    since = time.monotonic() - CONTEXT.last_open
    if since > 5:
        return
    from openjarvis.desktop.winapi import foreground_window

    deadline = CONTEXT.last_open + 5
    while time.monotonic() < deadline:
        current = foreground_window()
        # A new window in front, or the same browser window on a new tab.
        if current is not None and (current.hwnd, current.title) != (
            CONTEXT.front_before_open
        ):
            break
        time.sleep(0.1)
    time.sleep(0.4)
    CONTEXT.last_open = 0.0


def keyboard(
    action: Literal["type", "hotkey"],
    text: Annotated[
        str,
        Field(
            description="Text to type, or keys like 'ctrl+t', 'enter', 'alt+f4', "
            "'win+r'."
        ),
    ],
    times: int = 1,
) -> str:
    """Type text into the focused app, or press a key / shortcut."""
    from openjarvis.desktop import winapi

    # Typing needs a real app; global shortcuts (win, alt+tab) don't.
    _bring_app_forward(require=action == "type")
    _settle()
    before = _foreground_title()
    if action == "type":
        from openjarvis.desktop.screen import focused_control

        kind, name = focused_control()
        if _ADDRESS_BAR.search(name):
            raise DesktopError(
                "The cursor is in the browser's address bar, not in the page, so "
                "I didn't type there. Click the right field first with ui click "
                "(for emails use the email tool)."
            )
        winapi.type_text(text)
        return f"Typed it into {before!r}." if before else "Typed it."
    winapi.press_hotkey(text, times)
    pressed = f"Pressed {text}" + (f" {times} times." if times > 1 else ".")
    return pressed + _front_change(before)


def _front_change(before: str) -> str:
    """What the action did to the window in front (so the model can tell a
    dialog opened, e.g. ctrl+s -> 'Save As', instead of assuming success)."""
    time.sleep(0.3)
    after = _foreground_title()
    if after and after != before:
        return f" The window in front is now {after!r}."
    return f" Still in {before!r}." if before else ""


def mouse(
    action: Literal[
        "click", "double_click", "right_click", "move", "scroll_up", "scroll_down"
    ],
    x: Annotated[Optional[int], Field(description="0-1000 across the screen.")] = None,
    y: Annotated[Optional[int], Field(description="0-1000 down the screen.")] = None,
    amount: int = 5,
) -> str:
    """Click/move at a screen position (0-1000 grid) or scroll the wheel."""
    from openjarvis.desktop import winapi

    _bring_app_forward(require=True)
    if action in ("scroll_up", "scroll_down"):
        winapi.scroll(amount if action == "scroll_up" else -amount)
        return f"Scrolled {action.split('_')[1]}."
    if x is None or y is None:
        raise DesktopError("Give x and y (0-1000) for the mouse.")
    width, height = winapi.screen_size()
    px, py = int(x / 1000 * width), int(y / 1000 * height)
    if action == "move":
        winapi.move_mouse(px, py)
        return "Moved the mouse."
    before = _foreground_title()
    winapi.click(
        px,
        py,
        button="right" if action == "right_click" else "left",
        double=action == "double_click",
    )
    return "Clicked." + _front_change(before)


def _foreground_title() -> str:
    from openjarvis.desktop.winapi import foreground_window

    window_info = foreground_window()
    return window_info.title if window_info else ""


def screen(
    action: Literal[
        "describe", "check", "read", "click", "double_click", "right_click", "locate"
    ],
    query: Annotated[
        str,
        Field(
            description="Question for describe/check (check = a factual yes/no "
            "to verify a step, e.g. 'is a Save As dialog open?'), what to read, "
            "or the thing to click/locate, e.g. 'the Subscribe button'. Mention "
            "'left/right/top/bottom half' to look at that part only."
        ),
    ] = "",
) -> str:
    """Look at the screen with vision: describe it for the user, check a fact
    about it (to verify your own steps), read text, or find and click
    something by its description."""
    from openjarvis.desktop import screen as scr
    from openjarvis.desktop import winapi

    _bring_app_forward(require=action not in ("describe", "check", "read"))
    _settle()
    if action in ("describe", "check"):
        title = _foreground_title()
        context = f"The active window is titled {title!r}." if title else ""
        return scr.describe(
            _vision(), query, context=context, factual=action == "check"
        )
    if action == "read":
        return scr.read_text(_vision(), query)
    if not query.strip():
        raise DesktopError("Say what to look for on the screen.")
    before = _foreground_title()
    if action != "locate":
        # Exact accessibility names first: instant and pixel-perfect.
        try:
            return scr.click_control(
                query,
                double=action == "double_click",
                right=action == "right_click",
            ) + _front_change(before)
        except Exception:
            pass
    point = scr.locate(_vision(), query)
    if point is None:
        raise DesktopError(f"I can't see {query} on the screen.")
    if action == "locate":
        width, height = winapi.screen_size()
        return (
            f"{query} is at x={point[0] * 1000 // max(width, 1)}, "
            f"y={point[1] * 1000 // max(height, 1)} (0-1000 grid)."
        )
    winapi.click(
        *point,
        button="right" if action == "right_click" else "left",
        double=action == "double_click",
    )
    return f"Clicked {query}." + _front_change(before)


def ui(
    action: Literal["list", "click", "double_click", "right_click", "type"],
    name: Annotated[
        str, Field(description="Control name or its number from 'list'.")
    ] = "",
    text: Annotated[str, Field(description="Text for 'type'.")] = "",
) -> str:
    """Buttons, links and text boxes of the active window (accessibility
    tree): list them, click one, or type into one."""
    from openjarvis.desktop import screen as scr

    _bring_app_forward(require=True)
    _settle()
    if action == "list":
        title, controls = scr.list_controls()
        if not controls:
            return f"{title} exposes no controls; use screen instead."
        return f"Controls in {title}:\n" + "\n".join(c.describe() for c in controls)
    if action == "type":
        return scr.type_into(name, text)
    if not name.strip():
        raise DesktopError("Which control should I click?")
    before = _foreground_title()
    return scr.click_control(
        name, double=action == "double_click", right=action == "right_click"
    ) + _front_change(before)


_ADDRESS_BAR = re.compile(
    r"address and search bar|address bar|search or (?:enter|type) (?:web )?address|"
    r"search google or type a url",
    re.IGNORECASE,
)


def email(
    action: Literal["compose", "send"] = "compose",
    to: Annotated[str, Field(description="Email address(es), comma-separated.")] = "",
    subject: str = "",
    body: Annotated[str, Field(description="The full email text.")] = "",
) -> str:
    """Email: compose opens a draft with to/subject/body already filled in
    (Gmail or the mail app) - never type emails with keyboard; send sends
    the open draft (the user confirms)."""
    from openjarvis.desktop import mail

    if action == "compose":
        _mark_opening()
        return mail.compose(to, subject, body, client=CONTEXT.email_client)
    return mail.send_open_draft()


def whatsapp(
    action: Literal[
        "open_chat", "send", "read", "unread", "list", "call", "video_call"
    ],
    contact: Annotated[str, Field(description="Chat or contact name.")] = "",
    message: Annotated[str, Field(description="Text to send.")] = "",
) -> str:
    """WhatsApp in your signed-in WhatsApp Web/Desktop: open a chat by name,
    send a message (user confirms), read a chat, list unread or recent chats,
    or start a voice/video call (user confirms)."""
    from openjarvis.desktop import whatsapp as wa

    if action == "open_chat":
        return wa.open_chat(contact)
    if action == "send":
        return wa.send(contact, message)
    if action == "read":
        return wa.read(contact)
    if action == "unread":
        return wa.unread()
    if action in ("call", "video_call"):
        return wa.call(contact, video=action == "video_call")
    return wa.list_chats(20)


def contacts(
    action: Literal["list", "add", "forget", "open"],
    spoken: Annotated[
        str, Field(description="How the user says the name, e.g. 'Trisha'.")
    ] = "",
    name: Annotated[
        str, Field(description="The exact WhatsApp/email name, e.g. 'Thrisha ECE2'.")
    ] = "",
) -> str:
    """The user's contacts list (contacts.txt): how they say a name -> the
    exact chat name. add a mapping, forget one Jarvis learned, list them, or
    open the file in Notepad to edit it."""
    from openjarvis.desktop.contacts import directory

    book = directory()
    if action == "open":
        book.refresh()
        _mark_opening()
        from openjarvis.desktop.winapi import start_detached

        try:
            start_detached(["notepad.exe", str(book.path)])
        except Exception:
            os.startfile(str(book.path))  # type: ignore[attr-defined]
        return f"Opened your contacts list ({book.path})."
    if action == "add":
        if not spoken.strip() or not name.strip():
            raise DesktopError("Say how you call them and their exact chat name.")
        spoken, name = " ".join(spoken.split()), " ".join(name.split())
        if spoken.lower() == name.lower():
            raise DesktopError("A nickname has to differ from the saved name.")
        known = {n.lower(): n for n in book.names()}
        if name.lower() not in known:
            close = ", ".join(n for n, _ in book.candidates(name, 3)) or "none"
            raise DesktopError(
                f"There's no contact called {name!r} to point {spoken!r} at "
                f"(closest: {close}). Use the exact saved name."
            )
        name = known[name.lower()]
        if (book.resolve(spoken) or "").lower() == name.lower():
            return f"No need: when you say {spoken}, I already find {name}."
        book.add_nickname(spoken, name)
        return f"Saved: when you say {spoken}, I'll open {name}."
    if action == "forget":
        if book.forget(spoken):
            return f"Forgot what {spoken} meant."
        raise DesktopError(
            f"I hadn't learned {spoken!r}; edit the contacts list to change it."
        )
    book.refresh()
    pairs = [f"{k} = {v}" for k, v in book.file_aliases.items()]
    pairs += [f"{k} = {v} (learned)" for k, v in book.learned.items()]
    known = len(set(book.file_names))
    if not pairs and not known:
        return (
            "No contacts saved yet. Say 'open my contacts list' to add some "
            f"({book.path})."
        )
    head = f"I know {known} names from your contacts list."
    return head + ("\nNicknames:\n" + "\n".join(pairs[:40]) if pairs else "")


def code(
    action: Literal["write", "run", "open", "list"],
    filename: Annotated[
        str,
        Field(
            description="File name like hello.py (write), or the program to "
            "run/open; empty run = the last program written."
        ),
    ] = "",
    content: Annotated[str, Field(description="The complete source code.")] = "",
    language: Annotated[
        str, Field(description="python, javascript, powershell, html... (extension).")
    ] = "",
) -> str:
    """Programs: write saves the code as a file in the 'Jarvis Code' folder
    on the Desktop and opens it in VS Code; run executes a program and
    returns its real output; open shows one; list names them. Use this for
    'write a program/script' and 'run it' - never type code with keyboard."""
    from openjarvis.desktop import code as code_mod

    folder = code_mod.code_dir(CONTEXT.code_dir)
    if action == "write":
        path = code_mod.write(filename or "program", content, language, folder=folder)
        CONTEXT.recent.code_file = str(path)
        _mark_opening()
        editor = code_mod.open_in_editor(path)
        lines = content.rstrip("\n").count("\n") + 1
        return f"Wrote {path.name} ({lines} lines) to {path} and opened it in {editor}."
    if action == "list":
        return code_mod.listing(folder)
    target = filename.strip() or CONTEXT.recent.code_file
    if not target:
        raise DesktopError("Which program? Write one first or say its name.")
    path = code_mod.resolve(target, folder)
    CONTEXT.recent.code_file = str(path)
    if action == "open":
        _mark_opening()
        editor = code_mod.open_in_editor(path)
        return f"Opened {path} in {editor}."
    return f"Ran {path.name}. {code_mod.run(path)}"


# ---------------------------------------------------------------------------
# Alarms, memory, clipboard, files, commands
# ---------------------------------------------------------------------------


def _when_text(alarm: Any) -> str:
    from datetime import datetime

    at = alarm.at
    today = datetime.now().date()
    day = (
        "today"
        if at.date() == today
        else "tomorrow"
        if (at.date() - today).days == 1
        else f"on {at:%A %d %B}"
    )
    clock = f"{at:%I:%M %p}".lstrip("0")
    if alarm.repeat != "once":
        return f"{clock} {alarm.repeat_text()}"
    return f"{clock} {day}"


def _spoken_list(items: list) -> str:
    lines = []
    for alarm in items:
        name = f" '{alarm.label}'" if alarm.label else ""
        state = "" if alarm.enabled else " (off)"
        lines.append(f"{alarm.kind.title()}{name} at {_when_text(alarm)}{state}")
    return "\n".join(lines)


def schedule(
    action: Literal[
        "alarm",
        "timer",
        "reminder",
        "list",
        "cancel",
        "stop",
        "snooze",
        "turn_off",
        "turn_on",
    ],
    time: Annotated[
        str,
        Field(
            description="When: '7 am', '18:30', 'tomorrow 6:30 am', 'in 20 minutes'."
        ),
    ] = "",
    minutes: Annotated[
        Optional[float], Field(description="Timer length, or snooze minutes.")
    ] = None,
    label: Annotated[
        str, Field(description="Reminder text, alarm name, or which alarm.")
    ] = "",
    repeat: Literal[
        "once", "daily", "weekly", "weekdays", "weekends", "monthly", "yearly"
    ] = "once",
    days: Annotated[
        str, Field(description="Repeat on these days only, e.g. 'mon,wed,fri'.")
    ] = "",
    category: Literal[
        "general", "work", "personal", "meeting", "medication", "birthday"
    ] = "general",
    warn_minutes: Annotated[
        Optional[int], Field(description="Early warning, minutes before.")
    ] = None,
) -> str:
    """Real Windows alarms, timers and reminders (they ring and speak even
    when Jarvis is closed): set, list, cancel, stop/snooze the ringing one,
    or turn alarms off/on."""
    from openjarvis.desktop import alarms

    if action == "list":
        items = alarms.all_alarms()
        if not items:
            return "No alarms, timers or reminders are set."
        return _spoken_list(items)
    if action == "stop":
        if alarms.ringing() is None:
            alarms.stop_ringing()
            return "Nothing is ringing right now."
        alarms.stop_ringing()
        return "Alarm stopped."
    if action == "snooze":
        again = alarms.snooze(minutes or 5)
        if again is None:
            raise DesktopError("No alarm is ringing to snooze.")
        return f"Snoozed until {again.at:%I:%M %p}.".replace(" 0", " ")
    if action in ("cancel", "turn_off", "turn_on"):
        if action == "cancel":
            hits = alarms.cancel(label or "")
            verb = "Cancelled"
        else:
            hits = alarms.set_enabled(label or "", action == "turn_on")
            verb = "Turned on" if action == "turn_on" else "Turned off"
        if not hits:
            raise DesktopError(f"No alarm matches {label!r}.")
        names = [f"the {a.kind} for {_when_text(a)}" for a in hits]
        return f"{verb} " + ", ".join(names) + "."
    if action == "reminder" and not label.strip():
        raise DesktopError("What should I remind you about?")
    when = alarms.parse_time(time, minutes=minutes)
    advance = CONTEXT.alarm_advance if action == "alarm" else 0
    alarm = alarms.schedule(
        action,
        when,
        label,
        repeat,
        days=days,
        category="reminder"
        if action == "reminder" and category == "general"
        else category,
        voice=CONTEXT.alarm_voice,
        advance=warn_minutes if warn_minutes is not None else advance,
    )
    if action == "timer":
        from datetime import datetime

        seconds = max(1, round((when - datetime.now()).total_seconds()))
        span = (
            f"{seconds} seconds"
            if seconds < 90
            else f"{round(seconds / 60)} minutes"
            if seconds < 5400
            else _when_text(alarm)
        )
        return f"Timer set for {span}."
    what = f"Reminder '{alarm.label}'" if action == "reminder" else "Alarm"
    warning = f", with a {alarm.advance} minute warning" if alarm.advance else ""
    return f"{what} set for {_when_text(alarm)}{warning}."


def memory(
    action: Literal["remember", "recall", "forget", "list"],
    text: Annotated[
        str, Field(description="Fact to remember, or what to look up.")
    ] = "",
) -> str:
    """Long-term memory: remember a fact, recall what you know, forget one."""
    store = _memory()
    if action == "remember":
        if not text.strip():
            raise DesktopError("What should I remember?")
        store.remember(text)
        return "Got it, I'll remember that."
    if action == "forget":
        gone = store.forget_matching(text)
        if not gone:
            raise DesktopError(f"I have nothing about {text!r} to forget.")
        return "Forgot: " + "; ".join(m.content for m in gone)
    if action == "list":
        items = store.recent("fact", 15) + store.recent("place", 10)
        if not items:
            return "I haven't been asked to remember anything yet."
        return "\n".join(m.describe() for m in items)
    hits = store.search(text, limit=6) if text.strip() else store.recent(limit=6)
    if not hits:
        return f"I don't remember anything about {text!r}."
    return "\n".join(m.describe() for m in hits)


def clipboard(
    action: Literal["read", "write"],
    text: Annotated[str, Field(description="Text to copy, for write.")] = "",
) -> str:
    """Read the copied text, or copy text to the clipboard."""
    from openjarvis.pc import control

    if action == "read":
        content = control.read_clipboard()
        if not content.strip():
            return "The clipboard has no text."
        return content[:6000]
    if not text:
        raise DesktopError("No text to copy.")
    control.write_clipboard(text)
    return "Copied to the clipboard."


def recycle(
    path: Annotated[str, Field(description="Full path of the file or folder.")],
) -> str:
    """Delete a file or folder: moves it to the Recycle Bin (it can be
    restored). The fs_* file tools have no delete."""
    return system.recycle(path)


def run_command(
    command: Annotated[str, Field(description="A PowerShell command.")],
) -> str:
    """Run a PowerShell command for anything the other tools can't do.
    The user confirms anything that isn't a read-only query."""
    return system.run_powershell(command)


# ---------------------------------------------------------------------------
# Tool table, confirmation and auto-finish rules
# ---------------------------------------------------------------------------

_RO = {"readOnlyHint": True, "destructiveHint": False}
_ACT = {"readOnlyHint": False, "destructiveHint": False}
_RISK = {"readOnlyHint": False, "destructiveHint": True}

TOOLS: List[tuple[str, Callable[..., str], Dict[str, Any]]] = [
    ("open", open_target, {**_ACT, "openWorldHint": True}),
    ("browser_search", browser_search, {**_ACT, "openWorldHint": True}),
    ("open_page", open_page, {**_ACT, "openWorldHint": True}),
    ("play_youtube", play_youtube, {**_ACT, "openWorldHint": True}),
    ("window", window, _ACT),
    ("process", process, _RISK),
    ("system", system_control, _RISK),
    ("keyboard", keyboard, _ACT),
    ("mouse", mouse, _ACT),
    ("screen", screen, _ACT),
    ("ui", ui, _ACT),
    ("schedule", schedule, _ACT),
    ("memory", memory, _ACT),
    ("clipboard", clipboard, _ACT),
    ("recycle", recycle, _RISK),
    ("whatsapp", whatsapp, {**_ACT, "openWorldHint": True}),
    ("email", email, {**_ACT, "openWorldHint": True}),
    ("code", code, _ACT),
    ("contacts", contacts, _ACT),
    ("run_command", run_command, _RISK),
]

# Actions whose result is the whole answer: when the request was a single
# step, the agent speaks the tool's confirmation instead of asking the model
# to rephrase it (saves one model call, ~1-2 s).
TERMINAL_ACTIONS: Dict[str, set] = {
    "open": {"*"},
    "browser_search": {"*"},
    "open_page": {"*"},
    "play_youtube": {"*"},
    "window": {
        "focus",
        "minimize",
        "maximize",
        "restore",
        "close",
        "snap_left",
        "snap_right",
        "show_desktop",
    },
    "process": {"kill"},
    "system": set(SYSTEM_ACTIONS) - {"status"},
    "keyboard": {"*"},
    "mouse": {"*"},
    "screen": {"describe", "click", "double_click", "right_click"},
    "ui": {"click", "double_click", "right_click", "type"},
    "schedule": {
        "alarm",
        "timer",
        "reminder",
        "cancel",
        "stop",
        "snooze",
        "turn_off",
        "turn_on",
    },
    "memory": {"remember", "forget"},
    "clipboard": {"write"},
    "whatsapp": {"open_chat", "send", "unread", "call", "video_call"},
    "email": {"compose", "send"},
    "gmail_draft": {"*"},
    "gmail_send": {"*"},
    "gmail_unread": {"*"},
    "recycle": {"*"},
    # The filesystem MCP server's changes: its confirmation is the answer.
    "fs_write_file": {"*"},
    "fs_edit_file": {"*"},
    "fs_append_file": {"*"},
    "fs_create_directory": {"*"},
    "fs_move_file": {"*"},
    "fs_copy_file": {"*"},
    "code": {"write", "run", "open"},
    "contacts": {"add", "forget", "open"},
}


def is_terminal(name: str, args: Dict[str, Any]) -> bool:
    actions = TERMINAL_ACTIONS.get(name)
    if not actions:
        return False
    return "*" in actions or str(args.get("action", "")) in actions


def _file_question(tool: str, args: Dict[str, Any]) -> Optional[str]:
    """What to ask before a filesystem MCP change (None = just do it).

    Like the old file tool: moving and overwriting are asked about, new
    files, folders, copies and appends aren't.
    """
    path = str(args.get("path") or args.get("source") or "")
    name = os.path.basename(path.rstrip("\\/")) or path or "that"
    if tool == "move_file":
        source = str(args.get("source", ""))
        destination = str(args.get("destination", ""))
        same = os.path.dirname(source).lower() == os.path.dirname(destination).lower()
        if same and destination:
            return f"Rename {name} to {os.path.basename(destination)}?"
        return f"Move {name} to {os.path.dirname(destination) or destination}?"
    if tool == "write_file":
        return f"Replace everything in {name}?" if os.path.exists(path) else None
    if tool == "edit_file":
        return None if args.get("dryRun") else f"Change the text in {name}?"
    return None


def confirmation_question(
    name: str, args: Dict[str, Any], annotations: Any = None
) -> Optional[str]:
    """What to ask the user before running this call, or None to just run it."""
    action = str(args.get("action", ""))
    if name == "system":
        questions = {
            "shutdown": "Shut down the computer?",
            "restart": "Restart the computer?",
            "sign_out": "Sign out of Windows?",
            "sleep": "Put the computer to sleep?",
            "empty_recycle_bin": "Empty the Recycle Bin? That can't be undone.",
        }
        return questions.get(action)
    if name == "process":
        if action == "kill":
            return (
                f"Force-close {args.get('name', 'that program')}? Unsaved work is lost."
            )
        return None
    if name == "recycle":
        return f"Move {args.get('path', 'that')} to the Recycle Bin?"
    if name.startswith("fs_"):
        return _file_question(name[3:], args)
    if name == "email":
        return "Send the open email draft?" if action == "send" else None
    if name == "gmail_send":
        from openjarvis.desktop.gmail import describe_send

        return describe_send(args)
    if name.startswith("gmail_"):
        return None
    if name == "whatsapp":
        to = args.get("contact") or "the open chat"
        if action in ("call", "video_call"):
            kind = "video" if action == "video_call" else "voice"
            return f"Start a WhatsApp {kind} call with {to}?"
        if action != "send":
            return None
        return f"Send “{args.get('message', '')}” to {to} on WhatsApp?"
    if name == "run_command":
        command = str(args.get("command", ""))
        if system.is_read_only_command(command):
            return None
        return f"Run this PowerShell command: {command}?"
    if name == "code":
        if action != "run":
            return None
        # Programs Jarvis wrote in its own folder run freely; anything else
        # on the disk is asked about first.
        from openjarvis.desktop import code as code_mod

        folder = code_mod.code_dir(CONTEXT.code_dir)
        target = str(args.get("filename") or CONTEXT.recent.code_file or "")
        if not target:
            return None
        path = code_mod.resolve(target, folder)
        try:
            inside = path.resolve().is_relative_to(folder.resolve())
        except (OSError, ValueError):
            inside = False
        return None if inside else f"Run the program {path}?"
    if name in dict((t[0], t) for t in TOOLS):
        return None
    # External MCP tools: trust their own destructive hint.
    destructive = getattr(annotations, "destructiveHint", None)
    if destructive is None and isinstance(annotations, dict):
        destructive = annotations.get("destructiveHint")
    if destructive:
        shown = json.dumps(args)[:160]
        return f"Allow {name} with {shown}?"
    return None


# ---------------------------------------------------------------------------
# Building the server
# ---------------------------------------------------------------------------


def _external_servers(spec: Any) -> Dict[str, Dict[str, Any]]:
    """Normalise ``[desktop] mcp_servers`` / ``[tools.mcp] servers`` config."""
    if not spec:
        return {}
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except ValueError:
            logger.warning("Ignoring invalid MCP server config (not JSON).")
            return {}
    if isinstance(spec, dict):
        spec = spec.get("mcpServers", spec)
        return {str(k): dict(v) for k, v in spec.items() if isinstance(v, dict)}
    servers: Dict[str, Dict[str, Any]] = {}
    for item in spec if isinstance(spec, list) else []:
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except ValueError:
                continue
        if not isinstance(item, dict) or not item.get("name"):
            continue
        entry = {
            k: v for k, v in item.items() if k in ("command", "args", "url", "env")
        }
        servers[str(item["name"])] = entry
    return servers


def _proxy_target(name: str, cfg: Dict[str, Any]) -> Any:
    """What to proxy: a stdio server's stderr goes to its own log file.

    By default it would inherit ``sys.stderr``; the windowless Jarvis app
    has none (only a stand-in without a file handle), and starting the
    server then fails silently.
    """
    if not cfg.get("command"):
        return {"mcpServers": {name: cfg}}
    from fastmcp.client.transports import StdioTransport

    from openjarvis.core.paths import get_config_dir

    logs = get_config_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return StdioTransport(
        command=str(cfg["command"]),
        args=[str(a) for a in cfg.get("args") or []],
        env={str(k): str(v) for k, v in (cfg.get("env") or {}).items()},
        cwd=cfg.get("cwd"),
        log_file=logs / f"mcp-{name}.log",
    )


def mount_external(server: Any, spec: Any) -> List[str]:
    """Mount each configured MCP server under its name; returns the names."""
    from fastmcp.server import create_proxy

    mounted = []
    for name, cfg in _external_servers(spec).items():
        try:
            proxy = create_proxy(_proxy_target(name, cfg), name=name)
            server.mount(proxy, namespace=name)
            mounted.append(name)
        except Exception as exc:
            logger.warning("MCP server %r could not be mounted: %s", name, exc)
    return mounted


def mount_files(server: Any, extra_folders: Any = "") -> Optional[Any]:
    """Mount the official MCP filesystem server as ``fs_*`` and look files
    up for ``open`` through it. Returns its ``FileStore`` (None without
    Node.js)."""
    from openjarvis.desktop import files_mcp

    folders = files_mcp.user_folders(extra_folders or "")
    config = files_mcp.server_config(folders)
    store = None
    if config is None:
        logger.warning(
            "File tools are off: the MCP filesystem server needs Node.js 24+."
        )
    elif mount_external(server, {files_mcp.NAMESPACE: config}):
        store = files_mcp.FileStore(server, folders)
    places.use_files(store)
    return store


def add_openjarvis_tools(server: Any, tools: List[Any], executor: Any) -> List[str]:
    """Bridge OpenJarvis ``BaseTool``s (web_search, ...) onto the server.

    Calls still go through the OpenJarvis ``ToolExecutor``, so its RBAC,
    taint tracking and prompt-injection scanning apply as before.
    """
    import anyio
    from fastmcp.exceptions import ToolError
    from fastmcp.tools import Tool
    from fastmcp.tools.base import ToolResult as McpResult
    from pydantic import PrivateAttr

    from openjarvis.core.types import ToolCall

    class OpenJarvisTool(Tool):
        _executor: Any = PrivateAttr(default=None)

        async def run(self, arguments: Dict[str, Any]) -> McpResult:
            call = ToolCall(
                id=f"mcp-{time.monotonic_ns()}",
                name=self.name,
                arguments=json.dumps(arguments),
            )
            result = await anyio.to_thread.run_sync(self._executor.execute, call)
            if not result.success:
                raise ToolError(result.content)
            return McpResult(content=result.content)

    existing = {name for name, _fn, _a in TOOLS}
    added = []
    for tool in tools:
        spec = tool.spec
        if spec.name in existing:
            continue
        bridged = OpenJarvisTool(
            name=spec.name,
            description=spec.description,
            parameters=spec.parameters or {"type": "object", "properties": {}},
        )
        bridged._executor = executor
        server.add_tool(bridged)
        added.append(spec.name)
    return added


def build_server(
    *, external: Any = None, gmail: Optional[bool] = None, files: Any = True
) -> Any:
    """The FastMCP server with all desktop tools (plus mounted servers).

    ``gmail`` mounts the Gmail MCP server as ``gmail_*`` tools (default:
    when Gmail is connected) and then drops the browser-draft ``email`` tool.
    ``files`` mounts the MCP filesystem server as ``fs_*`` (True, or extra
    folders as "D:\\Games;E:\\Work"; False = no file tools).
    """
    from fastmcp import FastMCP

    from openjarvis.desktop import gmail as gmail_mcp

    if gmail is None:
        gmail = gmail_mcp.gmail_available() and gmail_mcp.is_connected()
    # Tool errors are reported to the caller; FastMCP's tracebacks for them
    # would otherwise be printed into the voice assistant's console.
    logging.getLogger("fastmcp").setLevel(logging.CRITICAL)
    server = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS)
    for name, fn, hints in TOOLS:
        if gmail and name == "email":
            continue
        server.tool(fn, name=name, annotations=hints)
    if gmail:
        server.mount(gmail_mcp.build_gmail_server(), namespace="gmail")
    if files is not False:
        mount_files(server, files if isinstance(files, (str, list)) else "")
    else:
        places.use_files(None)
    if external:
        from openjarvis.desktop.files_mcp import NAMESPACE

        others = _external_servers(external)
        if files is not False and others.pop(NAMESPACE, None) is not None:
            logger.warning(
                "Ignoring the configured MCP server %r: Jarvis's file tools use "
                "that name.",
                NAMESPACE,
            )
        mount_external(server, others)
    return server


__all__ = [
    "CONTEXT",
    "DesktopContext",
    "TERMINAL_ACTIONS",
    "TOOLS",
    "add_openjarvis_tools",
    "build_server",
    "confirmation_question",
    "is_terminal",
    "mount_external",
]
