"""Chatterbox-Turbo TTS backend: local, 350M parameters, voice cloning.

Resemble AI's Chatterbox-Turbo (MIT) is a GPT-2-medium speech-token model
plus a one-step flow-matching decoder. It sounds far more natural than small
TTS models and clones any voice from a 5-30 s clip. It needs an NVIDIA GPU to
be fast (a sentence in well under a second); on CPU it is several times
slower than real time.

Requires ``pip install chatterbox-tts`` (``uv sync --extra voice``) and, for
the GPU, a CUDA build of PyTorch.

Voices (``speech.voice_id``):

* ``"default"`` - the voice that ships with the model;
* a name, e.g. ``"jarvis"`` - ``~/.openjarvis/voices/jarvis.wav``;
* a path to any .wav/.mp3/.flac clip of the voice to clone (5+ seconds).

Chatterbox embeds Resemble AI's imperceptible "Perth" watermark in what it
generates; that is kept.
"""

from __future__ import annotations

import io
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from openjarvis.core.registry import TTSRegistry
from openjarvis.speech.tts import TTSBackend, TTSResult

logger = logging.getLogger(__name__)

DEFAULT_VOICE = "default"
# Longest text said in one model call; longer sentences are split.
_LONGEST_SENTENCE = 300
_AUDIO_SUFFIXES = (".wav", ".mp3", ".flac", ".ogg", ".m4a")


def voices_dir() -> Path:
    from openjarvis.core.paths import get_config_dir

    return get_config_dir() / "voices"


def resolve_voice(voice_id: str) -> Optional[Path]:
    """The clip to clone for ``voice_id`` (None = the model's own voice)."""
    voice_id = (voice_id or "").strip()
    if not voice_id or voice_id.lower() == DEFAULT_VOICE:
        return None
    path = Path(voice_id).expanduser()
    if path.suffix.lower() in _AUDIO_SUFFIXES and path.is_file():
        return path
    for suffix in _AUDIO_SUFFIXES:
        candidate = voices_dir() / f"{voice_id}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def _quiet_progress_bars() -> None:
    """Chatterbox draws tqdm bars and prints a line for every sentence (and
    Perth one at load); Jarvis's console doesn't need them. Only these
    modules' own ``print``/``tqdm`` names are replaced."""
    import importlib

    def silent(*args: Any, **kwargs: Any) -> None:
        return None

    for name in (
        "chatterbox.models.t3.t3",
        "chatterbox.models.s3gen.flow_matching",
        "chatterbox.models.s3gen.s3gen",
        "perth.perth_net.perth_net_implicit.model.perth_net",
    ):
        try:
            module = importlib.import_module(name)
        except ImportError:  # pragma: no cover - layout changed upstream
            continue
        if hasattr(module, "tqdm"):
            module.tqdm = lambda iterable=None, *args, **kwargs: iterable
        module.print = silent


def _load(device: str) -> Any:
    """ChatterboxTurboTTS from the local cache (download only the first time)."""
    from chatterbox import tts_turbo

    patterns = ["*.safetensors", "*.json", "*.txt", "*.pt", "*.model"]
    try:
        from huggingface_hub import snapshot_download

        try:  # no network round trip once the weights are cached
            path = snapshot_download(
                tts_turbo.REPO_ID, allow_patterns=patterns, local_files_only=True
            )
        except Exception:
            logger.info("Downloading Chatterbox-Turbo (~1.5 GB, first use only)...")
            path = snapshot_download(tts_turbo.REPO_ID, allow_patterns=patterns)
        model = tts_turbo.ChatterboxTurboTTS.from_local(path, device)
    except (ImportError, AttributeError):
        model = tts_turbo.ChatterboxTurboTTS.from_pretrained(device=device)
    _float32_loudness(model)
    return model


def _float32_loudness(model: Any) -> None:
    """Keep the reference clip float32 under numpy 2.

    ``norm_loudness`` multiplies by a numpy float64 gain; with numpy 2's
    casting rules that turns the clip into float64 and voice cloning fails
    with "expected scalar type Float but found Double".
    """
    original = getattr(model, "norm_loudness", None)
    if original is None:
        return

    def norm_loudness(wav: Any, sr: int, target_lufs: float = -27) -> Any:
        import numpy as np

        return np.asarray(original(wav, sr, target_lufs), dtype=np.float32)

    model.norm_loudness = norm_loudness


@TTSRegistry.register("chatterbox")
class ChatterboxTTSBackend(TTSBackend):
    """Chatterbox-Turbo - local neural TTS with voice cloning."""

    backend_id = "chatterbox"

    def __init__(self, *, device: str = "auto", fast: bool = True) -> None:
        self._device_pref = device
        self._fast = fast
        self.fast_decoding = False
        self._device = ""
        self._model: Any = None
        self._voice: Optional[str] = None  # voice whose conditionals are loaded
        self._builtin: Any = None  # the model's own conditionals
        self._lock = threading.RLock()

    # -- loading -------------------------------------------------------------

    def _pick_device(self) -> str:
        if self._device_pref not in ("", "auto"):
            return self._device_pref
        try:
            import torch
        except ImportError:
            return "cpu"
        return "cuda" if torch.cuda.is_available() else "cpu"

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        import warnings

        # Import-time deprecation notices (pkg_resources in perth, LoRA layers
        # in diffusers) that mean nothing to someone talking to Jarvis.
        warnings.filterwarnings("ignore", category=UserWarning, module=r"perth\b")
        warnings.filterwarnings(
            "ignore", category=FutureWarning, module=r"(diffusers|chatterbox)\b"
        )
        try:
            import chatterbox.tts_turbo  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "chatterbox-tts is not installed. Install with: uv sync --extra voice"
            ) from exc
        _quiet_progress_bars()
        device = self._pick_device()
        try:
            model = _load(device)
        except RuntimeError as exc:
            if device == "cpu":
                raise
            logger.warning("Chatterbox on %s failed (%s); using the CPU", device, exc)
            device = "cpu"
            model = _load(device)
        self._device = device
        self._model = model
        if self._fast and device.startswith("cuda"):
            from openjarvis.speech._chatterbox_fast import install

            self.fast_decoding = install(model)
        self._builtin = model.conds
        self._voice = DEFAULT_VOICE
        return model

    def _use_voice(self, voice_id: str) -> str:
        """Load the conditionals for ``voice_id`` (cached until it changes)."""
        model = self._ensure_model()
        clip = resolve_voice(voice_id)
        key = str(clip) if clip else DEFAULT_VOICE
        if key == self._voice:
            return key
        if clip is None:
            if voice_id and voice_id.lower() != DEFAULT_VOICE:
                logger.warning(
                    "Voice %r not found (put a clip in %s); using the default voice",
                    voice_id,
                    voices_dir(),
                )
            model.conds = self._builtin
        else:
            model.prepare_conditionals(str(clip))
        self._voice = key
        return key

    def _generate(self, text: str) -> Any:
        import torch

        with torch.inference_mode():
            return self._model.generate(text)

    def _say(self, text: str, depth: int = 0) -> Any:
        """Samples for ``text``; a sentence too long for the fast decoder is
        said in halves (split at commas or spaces) instead of slowing down."""
        import numpy as np

        from openjarvis.speech._chatterbox_fast import SentenceTooLong
        from openjarvis.speech.speakable import split_long

        def in_pieces(limit: int) -> Any:
            pieces = split_long(text, limit)
            if depth >= 4 or len(pieces) < 2:
                return None
            gap = np.zeros(int(0.12 * self._model.sr), dtype=np.float32)
            parts = []
            for piece in pieces:
                parts += [self._say(piece, depth + 1), gap]
            return np.concatenate(parts[:-1])

        if len(text) > _LONGEST_SENTENCE:
            # Split up front: a failed whole-sentence attempt costs seconds.
            joined = in_pieces(_LONGEST_SENTENCE)
            if joined is not None:
                return joined
        try:
            return self._generate(text).squeeze(0).float().cpu().numpy()
        except SentenceTooLong:
            joined = in_pieces(max(40, len(text) // 2))
            if joined is None:
                raise
            return joined

    def _reload_on_cpu(self) -> None:
        self.close()
        self._device_pref = "cpu"
        self._ensure_model()

    # -- TTSBackend ----------------------------------------------------------

    def synthesize(
        self,
        text: str,
        *,
        voice_id: str = "jarvis",
        speed: float = 1.0,
        output_format: str = "wav",
    ) -> TTSResult:
        import numpy as np
        import soundfile as sf

        text = (text or "").strip()
        meta: Dict[str, Any] = {"backend": self.backend_id}
        if not text:
            return TTSResult(
                audio=b"", format=output_format, voice_id=voice_id, metadata=meta
            )
        with self._lock:
            voice = self._use_voice(voice_id)
            try:
                samples = self._say(text)
            except RuntimeError as exc:  # CUDA out of memory, driver reset...
                if self._device == "cpu":
                    raise
                logger.warning(
                    "Chatterbox on %s failed (%s); using the CPU", self._device, exc
                )
                self._reload_on_cpu()
                voice = self._use_voice(voice_id)
                samples = self._say(text)
            rate = int(self._model.sr)
        if speed and abs(speed - 1.0) > 0.01:
            try:
                import librosa

                samples = librosa.effects.time_stretch(samples, rate=float(speed))
            except Exception:  # pragma: no cover - optional nicety
                logger.debug("time_stretch failed", exc_info=True)
        samples = np.clip(samples, -1.0, 1.0)
        buf = io.BytesIO()
        sf.write(buf, samples, rate, format=output_format.upper())
        meta.update({"device": self._device, "voice": voice})
        return TTSResult(
            audio=buf.getvalue(),
            format=output_format,
            voice_id=voice_id,
            sample_rate=rate,
            duration_seconds=len(samples) / rate,
            metadata=meta,
        )

    def available_voices(self) -> List[str]:
        names = [DEFAULT_VOICE]
        try:
            names += sorted(
                p.stem
                for p in voices_dir().iterdir()
                if p.suffix.lower() in _AUDIO_SUFFIXES
            )
        except OSError:
            pass
        return names

    def health(self) -> bool:
        with self._lock:
            try:
                self._ensure_model()
                return True
            except Exception as exc:
                logger.debug("Chatterbox unavailable: %s", exc)
                return False

    def close(self) -> None:
        with self._lock:
            self._model = None
            self._builtin = None
            self._voice = None
            try:
                import torch

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except ImportError:
                pass


__all__ = ["ChatterboxTTSBackend", "resolve_voice", "voices_dir"]
