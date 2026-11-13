"""Reminder parsing, persistence and delivery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from openjarvis.pc.reminders import ReminderStore, ReminderWatcher, parse_when

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


def test_relative_minutes() -> None:
    assert parse_when(in_minutes=20, now=NOW) == NOW + timedelta(minutes=20)


def test_clock_time_later_today_and_tomorrow() -> None:
    local_now = NOW.astimezone()
    later = (local_now + timedelta(hours=1)).strftime("%H:%M")
    earlier = (local_now - timedelta(hours=1)).strftime("%H:%M")

    due_later = parse_when(at=later, now=NOW).astimezone()
    due_earlier = parse_when(at=earlier, now=NOW).astimezone()

    assert timedelta(0) < due_later - local_now <= timedelta(hours=1)
    assert timedelta(hours=22) < due_earlier - local_now <= timedelta(hours=23)


@pytest.mark.parametrize(("text", "hour"), [("7 pm", 19), ("7:30am", 7), ("12am", 0)])
def test_am_pm_clock_times(text, hour) -> None:
    assert parse_when(at=text, now=NOW).astimezone().hour == hour


def test_iso_datetime() -> None:
    due = parse_when(at="2026-11-14T09:00:00+00:00", now=NOW)
    assert due == datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"in_minutes": 0},
        {"at": "25:00"},
        {"at": "teatime"},
        {"at": "2020-01-01T00:00:00+00:00"},
        {"in_minutes": 60 * 24 * 400},
    ],
)
def test_invalid_times_are_rejected(kwargs) -> None:
    with pytest.raises(ValueError):
        parse_when(now=NOW, **kwargs)


def test_store_add_list_cancel(tmp_path) -> None:
    store = ReminderStore(tmp_path / "reminders.json")
    first = store.add("stretch", NOW + timedelta(minutes=5))
    store.add("call mum", NOW + timedelta(minutes=1))

    assert [r.message for r in store.pending()] == ["call mum", "stretch"]
    assert store.cancel(first.id) is True
    assert store.cancel(first.id) is False
    assert [r.message for r in ReminderStore(store.path).pending()] == ["call mum"]


def test_empty_message_is_rejected(tmp_path) -> None:
    with pytest.raises(ValueError):
        ReminderStore(tmp_path / "r.json").add("   ", NOW)


def test_due_reminders_fire_exactly_once(tmp_path) -> None:
    store = ReminderStore(tmp_path / "reminders.json")
    store.add("tea", NOW - timedelta(seconds=1))
    store.add("later", NOW + timedelta(hours=1))

    assert [r.message for r in store.take_due(NOW)] == ["tea"]
    assert store.take_due(NOW) == []
    assert [r.message for r in store.pending()] == ["later"]


def test_unreadable_file_is_ignored(tmp_path) -> None:
    path = tmp_path / "reminders.json"
    path.write_text("{not json", encoding="utf-8")
    assert ReminderStore(path).pending() == []


def test_watcher_delivers_due_reminders(tmp_path) -> None:
    store = ReminderStore(tmp_path / "reminders.json")
    store.add("stand up", datetime.now(timezone.utc) - timedelta(seconds=1))
    delivered = []

    watcher = ReminderWatcher(delivered.append, store=store)

    assert watcher.check_now() == 1
    assert [r.message for r in delivered] == ["stand up"]


def test_watcher_survives_delivery_errors(tmp_path) -> None:
    store = ReminderStore(tmp_path / "reminders.json")
    store.add("x", datetime.now(timezone.utc) - timedelta(seconds=1))

    def boom(reminder):
        raise RuntimeError("notification service down")

    assert ReminderWatcher(boom, store=store).check_now() == 1
