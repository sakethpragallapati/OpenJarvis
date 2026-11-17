"""Speech-to-text name hints and transcript corrections."""

from __future__ import annotations

from typing import Any

import pytest

from openjarvis.speech.vocabulary import (
    correct_transcript,
    hotwords,
    is_phantom,
    vocabulary,
)


@pytest.mark.parametrize(
    ("heard", "fixed"),
    [
        ("Open VS core.", "Open VS Code."),
        ("Open V's code.", "Open VS Code."),
        ("open whats app", "open WhatsApp"),
        ("open task manger", "open Task Manager"),
        ("Open file explore", "Open File Explorer"),
        ("Open Git hub and search", "Open GitHub and search"),
        # Sound-alikes every Whisper size produced on noisy test commands.
        ("Open vase code", "Open VS Code"),
        ("Open BS Code.", "Open VS Code."),
        ("open v.s. code", "open VS Code"),
        ("Set them along for 11 43 p.m.", "set an alarm for 11:43 p.m."),
        ("set an alarm for 1143 p.m.", "set an alarm for 11:43 p.m."),
        ("Smooth the alarm for 10 minutes", "snooze the alarm for 10 minutes"),
        ("snoose", "snooze"),
        ("play Loafy hip-hop on YouTube", "play lofi hip-hop on YouTube"),
        ("OpenVase code", "Open VS Code"),
        ("OpenAsk Manager", "Open Task Manager"),
        ("open OpenJarvis on GitHub", "open OpenJarvis on GitHub"),
        ("open github on chrome", "open github on chrome"),
        ("Open Jarvis code.", "Open VS Code."),
        ("open jarvis and search", "open jarvis and search"),
        ("search GitHub for open jarvis", "search GitHub for open jarvis"),
        ("Open Git hub", "Open GitHub"),
        ("Open GitHub in Brave", "Open GitHub in Brave"),
        ("ask OpenAI", "ask OpenAI"),
        # Left alone: correct already, or ordinary words.
        ("the vase is on the table", "the vase is on the table"),
        ("smooth jazz please", "smooth jazz please"),
        ("it costs 1143 dollars", "it costs 1143 dollars"),
        ("Open the file explorer please", "Open the file explorer please"),
        ("I have a brave idea", "I have a brave idea"),
        ("the task is to manage", "the task is to manage"),
        ("call mom in 20 minutes", "call mom in 20 minutes"),
    ],
)
def test_corrections(heard: str, fixed: str) -> None:
    assert correct_transcript(heard) == fixed


@pytest.mark.parametrize(
    "text",
    [
        "We'll see you in the next one.",
        "Thanks for watching!",
        "Please subscribe",
        "you",
    ],
)
def test_phantom_phrases_are_dropped(text: str) -> None:
    assert is_phantom(text) and correct_transcript(text) == ""


def test_real_requests_are_not_phantoms() -> None:
    assert not is_phantom("Open YouTube and subscribe to MrBeast")
    assert not is_phantom("thank you")


def test_user_vocabulary_is_added_and_used() -> None:
    words = vocabulary("Valorant, OBS Studio, VS Code")
    assert words.count("VS Code") == 1 and "OBS Studio" in words
    assert correct_transcript("open OBS studios", words) == "open OBS Studio"
    hint = hotwords("Valorant")
    assert "VS Code" in hint and "Valorant" in hint and len(hint) <= 600


def test_faster_whisper_gets_command_tuning(monkeypatch) -> None:
    from openjarvis.speech import faster_whisper

    seen: dict[str, Any] = {}

    class FakeModel:
        def transcribe(self, path: str, **kwargs: Any):
            seen.update(kwargs)
            return iter([]), type("Info", (), {"language": "en", "duration": 1.0})()

    backend = faster_whisper.FasterWhisperBackend(device="cpu")
    monkeypatch.setattr(backend, "_ensure_model", lambda: FakeModel())
    backend.transcribe(b"RIFF", language="en", hotwords="VS Code", short_utterance=True)
    assert seen == {
        "language": "en",
        "hotwords": "VS Code",
        "condition_on_previous_text": False,
        "without_timestamps": True,
        "vad_filter": True,
    }
    seen.clear()
    backend.transcribe(b"RIFF", language="en")
    assert seen == {"language": "en"}  # long-form callers unchanged


def test_voice_session_passes_options_only_to_faster_whisper() -> None:
    from openjarvis.cli._voice_chat import VoiceSession
    from openjarvis.core.config import JarvisConfig

    config = JarvisConfig()
    config.speech.vocabulary = "Valorant"
    session = VoiceSession(config)
    whisper = type("B", (), {"backend_id": "faster-whisper"})()
    options = session.stt_options(whisper)
    assert options["short_utterance"] and "Valorant" in options["hotwords"]
    assert session.stt_options(type("B", (), {"backend_id": "openai"})()) == {}
    assert session.correct("open VS core") == "open VS Code"


@pytest.mark.parametrize(
    ("heard", "fixed"),
    [
        ("Set and Allow for 11.43 pm", "set an alarm for 11:43 pm"),
        ("Set an allow at 7 am", "set an alarm at 7 am"),
        ("set a time for 5 minutes", "set a timer for 5 minutes"),
        ("I allow it", "I allow it"),
        ("version 3.10 released", "version 3.10 released"),
    ],
)
def test_command_phrase_fixes(heard: str, fixed: str) -> None:
    assert correct_transcript(heard) == fixed
