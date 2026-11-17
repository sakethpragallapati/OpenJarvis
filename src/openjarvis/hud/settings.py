"""The JARVIS window's settings: which brain (local Ollama or Groq cloud),
the Groq API key, and where speech-to-text runs.

Choices are written to ``~/.openjarvis/config.toml`` (comments kept); the
API key goes to the Windows Credential Manager (``core.secrets``), never to
a file.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

GROQ_KEYS_URL = "https://console.groq.com/keys"
LOCAL_DEFAULT_MODEL = "qwen3.5:4b"


def _config_path() -> Path:
    from openjarvis.core.paths import get_config_path

    return Path(get_config_path())


def _load_doc() -> Any:
    import tomlkit

    path = _config_path()
    if path.exists():
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    return tomlkit.document()


def _table(doc: Any, *names: str) -> Any:
    import tomlkit

    node = doc
    for name in names:
        if name not in node:
            node[name] = tomlkit.table()
        node = node[name]
    return node


def ollama_installed() -> bool:
    tray = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama"
    return bool(shutil.which("ollama")) or (sys.platform == "win32" and tray.exists())


def ollama_models() -> List[str]:
    """Models already pulled into Ollama (empty if it isn't running)."""
    try:
        from openjarvis.core.config import load_config
        from openjarvis.engine._discovery import _make_engine

        engine = _make_engine("ollama", load_config())
        return sorted(engine.list_models()) if engine.health() else []
    except Exception:
        return []


def current() -> Dict[str, Any]:
    """What the settings panel shows."""
    from openjarvis.core.config import load_config
    from openjarvis.core.secrets import has_key

    config = load_config()
    engine = (config.engine.default or "ollama").lower()
    return {
        "engine": "groq" if engine == "groq" else "local",
        "local_model": config.intelligence.default_model or LOCAL_DEFAULT_MODEL,
        "groq_model": config.engine.groq.model,
        "groq_key_saved": has_key("groq"),
        "stt": "groq" if (config.speech.backend or "") == "groq" else "local",
        "ollama_installed": ollama_installed(),
        "ollama_models": ollama_models(),
        "keys_url": GROQ_KEYS_URL,
    }


def check_groq_key(key: str) -> Tuple[bool, str, List[str]]:
    """(works, message, chat models) for a Groq API key."""
    from openjarvis.engine.groq import GroqEngine

    key = (key or "").strip()
    if not key.startswith("gsk_") or len(key) < 20:
        return False, "That doesn't look like a Groq key (they start with gsk_).", []
    engine = GroqEngine(api_key=key, timeout=15.0)
    try:
        response = engine._client.get("/v1/models")
    except Exception as exc:
        return (
            False,
            f"Couldn't reach Groq ({type(exc).__name__}). Check your internet.",
            [],
        )
    if response.status_code == 401:
        return False, "Groq didn't accept that key.", []
    if response.status_code != 200:
        return False, f"Groq answered {response.status_code}; try again later.", []
    models = engine.chat_models()
    return True, "Key works.", models


def save_groq_key(key: str) -> Tuple[bool, str, List[str]]:
    from openjarvis.core.secrets import set_key

    ok, message, models = check_groq_key(key)
    if not ok:
        return ok, message, models
    try:
        set_key("groq", key)
    except OSError as exc:
        return False, f"Couldn't save the key: {exc}", []
    return True, "Saved in the Windows Credential Manager.", models


def remove_groq_key() -> None:
    from openjarvis.core.secrets import set_key

    set_key("groq", "")


def groq_models() -> List[str]:
    from openjarvis.engine.groq import GroqEngine

    engine = GroqEngine(timeout=15.0)
    if not engine.has_key:
        return []
    try:
        return engine.chat_models()
    except Exception:
        return []


def save(choice: Dict[str, Any]) -> Tuple[bool, str]:
    """Write the choices to config.toml. (ok, message)."""
    from openjarvis.core.secrets import has_key

    engine = "groq" if choice.get("engine") == "groq" else "local"
    stt = "groq" if choice.get("stt") == "groq" else "local"
    if (engine == "groq" or stt == "groq") and not has_key("groq"):
        return False, "Add your Groq API key first."
    doc = _load_doc()
    _table(doc, "engine")["default"] = "groq" if engine == "groq" else "ollama"
    groq_model = str(choice.get("groq_model") or "").strip()
    if groq_model:
        _table(doc, "engine", "groq")["model"] = groq_model
    local_model = str(choice.get("local_model") or "").strip()
    if local_model:
        _table(doc, "intelligence")["default_model"] = local_model
    speech = _table(doc, "speech")
    speech["backend"] = "groq" if stt == "groq" else "faster-whisper"
    if engine == "groq" and "desktop" in doc and "vision_model" in doc["desktop"]:
        # A local vision model name means nothing to Groq.
        del doc["desktop"]["vision_model"]
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    import tomlkit

    path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    return True, "Saved. Restarting Jarvis…"


def engine_problem() -> Optional[str]:
    """Why Jarvis can't start with the chosen brain (None = it can)."""
    from openjarvis.core.config import load_config
    from openjarvis.core.secrets import has_key

    config = load_config()
    engine = (config.engine.default or "ollama").lower()
    if engine == "groq":
        if not has_key("groq"):
            return "Jarvis is set to use Groq, but there's no Groq API key yet."
        return None
    if (config.speech.backend or "") == "groq" and not has_key("groq"):
        return "Speech-to-text is set to Groq, but there's no Groq API key yet."
    if engine == "ollama" and not ollama_installed():
        return (
            "Jarvis is set to use a local model, but Ollama isn't installed. "
            "Install it from ollama.com, or use Groq (cloud) instead."
        )
    return None


__all__ = [
    "GROQ_KEYS_URL",
    "check_groq_key",
    "current",
    "engine_problem",
    "groq_models",
    "remove_groq_key",
    "save",
    "save_groq_key",
]
