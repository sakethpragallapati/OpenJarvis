"""Wake-word detector loop and barge-in (openWakeWord mocked out)."""

from __future__ import annotations

import sys
import time
from types import SimpleNamespace

import pytest

from openjarvis.speech.wake_word import FRAME_SAMPLES, BargeIn, WakeWordDetector

_SILENCE = bytes(FRAME_SAMPLES * 2)


class _Model:
    def __init__(self, scores: list[float]) -> None:
        self.models = {"hey_jarvis_v0.1": object()}
        self._scores = iter(scores)
        self.resets = 0

    def predict(self, samples):
        assert len(samples) == FRAME_SAMPLES
        return {"hey_jarvis_v0.1": next(self._scores, 0.0)}

    def reset(self):
        self.resets += 1


class _Stream:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def read(self, n):
        time.sleep(0.001)
        return _SILENCE, False


@pytest.fixture()
def fake_mic(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "sounddevice",
        SimpleNamespace(RawInputStream=lambda **kwargs: _Stream()),
    )


def _detector(scores: list[float], threshold: float = 0.5) -> WakeWordDetector:
    detector = WakeWordDetector("hey_jarvis", threshold)
    detector._model = _Model(scores)
    detector._key = "hey_jarvis_v0.1"
    return detector


def test_label_is_readable() -> None:
    assert WakeWordDetector("hey_jarvis").label == "hey jarvis"
    assert WakeWordDetector("C:/models/hey_buddy_v2.onnx").label == "hey buddy"


@pytest.mark.parametrize("bad", [0.0, 1.0, -1])
def test_threshold_must_be_a_probability(bad) -> None:
    with pytest.raises(ValueError):
        WakeWordDetector(threshold=bad)


def test_wait_returns_when_score_crosses_threshold(fake_mic) -> None:
    detector = _detector([0.1, 0.2, 0.7])
    assert detector.wait() is True
    assert detector._model.resets >= 2  # before listening and after the hit


def test_wait_can_be_aborted(fake_mic) -> None:
    calls = iter([False, False, True])
    detector = _detector([0.0] * 10)
    assert detector.wait(should_abort=lambda: next(calls)) is False


def test_barge_in_uses_a_higher_threshold(fake_mic) -> None:
    detector = _detector([0.6] * 5 + [0.9])
    with BargeIn(detector) as barge:
        deadline = time.monotonic() + 2
        while not barge.heard() and time.monotonic() < deadline:
            time.sleep(0.01)
    assert barge.heard()
    # 0.6 alone (above the normal 0.5) must not have triggered it.
    assert detector._model._scores.__length_hint__() == 0


def test_missing_openwakeword_explains_the_extra(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "openwakeword", None)
    with pytest.raises(RuntimeError, match="--extra wakeword"):
        WakeWordDetector().load()
