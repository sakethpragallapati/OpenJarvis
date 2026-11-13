"""Microphone noise calibration and interruptible playback."""

from __future__ import annotations

import struct
import sys
from types import SimpleNamespace

import pytest

from openjarvis.cli._voice_chat import VoiceSession
from openjarvis.core.config import JarvisConfig
from openjarvis.speech import voice_io


class _Stream:
    def __init__(self, levels: list[int]) -> None:
        self._frames = iter(struct.pack("1024h", *([level] * 1024)) for level in levels)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def read(self, chunk):
        return next(self._frames), False


@pytest.mark.parametrize(
    ("noise", "expected"),
    [(0.0, 200), (100.0, 300), (900.0, 2700), (5000.0, 4000)],
)
def test_threshold_from_noise_is_clamped(noise, expected) -> None:
    assert voice_io.threshold_from_noise(noise) == expected


def test_noise_floor_is_the_median_level(monkeypatch) -> None:
    stream = _Stream([100, 3000, 120, 110, 105])  # one cough mid-measurement
    monkeypatch.setitem(
        sys.modules,
        "sounddevice",
        SimpleNamespace(RawInputStream=lambda **kwargs: stream),
    )

    noise = voice_io.measure_noise_floor(seconds=5 * 1024 / 16000)

    assert noise == pytest.approx(110)


def test_session_uses_pinned_threshold_without_touching_the_mic(monkeypatch) -> None:
    config = JarvisConfig()
    config.speech.silence_threshold = 750

    def no_mic(**kwargs):
        raise AssertionError("mic must not be opened")

    monkeypatch.setattr(voice_io, "measure_noise_floor", no_mic)
    session = VoiceSession(config)

    assert session.calibrate_microphone() == 750
    assert session.silence_threshold == 750


def test_session_calibrates_from_room_noise(monkeypatch) -> None:
    monkeypatch.setattr(voice_io, "measure_noise_floor", lambda **kwargs: 400.0)
    session = VoiceSession(JarvisConfig())

    assert session.calibrate_microphone() == 1200


def test_calibration_failure_keeps_default(monkeypatch) -> None:
    def broken(**kwargs):
        raise OSError("no input device")

    monkeypatch.setattr(voice_io, "measure_noise_floor", broken)
    session = VoiceSession(JarvisConfig())

    assert session.calibrate_microphone() is None
    assert session.silence_threshold is None


def test_play_wav_stops_when_asked(monkeypatch) -> None:
    stopped = []
    fake_sd = SimpleNamespace(
        play=lambda data, sr: None,
        wait=lambda: None,
        stop=lambda: stopped.append(True),
    )
    fake_sf = SimpleNamespace(read=lambda buf, dtype: ([0.0] * 24000 * 5, 24000))
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    monkeypatch.setitem(sys.modules, "soundfile", fake_sf)

    finished = voice_io.play_wav(b"RIFF", should_stop=lambda: True)

    assert finished is False
    assert stopped == [True]
