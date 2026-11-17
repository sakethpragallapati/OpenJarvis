"""Speech-to-text through Groq's hosted Whisper (cloud).

``[speech] backend = "groq"``. Uses the same API key as the Groq engine
(``GROQ_API_KEY`` or the Windows Credential Manager). Helpful on PCs
without an NVIDIA GPU, where local Whisper is slower. Your recordings are
sent to Groq.
"""

from __future__ import annotations

import time
from typing import List, Optional

import httpx

from openjarvis.core.registry import SpeechRegistry
from openjarvis.speech._stubs import SpeechBackend, TranscriptionResult

_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
DEFAULT_MODEL = "whisper-large-v3-turbo"
# Groq caps the prompt that steers spelling (names, app names) at 224 tokens.
_PROMPT_CHARS = 500


@SpeechRegistry.register("groq")
class GroqWhisperBackend(SpeechBackend):
    """Groq Whisper (whisper-large-v3-turbo by default)."""

    backend_id = "groq"
    cloud = True  # the voice loop skips its warm-up decode (it would cost a call)

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        model: str = DEFAULT_MODEL,
        timeout: float = 30.0,
    ) -> None:
        if not api_key:
            from openjarvis.core.secrets import get_key

            api_key = get_key("groq")
        self._api_key = api_key or ""
        self._model = model
        self._client = httpx.Client(timeout=timeout)

    def transcribe(
        self,
        audio: bytes,
        *,
        format: str = "wav",
        language: Optional[str] = None,
        hotwords: Optional[str] = None,
        **_ignored: object,
    ) -> TranscriptionResult:
        if not self._api_key:
            raise RuntimeError("No Groq API key (add it in Jarvis's settings).")
        ext = format.lstrip(".") or "wav"
        data = {"model": self._model, "response_format": "json", "temperature": "0"}
        if language:
            data["language"] = language
        if hotwords:
            data["prompt"] = hotwords[:_PROMPT_CHARS]
        started = time.perf_counter()
        response = self._client.post(
            _URL,
            headers={"Authorization": f"Bearer {self._api_key}"},
            data=data,
            files={"file": (f"audio.{ext}", audio, f"audio/{ext}")},
        )
        if response.status_code == 429:
            raise RuntimeError(
                "Groq's speech-to-text limit was reached; try again soon."
            )
        response.raise_for_status()
        text = str(response.json().get("text", "")).strip()
        return TranscriptionResult(
            text=text,
            language=language,
            confidence=None,
            duration_seconds=time.perf_counter() - started,
            segments=[],
        )

    def health(self) -> bool:
        return bool(self._api_key)

    def supported_formats(self) -> List[str]:
        return ["flac", "mp3", "mp4", "mpeg", "mpga", "m4a", "ogg", "wav", "webm"]


__all__ = ["DEFAULT_MODEL", "GroqWhisperBackend"]
