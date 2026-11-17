"""``jarvis alarm`` - Windows alarms, timers and reminders from the command line.

The same alarms Jarvis sets by voice: real Task Scheduler alarms that ring
(voice announcement + alarm notification) even when Jarvis isn't running.
Features follow Windows Alarm Pro, without its GUI.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import click


def _store():
    from openjarvis.desktop import alarms

    return alarms


def _until(at: datetime) -> str:
    left = at - datetime.now()
    minutes = max(0, int(left.total_seconds() // 60))
    if minutes < 60:
        return f"in {minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"in {hours} h {minutes} min"
    return f"in {hours // 24} days"


def _print_list(show_clock: bool = True) -> None:
    alarms = _store()
    items = alarms.all_alarms()
    if show_clock:
        click.echo(f"Now: {datetime.now():%a %d %b %Y  %I:%M:%S %p}")
        nxt = next((a for a in items if a.enabled), None)
        if nxt is not None:
            name = f"'{nxt.label}' " if nxt.label else ""
            click.echo(
                f"Next: {nxt.kind} {name}at {nxt.at:%I:%M %p} ({_until(nxt.at)})"
            )
        click.echo("")
    if not items:
        click.echo(
            "No alarms set. Try: jarvis alarm add 7am --label Gym --repeat weekdays"
        )
        return
    for alarm in items:
        click.echo(alarm.describe())


def _fail(exc: Exception) -> None:
    raise click.ClickException(str(exc))


@click.group(
    "alarm",
    invoke_without_command=True,
    help="Alarms, timers and reminders that ring even when Jarvis is closed.",
)
@click.pass_context
def alarm(ctx: click.Context) -> None:
    if ctx.invoked_subcommand is None:
        _print_list()


@alarm.command("list")
def list_cmd() -> None:
    """Show every alarm, soonest first, with the time and the next alarm."""
    _print_list()


@alarm.command("add")
@click.argument("when", nargs=-1, required=True)
@click.option("-l", "--label", default="", help="Name/description, e.g. 'Gym'.")
@click.option(
    "-r",
    "--repeat",
    default="once",
    help="once, daily, weekly, weekdays, weekends, monthly, yearly or days "
    "like 'mon,wed,fri'.",
)
@click.option(
    "-c",
    "--category",
    default="general",
    type=click.Choice(
        [
            "general",
            "work",
            "personal",
            "meeting",
            "medication",
            "birthday",
            "reminder",
        ],
        case_sensitive=False,
    ),
)
@click.option(
    "-v",
    "--voice",
    default="male",
    type=click.Choice(["male", "female", "none"], case_sensitive=False),
    help="Voice that announces the alarm.",
)
@click.option(
    "-a",
    "--advance",
    default=0,
    type=click.IntRange(0, 1440),
    help="Warn this many minutes before (0 = no warning).",
)
@click.option(
    "--ring",
    default=0,
    type=click.IntRange(0, 840),
    help="Seconds to ring (0 = until dismissed).",
)
@click.option(
    "--kind",
    default="alarm",
    type=click.Choice(["alarm", "reminder"], case_sensitive=False),
)
def add(
    when: tuple,
    label: str,
    repeat: str,
    category: str,
    voice: str,
    advance: int,
    ring: int,
    kind: str,
) -> None:
    """Set an alarm: jarvis alarm add 6:30 am --label Gym --repeat weekdays"""
    alarms = _store()
    try:
        at = alarms.parse_time(" ".join(when))
        repeat_name, days = alarms.parse_repeat(repeat)
        item = alarms.schedule(
            kind.lower(),
            at,
            label,
            repeat_name,
            days=days,
            category=category,
            voice=voice,
            advance=advance,
            ring=ring,
        )
    except alarms.AlarmError as exc:
        _fail(exc)
    click.echo(f"Set {item.describe()}  ({_until(item.at)})")


@alarm.command("timer")
@click.argument("minutes", type=float)
@click.option("-l", "--label", default="")
@click.option(
    "-v",
    "--voice",
    default="male",
    type=click.Choice(["male", "female", "none"], case_sensitive=False),
)
def timer(minutes: float, label: str, voice: str) -> None:
    """Start a timer: jarvis alarm timer 10"""
    alarms = _store()
    try:
        item = alarms.schedule(
            "timer", alarms.parse_time(minutes=minutes), label, voice=voice
        )
    except alarms.AlarmError as exc:
        _fail(exc)
    click.echo(f"Timer set: {item.describe()}")


@alarm.command("remind")
@click.argument("text", nargs=-1, required=True)
@click.option(
    "--at", "when", required=True, help="'5 pm', 'tomorrow 9 am', 'in 20 minutes'."
)
@click.option("-r", "--repeat", default="once")
@click.option("-a", "--advance", default=0, type=click.IntRange(0, 1440))
def remind(text: tuple, when: str, repeat: str, advance: int) -> None:
    """Set a reminder: jarvis alarm remind call mom --at 6pm"""
    alarms = _store()
    try:
        repeat_name, days = alarms.parse_repeat(repeat)
        item = alarms.schedule(
            "reminder",
            alarms.parse_time(when),
            " ".join(text),
            repeat_name,
            days=days,
            advance=advance,
        )
    except alarms.AlarmError as exc:
        _fail(exc)
    click.echo(f"Set {item.describe()}  ({_until(item.at)})")


@alarm.command("cancel")
@click.argument("what", nargs=-1, required=True)
def cancel(what: tuple) -> None:
    """Delete alarms by id, words from the label/time, or 'all'."""
    gone = _store().cancel(" ".join(what))
    if not gone:
        raise click.ClickException("Nothing matched. See `jarvis alarm list`.")
    for item in gone:
        click.echo(f"Cancelled {item.describe()}")


@alarm.command("off")
@click.argument("what", nargs=-1, required=True)
def off(what: tuple) -> None:
    """Turn alarms off without deleting them."""
    hits = _store().set_enabled(" ".join(what), False)
    if not hits:
        raise click.ClickException("Nothing matched. See `jarvis alarm list`.")
    for item in hits:
        click.echo(f"Off: {item.describe()}")


@alarm.command("on")
@click.argument("what", nargs=-1, required=True)
def on(what: tuple) -> None:
    """Turn alarms back on ('all' enables every alarm)."""
    hits = _store().set_enabled(" ".join(what), True)
    if not hits:
        raise click.ClickException("Nothing matched. See `jarvis alarm list`.")
    for item in hits:
        click.echo(f"On: {item.describe()}")


@alarm.command("stop")
def stop() -> None:
    """Silence the alarm that is ringing now."""
    _store().stop_ringing()
    click.echo("Stopped.")


@alarm.command("snooze")
@click.argument("minutes", type=float, default=5)
def snooze(minutes: float) -> None:
    """Snooze the ringing alarm (default 5 minutes)."""
    again = _store().snooze(minutes)
    if again is None:
        raise click.ClickException("No alarm is ringing.")
    click.echo(f"Snoozed until {again.at:%I:%M %p}.")


@alarm.command("test")
@click.option(
    "-v",
    "--voice",
    default="male",
    type=click.Choice(["male", "female", "none"], case_sensitive=False),
)
@click.option("--seconds", default=10, type=click.IntRange(3, 120))
def test(voice: str, seconds: int) -> None:
    """Ring a sample alarm now to hear the voice and sound."""
    click.echo(f"Ringing a test alarm for {seconds} s (Dismiss stops it)...")
    if not _store().test_ring(voice, ring=seconds):
        raise click.ClickException("Windows didn't show the notification.")


@alarm.command("export")
@click.argument("target", type=click.Path(dir_okay=False, path_type=Path))
def export(target: Path) -> None:
    """Save alarms to a .json or .csv file."""
    count = _store().export(target)
    click.echo(f"Exported {count} alarms to {target}.")


@alarm.command("import")
@click.argument(
    "source",
    required=False,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
def import_cmd(source: Optional[Path]) -> None:
    """Load alarms from a .json/.csv export or Windows Alarm Pro's alarms.xml
    (its own file is used when no file is given)."""
    alarms = _store()
    if source is None:
        source = alarms.alarm_pro_file()
        if not source.exists():
            raise click.ClickException(
                f"No Windows Alarm Pro alarms at {source}. Give a file to import."
            )
    added, skipped = alarms.import_file(source)
    click.echo(f"Imported {added} alarms ({skipped} skipped: past or already set).")


@alarm.command("clock")
def clock() -> None:
    """Live clock with a countdown to the next alarm (Ctrl+C to quit)."""
    import time

    from rich.live import Live
    from rich.text import Text

    alarms = _store()

    def frame() -> Text:
        now = datetime.now()
        text = Text(f"{now:%I:%M:%S %p}\n", style="bold cyan")
        text.append(f"{now:%A %d %B %Y}\n", style="dim")
        nxt = alarms.next_alarm()
        if nxt is not None:
            left = nxt.at - now
            hms = str(timedelta(seconds=max(0, int(left.total_seconds()))))
            name = nxt.label or nxt.kind
            text.append(f"Next: {name} at {nxt.at:%I:%M %p}  (in {hms})")
        else:
            text.append("No alarms set.", style="dim")
        return text

    try:
        with Live(frame(), refresh_per_second=2) as live:
            while True:
                time.sleep(0.5)
                live.update(frame())
    except KeyboardInterrupt:
        pass


@alarm.command("log")
@click.option("-n", "--lines", default=20, show_default=True)
def log(lines: int) -> None:
    """Show recent alarm activity (set, rang, missed, snoozed...)."""
    path = _store().log_path()
    try:
        tail = path.read_text(encoding="utf-8").splitlines()[-lines:]
    except OSError:
        click.echo("No alarm activity yet.")
        return
    click.echo("\n".join(tail))


__all__ = ["alarm"]
