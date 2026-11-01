"""F2: assembling voice input (jarvis/voice/system.py) and the app's
Stop-cuts-speech wiring. Every missing piece turns off only that piece,
with its missing-setup message (R14). No real device, model or hotkey."""

from __future__ import annotations

import time

import pytest

from jarvis.controller import Controller, State
from jarvis.settings import SETTINGS
from jarvis.voice import system
from jarvis.voice.audio_input import MicStatus

_MESSAGES = SETTINGS["setup_messages"]


class FakeMic:
    """Microphone stand-in: never opens a real device."""
    def __init__(self, on_frames, **kwargs):
        self.on_frames = on_frames
        self.status = MicStatus(live=True, device_name="Test Mic")

    def start(self):
        """Fake start for the tests."""
        return self.status

    def stop(self):
        """Fake stop for the tests."""
        pass


@pytest.fixture
def controller():
    """Fixture: a started Controller in AWAKE, shut down after the test."""
    c = Controller(start_state=State.AWAKE)
    c.start()
    yield c
    c.shutdown()


def _start(controller, monkeypatch, *, wake_ready=True, stt_ready=True, importable=lambda m: True):
    monkeypatch.setattr(system, "_importable", importable)
    monkeypatch.setattr(system, "wake_word_ready", lambda: wake_ready)
    monkeypatch.setattr(system, "stt_ready", lambda: stt_ready)
    monkeypatch.setattr(system, "MicInput", FakeMic)

    class FakeDetector:
        def __init__(self, on_detected):
            self.on_detected = on_detected

        def start(self):
            return True

        def stop(self):
            pass

        def release(self):
            pass

    monkeypatch.setattr(system, "WakeWordDetector", FakeDetector)
    v = system.start_voice(controller, lambda t, s: None, lambda: None, enable_hotkeys=False)
    return v


def test_everything_present_means_no_notes(controller, monkeypatch) -> None:
    """Everything present means no notes."""
    v = _start(controller, monkeypatch)
    try:
        assert v.notes == []
        assert v.mic_status.live and v.mic_status.device_name == "Test Mic"
        assert v.detector is not None and v.stt is not None
    finally:
        v.close()


def test_missing_wake_model_says_to_run_the_download_script(controller, monkeypatch) -> None:
    """Missing wake model says to run the download script."""
    v = _start(controller, monkeypatch, wake_ready=False)
    try:
        assert v.notes == [_MESSAGES["wake_model_missing"]]
        assert "scripts\\download_models.py" in v.notes[0] and "You can still type commands" in v.notes[0]
        assert v.detector is None and v.stt is not None  # push-to-talk can still work
    finally:
        v.close()


def test_missing_speech_model_says_to_run_the_download_script(controller, monkeypatch) -> None:
    """Missing speech model says to run the download script."""
    v = _start(controller, monkeypatch, stt_ready=False)
    try:
        assert v.notes == [_MESSAGES["stt_model_missing"]]
        assert v.stt is None
    finally:
        v.close()


def test_missing_libraries_give_one_setup_message(controller, monkeypatch) -> None:
    """Missing libraries give one setup message."""
    v = _start(controller, monkeypatch, importable=lambda m: m not in ("faster_whisper", "openwakeword", "sounddevice"))
    try:
        assert v.notes == [_MESSAGES["voice_library_missing"]]
        assert "pip install -r requirements.txt" in v.notes[0]
        assert v.mic is None
        assert v.mic_status.live is False and v.mic_status.note == _MESSAGES["voice_library_missing"]
    finally:
        v.close()


def test_no_failure_message_says_not_available() -> None:
    """No failure message says not available."""
    for key in ("mic_none", "mic_blocked", "mic_saved_missing", "voice_library_missing", "wake_model_missing", "stt_model_missing", "hotkey_in_use", "hotkey_invalid"):
        assert "not available" not in _MESSAGES[key].lower(), key


def test_app_cuts_speech_on_every_stop(monkeypatch) -> None:
    """make_app_components wires Stop -> speech.stop() once, for the STOP
    button, Esc and the global hotkey alike."""
    from jarvis.dashboard import app as app_module

    class FakeIndex:
        def start(self):
            pass

    class FakeSpeech:
        def __init__(self):
            self.stops = 0
            self.status = type("S", (), {"note": None})()

        def speak(self, text):
            pass

        def stop(self):
            self.stops += 1

    speech = FakeSpeech()
    monkeypatch.setattr(app_module, "FileIndex", FakeIndex)
    monkeypatch.setattr(app_module, "make_speech_output", lambda: speech)  # [speech] engine
    components = app_module.make_app_components()
    try:
        components.controller.request_stop("hotkey")
        deadline = time.monotonic() + 2
        while speech.stops == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert speech.stops == 1
    finally:
        components.controller.shutdown()
