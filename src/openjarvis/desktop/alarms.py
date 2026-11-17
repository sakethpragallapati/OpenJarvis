"""Real Windows alarms, timers and reminders (Task Scheduler + alarm toasts).

Each alarm is a Task Scheduler task under ``\\OpenJarvis\\`` that runs
``pythonw -m openjarvis.desktop.alarms fire <id>`` at the set time. That
announces the alarm out loud (Windows voices) and shows a Windows alarm
notification - looping alarm sound, Snooze and Dismiss buttons - so it rings
even when Jarvis is not running, and the task may wake the laptop from sleep.

The feature set follows Windows Alarm Pro (github.com/ponsivs/Windows-Alarm-Pro),
but as a CLI/API instead of a GUI: once/daily/weekly/weekdays/weekends/
monthly/yearly/custom-day repeats, categories, male/female/no voice, a ring
duration, an optional early warning, snooze, missed-alarm notices,
enable/disable, import/export (including Alarm Pro's own ``alarms.xml``) and a
rotating log. ``jarvis alarm --help`` is the command line.

The alarm list lives in ``~/.openjarvis/alarms.json``; the scheduled task is
what makes it ring.
"""

from __future__ import annotations

import base64
import calendar
import csv
import json
import logging
import logging.handlers
import os
import re
import subprocess
import sys
import tempfile
import threading
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape

logger = logging.getLogger(__name__)

TASK_FOLDER = "\\OpenJarvis\\"
REPEATS = (
    "once",
    "daily",
    "weekly",
    "weekdays",
    "weekends",
    "monthly",
    "yearly",
    "custom",
)
KINDS = ("alarm", "timer", "reminder")
CATEGORIES = (
    "general",
    "work",
    "personal",
    "meeting",
    "medication",
    "birthday",
    "reminder",
)
VOICES = ("male", "female", "none")
WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
_DAYS = {
    "weekdays": WEEKDAYS[:5],
    "weekends": WEEKDAYS[5:],
}
_DAY_NAMES = {
    **{d.lower(): d for d in WEEKDAYS},
    **{d[:3].lower(): d for d in WEEKDAYS},
    "tues": "Tuesday",
    "weds": "Wednesday",
    "thur": "Thursday",
    "thurs": "Thursday",
}
# An alarm the laptop slept or was off through rings this late at most;
# after that it only leaves a quiet "missed" notification.
MISSED_AFTER = timedelta(minutes=15)
_CREATE_NO_WINDOW = 0x08000000


class AlarmError(RuntimeError):
    """User-facing failure to set or cancel an alarm."""


@dataclass(slots=True)
class Alarm:
    id: str
    kind: str
    label: str
    when: str  # local ISO datetime of the (next) ring
    repeat: str = "once"
    created: str = ""
    days: List[str] = field(default_factory=list)  # for repeat == "custom"
    category: str = "general"
    voice: str = "male"  # spoken announcement: male, female or none
    advance: int = 0  # minutes of early warning, 0 = none
    ring: int = 0  # seconds to ring, 0 = until dismissed
    enabled: bool = True
    anchor: str = ""  # first ring; monthly/yearly repeats keep its day
    snoozes: int = 0

    @property
    def at(self) -> datetime:
        return datetime.fromisoformat(self.when)

    @property
    def first(self) -> datetime:
        return datetime.fromisoformat(self.anchor or self.when)

    @property
    def task_name(self) -> str:
        return f"{TASK_FOLDER}Jarvis {self.kind} {self.id}"

    @property
    def warning_task_name(self) -> str:
        return f"{self.task_name} warning"

    def ring_days(self) -> Tuple[str, ...]:
        """Weekdays a weekly-style repeat rings on (empty for the others)."""
        if self.repeat in _DAYS:
            return _DAYS[self.repeat]
        if self.repeat == "custom":
            return tuple(d for d in WEEKDAYS if d in self.days)
        if self.repeat == "weekly":
            return (self.first.strftime("%A"),)
        return ()

    def repeat_text(self) -> str:
        if self.repeat == "custom":
            return "every " + ", ".join(d[:3] for d in self.ring_days())
        if self.repeat == "weekly":
            return f"every {self.first:%A}"
        if self.repeat == "monthly":
            return f"monthly on day {self.first.day}"
        if self.repeat == "yearly":
            return f"yearly on {self.first:%d %b}"
        return self.repeat

    def describe(self) -> str:
        at = self.at
        if self.repeat == "once":
            day = "today" if at.date() == datetime.now().date() else f"{at:%a %d %b}"
        else:
            day = self.repeat_text()
        label = f" - {self.label}" if self.label else ""
        extras = []
        if self.category not in ("", "general", self.kind):
            extras.append(self.category)
        if self.advance:
            extras.append(f"{self.advance} min warning")
        if not self.enabled:
            extras.append("off")
        tail = f" ({', '.join(extras)})" if extras else ""
        return f"[{self.id}] {self.kind} {at:%I:%M %p} {day}{label}{tail}"


_FIELDS = {f.name for f in fields(Alarm)}


def _store_path() -> Path:
    from openjarvis.core.paths import get_config_dir

    return get_config_dir() / "alarms.json"


_LOCK = threading.Lock()


def _load(path: Optional[Path] = None) -> List[Alarm]:
    path = path or _store_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return []
    alarms = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            alarms.append(Alarm(**{k: v for k, v in item.items() if k in _FIELDS}))
        except TypeError:
            continue
    return alarms


def _save(alarms: List[Alarm], path: Optional[Path] = None) -> None:
    path = path or _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump([asdict(a) for a in alarms], handle, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Log (rotates at 5 MB, like Alarm Pro's)
# ---------------------------------------------------------------------------

_LOG = logging.getLogger("openjarvis.alarms.history")
_LOG_READY = False


def _history(message: str) -> None:
    global _LOG_READY
    if not _LOG_READY:
        _LOG_READY = True
        try:
            from openjarvis.core.paths import get_config_dir

            log_dir = get_config_dir() / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                log_dir / "alarms.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            _LOG.addHandler(handler)
            _LOG.setLevel(logging.INFO)
            _LOG.propagate = False
        except OSError:
            pass
    _LOG.info(message)


def log_path() -> Path:
    from openjarvis.core.paths import get_config_dir

    return get_config_dir() / "logs" / "alarms.log"


# ---------------------------------------------------------------------------
# Time and repeat parsing
# ---------------------------------------------------------------------------

_CLOCK = re.compile(
    r"(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?m\.?|p\.?m\.?|(?:in the )?morning|"
    r"(?:in the )?(?:evening|afternoon)|(?:at )?night|tonight)?",
    re.IGNORECASE,
)
_PM_WORDS = (
    "pm",
    "intheevening",
    "evening",
    "intheafternoon",
    "afternoon",
    "atnight",
    "night",
    "tonight",
)
_AM_WORDS = ("am", "inthemorning", "morning")


def parse_time(
    text: str = "",
    *,
    minutes: Optional[float] = None,
    now: Optional[datetime] = None,
) -> datetime:
    """'7 am', '7:30 pm tomorrow', '19:00', ISO, or ``minutes`` from now."""
    now = now or datetime.now()
    if minutes is not None and str(minutes) != "":
        delta = float(minutes)
        if delta <= 0:
            raise AlarmError("The timer length must be positive.")
        return now + timedelta(minutes=delta)
    text = (text or "").strip().lower()
    if not text:
        raise AlarmError("Tell me when, like '7 am' or 'in 10 minutes'.")
    rel = re.search(
        r"in\s+(\d+(?:\.\d+)?)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?)", text
    )
    if rel:
        value = float(rel.group(1))
        unit = rel.group(2)
        factor = 1 / 60 if unit.startswith("s") else 60 if unit.startswith("h") else 1
        return now + timedelta(minutes=value * factor)
    try:
        parsed = datetime.fromisoformat(text.upper().replace("Z", ""))
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        pass
    if "noon" in text:
        hour, minute, suffix = 12, 0, ""
    elif "midnight" in text:
        hour, minute, suffix = 0, 0, ""
    else:
        match = _CLOCK.search(text)
        if not match:
            raise AlarmError(f"I couldn't understand the time {text!r}.")
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        suffix = (match.group(3) or "").replace(".", "").replace(" ", "").lower()
        if not suffix:  # "6:30 tomorrow morning": the word comes later
            for word in ("morning", "evening", "afternoon", "night", "tonight"):
                if re.search(rf"\b{word}\b", text):
                    suffix = word
                    break
    if suffix in _PM_WORDS and hour < 12:
        hour += 12
    elif suffix in _AM_WORDS and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        raise AlarmError(f"{text!r} isn't a valid time.")
    due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if "tomorrow" in text:
        due += timedelta(days=1)
    elif due <= now:
        # "at 7" when it's 8 pm: the user means the next 7 o'clock.
        if not suffix and hour < 12 and due + timedelta(hours=12) > now:
            due += timedelta(hours=12)
        else:
            due += timedelta(days=1)
    return due


def parse_days(text: Iterable[str] | str) -> List[str]:
    """'mon, wed and fri' / ['Mon', 'Wed'] -> ['Monday', 'Wednesday', ...]."""
    if isinstance(text, str):
        words = re.split(r"[\s,;/&+]+|\band\b", text.lower())
    else:
        words = [str(w).lower() for w in text]
    found = set()
    for word in words:
        word = word.strip(" .")
        if word.endswith("s") and word[:-1] in _DAY_NAMES:  # "mondays"
            word = word[:-1]
        if word in ("weekday", "weekdays"):
            found.update(_DAYS["weekdays"])
        elif word in ("weekend", "weekends"):
            found.update(_DAYS["weekends"])
        elif word in _DAY_NAMES:
            found.add(_DAY_NAMES[word])
    return [d for d in WEEKDAYS if d in found]


_REPEAT_WORDS = {
    "once": "once",
    "one time": "once",
    "never": "once",
    "daily": "daily",
    "every day": "daily",
    "everyday": "daily",
    "weekly": "weekly",
    "every week": "weekly",
    "weekdays": "weekdays",
    "every weekday": "weekdays",
    "weekends": "weekends",
    "every weekend": "weekends",
    "monthly": "monthly",
    "every month": "monthly",
    "yearly": "yearly",
    "annually": "yearly",
    "every year": "yearly",
}


def parse_repeat(text: str, days: Iterable[str] | str = ()) -> Tuple[str, List[str]]:
    """``(repeat, days)`` from 'daily', 'every monday and friday', 'mon,wed'..."""
    raw = " ".join((text or "").lower().replace("-", " ").split())
    picked = parse_days(days)
    if not picked:
        if raw in _REPEAT_WORDS:
            return _REPEAT_WORDS[raw], []
        if raw == "":
            return "once", []
        if raw == "custom":
            raise AlarmError("Which days? Say something like 'mon, wed and fri'.")
        picked = parse_days(re.sub(r"\b(?:every|on|each)\b", " ", raw))
    if picked:
        if picked == list(_DAYS["weekdays"]):
            return "weekdays", []
        if picked == list(_DAYS["weekends"]):
            return "weekends", []
        if len(picked) == 7:
            return "daily", []
        return "custom", picked
    raise AlarmError(
        f"I don't know the repeat {text!r}. Try daily, weekly, weekdays, "
        "weekends, monthly, yearly or days like 'mon,wed,fri'."
    )


def _month_day(year: int, month: int, day: int) -> int:
    return min(day, calendar.monthrange(year, month)[1])


def _occurrences_after(alarm: Alarm, after: datetime) -> Optional[datetime]:
    """First ring strictly after ``after`` (None for a one-off in the past)."""
    first = alarm.first
    if alarm.repeat == "once":
        at = alarm.at
        return at if at > after else None
    if first > after:
        return first
    clock = first.time()
    if alarm.repeat in ("monthly", "yearly"):
        year, month = (
            after.year,
            after.month if alarm.repeat == "monthly" else first.month,
        )
        for _ in range(30):
            day = _month_day(year, month, first.day)
            at = datetime.combine(datetime(year, month, day).date(), clock)
            if at > after:
                return at
            if alarm.repeat == "monthly":
                year, month = (year + 1, 1) if month == 12 else (year, month + 1)
            else:
                year += 1
        return None
    days = alarm.ring_days()
    at = datetime.combine(after.date(), clock)
    for _ in range(15):
        if at > after and (not days or at.strftime("%A") in days):
            return at
        at += timedelta(days=1)
    return None


def last_due(alarm: Alarm, now: Optional[datetime] = None) -> datetime:
    """The scheduled ring time that is going off now (or most recently)."""
    now = now or datetime.now()
    if alarm.repeat == "once":
        return alarm.at
    best = alarm.first
    probe = _occurrences_after(alarm, now - timedelta(days=400))
    while probe is not None and probe <= now + timedelta(minutes=1):
        best = probe
        probe = _occurrences_after(alarm, probe)
    return best


# ---------------------------------------------------------------------------
# Task Scheduler
# ---------------------------------------------------------------------------


def _pythonw() -> str:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else exe)


_MONTHS = tuple(calendar.month_name[1:])
_MONTH_DAYS = {m: calendar.monthrange(2023, m)[1] for m in range(1, 13)}


def _by_month(start: str, months: Sequence[int], day: str) -> str:
    names = "".join(f"<{_MONTHS[m - 1]} />" for m in months)
    return (
        f"<CalendarTrigger><StartBoundary>{start}</StartBoundary>"
        "<Enabled>true</Enabled><ScheduleByMonth><DaysOfMonth>"
        f"<Day>{day}</Day></DaysOfMonth><Months>{names}</Months>"
        "</ScheduleByMonth></CalendarTrigger>"
    )


def _triggers(alarm: Alarm, at: datetime) -> Tuple[str, bool]:
    """``(trigger XML, expires)`` for an alarm ringing first at ``at``."""
    start = at.replace(microsecond=0).isoformat()
    if alarm.repeat == "once":
        # Kept 12 hours so a laptop that was off can still say "missed".
        end = (at + timedelta(hours=12)).replace(microsecond=0).isoformat()
        return (
            f"<TimeTrigger><StartBoundary>{start}</StartBoundary>"
            f"<EndBoundary>{end}</EndBoundary><Enabled>true</Enabled></TimeTrigger>",
            True,
        )
    if alarm.repeat == "daily":
        return (
            f"<CalendarTrigger><StartBoundary>{start}</StartBoundary>"
            "<Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval>"
            "</ScheduleByDay></CalendarTrigger>",
            False,
        )
    if alarm.repeat in ("monthly", "yearly"):
        day = alarm.first.day
        months = range(1, 13) if alarm.repeat == "monthly" else (alarm.first.month,)
        # Day 31 in a 30-day month (and day 29+ in February) rings on the
        # month's last day instead of being skipped. "Last" in February is
        # the 29th in leap years.
        short = [m for m in months if day > (28 if m == 2 else _MONTH_DAYS[m])]
        has_day = [m for m in months if m not in short]
        parts = []
        if has_day:
            parts.append(_by_month(start, has_day, str(day)))
        if short:
            parts.append(_by_month(start, short, "Last"))
        return "".join(parts), False
    days = "".join(f"<{d} />" for d in alarm.ring_days())
    return (
        f"<CalendarTrigger><StartBoundary>{start}</StartBoundary>"
        "<Enabled>true</Enabled><ScheduleByWeek><DaysOfWeek>"
        f"{days}</DaysOfWeek><WeeksInterval>1</WeeksInterval>"
        "</ScheduleByWeek></CalendarTrigger>",
        False,
    )


def task_xml(
    alarm: Alarm,
    *,
    command: str,
    arguments: str,
    at: Optional[datetime] = None,
    description: str = "",
) -> str:
    trigger, expires = _triggers(alarm, at or alarm.at)
    expire = "<DeleteExpiredTaskAfter>PT1H</DeleteExpiredTaskAfter>" if expires else ""
    enabled = "true" if alarm.enabled else "false"
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>OpenJarvis</Author>
    <Description>{escape(description or alarm.describe())}</Description>
  </RegistrationInfo>
  <Triggers>{trigger}</Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <WakeToRun>true</WakeToRun>
    <ExecutionTimeLimit>PT15M</ExecutionTimeLimit>
    {expire}
    <Enabled>{enabled}</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["schtasks", *args],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AlarmError(f"Task Scheduler is unavailable: {exc}") from exc


def _create_task(name: str, xml: str) -> None:
    fd, tmp = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        Path(tmp).write_text(xml, encoding="utf-16")
        result = _schtasks("/Create", "/XML", tmp, "/TN", name, "/F")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise AlarmError(f"Windows didn't accept the alarm: {detail}")


def _warning_at(alarm: Alarm) -> Optional[datetime]:
    """When the early-warning task first runs (None: no warning possible)."""
    if alarm.advance <= 0:
        return None
    at = alarm.at - timedelta(minutes=alarm.advance)
    if alarm.repeat == "once":
        return at if at > datetime.now() else None
    if at.date() != alarm.at.date():
        # A 00:05 alarm warned at 23:55 needs the previous day's schedule;
        # only daily repeats line up, so others skip the warning.
        return at if alarm.repeat == "daily" else None
    return at


def _register(alarm: Alarm) -> None:
    if sys.platform != "win32":
        raise AlarmError("System alarms need Windows (Task Scheduler).")
    command = _pythonw()
    _create_task(
        alarm.task_name,
        task_xml(
            alarm,
            command=command,
            arguments=f"-m openjarvis.desktop.alarms fire {alarm.id}",
        ),
    )
    warn = _warning_at(alarm)
    if warn is not None:
        _create_task(
            alarm.warning_task_name,
            task_xml(
                alarm,
                command=command,
                arguments=f"-m openjarvis.desktop.alarms warn {alarm.id}",
                at=warn,
                description=f"{alarm.advance} minute warning: {alarm.describe()}",
            ),
        )


def _unregister(alarm: Alarm) -> None:
    if sys.platform == "win32":
        _schtasks("/Delete", "/TN", alarm.task_name, "/F")
        if alarm.advance:
            _schtasks("/Delete", "/TN", alarm.warning_task_name, "/F")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def schedule(
    kind: str,
    when: datetime,
    label: str = "",
    repeat: str = "once",
    *,
    days: Iterable[str] | str = (),
    category: str = "",
    voice: str = "",
    advance: int = 0,
    ring: int = 0,
    snoozes: int = 0,
    path: Optional[Path] = None,
) -> Alarm:
    kind = kind if kind in KINDS else "alarm"
    if kind == "timer":
        repeat, picked = "once", []
    elif repeat in REPEATS and repeat != "custom" and not days:
        picked = []
    else:
        repeat, picked = parse_repeat(repeat, days)
    category = (category or ("reminder" if kind == "reminder" else "general")).lower()
    if category not in CATEGORIES:
        category = "general"
    voice = (voice or "male").lower()
    voice = voice if voice in VOICES else "male"
    if when <= datetime.now() - timedelta(seconds=5):
        raise AlarmError("That time has already passed.")
    draft = Alarm(
        id=uuid.uuid4().hex[:5],
        kind=kind,
        label=" ".join(label.split())[:120],
        when=when.replace(microsecond=0).isoformat(),
        repeat=repeat,
        created=datetime.now().replace(microsecond=0).isoformat(),
        days=picked,
        category=category,
        voice=voice,
        advance=max(0, min(int(advance or 0), 24 * 60)),
        ring=max(0, min(int(ring or 0), 14 * 60)),
        snoozes=snoozes,
    )
    ring_days = draft.ring_days()
    while ring_days and when.strftime("%A") not in ring_days:
        when += timedelta(days=1)
    draft.when = draft.anchor = when.replace(microsecond=0).isoformat()
    _register(draft)
    with _LOCK:
        alarms = _load(path)
        alarms.append(draft)
        _save(alarms, path)
    _history(f"SET {draft.describe()}")
    return draft


def _next_ring(alarm: Alarm, now: datetime) -> Optional[datetime]:
    if alarm.repeat == "once":
        at = alarm.at
        return at if at > now - timedelta(minutes=1) else None
    if alarm.at > now - timedelta(minutes=1):
        return alarm.at
    return _occurrences_after(alarm, now)


def all_alarms(path: Optional[Path] = None) -> List[Alarm]:
    """Every saved alarm with its next ring updated (past one-offs dropped)."""
    now = datetime.now()
    with _LOCK:
        alarms = _load(path)
        kept = []
        for alarm in alarms:
            ring = _next_ring(alarm, now)
            if ring is None:
                continue
            alarm.when = ring.isoformat()
            kept.append(alarm)
        if [a.when for a in kept] != [a.when for a in alarms]:
            _save(kept, path)
    return sorted(kept, key=lambda a: a.when)


def upcoming(path: Optional[Path] = None) -> List[Alarm]:
    """Alarms still to ring, soonest first (past one-offs are dropped)."""
    return all_alarms(path)


def next_alarm(path: Optional[Path] = None) -> Optional[Alarm]:
    return next((a for a in all_alarms(path) if a.enabled), None)


def find(what: str, alarms: Sequence[Alarm]) -> List[Alarm]:
    """Alarms matching an id, 'all', or words from their description."""
    what = (what or "").strip().lower()
    if what in ("all", "everything", "all alarms"):
        return list(alarms)
    hit = [a for a in alarms if a.id == what]
    if not hit and what:
        words = what.split()
        hit = [a for a in alarms if all(w in a.describe().lower() for w in words)]
    if not hit and len(alarms) == 1 and what in ("", "alarm", "it", "timer"):
        hit = list(alarms)
    return hit


def cancel(what: str, *, path: Optional[Path] = None) -> List[Alarm]:
    """Cancel by id, by label/time words, or 'all'; returns what was removed."""
    doomed = find(what, all_alarms(path))
    for alarm in doomed:
        _unregister(alarm)
        _history(f"CANCEL {alarm.describe()}")
    if doomed:
        gone = {a.id for a in doomed}
        with _LOCK:
            _save([a for a in _load(path) if a.id not in gone], path)
    return doomed


def set_enabled(
    what: str, enabled: bool, *, path: Optional[Path] = None
) -> List[Alarm]:
    """Turn matching alarms off (kept, not ringing) or back on."""
    hits = find(what, all_alarms(path))
    flag = "/ENABLE" if enabled else "/DISABLE"
    for alarm in hits:
        if sys.platform == "win32":
            result = _schtasks("/Change", "/TN", alarm.task_name, flag)
            if result.returncode != 0:
                _register(replace(alarm, enabled=enabled))
            elif alarm.advance:
                _schtasks("/Change", "/TN", alarm.warning_task_name, flag)
        alarm.enabled = enabled
        _history(f"{'ENABLE' if enabled else 'DISABLE'} {alarm.describe()}")
    if hits:
        changed = {a.id for a in hits}
        with _LOCK:
            saved = _load(path)
            for alarm in saved:
                if alarm.id in changed:
                    alarm.enabled = enabled
            _save(saved, path)
    return hits


# ---------------------------------------------------------------------------
# Ringing (runs inside the scheduled task)
# ---------------------------------------------------------------------------

_WINRT = "ContentType = WindowsRuntime"
# Windows PowerShell's AppUserModelID is always registered, so the toast shows
# without installing a Start Menu shortcut.
_APP_ID = (
    "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"
)
_PS_PRELUDE = [
    "$ErrorActionPreference = 'Stop'",
    "$null = [Windows.UI.Notifications.ToastNotificationManager, "
    f"Windows.UI.Notifications, {_WINRT}]",
    "$null = [Windows.Data.Xml.Dom.XmlDocument, "
    f"Windows.Data.Xml.Dom.XmlDocument, {_WINRT}]",
    f"$appId = '{_APP_ID}'",
    "$history = [Windows.UI.Notifications.ToastNotificationManager]::History",
]
# Speaks the announcement, shows the toast, re-announces every minute while it
# is still up, and takes it down after the ring duration. Dismiss, Snooze or
# `jarvis alarm stop` remove the toast, which ends the loop.
_TOAST_PS = "\n".join(
    [
        *_PS_PRELUDE,
        "$tag = $env:OPENJARVIS_TOAST_TAG",
        "$say = $env:OPENJARVIS_ALARM_SAY",
        "$ring = [int]$env:OPENJARVIS_ALARM_RING",
        "$synth = $null",
        "if ($say) { try {",
        "  Add-Type -AssemblyName System.Speech",
        "  $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer",
        "  $synth.Volume = 100",
        "  try { $synth.SelectVoiceByHints($env:OPENJARVIS_ALARM_VOICE) } catch {}",
        "  $synth.Speak($say)",
        "} catch { $synth = $null } }",
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument",
        "$xml.LoadXml($env:OPENJARVIS_TOAST_XML)",
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)",
        "$toast.Tag = $tag",
        "$toast.Group = 'openjarvis'",
        "[Windows.UI.Notifications.ToastNotificationManager]::"
        "CreateToastNotifier($appId).Show($toast)",
        "if ($env:OPENJARVIS_ALARM_WAIT -ne '1') { exit 0 }",
        "$limit = if ($ring -gt 0) { $ring } else { 840 }",
        "$started = Get-Date",
        "$spoken = Get-Date",
        "while (((Get-Date) - $started).TotalSeconds -lt $limit) {",
        "  Start-Sleep -Seconds 1",
        "  $up = @($history.GetHistory($appId) | Where-Object { $_.Tag -eq $tag })",
        "  if ($up.Count -eq 0) { exit 0 }",
        "  if ($synth -and ((Get-Date) - $spoken).TotalSeconds -ge 60) {",
        "    $synth.Speak($say); $spoken = Get-Date }",
        "}",
        "if ($ring -gt 0) { $history.Remove($tag, 'openjarvis', $appId) }",
    ]
)
_STOP_PS = "\n".join([*_PS_PRELUDE, "$history.RemoveGroup('openjarvis', $appId)"])


def _ringing_path() -> Path:
    return _store_path().with_name("alarm_ringing.json")


def _title(alarm: Alarm) -> str:
    return {"alarm": "Alarm", "timer": "Timer finished", "reminder": "Reminder"}[
        alarm.kind if alarm.kind in KINDS else "alarm"
    ]


def toast_xml(
    alarm: Alarm,
    *,
    now: Optional[datetime] = None,
    style: str = "ring",  # ring | warning | missed
) -> str:
    now = now or datetime.now()
    title = _title(alarm)
    body = alarm.label or ("Time's up" if alarm.kind == "timer" else f"{now:%I:%M %p}")
    if style == "warning":
        title = f"{title} in {alarm.advance} minutes"
        body = f"{alarm.label or alarm.kind.title()} at {alarm.at:%I:%M %p}"
    elif style == "missed":
        title = f"Missed {alarm.kind}"
        due = last_due(alarm, now)
        body = f"{alarm.label or alarm.kind.title()} was due at {due:%I:%M %p}"
    category = (
        "" if alarm.category in ("", "general") else f" - {alarm.category.title()}"
    )
    if style == "ring":
        scenario = "reminder" if alarm.kind == "reminder" else "alarm"
        sound = {
            "alarm": 'src="ms-winsoundevent:Notification.Looping.Alarm" loop="true"',
            "timer": 'src="ms-winsoundevent:Notification.Looping.Alarm2" loop="true"',
            "reminder": 'src="ms-winsoundevent:Notification.Reminder"',
        }[alarm.kind if alarm.kind in KINDS else "alarm"]
        actions = (
            "<actions>"
            '<input id="snoozeTime" type="selection" defaultInput="5">'
            '<selection id="5" content="5 minutes"/>'
            '<selection id="10" content="10 minutes"/>'
            '<selection id="15" content="15 minutes"/></input>'
            '<action activationType="system" arguments="snooze" '
            'hint-inputId="snoozeTime" content=""/>'
            '<action activationType="system" arguments="dismiss" content=""/>'
            "</actions>"
        )
    else:
        scenario = "default"
        sound = 'src="ms-winsoundevent:Notification.Reminder"'
        actions = ""
    head = f'<toast scenario="{scenario}"' if scenario != "default" else "<toast"
    return (
        f'{head} launch="openjarvis-{escape(alarm.id)}">'
        '<visual><binding template="ToastGeneric">'
        f"<text>{escape(title)}</text><text>{escape(body)}</text>"
        f"<text>{now:%I:%M %p}{escape(category)} - OpenJarvis</text>"
        f"</binding></visual>{actions}<audio {sound}/></toast>"
    )


def announcement(
    alarm: Alarm, *, now: Optional[datetime] = None, style: str = "ring"
) -> str:
    """What the alarm says out loud."""
    now = now or datetime.now()
    clock = f"{now:%I:%M %p}".lstrip("0")
    what = alarm.label
    if style == "warning":
        return f"In {alarm.advance} minutes: {what or 'your ' + alarm.kind}."
    if alarm.kind == "timer":
        return f"Your timer is done. {what}".strip()
    if alarm.kind == "reminder":
        return f"Reminder: {what}." if what else f"Reminder. It's {clock}."
    return f"Alarm. {what}. It's {clock}." if what else f"Alarm. It's {clock}."


def _run_ps(script: str, env_extra: Dict[str, str], *, timeout: float) -> bool:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ],
            env={**os.environ, **env_extra},
            capture_output=True,
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Alarm notification failed: %s", exc)
        return False
    return result.returncode == 0


def show_toast(alarm: Alarm, *, style: str = "ring", wait: bool = False) -> bool:
    """Announce and show the alarm; with ``wait``, keep ringing until it's
    dismissed, snoozed, stopped or its ring duration runs out."""
    speak = alarm.voice != "none" and style != "missed"
    env = {
        "OPENJARVIS_TOAST_XML": toast_xml(alarm, style=style),
        "OPENJARVIS_TOAST_TAG": f"jarvis-{alarm.id}",
        "OPENJARVIS_ALARM_SAY": announcement(alarm, style=style) if speak else "",
        "OPENJARVIS_ALARM_VOICE": "Female" if alarm.voice == "female" else "Male",
        "OPENJARVIS_ALARM_RING": str(alarm.ring),
        "OPENJARVIS_ALARM_WAIT": "1" if wait and style == "ring" else "0",
    }
    return _run_ps(_TOAST_PS, env, timeout=900 if wait else 60)


def stop_ringing() -> bool:
    """Take down every ringing Jarvis alarm (its sound and voice stop)."""
    try:
        _ringing_path().unlink()
    except OSError:
        pass
    _history("STOP ringing alarms")
    return _run_ps(_STOP_PS, {}, timeout=30)


def ringing() -> Optional[Alarm]:
    """The alarm that went off in the last 15 minutes and wasn't stopped."""
    try:
        data = json.loads(_ringing_path().read_text(encoding="utf-8"))
        since = datetime.fromisoformat(data["since"])
        alarm = Alarm(**{k: v for k, v in data["alarm"].items() if k in _FIELDS})
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if datetime.now() - since > timedelta(minutes=15):
        return None
    return alarm


def snooze(minutes: float = 5, *, path: Optional[Path] = None) -> Optional[Alarm]:
    """Stop the ringing alarm and ring it again in ``minutes``."""
    current = ringing()
    if current is None:
        return None
    stop_ringing()
    label = current.label
    again = schedule(
        current.kind if current.kind != "timer" else "alarm",
        datetime.now() + timedelta(minutes=float(minutes)),
        label,
        category=current.category,
        voice=current.voice,
        ring=current.ring,
        snoozes=current.snoozes + 1,
        path=path,
    )
    _history(f"SNOOZE {minutes:g} min -> {again.describe()}")
    return again


def _mark_ringing(alarm: Alarm) -> None:
    try:
        _ringing_path().write_text(
            json.dumps({"since": datetime.now().isoformat(), "alarm": asdict(alarm)}),
            encoding="utf-8",
        )
    except OSError:
        pass


def fire(alarm_id: str, *, path: Optional[Path] = None, wait: bool = True) -> bool:
    alarm = next((a for a in _load(path) if a.id == alarm_id), None)
    if alarm is None or not alarm.enabled:
        return False
    now = datetime.now()
    late = now - last_due(alarm, now)
    if alarm.repeat == "once":  # its task deletes itself once expired
        with _LOCK:
            _save([a for a in _load(path) if a.id != alarm_id], path)
    if late > MISSED_AFTER:
        minutes = int(late.total_seconds() // 60)
        _history(f"MISSED ({minutes} min late) {alarm.describe()}")
        shown = show_toast(alarm, style="missed")
    else:
        _history(f"RING {alarm.describe()}")
        _mark_ringing(alarm)
        shown = show_toast(alarm, style="ring", wait=wait)
        current = ringing()
        if wait and current is not None and current.id == alarm.id:
            try:  # dismissed, snoozed or timed out: nothing rings any more
                _ringing_path().unlink()
            except OSError:
                pass
    if not shown:
        from openjarvis.pc.notify import notify

        notify(_title(alarm), alarm.label or "Time's up")
    return True


def warn(alarm_id: str, *, path: Optional[Path] = None) -> bool:
    alarm = next((a for a in _load(path) if a.id == alarm_id), None)
    if alarm is None or not alarm.enabled or not alarm.advance:
        return False
    _history(f"WARNING {alarm.describe()}")
    return show_toast(alarm, style="warning")


def test_ring(voice: str = "male", label: str = "Test alarm", ring: int = 10) -> bool:
    """Ring a sample alarm now (nothing is saved)."""
    sample = Alarm(
        id="test",
        kind="alarm",
        label=label,
        when=datetime.now().replace(microsecond=0).isoformat(),
        voice=voice if voice in VOICES else "male",
        ring=ring,
    )
    return show_toast(sample, style="ring", wait=True)


# ---------------------------------------------------------------------------
# Import / export
# ---------------------------------------------------------------------------

_CSV_FIELDS = (
    "kind",
    "label",
    "when",
    "repeat",
    "days",
    "category",
    "voice",
    "advance",
    "ring",
    "enabled",
)


def export(target: Path, *, path: Optional[Path] = None) -> int:
    alarms = all_alarms(path)
    target = Path(target)
    if target.suffix.lower() == ".csv":
        with target.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=("id", *_CSV_FIELDS))
            writer.writeheader()
            for alarm in alarms:
                row = {k: getattr(alarm, k) for k in ("id", *_CSV_FIELDS)}
                row["days"] = ",".join(d[:3] for d in alarm.days)
                writer.writerow(row)
    else:
        target.write_text(
            json.dumps([asdict(a) for a in alarms], indent=2), encoding="utf-8"
        )
    _history(f"EXPORT {len(alarms)} alarms to {target}")
    return len(alarms)


def alarm_pro_file() -> Path:
    """Where Windows Alarm Pro keeps its alarms."""
    return (
        Path(os.environ.get("APPDATA", str(Path.home())))
        / ("IGRF_WindowsAlarmPro")
        / "alarms.xml"
    )


_PS_NS = "{http://schemas.microsoft.com/powershell/2004/04}"
_ALARM_PRO_REPEATS = {
    "one time": "once",
    "daily": "daily",
    "weekly": "weekly",
    "weekdays (mon-fri)": "weekdays",
    "weekdays": "weekdays",
    "weekends": "weekends",
    "monthly": "monthly",
    "yearly": "yearly",
    "custom days": "custom",
}


def _clixml_value(node: ET.Element):
    tag = node.tag.replace(_PS_NS, "")
    text = node.text or ""
    if tag == "Nil":
        return None
    if tag == "B":
        return text.strip().lower() == "true"
    if tag in ("I32", "I64", "I16", "U32", "By"):
        return int(text or 0)
    if tag in ("Db", "Sg", "D"):
        return float(text or 0)
    if tag == "DT":
        stamp = re.sub(r"(\.\d{6})\d+", r"\1", text.strip())
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if parsed.tzinfo:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed
    if tag == "Obj":
        items = node.find(f"{_PS_NS}LST")
        if items is not None:
            return [_clixml_value(child) for child in items]
        props = node.find(f"{_PS_NS}MS")
        if props is not None:
            return {c.get("N"): _clixml_value(c) for c in props if c.get("N")}
        return text
    return text


def read_alarm_pro(source: Path) -> List[dict]:
    """Alarms from a Windows Alarm Pro ``alarms.xml`` (PowerShell CliXml)."""
    root = ET.parse(source).getroot()
    out = []
    for obj in root.findall(f"{_PS_NS}Obj"):
        item = _clixml_value(obj)
        if not isinstance(item, dict) or not isinstance(item.get("DateTime"), datetime):
            continue
        voice = str(item.get("Voice") or "")
        repeat = _ALARM_PRO_REPEATS.get(
            str(item.get("Recurrence") or "One Time").lower(), "once"
        )
        out.append(
            {
                "kind": "alarm",
                "label": str(item.get("Name") or ""),
                "when": item["DateTime"].isoformat(),
                "repeat": repeat,
                "days": item.get("SelectedDays") or [],
                "category": str(item.get("Category") or "general").lower(),
                "voice": "female"
                if "female" in voice.lower()
                else "none"
                if voice.lower().startswith("no")
                else "male",
                "advance": 10 if item.get("AdvanceNotification") else 0,
                "ring": int(item.get("RingDuration") or 0),
                "enabled": bool(item.get("Enabled", True)),
                "done": bool(item.get("Alerted")) and repeat == "once",
            }
        )
    return out


def import_file(source: Path, *, path: Optional[Path] = None) -> Tuple[int, int]:
    """Schedule the alarms in a .json/.csv export or an Alarm Pro alarms.xml.

    Returns ``(added, skipped)``; one-off alarms already in the past and ones
    that are already set are skipped.
    """
    source = Path(source)
    suffix = source.suffix.lower()
    if suffix == ".xml":
        rows = read_alarm_pro(source)
    elif suffix == ".csv":
        with source.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
    else:
        rows = json.loads(source.read_text(encoding="utf-8"))
    existing = {(a.label, a.when, a.repeat) for a in all_alarms(path)}
    added = skipped = 0
    now = datetime.now()
    for row in rows if isinstance(rows, list) else []:
        try:
            when = datetime.fromisoformat(str(row.get("when") or ""))
            kind = str(row.get("kind") or "alarm")
            repeat = str(row.get("repeat") or "once")
            days = row.get("days") or []
            enabled = str(row.get("enabled", True)).lower() not in ("false", "0", "no")
            if row.get("done"):
                raise AlarmError("already rang")
            probe = Alarm(
                id="x",
                kind=kind,
                label="",
                when=when.isoformat(),
                repeat=repeat,
                days=parse_days(days),
            )
            if repeat != "once" and when <= now:
                when = _occurrences_after(probe, now) or when
            if (str(row.get("label") or ""), when.isoformat(), repeat) in existing:
                raise AlarmError("already set")
            alarm = schedule(
                kind,
                when,
                str(row.get("label") or ""),
                repeat,
                days=days if repeat == "custom" else (),
                category=str(row.get("category") or ""),
                voice=str(row.get("voice") or ""),
                advance=int(row.get("advance") or 0),
                ring=int(row.get("ring") or 0),
                path=path,
            )
            if not enabled:
                set_enabled(alarm.id, False, path=path)
            added += 1
        except (AlarmError, ValueError, TypeError, AttributeError) as exc:
            logger.debug("Skipped imported alarm %r: %s", row, exc)
            skipped += 1
    _history(f"IMPORT {added} alarms from {source} ({skipped} skipped)")
    return added, skipped


# ---------------------------------------------------------------------------
# Entry point for the scheduled tasks
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) == 2 and argv[0] == "fire":
        return 0 if fire(argv[1]) else 1
    if len(argv) == 2 and argv[0] == "warn":
        return 0 if warn(argv[1]) else 1
    print(
        "usage: python -m openjarvis.desktop.alarms fire|warn <id>  "
        "(use `jarvis alarm` for everything else)",
        file=sys.stderr,
    )
    return 2


__all__ = [
    "Alarm",
    "AlarmError",
    "CATEGORIES",
    "REPEATS",
    "VOICES",
    "all_alarms",
    "alarm_pro_file",
    "announcement",
    "cancel",
    "export",
    "find",
    "fire",
    "import_file",
    "last_due",
    "next_alarm",
    "parse_days",
    "parse_repeat",
    "parse_time",
    "read_alarm_pro",
    "ringing",
    "schedule",
    "set_enabled",
    "snooze",
    "stop_ringing",
    "task_xml",
    "test_ring",
    "toast_xml",
    "upcoming",
    "warn",
]


if __name__ == "__main__":
    raise SystemExit(main())
