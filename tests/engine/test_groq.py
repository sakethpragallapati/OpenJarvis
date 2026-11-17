"""Groq as Jarvis's brain (and ears): engine, key storage, settings."""

from __future__ import annotations

import json
import sys
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List

import httpx
import pytest

from openjarvis.core.types import Message, Role


def _mock(engine: Any, handler) -> List[Dict[str, Any]]:
    """Route the engine's HTTP calls to ``handler``; returns the requests."""
    seen: List[Dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}") if request.content else {}
        seen.append(
            {"path": request.url.path, "body": body, "headers": request.headers}
        )
        return handler(request, body)

    engine._client = httpx.Client(
        base_url=engine._host,
        headers=engine._headers,
        transport=httpx.MockTransport(respond),
    )
    return seen


def _reply(text: str = "Done.") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        },
    )


def test_groq_is_an_engine_with_its_own_key(monkeypatch) -> None:
    from openjarvis.core.config import JarvisConfig
    from openjarvis.core.registry import EngineRegistry
    from openjarvis.engine._discovery import _make_engine
    from openjarvis.engine.groq import GroqEngine

    # The suite clears registries before each test; importing registers it.
    if not EngineRegistry.contains("groq"):
        EngineRegistry.register("groq")(GroqEngine)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_from_the_environment")
    engine = _make_engine("groq", JarvisConfig())
    assert isinstance(engine, GroqEngine)
    assert engine._host == "https://api.groq.com/openai"
    assert engine._headers == {"Authorization": "Bearer gsk_from_the_environment"}


def test_requests_drop_ollama_options_and_send_images_as_parts(monkeypatch) -> None:
    from openjarvis.engine.groq import GroqEngine

    engine = GroqEngine(api_key="gsk_test_key_123456789")
    seen = _mock(engine, lambda request, body: _reply("A dark desktop."))
    shot = Message(role=Role.USER, content="What's on the screen?", images=["QUJD"])
    result = engine.generate(
        [shot], model="qwen/qwen3.8-27b", num_ctx=8192, num_gpu=99, max_tokens=50
    )
    assert result["content"] == "A dark desktop."
    body = seen[0]["body"]
    assert seen[0]["path"] == "/openai/v1/chat/completions"
    assert "num_ctx" not in body and "num_gpu" not in body
    content = body["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "What's on the screen?"}
    assert content[1]["image_url"]["url"] == "data:image/png;base64,QUJD"
    assert "images" not in body["messages"][0]


def test_chat_models_leave_out_speech_and_guard_models() -> None:
    from openjarvis.engine.groq import GroqEngine

    engine = GroqEngine(api_key="gsk_test_key_123456789")
    listed = ["whisper-large-v3-turbo", "openai/gpt-oss-20b", "qwen/qwen3.8-27b",
              "openai/gpt-oss-safeguard-20b", "llama-3.3-70b-versatile"]  # fmt: skip
    _mock(
        engine,
        lambda request, body: httpx.Response(
            200, json={"data": [{"id": m} for m in listed]}
        ),
    )
    assert engine.chat_models() == [
        "llama-3.3-70b-versatile",
        "openai/gpt-oss-20b",
        "qwen/qwen3.8-27b",
    ]


def test_listen_uses_the_groq_model_not_the_local_one() -> None:
    from openjarvis.cli._model_switch import resolve_chat_cli_model
    from openjarvis.core.config import JarvisConfig

    config = JarvisConfig()
    config.intelligence.default_model = "qwen3.5:4b"
    config.engine.groq.model = "openai/gpt-oss-20b"
    pick = lambda name: resolve_chat_cli_model(  # noqa: E731
        console=None,
        config=config,
        engine=None,
        engine_name=name,
        cli_model=None,
        chat_variant="chat",
    )
    assert pick("groq") == "openai/gpt-oss-20b"
    assert pick("ollama") == "qwen3.5:4b"


def test_groq_whisper_sends_the_name_hints(monkeypatch) -> None:
    from openjarvis.speech.groq_whisper import GroqWhisperBackend

    backend = GroqWhisperBackend(api_key="gsk_test_key_123456789")
    sent: Dict[str, Any] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        sent["url"] = str(request.url)
        sent["body"] = request.content
        return httpx.Response(200, json={"text": " Open VS Code. "})

    backend._client = httpx.Client(transport=httpx.MockTransport(respond))
    result = backend.transcribe(b"RIFFdata", language="en", hotwords="VS Code, Brave")
    assert result.text == "Open VS Code."
    assert sent["url"].endswith("/openai/v1/audio/transcriptions")
    assert (
        b"whisper-large-v3-turbo" in sent["body"] and b"VS Code, Brave" in sent["body"]
    )
    assert backend.cloud and backend.health()
    assert not GroqWhisperBackend(api_key="").health()


def test_the_voice_loop_gives_groq_its_hints_but_skips_local_options() -> None:
    from openjarvis.cli import _voice_chat
    from openjarvis.cli._voice_chat import VoiceSession  # noqa: F401

    session = _voice_chat.VoiceSession.__new__(_voice_chat.VoiceSession)
    session._config = None
    session._speech_config = lambda: SimpleNamespace(
        vocabulary="Minecraft Server", vad_filter=True, contact_hints=False
    )
    session.contact_hints = lambda: []
    options = session.stt_options(SimpleNamespace(backend_id="groq"))
    assert set(options) == {"hotwords"} and "Minecraft Server" in options["hotwords"]
    assert session.stt_options(SimpleNamespace(backend_id="openai")) == {}


# ---------------------------------------------------------------------------
# The key store and the settings the JARVIS window writes
# ---------------------------------------------------------------------------


def test_environment_key_wins(monkeypatch) -> None:
    from openjarvis.core import secrets

    monkeypatch.setenv("GROQ_API_KEY", "gsk_env")
    monkeypatch.setattr(secrets, "_vault_get", lambda name: "gsk_vault")
    assert secrets.get_key("groq") == "gsk_env"
    monkeypatch.delenv("GROQ_API_KEY")
    assert secrets.get_key("groq") == "gsk_vault"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager")
def test_credential_manager_round_trip(monkeypatch) -> None:
    from openjarvis.core import secrets

    name = f"pytest-{uuid.uuid4().hex[:8]}"  # a throwaway entry, removed below
    monkeypatch.delenv(f"{name.upper().replace('-', '_')}_API_KEY", raising=False)
    try:
        assert secrets.get_key(name) is None
        secrets.set_key(name, "gsk_round_trip_ü")
        assert secrets.get_key(name) == "gsk_round_trip_ü"
    finally:
        secrets.set_key(name, "")
    assert secrets.get_key(name) is None


@pytest.fixture
def settings_home(tmp_path, monkeypatch):
    from openjarvis.core import secrets
    from openjarvis.hud import settings

    config = tmp_path / "config.toml"
    config.write_text(
        '# my settings\n[engine]\ndefault = "ollama"\n\n[speech]\nmodel = "small.en"\n'
        '\n[desktop]\nvision_model = "qwen3.5:4b"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "_config_path", lambda: config)
    keys: Dict[str, str] = {}
    monkeypatch.setattr(secrets, "_vault_get", lambda name: keys.get(name))
    monkeypatch.setattr(
        secrets, "_vault_set", lambda name, value: keys.update({name: value})
    )
    monkeypatch.setattr(secrets, "_vault_delete", lambda name: keys.pop(name, None))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    return config, keys


def test_groq_needs_a_key_before_it_can_be_chosen(settings_home) -> None:
    from openjarvis.hud import settings

    ok, message = settings.save({"engine": "groq", "stt": "local"})
    assert not ok and "key" in message


def test_saving_the_choice_keeps_your_other_settings(
    settings_home, monkeypatch
) -> None:
    import tomllib

    from openjarvis.hud import settings

    config, keys = settings_home
    monkeypatch.setattr(
        settings,
        "check_groq_key",
        lambda key: (True, "Key works.", ["qwen/qwen3.8-27b"]),
    )
    ok, _message, models = settings.save_groq_key("gsk_" + "x" * 30)
    assert ok and keys["groq"].startswith("gsk_") and models
    assert "gsk_" not in config.read_text(encoding="utf-8")  # never in the file
    ok, _message = settings.save(
        {"engine": "groq", "stt": "groq", "groq_model": "openai/gpt-oss-20b"}
    )
    assert ok
    text = config.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    assert text.startswith("# my settings")
    assert data["engine"]["default"] == "groq"
    assert data["engine"]["groq"]["model"] == "openai/gpt-oss-20b"
    assert data["speech"] == {"model": "small.en", "backend": "groq"}
    assert "vision_model" not in data["desktop"]  # a local model name
    settings.save({"engine": "local", "stt": "local", "local_model": "qwen3.5:4b"})
    data = tomllib.loads(config.read_text(encoding="utf-8"))
    assert data["engine"]["default"] == "ollama"
    assert data["engine"]["groq"]["model"] == "openai/gpt-oss-20b"  # remembered
    assert data["speech"]["backend"] == "faster-whisper"


def test_bad_keys_are_refused_without_a_network_call() -> None:
    from openjarvis.hud import settings

    ok, message, _models = settings.check_groq_key("not-a-key")
    assert not ok and "gsk_" in message
