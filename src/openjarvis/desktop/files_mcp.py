"""Files through the official MCP filesystem server.

Jarvis's file tools are ``@modelcontextprotocol/server-filesystem``
(https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem),
mounted next to the desktop tools as ``fs_*``: ``fs_list_directory``,
``fs_search_files``, ``fs_read_text_file``, ``fs_write_file``,
``fs_edit_file``, ``fs_append_file``, ``fs_create_directory``,
``fs_move_file``, ``fs_copy_file``, ``fs_get_file_info`` ... It can only
reach your own folders (Desktop, Documents, Downloads, Pictures, Music,
Videos, plus ``[desktop] file_folders``).

The server can't launch anything, so ``open`` still opens files in their
app; it finds them through this server too (``FileStore``), then matches
what you said - partial or misheard - against the names it returns.

Installed once with npm into ``~/.openjarvis/mcp/server-filesystem``
(automatically on first start, or ``jarvis desktop files --install``).
Needs Node.js 24 or newer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

PACKAGE = "@modelcontextprotocol/server-filesystem"
VERSION = "2.2.0"  # pinned; newer releases are checked before moving up
NAMESPACE = "fs"

# Not shown to the model: base64 media and whole trees would flood a small
# model's context, the rest are covered by other tools. ``open`` uses
# directory_tree itself.
HIDDEN = (
    "read_file",  # deprecated alias of read_text_file
    "read_media_file",
    "read_multiple_files",
    "directory_tree",
    "list_allowed_directories",
)

# Folders never worth searching through for a spoken name.
_SKIP_NAMES = (
    "node_modules",
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "site-packages",
    ".cache",
    "$RECYCLE.BIN",
)
_SKIP_GLOBS = [f"**/{name}/**" for name in _SKIP_NAMES]
# Searched first when a name is spoken without a folder.
_NEARBY = ("Desktop", "Documents", "Downloads")
_STANDARD = ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos")


def hidden_names() -> set:
    return {f"{NAMESPACE}_{name}" for name in HIDDEN}


# ---------------------------------------------------------------------------
# Installing and starting the server
# ---------------------------------------------------------------------------


def install_dir() -> Path:
    from openjarvis.core.paths import get_config_dir

    return get_config_dir() / "mcp" / "server-filesystem"


def entry_script(base: Optional[Path] = None) -> Optional[Path]:
    """The installed server's ``dist/index.js``, or None."""
    script = (
        (base or install_dir())
        / "node_modules"
        / "@modelcontextprotocol"
        / "server-filesystem"
        / "dist"
        / "index.js"
    )
    return script if script.is_file() else None


def installed_version(base: Optional[Path] = None) -> str:
    package = (
        (base or install_dir())
        / "node_modules"
        / "@modelcontextprotocol"
        / "server-filesystem"
        / "package.json"
    )
    try:
        return str(json.loads(package.read_text(encoding="utf-8")).get("version", ""))
    except (OSError, ValueError):
        return ""


def _bundled_node() -> Optional[Path]:
    """The Node.js the OpenJarvis installer puts next to Jarvis
    (``...\\OpenJarvis\\node``, beside the ``venv`` it runs from)."""
    import sys

    folder = Path(sys.prefix).parent / "node"
    return folder if (folder / "node.exe").is_file() else None


def _which(*names: str) -> Optional[str]:
    """A Node.js program: the installer's own copy first (pinned to a
    version the server supports), then PATH."""
    bundled = _bundled_node()
    for name in names:
        if bundled is not None and (bundled / name).is_file():
            return str(bundled / name)
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def _npm() -> Optional[str]:
    return _which("npm.cmd", "npm")


def install(version: str = VERSION, base: Optional[Path] = None) -> Path:
    """``npm install`` the pinned server into Jarvis's folder; returns its
    entry script. Raises RuntimeError when npm is missing or fails."""
    npm = _npm()
    if npm is None:
        raise RuntimeError(
            "Node.js (npm) isn't installed. Install Node.js 24+ from nodejs.org."
        )
    folder = base or install_dir()
    folder.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(
        [
            npm,
            "install",
            "--prefix",
            str(folder),
            "--no-audit",
            "--no-fund",
            "--save-exact",
            f"{PACKAGE}@{version}",
        ],
        capture_output=True,
        text=True,
        timeout=600,
        creationflags=0x08000000 if os.name == "nt" else 0,  # no window
    )
    script = entry_script(folder)
    if done.returncode != 0 or script is None:
        tail = (done.stderr or done.stdout or "").strip().splitlines()[-3:]
        raise RuntimeError("npm install failed: " + " ".join(tail))
    return script


def user_folders(extra: str | Sequence[str] = "") -> List[str]:
    """The folders the server may touch: your standard folders (and their
    OneDrive copies) plus ``extra`` ("D:\\Games;E:\\Work")."""
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    wanted: List[Path] = []
    for name in _STANDARD:
        for base in (home, home / "OneDrive"):
            wanted.append(base / name)
    if isinstance(extra, str):
        extra = [p for p in extra.split(";")]
    wanted += [
        Path(os.path.expandvars(os.path.expanduser(p.strip())))
        for p in extra
        if p.strip()
    ]
    seen, out = set(), []
    for path in wanted:
        key = str(path).lower().rstrip("\\/")
        if key not in seen and path.is_dir():
            seen.add(key)
            out.append(str(path))
    return out


def server_config(
    folders: Sequence[str], *, auto_install: bool = True
) -> Optional[Dict[str, Any]]:
    """How to start the server (stdio), or None when Node.js isn't there."""
    if not folders:
        return None
    node = _which("node.exe", "node")
    script = entry_script()
    if script is None and node and auto_install:
        try:
            script = install()
            logger.info("Installed %s %s.", PACKAGE, VERSION)
        except Exception as exc:
            logger.warning("Could not install the filesystem MCP server: %s", exc)
    # --confine-roots: an MCP client's "roots" may only narrow these folders,
    # never replace them (e.g. when `jarvis desktop mcp` serves another app).
    # "--" ends the options, so no folder is read as one.
    tail = ["--confine-roots", "--", *folders]
    if node and script is not None:
        return {"command": node, "args": [str(script), *tail]}
    npx = _which("npx.cmd", "npx")
    if npx:  # slower: npx checks the registry each start
        return {"command": npx, "args": ["-y", f"{PACKAGE}@{VERSION}", *tail]}
    return None


# ---------------------------------------------------------------------------
# Looking files up through the server (for ``open``)
# ---------------------------------------------------------------------------


def _text(result: Any) -> str:
    return "\n".join(
        getattr(block, "text", "") for block in (getattr(result, "content", None) or [])
    ).strip()


def _glob_word(word: str) -> str:
    """'notes' -> '**/*[nN][oO][tT][eE][sS]*' (the server's globs are
    case-sensitive)."""
    parts = []
    for ch in word:
        if ch.isalpha():
            parts.append(f"[{ch.lower()}{ch.upper()}]")
        elif ch.isalnum():
            parts.append(ch)
    return "**/*" + "".join(parts) + "*"


def listing(folder: str, text: str) -> List[Tuple[str, bool]]:
    """(path, is_dir) from list_directory output ("[FILE] a.pdf", "[DIR] b")."""
    out = []
    base = folder.rstrip("\\/")
    for line in (text or "").splitlines():
        line = line.strip()
        for tag, is_dir in (("[DIR]", True), ("[FILE]", False)):
            if line.startswith(tag):
                name = line[len(tag) :].strip()
                # list_directory_with_sizes adds the size after the name.
                if not is_dir and "  " in name:
                    name = name.split("  ")[0].rstrip()
                if name:
                    out.append((base + "\\" + name, is_dir))
    return out


def _flatten(root: str, nodes: Any) -> List[Tuple[str, bool]]:
    out: List[Tuple[str, bool]] = []
    stack = [(root.rstrip("\\/"), n) for n in (nodes or [])]
    while stack:
        parent, node = stack.pop(0)
        if not isinstance(node, dict) or not node.get("name"):
            continue
        path = parent + "\\" + str(node["name"])
        is_dir = node.get("type") == "directory"
        out.append((path, is_dir))
        stack += [(path, child) for child in node.get("children") or []]
    return out


class FileStore:
    """Calls the mounted filesystem server for ``open`` and the fast path.

    Works from inside a tool call (FastMCP runs sync tools in AnyIO worker
    threads) and from the agent's own thread (through the event loop the
    agent's ToolRuntime runs, set as ``loop``).
    """

    def __init__(
        self,
        server: Any,
        folders: Sequence[str],
        *,
        timeout: float = 20.0,
        cache_seconds: float = 60.0,
    ) -> None:
        self.server = server
        self.folders = list(folders)
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._trees: Dict[str, Tuple[float, List[Tuple[str, bool]]]] = {}

    # -- calling the server ----------------------------------------------------

    def _run(self, make: Callable[[], Awaitable[Any]]) -> Any:
        import anyio
        import anyio.from_thread

        async def bounded() -> Any:
            with anyio.fail_after(self.timeout):
                return await make()

        try:
            return anyio.from_thread.run(bounded)  # inside a tool call
        except RuntimeError as exc:
            if "worker thread" not in str(exc) and "event loop" not in str(exc):
                raise
        if self.loop is None:
            from openjarvis.desktop.winapi import DesktopError

            raise DesktopError("Jarvis's file tools are still starting.")
        future = asyncio.run_coroutine_threadsafe(bounded(), self.loop)
        return future.result(timeout=self.timeout + 1)

    def call(self, tool: str, args: Dict[str, Any]) -> str:
        """One server tool's text result; raises DesktopError on its errors."""
        from openjarvis.desktop.winapi import DesktopError

        name = f"{NAMESPACE}_{tool}"
        try:
            result = self._run(lambda: self.server.call_tool(name, args))
        except DesktopError:
            raise
        except Exception as exc:
            raise DesktopError(str(exc).split("':", 1)[-1].strip() or str(exc)) from exc
        if getattr(result, "is_error", False):
            raise DesktopError(_text(result))
        return _text(result)

    # -- what open uses --------------------------------------------------------

    def list(self, folder: str | Path) -> List[str]:
        """Full paths of what's directly in ``folder``."""
        text = self.call("list_directory", {"path": str(folder)})
        return [path for path, _ in listing(str(folder), text)]

    def tree(self, root: str, depth: int = 2) -> List[Tuple[str, bool]]:
        """(path, is_dir) up to ``depth`` levels under ``root`` (cached)."""
        key = f"{root.lower()}|{depth}"
        cached = self._trees.get(key)
        if cached and time.monotonic() - cached[0] < self.cache_seconds:
            return cached[1]
        text = self.call(
            "directory_tree",
            {"path": root, "depth": depth, "excludePatterns": list(_SKIP_NAMES)},
        )
        try:
            entries = _flatten(root, json.loads(text))
        except ValueError:
            entries = []
        self._trees[key] = (time.monotonic(), entries)
        return entries

    def nearby_roots(self) -> List[str]:
        roots = [f for f in self.folders if Path(f).name in _NEARBY]
        return roots or self.folders[:3]

    def nearby(self, kind: str = "any", depth: int = 2) -> List[Tuple[str, bool]]:
        """Everything a couple of levels into Desktop, Documents, Downloads."""
        out: List[Tuple[str, bool]] = []
        for root in self.nearby_roots():
            try:
                entries = self.tree(root, depth)
            except Exception as exc:
                logger.debug("directory_tree %s failed: %s", root, exc)
                continue
            out += [
                (p, d) for p, d in entries if kind == "any" or (kind == "folder") == d
            ]
        return out

    def find(self, word: str) -> List[str]:
        """Paths whose name contains ``word``, in all your folders at once."""

        async def everywhere() -> List[Any]:
            calls = [
                self.server.call_tool(
                    f"{NAMESPACE}_search_files",
                    {
                        "path": root,
                        "pattern": _glob_word(word),
                        "excludePatterns": _SKIP_GLOBS,
                    },
                )
                for root in self.folders
            ]
            return await asyncio.gather(*calls, return_exceptions=True)

        try:
            results = self._run(everywhere)
        except Exception as exc:
            logger.debug("search_files failed: %s", exc)
            return []
        paths: List[str] = []
        for result in results:
            if isinstance(result, BaseException) or getattr(result, "is_error", False):
                continue
            paths += [
                line.strip()
                for line in _text(result).splitlines()
                if line.strip()[1:3] == ":\\"
            ]
        return paths

    def search(self, query: str, kind: str = "any", limit: int = 8) -> List[Any]:
        """Folders/files matching a spoken name, best first (places.Found)."""
        from openjarvis.desktop import places

        query = " ".join((query or "").split())
        kind = kind if kind in ("folder", "file") else "any"
        words = places._words(query)
        if not words:
            return []
        found = {
            p.lower(): places.Found(p, d, places._score(query, p, d))
            for p, d in self.nearby(kind)
        }
        best = max((f.score for f in found.values()), default=0.0)
        if best < 0.9:
            # Deeper than two levels: ask the server to search by the most
            # telling word ("minecraft" of "minecraft server").
            letters = [w for w in words if w.isalpha() and len(w) >= 3] or words
            for path in self.find(max(letters, key=len)):
                if path.lower() in found:
                    continue
                is_dir = os.path.isdir(path)
                if kind != "any" and (kind == "folder") != is_dir:
                    continue
                found[path.lower()] = places.Found(
                    path, is_dir, places._score(query, path, is_dir)
                )
        ranked = sorted(found.values(), key=lambda f: f.score, reverse=True)
        return [f for f in ranked if f.score >= 0.6][:limit]

    def warm(self) -> None:
        """Fill the cache (the first look at a folder is the slow one)."""
        self.nearby()


__all__ = [
    "FileStore",
    "HIDDEN",
    "NAMESPACE",
    "PACKAGE",
    "VERSION",
    "entry_script",
    "hidden_names",
    "install",
    "install_dir",
    "listing",
    "server_config",
    "user_folders",
]
