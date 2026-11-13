"""Test fixtures for installer / cold-start refresh tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture
def tmp_openjarvis_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``DEFAULT_CONFIG_DIR`` at a tmpdir for isolated tests.

    Returns the directory; teardown is automatic via tmp_path.
    """
    home = tmp_path / ".openjarvis"
    home.mkdir()
    (home / ".state").mkdir()
    (home / ".state" / "models").mkdir()
    (home / ".scripts").mkdir()
    config_path = home / "config.toml"
    monkeypatch.setattr("openjarvis.core.config.DEFAULT_CONFIG_DIR", home)
    monkeypatch.setattr("openjarvis.core.config.DEFAULT_CONFIG_PATH", config_path)
    # Also patch init_cmd's module-level bindings (imported with ``from ... import``).
    monkeypatch.setattr("openjarvis.cli.init_cmd.DEFAULT_CONFIG_DIR", home)
    monkeypatch.setattr("openjarvis.cli.init_cmd.DEFAULT_CONFIG_PATH", config_path)
    # Patch doctor_cmd's module-level bindings.
    monkeypatch.setattr("openjarvis.cli.doctor_cmd.DEFAULT_CONFIG_PATH", config_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    # These tests drive state purely via marker files; pin the runtime Rust
    # import check off so a built extension in the dev venv can't flip
    # "pending"/"failed" to "ready" behind their backs.
    monkeypatch.setattr("openjarvis._rust_bridge.RUST_AVAILABLE", False)
    return home


# Model marker files are named "<model>.<state>" with Ollama tags such as
# "qwen3.5:9b". Only the POSIX pull-model.sh writes them; the native Windows
# installer pulls models directly, and ':' is not a valid Windows filename
# character (it would create an NTFS alternate data stream instead).
needs_colon_filenames = pytest.mark.skipif(
    sys.platform == "win32",
    reason="model marker filenames contain ':', which Windows does not allow",
)
