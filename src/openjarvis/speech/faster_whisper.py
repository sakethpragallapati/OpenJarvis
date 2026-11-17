"""Faster-Whisper speech-to-text backend (local, CTranslate2-based)."""

from __future__ import annotations

import logging
import os
import tempfile
from typing import List, Optional

from openjarvis.core.registry import SpeechRegistry
from openjarvis.speech._stubs import Segment, SpeechBackend, TranscriptionResult

logger = logging.getLogger(__name__)

# CTranslate2's CUDA path needs cuBLAS 12 and cuDNN 9, which pip doesn't put
# on PATH. A CUDA 12 PyTorch wheel ships exactly those DLLs in torch/lib, as
# do the nvidia-cublas-cu12 / nvidia-cudnn-cu12 wheels.
_CUDA_DLL = "cublas64_12.dll"
CUDA_DLL_DIRS: List[str] = []


def _cuda_dll_dirs() -> List[str]:
    """Folders in this environment that hold cuBLAS 12 / cuDNN 9 DLLs."""
    import importlib.util
    from pathlib import Path

    found: List[str] = []
    candidates = []
    try:
        spec = importlib.util.find_spec("torch")  # locates it without importing
    except (ImportError, ValueError):
        spec = None
    if spec is not None and spec.origin:
        candidates.append(Path(spec.origin).parent / "lib")
    try:
        nvidia = importlib.util.find_spec("nvidia")
    except (ImportError, ValueError):
        nvidia = None
    for root in getattr(nvidia, "submodule_search_locations", None) or []:
        for sub in ("cublas", "cudnn", "cuda_runtime"):
            candidates.append(Path(root) / sub / "bin")
    for folder in candidates:
        if (folder / _CUDA_DLL).is_file() or any(folder.glob("cudnn64_*.dll")):
            found.append(str(folder))
    return found


def _register_cuda_dlls() -> None:
    if os.name != "nt" or CUDA_DLL_DIRS:
        return
    for folder in _cuda_dll_dirs():
        try:
            os.add_dll_directory(folder)
        except OSError:
            continue
        # CTranslate2 loads cuBLAS/cuDNN lazily with a plain LoadLibrary,
        # which searches PATH rather than the add_dll_directory list.
        os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
        CUDA_DLL_DIRS.append(folder)
    if CUDA_DLL_DIRS:
        logger.debug("CUDA DLLs for faster-whisper: %s", CUDA_DLL_DIRS)


_register_cuda_dlls()

# On Windows, CTranslate2 loads its bundled DLLs at import time, so a broken
# install (or a path over MAX_PATH) raises OSError rather than ImportError.
# Treat both as "backend unavailable" instead of breaking every speech import.
try:
    from faster_whisper import WhisperModel
except (ImportError, OSError) as _exc:
    logger.debug("faster-whisper unavailable: %s", _exc)
    WhisperModel = None  # type: ignore[assignment, misc]

try:
    import ctranslate2
except (ImportError, OSError):
    ctranslate2 = None  # type: ignore[assignment]

# Substrings of CUDA runtime failures (missing cuDNN/cuBLAS DLLs, driver too
# old, no device) that mean "this GPU path cannot work here" -- as opposed to
# a bad audio file, which the CPU would reject just the same.
_GPU_ERROR_MARKERS = ("cuda", "cudnn", "cublas", "gpu", ".dll", "library")


def _is_gpu_runtime_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _GPU_ERROR_MARKERS)


@SpeechRegistry.register("faster-whisper")
class FasterWhisperBackend(SpeechBackend):
    """Local speech-to-text using Faster-Whisper (CTranslate2)."""

    backend_id = "faster-whisper"

    def __init__(
        self,
        model_size: str = "base",
        device: str = "auto",
        compute_type: str = "float16",
    ) -> None:
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._model: Optional[WhisperModel] = None
        self._last_error: Optional[str] = None

    def _resolve_compute_type(self) -> str:
        """Pick a CTranslate2 compute type supported by the configured device."""
        if ctranslate2 is None:
            return self._compute_type

        try:
            supported = set(ctranslate2.get_supported_compute_types(self._device))
        except Exception as exc:
            logger.debug(
                "Could not inspect CTranslate2 compute types for %s: %s",
                self._device,
                exc,
            )
            return self._compute_type

        if self._compute_type in supported:
            return self._compute_type

        preferences = (
            ("int8", "float32", "int8_float32", "int16")
            if self._compute_type == "float16"
            else ("float32", "int8", "int8_float32", "int16")
        )
        fallback = next((value for value in preferences if value in supported), None)
        if fallback is None:
            return self._compute_type

        logger.warning(
            "CTranslate2 compute_type=%r is not supported on device=%r; "
            "using %r instead",
            self._compute_type,
            self._device,
            fallback,
        )
        return fallback

    def _ensure_model(self) -> WhisperModel:
        """Lazy-load the Whisper model on first use."""
        if self._model is None:
            if WhisperModel is None:
                self._last_error = (
                    "faster-whisper is not installed. "
                    "Install with: uv sync --extra desktop"
                )
                raise ImportError(self._last_error)
            try:
                self._model = self._load_model()
            except Exception as exc:
                if not self._switch_to_cpu(exc):
                    raise
                self._model = self._load_model()
        self._last_error = None
        return self._model

    def _load_model(self) -> WhisperModel:
        return WhisperModel(
            self._model_size,
            device=self._device,
            compute_type=self._resolve_compute_type(),
        )

    def _switch_to_cpu(self, exc: BaseException) -> bool:
        """Fall back to CPU int8 after a CUDA failure; False if already on CPU.

        On Windows the GPU path needs cuDNN/cuBLAS DLLs that pip does not
        install, and the failure often surfaces only on the first transcribe.
        Whisper base/small run in real time on a laptop CPU, so degrading is
        better than leaving voice mode deaf.
        """
        if self._device == "cpu" or not _is_gpu_runtime_error(exc):
            return False
        logger.warning(
            "Faster-Whisper could not use the GPU (%s); falling back to CPU int8.",
            exc,
        )
        self._device = "cpu"
        self._compute_type = "int8"
        self._model = None
        return True

    def transcribe(
        self,
        audio: bytes,
        *,
        format: str = "wav",
        language: Optional[str] = None,
        hotwords: Optional[str] = None,
        short_utterance: bool = False,
        vad_filter: bool = True,
    ) -> TranscriptionResult:
        """Transcribe audio bytes using Faster-Whisper.

        ``hotwords`` biases spelling toward known names ("VS Code", not
        "V's code"). ``short_utterance`` tunes decoding for a spoken command:
        no conditioning on earlier text, no timestamps, and (``vad_filter``)
        Silero VAD to drop silence/noise that Whisper would hallucinate on.
        """
        try:
            model = self._ensure_model()

            # Write audio to a temp file (faster-whisper needs a file path).
            # delete=False + manual unlink: on Windows an open
            # NamedTemporaryFile holds an exclusive handle, so PyAV's reopen
            # of tmp.name inside model.transcribe() fails with EACCES.
            suffix = f".{format}" if not format.startswith(".") else format
            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            try:
                with tmp:
                    tmp.write(audio)

                kwargs: dict = {}
                if language:
                    kwargs["language"] = language
                if hotwords:
                    kwargs["hotwords"] = hotwords
                if short_utterance:
                    kwargs["condition_on_previous_text"] = False
                    kwargs["without_timestamps"] = True
                    if vad_filter:
                        kwargs["vad_filter"] = True

                try:
                    segments_iter, info = model.transcribe(tmp.name, **kwargs)
                    segments_list = list(segments_iter)
                except Exception as exc:
                    if not self._switch_to_cpu(exc):
                        raise
                    model = self._ensure_model()
                    segments_iter, info = model.transcribe(tmp.name, **kwargs)
                    segments_list = list(segments_iter)
            finally:
                try:
                    os.unlink(tmp.name)
                except OSError as unlink_exc:
                    logger.debug(
                        "Could not remove temp audio file %s: %s",
                        tmp.name,
                        unlink_exc,
                    )
        except Exception as exc:
            self._last_error = str(exc)
            raise

        # Build result
        text = "".join(seg.text for seg in segments_list).strip()
        segments = [
            Segment(
                text=seg.text.strip(),
                start=seg.start,
                end=seg.end,
                confidence=None,
            )
            for seg in segments_list
        ]

        self._last_error = None
        return TranscriptionResult(
            text=text,
            language=getattr(info, "language", None),
            confidence=getattr(info, "language_probability", None),
            duration_seconds=getattr(info, "duration", 0.0),
            segments=segments,
        )

    def health(self) -> bool:
        """Check if model is loaded or loadable."""
        try:
            self._ensure_model()
            return True
        except Exception as exc:
            self._last_error = str(exc)
            logger.debug("Faster-Whisper health check failed: %s", exc)
            return False

    def last_error(self) -> Optional[str]:
        """Return the last model load or transcription error, if any."""
        return self._last_error

    def supported_formats(self) -> List[str]:
        """Supported audio formats (same as ffmpeg/Whisper)."""
        return ["wav", "mp3", "m4a", "ogg", "flac", "webm"]
