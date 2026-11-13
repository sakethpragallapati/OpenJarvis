"""Timers and reminders that fire as a desktop notification (and speech).

Reminders live in ``~/.openjarvis/reminders.json`` so a reminder set from
``jarvis chat`` fires in a running ``jarvis listen`` and vice versa. Any
OpenJarvis process with a :class:`ReminderWatcher` delivers due reminders;
one that came due while nothing was running fires on the next start.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

_MAX_MESSAGE_CHARS = 300
_MAX_DELAY = timedelta(days=366)
_CLOCK_TIME = re.compile(
    r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\s*$", re.IGNORECASE
)


@dataclass(slots=True)
class Reminder:
    id: str
    message: str
    due: str  # ISO-8601, UTC
    created: str
    fired: bool = False

    @property
    def due_at(self) -> datetime:
        return datetime.fromisoformat(self.due)

    def describe(self) -> str:
        local = self.due_at.astimezone()
        return f"[{self.id}] {local:%a %d %b %H:%M} — {self.message}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_when(
    *,
    in_minutes: Optional[float] = None,
    at: Optional[str] = None,
    now: Optional[datetime] = None,
) -> datetime:
    """Resolve a relative delay or a clock time / ISO datetime to UTC.

    ``at`` accepts "18:30", "6:30 pm", "7pm" (local time; rolls over to
    tomorrow if already past) or an ISO datetime ("2026-11-14T09:00").
    """
    now = now or _now()
    if in_minutes is not None:
        minutes = float(in_minutes)
        if minutes <= 0:
            raise ValueError("in_minutes must be positive")
        due = now + timedelta(minutes=minutes)
    elif at:
        match = _CLOCK_TIME.match(at)
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2) or 0)
            suffix = (match.group(3) or "").lower().replace(".", "")
            if suffix == "pm" and hour < 12:
                hour += 12
            elif suffix == "am" and hour == 12:
                hour = 0
            if hour > 23 or minute > 59:
                raise ValueError(f"Invalid time: {at!r}")
            local_now = now.astimezone()
            due_local = local_now.replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            if due_local <= local_now:
                due_local += timedelta(days=1)
            due = due_local.astimezone(timezone.utc)
        else:
            try:
                parsed = datetime.fromisoformat(at.strip())
            except ValueError:
                raise ValueError(
                    f"Could not understand time {at!r}; use HH:MM or an ISO datetime"
                ) from None
            if parsed.tzinfo is None:
                parsed = parsed.astimezone()  # naive = local time
            due = parsed.astimezone(timezone.utc)
            if due <= now:
                raise ValueError("That time is already in the past")
    else:
        raise ValueError("Give either in_minutes or at")
    if due - now > _MAX_DELAY:
        raise ValueError("Reminders can be at most one year ahead")
    return due


class ReminderStore:
    """Thread-safe JSON-file store with atomic writes.

    Writes are atomic, so other processes never read a torn file. There is
    no cross-process lock: two processes polling in the same instant could
    both deliver one reminder, which is harmless for a notification.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        if path is None:
            from openjarvis.core.paths import get_config_dir

            path = get_config_dir() / "reminders.json"
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> List[Reminder]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable reminders file %s: %s", self.path, exc)
            return []
        reminders = []
        for item in raw if isinstance(raw, list) else []:
            try:
                reminders.append(Reminder(**item))
            except TypeError:
                continue
        return reminders

    def _save(self, reminders: List[Reminder]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace: a reader in another process never sees half a file.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump([asdict(r) for r in reminders], handle, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def add(self, message: str, due: datetime) -> Reminder:
        message = " ".join(message.split())[:_MAX_MESSAGE_CHARS]
        if not message:
            raise ValueError("Reminder message is empty")
        reminder = Reminder(
            id=uuid.uuid4().hex[:6],
            message=message,
            due=due.astimezone(timezone.utc).isoformat(),
            created=_now().isoformat(),
        )
        with self._lock:
            reminders = self._load()
            reminders.append(reminder)
            self._save(reminders)
        return reminder

    def pending(self) -> List[Reminder]:
        with self._lock:
            items = [r for r in self._load() if not r.fired]
        return sorted(items, key=lambda r: r.due)

    def cancel(self, reminder_id: str) -> bool:
        with self._lock:
            reminders = self._load()
            kept = [r for r in reminders if r.id != reminder_id.strip()]
            if len(kept) == len(reminders):
                return False
            self._save(kept)
        return True

    def take_due(self, now: Optional[datetime] = None) -> List[Reminder]:
        """Mark due reminders fired and return them (each is returned once)."""
        now = now or _now()
        with self._lock:
            reminders = self._load()
            due = [r for r in reminders if not r.fired and r.due_at <= now]
            if not due:
                return []
            for reminder in due:
                reminder.fired = True
            # Keep a day of fired history for "what did you remind me of?".
            cutoff = now - timedelta(days=1)
            reminders = [r for r in reminders if not r.fired or r.due_at > cutoff]
            self._save(reminders)
        return due


class ReminderWatcher:
    """Background thread that hands due reminders to ``on_due``."""

    def __init__(
        self,
        on_due: Callable[[Reminder], None],
        *,
        store: Optional[ReminderStore] = None,
        interval: float = 5.0,
    ) -> None:
        self._on_due = on_due
        self._store = store or ReminderStore()
        self._interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def check_now(self) -> int:
        fired = 0
        for reminder in self._store.take_due():
            try:
                self._on_due(reminder)
            except Exception as exc:
                logger.warning("Reminder delivery failed: %s", exc)
            fired += 1
        return fired

    def _run(self) -> None:
        while not self._stop.is_set():
            self.check_now()
            self._stop.wait(self._interval)

    def start(self) -> "ReminderWatcher":
        self._thread = threading.Thread(
            target=self._run, name="openjarvis-reminders", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


def notify_reminder(reminder: Reminder) -> None:
    """Default delivery: a desktop notification."""
    from openjarvis.pc.notify import notify

    notify("Reminder", reminder.message)


__all__ = [
    "Reminder",
    "ReminderStore",
    "ReminderWatcher",
    "notify_reminder",
    "parse_when",
]
