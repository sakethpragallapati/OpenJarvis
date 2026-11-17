"""Fast path: which spoken commands skip the model, and with what call."""

from __future__ import annotations

import pytest

from openjarvis.desktop.fastpath import match, normalize


@pytest.mark.parametrize(
    ("text", "tool", "args"),
    [
        ("What time is it?", "system", {"action": "time"}),
        ("How much battery is left?", "system", {"action": "battery"}),
        ("Volume to 30.", "system", {"action": "set_volume", "level": 30}),
        ("Set the volume to 150%", "system", {"action": "set_volume", "level": 100}),
        ("Turn the volume up", "system", {"action": "volume_up"}),
        ("Mute", "system", {"action": "mute"}),
        ("Pause the music.", "system", {"action": "play_pause"}),
        ("Next song", "system", {"action": "next_track"}),
        ("Brightness 60", "system", {"action": "set_brightness", "level": 60}),
        ("Lock the screen", "system", {"action": "lock"}),
        ("Shut down the computer", "system", {"action": "shutdown"}),
        ("Take a screenshot", "system", {"action": "screenshot"}),
        ("New tab", "keyboard", {"action": "hotkey", "text": "ctrl+t"}),
        ("Scroll down", "mouse", {"action": "scroll_down", "amount": 5}),
        (
            "Press delete",
            "keyboard",
            {"action": "hotkey", "text": "delete", "times": 1},
        ),
        (
            "hit enter twice",
            "keyboard",
            {"action": "hotkey", "text": "enter", "times": 2},
        ),
        ("Close Chrome", "window", {"action": "close", "name": "chrome"}),
        ("Show the desktop", "window", {"action": "show_desktop"}),
        (
            "Play lofi hip hop on YouTube.",
            "play_youtube",
            {"query": "lofi hip hop"},
        ),
        (
            "Search YouTube for vision quiz",
            "browser_search",
            {"query": "vision quiz", "site": "youtube"},
        ),
        (
            "Open YouTube and search for vision quiz.",
            "browser_search",
            {"query": "vision quiz", "site": "youtube"},
        ),
        (
            "search for cheap flights on google",
            "browser_search",
            {"query": "cheap flights", "site": "google"},
        ),
        (
            "Set a timer for 5 minutes.",
            "schedule",
            {"action": "timer", "minutes": 5.0},
        ),
        (
            "Set an alarm for 6:30 tomorrow morning called gym.",
            "schedule",
            {"action": "alarm", "time": "6:30 tomorrow morning", "label": "gym"},
        ),
        (
            "Set an alarm for 7 am every day",
            "schedule",
            {"action": "alarm", "time": "7 am", "repeat": "daily"},
        ),
        (
            "Set a medication alarm for 9 pm every monday and friday",
            "schedule",
            {
                "action": "alarm",
                "time": "9 pm",
                "days": "monday and friday",
                "category": "medication",
            },
        ),
        (
            "Alarm for 8 am every month called rent",
            "schedule",
            {"action": "alarm", "time": "8 am", "repeat": "monthly", "label": "rent"},
        ),
        (
            "Wake me up at 6 on weekdays",
            "schedule",
            {"action": "alarm", "time": "6", "repeat": "weekdays"},
        ),
        ("Stop the alarm", "schedule", {"action": "stop"}),
        ("Snooze for 10 minutes", "schedule", {"action": "snooze", "minutes": 10.0}),
        ("Snooze", "schedule", {"action": "snooze"}),
        ("What alarms do I have?", "schedule", {"action": "list"}),
        (
            "Remind me to drink water in 20 minutes.",
            "schedule",
            {"action": "reminder", "time": "in 20 minutes", "label": "drink water"},
        ),
        (
            "Remember that my server uses port 25565",
            "memory",
            {"action": "remember", "text": "my server uses port 25565"},
        ),
    ],
)
def test_direct_commands(text: str, tool: str, args: dict) -> None:
    intent = match(text)
    assert intent is not None, text
    assert (intent.tool, intent.args) == (tool, args)


@pytest.mark.parametrize(
    "text",
    [
        "Open notepad and type hello",
        "Please open Google and search for biryani, then open the first result",
        "What's on my screen?",
        "We'll see you in the next one.",
        "Tell me a joke",
        "press a",  # a lone letter is too likely to be mis-heard
        "Set an alarm",  # no time: the agent asks
    ],
)
def test_left_to_the_agent(text: str) -> None:
    assert match(text, can_open=lambda target: True) is None


def test_open_needs_a_confident_resolution() -> None:
    assert match("Open Task Manager.") is None  # no resolver given
    assert match("Open Task Manager.", can_open=lambda t: False) is None
    intent = match("Please open the Minecraft server folder.", can_open=lambda t: True)
    assert intent is not None and intent.tool == "open"
    assert intent.args == {"target": "the Minecraft server folder"}
    assert intent.fallback_on_error


def test_open_browser_and_go_to_site() -> None:
    intent = match("Open chrome and go to gmail.")
    assert intent is not None
    assert (intent.tool, intent.args) == (
        "open",
        {"target": "gmail", "kind": "website"},
    )


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Hey Jarvis, please open file manager for me.", "open file manager"),
        ("Can you pause the music?", "pause the music"),
        ("  Now go to the downloads folder. ", "go to the downloads folder"),
    ],
)
def test_normalize_strips_politeness(raw: str, clean: str) -> None:
    assert normalize(raw) == clean
