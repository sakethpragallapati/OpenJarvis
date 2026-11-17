"""System alarms: parsing, repeats, Task Scheduler XML, ringing, import/export."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from openjarvis.desktop import alarms
from openjarvis.desktop.alarms import Alarm, AlarmError, parse_time

NOW = datetime(2026, 11, 13, 20, 0)  # a Friday, 8 pm


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("7 am", datetime(2026, 11, 14, 7, 0)),
        ("7:30 pm", datetime(2026, 11, 13, 19, 30) + timedelta(days=1)),
        ("21:15", datetime(2026, 11, 13, 21, 15)),
        ("9", datetime(2026, 11, 13, 21, 0)),  # next 9 o'clock: tonight
        ("6:30 tomorrow morning", datetime(2026, 11, 14, 6, 30)),
        ("tomorrow 6 in the evening", datetime(2026, 11, 14, 18, 0)),
        ("noon", datetime(2026, 11, 14, 12, 0)),
        ("in 20 minutes", NOW + timedelta(minutes=20)),
        ("in 2 hours", NOW + timedelta(hours=2)),
        ("2026-12-01T08:00", datetime(2026, 12, 1, 8, 0)),
    ],
)
def test_parse_time(text: str, expected: datetime) -> None:
    assert parse_time(text, now=NOW) == expected


def test_parse_time_minutes_and_errors() -> None:
    assert parse_time(minutes=5, now=NOW) == NOW + timedelta(minutes=5)
    with pytest.raises(AlarmError):
        parse_time("whenever", now=NOW)
    with pytest.raises(AlarmError):
        parse_time("25:00", now=NOW)
    with pytest.raises(AlarmError):
        parse_time(minutes=-1, now=NOW)


@pytest.mark.parametrize(
    ("text", "days", "expected"),
    [
        ("daily", "", ("daily", [])),
        ("every day", "", ("daily", [])),
        ("weekly", "", ("weekly", [])),
        ("annually", "", ("yearly", [])),
        ("", "", ("once", [])),
        ("mon,wed,fri", "", ("custom", ["Monday", "Wednesday", "Friday"])),
        ("every tuesday and thursday", "", ("custom", ["Tuesday", "Thursday"])),
        ("on saturdays", "", ("custom", ["Saturday"])),
        ("custom", ["Sun", "Mon"], ("custom", ["Monday", "Sunday"])),
        ("", "mon tue wed thu fri", ("weekdays", [])),
        ("sat and sun", "", ("weekends", [])),
    ],
)
def test_parse_repeat(text, days, expected) -> None:
    assert alarms.parse_repeat(text, days) == expected


def test_parse_repeat_rejects_nonsense() -> None:
    with pytest.raises(AlarmError):
        alarms.parse_repeat("fortnightly")
    with pytest.raises(AlarmError):
        alarms.parse_repeat("custom")


def _alarm(**kw) -> Alarm:
    base = dict(id="ab12c", kind="alarm", label="gym", when="2026-11-14T06:30:00")
    base.update(kw)
    return Alarm(**base)


def test_task_xml_once_wakes_and_reports_missed() -> None:
    xml = alarms.task_xml(_alarm(), command=r"C:\py\pythonw.exe", arguments="-m x")
    assert "<StartBoundary>2026-11-14T06:30:00</StartBoundary>" in xml
    assert "<EndBoundary>2026-11-14T18:30:00</EndBoundary>" in xml
    assert "<DeleteExpiredTaskAfter>" in xml
    assert "<WakeToRun>true</WakeToRun>" in xml
    # Runs late after sleep/shutdown so it can say "missed".
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml
    assert r"<Command>C:\py\pythonw.exe</Command>" in xml
    off = alarms.task_xml(_alarm(enabled=False), command="p", arguments="a")
    assert "<Enabled>false</Enabled>\n  </Settings>" in off


def test_task_xml_weekly_style_repeats() -> None:
    daily = alarms.task_xml(_alarm(repeat="daily"), command="p", arguments="a")
    assert "<ScheduleByDay>" in daily and "<EndBoundary>" not in daily
    weekdays = alarms.task_xml(_alarm(repeat="weekdays"), command="p", arguments="a")
    assert "<Monday />" in weekdays and "<Saturday />" not in weekdays
    weekly = alarms.task_xml(_alarm(repeat="weekly"), command="p", arguments="a")
    assert "<Saturday />" in weekly and "<Monday />" not in weekly  # 14 Nov
    custom = alarms.task_xml(
        _alarm(repeat="custom", days=["Tuesday", "Sunday"]), command="p", arguments="a"
    )
    assert "<Tuesday /><Sunday />" in custom and "<Saturday />" not in custom


def test_task_xml_monthly_uses_last_day_for_short_months() -> None:
    xml = alarms.task_xml(
        _alarm(repeat="monthly", when="2026-12-31T08:00:00"), command="p", arguments="a"
    )
    assert "<Day>31</Day>" in xml and "<Day>Last</Day>" in xml
    by_day, by_last = xml.split("<Day>Last</Day>")
    assert "<January />" in by_day and "<April />" not in by_day
    assert "<February />" in by_last and "<April />" in by_last
    yearly = alarms.task_xml(
        _alarm(repeat="yearly", when="2028-02-29T09:00:00"), command="p", arguments="a"
    )
    assert "<Day>Last</Day>" in yearly and "<February />" in yearly
    assert "<March />" not in yearly


def test_next_ring_keeps_the_monthly_day() -> None:
    alarm = _alarm(
        repeat="monthly", when="2027-01-31T07:00:00", anchor="2027-01-31T07:00:00"
    )
    feb = alarms._occurrences_after(alarm, datetime(2027, 2, 1))
    assert feb == datetime(2027, 2, 28, 7, 0)
    assert alarms._occurrences_after(alarm, feb) == datetime(2027, 3, 31, 7, 0)
    custom = _alarm(repeat="custom", days=["Monday"], when="2026-11-16T06:00:00")
    assert alarms._occurrences_after(custom, datetime(2026, 11, 16, 7)) == datetime(
        2026, 11, 23, 6, 0
    )


def test_last_due_finds_the_occurrence_going_off() -> None:
    daily = _alarm(repeat="daily", when="2026-11-01T06:30:00")
    assert alarms.last_due(daily, datetime(2026, 11, 13, 9, 0)) == datetime(
        2026, 11, 13, 6, 30
    )
    assert alarms.last_due(_alarm(), datetime(2026, 11, 14, 9)) == datetime(
        2026, 11, 14, 6, 30
    )


def test_toast_xml_styles() -> None:
    xml = alarms.toast_xml(_alarm(label="<b>Tea & cake</b>"), now=NOW)
    assert "&lt;b&gt;Tea &amp; cake&lt;/b&gt;" in xml
    assert 'scenario="alarm"' in xml and 'loop="true"' in xml
    assert 'arguments="snooze"' in xml and 'arguments="dismiss"' in xml
    reminder = alarms.toast_xml(_alarm(kind="reminder"), now=NOW)
    assert 'scenario="reminder"' in reminder and "loop" not in reminder
    warning = alarms.toast_xml(_alarm(advance=10), now=NOW, style="warning")
    assert "Alarm in 10 minutes" in warning and "snooze" not in warning
    missed = alarms.toast_xml(_alarm(), now=datetime(2026, 11, 14, 9), style="missed")
    assert "Missed alarm" in missed and "06:30 AM" in missed
    work = alarms.toast_xml(_alarm(category="work"), now=NOW)
    assert "Work - OpenJarvis" in work


def test_announcement() -> None:
    at = datetime(2026, 11, 14, 6, 30)
    assert alarms.announcement(_alarm(), now=at) == "Alarm. gym. It's 6:30 AM."
    assert alarms.announcement(_alarm(label=""), now=at) == "Alarm. It's 6:30 AM."
    assert alarms.announcement(_alarm(kind="timer", label=""), now=at) == (
        "Your timer is done."
    )
    assert "In 10 minutes: gym" in alarms.announcement(
        _alarm(advance=10), now=at, style="warning"
    )


@pytest.fixture
def fake_scheduler(monkeypatch, tmp_path: Path):
    calls = []

    def fake(*args: str):
        calls.append(args)
        return subprocess.CompletedProcess(["schtasks", *args], 0, "", "")

    monkeypatch.setattr(alarms, "_schtasks", fake)
    monkeypatch.setattr(alarms.sys, "platform", "win32")
    monkeypatch.setattr(alarms, "_store_path", lambda: tmp_path / "alarms.json")
    monkeypatch.setattr(alarms, "_run_ps", lambda script, env, timeout: True)
    return calls


def test_schedule_list_cancel(fake_scheduler) -> None:
    when = datetime.now() + timedelta(hours=1)
    alarm = alarms.schedule("alarm", when, "gym")
    assert fake_scheduler[0][:2] == ("/Create", "/XML")
    assert fake_scheduler[0][3:5] == ("/TN", alarm.task_name)
    assert alarm.anchor == alarm.when and alarm.voice == "male"
    assert [a.id for a in alarms.upcoming()] == [alarm.id]
    gone = alarms.cancel("gym")
    assert [a.id for a in gone] == [alarm.id]
    assert fake_scheduler[-1] == ("/Delete", "/TN", alarm.task_name, "/F")
    assert alarms.upcoming() == []


def test_cancel_all_and_past_times(fake_scheduler) -> None:
    soon = datetime.now() + timedelta(minutes=30)
    alarms.schedule("timer", soon)
    alarms.schedule("reminder", soon, "call mom")
    assert len(alarms.cancel("all")) == 2
    with pytest.raises(AlarmError):
        alarms.schedule("alarm", datetime.now() - timedelta(hours=1))


def test_weekday_alarm_moves_to_a_weekday(fake_scheduler) -> None:
    today = datetime.now().replace(hour=23, minute=59, second=0, microsecond=0)
    saturday = today + timedelta(days=(5 - today.weekday()) % 7)
    if saturday <= datetime.now():
        saturday += timedelta(days=7)
    alarm = alarms.schedule("alarm", saturday, repeat="weekdays")
    assert alarm.at.strftime("%A") == "Monday"
    custom = alarms.schedule("alarm", saturday, days="wed")
    assert custom.repeat == "custom" and custom.at.strftime("%A") == "Wednesday"


def test_early_warning_task_and_cleanup(fake_scheduler) -> None:
    alarm = alarms.schedule(
        "alarm", datetime.now() + timedelta(hours=2), "meeting", advance=10
    )
    names = [call[4] for call in fake_scheduler if call[0] == "/Create"]
    assert names == [alarm.task_name, alarm.warning_task_name]
    alarms.cancel(alarm.id)
    deleted = [call[2] for call in fake_scheduler if call[0] == "/Delete"]
    assert deleted == [alarm.task_name, alarm.warning_task_name]
    # A warning that would already be in the past is skipped.
    fake_scheduler.clear()
    alarms.schedule("alarm", datetime.now() + timedelta(minutes=3), advance=10)
    assert [c[4] for c in fake_scheduler if c[0] == "/Create"][1:] == []


def test_turn_off_and_on(fake_scheduler) -> None:
    alarm = alarms.schedule("alarm", datetime.now() + timedelta(hours=1), "gym")
    assert [a.id for a in alarms.set_enabled("gym", False)] == [alarm.id]
    assert fake_scheduler[-1] == ("/Change", "/TN", alarm.task_name, "/DISABLE")
    assert alarms.all_alarms()[0].enabled is False
    assert "(off)" in alarms.all_alarms()[0].describe()
    alarms.set_enabled("all", True)
    assert alarms.all_alarms()[0].enabled is True


def test_fire_rings_and_drops_one_off(fake_scheduler, monkeypatch) -> None:
    shown = []
    monkeypatch.setattr(
        alarms, "show_toast", lambda a, **kw: shown.append((a.id, kw["style"])) or True
    )
    alarm = alarms.schedule("alarm", datetime.now() + timedelta(minutes=5), "wake")
    assert alarms.main(["fire", alarm.id]) == 0
    assert shown == [(alarm.id, "ring")] and alarms.upcoming() == []
    assert alarms.main(["fire", "nope"]) == 1


def test_fire_late_only_reports_missed(fake_scheduler, monkeypatch) -> None:
    shown = []
    monkeypatch.setattr(
        alarms, "show_toast", lambda a, **kw: shown.append(kw["style"]) or True
    )
    alarm = alarms.schedule("alarm", datetime.now() + timedelta(minutes=5), "wake")
    later = datetime.now() + timedelta(hours=1)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return later

    monkeypatch.setattr(alarms, "datetime", Clock)
    alarms.fire(alarm.id)
    assert shown == ["missed"]


def test_snooze_rings_again_later(fake_scheduler, monkeypatch) -> None:
    assert alarms.snooze() is None  # nothing ringing
    alarm = alarms.schedule("alarm", datetime.now() + timedelta(minutes=1), "tea")
    alarms._mark_ringing(alarm)
    assert alarms.ringing().id == alarm.id
    again = alarms.snooze(10)
    assert again.label == "tea" and again.snoozes == 1
    assert timedelta(minutes=9) < again.at - datetime.now() <= timedelta(minutes=10)
    assert alarms.ringing() is None


def test_export_and_import_roundtrip(fake_scheduler, tmp_path: Path) -> None:
    alarms.schedule(
        "alarm",
        datetime.now() + timedelta(hours=3),
        "standup",
        days="mon,thu",
        category="work",
        voice="female",
    )
    json_file, csv_file = tmp_path / "out.json", tmp_path / "out.csv"
    assert alarms.export(json_file) == 1 and alarms.export(csv_file) == 1
    assert "standup" in csv_file.read_text(encoding="utf-8")
    assert alarms.import_file(json_file) == (0, 1)  # already set
    alarms.cancel("all")
    assert alarms.import_file(csv_file) == (1, 0)
    (back,) = alarms.all_alarms()
    assert (back.label, back.repeat, back.days, back.category, back.voice) == (
        "standup",
        "custom",
        ["Monday", "Thursday"],
        "work",
        "female",
    )


_ALARM_PRO_XML = """<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">
  <Obj RefId="0">
    <TN RefId="0"><T>System.Management.Automation.PSCustomObject</T></TN>
    <MS>
      <S N="Id">5f0c</S>
      <S N="Name">Take tablets</S>
      <DT N="DateTime">{when}</DT>
      <S N="Recurrence">Custom Days</S>
      <S N="Voice">IGRF-Saraswathi (Female)</S>
      <I32 N="RingDuration">30</I32>
      <B N="AdvanceNotification">true</B>
      <Obj N="SelectedDays" RefId="1"><TNRef RefId="1" />
        <LST><S>Mon</S><S>Fri</S></LST></Obj>
      <B N="Alerted">false</B>
      <B N="Enabled">true</B>
      <Nil N="CustomSoundPath" />
    </MS>
  </Obj>
  <Obj RefId="2">
    <TNRef RefId="0" />
    <MS>
      <S N="Name">Old one-off</S>
      <DT N="DateTime">2020-01-01T07:00:00</DT>
      <S N="Recurrence">One Time</S>
      <B N="Alerted">true</B>
    </MS>
  </Obj>
</Objs>"""


def test_import_windows_alarm_pro(fake_scheduler, tmp_path: Path) -> None:
    when = (datetime.now() + timedelta(days=2)).replace(microsecond=0)
    source = tmp_path / "alarms.xml"
    source.write_text(
        _ALARM_PRO_XML.format(when=when.isoformat() + ".0000000"), encoding="utf-8"
    )
    rows = alarms.read_alarm_pro(source)
    assert rows[0]["days"] == ["Mon", "Fri"] and rows[0]["voice"] == "female"
    assert rows[0]["advance"] == 10 and rows[1]["done"] is True
    assert alarms.import_file(source) == (1, 1)
    (alarm,) = alarms.all_alarms()
    assert alarm.label == "Take tablets" and alarm.repeat == "custom"
    assert alarm.days == ["Monday", "Friday"] and alarm.ring == 30


def test_old_store_entries_still_load(tmp_path: Path) -> None:
    store = tmp_path / "alarms.json"
    store.write_text(
        json.dumps(
            [
                {
                    "id": "a",
                    "kind": "alarm",
                    "label": "",
                    "when": "2030-01-01T07:00:00",
                    "repeat": "daily",
                    "created": "",
                    "future_field": 1,
                },
                "junk",
            ]
        ),
        encoding="utf-8",
    )
    (alarm,) = alarms._load(store)
    assert alarm.repeat == "daily" and alarm.voice == "male" and alarm.enabled


def test_alarm_cli(fake_scheduler) -> None:
    from openjarvis.cli.alarm_cmd import alarm as alarm_cli

    runner = CliRunner()
    result = runner.invoke(
        alarm_cli,
        ["add", "11:59", "pm", "-l", "Pills", "-r", "mon,wed", "-c", "medication"],
    )
    assert result.exit_code == 0, result.output
    assert "Pills" in result.output and "every Mon, Wed" in result.output
    listing = runner.invoke(alarm_cli, [])
    assert "Now:" in listing.output and "Next: alarm 'Pills'" in listing.output
    assert runner.invoke(alarm_cli, ["off", "pills"]).exit_code == 0
    assert "(medication, off)" in runner.invoke(alarm_cli, ["list"]).output
    assert runner.invoke(alarm_cli, ["snooze"]).exit_code != 0  # nothing ringing
    bad = runner.invoke(alarm_cli, ["add", "7am", "-r", "fortnightly"])
    assert bad.exit_code != 0 and "repeat" in bad.output
    assert "Cancelled" in runner.invoke(alarm_cli, ["cancel", "all"]).output
