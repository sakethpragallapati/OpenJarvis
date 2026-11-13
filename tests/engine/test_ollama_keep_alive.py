"""Ollama keep_alive: keep the model loaded between voice commands."""

from __future__ import annotations

import json

import httpx
import pytest

from openjarvis.core.config import JarvisConfig
from openjarvis.core.registry import EngineRegistry
from openjarvis.core.types import Message, Role
from openjarvis.engine._discovery import _make_engine
from openjarvis.engine.ollama import OllamaEngine, _normalize_keep_alive

_REPLY = {
    "message": {"role": "assistant", "content": "ok"},
    "done": True,
    "prompt_eval_count": 1,
    "eval_count": 1,
}


def _capture(engine: OllamaEngine) -> list[dict]:
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append({"path": request.url.path, **json.loads(request.content)})
        return httpx.Response(200, json=_REPLY)

    engine._client = httpx.Client(
        base_url="http://testhost:11434", transport=httpx.MockTransport(handler)
    )
    return sent


def _ask(engine: OllamaEngine, **kwargs) -> None:
    engine.generate([Message(role=Role.USER, content="hi")], model="m", **kwargs)


@pytest.mark.parametrize(
    ("raw", "expected"), [("30m", "30m"), ("-1", -1), (" 600 ", 600), (0, 0)]
)
def test_normalize_keep_alive(raw, expected) -> None:
    assert _normalize_keep_alive(raw) == expected


def test_no_keep_alive_by_default(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_OLLAMA_KEEP_ALIVE", raising=False)
    engine = OllamaEngine(host="http://testhost:11434")
    sent = _capture(engine)
    _ask(engine)
    assert "keep_alive" not in sent[0]


def test_constructor_keep_alive_is_sent(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_OLLAMA_KEEP_ALIVE", raising=False)
    engine = OllamaEngine(host="http://testhost:11434", keep_alive="-1")
    sent = _capture(engine)
    _ask(engine)
    assert sent[0]["keep_alive"] == -1


def test_env_keep_alive_and_per_call_override(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_OLLAMA_KEEP_ALIVE", "1h")
    engine = OllamaEngine(host="http://testhost:11434")
    sent = _capture(engine)
    _ask(engine)
    _ask(engine, keep_alive="30m")
    assert [s["keep_alive"] for s in sent] == ["1h", "30m"]


def test_preload_sends_empty_generate(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_OLLAMA_KEEP_ALIVE", raising=False)
    engine = OllamaEngine(host="http://testhost:11434")
    sent = _capture(engine)
    assert engine.preload("qwen3.5:4b", keep_alive="30m") is True
    assert sent == [
        {"path": "/api/generate", "model": "qwen3.5:4b", "keep_alive": "30m"}
    ]


def test_preload_reports_unreachable_server() -> None:
    engine = OllamaEngine(host="http://testhost:11434")

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    engine._client = httpx.Client(
        base_url="http://testhost:11434", transport=httpx.MockTransport(down)
    )
    assert engine.preload("m") is False


def test_config_keep_alive_reaches_engine(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_OLLAMA_KEEP_ALIVE", raising=False)
    EngineRegistry.register_value("ollama", OllamaEngine)
    config = JarvisConfig()
    config.engine.ollama.host = "http://testhost:11434"
    config.engine.ollama.keep_alive = "45m"

    engine = _make_engine("ollama", config)

    assert engine._keep_alive == "45m"
    assert engine._host == "http://testhost:11434"
