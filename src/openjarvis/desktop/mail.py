"""Email drafts without typing into the browser.

Typing a draft key by key is fragile: if the focus is in the address bar,
the email ends up there (it did). Instead the draft opens already filled
in - Gmail's compose link (``view=cm&to=&su=&body=``) or a ``mailto:`` link
for the default mail app - and sending is a separate, confirmed step that
clicks the draft's own Send button.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Optional
from urllib.parse import quote, urlencode

from openjarvis.desktop.winapi import COM, DesktopError

_EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[A-Za-z]{2,}$")


def addresses(text: str) -> tuple[list[str], list[str]]:
    """Split 'a@x.com, Priya' into (valid addresses, names without one)."""
    parts = [p.strip() for p in re.split(r"[,;]|\s+and\s+", text or "") if p.strip()]
    good = [p for p in parts if _EMAIL.match(p)]
    return good, [p for p in parts if not _EMAIL.match(p)]


def compose_url(
    to: str = "", subject: str = "", body: str = "", *, client: str = "gmail"
) -> str:
    good, _names = addresses(to)
    if client == "gmail":
        query = {"view": "cm", "fs": "1"}
        if good:
            query["to"] = ",".join(good)
        if subject:
            query["su"] = subject
        if body:
            query["body"] = body
        return "https://mail.google.com/mail/?" + urlencode(query, quote_via=quote)
    params = {k: v for k, v in (("subject", subject), ("body", body)) if v}
    tail = ("?" + urlencode(params, quote_via=quote)) if params else ""
    return "mailto:" + ",".join(good) + tail


def compose(
    to: str = "", subject: str = "", body: str = "", *, client: str = "gmail"
) -> str:
    if not (subject or body):
        raise DesktopError("What should the email say?")
    os.startfile(compose_url(to, subject, body, client=client))  # type: ignore[attr-defined]
    good, names = addresses(to)
    who = ", ".join(good) if good else "no recipient yet"
    note = (
        f" I don't have an email address for {', '.join(names)}; add it in the To box."
        if names
        else ""
    )
    return (
        f"Opened a draft to {who} with subject '{subject}'.{note} "
        "Say 'send it' when it looks right."
    )


def _send_button(hwnd: int) -> Optional[tuple[int, int]]:
    import uiautomation as auto

    root = auto.ControlFromHandle(hwnd)
    for control, _depth in auto.WalkControl(root, maxDepth=60):
        if control.ControlTypeName != "ButtonControl":
            continue
        name = (control.Name or "").strip().lower()
        if name == "send" or name.startswith("send ") or name.startswith("send‪"):
            if control.IsOffscreen:
                continue
            try:
                pattern = control.GetInvokePattern()
                if pattern is not None:
                    pattern.Invoke()
                    return (0, 0)
            except Exception:
                pass
            rect = control.BoundingRectangle
            return ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
    return None


def _draft_window() -> Any:
    from openjarvis.desktop.winapi import list_windows

    for window in list_windows():
        title = window.title.lower()
        if (
            "compose mail" in title
            or "new message" in title
            or (title.startswith("compose") and "mail" in title)
        ):
            return window
    for window in list_windows():  # Gmail inbox with a pop-up draft
        if "gmail" in window.title.lower() or "inbox" in window.title.lower():
            return window
    return None


def send_open_draft() -> str:
    from openjarvis.desktop import winapi

    window = _draft_window()
    if window is None:
        raise DesktopError("I don't see an open email draft to send.")
    winapi.focus_window(window.hwnd)
    time.sleep(0.3)
    target = COM.run(lambda: _send_button(window.hwnd), timeout=30)
    if target is None:
        raise DesktopError("I can't find the draft's Send button.")
    if target != (0, 0):
        winapi.click_in(window.hwnd, *target)
    return "Email sent."


__all__ = ["addresses", "compose", "compose_url", "send_open_draft"]
