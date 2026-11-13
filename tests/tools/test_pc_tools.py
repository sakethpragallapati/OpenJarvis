"""Agent-facing PC, clipboard and reminder tools."""

from __future__ import annotations

import pytest

import openjarvis.tools  # noqa: F401 -- registers built-in tools
from openjarvis.core.registry import ToolRegistry
from openjarvis.pc import control
from openjarvis.tools.pc_tools import (
    CancelReminderTool,
    ClipboardTool,
    ListRemindersTool,
    LockScreenTool,
    PCControlTool,
    SetReminderTool,
)


@pytest.fixture(autouse=True)
def _register():
    for name, cls in (
        ("pc_control", PCControlTool),
        ("lock_screen", LockScreenTool),
        ("clipboard", ClipboardTool),
        ("set_reminder", SetReminderTool),
        ("list_reminders", ListRemindersTool),
        ("cancel_reminder", CancelReminderTool),
    ):
        if not ToolRegistry.contains(name):
            ToolRegistry.register_value(name, cls)


def test_tools_are_registered() -> None:
    for name in ("pc_control", "lock_screen", "clipboard", "set_reminder"):
        assert ToolRegistry.contains(name)


def test_lock_screen_needs_confirmation() -> None:
    assert LockScreenTool().spec.requires_confirmation is True
    assert PCControlTool().spec.requires_confirmation is False


def test_open_app_dispatch(monkeypatch) -> None:
    monkeypatch.setattr(control, "open_app", lambda name: "Visual Studio Code")
    result = PCControlTool().execute(action="open_app", name="vs code")
    assert result.success and result.content == "Opened Visual Studio Code."


def test_set_volume_requires_level() -> None:
    result = PCControlTool().execute(action="set_volume")
    assert not result.success and "level" in result.content


def test_control_errors_become_failed_results(monkeypatch) -> None:
    def missing(name):
        raise control.PCControlError("I couldn't find an app called 'zzz'")

    monkeypatch.setattr(control, "open_app", missing)
    result = PCControlTool().execute(action="open_app", name="zzz")
    assert not result.success and "couldn't find" in result.content


def test_unknown_action_lists_valid_ones() -> None:
    result = PCControlTool().execute(action="format_disk")
    assert not result.success and "open_app" in result.content


def test_clipboard_read_truncates(monkeypatch) -> None:
    monkeypatch.setattr(control, "read_clipboard", lambda: "x" * 9000)
    result = ClipboardTool().execute(action="read")
    assert result.success and result.content.endswith("[... truncated]")


def test_clipboard_write(monkeypatch) -> None:
    written = []
    monkeypatch.setattr(control, "write_clipboard", written.append)
    result = ClipboardTool().execute(action="write", text="hello")
    assert result.success and written == ["hello"]


def test_reminder_round_trip() -> None:
    created = SetReminderTool().execute(message="drink water", in_minutes=15)
    assert created.success, created.content
    reminder_id = created.content.rsplit("(id ", 1)[1].rstrip(").")

    listed = ListRemindersTool().execute()
    assert "drink water" in listed.content

    assert CancelReminderTool().execute(id=reminder_id).success
    assert ListRemindersTool().execute().content == "No upcoming reminders."


def test_bad_reminder_time_is_reported() -> None:
    result = SetReminderTool().execute(message="x", at="whenever")
    assert not result.success and "Could not set reminder" in result.content
