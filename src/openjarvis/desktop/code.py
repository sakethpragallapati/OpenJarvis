"""Write and run programs the user asks for, without typing into an editor.

"Write me a Hello World program in Python" used to mean keyboard-typing into
whatever had focus, then guessing at Ctrl+S / F5 and hoping. Instead the
code goes into a real file in the "Jarvis Code" folder on the Desktop, VS
Code opens it, and "run it" executes it with the right interpreter and
returns the actual output.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from openjarvis.desktop.winapi import DesktopError

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
EXTENSIONS: Dict[str, str] = {
    "python": ".py",
    "py": ".py",
    "javascript": ".js",
    "js": ".js",
    "node": ".js",
    "typescript": ".ts",
    "ts": ".ts",
    "powershell": ".ps1",
    "ps1": ".ps1",
    "batch": ".bat",
    "bat": ".bat",
    "cmd": ".cmd",
    "bash": ".sh",
    "shell": ".sh",
    "sh": ".sh",
    "html": ".html",
    "java": ".java",
    "c": ".c",
    "cpp": ".cpp",
    "c++": ".cpp",
    "go": ".go",
    "rust": ".rs",
    "text": ".txt",
}
_FOLDER = "Jarvis Code"


def code_dir(override: str = "") -> Path:
    if override:
        return Path(override).expanduser()
    desktop = Path.home() / "Desktop"
    if not desktop.exists():
        from openjarvis.core.paths import get_config_dir

        return get_config_dir() / "code"
    return desktop / _FOLDER


def _safe_name(filename: str, language: str = "") -> str:
    name = re.sub(r"[^\w.\- ]", "", (filename or "").strip()).strip(" .")
    name = name.replace(" ", "_") or "program"
    if "." not in name:
        name += EXTENSIONS.get(language.lower().strip(), ".py" if language else ".txt")
    return name


def resolve(filename: str, folder: Path) -> Path:
    """A file the user named: a full path, or a name inside the code folder."""
    candidate = Path(filename).expanduser()
    if candidate.is_absolute():
        return candidate
    direct = folder / filename
    if direct.exists():
        return direct
    stem = Path(filename).stem.lower().replace(" ", "_")
    matches = sorted(
        (p for p in folder.glob("*") if p.is_file() and p.stem.lower() == stem),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else direct


def write(filename: str, content: str, language: str = "", *, folder: Path) -> Path:
    if not (content or "").strip():
        raise DesktopError("There's no code to write.")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / _safe_name(filename, language)
    text = content.replace("\r\n", "\n")
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8")
    return path


def vscode() -> Optional[str]:
    found = shutil.which("code") or shutil.which("code.cmd")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    for base in (
        Path(local) / "Programs" / "Microsoft VS Code" / "bin" / "code.cmd",
        Path(os.environ.get("ProgramFiles", ""))
        / "Microsoft VS Code"
        / "bin"
        / "code.cmd",
    ):
        if base.is_file():
            return str(base)
    return None


def open_in_editor(path: Path) -> str:
    """Open the file in VS Code (same window) or the default editor."""
    editor = vscode()
    if editor:
        try:
            subprocess.Popen(
                [editor, "--reuse-window", str(path)],
                creationflags=_CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=editor.lower().endswith(".cmd"),
            )
            return "VS Code"
        except OSError:
            pass
    from openjarvis.desktop.winapi import shell_open

    shell_open(str(path))
    return "the default editor"


def _python() -> str:
    return shutil.which("python") or shutil.which("py") or sys.executable


def command_for(path: Path) -> Optional[List[str]]:
    """How to run a file, by extension (None = not runnable here)."""
    suffix = path.suffix.lower()
    if suffix == ".py":
        return [_python(), str(path)]
    if suffix == ".js":
        node = shutil.which("node")
        return [node, str(path)] if node else None
    if suffix == ".ts":
        npx = shutil.which("npx") or shutil.which("npx.cmd")
        return [npx, "-y", "tsx", str(path)] if npx else None
    if suffix == ".ps1":
        return [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(path),
        ]
    if suffix in (".bat", ".cmd"):
        return ["cmd", "/c", str(path)]
    if suffix == ".sh":
        bash = shutil.which("bash")
        return [bash, str(path)] if bash else None
    if suffix == ".go":
        go = shutil.which("go")
        return [go, "run", str(path)] if go else None
    return None


def run(path: Path, timeout: float = 30.0) -> str:
    """Run the program and return what it printed (or its error)."""
    if not path.is_file():
        raise DesktopError(f"There's no program at {path}.")
    if path.suffix.lower() in (".html", ".htm"):
        from openjarvis.desktop.winapi import shell_open

        shell_open(str(path))
        return f"Opened {path.name} in the browser."
    command = command_for(path)
    if command is None:
        raise DesktopError(
            f"I don't know how to run {path.suffix or 'that'} files on this PC."
        )
    started = time.perf_counter()
    try:
        result = subprocess.run(
            command,
            cwd=str(path.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
            shell=str(command[0]).lower().endswith(".cmd"),
        )
    except subprocess.TimeoutExpired:
        seconds = int(timeout)
        return f"{path.name} was still running after {seconds} s, so I stopped it."
    except OSError as exc:
        raise DesktopError(f"Couldn't start {path.name}: {exc}") from exc
    seconds = time.perf_counter() - started
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if len(out) > 1500:
        out = out[:1500] + " [...]"
    if len(err) > 800:
        err = err[-800:]
    if result.returncode == 0:
        body = f"Output:\n{out}" if out else "It ran with no output."
        if err:
            body += f"\nWarnings:\n{err}"
        return f"{body}\n(ran in {seconds:.1f} s)"
    detail = err or out or "no error message"
    return f"It failed with exit code {result.returncode}:\n{detail}"


def listing(folder: Path, limit: int = 12) -> str:
    try:
        files = sorted(
            (p for p in folder.iterdir() if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        files = []
    if not files:
        return f"No programs yet in {folder}."
    return f"Programs in {folder}:\n" + "\n".join(p.name for p in files[:limit])


__all__ = [
    "EXTENSIONS",
    "code_dir",
    "command_for",
    "listing",
    "open_in_editor",
    "resolve",
    "run",
    "vscode",
    "write",
]
