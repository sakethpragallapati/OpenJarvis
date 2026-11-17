"""Battery saver and low-VRAM handling for voice sessions."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from openjarvis.cli import _assistant
from openjarvis.core.config import JarvisConfig
from openjarvis.pc.power import BatteryStatus


@pytest.fixture()
def config() -> JarvisConfig:
    cfg = JarvisConfig()
    cfg.speech.device = "auto"
    cfg.power.battery_model = "qwen3.5:2b"
    return cfg


def _patch(monkeypatch, battery, vram=None) -> None:
    monkeypatch.setattr("openjarvis.pc.power.battery_status", lambda: battery)
    monkeypatch.setattr("openjarvis.pc.power.free_vram_mb", lambda: vram)


def test_on_battery_switches_model_and_speech_device(monkeypatch, config) -> None:
    _patch(monkeypatch, BatteryStatus(percent=40, plugged_in=False))
    model = _assistant.apply_power_policy(
        MagicMock(), config, "qwen3.5:9b", explicit_model=False
    )
    assert model == "qwen3.5:2b"
    assert (config.speech.device, config.speech.compute_type) == ("cpu", "int8")


def test_explicit_model_is_respected_on_battery(monkeypatch, config) -> None:
    _patch(monkeypatch, BatteryStatus(percent=40, plugged_in=False))
    model = _assistant.apply_power_policy(
        MagicMock(), config, "qwen3.5:9b", explicit_model=True
    )
    assert model == "qwen3.5:9b"
    assert config.speech.device == "cpu"


def test_plugged_in_changes_nothing(monkeypatch, config) -> None:
    _patch(monkeypatch, BatteryStatus(percent=80, plugged_in=True), vram=6000)
    console = MagicMock()
    model = _assistant.apply_power_policy(
        console, config, "qwen3.5:9b", explicit_model=False
    )
    assert model == "qwen3.5:9b"
    assert config.speech.device == "auto"
    console.print.assert_not_called()


def test_battery_saver_can_be_disabled(monkeypatch, config) -> None:
    config.power.battery_saver = False
    _patch(monkeypatch, BatteryStatus(percent=10, plugged_in=False))
    model = _assistant.apply_power_policy(
        MagicMock(), config, "qwen3.5:9b", explicit_model=False
    )
    assert model == "qwen3.5:9b"


def test_low_vram_warns_and_moves_speech_to_cpu(monkeypatch, config) -> None:
    _patch(monkeypatch, None, vram=300)
    console = MagicMock()
    _assistant.apply_power_policy(console, config, "m", explicit_model=False)
    assert "300 MB" in str(console.print.call_args_list[0])
    assert config.speech.device == "cpu"


@pytest.mark.parametrize(
    ("model", "language", "expected"),
    [
        ("small.en", "en", "base.en"),
        ("large-v3-turbo", "en", "base.en"),
        ("large-v3", "", "base"),
        ("base.en", "en", "base.en"),
        ("tiny", "", "tiny"),
        (r"C:\models\my-whisper", "en", r"C:\models\my-whisper"),
    ],
)
def test_cpu_fallback_keeps_a_fast_whisper(
    monkeypatch, config, model, language, expected
) -> None:
    config.speech.model, config.speech.language = model, language
    config.speech.device = "cuda"
    _patch(monkeypatch, None, vram=300)
    console = MagicMock()
    _assistant.apply_power_policy(console, config, "m", explicit_model=False)
    assert (config.speech.device, config.speech.model) == ("cpu", expected)


def test_battery_description() -> None:
    status = BatteryStatus(percent=22, plugged_in=False, seconds_left=2448)
    assert status.describe() == "22%, on battery, about 0h 40m left"


def test_voice_keep_alive_respects_user_setting(monkeypatch, config) -> None:
    monkeypatch.delenv("JARVIS_OLLAMA_KEEP_ALIVE", raising=False)
    assert _assistant.voice_keep_alive(config) == "30m"
    config.engine.ollama.keep_alive = "-1"
    assert _assistant.voice_keep_alive(config) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [("Stop listening.", True), ("Goodbye!", True), ("stop the music", False)],
)
def test_exit_phrases(text, expected) -> None:
    assert _assistant.is_exit_phrase(text) is expected
