"""``jarvis listen``: defaults, autostart, and the wake-word input loop."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from openjarvis.cli import _assistant, listen_cmd
from openjarvis.cli._voice_chat import VOICE_EXIT
from openjarvis.core.config import JarvisConfig


def _run_listen(args: list[str], config: JarvisConfig):
    with (
        patch("openjarvis.core.config.load_config", return_value=config),
        patch("openjarvis.cli.chat_cmd.chat") as chat,
    ):
        result = CliRunner().invoke(listen_cmd.listen, args)
    return result, (chat.call_args.kwargs if chat.called else {})


def test_listen_defaults_to_the_desktop_agent() -> None:
    with patch("openjarvis.agents.desktop.desktop_agent_available", return_value=True):
        result, kwargs = _run_listen([], JarvisConfig())
    assert result.exit_code == 0, result.output
    assert kwargs["wake_mode"] is True and kwargs["voice_mode"] is True
    assert kwargs["agent_name"] == _assistant.LISTEN_DEFAULT_AGENT == "desktop"
    # The desktop agent brings its own PC tools; only these are bridged in.
    assert kwargs["tools"] == _assistant.LISTEN_DESKTOP_TOOLS
    assert kwargs["skip_runtime_panel"] is True


def test_listen_falls_back_to_orchestrator_without_the_agent_extra() -> None:
    with patch("openjarvis.agents.desktop.desktop_agent_available", return_value=False):
        result, kwargs = _run_listen([], JarvisConfig())
    assert kwargs["agent_name"] == _assistant.LISTEN_FALLBACK_AGENT
    assert "pc_control" in kwargs["tools"] and "set_reminder" in kwargs["tools"]
    assert "uv sync --extra agent" in result.output


def test_listen_keeps_configured_agent_but_uses_the_voice_tool_set() -> None:
    config = JarvisConfig()
    config.agent.default_agent = "native_react"
    config.tools.enabled = "code_interpreter,shell_exec"
    _, kwargs = _run_listen([], config)
    assert kwargs["agent_name"] is None
    # Chat tools would add prompt tokens (latency) on every voice turn.
    assert kwargs["tools"] == _assistant.LISTEN_DEFAULT_TOOLS


def test_explicit_tools_win() -> None:
    _, kwargs = _run_listen(["--tools", "calculator"], JarvisConfig())
    assert kwargs["tools"] == "calculator"


def test_autostart_writes_and_removes_startup_script(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(listen_cmd.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    runner = CliRunner()

    on = runner.invoke(listen_cmd.listen, ["--autostart"])
    script = (
        tmp_path
        / "Microsoft/Windows/Start Menu/Programs/Startup"
        / listen_cmd._STARTUP_FILE
    )
    assert on.exit_code == 0, on.output
    assert "-m openjarvis.cli listen" in script.read_text(encoding="utf-8")

    off = runner.invoke(listen_cmd.listen, ["--no-autostart"])
    assert off.exit_code == 0 and not script.exists()


def test_autostart_is_windows_only(monkeypatch) -> None:
    monkeypatch.setattr(listen_cmd.sys, "platform", "linux")
    result = CliRunner().invoke(listen_cmd.listen, ["--autostart"])
    assert result.exit_code != 0 and "Windows only" in result.output


def _announcer() -> _assistant.ReminderAnnouncer:
    return _assistant.ReminderAnnouncer(MagicMock(), speak_aloud=True)


def test_wake_then_record() -> None:
    detector = SimpleNamespace(label="hey jarvis", wait=MagicMock(return_value=True))
    with (
        patch("openjarvis.speech.voice_io.play_chime"),
        patch.object(_assistant, "record_voice", return_value="open notepad"),
    ):
        result = _assistant.wait_for_wake_command(
            MagicMock(), MagicMock(), detector, _announcer()
        )
    assert result == "open notepad"


def test_exit_phrase_ends_the_session() -> None:
    detector = SimpleNamespace(label="hey jarvis", wait=MagicMock(return_value=True))
    with (
        patch("openjarvis.speech.voice_io.play_chime"),
        patch.object(_assistant, "record_voice", return_value="Stop listening."),
    ):
        result = _assistant.wait_for_wake_command(
            MagicMock(), MagicMock(), detector, _announcer()
        )
    assert result is VOICE_EXIT


def test_ctrl_c_while_waiting_exits() -> None:
    detector = SimpleNamespace(
        label="hey jarvis", wait=MagicMock(side_effect=KeyboardInterrupt)
    )
    result = _assistant.wait_for_wake_command(
        MagicMock(), MagicMock(), detector, _announcer()
    )
    assert result is VOICE_EXIT


def test_pending_reminders_are_spoken_while_waiting() -> None:
    announcer = _announcer()
    announcer.pending.put("Reminder: tea")
    spoken = []
    detector = SimpleNamespace(label="hey jarvis", wait=MagicMock(return_value=True))
    with (
        patch.object(
            _assistant, "speak", side_effect=lambda text, *a, **k: spoken.append(text)
        ),
        patch("openjarvis.speech.voice_io.play_chime"),
        patch.object(_assistant, "record_voice", return_value="thanks"),
    ):
        _assistant.wait_for_wake_command(MagicMock(), MagicMock(), detector, announcer)
    assert spoken == ["Reminder: tea"]


def test_barge_in_skips_the_wake_word() -> None:
    detector = SimpleNamespace(label="hey jarvis", wait=MagicMock())
    with (
        patch("openjarvis.speech.voice_io.play_chime"),
        patch.object(_assistant, "record_voice", return_value="what time is it"),
    ):
        result = _assistant.wait_for_wake_command(
            MagicMock(), MagicMock(), detector, _announcer(), skip_wake=True
        )
    assert result == "what time is it"
    detector.wait.assert_not_called()


def test_cut_off_sentence_listens_for_the_rest() -> None:
    session = MagicMock()
    with patch.object(_assistant, "record_voice", return_value="WhatsApp with Daddy."):
        full = _assistant.complete_sentence(
            MagicMock(), session, "Open a chat in", "hey jarvis"
        )
    assert full == "Open a chat in WhatsApp with Daddy."
    with patch.object(_assistant, "record_voice") as record:
        assert _assistant.complete_sentence(MagicMock(), session, "Open Notepad") == (
            "Open Notepad"
        )
    record.assert_not_called()


def test_spoken_yes_and_no() -> None:
    assert _assistant.is_affirmative("Yes.") and _assistant.is_affirmative("okay do it")
    assert not _assistant.is_affirmative("no") and not _assistant.is_affirmative(
        "yesterday"
    )


@pytest.mark.parametrize(
    ("text", "in_conversation", "verdict"),
    [
        ("Open YouTube.", False, "command"),
        ("Set an alarm for 7 am", False, "command"),
        ("pause the music", False, "command"),
        ("Pause", False, "command"),
        ("We will see you in the next one", False, "ignore"),
        ("I think we should go to the mall later", False, "ignore"),
        ("What time is it?", False, "ignore"),
        ("What time is it?", True, "question"),
        ("Jarvis, what time is it?", False, "question"),
        ("Jarvis, pause.", False, "pause"),
        ("Go to sleep", False, "pause"),
        ("open", False, "ignore"),
    ],
)
def test_always_listening_classifier(
    text: str, in_conversation: bool, verdict: str
) -> None:
    assert (
        _assistant.classify_utterance(text, in_conversation=in_conversation) == verdict
    )


def test_always_listening_ignores_chatter_then_takes_a_command() -> None:
    heard = iter([None, "we'll be right back after this", "Open Notepad."])
    listener = _assistant.AlwaysListener()
    detector = SimpleNamespace(label="hey jarvis", wait=MagicMock())
    console = MagicMock()
    with patch.object(
        _assistant, "record_voice", side_effect=lambda *a, **k: next(heard)
    ):
        result = _assistant.listen_always(
            console, MagicMock(), detector, _announcer(), listener
        )
    assert result == "Open Notepad."
    detector.wait.assert_not_called()  # no wake word needed
    printed = " ".join(str(c.args[0]) for c in console.print.call_args_list)
    assert "not for me" in printed


def test_pause_switches_to_the_wake_word() -> None:
    listener = _assistant.AlwaysListener()
    detector = SimpleNamespace(label="hey jarvis", wait=MagicMock(return_value=True))
    with (
        patch.object(_assistant, "record_voice", return_value="Jarvis, pause."),
        patch.object(_assistant, "speak"),
    ):
        assert (
            _assistant.listen_always(
                MagicMock(), MagicMock(), detector, _announcer(), listener
            )
            is None
        )
    assert listener.paused
    with (
        patch("openjarvis.speech.voice_io.play_chime"),
        patch.object(_assistant, "record_voice", return_value="open notepad"),
    ):
        result = _assistant.listen_always(
            MagicMock(), MagicMock(), detector, _announcer(), listener
        )
    assert result == "open notepad" and not listener.paused
    detector.wait.assert_called()


def test_listen_always_flag_reaches_chat() -> None:
    _, kwargs = _run_listen(["--always"], JarvisConfig())
    assert kwargs["always_listen"] is True
    _, kwargs = _run_listen([], JarvisConfig())
    assert kwargs["always_listen"] is None  # falls back to speech.listen_mode
