"""Groq cloud inference (OpenAI-compatible API).

Pick it with ``[engine] default = "groq"`` (or in the JARVIS window's
settings). The API key comes from ``GROQ_API_KEY`` or the Windows
Credential Manager (``openjarvis.core.secrets``); the model from
``[engine.groq] model``. Requests, and screenshots for screen vision, go to
Groq's servers: https://console.groq.com
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

from openjarvis.core.registry import EngineRegistry
from openjarvis.core.types import Message
from openjarvis.engine._openai_compat import _OpenAICompatibleEngine

GROQ_HOST = "https://api.groq.com/openai"
# Tool use, parallel calls and image input in one model (Groq preview).
DEFAULT_MODEL = "qwen/qwen3.8-27b"
# Options only Ollama understands; Groq rejects unknown request fields.
_LOCAL_ONLY = ("num_ctx", "num_gpu", "keep_alive", "think", "options")


@EngineRegistry.register("groq")
class GroqEngine(_OpenAICompatibleEngine):
    """Groq's hosted models through its OpenAI-compatible endpoint."""

    engine_id = "groq"
    _default_host = GROQ_HOST
    _api_prefix = "/v1"
    _stream_400_signals_context_length = False

    def __init__(
        self,
        host: str | None = None,
        *,
        api_key: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        if not api_key:
            from openjarvis.core.secrets import get_key

            api_key = get_key("groq")
        super().__init__(host, api_key=api_key, timeout=timeout)

    @property
    def has_key(self) -> bool:
        return bool(self._api_key)

    def generate(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        for key in _LOCAL_ONLY:
            kwargs.pop(key, None)
        return super().generate(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )

    def chat_models(self) -> List[str]:
        """Groq's current chat models (speech and guard models left out)."""
        skip = ("whisper", "tts", "guard", "safeguard", "orpheus", "prompt-guard")
        return sorted(
            m for m in self.list_models() if not any(word in m for word in skip)
        )


__all__ = ["DEFAULT_MODEL", "GROQ_HOST", "GroqEngine"]
