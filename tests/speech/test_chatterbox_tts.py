"""Chatterbox-Turbo backend with a fake ``chatterbox`` package."""

from __future__ import annotations

import io
import sys
import types
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from openjarvis.core.registry import TTSRegistry


class _FakeModel:
    sr = 24000
    instances = 0

    def __init__(self, device: str) -> None:
        _FakeModel.instances += 1
        self.device = device
        self.conds = "builtin"
        self.prepared: list = []
        self.texts: list = []

    @classmethod
    def from_pretrained(cls, device: str):
        if device == "cuda" and getattr(cls, "cuda_breaks", False):
            raise RuntimeError("CUDA error: no kernel image")
        return cls(device)

    def prepare_conditionals(self, path: str) -> None:
        self.prepared.append(Path(path).name)
        self.conds = f"clone:{Path(path).name}"

    def generate(self, text: str):
        import torch

        if self.device == "cuda" and getattr(_FakeModel, "generate_breaks", False):
            raise RuntimeError("CUDA out of memory")
        self.texts.append((text, self.conds))
        return torch.zeros(1, 2400)


@pytest.fixture
def chatterbox(monkeypatch, tmp_path: Path):
    pytest.importorskip("torch")
    pkg = types.ModuleType("chatterbox")
    turbo = types.ModuleType("chatterbox.tts_turbo")
    turbo.ChatterboxTurboTTS = _FakeModel
    monkeypatch.setitem(sys.modules, "chatterbox", pkg)
    monkeypatch.setitem(sys.modules, "chatterbox.tts_turbo", turbo)
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path))
    _FakeModel.instances = 0
    _FakeModel.cuda_breaks = False
    _FakeModel.generate_breaks = False
    import openjarvis.speech.chatterbox_tts as mod

    return mod


def test_registered() -> None:
    from openjarvis.speech.chatterbox_tts import ChatterboxTTSBackend

    if not TTSRegistry.contains("chatterbox"):  # registries reset between tests
        TTSRegistry.register_value("chatterbox", ChatterboxTTSBackend)
    assert TTSRegistry.get("chatterbox") is ChatterboxTTSBackend


def test_resolve_voice(chatterbox, tmp_path: Path) -> None:
    voices = tmp_path / "voices"
    voices.mkdir()
    (voices / "jarvis.wav").write_bytes(b"x")
    clip = tmp_path / "me.mp3"
    clip.write_bytes(b"x")
    assert chatterbox.resolve_voice("default") is None
    assert chatterbox.resolve_voice("") is None
    assert chatterbox.resolve_voice("jarvis") == voices / "jarvis.wav"
    assert chatterbox.resolve_voice(str(clip)) == clip
    assert chatterbox.resolve_voice("nobody") is None


def test_synthesize_clones_once_and_returns_wav(chatterbox, tmp_path: Path) -> None:
    voices = tmp_path / "voices"
    voices.mkdir()
    (voices / "jarvis.wav").write_bytes(b"x")
    backend = chatterbox.ChatterboxTTSBackend(device="cpu")
    first = backend.synthesize("Opening VS Code.", voice_id="jarvis")
    backend.synthesize("Done.", voice_id="jarvis")
    model = backend._model
    assert model.prepared == ["jarvis.wav"]  # conditionals cached per voice
    assert model.texts == [
        ("Opening VS Code.", "clone:jarvis.wav"),
        ("Done.", "clone:jarvis.wav"),
    ]
    data, rate = sf.read(io.BytesIO(first.audio))
    assert rate == 24000 and len(data) == 2400
    assert first.duration_seconds == pytest.approx(0.1)
    assert first.metadata["backend"] == "chatterbox"
    backend.synthesize("Back to default.", voice_id="default")
    assert model.texts[-1] == ("Back to default.", "builtin")
    assert "jarvis" in backend.available_voices()
    assert backend.synthesize("   ").audio == b""


def test_falls_back_to_cpu_when_cuda_fails(chatterbox, monkeypatch) -> None:
    _FakeModel.cuda_breaks = True
    backend = chatterbox.ChatterboxTTSBackend(device="cuda")
    assert backend.health() is True
    assert backend._device == "cpu"


def test_out_of_memory_mid_reply_moves_to_cpu(chatterbox, tmp_path: Path) -> None:
    voices = tmp_path / "voices"
    voices.mkdir()
    (voices / "jarvis.wav").write_bytes(b"x")
    _FakeModel.generate_breaks = True
    backend = chatterbox.ChatterboxTTSBackend(device="cuda")
    result = backend.synthesize("Still talking.", voice_id="jarvis")
    assert result.metadata["device"] == "cpu" and result.audio
    assert backend._model.texts == [("Still talking.", "clone:jarvis.wav")]


def test_health_false_without_package(monkeypatch) -> None:
    import openjarvis.speech.chatterbox_tts as mod

    monkeypatch.setitem(sys.modules, "chatterbox", None)
    monkeypatch.setitem(sys.modules, "chatterbox.tts_turbo", None)
    assert mod.ChatterboxTTSBackend().health() is False
    with pytest.raises(RuntimeError, match="chatterbox-tts is not installed"):
        mod.ChatterboxTTSBackend()._ensure_model()


def test_speed_changes_length(chatterbox) -> None:
    pytest.importorskip("librosa")
    backend = chatterbox.ChatterboxTTSBackend(device="cpu")
    normal = backend.synthesize("hi", voice_id="default")
    fast = backend.synthesize("hi", voice_id="default", speed=1.25)
    assert fast.duration_seconds < normal.duration_seconds
    assert np.isfinite(fast.duration_seconds)
