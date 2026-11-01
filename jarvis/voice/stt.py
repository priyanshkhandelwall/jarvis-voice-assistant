"""F2 Stage 2: speech-to-text with faster-whisper ("base", CPU, int8 -- no
GPU/CUDA dependency). Local and offline: the model is loaded from the
project's models/ folder, never fetched at run time.

Decoding options come from settings ([stt]): VAD filter on, greedy
decoding (beam size 1), no conditioning on previous text -- so silence and
noise don't produce invented words.

R15 (stay light): the model is loaded on first use and unloaded after
[stt] unload_after_idle_seconds without a transcription.

Only ever called for audio recorded in LISTENING (after the wake word or
push-to-talk, or any utterance in conversation mode).
"""

from __future__ import annotations

import gc
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from jarvis.settings import SETTINGS
from jarvis.voice import model_files

logger = logging.getLogger(__name__)

_STT = SETTINGS["stt"]
_AUDIO = SETTINGS["audio"]

ModelFactory = Callable[[], Any]


def _default_model_factory() -> Any:
    from faster_whisper import WhisperModel

    return WhisperModel(
        str(model_files.whisper_dir()),
        device=str(_STT["device"]),
        compute_type=str(_STT["compute_type"]),
        local_files_only=True,
    )


def stt_ready() -> bool:
    """True if the Whisper model files are on disk."""
    return model_files.stt_ready()


def transcribe_options() -> dict:
    """The exact decoding options passed to faster-whisper (all settings)."""
    language = str(_STT["language"]).strip() or None
    options = {
        "beam_size": int(_STT["beam_size"]),
        "vad_filter": bool(_STT["vad_filter"]),
        "condition_on_previous_text": bool(_STT["condition_on_previous_text"]),
        "language": language,
        # Stops a short clip looping on a hotword (see settings).
        "without_timestamps": bool(_STT.get("without_timestamps", False)),
    }
    # Bias toward her name (faster-whisper 1.2.1 accepts both). Left out
    # entirely when empty, so the call is exactly as before.
    for key in ("hotwords", "initial_prompt"):
        value = str(_STT.get(key, "")).strip()
        if value:
            options[key] = value
    return options


@dataclass(frozen=True)
class Transcript:
    """Whisper's text for one utterance and how likely it was not speech at all."""
    text: str
    # Whisper's own estimate (0-1) that there was no speech at all: the
    # lowest across segments, i.e. how sure it is about its clearest
    # segment. 1.0 when nothing was transcribed.
    no_speech_prob: float


class SpeechToText:
    """F2: local speech-to-text with faster-whisper base.en (CPU, int8), loaded on first use and unloaded
    when idle."""
    def __init__(
        self,
        model_factory: Optional[ModelFactory] = None,
        unload_after_idle_seconds: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._model_factory = model_factory or _default_model_factory
        self._idle_limit = float(_STT["unload_after_idle_seconds"] if unload_after_idle_seconds is None else unload_after_idle_seconds)
        self._clock = clock
        self._model: Any = None
        self._last_used = 0.0
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        """True while the Whisper model is in memory."""
        return self._model is not None

    def transcribe(self, samples: Any) -> str:
        """`samples`: 1-D int16 numpy array at [audio] sample_rate. Returns
        the stripped text ("" for silence or noise). Loads the model on
        first use; raises if it cannot be loaded (the caller reports it)."""
        return self.transcribe_detailed(samples).text

    def transcribe_detailed(self, samples: Any) -> Transcript:
        """Like transcribe(), plus Whisper's no-speech probability, which
        conversation mode uses to ignore unclear fragments."""
        import numpy as np

        audio = np.asarray(samples, dtype=np.float32) / 32768.0
        with self._lock:
            if self._model is None:
                started = self._clock()
                self._model = self._model_factory()
                logger.info("Speech-to-text model loaded in %.1fs", self._clock() - started)
            segments, _info = self._model.transcribe(audio, **transcribe_options())
            segments = list(segments)
            text = " ".join(segment.text.strip() for segment in segments).strip()
            probs = [float(getattr(segment, "no_speech_prob", 0.0)) for segment in segments]
            self._last_used = self._clock()
        return Transcript(text=text, no_speech_prob=min(probs) if probs else 1.0)

    def release(self) -> None:
        """Free the model now (clean shutdown)."""
        with self._lock:
            self._model = None
        gc.collect()

    def unload_if_idle(self) -> bool:
        """Free the model if it hasn't been used for the idle limit. Called
        periodically by the listener. Returns True if it unloaded."""
        with self._lock:
            if self._model is None or self._clock() - self._last_used < self._idle_limit:
                return False
            self._model = None
        gc.collect()
        logger.info("Speech-to-text model unloaded after %.0fs idle (R15)", self._idle_limit)
        return True
