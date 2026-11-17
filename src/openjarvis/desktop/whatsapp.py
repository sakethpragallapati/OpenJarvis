"""WhatsApp through the app you're already signed into (WhatsApp Web/Desktop).

Drives the real window with UI Automation - the same accessibility tree a
screen reader uses - so there is no unofficial API, no QR pairing and no
account risk:

- chats are found by name in the chat list (or via its search box),
- the message box is named "Type a message to <name>", which is checked
  before anything is sent, so a message can't go to the wrong chat,
- sending is always confirmed by the user first (see server rules).
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

_TIME = re.compile(
    r"\s(?:\d{1,2}:\d{2}\s?(?:am|pm)?|yesterday|monday|tuesday|wednesday|"
    r"thursday|friday|saturday|sunday|\d{1,2}/\d{1,2}/\d{2,4})(?=\s|$)",
    re.IGNORECASE,
)
_UNREAD = re.compile(r"^(\d+)\s+unread messages?\s+", re.IGNORECASE)
_NOISE = re.compile(
    r"^(?:\d{1,2}:\d{2}\s?(?:am|pm)?|today|yesterday|monday|tuesday|wednesday|"
    r"thursday|friday|saturday|sunday|\+\d+|read|delivered|sent|"
    r"\d{1,2}/\d{1,2}/\d{2,4})$",
    re.IGNORECASE,
)
WEB_URL = "https://web.whatsapp.com"
# Section headings in WhatsApp's list and search results, not chats.
_SECTION = re.compile(
    r"^(?:chats|messages|contacts|groups|groups in common|other contacts|"
    r"contacts on whatsapp|not in your contacts)$",
    re.IGNORECASE,
)


@dataclass(slots=True)
class ChatRow:
    title: str
    unread: int
    preview: str
    center: tuple[int, int]
    # In WhatsApp's list but scrolled out of view (reveal it before clicking).
    offscreen: bool = False


def parse_row(name: str) -> tuple[str, int, str]:
    """'3 unread messages Vishnu 11:09 pm Hi' -> ('Vishnu', 3, 'Hi')."""
    text = " ".join(name.replace("\xa0", " ").split())
    unread = 0
    match = _UNREAD.match(text)
    if match:
        unread = int(match.group(1))
        text = text[match.end() :]
    text = re.sub(r"\s*Pinned chat$", "", text)
    stamp = _TIME.search(text)
    if not stamp:
        return text, unread, ""
    return text[: stamp.start()].strip(), unread, text[stamp.end() :].strip()


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def best_chat(rows: List[ChatRow], contact: str) -> Optional[ChatRow]:
    """The chat row ``contact`` means, tolerating mishearing ("Trisha" for
    "Thrisha ECE2") and spelled-out names ("T-H-R-I-S-H-A")."""
    from openjarvis.desktop.contacts import despell, score_name

    wanted = _norm(despell(contact))
    if not wanted:
        return None
    titles = [_norm(r.title) for r in rows]
    if len(wanted.replace(" ", "")) < 3:
        # "on", "a": only an exact name. A substring match once opened the
        # "Codefest" group for a misheard "on".
        exact = [row for row, title in zip(rows, titles) if title == wanted]
        return exact[0] if exact else None
    for rule in (
        lambda t: t == wanted,
        lambda t: t.startswith(wanted + " ") or t.startswith(wanted),
        lambda t: all(w in t.split() for w in wanted.split()),
        # Inside the name only at a word start ("Kaushik" in "Krishna
        # Kaushik"), never mid-word ("on" in "Codefest").
        lambda t: re.search(rf"\b{re.escape(wanted)}", t) is not None,
    ):
        for row, title in zip(rows, titles):
            if title and rule(title):
                return row
    scored = sorted(
        ((score_name(wanted, row.title), i) for i, row in enumerate(rows)),
        reverse=True,
    )
    if scored and scored[0][0] >= 0.8:
        if len(scored) == 1 or scored[1][0] < scored[0][0] - 0.02:
            return rows[scored[0][1]]
    # Last resort, on the parts of the names that differ (whole-title
    # similarity matched "Thrisha ECE-2" to "Harsha ECE-2" via "ECE-2").
    wanted_words = wanted.split()
    close = []
    for row, title in zip(rows, titles):
        words = title.split()
        mine = " ".join(w for w in wanted_words if w not in words)
        theirs = " ".join(w for w in words if w not in wanted_words)
        if mine and theirs:
            ratio = difflib.SequenceMatcher(None, mine, theirs).ratio()
            if ratio >= 0.8:
                close.append((ratio, row))
    close.sort(key=lambda pair: -pair[0])
    if close and (len(close) == 1 or close[1][0] < close[0][0] - 0.02):
        return close[0][1]
    return None


def _chat_for(rows: List[ChatRow], contact: str) -> Optional[ChatRow]:
    """``best_chat``, but never a *different* saved contact: asked for
    "Sarika ECE-2", Jarvis must not settle for "Siri ECE-2"."""
    row = best_chat(rows, contact)
    if (
        row is not None
        and not _same_person(row.title, contact)
        and _is_contact(contact)
        and _is_contact(row.title)
    ):
        logger.debug("Not opening %r for the contact %r", row.title, contact)
        return None
    return row


def suggestions(rows: List[ChatRow], contact: str, limit: int = 3) -> List[str]:
    """Names the user may have meant, from the list on screen and every
    chat Jarvis has seen before."""
    from openjarvis.desktop.contacts import despell, directory, score_name

    wanted = _norm(despell(contact))
    scored = {}
    for row in rows:
        score = score_name(wanted, row.title)
        if score >= 0.55:
            scored[row.title] = score
    try:
        for name, score in directory().candidates(contact, limit + 2):
            if score >= 0.55:
                scored[name] = max(scored.get(name, 0.0), score)
    except Exception:
        pass
    return [n for n, _ in sorted(scored.items(), key=lambda kv: -kv[1])][:limit]


# ---------------------------------------------------------------------------
# Window and tree (all on the COM thread)
# ---------------------------------------------------------------------------


def find_window() -> Any:
    from openjarvis.desktop.winapi import list_windows

    for window in list_windows():
        process = window.process.lower()
        if "whatsapp" in window.title.lower() or process.startswith("whatsapp"):
            return window
    return None


# When Jarvis last had to start WhatsApp itself (its chats take a while).
_LAUNCHED_AT = 0.0
_BROWSERS = ("brave", "chrome", "msedge", "firefox")


def _whatsapp_tab(hwnd: int) -> bool:
    """Switch a browser window to its WhatsApp tab, if it has one."""
    import uiautomation as auto

    from openjarvis.desktop.ytmusic import _find

    root = auto.ControlFromHandle(hwnd)
    for tab in _find(root, "TabItemControl"):
        if "whatsapp" not in (tab.Name or "").lower():
            continue
        for select in (
            lambda: tab.GetSelectionItemPattern().Select(),
            lambda: tab.GetInvokePattern().Invoke(),
            lambda: tab.Click(simulateMove=False),
        ):
            try:
                select()
                return True
            except Exception:
                continue
    return False


def _from_background_tab(timeout: float = 3.0) -> Any:
    """WhatsApp Web open in a tab that isn't the front one: switch to it
    instead of opening a second WhatsApp (which then loads for ages)."""
    from openjarvis.desktop.winapi import list_windows

    for window in list_windows():
        if not window.process.lower().startswith(_BROWSERS):
            continue
        try:
            switched = COM.run(lambda: _whatsapp_tab(window.hwnd), timeout=20)
        except Exception:
            continue
        if switched:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                found = find_window()
                if found is not None:
                    return found
                time.sleep(0.2)
    return None


def ensure_window(timeout: float = 25.0) -> Any:
    """The WhatsApp window, opening WhatsApp (app or Web) if needed."""
    import os

    from openjarvis.desktop.winapi import focus_window

    global _LAUNCHED_AT
    window = find_window() or _from_background_tab()
    if window is None:
        from openjarvis.pc.control import find_app

        _LAUNCHED_AT = time.monotonic()

        app = find_app("whatsapp")
        if app is not None and "whatsapp" in app[0].lower():
            from openjarvis.desktop.winapi import shell_open

            shell_open(app[1])
        else:
            os.startfile(WEB_URL)  # type: ignore[attr-defined]
        deadline = time.monotonic() + timeout
        while window is None and time.monotonic() < deadline:
            time.sleep(0.5)
            window = find_window()
        if window is None:
            raise DesktopError("WhatsApp didn't open. Is WhatsApp Web signed in?")
    focus_window(window.hwnd)
    return window


@dataclass(slots=True)
class _Tree:
    rows: List[ChatRow]
    search: Optional[tuple[int, int]]  # centre of the search box
    message_box: Optional[tuple[int, int]]  # centre of "Type a message to X"
    recipient: str
    pane_text: List[tuple[int, str]]


def _scan(hwnd: int, attempts: int = 4) -> _Tree:
    """Read the window's chat list, search box and open chat.

    While WhatsApp re-renders (search results arriving), UI Automation calls
    fail with COM errors such as "an event was unable to invoke any of the
    subscribers"; the whole read is retried after a short pause.
    """
    last: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            return _scan_once(hwnd)
        except Exception as exc:  # COMError / LookupError mid-render
            last = exc
            logger.debug("WhatsApp scan attempt %d failed: %s", attempt + 1, exc)
            time.sleep(0.35)
    raise DesktopError(f"WhatsApp's window kept changing while I read it ({last}).")


def _unnamed_search_box(
    edits: List[tuple[int, int, int, int]], box_top: int, list_right: int
) -> tuple[Optional[tuple[int, int]], int]:
    """The search box when it has text in it (and so no name): the topmost
    unnamed text box in the chat-list column, above the message box."""
    column = [
        e
        for e in edits
        if e[1] < box_top and (not list_right or e[2] <= list_right + 40)
    ]
    if not column:
        return None, 0
    left, top, right, bottom = min(column, key=lambda e: e[1])
    return ((left + right) // 2, (top + bottom) // 2), top


def _scan_once(hwnd: int) -> _Tree:
    import uiautomation as auto

    root = auto.ControlFromHandle(hwnd)
    rows: List[ChatRow] = []
    row_boxes: List[tuple[int, int, int, int]] = []
    search_top = 0
    search: Optional[tuple[int, int]] = None
    message_box: Optional[tuple[int, int]] = None
    box_top = 10**6
    recipient = ""
    texts: List[tuple[int, int, str]] = []
    unnamed_edits: List[tuple[int, int, int, int]] = []
    list_right = 0
    for control, _depth in auto.WalkControl(root, maxDepth=60):
        kind = control.ControlTypeName
        name = (control.Name or "").strip()
        if kind == "EditControl":
            rect = control.BoundingRectangle
            centre = ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
            if name.lower().startswith("search or start"):
                search, search_top = centre, rect.top
            elif not name and rect.right > rect.left and rect.bottom > rect.top:
                # The search box loses its name once it has text in it.
                unnamed_edits.append((rect.left, rect.top, rect.right, rect.bottom))
            elif name.lower().startswith("type a message"):
                message_box, box_top = centre, rect.top
                recipient = re.sub(r"^type a message to\s*", "", name, flags=re.I)
                # Groups read "Type a message to group Codefest".
                recipient = re.sub(r"^group\s+", "", recipient, flags=re.I)
            continue
        if kind in ("DataItemControl", "ListItemControl") and name:
            rect = control.BoundingRectangle
            if rect.right - rect.left < 200:
                continue
            if any(
                b[0] <= rect.left and b[1] <= rect.top and rect.bottom <= b[3] + 4
                for b in row_boxes
            ):
                continue  # a cell nested inside a row already seen
            row_boxes.append((rect.left, rect.top, rect.right, rect.bottom))
            title, unread, preview = parse_row(name)
            if title and not _SECTION.match(title):
                rows.append(
                    ChatRow(
                        title,
                        unread,
                        preview,
                        ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2),
                        offscreen=bool(control.IsOffscreen),
                    )
                )
                list_right = max(list_right, rect.right)
        elif kind == "TextControl" and name:
            rect = control.BoundingRectangle
            texts.append((rect.left, rect.top, name))
    if search is None:
        search, search_top = _unnamed_search_box(unnamed_edits, box_top, list_right)
    pane = sorted(
        (top, " ".join(text.split()))
        for left, top, text in texts
        if left > list_right
        and search_top - 15 < top < box_top  # below the chat header
        and not _NOISE.match(text.strip())
    )
    return _Tree(rows, search, message_box, recipient, pane)


def _ready(tree: _Tree) -> bool:
    return tree.search is not None and bool(tree.rows)


def _scan_window(timeout: float = 8.0) -> tuple[Any, _Tree]:
    """The WhatsApp window and what's in it, once WhatsApp is ready.

    Brave rebuilds a page's accessibility tree when its window comes to the
    front (and when Jarvis first reads it), so that first read can be almost
    empty: no search box, no chats. Then read again until both are there and
    the chat list has stopped changing. A WhatsApp that Jarvis just started
    gets longer: loading its chats took more than 8 seconds.
    """
    window = ensure_window()
    tree = COM.run(lambda: _scan(window.hwnd), timeout=40)
    if _ready(tree):
        return window, tree
    if time.monotonic() - _LAUNCHED_AT < 30.0:
        timeout = max(timeout, 30.0)
    deadline = time.monotonic() + timeout
    previous: Optional[List[str]] = None
    while time.monotonic() < deadline:
        time.sleep(0.4)
        tree = COM.run(lambda: _scan(window.hwnd), timeout=40)
        titles = [r.title for r in tree.rows]
        if _ready(tree) and titles == previous:
            break
        previous = titles if _ready(tree) else None
    logger.debug("WhatsApp ready=%s after waiting", _ready(tree))
    return window, tree


def _same_person(recipient: str, contact: str) -> bool:
    a, b = _norm(recipient), _norm(contact)
    if not a or not b:
        return False
    return (
        a == b
        or a.startswith(b)
        or b.startswith(a)
        or all(w in a.split() for w in b.split())
    )


def _wait_for_recipient(window: Any, contact: str, timeout: float = 4.0) -> _Tree:
    deadline = time.monotonic() + timeout
    tree = COM.run(lambda: _scan(window.hwnd), timeout=40)
    while not _same_person(tree.recipient, contact) and time.monotonic() < deadline:
        time.sleep(0.4)
        tree = COM.run(lambda: _scan(window.hwnd), timeout=40)
    return tree


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _remember_seen(tree: _Tree) -> None:
    try:
        from openjarvis.desktop.contacts import directory

        directory().saw(row.title for row in tree.rows)
    except Exception:
        pass


def _search_terms(contact: str) -> List[str]:
    """What to type into WhatsApp's search (it matches substrings): the name,
    its first word, then the first word minus its first letter or two, so
    "Trisha" also finds "Thrisha"."""
    from openjarvis.desktop.contacts import despell

    name = " ".join(despell(contact).split())
    terms = [name]
    first = name.split()[0] if name.split() else ""
    if first and first != name:
        terms.append(first)
    if len(first) >= 5:
        terms.append(first[1:])
    if len(first) >= 6:
        terms.append(first[2:])
    seen, out = set(), []
    for term in terms:
        if term and term.lower() not in seen:
            seen.add(term.lower())
            out.append(term)
    return out


def _search_box_focused(point: tuple[int, int]) -> bool:
    """Is the keyboard focus in the search box at ``point`` (not the message
    box)? Checked before Ctrl+A so a click that missed can't wipe a draft."""
    import uiautomation as auto

    focused = auto.GetFocusedControl()
    if focused is None or focused.ControlTypeName != "EditControl":
        return False
    if (focused.Name or "").strip().lower().startswith("type a message"):
        return False
    rect = focused.BoundingRectangle
    x, y = point
    return rect.left <= x <= rect.right and rect.top <= y <= rect.bottom


def _focus_search(window: Any, point: tuple[int, int]) -> bool:
    from openjarvis.desktop import winapi

    winapi.click_in(window.hwnd, *point)
    for _ in range(4):
        time.sleep(0.1)
        if COM.run(lambda: _search_box_focused(point), timeout=40):
            return True
    return False


def _search(window: Any, tree: _Tree, term: str) -> _Tree:
    from openjarvis.desktop import winapi

    point = tree.search
    if point is None or not _focus_search(window, point):
        raise DesktopError(
            "I couldn't get into WhatsApp's search box, so I didn't type anything."
        )
    winapi.press_hotkey("ctrl+a")  # only the search box's own text
    winapi.type_text(term)
    # WhatsApp shows the results in steps (often an empty list first), so
    # wait until the list has changed *and* stopped changing.
    deadline = time.monotonic() + 4.0
    before = [r.title for r in tree.rows]
    found, previous = tree, None
    while time.monotonic() < deadline:
        time.sleep(0.4)
        found = COM.run(lambda: _scan(window.hwnd), timeout=40)
        titles = [r.title for r in found.rows]
        if titles != before and titles == previous and titles:
            break
        if _chat_for(found.rows, term) is not None:
            break
        previous = titles
    logger.debug("WhatsApp search %r -> %s", term, [r.title for r in found.rows][:8])
    if found.search is None:
        found.search = point  # same box, now holding the term
    return found


def _scroll_into_view(hwnd: int, title: str) -> bool:
    """Ask UI Automation to scroll the chat row ``title`` into view."""
    import uiautomation as auto

    root = auto.ControlFromHandle(hwnd)
    for control, _depth in auto.WalkControl(root, maxDepth=60):
        if control.ControlTypeName not in ("DataItemControl", "ListItemControl"):
            continue
        name = (control.Name or "").strip()
        if not name or parse_row(name)[0] != title:
            continue
        try:
            control.GetScrollItemPattern().ScrollIntoView()
            return True
        except Exception:
            try:
                control.SetFocus()  # Chromium scrolls a focused row into view
                return True
            except Exception:
                return False
    return False


def _reveal(window: Any, row: ChatRow) -> ChatRow:
    """The same row once it's scrolled into view (fresh coordinates)."""
    COM.run(lambda: _scroll_into_view(window.hwnd, row.title), timeout=40)
    deadline = time.monotonic() + 2.5
    while time.monotonic() < deadline:
        time.sleep(0.3)
        tree = COM.run(lambda: _scan(window.hwnd), timeout=40)
        fresh = next((r for r in tree.rows if r.title == row.title), None)
        if fresh is not None and not fresh.offscreen:
            return fresh
    raise DesktopError(
        f"{row.title}'s chat is in your list but I couldn't scroll to it."
    )


def _is_contact(name: str) -> bool:
    try:
        from openjarvis.desktop.contacts import directory

        return _norm(name) in {_norm(n) for n in directory().names()}
    except Exception:
        return False


def _clear_search(window: Any, tree: _Tree) -> None:
    """Empty the search box again, then put the cursor back in the chat.

    No Esc: in an open chat WhatsApp Web treats it as "close this chat".
    """
    from openjarvis.desktop import winapi

    if tree.search is None:
        return
    try:
        if not _focus_search(window, tree.search):
            return
        winapi.press_hotkey("ctrl+a")
        winapi.press_hotkey("backspace")
        if tree.message_box is not None:
            time.sleep(0.2)
            winapi.click_in(window.hwnd, *tree.message_box)
    except DesktopError:
        pass


def open_chat(contact: str) -> str:
    from openjarvis.desktop import winapi
    from openjarvis.desktop.contacts import despell, directory, unfuse_chat

    spoken = contact
    if not contact.strip():
        raise DesktopError("Whose chat should I open?")
    choices: List[str] = []
    try:
        book = directory()
        alias = book.alias(contact)
        if alias:
            contact = alias
        else:
            # Your contacts' names are the chat names: "Trisha" -> the one
            # saved as "Thrisha ECE-2"; "Krishna" -> ask which of three.
            choices = book.ambiguous(contact)
            resolved = book.resolve(contact)
            if not resolved and not choices:
                # "Ammatak": "Amma chat" heard as one word.
                stem = unfuse_chat(contact)
                resolved = book.alias(stem) or book.resolve(stem) if stem else None
            contact = resolved or despell(contact)
    except Exception:
        contact = despell(contact)
    window, tree = _scan_window()
    _remember_seen(tree)
    if _same_person(tree.recipient, contact) or any(
        _norm(tree.recipient) == _norm(c) for c in choices
    ):
        return f"The chat with {tree.recipient} is already open."
    if choices:
        listed = ", ".join(choices[:-1]) + f" or {choices[-1]}"
        raise DesktopError(
            f"There's more than one {spoken!r} in your contacts. Did you mean "
            f"{listed}? Say which."
        )
    row = _chat_for(tree.rows, contact)
    all_rows = list(tree.rows)
    searched = False
    if row is None and tree.search is not None:
        # Not on screen: use WhatsApp's own search box, trying shorter
        # pieces of the name in case it was misheard.
        for term in _search_terms(contact):
            searched = True
            tree = _search(window, tree, term)
            _remember_seen(tree)
            all_rows += tree.rows
            row = _chat_for(tree.rows, contact)
            if row is not None:
                break
    if row is None and not searched:
        # Nothing was searched, so don't blame WhatsApp's search.
        raise DesktopError(
            "WhatsApp is still loading (I couldn't see its search box yet), so I "
            "didn't open anything. Say 'try again' in a moment."
        )
    if row is None:
        tried = {_norm(spoken), _norm(contact)}
        options = [o for o in suggestions(all_rows, contact) if _norm(o) not in tried]
        _clear_search(window, tree)
        listed = ""
        if options:
            listed = ", ".join(options[:-1]) + (
                f" or {options[-1]}" if len(options) > 1 else options[0]
            )
        if _norm(contact) != _norm(spoken) or _is_contact(contact):
            # The name is right (it's in the contacts list); WhatsApp just
            # didn't show a chat for it. Don't offer the same name back.
            raise DesktopError(
                f"{contact} is in your contacts, but WhatsApp's search didn't "
                "show a chat with them, so I didn't open anything."
                + (f" Or did you mean {listed}? Say which." if listed else "")
            )
        if listed:
            raise DesktopError(
                f"I couldn't find a WhatsApp chat called {spoken!r}. "
                f"Did you mean {listed}? Say which."
            )
        raise DesktopError(
            f"I couldn't find a WhatsApp chat called {spoken!r}. Add how you say "
            "it to your contacts list (say 'open my contacts list')."
        )
    if row.offscreen:
        row = _reveal(window, row)
    winapi.click_in(window.hwnd, *row.center)
    tree = _wait_for_recipient(window, row.title)
    if not _same_person(tree.recipient, row.title):
        raise DesktopError(f"I clicked {row.title} but its chat didn't open.")
    if searched:
        _clear_search(window, tree)  # don't leave the name in the search box
    try:
        from openjarvis.desktop.contacts import score_name

        opened = tree.recipient or row.title
        # Only remember a spoken form that plausibly *is* that name; corrections
        # ("Arnold" -> "it's Anand") are learned by the follow-up tracker.
        if score_name(_norm(despell(spoken)), opened) >= 0.6:
            directory().learn(spoken, opened)
    except Exception:
        pass
    return f"Opened your WhatsApp chat with {tree.recipient}."


def send(contact: str, message: str) -> str:
    from openjarvis.desktop import winapi

    message = message.strip()
    if not message:
        raise DesktopError("What should the message say?")
    if contact.strip():
        open_chat(contact)
    window, tree = _scan_window()
    if tree.message_box is None or not tree.recipient:
        raise DesktopError("No WhatsApp chat is open to send to.")
    if contact.strip() and not _same_person(tree.recipient, contact):
        raise DesktopError(
            f"The open chat is {tree.recipient}, not {contact}, so I didn't send it."
        )
    winapi.click_in(window.hwnd, *tree.message_box)
    if "\n" in message or len(message) > 160:
        # Typing a newline would press Enter and send each line separately;
        # pasting keeps a multi-line message (a poem, a list) in one piece.
        if not winapi.paste_text(message):
            for i, line in enumerate(message.split("\n")):
                if i:
                    winapi.press_hotkey("shift+enter")
                winapi.type_text(line)
    else:
        winapi.type_text(message)
    time.sleep(0.3)
    winapi.press_hotkey("enter")
    return f"Sent to {tree.recipient}: {message}"


_VOICE_BUTTON = re.compile(
    r"^(?:voice call|audio call|call|start (?:a )?voice call)$", re.I
)
_VIDEO_BUTTON = re.compile(r"^(?:video call|start (?:a )?video call)$", re.I)


def _call_button(hwnd: int, video: bool) -> Optional[tuple[int, int]]:
    """Centre of the chat header's voice/video call button, if there is one."""
    import uiautomation as auto

    wanted = _VIDEO_BUTTON if video else _VOICE_BUTTON
    root = auto.ControlFromHandle(hwnd)
    for control, _depth in auto.WalkControl(root, maxDepth=60):
        if control.ControlTypeName not in ("ButtonControl", "MenuItemControl"):
            continue
        name = (control.Name or "").strip()
        if name and wanted.match(name) and not control.IsOffscreen:
            rect = control.BoundingRectangle
            return ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
    return None


def call(contact: str = "", *, video: bool = False) -> str:
    """Start a WhatsApp voice/video call from the open (or named) chat."""
    from openjarvis.desktop import winapi

    if contact.strip():
        open_chat(contact)
    window, tree = _scan_window()
    if not tree.recipient:
        raise DesktopError("No WhatsApp chat is open. Say who to call.")
    kind = "video" if video else "voice"
    point = COM.run(lambda: _call_button(window.hwnd, video), timeout=40)
    if point is None:
        raise DesktopError(
            f"I can't see a {kind} call button in {tree.recipient}'s chat, so I "
            "didn't call. WhatsApp in the browser may not offer calls here; the "
            "WhatsApp app does."
        )
    winapi.click_in(window.hwnd, *point)
    return f"Starting a WhatsApp {kind} call with {tree.recipient}."


def read(contact: str = "", limit: int = 10) -> str:
    if contact.strip():
        open_chat(contact)
    _window, tree = _scan_window()
    if not tree.recipient:
        raise DesktopError("No WhatsApp chat is open. Say whose messages to read.")
    lines = [text for _top, text in tree.pane_text][-limit:]
    if not lines:
        return f"I can't read the messages in {tree.recipient}'s chat."
    return f"Latest in {tree.recipient}:\n" + "\n".join(lines)


def unread() -> str:
    _window, tree = _scan_window()
    waiting = [row for row in tree.rows if row.unread]
    if not waiting:
        return "No unread WhatsApp chats on screen."
    return "Unread WhatsApp chats: " + "; ".join(
        f"{row.title} ({row.unread}): {row.preview[:80]}" for row in waiting
    )


def list_chats(limit: int = 12) -> str:
    _window, tree = _scan_window()
    _remember_seen(tree)
    if not tree.rows:
        return "I can't see the WhatsApp chat list."
    return "Chats: " + "; ".join(
        row.title + (f" ({row.unread} unread)" if row.unread else "")
        for row in tree.rows[:limit]
    )


__all__ = [
    "ChatRow",
    "best_chat",
    "call",
    "suggestions",
    "list_chats",
    "open_chat",
    "parse_row",
    "read",
    "send",
    "unread",
]
