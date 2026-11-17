"""``jarvis desktop`` — the desktop agent's MCP server, memory, Gmail sign-in.

Alarms have their own command: ``jarvis alarm``.
"""

from __future__ import annotations

import click


@click.group("desktop", help="Desktop agent: MCP tool server, memory, Gmail sign-in.")
def desktop() -> None:
    pass


def _set_up_context(with_vision: bool) -> None:
    from openjarvis.desktop import server
    from openjarvis.desktop.memory import MemoryStore

    server.CONTEXT.memory = MemoryStore()
    if not with_vision:
        return
    try:
        from openjarvis.core.config import load_config
        from openjarvis.core.types import Message, Role
        from openjarvis.engine import get_engine

        config = load_config()
        resolved = get_engine(config, None)
        if resolved is None:
            return
        _name, engine = resolved
        model = config.desktop.vision_model or config.intelligence.default_model

        def ask(prompt: str, image_b64: str, max_tokens: int) -> str:
            result = engine.generate(
                [Message(role=Role.USER, content=prompt, images=[image_b64])],
                model=model,
                temperature=0.1,
                max_tokens=max_tokens,
            )
            return str(result.get("content", "") or "")

        server.CONTEXT.vision = ask
    except Exception:  # vision is optional for MCP clients
        pass


@desktop.command("mcp")
@click.option(
    "--transport",
    type=click.Choice(["stdio", "http"]),
    default="stdio",
    show_default=True,
)
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8765, show_default=True, type=int)
@click.option("--no-vision", is_flag=True, help="Don't load the vision model.")
def mcp(transport: str, host: str, port: int, no_vision: bool) -> None:
    """Serve the PC tools over MCP (for Claude Desktop, Cursor, ...).

    stdio config for an MCP client:
    {"command": "<venv>\\Scripts\\jarvis.exe", "args": ["desktop", "mcp"]}
    """
    from openjarvis.core.config import load_config
    from openjarvis.desktop.server import build_server
    from openjarvis.desktop.winapi import ensure_dpi_aware

    ensure_dpi_aware()
    _set_up_context(with_vision=not no_vision)
    config = load_config()
    server = build_server(external=config.desktop.mcp_servers or None)
    if transport == "stdio":
        server.run("stdio", show_banner=False)
    else:
        click.echo(f"Serving desktop tools at http://{host}:{port}/mcp", err=True)
        server.run("http", host=host, port=port, show_banner=False)


@desktop.command("gmail-auth")
@click.option(
    "--credentials",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="OAuth client JSON (Desktop app) downloaded from Google Cloud Console.",
)
def gmail_auth(credentials: str | None) -> None:
    """Connect your Gmail (opens Google's sign-in page in your browser)."""
    from pathlib import Path

    from openjarvis.desktop import gmail

    if not gmail.gmail_available():
        raise click.ClickException("Install the agent extra: uv sync --extra agent")
    try:
        address = gmail.authorize(Path(credentials) if credentials else None)
    except gmail.GmailError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Gmail connected: {address}. Restart `jarvis listen` to use it.")


@desktop.command("gmail-mcp")
def gmail_mcp() -> None:
    """Serve only the Gmail tools over MCP (stdio), for other MCP apps."""
    from openjarvis.desktop.gmail import build_gmail_server

    build_gmail_server().run("stdio", show_banner=False)


@desktop.command("files")
@click.option("--install", "reinstall", is_flag=True, help="(Re)install the server.")
def files(reinstall: bool) -> None:
    """Jarvis's file tools: the MCP filesystem server and its folders."""

    from openjarvis.core.config import load_config
    from openjarvis.desktop import files_mcp

    if reinstall:
        try:
            files_mcp.install()
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
    version = files_mcp.installed_version()
    node = files_mcp._which("node.exe", "node")
    click.echo(
        f"Server:  {files_mcp.PACKAGE} "
        + (f"{version} ({files_mcp.install_dir()})" if version else "not installed")
    )
    click.echo(f"Node.js: {node or 'not found (install Node.js 24+)'}")
    click.echo("Folders:")
    for folder in files_mcp.user_folders(load_config().desktop.file_folders):
        click.echo(f"  {folder}")
    if not version:
        click.echo("Run `jarvis desktop files --install` (or just start Jarvis).")


@desktop.command("tools")
def tools() -> None:
    """List the desktop tools the agent can use."""
    from openjarvis.desktop.server import TOOLS

    for name, fn, _hints in TOOLS:
        summary = " ".join((fn.__doc__ or "").split())
        click.echo(f"{name:15} {summary}")


@desktop.command("contacts")
@click.argument(
    "action", type=click.Choice(["list", "import", "open", "find"]), default="list"
)
@click.argument("value", required=False, default="")
def contacts(action: str, value: str) -> None:
    """Names Jarvis knows for WhatsApp/email: list, find a spoken name,
    open contacts.txt, or import a Google/Outlook contacts CSV or vCard
    (names only; phone numbers are not copied)."""
    from pathlib import Path

    from openjarvis.desktop.contacts import directory, import_file

    book = directory()
    if action == "import":
        if not value:
            raise click.UsageError("Give the exported file, e.g. contacts.csv.")
        source = Path(value).expanduser()
        if not source.is_file():
            raise click.ClickException(f"No file at {source}.")
        count, nicknames = import_file(source, book)
        extra = f" and {nicknames} nicknames" if nicknames else ""
        click.echo(f"Imported {count} names{extra} into {book.path}.")
        return
    if action == "open":
        import os

        book.refresh()
        os.startfile(str(book.path))  # type: ignore[attr-defined]
        return
    if action == "find":
        if not value:
            raise click.UsageError("Say which name to look up.")
        tied = book.ambiguous(value)
        if tied:
            click.echo(f"{value!r} could be: " + ", ".join(tied))
            return
        match = book.resolve(value)
        if match:
            click.echo(f"{value!r} -> {match}")
            return
        close = book.candidates(value, 3)
        hint = ", ".join(name for name, _ in close) or "nothing close"
        click.echo(f"No clear match for {value!r} ({hint}).")
        return
    book.refresh()
    click.echo(f"{book.path}")
    click.echo(
        f"{len(book.file_aliases)} spoken names, {len(book.file_names)} names, "
        f"{len(book.learned)} learned"
    )
    for spoken, name in sorted(book.file_aliases.items()):
        click.echo(f"  {spoken} = {name}")
    for spoken, name in sorted(book.learned.items()):
        click.echo(f"  {spoken} = {name}  (learned)")


@desktop.command("memory")
@click.argument(
    "action", type=click.Choice(["list", "forget", "clear"]), default="list"
)
@click.argument("memory_id", required=False, type=int)
@click.option("--kind", type=click.Choice(["fact", "place", "episode"]), default=None)
def memory(action: str, memory_id: int | None, kind: str | None) -> None:
    """Show or edit what Jarvis remembers (facts, places, recent tasks)."""
    from openjarvis.desktop.memory import MemoryStore

    store = MemoryStore()
    if action == "list":
        items = store.recent(kind, limit=200)
        if not items:
            click.echo("Nothing remembered yet.")
        for item in items:
            click.echo(f"{item.kind:8} {item.describe()}")
        click.echo(f"\n({store.path})")
    elif action == "forget":
        if memory_id is None:
            raise click.UsageError("Give the id shown by `jarvis desktop memory`.")
        click.echo("Forgotten." if store.forget(memory_id) else "No such memory.")
    else:
        label = kind or "all"
        if click.confirm(
            f"Delete {label} memories? This can't be undone.", default=False
        ):
            click.echo(f"Deleted {store.clear(kind)} memories.")


__all__ = ["desktop"]
