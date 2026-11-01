"""F2 Stage 2: speech-to-text (jarvis/voice/stt.py), with faster-whisper
replaced by a fake model -- no model files, no audio device."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from jarvis.settings import SETTINGS
from jarvis.voice import stt as stt_module
from jarvis.voice.stt import SpeechToText, transcribe_options


class FakeWhisper:
    """Whisper model stand-in returning prepared texts in turn."""
    def __init__(self, texts=("open chrome",)):
        self.texts = list(texts)
        self.calls = []

    def transcribe(self, audio, **options):
        """Fake transcribe for the tests."""
        self.calls.append((audio, options))
        return iter([SimpleNamespace(text=f" {t} ") for t in self.texts]), SimpleNamespace(language="en")


class FakeClock:
    """A clock the test moves by hand."""
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_exact_decoding_options_come_from_settings() -> None:
    """Exact decoding options come from settings."""
    cfg = SETTINGS["stt"]
    assert transcribe_options() == {
        "beam_size": 1,
        "vad_filter": True,
        "condition_on_previous_text": False,
        "language": "en",
        # Bias toward her name; initial_prompt stays off.
        # Plus app and site names, and no timestamp tokens (a
        # lone "VS Code" looped for 16-21 s with them).
        "hotwords": "JARVIS, Chrome, ChatGPT, VS Code, Visual Studio Code, Notepad, YouTube, Spotify, Gmail, GitHub, Priyansh",
        "without_timestamps": True,
    }
    assert cfg["device"] == "cpu" and cfg["compute_type"] == "int8"


def test_model_is_loaded_lazily_and_the_options_are_passed() -> None:
    """Model is loaded lazily and the options are passed."""
    model = FakeWhisper()
    loads = []
    s = SpeechToText(model_factory=lambda: (loads.append(1), model)[1])
    assert loads == [] and not s.loaded  # nothing loaded at construction (R15)
    text = s.transcribe(np.zeros(16000, dtype=np.int16))
    assert text == "open chrome"
    assert loads == [1]
    audio, options = model.calls[0]
    assert options == transcribe_options()
    assert audio.dtype == np.float32 and float(np.abs(audio).max()) <= 1.0
    s.transcribe(np.zeros(16000, dtype=np.int16))
    assert loads == [1]  # loaded once


def test_silence_gives_an_empty_string() -> None:
    """Silence gives an empty string."""
    s = SpeechToText(model_factory=lambda: FakeWhisper(texts=()))
    assert s.transcribe(np.zeros(16000, dtype=np.int16)) == ""


def test_unloaded_after_idle_and_reloaded_on_next_use() -> None:
    """Unloaded after idle and reloaded on next use."""
    clock = FakeClock()
    loads = []
    s = SpeechToText(model_factory=lambda: (loads.append(1), FakeWhisper())[1], unload_after_idle_seconds=120, clock=clock)
    s.transcribe(np.zeros(1600, dtype=np.int16))
    clock.now = 119
    assert s.unload_if_idle() is False and s.loaded
    clock.now = 121
    assert s.unload_if_idle() is True and not s.loaded
    s.transcribe(np.zeros(1600, dtype=np.int16))
    assert loads == [1, 1]


def test_default_factory_loads_from_the_project_folder_offline(monkeypatch) -> None:
    """Default factory loads from the project folder offline."""
    captured = {}

    class FakeWhisperModel:
        def __init__(self, path, **kwargs):
            captured["path"], captured["kwargs"] = path, kwargs

    import faster_whisper

    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeWhisperModel)
    stt_module._default_model_factory()
    assert captured["path"] == str(stt_module.model_files.whisper_dir())
    assert captured["kwargs"] == {"device": "cpu", "compute_type": "int8", "local_files_only": True}
