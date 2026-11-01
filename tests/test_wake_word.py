"""F2 Stage 2/3: the wake-word detector (jarvis/voice/wake_word.py), with
openWakeWord replaced by a fake model -- no audio, no model files."""

from __future__ import annotations

import threading
import time

import numpy as np

from jarvis.voice import wake_word
from jarvis.voice.wake_word import WakeWordDetector

_FRAME = np.zeros(1280, dtype=np.int16)


class FakeModel:
    """Wake-word model stand-in returning prepared scores."""
    def __init__(self, scores):
        self.scores = list(scores)
        self.resets = 0
        self.seen = 0

    def predict(self, frame):
        """Fake predict for the tests."""
        self.seen += 1
        score = self.scores.pop(0) if self.scores else 0.0
        return {"hey_jarvis_v0.1": score}

    def reset(self):
        """Fake reset for the tests."""
        self.resets += 1


class FakeClock:
    """A clock the test moves by hand."""
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def _detector(scores, threshold=0.5, queue_frames=5, clock=None):
    model = FakeModel(scores)
    hits = []
    d = WakeWordDetector(hits.append, model_factory=lambda: model, threshold=threshold, queue_frames=queue_frames, clock=clock or FakeClock())
    assert d.start()
    d.stop()  # tests drive process() directly, without the thread
    return d, model, hits


def test_score_below_threshold_is_not_a_detection() -> None:
    """Score below threshold is not a detection."""
    d, model, hits = _detector([0.49])
    assert d.process(_FRAME) is False
    assert hits == []


def test_score_at_or_above_threshold_fires_once_and_resets() -> None:
    """Score at or above threshold fires once and resets."""
    d, model, hits = _detector([0.5, 0.97])
    assert d.process(_FRAME) is True
    assert hits == [0.5]
    assert model.resets == 1  # model state cleared so one phrase fires once


def test_detection_drains_queued_frames() -> None:
    """Detection drains queued frames."""
    d, model, hits = _detector([0.9], queue_frames=10)
    for _ in range(4):
        d.feed(_FRAME)
    d.process(_FRAME)
    assert d._queue.empty()


def test_full_queue_drops_frames_without_blocking() -> None:
    """Full queue drops frames without blocking."""
    d, model, hits = _detector([], queue_frames=3)
    started = time.monotonic()
    for _ in range(1000):
        d.feed(_FRAME)
    assert time.monotonic() - started < 0.5
    assert d.dropped_frames == 997
    assert d._queue.qsize() == 3


def test_thread_scores_frames_and_fires_exactly_once() -> None:
    """Thread scores frames and fires exactly once."""
    model = FakeModel([0.1, 0.2, 0.95, 0.95, 0.95])
    hits = []
    fired = threading.Event()
    d = WakeWordDetector(lambda s: (hits.append(s), fired.set()), model_factory=lambda: model, queue_frames=10)
    assert d.start()
    try:
        for _ in range(5):
            d.feed(_FRAME)
        assert fired.wait(2)
        time.sleep(0.2)
    finally:
        d.stop()
    # The detection drained the queue, so the later 0.95 frames never scored.
    assert len(hits) == 1


def test_model_that_fails_to_load_reports_false() -> None:
    """Model that fails to load reports false."""
    def broken():
        raise FileNotFoundError("no model")

    d = WakeWordDetector(lambda s: None, model_factory=broken)
    assert d.start() is False
    assert "no model" in d.error


def test_readiness_checks_files_without_loading_the_model(tmp_path, monkeypatch) -> None:
    """Readiness checks files without loading the model."""
    built = []
    monkeypatch.setattr(wake_word, "_default_model_factory", lambda: built.append(1))
    monkeypatch.setattr(wake_word.model_files, "wake_word_dir", lambda: tmp_path)
    assert wake_word.wake_word_ready() is False
    for name in ("hey_jarvis_v0.1.onnx", "melspectrogram.onnx", "embedding_model.onnx"):
        (tmp_path / name).write_bytes(b"x")
    assert wake_word.wake_word_ready() is True
    assert built == []


# -- Self-echo guard (Stage 3) ------------------------------------------------


def test_detection_inside_echo_tail_is_ignored_and_resets() -> None:
    """Detection inside echo tail is ignored and resets."""
    clock = FakeClock()
    d, model, hits = _detector([0.99, 0.99], clock=clock)
    d.ignore_for(0.3)
    resets_after_guard = model.resets
    assert resets_after_guard == 1  # the guard itself resets the model
    clock.now += 0.2  # still inside the tail
    assert d.process(_FRAME) is False
    assert hits == []
    assert model.resets == resets_after_guard + 1


def test_detection_just_after_echo_tail_counts() -> None:
    """Detection just after echo tail counts."""
    clock = FakeClock()
    d, model, hits = _detector([0.99], clock=clock)
    d.ignore_for(0.3)
    clock.now += 0.31
    assert d.process(_FRAME) is True
    assert hits == [0.99]
