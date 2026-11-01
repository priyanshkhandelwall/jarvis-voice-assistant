"""F2 Stage 2: microphone selection and failure messages
(jarvis/voice/audio_input.py), with sounddevice replaced by a fake -- no
real audio device is opened."""

from __future__ import annotations

import numpy as np
import pytest

from jarvis.settings import SETTINGS
from jarvis.voice import audio_input
from jarvis.voice.audio_input import MicInput, find_device_by_name

_MESSAGES = SETTINGS["setup_messages"]


class FakeStream:
    """sounddevice input-stream stand-in; can be set to fail on start."""
    def __init__(self, fail=False, **kwargs):
        if fail:
            raise RuntimeError("Error opening InputStream: Unanticipated host error")
        self.kwargs = kwargs
        self.started = False
        self.closed = False

    def start(self):
        """Fake start for the tests."""
        self.started = True

    def stop(self):
        """Fake stop for the tests."""
        self.started = False

    def close(self):
        """Fake close for the tests."""
        self.closed = True


class FakeDefault:
    """sounddevice's `default` stand-in holding the default device index."""
    def __init__(self, index):
        self.device = (index, 3)


class FakeSD:
    """sounddevice stand-in with a chosen device list; can fail to open chosen devices."""
    def __init__(self, devices, default_index=0, fail_indices=()):
        self._devices = devices
        self.default = FakeDefault(default_index)
        self.fail_indices = set(fail_indices)
        self.opened = []

    def query_devices(self, index=None):
        """Fake query devices for the tests."""
        return self._devices if index is None else self._devices[index]

    def InputStream(self, **kwargs):
        """Fake inputstream for the tests."""
        self.opened.append(kwargs["device"])
        return FakeStream(fail=kwargs["device"] in self.fail_indices, **kwargs)


@pytest.fixture(autouse=True)
def _windows_default_microphone(monkeypatch):
    """Tests never depend on the developer's saved microphone ([audio]
    input_device_name): on it was the Contoso earbuds, which weren't
    connected, and a test that didn't name a device failed. A test that
    wants a named device passes device_name itself."""
    monkeypatch.setitem(audio_input._AUDIO, "input_device_name", "")


def _dev(name, inputs=1, hostapi=0):
    return {"name": name, "max_input_channels": inputs, "hostapi": hostapi}


_DEVICES = [
    _dev("Microphone Array (Realtek(R) Au"),
    _dev("Speakers", inputs=0),
    _dev("Headset Microphone (Earphones)"),
]


def test_named_device_is_found_and_opened_at_16k_mono_int16_1280() -> None:
    """Named device is found and opened at 16k mono int16 1280."""
    sd = FakeSD(_DEVICES)
    mic = MicInput(on_frames=lambda f: None, device_name="Headset Microphone (Earphones)", sd_module=sd)
    status = mic.start()
    assert status.live and status.device_name == "Headset Microphone (Earphones)" and status.note is None
    stream = mic._stream
    assert stream.kwargs["samplerate"] == 16000 and stream.kwargs["channels"] == 1
    assert stream.kwargs["dtype"] == "int16" and stream.kwargs["blocksize"] == 1280
    assert sd.opened == [2]


def test_name_lookup_is_case_insensitive_and_handles_mme_truncation() -> None:
    """Name lookup is case insensitive and handles mme truncation."""
    devices = audio_input._input_devices(FakeSD(_DEVICES))  # inputs only, as start() does
    assert find_device_by_name(devices, "headset microphone (earphones)", 0) == 2
    # MME cuts names at 31 characters; a full saved name still matches.
    assert find_device_by_name(devices, "Microphone Array (Realtek(R) Audio)", 0) == 0
    assert find_device_by_name(devices, "Speakers", 0) is None  # no input channels
    assert find_device_by_name(devices, "", 0) is None


def test_named_device_absent_uses_default_and_says_so() -> None:
    """Named device absent uses default and says so."""
    sd = FakeSD(_DEVICES)
    mic = MicInput(on_frames=lambda f: None, device_name="USB Podcast Mic", sd_module=sd)
    status = mic.start()
    assert status.live is True
    assert status.device_name == "Microphone Array (Realtek(R) Au"
    assert status.note == _MESSAGES["mic_saved_missing"].format(name="USB Podcast Mic", default="Microphone Array (Realtek(R) Au")


def test_named_device_that_fails_to_open_falls_back_to_default() -> None:
    """Named device that fails to open falls back to default."""
    sd = FakeSD(_DEVICES, fail_indices={2})
    status = MicInput(on_frames=lambda f: None, device_name="Headset Microphone (Earphones)", sd_module=sd).start()
    assert status.live and status.device_name.startswith("Microphone Array")
    assert "isn't connected" in status.note


def test_no_microphone_at_all_gives_the_setup_message_and_no_crash() -> None:
    """No microphone at all gives the setup message and no crash."""
    sd = FakeSD([_dev("Speakers", inputs=0)])
    status = MicInput(on_frames=lambda f: None, sd_module=sd).start()
    assert status.live is False
    assert status.note == _MESSAGES["mic_none"]


def test_default_that_cannot_open_says_windows_may_be_blocking() -> None:
    """Default that cannot open says Windows may be blocking."""
    sd = FakeSD(_DEVICES, fail_indices={0})
    status = MicInput(on_frames=lambda f: None, sd_module=sd).start()
    assert status.live is False
    assert status.note == _MESSAGES["mic_blocked"]


def test_missing_library_gives_the_setup_message(monkeypatch) -> None:
    """Missing library gives the setup message."""
    def missing():
        raise audio_input.MicUnavailable(_MESSAGES["voice_library_missing"])

    monkeypatch.setattr(audio_input, "_load_sounddevice", missing)
    status = MicInput(on_frames=lambda f: None).start()
    assert status.live is False and status.note == _MESSAGES["voice_library_missing"]
    assert "pip install -r requirements.txt" in status.note


def test_all_zero_audio_is_reported_as_windows_blocking_the_mic() -> None:
    """All zero audio is reported as Windows blocking the mic."""
    seen = []
    statuses = []
    mic = MicInput(on_frames=seen.append, sd_module=FakeSD(_DEVICES), on_status=statuses.append)
    mic.start()
    block = np.zeros((1280, 1), dtype=np.int16)
    for _ in range(mic._silent_check_frames):
        mic._callback(block, 1280, None, None)
    assert mic.status.live is False and mic.status.note == _MESSAGES["mic_blocked"]
    assert statuses[-1].note == _MESSAGES["mic_blocked"]


def test_real_noise_is_not_reported_as_blocked() -> None:
    """Real noise is not reported as blocked."""
    mic = MicInput(on_frames=lambda f: None, sd_module=FakeSD(_DEVICES))
    mic.start()
    noise = np.full((1280, 1), 3, dtype=np.int16)
    for _ in range(mic._silent_check_frames + 5):
        mic._callback(noise, 1280, None, None)
    assert mic.status.live is True and mic.status.note is None


def test_callback_hands_a_copy_on_and_never_raises() -> None:
    """Callback hands a copy on and never raises."""
    def broken(frame):
        raise ValueError("listener bug")

    mic = MicInput(on_frames=broken, sd_module=FakeSD(_DEVICES))
    mic.start()
    mic._callback(np.ones((1280, 1), dtype=np.int16), 1280, None, None)  # must not raise


def test_frames_are_one_dimensional_copies() -> None:
    """Frames are one dimensional copies."""
    got = []
    mic = MicInput(on_frames=got.append, sd_module=FakeSD(_DEVICES))
    mic.start()
    block = np.ones((1280, 1), dtype=np.int16)
    mic._callback(block, 1280, None, None)
    block[:] = 7
    assert got[0].shape == (1280,) and int(got[0][0]) == 1


def test_stop_closes_the_stream() -> None:
    """Stop closes the stream."""
    mic = MicInput(on_frames=lambda f: None, sd_module=FakeSD(_DEVICES))
    mic.start()
    stream = mic._stream
    mic.stop()
    assert stream.closed and mic._stream is None
