"""The LangGraph agent loop with a scripted model and fake tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

pytest.importorskip("langgraph")

from openjarvis.desktop.graph import (  # noqa: E402
    DesktopGraph,
    clean_reply,
    is_single_step,
    parse_text_tool_calls,
    window,
)
from openjarvis.desktop.runtime import ToolOutcome  # noqa: E402


def _call(name: str, **args: Any) -> Dict[str, Any]:
    return {"id": f"c_{name}", "name": name, "arguments": json.dumps(args)}


class FakeRuntime:
    def __init__(self, results: Dict[str, str] | None = None) -> None:
        self.calls: List[tuple] = []
        self.results = results or {}
        self.schemas = [{"type": "function", "function": {"name": "open"}}]

    def annotations(self, name: str) -> Any:
        return None

    def call(self, name: str, args: Dict[str, Any] | None = None) -> ToolOutcome:
        self.calls.append((name, args))
        text = self.results.get(name, f"Did {name}.")
        return ToolOutcome(name, text, not text.startswith("FAIL"))


class ScriptedModel:
    """Returns one scripted reply per call and records what it was sent."""

    def __init__(self, replies: List[Dict[str, Any]]) -> None:
        self.replies = list(replies)
        self.seen: List[List[Any]] = []
        self.tools_seen: List[Any] = []

    def __call__(self, messages, tools=None) -> Dict[str, Any]:
        self.seen.append(messages)
        self.tools_seen.append(tools)
        return self.replies.pop(0) if self.replies else {"content": "Done."}


def _graph(model, runtime, tmp_path: Path | None = None, **kw) -> DesktopGraph:
    return DesktopGraph(
        generate=model,
        runtime=runtime,
        checkpoint_path=(tmp_path / "threads.sqlite") if tmp_path else None,
        **kw,
    )


def test_single_step_auto_finishes_without_second_model_call() -> None:
    model = ScriptedModel([{"content": "", "tool_calls": [_call("open", target="x")]}])
    runtime = FakeRuntime({"open": "Opened Task Manager."})
    state = _graph(model, runtime).invoke("open task manager")
    assert state["reply"] == "Opened Task Manager."
    assert len(model.seen) == 1 and runtime.calls == [("open", {"target": "x"})]


def test_multi_step_keeps_thinking_until_an_answer() -> None:
    model = ScriptedModel(
        [
            {"content": "", "tool_calls": [_call("open", target="notepad")]},
            {
                "content": "",
                "tool_calls": [_call("keyboard", action="type", text="hi")],
            },
            {"content": "<think>x</think>Opened **Notepad** and typed hi."},
        ]
    )
    runtime = FakeRuntime()
    state = _graph(model, runtime).invoke("open notepad and type hi")
    # Think blocks go; Markdown stays for the screen (the voice cleans it).
    assert state["reply"] == "Opened **Notepad** and typed hi."
    assert [c[0] for c in runtime.calls] == ["open", "keyboard"]
    # The tool result reaches the model on the next step.
    last_seen = model.seen[-1]
    assert last_seen[-1].role.value == "tool" and "keyboard" in last_seen[-1].content


def test_failed_tool_is_reported_and_not_auto_finished() -> None:
    model = ScriptedModel(
        [
            {"content": "", "tool_calls": [_call("open", target="zzz")]},
            {"content": "I couldn't find zzz."},
        ]
    )
    runtime = FakeRuntime({"open": "FAIL nothing called zzz"})
    state = _graph(model, runtime).invoke("open zzz")
    assert state["reply"] == "I couldn't find zzz."
    assert model.seen[-1][-1].content.startswith("Error: FAIL")


def test_risky_call_needs_confirmation() -> None:
    model = ScriptedModel(
        [
            {"content": "", "tool_calls": [_call("system", action="shutdown")]},
            {"content": "Okay, I won't."},
        ]
    )
    runtime = FakeRuntime()
    asked = []
    graph = _graph(model, runtime, confirm=lambda q: asked.append(q) or False)
    state = graph.invoke("shut down")
    assert asked == ["Shut down the computer?"] and runtime.calls == []
    assert state["reply"] == "Okay, I won't."


def test_repeated_identical_calls_are_blocked() -> None:
    look = {"content": "", "tool_calls": [_call("screen", action="describe")]}
    model = ScriptedModel([look, look, look, look])
    runtime = FakeRuntime({"screen": "You see a browser."})
    state = _graph(model, runtime).invoke("look at the screen, then decide")
    assert len(runtime.calls) == 1  # the repeats never ran
    assert state["reply"] == "You see a browser."


def test_step_limit_forces_an_answer_without_tools() -> None:
    step = {"content": "", "tool_calls": [_call("mouse", action="scroll_down")]}
    model = ScriptedModel([step, step, {"content": "Scrolled a lot."}])
    state = _graph(model, FakeRuntime(), max_steps=2).invoke("scroll and scroll")
    assert model.tools_seen[-1] is None and state["reply"] == "Scrolled a lot."


def test_memory_note_rides_on_the_request_only(tmp_path: Path) -> None:
    from openjarvis.desktop.memory import MemoryStore

    memory = MemoryStore(tmp_path / "m.sqlite")
    memory.remember("My favourite music is lofi")
    model = ScriptedModel([{"content": "You like lofi."}])
    graph = _graph(
        model, FakeRuntime(), memory=memory, context_fn=lambda: "Active window: X"
    )
    graph.invoke("what music do I like")
    sent = model.seen[0]
    assert sent[0].role.value == "system" and "lofi" not in sent[0].content
    assert "lofi" in sent[-1].content and "Active window: X" in sent[-1].content
    stored = graph.history_messages()
    assert stored[0]["content"] == "what music do I like"  # note not persisted


def test_thread_persists_across_restarts_and_records_fast_path(tmp_path: Path) -> None:
    graph = _graph(ScriptedModel([{"content": "Hi!"}]), FakeRuntime(), tmp_path)
    graph.invoke("hello")
    graph.record("open notepad", "open", {"target": "notepad"}, "Opened Notepad.", True)
    graph.close()
    model = ScriptedModel([{"content": "You opened Notepad."}])
    again = _graph(model, FakeRuntime(), tmp_path)
    again.invoke("what did I just open?")
    contents = [m.content for m in model.seen[0]]
    assert "hello" in contents and "Opened Notepad." in contents
    again.reset()
    assert again.history_messages() == []


def test_text_tool_calls_are_parsed() -> None:
    text = 'ok <tool_call>{"name": "open", "arguments": {"target": "x"}}</tool_call>'
    calls = parse_text_tool_calls(text)
    assert calls[0]["name"] == "open" and json.loads(calls[0]["arguments"]) == {
        "target": "x"
    }
    assert clean_reply(text) == "ok"


def test_window_starts_at_a_user_turn_and_shrinks_old_results() -> None:
    messages = [
        {"role": "tool", "content": "orphan"},
        {"role": "user", "content": "a"},
        {"role": "tool", "content": "x" * 1000},
        {"role": "user", "content": "b"},
        {"role": "tool", "content": "y" * 1000},
    ]
    out = window(messages, 10)
    assert out[0]["content"] == "a"
    assert len(out[1]["content"]) < 500 and len(out[3]["content"]) == 1000


@pytest.mark.parametrize(
    ("request_text", "single"),
    [("open notepad", True), ("open notepad and type hi", False), ("a, b", False)],
)
def test_single_step_detection(request_text: str, single: bool) -> None:
    assert is_single_step(request_text) is single


def test_empty_first_reply_is_retried_once() -> None:
    model = ScriptedModel(
        [{"content": ""}, {"content": "", "tool_calls": [_call("open", target="x")]}]
    )
    runtime = FakeRuntime({"open": "Opened X."})
    state = _graph(model, runtime).invoke("open x")
    assert state["reply"] == "Opened X." and len(model.seen) == 2


@pytest.mark.parametrize(
    ("request_text", "hint"),
    [
        ("Email my friend asking if he is free", "Use the email tool."),
        ("send hi on WhatsApp", "Use the whatsapp tool."),
        ("what is on my screen", "Use the screen tool."),
        ("check my gmail", ""),
        ("open notepad", ""),
    ],
)
def test_tool_hints(request_text: str, hint: str) -> None:
    from openjarvis.desktop.graph import tool_hint

    assert tool_hint(request_text) == hint
