"""Digest audio playback (#1211: silent on native Windows)."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from openjarvis.cli import digest_cmd


def _audio_file_available(path: Path) -> bool:
    pytest.importorskip("fastapi")
    from openjarvis.server.digest_routes import _audio_file_available as check

    return check(path)


def test_failed_tts_path_is_not_treated_as_audio(tmp_path, monkeypatch) -> None:
    # Path("") renders as "." -- the current directory -- which exists().
    monkeypatch.chdir(tmp_path)
    assert Path("").exists()
    assert digest_cmd._has_audio(SimpleNamespace(audio_path=Path(""))) is False


def test_api_does_not_serve_a_directory_as_audio(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert _audio_file_available(Path("")) is False


def test_directory_is_not_treated_as_audio(tmp_path) -> None:
    assert digest_cmd._has_audio(SimpleNamespace(audio_path=tmp_path)) is False


def test_real_file_is_audio(tmp_path) -> None:
    audio = tmp_path / "digest.wav"
    audio.write_bytes(b"RIFF")
    assert digest_cmd._has_audio(SimpleNamespace(audio_path=audio)) is True


def test_playback_is_in_process_first(monkeypatch) -> None:
    played: list[str] = []
    monkeypatch.setattr(
        "openjarvis.speech.voice_io.play_audio_file", lambda path: played.append(path)
    )
    run = MagicMock()
    monkeypatch.setattr(subprocess, "run", run)

    digest_cmd._play_audio("digest.wav")

    assert played == ["digest.wav"]
    run.assert_not_called()


def test_playback_falls_back_to_external_players(monkeypatch) -> None:
    def broken(path):
        raise RuntimeError("no output device")

    monkeypatch.setattr("openjarvis.speech.voice_io.play_audio_file", broken)
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "ffplay":
            raise FileNotFoundError(cmd[0])
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    digest_cmd._play_audio("digest.mp3")

    assert [c[0] for c in calls] == ["ffplay", "aplay"]


def test_command_waits_for_playback_to_finish() -> None:
    finished = threading.Event()

    def slow_playback() -> None:
        finished.wait(0.3)
        finished.set()

    thread = threading.Thread(target=slow_playback, daemon=True)
    thread.start()
    digest_cmd._wait_for_playback(thread, MagicMock())

    assert finished.is_set()
    assert not thread.is_alive()
