"""F2 Stage 2: the "Hey JARVIS" wake-word detector (openWakeWord, using its
pre-trained "hey jarvis" model, ONNX runtime -- tflite has no Windows
build, so inference_framework="onnx" is forced).

- The audio callback only puts frames into a bounded queue. When the queue
  is full the newest frame is dropped: feed() never blocks.
- The model runs on its own thread.
- After a detection the queued frames are discarded and the model state is
  reset, so one spoken phrase fires once.
- Self-echo guard (F2 Stage 3): ignore_for(seconds) discards detections for
  a short tail after she finishes speaking, and resets the model so her
  own voice saying her name cannot wake her.
- Wake-word audio is never saved, transcribed or shown by this module:
  frames live only in this queue and the model's own rolling buffer, both
  in memory. ([wake_word] debug_scores logs numbers only, never audio.)
- "Ready" means the model files are on disk, checked without loading them.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from jarvis.settings import SETTINGS
from jarvis.voice import model_files

logger = logging.getLogger(__name__)

_SETTINGS = SETTINGS["wake_word"]
_AUDIO = SETTINGS["audio"]

ModelFactory = Callable[[], Any]


def openwakeword_model_class() -> Any:
    """openwakeword.model.Model, without the sklearn/scipy.stats import its
    package __init__ makes for custom verifier TRAINING, which JARVIS never
    uses (That import alone was 1,286 files and 108 MB,
    most of the 32 s the wake-word step took on a cold start from the D:
    hard disk; with the stub it is 314 files, 31 MB)."""
    import sys
    import types

    name = "openwakeword.custom_verifier_model"
    if name not in sys.modules:
        stub = types.ModuleType(name)

        def train_custom_verifier(*_args, **_kwargs):
            raise RuntimeError("JARVIS doesn't train openWakeWord custom verifier models.")

        stub.train_custom_verifier = train_custom_verifier
        sys.modules[name] = stub
    from openwakeword.model import Model

    return Model


def _default_model_factory() -> Any:
    Model = openwakeword_model_class()

    return Model(
        wakeword_models=[str(model_files.wake_word_model_path())],
        inference_framework="onnx",
        melspec_model_path=str(model_files.melspectrogram_model_path()),
        embedding_model_path=str(model_files.embedding_model_path()),
    )


def wake_word_ready() -> bool:
    """True if the wake-word model files are on disk."""
    return model_files.wake_word_ready()


class WakeWordDetector:
    """F2: openWakeWord's 'hey jarvis' model, scoring audio frames on its own thread."""
    def __init__(
        self,
        on_detected: Callable[[float], None],
        model_factory: Optional[ModelFactory] = None,
        threshold: Optional[float] = None,
        queue_frames: Optional[int] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._on_detected = on_detected
        self._model_factory = model_factory or _default_model_factory
        self._threshold = float(_SETTINGS["threshold"] if threshold is None else threshold)
        self._queue: "queue.Queue[Any]" = queue.Queue(maxsize=int(queue_frames or _AUDIO["wake_queue_frames"]))
        self._clock = clock
        self._model: Any = None
        self._model_lock = threading.Lock()
        self._ignore_until = 0.0
        self._running = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.dropped_frames = 0
        self.error: Optional[str] = None
        # Tuning aid ([wake_word] debug_scores): peak score per second.
        self._debug_path: Optional[Path] = (
            model_files.PROJECT_ROOT / str(_SETTINGS["debug_log_path"]) if bool(_SETTINGS["debug_scores"]) else None
        )
        self._frames_per_second = max(1, round(int(_AUDIO["sample_rate"]) / int(_AUDIO["block_size"])))
        self._second_peak = 0.0
        self._second_frames = 0
        self._second_rms: list[float] = []
        self._log_lock = threading.Lock()

    # -- control -------------------------------------------------------------

    def start(self) -> bool:
        """Load the model (on the caller's thread, so a failure is known at
        once) and start the detection thread. Returns False if the model
        could not be loaded; the caller shows the setup message."""
        try:
            self._model = self._model_factory()
        except Exception as exc:
            logger.exception("Wake-word model failed to load")
            self.error = str(exc)
            return False
        self._running.set()
        self._thread = threading.Thread(target=self._run, name="jarvis-wake-word", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        """Stop the detector thread."""
        self._running.clear()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2)

    def release(self) -> None:
        """Free the model (clean shutdown). Call after stop()."""
        with self._model_lock:
            self._model = None

    def feed(self, frame: Any) -> None:
        """Called from the audio callback. Never blocks: a full queue drops
        this frame."""
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            self.dropped_frames += 1

    def ignore_for(self, seconds: float) -> None:
        """Self-echo guard: no detection counts until `seconds` from now, and
        the model forgets the audio it has heard so far."""
        self._ignore_until = self._clock() + seconds
        self.reset()

    def reset(self) -> None:
        """Drop queued frames and clear the model's rolling state."""
        self._drain()
        with self._model_lock:
            if self._model is not None:
                try:
                    self._model.reset()
                except Exception:
                    logger.exception("Wake-word model reset failed")

    # -- detection thread ------------------------------------------------------

    def _drain(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def process(self, frame: Any) -> bool:
        """Score one frame. Returns True if it counted as a detection. Kept
        separate from the thread loop so tests can drive it directly."""
        with self._model_lock:
            if self._model is None:
                return False  # released (shutting down)
            scores = self._model.predict(frame)
        score = max(scores.values()) if scores else 0.0
        self._log_score(score, frame)
        if score < self._threshold:
            return False
        if self._clock() < self._ignore_until:
            # Inside the echo tail: her own voice. Forget it and carry on.
            self.reset()
            return False
        self.reset()  # one phrase fires once
        try:
            self._on_detected(float(score))
        except Exception:
            logger.exception("Wake-word detection handler failed")
        return True

    def _log_score(self, score: float, frame: Any = None) -> None:
        """Debug mode only: one line per second with that second's peak
        score and median microphone loudness (RMS) -- numbers, never audio."""
        if self._debug_path is None:
            return
        self._second_peak = max(self._second_peak, score)
        self._second_frames += 1
        if frame is not None and len(frame):
            import numpy as np

            samples = np.asarray(frame, dtype=np.float32)
            self._second_rms.append(float(np.sqrt(np.mean(samples * samples))))
        if self._second_frames < self._frames_per_second:
            return
        levels = sorted(self._second_rms)
        rms = f" rms {levels[len(levels) // 2]:.0f}" if levels else ""
        peak = self._second_peak
        self._second_peak, self._second_frames, self._second_rms = 0.0, 0, []
        self.log_line(f"peak {peak:.3f} threshold {self._threshold:.2f}{rms}")

    def log_line(self, text: str) -> None:
        """One timestamped line in the wake-score log (debug mode only).
        Numbers and fixed words only -- callers never pass what was said."""
        if self._debug_path is None:
            return
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {text}\n"
        try:
            with self._log_lock:
                self._debug_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._debug_path, "a", encoding="utf-8") as f:
                    f.write(line)
        except OSError:
            logger.exception("Could not write the wake-score log")

    def _run(self) -> None:
        while self._running.is_set():
            try:
                frame = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if frame is None:
                continue
            try:
                self.process(frame)
            except Exception:
                logger.exception("Wake-word scoring failed; carrying on")
