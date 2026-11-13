"""``jarvis listen`` — hands-free assistant: say "Hey Jarvis", then a request."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click

_STARTUP_FILE = "OpenJarvis Listen.cmd"


def _startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise click.ClickException(
            "APPDATA is not set; cannot find the Startup folder."
        )
    return (
        Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    )


def _startup_script() -> str:
    # Launch through the interpreter running now, so autostart uses the same
    # virtualenv and does not depend on PATH at logon.
    # A console interpreter is required: the listener prints status and reads
    # keys to interrupt replies, so pythonw.exe would leave it blind.
    python = Path(sys.executable)
    if python.name.lower() == "pythonw.exe":
        python = python.with_name("python.exe")
    return (
        "@echo off\r\n"
        "rem Created by `jarvis listen --autostart`; remove with "
        "`jarvis listen --no-autostart`.\r\n"
        f'start "OpenJarvis" /min "{python}" -m openjarvis.cli listen\r\n'
    )


def _set_autostart(enabled: bool) -> str:
    if sys.platform != "win32":
        raise click.ClickException(
            "--autostart currently supports Windows only. On Linux use a "
            "systemd user service; on macOS a LaunchAgent running `jarvis listen`."
        )
    target = _startup_dir() / _STARTUP_FILE
    if enabled:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_startup_script(), encoding="utf-8")
        return f"Jarvis will start listening when you log in ({target})."
    if target.exists():
        target.unlink()
        return "Removed the logon autostart."
    return "Autostart was not enabled."


@click.command(
    "listen",
    help=(
        "Hands-free assistant: say 'Hey Jarvis', then your request. Replies "
        "are spoken; reminders pop up and are read aloud."
    ),
)
@click.option("-e", "--engine", "engine_key", default=None, help="Engine backend.")
@click.option("-m", "--model", "model_name", default=None, help="Model id.")
@click.option(
    "-a",
    "--agent",
    "agent_name",
    default=None,
    help="Agent type (default: orchestrator, which can use tools).",
)
@click.option(
    "--tools",
    default=None,
    help="Comma-separated tools (default: PC control, clipboard, reminders, ...).",
)
@click.option(
    "--autostart/--no-autostart",
    default=None,
    help="Start (or stop starting) `jarvis listen` at Windows logon, then exit.",
)
@click.pass_context
def listen(
    ctx: click.Context,
    engine_key: str | None,
    model_name: str | None,
    agent_name: str | None,
    tools: str | None,
    autostart: bool | None,
) -> None:
    if autostart is not None:
        click.echo(_set_autostart(autostart))
        return

    from openjarvis.cli._assistant import LISTEN_DEFAULT_AGENT, LISTEN_DEFAULT_TOOLS
    from openjarvis.cli.chat_cmd import chat
    from openjarvis.core.config import load_config

    config = load_config()
    if agent_name is None and config.agent.default_agent in ("", "simple"):
        # The simple agent can't call tools, so "open VS Code" would only
        # get a chatty answer. Respect any other configured agent.
        agent_name = LISTEN_DEFAULT_AGENT
    if tools is None:
        # The desktop set, not the chat config's list: every tool's schema is
        # re-sent with each request, and code/shell/file tools add prompt
        # tokens (= latency) without being useful by voice. --tools overrides.
        tools = LISTEN_DEFAULT_TOOLS

    ctx.invoke(
        chat,
        engine_key=engine_key,
        model_name=model_name,
        pick_model=False,
        agent_name=agent_name,
        tools=tools,
        system_prompt=None,
        persona_name=None,
        voice_mode=True,
        wake_mode=True,
        num_ctx=None,
        num_gpu=None,
        skip_runtime_panel=True,
    )


__all__ = ["listen"]
