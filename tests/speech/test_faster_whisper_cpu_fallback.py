"""Faster-Whisper falls back to CPU when the CUDA runtime is unusable."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from openjarvis.speech.faster_whisper import FasterWhisperBackend


def _segment(text: str) -> MagicMock:
    seg = MagicMock()
    seg.text = text
    seg.start, seg.end = 0.0, 1.0
    return seg


def _info() -> MagicMock:
    info = MagicMock()
    info.language, info.language_probability, info.duration = "en", 0.9, 1.0
    return info


def test_gpu_load_failure_retries_on_cpu() -> None:
    cpu_model = MagicMock()

    def factory(size, device, compute_type):
        if device != "cpu":
            raise RuntimeError("Could not load library cudnn_ops64_9.dll")
        return cpu_model

    with patch("openjarvis.speech.faster_whisper.WhisperModel", side_effect=factory):
        backend = FasterWhisperBackend(device="auto", compute_type="float16")
        assert backend._ensure_model() is cpu_model

    assert backend._device == "cpu"
    assert backend._compute_type == "int8"


def test_gpu_failure_during_transcribe_retries_on_cpu() -> None:
    gpu_model = MagicMock()
    gpu_model.transcribe.side_effect = RuntimeError(
        "Library cublas64_12.dll is not found or cannot be loaded"
    )
    cpu_model = MagicMock()
    cpu_model.transcribe.return_value = ([_segment(" hello")], _info())

    with patch(
        "openjarvis.speech.faster_whisper.WhisperModel",
        side_effect=[gpu_model, cpu_model],
    ) as factory:
        backend = FasterWhisperBackend(device="cuda")
        result = backend.transcribe(b"wav")

    assert result.text == "hello"
    assert factory.call_args_list[-1].kwargs["device"] == "cpu"


def test_non_gpu_errors_are_not_retried() -> None:
    model = MagicMock()
    model.transcribe.side_effect = RuntimeError("Invalid data found when processing")

    with patch(
        "openjarvis.speech.faster_whisper.WhisperModel", return_value=model
    ) as factory:
        backend = FasterWhisperBackend(device="cuda")
        with pytest.raises(RuntimeError, match="Invalid data"):
            backend.transcribe(b"wav")

    factory.assert_called_once()
    assert backend._device == "cuda"


def test_cpu_failures_are_not_retried() -> None:
    with patch(
        "openjarvis.speech.faster_whisper.WhisperModel",
        side_effect=RuntimeError("missing cublas64_12.dll"),
    ) as factory:
        backend = FasterWhisperBackend(device="cpu")
        assert backend.health() is False

    factory.assert_called_once()
