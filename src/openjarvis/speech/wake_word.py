"""Wake-word detection ("Hey Jarvis") for hands-free voice mode.

Backed by openWakeWord, whose small ONNX models run on CPU at a few percent
of one core, so the detector can stay on all day without touching the GPU
that the LLM needs.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
# openWakeWord is trained on 80 ms frames; feeding exactly that keeps
# detection latency at one frame.
FRAME_SAMPLES = 1280
_INSTALL_HINT = (
    "Wake word needs openWakeWord. Install with: uv sync --extra wakeword "
    "(or pip install 'OpenJarvis[wakeword]')"
)


class WakeWordDetector:
    """Score microphone frames against one wake-word model."""

    def __init__(self, model: str = "hey_jarvis", threshold: float = 0.5) -> None:
        if not 0.0 < threshold < 1.0:
            raise ValueError("wake_word_threshold must be between 0 and 1")
        self._model_ref = model
        self.threshold = threshold
        self._model: Any = None
        self._key: str = ""

    @property
    def label(self) -> str:
        """Human-readable wake phrase, e.g. "hey jarvis"."""
        base = os.path.splitext(os.path.basename(self._model_ref))[0]
        return base.split("_v")[0].replace("_", " ")

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            import openwakeword
            from openwakeword.model import Model
            from openwakeword.utils import download_models
        except (ImportError, OSError) as exc:
            raise RuntimeError(f"{_INSTALL_HINT} ({exc})") from exc

        ref = self._model_ref
        if not os.path.exists(ref):
            if ref not in openwakeword.MODELS:
                known = ", ".join(sorted(openwakeword.MODELS))
                raise ValueError(
                    f"Unknown wake word {ref!r}. Use one of: {known}, "
                    "or a path to a custom .onnx model."
                )
            # First run only: fetches the ~1 MB model plus the shared
            # feature extractors into openWakeWord's resources folder.
            download_models([ref])
        self._model = Model(wakeword_models=[ref], inference_framework="onnx")
        self._key = next(iter(self._model.models))
        return self._model

    def load(self) -> None:
        """Load (and on first use download) the model; raises on failure."""
        self._ensure_model()

    def score(self, frame: Any) -> float:
        """Return the wake-word probability for one int16 audio frame."""
        import numpy as np

        model = self._ensure_model()
        samples = (
            np.frombuffer(frame, dtype=np.int16)
            if isinstance(frame, (bytes, bytearray, memoryview))
            else frame
        )
        prediction = model.predict(samples)
        return float(prediction.get(self._key, 0.0))

    def reset(self) -> None:
        """Clear buffered audio so one utterance can't trigger twice."""
        if self._model is not None:
            self._model.reset()

    def wait(
        self,
        *,
        should_abort: Optional[Callable[[], bool]] = None,
        threshold: Optional[float] = None,
    ) -> bool:
        """Block until the wake word is heard (True) or ``should_abort()`` (False).

        Opens its own input stream and closes it before returning, so the
        caller can record the command on the same microphone right after.
        """
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError(
                "sounddevice is required for voice input. "
                "Install with: pip install sounddevice"
            ) from exc

        self._ensure_model()
        self.reset()
        limit = self.threshold if threshold is None else threshold
        with sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=FRAME_SAMPLES,
        ) as stream:
            while True:
                if should_abort is not None and should_abort():
                    return False
                raw, _ = stream.read(FRAME_SAMPLES)
                if self.score(bytes(raw)) >= limit:
                    self.reset()
                    return True


class BargeIn:
    """Listen for the wake word on a thread while Jarvis is speaking.

    Use as a context manager around playback and pass :meth:`heard` as the
    ``should_stop`` callback. Speaker echo rarely contains the wake phrase,
    but the threshold is raised a little to keep it from cutting itself off.
    """

    def __init__(self, detector: WakeWordDetector, *, margin: float = 0.15) -> None:
        self._detector = detector
        self._threshold = min(0.95, detector.threshold + margin)
        self._heard = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def heard(self) -> bool:
        return self._heard.is_set()

    def _run(self) -> None:
        try:
            if self._detector.wait(
                should_abort=self._stop.is_set, threshold=self._threshold
            ):
                self._heard.set()
        except Exception as exc:  # mic busy / device lost: just no barge-in
            logger.debug("Barge-in listener stopped: %s", exc)

    def __enter__(self) -> "BargeIn":
        self._thread = threading.Thread(
            target=self._run, name="openjarvis-barge-in", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


__all__ = ["BargeIn", "FRAME_SAMPLES", "SAMPLE_RATE", "WakeWordDetector"]
