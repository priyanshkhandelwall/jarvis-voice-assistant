"""Microphone choice, native sample rates, the
wake-score debug log, saving the mic by name, and clean shutdown. A fake
sounddevice is used; no real device is opened."""

from __future__ import annotations

import re

import numpy as np
import pytest

from jarvis.settings import SETTINGS, load_settings, save_string_setting
from jarvis.voice import audio_input
from jarvis.voice.audio_input import Decimator, MicInput, list_input_device_names, plan_capture


class FakeSD:
    """The laptop's real device layout, host APIs included."""

    APIS = [{"name": "MME"}, {"name": "Windows DirectSound"}, {"name": "Windows WASAPI"}, {"name": "Windows WDM-KS"}]
    DEVICES = [
        {"name": "Microsoft Sound Mapper - Input", "max_input_channels": 2, "hostapi": 0, "default_samplerate": 44100.0},
        {"name": "Microphone Array (Realtek(R) Au", "max_input_channels": 2, "hostapi": 0, "default_samplerate": 44100.0},
        {"name": "Headset (Contoso Wireless Earbuds)", "max_input_channels": 1, "hostapi": 0, "default_samplerate": 44100.0},
        {"name": "Microphone Array (Realtek(R) Audio)", "max_input_channels": 2, "hostapi": 2, "default_samplerate": 48000.0},
        {"name": "Headset (Contoso Wireless Earbuds)", "max_input_channels": 1, "hostapi": 2, "default_samplerate": 16000.0},
        {"name": "Stereo Mix (Realtek HD Audio Stereo input)", "max_input_channels": 2, "hostapi": 3, "default_samplerate": 48000.0},
    ]

    class default:
        """sounddevice's `default` stand-in: input device 1, output device 3."""

        device = (1, 3)

    def __init__(self, fail=()):
        self.fail = set(fail)
        self.opened = []

    def query_devices(self, index=None):
        """Fake query devices for the tests."""
        return self.DEVICES if index is None else self.DEVICES[index]

    def query_hostapis(self, index):
        """Fake query hostapis for the tests."""
        return self.APIS[index]

    def InputStream(self, **kw):
        """Fake inputstream for the tests."""
        self.opened.append((kw["device"], kw["samplerate"], kw["blocksize"]))
        if kw["device"] in self.fail:
            raise RuntimeError("Invalid sample rate")
        return _Stream()


class _Stream:
    """Test helper: Stream."""
    def start(self):
        """Fake start for the tests."""
        pass

    def stop(self):
        """Fake stop for the tests."""
        pass

    def close(self):
        """Fake close for the tests."""
        pass


def test_picker_lists_each_microphone_once_by_full_name() -> None:
    """Picker lists each microphone once by full name."""
    names = list_input_device_names(FakeSD())
    assert names == ["Headset (Contoso Wireless Earbuds)", "Microphone Array (Realtek(R) Audio)"]
    # The Windows "Sound Mapper" alias and WDM-KS devices are not offered.


def test_native_rate_copy_is_preferred_and_decimated() -> None:
    """Native rate copy is preferred and decimated."""
    sd = FakeSD()
    devices = audio_input._input_devices(sd)
    realtek = plan_capture(sd, devices, "Microphone Array (Realtek(R) Audio)")
    assert (realtek[0].index, realtek[0].rate, realtek[0].factor) == (3, 48000, 3)
    assert "decimated to 16000 Hz in JARVIS" in realtek[0].description
    contoso = plan_capture(sd, devices, "Headset (Contoso Wireless Earbuds)")
    assert (contoso[0].index, contoso[0].rate, contoso[0].factor) == (4, 16000, 1)  # earphones: 16 kHz native, no resampling
    assert all(p.index != 5 for p in realtek + contoso)  # WDM-KS never used


def test_falls_back_to_windows_resampling_when_native_open_fails() -> None:
    """Falls back to Windows resampling when native open fails."""
    sd = FakeSD(fail={3})
    mic = MicInput(on_frames=lambda f: None, device_name="Microphone Array (Realtek(R) Audio)", sd_module=sd)
    status = mic.start()
    assert status.live
    assert sd.opened[0] == (3, 48000, 3840)  # native first: 3 x 1280 samples per block
    assert sd.opened[1][1] == 16000 and "Windows resamples" in status.capture


def test_saved_earphone_mic_opens_at_its_native_16k() -> None:
    """Saved earphone mic opens at its native 16k."""
    sd = FakeSD()
    status = MicInput(on_frames=lambda f: None, device_name="Headset (Contoso Wireless Earbuds)", sd_module=sd).start()
    assert status.live and status.device_name == "Headset (Contoso Wireless Earbuds)"
    assert sd.opened == [(4, 16000, 1280)]
    assert status.capture == "Windows WASAPI, 16000 Hz native"


def _tone(freq, rate, seconds=1.0, amp=10000):
    t = np.arange(int(rate * seconds)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.int16)


def test_decimator_keeps_speech_and_removes_aliasing() -> None:
    """48 kHz -> 16 kHz in 1280-sample output blocks: a 1 kHz tone passes, a
    12 kHz tone (which would alias to 4 kHz without the filter) is removed."""
    for freq, should_pass in ((1000, True), (12000, False)):
        dec = Decimator(3, 48000)
        signal = _tone(freq, 48000)
        out = np.concatenate([dec(signal[i : i + 3840]) for i in range(0, len(signal) - 3839, 3840)])
        assert len(out) == len(signal) // 3 // 1280 * 1280
        rms = float(np.sqrt(np.mean(out[2000:].astype(np.float64) ** 2)))
        if should_pass:
            assert rms > 6000  # ~7071 for a 10000-amplitude sine
        else:
            assert rms < 200


def test_decimated_frames_are_1280_int16() -> None:
    """Decimated frames are 1280 int16."""
    got = []
    sd = FakeSD()
    mic = MicInput(on_frames=got.append, device_name="Microphone Array (Realtek(R) Audio)", sd_module=sd)
    mic.start()
    mic._callback(np.ones((3840, 1), dtype=np.int16), 3840, None, None)
    assert got[0].shape == (1280,) and got[0].dtype == np.int16


# -- saving the choice by name -------------------------------------------------------------------


def test_save_string_setting_changes_only_that_line(tmp_path) -> None:
    """Save string setting changes only that line."""
    path = tmp_path / "settings.toml"
    path.write_text(
        '[audio]\n# the mic, by name\ninput_device_name = ""\nsample_rate = 16000\n\n[other]\ninput_device_name = "keep me"\n',
        encoding="utf-8",
    )
    save_string_setting("audio", "input_device_name", 'Headset (Contoso "Wireless" Earbuds)', path=path)
    text = path.read_text(encoding="utf-8")
    assert "# the mic, by name" in text and "sample_rate = 16000" in text
    loaded = load_settings(path)
    assert loaded["audio"]["input_device_name"] == 'Headset (Contoso "Wireless" Earbuds)'
    assert loaded["other"]["input_device_name"] == "keep me"


def test_save_string_setting_refuses_an_unknown_key(tmp_path) -> None:
    """Save string setting refuses an unknown key."""
    path = tmp_path / "settings.toml"
    path.write_text("[audio]\nsample_rate = 16000\n", encoding="utf-8")
    with pytest.raises(KeyError):
        save_string_setting("audio", "input_device_name", "x", path=path)


# -- wake-score debug log ------------------------------------------------------------------------


def test_wake_score_log_is_off_again() -> None:
    """Wake score log is off again."""
    # Developer: ON so wake_scores.log filled for tuning, OFF.
    assert SETTINGS["wake_word"]["debug_scores"] is False
    assert SETTINGS["wake_word"]["threshold"] == 0.5


def test_wake_score_log_writes_one_number_line_per_second(tmp_path, monkeypatch) -> None:
    """Wake score log writes one number line per second."""
    from jarvis.voice import wake_word

    class Model:
        def __init__(self):
            self.scores = iter([0.1] * 12 + [0.42] + [0.0] * 12)

        def predict(self, frame):
            return {"hey_jarvis": next(self.scores)}

        def reset(self):
            pass

    d = wake_word.WakeWordDetector(lambda s: None, model_factory=Model)
    d._debug_path = tmp_path / "wake_scores.log"
    d._frames_per_second = 12
    assert d.start()
    d.stop()
    for _ in range(24):
        d.process(np.zeros(1280, dtype=np.int16))
    lines = (tmp_path / "wake_scores.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("peak 0.100 threshold 0.50 rms 0") and "peak 0.420" in lines[1]
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d peak \d\.\d{3} threshold \d\.\d\d rms \d+", line) for line in lines)


# -- clean shutdown --------------------------------------------------------------------------------


def test_shutdown_stops_everything_even_if_one_step_fails() -> None:
    """Shutdown stops everything even if one step fails."""
    from types import SimpleNamespace

    from jarvis.dashboard.app import shutdown_app

    calls = []

    def step(name, fail=False):
        def run():
            calls.append(name)
            if fail:
                raise RuntimeError(name)
        return run

    app = SimpleNamespace(
        speech=SimpleNamespace(stop=step("speech.stop", fail=True), close=step("speech.close")),
        file_index=SimpleNamespace(stop=step("file_index.stop")),
        controller=SimpleNamespace(shutdown=step("controller.shutdown")),
    )
    voice = SimpleNamespace(close=step("voice.close"))
    shutdown_app(app, voice)
    assert calls == ["voice.close", "speech.stop", "speech.close", "file_index.stop", "controller.shutdown"]


def test_voice_close_releases_mic_hotkeys_threads_and_models() -> None:
    """Voice close releases mic hotkeys threads and models."""
    from jarvis.voice.system import VoiceSystem

    calls = []

    class Part:
        def __init__(self, name):
            self.name = name

        def __getattr__(self, attr):
            return lambda *a, **k: calls.append(f"{self.name}.{attr}")

    system = VoiceSystem(listener=Part("listener"), mic=Part("mic"), detector=Part("detector"), stt=Part("stt"), hotkeys=Part("hotkeys"))
    system.close()
    system.close()  # a second close is harmless
    assert calls == ["hotkeys.stop", "mic.stop", "listener.detach", "detector.stop", "detector.release", "stt.release"]
