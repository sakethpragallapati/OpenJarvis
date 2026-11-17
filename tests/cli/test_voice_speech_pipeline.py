"""Sentence-pipelined speech output and interruption."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from openjarvis.cli._voice_chat import (
    VoiceSession,
    speak,
    speakable_text,
    split_for_speech,
)
from openjarvis.core.config import JarvisConfig
from openjarvis.core.registry import TTSRegistry


def test_speakable_text_drops_markdown_noise() -> None:
    text = (
        "# Result\n"
        "- **Bold** item with `code`\n"
        "See [the docs](https://example.com/docs) or https://x.y/z\n"
        "```python\nprint('hi')\n```\n"
        "Done."
    )
    spoken = speakable_text(text)

    for noise in ("#", "**", "`", "](", "https://", "print("):
        assert noise not in spoken
    assert "Bold item with code" in spoken
    assert "the docs" in spoken
    assert "code shown on screen" in spoken


def test_first_chunk_stays_short_and_later_ones_merge() -> None:
    chunks = split_for_speech("Sure. It is sunny. Highs of 20. Low wind. Have fun!")

    assert chunks[0] == "Sure."
    assert len(chunks) < 5
    assert " ".join(chunks) == "Sure. It is sunny. Highs of 20. Low wind. Have fun!"


def _backend(spoken: list[str]) -> MagicMock:
    backend = MagicMock()
    backend.backend_id = "kokoro"
    backend.health.return_value = True

    def synthesize(text, **kwargs):
        spoken.append(text)
        return SimpleNamespace(audio=text.encode(), sample_rate=24000)

    backend.synthesize.side_effect = synthesize
    return backend


def test_each_sentence_is_played_in_order() -> None:
    spoken: list[str] = []
    played: list[bytes] = []
    backend = _backend(spoken)
    text = "First sentence here. " + "Second part is a much longer sentence. " * 3

    with (
        patch.object(TTSRegistry, "contains", return_value=True),
        patch.object(TTSRegistry, "get", return_value=lambda: backend),
        patch(
            "openjarvis.speech.voice_io.play_wav",
            side_effect=lambda audio, **kwargs: played.append(audio),
        ),
    ):
        assert speak(text, MagicMock(), VoiceSession(JarvisConfig())) is True

    assert len(spoken) >= 2
    assert played == [chunk.encode() for chunk in spoken]


def test_should_stop_interrupts_and_skips_the_rest() -> None:
    spoken: list[str] = []
    backend = _backend(spoken)
    console = MagicMock()

    def play(audio, *, sample_rate, should_stop):
        return not should_stop()

    with (
        patch.object(TTSRegistry, "contains", return_value=True),
        patch.object(TTSRegistry, "get", return_value=lambda: backend),
        patch("openjarvis.speech.voice_io.play_wav", side_effect=play) as player,
    ):
        result = speak(
            "One. Two is longer than one. Three is the longest of them all.",
            console,
            VoiceSession(JarvisConfig()),
            should_stop=lambda: True,
        )

    assert result is False
    assert player.call_count <= 1
    assert "Stopped speaking" in str(console.print.call_args)


def test_ctrl_c_stops_playback() -> None:
    backend = _backend([])

    with (
        patch.object(TTSRegistry, "contains", return_value=True),
        patch.object(TTSRegistry, "get", return_value=lambda: backend),
        patch("openjarvis.speech.voice_io.play_wav", side_effect=KeyboardInterrupt),
        patch("openjarvis.speech.voice_io.stop_playback") as stop,
    ):
        assert speak("Hello there.", MagicMock(), VoiceSession(JarvisConfig())) is False

    stop.assert_called_once()


def test_missing_tts_hint_names_the_voice_extra() -> None:
    console = MagicMock()
    with patch.object(TTSRegistry, "contains", return_value=False):
        speak("Hello.", console, VoiceSession(JarvisConfig()))

    assert "uv sync --extra voice" in str(console.print.call_args)


def test_the_window_hears_each_sentence_as_it_is_spoken() -> None:
    import io
    import wave

    from openjarvis.core import live

    def wav(seconds: float) -> bytes:
        out = io.BytesIO()
        with wave.open(out, "wb") as clip:
            clip.setnchannels(1)
            clip.setsampwidth(2)
            clip.setframerate(24000)
            clip.writeframes(b"\0\0" * int(24000 * seconds))
        return out.getvalue()

    backend = MagicMock()
    backend.backend_id = "fake"
    backend.health.return_value = True
    backend.synthesize.side_effect = lambda text, **kw: SimpleNamespace(
        audio=wav(len(text) / 40), sample_rate=24000
    )
    events: list = []
    unsubscribe = live.subscribe(lambda kind, data: events.append((kind, data)))
    text = "First sentence here is fine. " + "Second part is a much longer one. " * 3
    try:
        with (
            patch.object(TTSRegistry, "contains", return_value=True),
            patch.object(TTSRegistry, "get", return_value=lambda: backend),
            patch("openjarvis.speech.voice_io.play_wav", return_value=True),
        ):
            assert speak(text, MagicMock(), VoiceSession(JarvisConfig())) is True
    finally:
        unsubscribe()
    said = [d for k, d in events if k == "say"]
    assert len(said) >= 2 and [d["index"] for d in said] == list(range(len(said)))
    total = said[0]["total"]
    assert said[0]["start"] == 0 and all(d["total"] == total for d in said)
    assert said[1]["start"] == len(said[0]["text"])  # where each sentence begins
    assert said[0]["seconds"] == round(len(said[0]["text"]) / 40, 2)  # real length
    kinds = [k for k, _ in events if k in ("speaking", "say", "spoken")]
    assert kinds[0] == "speaking" and kinds[-1] == "spoken"
