"""Browser helpers: site searches and "play X on YouTube"."""

from __future__ import annotations

import html
import json
import re
import shlex
import time
from typing import Any, List, Optional, Tuple
from urllib.parse import quote, quote_plus

SEARCH_URLS = {
    "google": "https://www.google.com/search?q={q}",
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "bing": "https://www.bing.com/search?q={q}",
    "duckduckgo": "https://duckduckgo.com/?q={q}",
    "images": "https://www.google.com/search?tbm=isch&q={q}",
    "maps": "https://www.google.com/maps/search/{p}",
    "amazon": "https://www.amazon.in/s?k={q}",
    "flipkart": "https://www.flipkart.com/search?q={q}",
    "github": "https://github.com/search?q={q}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}",
    "spotify": "https://open.spotify.com/search/{p}",
    "netflix": "https://www.netflix.com/search?q={q}",
    "reddit": "https://www.reddit.com/search/?q={q}",
    "stackoverflow": "https://stackoverflow.com/search?q={q}",
    "chatgpt": "https://chatgpt.com/?q={q}",
    "perplexity": "https://www.perplexity.ai/search?q={q}",
    "news": "https://news.google.com/search?q={q}",
}
_SITE_ALIASES = {
    "google images": "images",
    "image": "images",
    "google maps": "maps",
    "map": "maps",
    "stack overflow": "stackoverflow",
    "yt": "youtube",
    "you tube": "youtube",
    "web": "google",
    "internet": "google",
    "browser": "google",
    "": "google",
}
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


def search_url(query: str, site: str = "google") -> str:
    site = (site or "google").strip().lower()
    site = re.sub(r"^https?://", "", site).removeprefix("www.").strip("/ ")
    site = _SITE_ALIASES.get(site, site)
    if site not in SEARCH_URLS:
        # "amazon.in", "flipkart.com": the site's own search, not Google's
        # "site:amazon.in" (which only listed links to click).
        name = site.split(".")[0]
        if "." in site and name in SEARCH_URLS:
            site = name
    template = SEARCH_URLS.get(site)
    if template is None:
        # Unknown site: let Google scope it ("search reddit.com for ...").
        return SEARCH_URLS["google"].format(q=quote_plus(f"{query} site:{site}"))
    return template.format(q=quote_plus(query), p=quote(query))


_YT_DATA = re.compile(r"var ytInitialData\s*=\s*")
# Ads and Shorts shelves aren't what "play X" means.
_YT_SKIP = {
    "adSlotRenderer",
    "reelShelfRenderer",
    "shortsLockupViewModel",
    "gridShelfViewModel",
}


def _results_from_data(text: str, limit: int) -> List[Tuple[str, str]]:
    """Videos from the page's ``ytInitialData``, in page order.

    YouTube serves two result formats at random: ``videoRenderer`` and the
    newer ``lockupViewModel``. Reading only the first made about half of all
    searches ("OnePlus Nord CE 6 Lite review") come back empty.
    """
    match = _YT_DATA.search(text)
    if not match:
        return []
    data, _ = json.JSONDecoder().raw_decode(text, match.end())
    results: List[Tuple[str, str]] = []
    seen: set = set()

    def add(video_id: Any, title: Any) -> None:
        if isinstance(video_id, str) and re.fullmatch(r"[\w-]{11}", video_id):
            if video_id not in seen:
                seen.add(video_id)
                results.append((video_id, str(title or "")))

    def walk(node: Any) -> None:
        if len(results) >= limit:
            return
        if isinstance(node, dict):
            video = node.get("videoRenderer")
            if isinstance(video, dict):
                runs = (video.get("title") or {}).get("runs") or [{}]
                add(video.get("videoId"), runs[0].get("text", ""))
            lockup = node.get("lockupViewModel")
            if isinstance(lockup, dict) and lockup.get("contentType") == (
                "LOCKUP_CONTENT_TYPE_VIDEO"
            ):
                meta = (lockup.get("metadata") or {}).get("lockupMetadataViewModel")
                title = ((meta or {}).get("title") or {}).get("content", "")
                add(lockup.get("contentId"), title)
            for key, value in node.items():
                if key not in _YT_SKIP:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return results[:limit]


def youtube_results(query: str, limit: int = 5) -> List[Tuple[str, str]]:
    """(video_id, title) for the top YouTube results, without an API key."""
    import httpx

    response = httpx.get(
        "https://www.youtube.com/results",
        params={"search_query": query},
        headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
        timeout=10,
        follow_redirects=True,
    )
    response.raise_for_status()
    text = response.text
    try:
        found = _results_from_data(text, limit)
    except Exception:
        found = []
    if found:
        return found
    results: List[Tuple[str, str]] = []
    seen = set()
    for match in re.finditer(r'"videoRenderer":\{"videoId":"([\w-]{11})"', text):
        video_id = match.group(1)
        if video_id in seen:
            continue
        seen.add(video_id)
        window = text[match.end() : match.end() + 4000]
        title_match = re.search(
            r'"title":\{"runs":\[\{"text":"((?:[^"\\]|\\.)*)"', window
        )
        title = ""
        if title_match:
            try:
                title = json.loads(f'"{title_match.group(1)}"')
            except ValueError:
                title = html.unescape(title_match.group(1))
        results.append((video_id, title))
        if len(results) >= limit:
            break
    return results


def youtube_watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def youtube_music_url(video_id: str) -> str:
    return f"https://music.youtube.com/watch?v={video_id}"


# ---------------------------------------------------------------------------
# Installed web apps (YouTube Music, etc. installed from Brave/Chrome/Edge)
# ---------------------------------------------------------------------------


def _read_shortcut(path: str) -> Tuple[str, str]:
    """(target, arguments) of a Windows .lnk shortcut."""
    from openjarvis.desktop.winapi import COM

    def read() -> Tuple[str, str]:
        import comtypes.client

        shell = comtypes.client.CreateObject("WScript.Shell", dynamic=True)
        link = shell.CreateShortcut(path)
        return str(link.TargetPath or ""), str(link.Arguments or "")

    return COM.run(read, timeout=20)


def web_app(name: str) -> Optional[List[str]]:
    """The command that starts an installed browser web app ("YouTube
    Music" installed from Brave/Chrome/Edge is ``chrome_proxy.exe
    --profile-directory=... --app-id=...``), or None if it isn't one."""
    from openjarvis.pc.control import find_app

    found = find_app(name)
    if not found or not found[1].lower().endswith(".lnk"):
        return None
    try:
        target, arguments = _read_shortcut(found[1])
    except Exception:
        return None
    if "--app-id=" not in arguments or not target:
        return None
    return [target, *shlex.split(arguments, posix=False)]


def open_in_web_app(name: str, url: str, *, window_title: str = "") -> bool:
    """Show ``url`` inside the installed web app ``name`` (its own window,
    not a browser tab). An open window of that app is closed first so the
    old song doesn't keep playing next to the new one. False if the app
    isn't installed as a web app."""
    command = web_app(name)
    if command is None:
        return False
    from openjarvis.desktop.winapi import close_window, list_windows, start_detached

    title = (window_title or name).lower()
    browsers = ("brave", "chrome", "msedge")
    open_windows = [
        w
        for w in list_windows()
        if w.title.lower().endswith(title) and w.process.lower().startswith(browsers)
    ]
    for window in open_windows:
        try:
            close_window(window.hwnd)
        except Exception:
            pass
    if open_windows:
        time.sleep(1.0)
    start_detached([*command, f"--app-launch-url-for-shortcuts-menu-item={url}"])
    return True


__all__ = [
    "SEARCH_URLS",
    "open_in_web_app",
    "search_url",
    "web_app",
    "youtube_music_url",
    "youtube_results",
    "youtube_watch_url",
]
