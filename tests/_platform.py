"""Platform capability markers shared across test modules."""

from __future__ import annotations

import os
import shutil
import sys

import pytest


def _posix_bash_available() -> bool:
    bash = shutil.which("bash")
    if bash is None:
        return False
    if sys.platform != "win32":
        return True
    # subprocess.run(["bash", ...]) uses CreateProcess, which searches
    # System32 *before* PATH, so WSL's launcher wins even when Git Bash is
    # first on PATH. WSL bash cannot read the C:\ paths the tests hand it.
    system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    if os.path.exists(os.path.join(system32, "bash.exe")):
        return False
    lowered = bash.lower()
    return "\\system32\\" not in lowered and "\\windowsapps\\" not in lowered


needs_posix_shell = pytest.mark.skipif(
    not _posix_bash_available(),
    reason="needs a native POSIX bash (CI runs these on Linux/macOS)",
)

needs_posix_exec = pytest.mark.skipif(
    sys.platform == "win32",
    reason="executes a '#!' script directly, which Windows cannot do",
)

# Extra seconds for a fresh interpreter to import OpenJarvis in a subprocess.
# Process start + imports are several times slower on Windows (AV scanning,
# no fork), which says nothing about the hang/regex behaviour under test.
SUBPROCESS_STARTUP_ALLOWANCE = 20 if sys.platform == "win32" else 0
