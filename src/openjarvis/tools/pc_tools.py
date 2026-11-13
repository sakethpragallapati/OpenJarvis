"""Desktop-assistant tools: control the PC, the clipboard and reminders.

These back ``jarvis listen`` ("Hey Jarvis, open VS Code", "volume to 30",
"remind me in 20 minutes to stretch") but work in any tool-using agent.
"""

from __future__ import annotations

from typing import Any, Callable, Dict

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

_MAX_CLIPBOARD_CHARS = 8000

PC_ACTIONS = (
    "open_app",
    "open_website",
    "volume_up",
    "volume_down",
    "set_volume",
    "mute",
    "play_pause",
    "next_track",
    "previous_track",
    "set_brightness",
    "get_brightness",
    "battery_status",
)


def _result(name: str, content: str, success: bool = True) -> ToolResult:
    return ToolResult(tool_name=name, content=content, success=success)


def _int_param(params: Dict[str, Any], key: str, default: int | None = None) -> int:
    value = params.get(key, default)
    if value is None or value == "":
        raise ValueError(f"'{key}' is required for this action")
    try:
        return int(float(value))
    except (TypeError, ValueError):
        raise ValueError(f"'{key}' must be a number") from None


def _battery_summary() -> str:
    from openjarvis.pc.power import battery_status

    status = battery_status()
    return "No battery found (desktop PC)." if status is None else status.describe()


@ToolRegistry.register("pc_control")
class PCControlTool(BaseTool):
    """Open apps and websites, change volume/brightness, control media."""

    tool_id = "pc_control"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="pc_control",
            description=(
                "Control this computer: open an installed app by name, open a "
                "website, change or set the volume, mute, play/pause/skip "
                "media, read or set screen brightness, or check the battery."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": list(PC_ACTIONS)},
                    "name": {
                        "type": "string",
                        "description": "App name for open_app (e.g. 'VS Code').",
                    },
                    "url": {
                        "type": "string",
                        "description": "Address for open_website.",
                    },
                    "level": {
                        "type": "integer",
                        "description": "0-100 for set_volume / set_brightness.",
                    },
                    "amount": {
                        "type": "integer",
                        "description": "Percent for volume_up/down (default 10).",
                    },
                },
                "required": ["action"],
            },
            category="system",
            timeout_seconds=20.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        from openjarvis.pc import control

        action = str(params.get("action", "")).strip()
        handlers: Dict[str, Callable[[], str]] = {
            "open_app": lambda: (
                f"Opened {control.open_app(str(params.get('name', '')))}."
            ),
            "open_website": lambda: (
                f"Opened {control.open_website(str(params.get('url', '')))}."
            ),
            "volume_up": lambda: control.change_volume(
                "up", _int_param(params, "amount", 10)
            ),
            "volume_down": lambda: control.change_volume(
                "down", _int_param(params, "amount", 10)
            ),
            "set_volume": lambda: control.set_volume(_int_param(params, "level")),
            "mute": control.toggle_mute,
            "play_pause": lambda: control.media_key("play_pause"),
            "next_track": lambda: control.media_key("next"),
            "previous_track": lambda: control.media_key("previous"),
            "set_brightness": lambda: control.set_brightness(
                _int_param(params, "level")
            ),
            "get_brightness": lambda: f"Brightness is {control.get_brightness()}%.",
            "battery_status": lambda: f"Battery: {_battery_summary()}.",
        }
        handler = handlers.get(action)
        if handler is None:
            return _result(
                "pc_control",
                f"Unknown action {action!r}. Use one of: {', '.join(PC_ACTIONS)}.",
                success=False,
            )
        try:
            return _result("pc_control", handler())
        except (control.PCControlError, ValueError) as exc:
            return _result("pc_control", str(exc), success=False)


@ToolRegistry.register("lock_screen")
class LockScreenTool(BaseTool):
    """Lock the workstation (asks for confirmation first)."""

    tool_id = "lock_screen"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="lock_screen",
            description="Lock this computer's screen.",
            parameters={"type": "object", "properties": {}},
            category="system",
            requires_confirmation=True,
        )

    def execute(self, **params: Any) -> ToolResult:
        from openjarvis.pc import control

        try:
            return _result("lock_screen", control.lock_screen())
        except control.PCControlError as exc:
            return _result("lock_screen", str(exc), success=False)


@ToolRegistry.register("clipboard")
class ClipboardTool(BaseTool):
    """Read the text on the clipboard, or put text on it."""

    tool_id = "clipboard"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="clipboard",
            description=(
                "Read the text the user copied (action 'read'), e.g. to "
                "summarize or translate it, or copy text for them ('write')."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["read", "write"]},
                    "text": {
                        "type": "string",
                        "description": "Text to copy, for action 'write'.",
                    },
                },
                "required": ["action"],
            },
            category="system",
        )

    def execute(self, **params: Any) -> ToolResult:
        from openjarvis.pc import control

        action = str(params.get("action", "")).strip()
        try:
            if action == "read":
                text = control.read_clipboard()
                if not text.strip():
                    return _result("clipboard", "The clipboard has no text.")
                if len(text) > _MAX_CLIPBOARD_CHARS:
                    text = text[:_MAX_CLIPBOARD_CHARS] + "\n[... truncated]"
                return _result("clipboard", text)
            if action == "write":
                text = str(params.get("text", ""))
                if not text:
                    return _result("clipboard", "No text to copy.", success=False)
                control.write_clipboard(text)
                return _result("clipboard", f"Copied {len(text)} characters.")
        except control.PCControlError as exc:
            return _result("clipboard", str(exc), success=False)
        return _result("clipboard", "Action must be 'read' or 'write'.", success=False)


@ToolRegistry.register("set_reminder")
class SetReminderTool(BaseTool):
    """Set a timer/reminder that pops up (and is spoken in voice mode)."""

    tool_id = "set_reminder"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="set_reminder",
            description=(
                "Set a reminder or timer. Use in_minutes for relative times "
                "('in 20 minutes' -> 20) or at for a clock time ('18:30', "
                "'7 pm') or ISO datetime. It shows a desktop notification "
                "when due."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "What to remind the user about.",
                    },
                    "in_minutes": {"type": "number"},
                    "at": {"type": "string"},
                },
                "required": ["message"],
            },
            category="productivity",
        )

    def execute(self, **params: Any) -> ToolResult:
        from openjarvis.pc.reminders import ReminderStore, parse_when

        in_minutes = params.get("in_minutes")
        at = params.get("at")
        try:
            due = parse_when(
                in_minutes=float(in_minutes) if in_minutes not in (None, "") else None,
                at=str(at) if at else None,
            )
            reminder = ReminderStore().add(str(params.get("message", "")), due)
        except (TypeError, ValueError) as exc:
            return _result("set_reminder", f"Could not set reminder: {exc}", False)
        local = reminder.due_at.astimezone()
        return _result(
            "set_reminder",
            f"Reminder set for {local:%H:%M on %a %d %b}: {reminder.message} "
            f"(id {reminder.id}).",
        )


@ToolRegistry.register("list_reminders")
class ListRemindersTool(BaseTool):
    tool_id = "list_reminders"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="list_reminders",
            description="List upcoming reminders with their ids and times.",
            parameters={"type": "object", "properties": {}},
            category="productivity",
        )

    def execute(self, **params: Any) -> ToolResult:
        from openjarvis.pc.reminders import ReminderStore

        pending = ReminderStore().pending()
        if not pending:
            return _result("list_reminders", "No upcoming reminders.")
        return _result("list_reminders", "\n".join(r.describe() for r in pending))


@ToolRegistry.register("cancel_reminder")
class CancelReminderTool(BaseTool):
    tool_id = "cancel_reminder"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="cancel_reminder",
            description="Cancel a reminder by the id shown in list_reminders.",
            parameters={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
            category="productivity",
        )

    def execute(self, **params: Any) -> ToolResult:
        from openjarvis.pc.reminders import ReminderStore

        reminder_id = str(params.get("id", "")).strip()
        if reminder_id and ReminderStore().cancel(reminder_id):
            return _result("cancel_reminder", f"Cancelled reminder {reminder_id}.")
        return _result(
            "cancel_reminder", f"No reminder with id {reminder_id!r}.", success=False
        )


__all__ = [
    "CancelReminderTool",
    "ClipboardTool",
    "ListRemindersTool",
    "LockScreenTool",
    "PCControlTool",
    "SetReminderTool",
]
