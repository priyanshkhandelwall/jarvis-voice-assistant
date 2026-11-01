"""Voice cache: her fixed lines are pre-rendered in the
background and played from jarvis_data/voice_cache/; changed text, voice or
model renders again; a missing or broken file means live synthesis; Stop and
the echo guard work as for live speech. Synthesis and playback are faked."""

from __future__ import annotations

import threading
import time
from typing import List

import numpy as np

from jarvis.actions.intro import load_intro_text
from jarvis.settings import SETTINGS
from jarvis.speech import neural_tts
from jarvis.speech.neural_tts import KokoroSynth, NeuralSpeechOutput, make_speech_output
from jarvis.speech.voice_cache import VoiceCache, fixed_lines, read_wav, sentences_of, write_wav

NAME = SETTINGS["user"]["name"]
RUDE = SETTINGS["rudeness"]["reply_text"]
CHECK_IN = SETTINGS["mood"]["check_in_pool"][0]
SIG = {"engine": "kokoro", "model": "kokoro-v1.0.onnx", "voice": "af_bella", "speed": 1.0, "language": "en-us"}


class FakeSynth:
    """Neural voice stand-in: 'synthesises' silence of a set length per sentence."""
    name = "Fake voice"

    def __init__(self, seconds_per_sentence=0.0) -> None:
        self.sentences: List[str] = []
        self.delay = seconds_per_sentence

    def load(self) -> None:
        """Fake load for the tests."""
        pass

    def synth(self, sentence: str):
        """Fake synth for the tests."""
        time.sleep(self.delay)
        self.sentences.append(sentence)
        return np.full(2400, 0.1, dtype=np.float32), 24000  # 0.1 s of audio


class FakePlayer:
    """Audio player stand-in: 'plays' for a set time per sentence and honours Stop."""
    def __init__(self, seconds=0.05) -> None:
        self.played: List[int] = []
        self.seconds = seconds
        self.started = threading.Event()
        self.speaking_seen: List[bool] = []
        self.out = None

    def __call__(self, samples, rate, should_stop) -> bool:
        self.started.set()
        end = time.monotonic() + self.seconds
        while time.monotonic() < end:
            if self.out is not None:
                self.speaking_seen.append(self.out.is_speaking())
            if should_stop():
                return False
            time.sleep(0.01)
        self.played.append(len(samples))
        return True


def _setup(tmp_path, lines, synth=None, player=None):
    synth, player = synth or FakeSynth(), player or FakePlayer()
    cache = VoiceCache(SIG, folder=tmp_path, lines=lambda: lines)
    out = NeuralSpeechOutput(synth, player=player, cache=cache)
    player.out = out
    out.wait_until_loaded(5)
    return out, synth, player, cache


def _render(out) -> None:
    out.render_cache().join(10)


# -- which lines ------------------------------------------------------------------


def test_every_fixed_line_the_developer_listed_is_cached() -> None:
    """Every fixed line the developer listed is cached."""
    lines = fixed_lines()
    keys = {sentences_of(line) for line in lines}
    assert sentences_of(load_intro_text()) in keys  # the intro, whole
    for pool in SETTINGS["greeting"]["pools"].values():
        for line in pool:
            assert sentences_of(line.format(name=NAME)) in keys
    for line in ("Goodnight.", "I'm already awake.", RUDE, SETTINGS["rudeness"]["reply_text_nothing_asked"]):
        assert sentences_of(line) in keys
    for line in SETTINGS["mood"]["check_in_pool"]:
        assert sentences_of(line) in keys
    plain_messages = [m for m in SETTINGS["setup_messages"].values() if isinstance(m, str) and "{" not in m]
    assert plain_messages and all(sentences_of(m) in keys for m in plain_messages)
    assert not any("{" in line for line in lines)  # nothing with a blank still to fill in
    assert len(keys) == len(lines)  # no line twice


def test_the_key_changes_with_text_voice_or_model(tmp_path) -> None:
    """The key changes with text voice or model."""
    cache = VoiceCache(SIG, folder=tmp_path)
    same = VoiceCache(dict(SIG), folder=tmp_path)
    assert cache.path_for(("Goodnight.",)) == same.path_for(("Goodnight.",))
    assert cache.path_for(("Goodnight.",)) != cache.path_for(("Good night.",))
    assert cache.path_for(("Goodnight.",)) != VoiceCache({**SIG, "voice": "af_heart"}, folder=tmp_path).path_for(("Goodnight.",))
    assert cache.path_for(("Goodnight.",)) != VoiceCache({**SIG, "model": "kokoro-v1.0.int8.onnx"}, folder=tmp_path).path_for(("Goodnight.",))


def test_wav_round_trip(tmp_path) -> None:
    """Wav round trip."""
    samples = np.sin(np.linspace(0, 20, 2400)).astype(np.float32) * 0.5
    write_wav(tmp_path / "a.wav", samples, 24000)
    back, rate = read_wav(tmp_path / "a.wav")
    assert rate == 24000 and np.allclose(back, samples, atol=1e-4)
    assert not list(tmp_path.glob("*.part"))


# -- rendering --------------------------------------------------------------------


def test_render_makes_one_file_per_line_then_plays_it_without_synthesis(tmp_path) -> None:
    """Render makes one file per line then plays it without synthesis."""
    out, synth, player, cache = _setup(tmp_path, ["Goodnight.", "Hello there. How can I help?"])
    _render(out)
    assert len(list(tmp_path.glob("*.wav"))) == 2
    assert synth.sentences == ["Goodnight.", "Hello there.", "How can I help?"]  # one sentence at a time
    synth.sentences.clear()
    out.speak("Hello there. How can I help?")
    assert synth.sentences == [] and len(player.played) == 1  # one file, no gap between the sentences


def test_changed_text_renders_again_and_the_old_file_goes(tmp_path) -> None:
    """Changed text renders again and the old file goes."""
    lines = ["Goodnight."]
    out, synth, _player, cache = _setup(tmp_path, lines)
    _render(out)
    old = cache.path_for(("Goodnight.",))
    lines[0] = "Sleep well."
    _render(out)
    assert not old.exists() and cache.path_for(("Sleep well.",)).exists()
    assert synth.sentences == ["Goodnight.", "Sleep well."]
    _render(out)
    assert synth.sentences == ["Goodnight.", "Sleep well."]  # nothing new to render


def test_rendering_waits_while_she_is_speaking(tmp_path) -> None:
    """Rendering waits while she is speaking."""
    synth, player = FakeSynth(), FakePlayer(seconds=0.6)
    out, synth, player, cache = _setup(tmp_path, ["Goodnight."], synth, player)
    speaker = threading.Thread(target=out.speak, args=("Something live.",))
    speaker.start()
    assert player.started.wait(2)
    out.render_cache()
    time.sleep(0.3)
    assert synth.sentences == ["Something live."]  # not rendering while she talks
    speaker.join(3)
    out.render_cache().join(5)
    assert cache.path_for(("Goodnight.",)).exists()


# -- playing ---------------------------------------------------------------------


def test_cached_line_starts_at_once_live_one_waits_for_synthesis(tmp_path) -> None:
    """Cached line starts at once live one waits for synthesis."""
    out, synth, _player, _cache = _setup(tmp_path, ["Goodnight."], FakeSynth(seconds_per_sentence=0.0))
    _render(out)
    synth.delay = 0.5
    out.speak("Goodnight.")
    cached = out.last_first_audio_seconds
    out.speak("Not cached.")
    live = out.last_first_audio_seconds
    assert cached < 0.2 and live >= 0.5


def test_missing_file_means_live_synthesis(tmp_path) -> None:
    """Missing file means live synthesis."""
    out, synth, player, cache = _setup(tmp_path, ["Goodnight."])
    out.speak("Goodnight.")  # never rendered
    assert synth.sentences == ["Goodnight."] and len(player.played) == 1


def test_broken_file_means_live_synthesis_never_silence(tmp_path) -> None:
    """Broken file means live synthesis never silence."""
    out, synth, player, cache = _setup(tmp_path, ["Goodnight."])
    _render(out)
    cache.path_for(("Goodnight.",)).write_bytes(b"not a wav")
    synth.sentences.clear()
    out.speak("Goodnight.")
    assert synth.sentences == ["Goodnight."] and len(player.played) == 1


def test_rude_prefix_and_check_in_play_cached_around_a_live_answer(tmp_path) -> None:
    """Rude prefix and check in play cached around a live answer."""
    out, synth, player, cache = _setup(tmp_path, [RUDE, CHECK_IN])
    _render(out)
    synth.sentences.clear()
    line = f"{RUDE} Ottawa is the capital of Canada. {CHECK_IN}"
    kinds = [kind for kind, _path, _text in cache.split(line)]
    assert kinds == ["cached", "live", "cached"]
    out.speak(line)
    assert synth.sentences == ["Ottawa is the capital of Canada."] and len(player.played) == 3


def test_the_longest_cached_run_wins(tmp_path) -> None:
    """The longest cached run wins."""
    out, _synth, _player, cache = _setup(tmp_path, ["One.", "One. Two. Three."])
    _render(out)
    assert [(k, t) for k, _p, t in cache.split("One. Two. Three. Four.")] == [
        ("cached", "One. Two. Three."), ("live", "Four.")]


def test_stop_cuts_a_cached_line_at_once_and_the_echo_guard_sees_it(tmp_path) -> None:
    """Stop cuts a cached line at once and the echo guard sees it."""
    player = FakePlayer(seconds=2.0)
    out, synth, player, cache = _setup(tmp_path, ["Goodnight."], player=player)
    _render(out)
    player.started.clear()
    speaker = threading.Thread(target=out.speak, args=("Goodnight.",))
    speaker.start()
    assert player.started.wait(2)
    time.sleep(0.2)
    assert out.is_speaking()  # the voice listener ignores the microphone
    stopped = time.monotonic()
    out.stop()
    speaker.join(2)
    assert time.monotonic() - stopped < 0.2 and not out.is_speaking()
    assert player.played == [] and all(player.speaking_seen)


def test_her_voice_gets_a_cache_keyed_by_voice_and_model(monkeypatch) -> None:
    """Her voice gets a cache keyed by voice and model."""
    made = {}
    monkeypatch.setattr(KokoroSynth, "missing", lambda self: None)
    monkeypatch.setattr(neural_tts, "NeuralSpeechOutput", lambda synth, cache=None: made.update(cache=cache) or synth)
    make_speech_output("kokoro")
    assert isinstance(made["cache"], VoiceCache)
    assert made["cache"].folder.name == "voice_cache" and made["cache"].folder.parent.name == "jarvis_data"
    assert made["cache"]._signature["voice"] == "af_bella" and made["cache"]._signature["model"] == "kokoro-v1.0.onnx"
