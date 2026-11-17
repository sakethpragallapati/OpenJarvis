"""``jarvis app`` - the JARVIS window, its shortcuts and autostart."""

from __future__ import annotations

import subprocess
import sys

import click


@click.command()
@click.option(
    "--install",
    "do_install",
    is_flag=True,
    help="Make the Jarvis icon and Desktop + Start-menu shortcuts, then exit.",
)
@click.option(
    "--uninstall",
    "do_uninstall",
    is_flag=True,
    help="Remove the Jarvis shortcuts (Desktop, Start menu, startup), then exit.",
)
@click.option(
    "--autostart/--no-autostart",
    default=None,
    help="Open the Jarvis window when you sign in to Windows (or stop), then exit.",
)
@click.option(
    "--demo",
    is_flag=True,
    help="Show the window with a scripted demo (no microphone, no models).",
)
def app(
    do_install: bool, do_uninstall: bool, autostart: bool | None, demo: bool
) -> None:
    """Open the JARVIS window: the voice assistant without a console.

    Same as double-clicking the Jarvis shortcut. It runs `jarvis listen`
    inside the window; say "stop listening" or close the window to quit.
    """
    from openjarvis.hud import install as setup

    if do_uninstall:
        removed = setup.uninstall()
        click.echo(
            "Removed: " + ", ".join(map(str, removed)) if removed else "No shortcuts."
        )
        return
    if do_install:
        for path in setup.install():
            click.echo(f"Shortcut: {path}")
        click.echo(f"Runs: {' '.join(setup.launcher())}")
        if autostart is None:
            return
    if autostart is not None:
        path = setup.set_autostart(autostart)
        click.echo(
            f"Jarvis will open when you sign in ({path})."
            if autostart
            else "Jarvis won't open at sign-in any more."
        )
        return
    import os

    command = setup.launcher()
    flags = 0x00000008 | 0x00000200 if sys.platform == "win32" else 0  # detached
    env = dict(os.environ, JARVIS_HUD_DEMO="1") if demo else None
    subprocess.Popen(command, creationflags=flags, close_fds=True, env=env)
    click.echo("Opening the Jarvis window" + (" (demo)…" if demo else "…"))


__all__ = ["app"]
