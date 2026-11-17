"""WhatsApp row parsing/matching, email drafts, and their safety rules."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from openjarvis.desktop import mail, server
from openjarvis.desktop.fastpath import match
from openjarvis.desktop.whatsapp import ChatRow, best_chat, parse_row


@pytest.mark.parametrize(
    ("row", "parsed"),
    [
        (
            "3 unread messages Vishnu Vardhan AIDS 11:09 pm Whisper flow ah",
            ("Vishnu Vardhan AIDS", 3, "Whisper flow ah"),
        ),
        (
            'Codefest 11:04 pm You reacted 😮 to: \xa0 " Worst cinema" Pinned chat',
            ("Codefest", 0, 'You reacted 😮 to: " Worst cinema"'),
        ),
        (
            "7 X KAP'S 8:27 pm You :  Whisper for voice",
            ("7 X KAP'S", 0, "You : Whisper for voice"),
        ),
        ("Daddy Yesterday Okay", ("Daddy", 0, "Okay")),
        ("Group chat 12/11/2026 Hello", ("Group chat", 0, "Hello")),
    ],
)
def test_parse_row(row: str, parsed: tuple) -> None:
    assert parse_row(row) == parsed


ROWS = [
    ChatRow("Placement Info - CSE '27", 0, "", (1, 1)),
    ChatRow("Vishnu Vardhan AIDS", 3, "", (2, 2)),
    ChatRow("Daddy", 0, "", (3, 3)),
    ChatRow("Saketh Vemulapati", 0, "", (4, 4)),
]


@pytest.mark.parametrize(
    ("spoken", "title"),
    [
        ("daddy", "Daddy"),
        ("Vishnu", "Vishnu Vardhan AIDS"),
        ("vishnu vardhan", "Vishnu Vardhan AIDS"),
        ("placement info", "Placement Info - CSE '27"),
        ("Dady", "Daddy"),
        ("nobody here", None),
    ],
)
def test_best_chat(spoken: str, title) -> None:
    found = best_chat(ROWS, spoken)
    assert (found.title if found else None) == title


@pytest.mark.parametrize(
    ("text", "args"),
    [
        ("Open chat with Daddy.", {"action": "open_chat", "contact": "Daddy"}),
        ("Open Daddy's chat on WhatsApp", {"action": "open_chat", "contact": "Daddy"}),
        ("Any new WhatsApp messages?", {"action": "unread"}),
        (
            "Send hi to Daddy on WhatsApp.",
            {"action": "send", "contact": "Daddy", "message": "hi"},
        ),
        (
            "WhatsApp Daddy saying I will be late, sorry.",
            {"action": "send", "contact": "Daddy", "message": "I will be late, sorry"},
        ),
        (
            "Read the latest messages from Vishnu on WhatsApp",
            {"action": "read", "contact": "Vishnu"},
        ),
    ],
)
def test_whatsapp_fast_path(text: str, args: dict) -> None:
    intent = match(text)
    assert intent is not None and (intent.tool, intent.args) == ("whatsapp", args)


def test_open_a_chat_is_not_a_contact() -> None:
    intent = match("Open the chat", can_open=lambda target: False)
    assert intent is None


def test_sending_is_always_confirmed() -> None:
    question = server.confirmation_question(
        "whatsapp", {"action": "send", "contact": "Daddy", "message": "hi"}
    )
    assert question == "Send “hi” to Daddy on WhatsApp?"
    assert server.confirmation_question("whatsapp", {"action": "open_chat"}) is None
    assert server.confirmation_question("email", {"action": "send"})
    assert server.confirmation_question("email", {"action": "compose"}) is None


def test_gmail_compose_link_is_prefilled() -> None:
    url = mail.compose_url(
        "prof@cbit.ac.in, Priya", "Leave request", "Dear Sir,\nLeave tomorrow & Friday."
    )
    query = parse_qs(urlparse(url).query)
    assert query["view"] == ["cm"] and query["to"] == ["prof@cbit.ac.in"]
    assert query["su"] == ["Leave request"]
    assert query["body"] == ["Dear Sir,\nLeave tomorrow & Friday."]
    assert mail.compose_url("a@b.co", "Hi", "x", client="default") == (
        "mailto:a@b.co?subject=Hi&body=x"
    )


def test_compose_reports_missing_addresses(monkeypatch) -> None:
    opened = []
    monkeypatch.setattr(mail.os, "startfile", opened.append, raising=False)
    reply = mail.compose("Priya", "Hi", "Hello")
    assert opened and "no recipient yet" in reply and "Priya" in reply
    with pytest.raises(Exception):
        mail.compose("a@b.co", "", "")


def test_typing_into_the_address_bar_is_refused(monkeypatch) -> None:
    from openjarvis.desktop import screen, winapi

    typed = []
    monkeypatch.setattr(server, "_bring_app_forward", lambda **k: None)
    monkeypatch.setattr(server, "_foreground_title", lambda: "Untitled - Notepad")
    monkeypatch.setattr(
        screen, "focused_control", lambda: ("EditControl", "Address and search bar")
    )
    monkeypatch.setattr(winapi, "type_text", typed.append)
    with pytest.raises(winapi.DesktopError, match="address bar"):
        server.keyboard("type", "Subject: Sale")
    assert typed == []
    monkeypatch.setattr(screen, "focused_control", lambda: ("EditControl", "Subject"))
    assert server.keyboard("type", "Hello") == "Typed it into 'Untitled - Notepad'."
    assert typed == ["Hello"]


def test_key_presses_report_the_window_that_came_up(monkeypatch) -> None:
    from openjarvis.desktop import winapi

    titles = iter(["hello.py - Visual Studio Code", "Save As"])
    monkeypatch.setattr(server, "_bring_app_forward", lambda **k: None)
    monkeypatch.setattr(server, "_foreground_title", lambda: next(titles))
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    monkeypatch.setattr(winapi, "press_hotkey", lambda combo, times=1: None)
    # The agent learns ctrl+s opened a dialog instead of assuming it saved.
    assert server.keyboard("hotkey", "ctrl+s") == (
        "Pressed ctrl+s. The window in front is now 'Save As'."
    )


def test_screen_questions_default_to_the_request() -> None:
    from openjarvis.desktop.graph import with_defaults

    args = with_defaults("screen", {"action": "describe"}, "which chat has 3 messages?")
    assert args["query"] == "which chat has 3 messages?"
    kept = with_defaults("screen", {"action": "describe", "query": "x"}, "y")
    assert kept["query"] == "x"
    assert with_defaults("open", {"target": "x"}, "y") == {"target": "x"}


def test_no_source_file_contains_control_characters() -> None:
    # A shell-escaping slip once turned a regex's \\b into a backspace,
    # silently breaking yes/no confirmation.
    root = Path(__file__).resolve().parents[2] / "src" / "openjarvis"
    bad = [
        str(path)
        for path in root.rglob("*.py")
        if any(
            ord(c) < 32 and c not in "\n\r\t\f"
            for c in path.read_text(encoding="utf-8")
        )
    ]
    assert bad == []
