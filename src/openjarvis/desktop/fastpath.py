"""Answer common one-step commands without the language model.

"Open Task Manager", "volume to 30", "pause", "play lofi on YouTube", "set
an alarm for 7 am" don't need a 4B model to plan anything; matching them
here turns a 3-6 s turn into well under a second. Anything ambiguous or
multi-step ("open YouTube and search ...") returns None and goes to the
agent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

# "Jarvis" as speech recognition sometimes hears it at the start of a
# sentence ("Darv, check my mails").
WAKE_NAMES = r"(?:jarvis|jarvi|jarv|darv|darvis|javis|jervis|jarvish|charvis)"
_POLITE_START = re.compile(
    rf"^(?:(?:hey|ok|okay|hi)\s+)?(?:{WAKE_NAMES}[\s,.!]+)?"
    r"(?:(?:please|kindly|just|now|so|um|uh|and)\s+)*"
    r"(?:(?:can|could|would|will) you\s+(?:please\s+)?)?"
    r"(?:(?:please|kindly|just)\s+)?",
    re.IGNORECASE,
)
_POLITE_END = re.compile(
    r"(?:\s+(?:please|for me|now|right now|thanks|thank you|jarvis))+$", re.I
)
_MULTI = re.compile(r"\b(?:and|then|after that|also|plus)\b|[,;]", re.I)
_SITES = (
    "google|youtube|amazon|flipkart|github|wikipedia|maps|google maps|reddit|"
    "spotify|netflix|images|google images|bing|news|stack overflow"
)
_HOTKEYS = {
    "new tab": "ctrl+t",
    "open a new tab": "ctrl+t",
    "open new tab": "ctrl+t",
    "close tab": "ctrl+w",
    "close the tab": "ctrl+w",
    "close this tab": "ctrl+w",
    "reopen tab": "ctrl+shift+t",
    "reopen the tab": "ctrl+shift+t",
    "refresh": "f5",
    "refresh the page": "f5",
    "reload": "f5",
    "reload the page": "f5",
    "go back": "alt+left",
    "go forward": "alt+right",
    "copy": "ctrl+c",
    "copy that": "ctrl+c",
    "paste": "ctrl+v",
    "paste it": "ctrl+v",
    "undo": "ctrl+z",
    "undo that": "ctrl+z",
    "redo": "ctrl+y",
    "save": "ctrl+s",
    "save it": "ctrl+s",
    "save the file": "ctrl+s",
    "select all": "ctrl+a",
    "full screen": "f11",
    "fullscreen": "f11",
    "switch window": "alt+tab",
    "switch windows": "alt+tab",
    "next tab": "ctrl+tab",
    "previous tab": "ctrl+shift+tab",
    "press enter": "enter",
    "hit enter": "enter",
    "press escape": "esc",
}
_MEDIA_WORDS = {
    "",
    "music",
    "the music",
    "some music",
    "song",
    "the song",
    "video",
    "the video",
    "it",
    "playback",
    "media",
}


@dataclass(slots=True)
class FastIntent:
    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    # Fall back to the agent if the tool fails (e.g. "open X" not found).
    fallback_on_error: bool = False


def normalize(text: str) -> str:
    text = " ".join((text or "").split())
    text = text.strip(" .!?\"'")
    text = _POLITE_START.sub("", text, count=1)
    text = _POLITE_END.sub("", text)
    return text.strip(" .!?,")


def _system(action: str, level: Optional[int] = None) -> FastIntent:
    args: Dict[str, Any] = {"action": action}
    if level is not None:
        args["level"] = level
    return FastIntent("system", args)


_CATEGORY = {"medicine": "medication"}
_DAY = r"(?:mon|tues?|wed(?:nes)?|thu(?:rs?)?|fri|sat(?:ur)?|sun)(?:day)?s?"
_DAY_LIST = rf"{_DAY}(?:\s*(?:,|and|&)\s*{_DAY})*"


def _repeat(text: str) -> tuple[str, str, str]:
    """Split a trailing repeat off ``text``: (time text, repeat, days)."""
    for pattern, repeat in (
        (r"\s+(?:every day|daily|everyday)$", "daily"),
        (r"\s+(?:on |every )?weekdays?$", "weekdays"),
        (r"\s+(?:on |every )?weekends?$", "weekends"),
        (r"\s+(?:every week|weekly)$", "weekly"),
        (r"\s+(?:every month|monthly)$", "monthly"),
        (r"\s+(?:every year|yearly|annually)$", "yearly"),
    ):
        match = re.search(pattern, text, re.I)
        if match:
            return text[: match.start()].strip(), repeat, ""
    match = re.search(rf"\s+(?:on |every )({_DAY_LIST})$", text, re.I)
    if match:
        return text[: match.start()].strip(), "custom", match.group(1)
    return text, "once", ""


def _valid_time(text: str) -> bool:
    from openjarvis.desktop.alarms import AlarmError, parse_time

    try:
        parse_time(text)
    except (AlarmError, ValueError):
        return False
    return True


def match(
    text: str,
    *,
    can_open: Optional[Callable[[str], bool]] = None,
    tools: Optional[set] = None,
    recent: Any = None,
) -> Optional[FastIntent]:
    """A direct tool call for ``text``, or None if the agent should handle it.

    ``can_open(target)`` reports whether "open <target>" resolves with
    confidence; without it every "open X" is left to the agent. ``recent``
    (a ``Recent``) lets follow-ups resolve: "play the next one", "open his
    chat", "it's Anand" after a failed lookup.
    """
    t = normalize(text)
    low = t.lower()
    if not low:
        return None
    if tools is None or "play_youtube" in tools:
        on_page = page_video(low)
        if on_page is not None:
            return on_page
    if recent is not None:
        follow = _follow_up(t, low, recent)
        if follow is not None:
            return follow

    # -- information -----------------------------------------------------------
    if re.fullmatch(
        r"(?:what(?:'s| is)? the time|what time is it(?: now)?|tell me the time|"
        r"time|current time|what(?:'s| is)? the date|what(?:'s| is)? today's date|"
        r"what day is (?:it|today)|today's date|date)",
        low,
    ):
        return _system("time")
    if re.fullmatch(
        r"(?:how much )?battery(?: left| level| status| percentage| is left)?|"
        r"how much battery (?:is left|do i have|left)|what(?:'s| is) the battery"
        r"(?: level| percentage)?|battery status",
        low,
    ):
        return _system("battery")

    # -- alarms, timers, reminders (before "set"/"open" patterns) -------------
    m = re.fullmatch(
        r"(?:set|start|create|put)?\s*(?:an?\s+)?timer\s+(?:for\s+)?"
        r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?)|"
        r"(\d+(?:\.\d+)?)\s*(second|minute|hour)s?\s+timer",
        low,
    )
    if m:
        value = float(m.group(1) or m.group(3))
        unit = (m.group(2) or m.group(4) or "minute").lower()
        minutes = (
            value / 60
            if unit.startswith("s")
            else value * 60
            if unit.startswith("h")
            else value
        )
        return FastIntent("schedule", {"action": "timer", "minutes": minutes})
    m = re.fullmatch(
        r"(?:set|create|make|put)?\s*(?:an?\s+|my\s+)?"
        r"(?:(work|meeting|medication|medicine|birthday|personal)\s+)?"
        r"alarm\s+(?:for|at)\s+(.+)|"
        r"wake me(?: up)?\s+(?:at|by|tomorrow at)\s+(.+)",
        low,
    )
    if m:
        when, repeat, days = _repeat(m.group(m.lastindex or 1) or "")
        label = ""
        named = re.search(r"\s+(?:called|named|labell?ed|titled|for)\s+(.+)$", when)
        if named and not _valid_time(named.group(1)):
            label, when = named.group(1), when[: named.start()]
            if repeat == "once":
                when, repeat, days = _repeat(when)
        if "tomorrow at" in low and "tomorrow" not in when:
            when = f"tomorrow {when}"
        if not _MULTI.search(when) and _valid_time(when):
            args = {"action": "alarm", "time": when}
            if repeat == "custom":
                args["days"] = days
            elif repeat != "once":
                args["repeat"] = repeat
            if label:
                args["label"] = label
            if m.group(1):
                args["category"] = _CATEGORY.get(m.group(1), m.group(1))
            return FastIntent("schedule", args)
        return None
    m = re.fullmatch(
        r"remind me\s+(in\s+\d+(?:\.\d+)?\s*(?:seconds?|minutes?|mins?|hours?))\s+"
        r"(?:to|that|about)\s+(.+)|"
        r"remind me\s+(?:to|that|about)\s+(.+?)\s+"
        r"(in\s+\d+(?:\.\d+)?\s*(?:seconds?|minutes?|mins?|hours?)|"
        r"at\s+.+|tomorrow(?:\s+at)?\s+.+)",
        low,
    )
    if m:
        when = m.group(1) or m.group(4) or ""
        label = m.group(2) or m.group(3) or ""
        when = re.sub(r"^at\s+", "", when)
        if label and _valid_time(when):
            # Keep the user's own casing for the reminder text.
            start = low.find(label)
            original = t[start : start + len(label)] if start >= 0 else label
            return FastIntent(
                "schedule", {"action": "reminder", "time": when, "label": original}
            )
        return None
    if re.fullmatch(
        r"(?:cancel|delete|remove|turn off|clear)\s+(?:all\s+)?(?:my\s+|the\s+)?"
        r"(?:alarms|timers|reminders)",
        low,
    ):
        return FastIntent("schedule", {"action": "cancel", "label": "all"})
    if re.fullmatch(
        r"(?:stop|dismiss|silence|mute)(?: the| that| this| my)? "
        r"(?:alarm|timer|reminder|ringing)(?: sound| noise)?|stop ringing",
        low,
    ):
        return FastIntent("schedule", {"action": "stop"})
    m = re.fullmatch(
        r"snooze(?: (?:it|the alarm|that|the timer))?"
        r"(?: for)?(?: (\d+(?:\.\d+)?) (?:more )?(?:minutes?|mins?))?",
        low,
    )
    if m:
        args = {"action": "snooze"}
        if m.group(1):
            args["minutes"] = float(m.group(1))
        return FastIntent("schedule", args)
    if re.fullmatch(
        r"(?:what|which|show|list|tell me)(?: are)?(?: me)?(?: all)? (?:my )?"
        r"(?:alarms|timers|reminders)(?: are set| do i have)?|"
        r"(?:do i have|are there) any (?:alarms|timers|reminders)(?: set)?",
        low,
    ):
        return FastIntent("schedule", {"action": "list"})

    # -- contacts list ----------------------------------------------------------
    if re.fullmatch(
        r"(?:open|show|edit|bring up)\s+(?:my\s+|the\s+)?(?:contacts?|contact)"
        r"(?:\s+(?:list|file|book|names))?",
        low,
    ):
        return FastIntent("contacts", {"action": "open"})
    m = re.fullmatch(
        r"(?:add|save|remember)\s+(.+?)\s+(?:to|in)\s+(?:my\s+)?contacts\s+as\s+(.+)|"
        r"(?:when i say|if i say)\s+(.+?)\s*,?\s+(?:i mean|open|that'?s|it'?s)\s+(.+)",
        low,
    )
    if m:
        spoken = m.group(1) or m.group(3)
        name = m.group(2) or m.group(4)
        return FastIntent(
            "contacts",
            {
                "action": "add",
                "spoken": _original(t, low, spoken),
                "name": _original(t, low, name),
            },
        )

    if (
        tools
        and "gmail_unread" in tools
        and re.fullmatch(
            r"(?:(?:read|check|show|open)(?: me)?(?: my)?(?: new| unread| latest)?"
            r" (?:e-?mails?|gmail|mails?|inbox)|"
            r"any (?:new|unread) (?:e-?mails?|mails?)|"
            r"do i have (?:any )?(?:new|unread) (?:e-?mails?|mails?))",
            low,
        )
    ):
        return FastIntent("gmail_unread", {})
    whatsapp = _whatsapp(t, low)
    if whatsapp is not None:
        return whatsapp

    m = re.fullmatch(
        rf"(?:open|go to)\s+({_SITES})\s+and\s+(?:search|look)\s+(?:for\s+)?(.+)",
        low,
    )
    if m and not _MULTI.search(m.group(2)):  # "open YouTube and search for X"
        return FastIntent("browser_search", {"query": m.group(2), "site": m.group(1)})
    music = _music(low)
    if music is not None:
        return music
    m = re.fullmatch(
        r"(?:open|launch|start)\s+(?:the\s+|my\s+)?(?:web\s+)?(?:browser|chrome|"
        r"google chrome|edge|microsoft edge|brave|firefox)\s+and\s+"
        r"(?:go to|open|visit|navigate to)\s+(.+)",
        low,
    )
    if m and not _MULTI.search(m.group(1)):  # "open chrome and go to gmail"
        return FastIntent(
            "open", {"target": m.group(1), "kind": "website"}, fallback_on_error=True
        )

    # -- single-clause commands only from here --------------------------------
    if _MULTI.search(low):
        return None

    # -- volume, media, brightness ---------------------------------------------
    m = re.fullmatch(
        r"(?:set|turn|put|change)?\s*(?:the\s+)?(?:volume|sound)\s+(?:to|at)?\s*"
        r"(\d{1,3})\s*(?:percent|%)?",
        low,
    )
    if m:
        return _system("set_volume", min(100, int(m.group(1))))
    if re.fullmatch(
        r"(?:turn (?:the )?(?:volume|sound) up|(?:volume|sound) up|"
        r"increase (?:the )?(?:volume|sound)|louder|turn it up)",
        low,
    ):
        return _system("volume_up")
    if re.fullmatch(
        r"(?:turn (?:the )?(?:volume|sound) down|(?:volume|sound) down|"
        r"decrease (?:the )?(?:volume|sound)|lower (?:the )?(?:volume|sound)|"
        r"quieter|turn it down)",
        low,
    ):
        return _system("volume_down")
    if re.fullmatch(r"(?:un)?mute(?: the)?(?: volume| sound| audio| pc| laptop)?", low):
        return _system("mute")
    m = re.fullmatch(r"(?:pause|resume|play|stop)(?:\s+(?:the\s+)?(.*))?", low)
    if m and (m.group(1) or "") in _MEDIA_WORDS:
        return _system("play_pause")
    if re.fullmatch(
        r"(?:next|skip)(?:\s+(?:this\s+|the\s+)?(?:song|track|video|one))?|"
        r"(?:play|go to)\s+(?:the\s+)?next(?:\s+(?:song|track|video|one))?",
        low,
    ):
        return _system("next_track")
    if re.fullmatch(
        r"(?:previous|last)(?:\s+(?:song|track|video))?|go back a (?:song|track)|"
        r"play the previous (?:song|track|video)",
        low,
    ):
        return _system("previous_track")
    m = re.fullmatch(
        r"(?:set|turn|put|change)?\s*(?:the\s+)?(?:screen\s+)?brightness\s+"
        r"(?:to|at)?\s*(\d{1,3})\s*(?:percent|%)?",
        low,
    )
    if m:
        return _system("set_brightness", min(100, int(m.group(1))))
    if re.fullmatch(
        r"(?:increase (?:the )?brightness|brightness up|brighter|turn up "
        r"(?:the )?brightness)",
        low,
    ):
        return _system("brightness_up")
    if re.fullmatch(
        r"(?:decrease (?:the )?brightness|lower (?:the )?brightness|brightness "
        r"down|dimmer|dim the screen|turn down (?:the )?brightness)",
        low,
    ):
        return _system("brightness_down")

    # -- session / power ---------------------------------------------------------
    if re.fullmatch(r"lock(?: the| my)?(?: screen| computer| laptop| pc)?", low):
        return _system("lock")
    if re.fullmatch(r"(?:take |capture )?(?:a )?screenshot", low):
        return _system("screenshot")
    if re.fullmatch(
        r"(?:shut ?down|turn off|power off)(?: the| my)?(?: computer| laptop| pc)", low
    ):
        return _system("shutdown")
    if re.fullmatch(
        r"restart(?: the| my)?(?: computer| laptop| pc)?|reboot(?: the computer)?", low
    ):
        return _system("restart")
    if re.fullmatch(
        r"(?:go to sleep|sleep(?: mode)?|put (?:the )?(?:computer|laptop|pc) to sleep)",
        low,
    ):
        return _system("sleep")
    if re.fullmatch(r"cancel (?:the )?(?:shutdown|restart)", low):
        return _system("cancel_shutdown")
    if re.fullmatch(r"empty (?:the )?(?:recycle bin|trash)", low):
        return _system("empty_recycle_bin")

    # -- keys ---------------------------------------------------------------------
    if low in _HOTKEYS:
        return FastIntent("keyboard", {"action": "hotkey", "text": _HOTKEYS[low]})
    m = re.fullmatch(
        r"(?:press|hit|tap|push)\s+(?:the\s+)?(.+?)(?:\s+(?:key|button))?"
        r"(?:\s+(\d+|twice|two|three)\s*(?:times)?)?",
        low,
    )
    if m:
        # "press delete" means the Delete key - never type the word.
        combo = _key_combo(m.group(1))
        if combo:
            times = {"twice": 2, "two": 2, "three": 3}.get(m.group(2) or "", 0)
            times = times or int(m.group(2) if (m.group(2) or "").isdigit() else 1)
            return FastIntent(
                "keyboard", {"action": "hotkey", "text": combo, "times": times}
            )
    m = re.fullmatch(r"scroll (up|down)(?: a bit| a lot)?", low)
    if m:
        amount = 15 if "a lot" in low else 5
        return FastIntent("mouse", {"action": f"scroll_{m.group(1)}", "amount": amount})
    m = re.fullmatch(r"type\s+(.+)", t, re.I)
    if m:
        return FastIntent("keyboard", {"action": "type", "text": m.group(1)})

    # -- windows --------------------------------------------------------------------
    if re.fullmatch(
        r"(?:show (?:the |me the )?desktop|"
        r"minimi[sz]e (?:all|everything)(?: windows)?)",
        low,
    ):
        return FastIntent("window", {"action": "show_desktop"})
    m = re.fullmatch(r"(minimi[sz]e|maximi[sz]e|restore)\s*(.*)", low)
    if m:
        action = {"minimise": "minimize", "maximise": "maximize"}.get(
            m.group(1), m.group(1)
        )
        return FastIntent(
            "window", {"action": action, "name": _window_name(m.group(2))}
        )
    m = re.fullmatch(r"(?:close|quit|exit)\s+(.+)", low)
    if m:
        return FastIntent(
            "window", {"action": "close", "name": _window_name(m.group(1))}
        )
    m = re.fullmatch(r"(?:switch to|go back to|bring up|focus(?: on)?)\s+(.+)", low)
    if m:
        return FastIntent(
            "window",
            {"action": "focus", "name": _window_name(m.group(1))},
            fallback_on_error=True,
        )

    # -- memory -------------------------------------------------------------------
    m = re.fullmatch(r"remember(?: that)?\s+(.+)", t, re.I)
    if m and not low.startswith(("remember when", "remember what", "remember where")):
        return FastIntent("memory", {"action": "remember", "text": m.group(1)})

    # -- web ----------------------------------------------------------------------
    m = re.fullmatch(r"play\s+(.+?)\s+on\s+(?:youtube|you tube)", low) or re.fullmatch(
        r"(?:play|put on)\s+(.+)", low
    )
    if m:
        query = m.group(1)
        if query in _MEDIA_WORDS:
            return _system("play_pause")
        return FastIntent("play_youtube", {"query": query}, fallback_on_error=True)
    m = re.fullmatch(rf"(?:search|look)\s+({_SITES})\s+for\s+(.+)", low)
    if m:
        return FastIntent("browser_search", {"query": m.group(2), "site": m.group(1)})
    m = re.fullmatch(
        rf"(?:search the web for|search google for|search for|google|search)\s+(.+?)"
        rf"(?:\s+on\s+({_SITES}))?",
        low,
    )
    if m:
        return FastIntent(
            "browser_search", {"query": m.group(1), "site": m.group(2) or "google"}
        )

    # -- open ---------------------------------------------------------------------
    m = re.fullmatch(
        r"(?:open|launch|start|run|show me|go to|take me to|navigate to|"
        r"bring up)\s+(.+)",
        t,
        re.I,
    )
    if m and can_open is not None:
        target = m.group(1)
        if can_open(target):
            return FastIntent("open", {"target": target}, fallback_on_error=True)
    return None


_YTM = r"(?:youtube|you tube|yt)\s*music(?:\s+app)?"
_MUSIC_APP = (
    rf"(?:the\s+|my\s+)?{_YTM}(?:\s+(?:which|that)\s+is\s+(?:installed|present)"
    r"\s+(?:in|on)\s+my\s+(?:laptop|pc|computer))?"
)


def _music(low: str) -> Optional[FastIntent]:
    """'play X on YouTube Music', 'open YouTube Music and play X', 'open
    YouTube and play X', 'play the song that's paused' (= resume)."""
    # Whisper adds pauses as commas: "Open YouTube Music and Play, My Anime…"
    low = " ".join(re.sub(r"\s*,\s*", " ", low).split())
    if re.search(r"\bpaused\b", low) and re.search(
        r"\b(?:play|played|resume|continue|unpause|start)\b", low
    ):
        return _system("play_pause")  # "play the song which is paused in…"
    m = re.fullmatch(
        rf"(?:open|launch|start|go to)\s+{_MUSIC_APP}\s+(?:and|then)\s+"
        rf"(?:open\s+and\s+)?(?:play|put on|start|open|search for|find)\s+(.+)|"
        rf"(?:play|put on|start playing)\s+(.+?)\s+(?:on|in|using|from)\s+{_MUSIC_APP}",
        low,
    )
    if m:
        query = (m.group(1) or m.group(2) or "").strip()
        # "search for telugu songs and play a song": the search is the songs.
        query = re.sub(
            r"\s+(?:and|then)\s+play(?:\s+(?:a|the|it|one|that|any|some))?"
            r"(?:\s+(?:song|songs|track|one|first one|video))?$",
            "",
            query,
        ).strip()
        if query and query not in _MEDIA_WORDS:
            return FastIntent(
                "play_youtube",
                {"query": query, "where": "youtube_music"},
                fallback_on_error=True,
            )
    m = re.fullmatch(
        r"(?:open|go to)\s+(?:the\s+)?(?:youtube|you tube)\s+(?:and|then)\s+"
        r"(?:play|put on|watch|show me)\s+(.+)",
        low,
    )
    if m and not _MULTI.search(m.group(1)):  # one tab, straight to the video
        return FastIntent("play_youtube", {"query": m.group(1)}, fallback_on_error=True)
    return None


_WA = r"(?:\s+(?:on|in|via|through)\s+whats\s?app)"
_NOT_A_NAME = {"a", "an", "the", "my", "new", "this", "that", "it", "whatsapp", ""}
# "open WhatsApp and open Trisha's chat" is one request: the chat.
_WA_PREFIX = re.compile(
    r"^(?:open|launch|start|go to|switch to)\s+whats\s?app\s+(?:and|then)\s+"
    r"(?:open|show|go to|find|bring up|read)\s+",
    re.IGNORECASE,
)
_NAME_TAIL = re.compile(r"(?:\s+(?:the|a|an|my|chat|chats|conversation))+$", re.I)
_NAME_HEAD = re.compile(r"^(?:the\s+)?(?:chat\s+(?:of|with)\s+)", re.I)
_HIM = r"(?:him|her|them|that person|that contact|the same person)"
_CORRECTION_HEAD = re.compile(
    r"^(?:(?:no|nope|not that)[,.]?\s+)?(?:(?:it'?s|it is|its|that'?s|i meant|"
    r"i said|i mean|the name is|his name is|her name is|the chat is|try|"
    r"the contact is|the person is|i want)\s+)?",
    re.IGNORECASE,
)
_THEY = r"(?:he|she|they)"
# "call X", "make a call to X", "start a video call with X", "video call X".
_CALL = (
    r"(?:(?:make|place|start|give|do)\s+(?:a\s+)?)?"
    r"(?:(video|voice|whats\s?app)\s+)?call\s+(?:to\s+|with\s+)?"
)
_LOOKS_LIKE_COMMAND = re.compile(
    r"^(?:open|close|play|pause|stop|search|google|find|set|turn|send|read|"
    r"show|what|who|where|when|why|how|which|can|could|would|is|are|do|does|"
    r"take|make|write|run|go|switch|lock|mute|volume|brightness|remind|"
    r"call|check|look|type|press|scroll|cancel|list|tell|give)\b",
    re.IGNORECASE,
)
_DID_YOU_MEAN = re.compile(r"did you mean (.+?)\? say which", re.S | re.I)
_ORDINALS = {
    "first": 0,
    "1": 0,
    "one": 0,
    "second": 1,
    "2": 1,
    "two": 1,
    "third": 2,
    "3": 2,
    "three": 2,
}


def _original(text: str, low: str, part: str) -> str:
    """``part`` (found in ``low``) with the user's own casing from ``text``."""
    start = low.find(part)
    return text[start : start + len(part)] if start >= 0 else part


def _clean_name(name: str) -> str:
    name = _NAME_HEAD.sub("", name.strip())
    name = _NAME_TAIL.sub("", name)
    return name.strip(" ,.'\"")


def _suggested(error_text: str) -> list:
    """Names from a 'Did you mean A, B or C?' failure message."""
    match = _DID_YOU_MEAN.search(error_text or "")
    if not match:
        return []
    return [n.strip() for n in re.split(r",\s*|\s+or\s+", match.group(1)) if n.strip()]


def _ordinal_pick(low: str, error_text: str) -> Optional[str]:
    names = _suggested(error_text)
    if not names:
        return None
    low = _CORRECTION_HEAD.sub("", low, count=1).strip()  # "I want the second one"
    match = re.fullmatch(
        r"(?:the\s+|number\s+|option\s+)?(first|second|third|one|two|three|1|2|3)"
        r"(?:\s+one)?",
        low,
    )
    if match:
        index = _ORDINALS[match.group(1)]
        return names[index] if index < len(names) else None
    for name in names:  # the user repeated one of the suggestions
        if low == name.lower() or low in name.lower():
            return name
    return None


_NOT_A_CORRECTION = re.compile(
    r"^(?:ok(?:ay)?|yes|yeah|yep|no|nope|nah|thanks|thank you|hmm+|fine|sure|"
    r"cool|nice|good|great|never ?mind|forget (?:it|that)|leave it|nothing|"
    r"cancel|stop|wait|sorry|what|huh|right|alright|all right)\W*$",
    re.IGNORECASE,
)


def _correction(t: str, low: str) -> Optional[str]:
    """'It's Anand' / 'T-H-R-I-S-H-A ECE2' -> the corrected name, else None."""
    from openjarvis.desktop.contacts import despell

    if _NOT_A_CORRECTION.match(low):
        return None

    head = _CORRECTION_HEAD.match(t)
    explicit = bool(head and head.group(0).strip())  # "it's …", "no, …"
    if _LOOKS_LIKE_COMMAND.match(low) and not explicit:
        return None
    raw = t[head.end() :] if head else t
    spelled = despell(raw) != raw.strip()
    rest = _clean_name(despell(raw))
    if not rest or len(rest.split()) > 4 or not re.search(r"[A-Za-z]", rest):
        return None
    if re.match(r"(?:to|a|an|the|some)\b", rest, re.I) or _LOOKS_LIKE_COMMAND.match(
        rest
    ):
        return None  # "I want to play music" is a new request, not a name
    if not explicit:
        if _LOOKS_LIKE_COMMAND.match(rest):
            return None
        # A bare correction is a name: speech-to-text capitalises names
        # ("Anand", "Thrisha ECE2"), not "bear the next to her".
        if not spelled and not any(w[:1].isupper() for w in rest.split()):
            return None
    return rest


def _retry_after_error(t: str, low: str, error: Dict[str, Any]) -> Optional[FastIntent]:
    """'try again' / 'It's Anand' / 'the second one' after a failed step."""
    tool, args, text = error["tool"], dict(error["args"]), error.get("text", "")
    action = str(args.get("action", ""))
    if re.fullmatch(
        r"(?:try again|retry|try (?:it|that) again|do it again|again|once more)",
        low,
    ):
        return FastIntent(tool, args)
    if tool == "whatsapp" and action in ("open_chat", "send", "read", "call"):
        said = text.lower()  # "Or did you mean …" starts a sentence
        if any(
            k in said
            for k in (
                "couldn't find",
                "more than one",
                "or did you mean",
                "say which",
                "didn't show a chat",
            )
        ):
            name = _ordinal_pick(low, text) or _correction(t, low)
            if name:
                return FastIntent("whatsapp", {**args, "contact": name})
    elif tool == "open" and ("couldn't find" in text or "not sure" in text):
        # "Did you mean CC Mid-1 Notes.docx.pdf? Say which." -> "yes" / "the
        # second one" / the name again opens that file.
        picked = _ordinal_pick(low, text)
        names = _suggested(text)
        if not picked and len(names) == 1 and _YES_PLEASE.fullmatch(low):
            picked = names[0]
        if not picked and names:
            from openjarvis.desktop.places import best_listed

            picked = best_listed(low, names)  # "cc mid 2" -> CC Mid-2 Notes
        if picked:
            return FastIntent("open", {"target": picked}, fallback_on_error=True)
        name = _correction(t, low)
        if name:
            return FastIntent("open", {"target": name}, fallback_on_error=True)
    return None


_YES_PLEASE = re.compile(
    r"(?:yes|yeah|yep|yup|sure|ok(?:ay)?|please|yes please|yeah please|"
    r"open it|open that|do it|go ahead|that one)(?:\s+please)?\W*"
)


def _follow_up(t: str, low: str, recent: Any) -> Optional[FastIntent]:
    """Resolve a reference to what just happened, or None."""
    error = recent.recent_error() if hasattr(recent, "recent_error") else None
    if error and re.fullmatch(
        r"(?:try again|retry|try (?:it|that) again|do it again|again|once more)", low
    ):
        return FastIntent(error["tool"], dict(error["args"]))
    found = _reference(t, low, recent)
    if found is not None:
        return found
    if error:
        return _retry_after_error(t, low, error)
    return None


_COUNTS = {
    "one": 1, "1": 1, "two": 2, "2": 2, "three": 3, "3": 3, "four": 4, "4": 4,
    "five": 5, "5": 5, "couple of": 2, "few": 3,
}  # fmt: skip
_OPEN_PAGES = re.compile(
    r"(?:(?:proceed to|go ahead and|now|then|ok(?:ay)?|so)\s+)*"
    r"(?:open|show|visit)\s+(?:me\s+)?(?:the\s+)?(?:first|top)\s+"
    r"(?:(one|two|three|four|five|couple of|few|\d)\s+)?"
    r"(websites?|web ?sites?|sites?|pages?|links?|results?)",
)
_WEB_TOOLS = ("web_search", "browser_search", "open_page")

_NTH = {
    "first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
    "fourth": 4, "4th": 4, "fifth": 5, "5th": 5, "sixth": 6, "6th": 6,
    "seventh": 7, "7th": 7, "eighth": 8, "8th": 8, "ninth": 9, "9th": 9,
    "tenth": 10, "10th": 10, "top": 1,
}  # fmt: skip
_NTH_VIDEO = re.compile(
    r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|top|"
    r"1st|2nd|3rd|[4-9]th|10th)\s+(?:\w+\s+)?(video|one|suggestion|thumbnail)s?\b"
)
# Where on the page: the suggestions column, a side, "on this page".
_ON_PAGE = re.compile(
    r"\b(?:right|left)(?:\s+hand)?\s*(?:side|panel|column|bar)?\b.*|"
    r"\bside\s*(?:bar|panel)\b|\bpanel\b|\bsuggest(?:ed|ions?)\b|"
    r"\brecommend(?:ed|ations?)\b|\bup next\b|\bon (?:the|this) (?:page|screen)\b|"
    r"\bon screen\b|\bbelow\b"
)


def page_video(low: str) -> Optional[FastIntent]:
    """'Open the third video on the right side' (also misheard: "Weigh the
    third video which is present in the right side of the panel") -> that
    video on the YouTube page, not the third result of a new search."""
    match = _NTH_VIDEO.search(low)
    if not match:
        return None
    around = low[match.end() :] + " " + low[: match.start()]
    if match.group(2) not in ("suggestion", "thumbnail") and not _ON_PAGE.search(
        around
    ):
        return None
    if re.search(r"\b(?:whats\s?app|chat|email|mail|file|folder|tab)\b", low):
        return None
    return FastIntent("play_youtube", {"which": _NTH[match.group(1)], "on_page": True})


def _open_pages(low: str, recent: Any) -> Optional[FastIntent]:
    """'open the first three websites' after a search -> those real pages
    (it once described the screen instead of opening anything)."""
    match = _OPEN_PAGES.fullmatch(low)
    if not match or not (
        getattr(recent, "links", None) or getattr(recent, "query", "")
    ):
        return None
    last = (getattr(recent, "last", None) or {}).get("tool", "")
    if match.group(2).startswith("result") and last not in _WEB_TOOLS:
        return None  # "the first result" after a YouTube search is a video
    count = _COUNTS.get(match.group(1) or "", 1)
    return FastIntent("open_page", {"count": count})


def _reference(t: str, low: str, recent: Any) -> Optional[FastIntent]:
    """'the next one', 'tell him …', 'close it', 'run it' -> the last thing."""
    pages = _open_pages(low, recent)
    if pages is not None:
        return pages
    in_music_app = getattr(recent, "youtube_where", "") == "youtube_music"
    if getattr(recent, "youtube_results", None) and in_music_app:
        # YouTube Music has its own queue: next/previous are media keys there.
        if re.search(r"\bnext\b|\bskip\b|\banother\b", low) and len(low.split()) <= 8:
            return _system("next_track")
        if re.search(r"\b(?:previous|last|go back|before)\b", low) and re.search(
            r"\b(?:song|track|one|music)\b", low
        ):
            return _system("previous_track")
    if getattr(recent, "youtube_results", None) and not in_music_app:
        m = re.fullmatch(
            r"(?:(?:stop|pause|skip)(?: (?:this|the|that|current))*"
            r"(?: (?:music|song|video|one|track))?(?: and| then)?\s+)?"
            r"(?:play\s+)?(?:me\s+)?(?:the\s+)?(next|another|a different|second|"
            r"third|other)(?: one| video| song| result| track)?|"
            r"skip(?: (?:this|the|that|current))*(?: (?:video|song|one|track))",
            low,
        )
        if m:
            which = 3 if m.group(1) == "third" else 2 if m.group(1) == "second" else 0
            return FastIntent("play_youtube", {"which": which})
        # "Open the first video" after a YouTube search once became two mouse
        # clicks at made-up spots.
        m = re.fullmatch(
            r"(?:open|play|watch|show|start|click)\s+(?:me\s+)?(?:the\s+)?"
            r"(first|1st|second|2nd|third|3rd|top)\s+(?:video|one|result|link)",
            low,
        )
        if m:
            which = {"second": 2, "2nd": 2, "third": 3, "3rd": 3}.get(m.group(1), 1)
            return FastIntent("play_youtube", {"which": which})
        # Misheard but clear enough: "bear the next to her", "be the next one".
        if (
            re.search(r"\bnext\b", low)
            and len(low.split()) <= 7
            and not re.search(
                r"\b(?:alarm|tab|page|slide|chat|message|email|mail|week|month|"
                r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|time)\b",
                low,
            )
        ):
            return FastIntent("play_youtube", {"which": 0})
    contact = getattr(recent, "contact", "")
    if contact:
        if re.fullmatch(
            r"(?:open|show|go to|go back to|back to|bring up)\s+(?:his|her|their|"
            r"that|the|the same|this)\s+(?:whats\s?app\s+)?(?:chat|conversation)"
            r"(?:\s+again)?",
            low,
        ):
            return FastIntent("whatsapp", {"action": "open_chat", "contact": contact})
        m = re.fullmatch(_CALL + rf"{_HIM}(?:\s+on\s+(video|whats\s?app))?", low)
        if m:
            video = "video" in " ".join(g or "" for g in m.groups())
            return FastIntent(
                "whatsapp",
                {"action": "video_call" if video else "call", "contact": contact},
            )
        m = re.fullmatch(
            rf"(?:tell|ask)\s+{_HIM}\s+(?:that\s+)?(.+)|"
            rf"(?:send|text|message|whatsapp)\s+{_HIM}\s+(?:saying\s+|that\s+|:\s*)?(.+)|"
            rf"send\s+(?:a\s+)?(?:message|text|whats\s?app)\s+to\s+{_HIM}\s+"
            rf"(?:saying|that says|that|:)\s*(.+)|"
            rf"send\s+(.+?)\s+to\s+{_HIM}|"
            rf"reply(?:\s+to\s+{_HIM})?\s+(?:with\s+|saying\s+)(.+)|"
            rf"say\s+(.+?)\s+to\s+{_HIM}",
            low,
        )
        if m:
            message = next(g for g in m.groups() if g)
            return FastIntent(
                "whatsapp",
                {
                    "action": "send",
                    "contact": contact,
                    "message": _original(t, low, message),
                },
            )
        if re.fullmatch(
            rf"(?:read|check)\s+(?:his|her|their)\s+(?:latest\s+|new\s+|last\s+)?"
            rf"(?:messages?|chat|reply|replies)|what did {_THEY} (?:say|reply|send|"
            rf"write|answer)|(?:did|has) {_THEY} (?:reply|replied|answer|answered|"
            rf"respond|responded)(?: yet)?|any reply from {_HIM}",
            low,
        ):
            return FastIntent("whatsapp", {"action": "read", "contact": contact})
    app = getattr(recent, "app", "")
    if app and re.fullmatch(
        r"close (?:it|that|this|the app|the window|that app|that window)", low
    ):
        return FastIntent("window", {"action": "close", "name": app})
    query = getattr(recent, "query", "")
    if query:
        m = re.fullmatch(
            rf"(?:search|look up|google|find)\s+(?:for\s+)?(?:that|it|the same|this|"
            rf"the same thing)(?:\s+on\s+({_SITES}))?",
            low,
        )
        if m:
            return FastIntent(
                "browser_search", {"query": query, "site": m.group(1) or "google"}
            )
    if getattr(recent, "code_file", "") and (
        re.fullmatch(r"run|show me the output|what(?:'s| is) the output", low)
        or (
            re.search(r"\b(?:run|execute|re-?run)\b", low)
            and re.search(
                r"\b(?:it|that|this|program|code|script|file|output|execution)\b",
                low,
            )
            and len(low.split()) <= 16
            and not re.search(r"\b(?:write|change|edit|modify|fix|add)\b", low)
        )
    ):
        return FastIntent("code", {"action": "run"})
    paths = getattr(recent, "paths", None) or []
    if paths and re.fullmatch(
        r"open (?:it|that|that one|the (?:first|second|third) one|the (?:file|folder))",
        low,
    ):
        index = 1 if "second" in low else 2 if "third" in low else 0
        if index < len(paths):
            return FastIntent("open", {"target": paths[index]}, fallback_on_error=True)
    return None


def _whatsapp(text: str, low: str) -> Optional[FastIntent]:
    """'open chat with Daddy', 'send hi to Daddy on WhatsApp', 'any new
    WhatsApp messages?' -> the whatsapp tool."""
    text = _WA_PREFIX.sub("open ", text)
    low = text.lower()
    if re.fullmatch(
        r"(?:(?:read|check|show)(?: me)?(?: my)?(?: new| unread| latest)?"
        r" whats\s?app(?: messages| chats)?|any (?:new|unread) (?:whats\s?app )?"
        r"messages|do i have (?:any )?(?:new|unread) (?:whats\s?app )?messages|"
        r"(?:who|anyone) (?:messaged|texted) me(?: on whats\s?app)?)",
        low,
    ):
        return FastIntent("whatsapp", {"action": "unread"})
    m = re.fullmatch(
        rf"(?:open|show|go to|bring up)\s+(?:my\s+|the\s+)?(?:whats\s?app\s+)?"
        rf"(?:chat|conversation|messages)\s+(?:with|of|for|from)\s+(.+?){_WA}?",
        low,
    ) or re.fullmatch(
        rf"(?:open|show|go to)\s+(?:my\s+)?(.+?)(?:'s)?\s+(?:whats\s?app\s+)?"
        rf"(?:chat|conversation){_WA}?",
        low,
    )
    if m and _clean_name(m.group(1)).lower() not in _NOT_A_NAME:
        contact = _clean_name(_original(text, low, m.group(1)))
        return FastIntent("whatsapp", {"action": "open_chat", "contact": contact})
    m = re.fullmatch(
        rf"(?:read|what did)\s+(?:the\s+)?(?:latest\s+|last\s+|new\s+)?"
        rf"(?:messages?\s+)?(?:from|in|by)?\s*(.+?)(?:'s chat)?"
        rf"(?:\s+(?:say|send|write))?{_WA}",
        low,
    )
    if m and m.group(1) not in _NOT_A_NAME:
        contact = _original(text, low, m.group(1))
        return FastIntent("whatsapp", {"action": "read", "contact": contact})
    for pattern, contact_group, message_group in (
        # "send hi to Daddy on WhatsApp"
        (rf"send\s+(.+?)\s+to\s+(.+?){_WA}", 2, 1),
        # "WhatsApp Daddy saying I'm late" / "message Daddy on WhatsApp: hi"
        (
            rf"(?:whats\s?app|message|text|send a message to|tell)\s+(.+?){_WA}?"
            r"\s*(?:saying|that says|that|:)\s+(.+)",
            1,
            2,
        ),
    ):
        m = re.fullmatch(pattern, low)
        if m and (pattern.startswith("send") or "whats" in low):
            contact = _clean_name(_original(text, low, m.group(contact_group)))
            message = _original(text, low, m.group(message_group))
            if contact.lower() not in _NOT_A_NAME and message:
                return FastIntent(
                    "whatsapp",
                    {"action": "send", "contact": contact, "message": message},
                )
    # "call Anand", "make a video call to Daddy on WhatsApp": the only kind
    # of call Jarvis can place (confirmed before it rings).
    m = re.fullmatch(_CALL + r"(.+?)(?:\s+on\s+(video|whats\s?app))?", low)
    if m:
        name = _clean_name(m.group(2))
        first = name.lower().split()[0] if name.split() else ""
        if name.lower() not in _NOT_A_NAME | _NOT_A_CALLEE and first not in _IDIOM:
            video = "video" in f"{m.group(1) or ''} {m.group(3) or ''}"
            return FastIntent(
                "whatsapp",
                {
                    "action": "video_call" if video else "call",
                    "contact": _original(text, low, name),
                },
            )
    return None


_NOT_A_CALLEE = {"me", "him", "her", "them", "someone", "somebody", "anyone", "back"}
# "call it a day", "call off the meeting", "call out", "call me back".
_IDIOM = {
    "it",
    "that",
    "this",
    "off",
    "back",
    "out",
    "up",
    "for",
    "on",
    "me",
    "us",
    "you",
}


def _key_combo(spoken: str) -> Optional[str]:
    """'control shift escape' / 'ctrl+s' / 'delete' -> a hotkey string."""
    from openjarvis.desktop.winapi import VK_CODES

    names = {"control": "ctrl", "windows": "win", "escape": "esc", "return": "enter"}
    parts = [p for p in re.split(r"\s*\+\s*|\s+plus\s+|\s+", spoken.strip()) if p]
    keys = []
    for part in parts:
        key = names.get(part, part)
        if key not in VK_CODES and not (len(key) == 1 and key.isalnum()):
            return None
        keys.append(key)
    # A lone letter ("press a") is too likely to be mis-heard speech.
    if not keys or (len(keys) == 1 and len(keys[0]) == 1):
        return None
    return "+".join(keys)


def _window_name(text: str) -> str:
    text = re.sub(r"^(?:the|this|that|my)\s+", "", text.strip())
    text = re.sub(r"\s+(?:window|app|application)$", "", text)
    return "" if text in ("window", "it", "this", "app", "") else text


__all__ = ["FastIntent", "match", "normalize"]
