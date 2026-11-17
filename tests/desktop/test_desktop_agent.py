"""DesktopAgent end to end with a fake engine (real FastMCP + LangGraph)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("fastmcp")

from openjarvis.agents.desktop import DesktopAgent  # noqa: E402
from openjarvis.core.config import DesktopConfig  # noqa: E402
from openjarvis.desktop import server  # noqa: E402


class FakeEngine:
    engine_id = "fake"

    def __init__(self, replies: List[Dict[str, Any]]) -> None:
        self.replies = list(replies)
        self.calls: List[Dict[str, Any]] = []

    def generate(self, messages, *, model, temperature, max_tokens, **kw):
        self.calls.append({"messages": messages, "max_tokens": max_tokens, **kw})
        return self.replies.pop(0) if self.replies else {"content": "ok"}


@pytest.fixture
def make_agent(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(server, "CONTEXT", server.DesktopContext())
    agents = []

    def build(
        replies: List[Dict[str, Any]], **cfg: Any
    ) -> tuple[DesktopAgent, FakeEngine]:
        engine = FakeEngine(replies)
        agent = DesktopAgent(
            engine,
            "qwen3.5:4b",
            desktop_config=DesktopConfig(**cfg),
            memory_path=tmp_path / "memory.sqlite",
            checkpoint_path=tmp_path / "threads.sqlite",
            background_setup=False,
        )
        agents.append(agent)
        return agent, engine

    yield build
    for agent in agents:
        agent._runtime.close()
        agent._graph.close()


def test_fast_path_answers_without_the_model(make_agent) -> None:
    agent, engine = make_agent([])
    result = agent.run("What time is it?")
    assert result.metadata["fast_path"] and result.content.startswith("It's ")
    assert engine.calls == []


def test_memory_is_remembered_then_recalled_into_the_prompt(make_agent) -> None:
    agent, engine = make_agent([{"content": "You like chess."}])
    assert agent.run("Remember that I like chess").content.startswith("Got it")
    result = agent.run("What game do I like?")
    assert result.content == "You like chess." and not result.metadata["fast_path"]
    prompt = engine.calls[0]["messages"][-1].content
    assert "I like chess" in prompt  # recalled automatically
    assert engine.calls[0]["tools"]  # the FastMCP tool list was offered


def test_model_tool_call_runs_through_mcp(make_agent) -> None:
    agent, engine = make_agent(
        [
            {
                "content": "",
                "tool_calls": [
                    {
                        "id": "1",
                        "name": "memory",
                        "arguments": '{"action": "remember", "text": "exam monday"}',
                    }
                ],
            }
        ]
    )
    result = agent.run("please note that my exam is monday")
    assert result.content == "Got it, I'll remember that."  # auto-finished
    assert result.tool_results[0].tool_name == "memory"
    assert agent.memory.search("exam")[0].content == "exam monday"
    assert len(engine.calls) == 1


def test_fast_path_can_be_disabled(make_agent) -> None:
    agent, engine = make_agent([{"content": "It is late."}], fast_path=False)
    assert agent.run("What time is it?").content == "It is late."
    assert len(engine.calls) == 1


def test_warm_up_primes_the_prompt_with_tools(make_agent) -> None:
    agent, engine = make_agent([])
    agent.warm_up()
    primed = engine.calls[0]
    assert primed["max_tokens"] == 1 and primed["tools"]
    assert primed["messages"][0].content == agent._graph.system_prompt


def test_declined_confirmation_is_spoken_politely(make_agent) -> None:
    agent, _ = make_agent([])
    agent.set_confirm(lambda question: False)
    result = agent.run("Empty the recycle bin")
    assert result.content == "Okay, I won't."
