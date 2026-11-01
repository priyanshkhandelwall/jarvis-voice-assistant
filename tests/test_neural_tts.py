"""Neural voices: Kokoro af_bella is her voice and Piper
is optional, both behind [speech] engine, with Stop, the echo guard and
markdown stripping working for each, and pyttsx3/Zira as the fallback when
a package or model is missing. Synthesis and playback are faked; nothing
is played aloud."""

from __future__ import annotations

import threading
import time
from typing import List

import numpy as np

from jarvis.settings import SETTINGS
from jarvis.speech import neural_tts
from jarvis.speech.neural_tts import KokoroSynth, NeuralSpeechOutput, PiperSynth, make_speech_output, speaking_chunks
from jarvis.speech.tts import VoiceStatus

_MESSAGES = SETTINGS["setup_messages"]


class FakeSynth:
    """Neural voice stand-in: 'synthesises' silence of a set length per sentence."""
    name = "Fake voice"

    def __init__(self, seconds_per_sentence=0.0, fail_load=False) -> None:
        self.sentences: List[str] = []
        self.delay = seconds_per_sentence
        self.fail_load = fail_load
        self.warmed_up = 0

    def load(self) -> None:
        """Fake load for the tests."""
        if self.fail_load:
            raise RuntimeError("model file damaged")

    def warm_up(self) -> None:
        """Fake warm up for the tests."""
        self.warmed_up += 1

    def synth(self, sentence: str):
        """Fake synth for the tests."""
        time.sleep(self.delay)
        self.sentences.append(sentence)
        return np.zeros(2400, dtype=np.float32), 24000  # 0.1 s of audio


class FakePlayer:
    """Plays in 10 ms blocks of real time, checking Stop between blocks, like the real one."""

    def __init__(self, seconds_per_sentence=0.1) -> None:
        self.played: List[int] = []
        self.seconds = seconds_per_sentence
        self.started = threading.Event()

    def __call__(self, samples, rate, should_stop) -> bool:
        self.started.set()
        end = time.monotonic() + self.seconds
        while time.monotonic() < end:
            if should_stop():
                return False
            time.sleep(0.01)
        self.played.append(len(samples))
        return True


def _out(synth=None, player=None, **kw):
    return NeuralSpeechOutput(synth or FakeSynth(), player=player or FakePlayer(), **kw)


def test_speak_blocks_until_every_sentence_is_played() -> None:
    """Speak blocks until every sentence is played."""
    synth, player = FakeSynth(), FakePlayer()
    out = _out(synth, player)
    out.speak("Good evening, Priyansh. I opened Visual Studio Code. The weather is fine.")
    assert synth.sentences == ["Good evening, Priyansh.", "I opened Visual Studio Code.", "The weather is fine."]
    assert len(player.played) == 3 and not out.is_speaking()


def test_markdown_is_stripped_before_speaking() -> None:
    """Markdown is stripped before speaking."""
    synth = FakeSynth()
    _out(synth).speak("**Note:** open `notepad` now.")
    assert all("*" not in s and "`" not in s for s in synth.sentences)


def test_stop_cuts_mid_sentence_and_drops_the_rest() -> None:
    """Stop cuts mid sentence and drops the rest."""
    synth, player = FakeSynth(), FakePlayer(seconds_per_sentence=2.0)
    out = _out(synth, player)
    t = threading.Thread(target=out.speak, args=("First sentence. Second sentence. Third sentence.",))
    t.start()
    assert player.started.wait(2)
    time.sleep(0.2)
    stopped_at = time.monotonic()
    out.stop()
    t.join(3)
    assert not t.is_alive()  # speak() returned
    assert out.last_cut_at is not None and out.last_cut_at - stopped_at < 0.2  # cut within a block
    assert player.played == []  # the first sentence was cut, the others never played
    assert not out.is_speaking()


def test_stop_never_waits_for_a_slow_synthesis() -> None:
    """Stop never waits for a slow synthesis."""
    # Real Kokoro takes seconds per sentence and can't be interrupted. Stop
    # took 3.7 s on the laptop while speak waited for it.
    synth, player = FakeSynth(seconds_per_sentence=0.8), FakePlayer(seconds_per_sentence=2.0)
    out = _out(synth, player)
    t = threading.Thread(target=out.speak, args=("First sentence. Second sentence. Third sentence.",))
    t.start()
    assert player.started.wait(3)
    time.sleep(0.1)  # "Second sentence." is being synthesised now
    stopped_at = time.monotonic()
    out.stop()
    t.join(3)
    assert time.monotonic() - stopped_at < 0.3 and not out.is_speaking()
    out.speak("Next line.")  # waits for the cut line's synthesiser, then speaks normally
    assert synth.sentences[-1] == "Next line." and "Third sentence." not in synth.sentences


def test_stop_between_sentences_while_the_next_is_still_being_made() -> None:
    """Stop between sentences while the next is still being made."""
    synth, player = FakeSynth(seconds_per_sentence=1.0), FakePlayer(seconds_per_sentence=0.05)
    out = _out(synth, player)
    t = threading.Thread(target=out.speak, args=("One. Two.",))
    t.start()
    while not player.played:  # "One." has finished; "Two." is still being made
        time.sleep(0.01)
    stopped_at = time.monotonic()
    out.stop()
    t.join(3)
    assert time.monotonic() - stopped_at < 0.3 and len(player.played) == 1


def test_stop_also_drops_queued_lines() -> None:
    """Stop also drops queued lines."""
    player = FakePlayer(seconds_per_sentence=1.0)
    out = _out(FakeSynth(), player)
    first = threading.Thread(target=out.speak, args=("One.",))
    second = threading.Thread(target=out.speak, args=("Two.",))
    first.start()
    assert player.started.wait(2)
    second.start()
    time.sleep(0.05)
    out.stop()
    first.join(2)
    second.join(2)
    assert player.played == []


def test_echo_guard_sees_her_speaking() -> None:
    """Echo guard sees her speaking."""
    player = FakePlayer(seconds_per_sentence=0.5)
    out = _out(FakeSynth(), player)
    t = threading.Thread(target=out.speak, args=("Hello there.",))
    t.start()
    assert player.started.wait(2)
    assert out.is_speaking()  # the voice listener ignores the microphone now
    t.join(3)
    assert not out.is_speaking()


def test_next_sentence_is_synthesised_while_this_one_plays() -> None:
    """Next sentence is synthesised while this one plays."""
    synth, player = FakeSynth(seconds_per_sentence=0.3), FakePlayer(seconds_per_sentence=0.3)
    out = _out(synth, player)
    started = time.monotonic()
    out.speak("One. Two. Three. Four.")
    took = time.monotonic() - started
    # Sequential would be 4 x (0.3 + 0.3) = 2.4 s; overlapped is about 0.3 + 4 x 0.3 = 1.5 s.
    assert took < 2.0


def test_a_model_that_fails_to_load_means_text_only_never_a_crash() -> None:
    """A model that fails to load means text only never a crash."""
    out = _out(FakeSynth(fail_load=True))
    out.speak("Hello.")
    assert out.status.ready is False and out.status.note == _MESSAGES["tts_engine_failed"]


class FakePyttsx3:
    """pyttsx3 engine stand-in."""
    def __init__(self):
        self.status = VoiceStatus(ready=True, voice_name="Microsoft Zira Desktop")


def test_her_voice_is_kokoro_af_bella_full_precision(monkeypatch) -> None:
    """Her voice is Kokoro af bella full precision."""
    # The developer's choice; full precision because int8 was ~3x slower here.
    cfg = SETTINGS["speech"]
    assert cfg["engine"] == "kokoro"
    assert cfg["kokoro"]["voice"] == "af_bella" and cfg["kokoro"]["model_file"] == "kokoro-v1.0.onnx"
    made = []
    monkeypatch.setattr(KokoroSynth, "missing", lambda self: None)
    monkeypatch.setattr(neural_tts, "NeuralSpeechOutput", lambda synth, cache=None: made.append(synth) or synth)
    monkeypatch.setattr(neural_tts, "Pyttsx3SpeechOutput", FakePyttsx3)
    make_speech_output()
    assert len(made) == 1 and isinstance(made[0], KokoroSynth) and made[0].voice == "af_bella"


def test_missing_kokoro_model_falls_back_to_zira_with_the_fix(tmp_path, monkeypatch) -> None:
    """Missing Kokoro model falls back to zira with the fix."""
    monkeypatch.setattr(neural_tts, "Pyttsx3SpeechOutput", FakePyttsx3)
    monkeypatch.setattr(neural_tts, "_PROJECT_ROOT", tmp_path)  # no models here
    out = make_speech_output("kokoro")
    assert isinstance(out, FakePyttsx3) and out.status.ready
    assert "Run python scripts\\download_models.py in the JARVIS folder, then restart JARVIS." in out.status.note
    assert "Windows voice" in out.status.note


def test_missing_kokoro_package_says_to_install_requirements(monkeypatch) -> None:
    """Missing Kokoro package says to install requirements."""
    monkeypatch.setattr(neural_tts, "Pyttsx3SpeechOutput", FakePyttsx3)
    monkeypatch.setattr(neural_tts, "_installed", lambda module: False)
    out = make_speech_output("kokoro")
    assert isinstance(out, FakePyttsx3)
    assert "pip install -r requirements.txt" in out.status.note and "Windows voice" in out.status.note


def test_missing_piper_voice_names_its_optional_download(tmp_path, monkeypatch) -> None:
    """Missing piper voice names its optional download."""
    monkeypatch.setattr(neural_tts, "Pyttsx3SpeechOutput", FakePyttsx3)
    monkeypatch.setattr(neural_tts, "_installed", lambda module: True)
    monkeypatch.setattr(neural_tts, "_PROJECT_ROOT", tmp_path)
    out = make_speech_output("piper")
    assert "Run python scripts\\download_models.py --voices piper in the JARVIS folder" in out.status.note


def test_package_check_does_not_import_anything() -> None:
    """Package check does not import anything."""
    assert neural_tts._installed("json") and not neural_tts._installed("no_such_package_jarvis")


def test_model_is_warmed_up_once_before_the_first_line() -> None:
    """Model is warmed up once before the first line."""
    synth = FakeSynth()
    out = _out(synth)
    out.speak("Hello.")
    assert synth.warmed_up == 1 and synth.sentences == ["Hello."]  # the warm-up is never heard


def test_time_to_first_audio_is_recorded() -> None:
    """Time to first audio is recorded."""
    out = _out(FakeSynth(seconds_per_sentence=0.2))
    out.speak("Good evening.")
    assert out.last_first_audio_seconds is not None and 0.15 < out.last_first_audio_seconds < 1.0


def test_a_long_first_sentence_starts_with_its_first_clause() -> None:
    """A long first sentence starts with its first clause."""
    text = ("Ottawa is the capital of Canada, located in Ontario on the Ottawa River. "
            "It became the capital in 1857. Parliament Hill is its best-known landmark.")
    assert speaking_chunks(text, max_chars=60, min_chars=12, tail_ratio=1.5) == [
        "Ottawa is the capital of Canada,",
        "located in Ontario on the Ottawa River.",
        "It became the capital in 1857.",
        "Parliament Hill is its best-known landmark.",
    ]


def test_short_first_sentences_and_later_sentences_are_never_cut() -> None:
    """Short first sentences and later sentences are never cut."""
    text = "Hello everyone. I am JARVIS, which stands for Just A Rather Very Intelligent System."
    assert speaking_chunks(text, max_chars=60, min_chars=12, tail_ratio=1.5) == [
        "Hello everyone.", "I am JARVIS, which stands for Just A Rather Very Intelligent System."]


def test_a_tiny_first_clause_is_not_split_off() -> None:
    """A tiny first clause is not split off."""
    # "Well," alone is shorter than min_chars, so the next break is used.
    text = "Well, that depends on the weather, the traffic and how early you leave."
    assert speaking_chunks(text, max_chars=40, min_chars=12, tail_ratio=1.5)[0] == "Well, that depends on the weather,"
    assert speaking_chunks("Yes, absolutely and without any doubt whatsoever my friend.", 20, 12, 1.5) == [
        "Yes, absolutely and without any doubt whatsoever my friend."]


def test_no_split_when_the_rest_would_not_be_ready_in_time() -> None:
    """No split when the rest would not be ready in time."""
    # A 32-character head plays for ~2 s; a 90-character tail takes ~3 s to make:
    # she would stop mid-sentence for a second, so the sentence stays whole.
    text = ("Ottawa is Canada's capital city, and it sits on the south bank of the Ottawa River "
            "in eastern Ontario near Quebec.")
    assert speaking_chunks(text, max_chars=60, min_chars=12, tail_ratio=1.5) == [text]


def test_a_later_break_is_used_when_the_first_leaves_too_long_a_tail() -> None:
    """A later break is used when the first leaves too long a tail."""
    text = "Sure, the capital of Australia is Canberra, which was chosen as a compromise."
    assert speaking_chunks(text, max_chars=60, min_chars=4, tail_ratio=1.5) == [
        "Sure, the capital of Australia is Canberra,", "which was chosen as a compromise."]


def test_every_word_of_the_intro_is_kept_when_chunking() -> None:
    """Every word of the intro is kept when chunking."""
    text = (neural_tts._PROJECT_ROOT / "jarvis" / "settings" / "intro.txt").read_text(encoding="utf-8").strip()
    assert " ".join(speaking_chunks(text)).split() == text.split()


def test_unknown_engine_says_which_values_work(monkeypatch) -> None:
    """Unknown engine says which values work."""
    monkeypatch.setattr(neural_tts, "Pyttsx3SpeechOutput", FakePyttsx3)
    out = make_speech_output("siri")
    assert "pyttsx3, kokoro or piper" in out.status.note


def test_real_model_files_are_found_when_downloaded() -> None:
    """Real model files are found when downloaded."""
    # Elsewhere (or Piper, whose voices were deleted) they may be missing, which is fine.
    for synth in (KokoroSynth(), PiperSynth()):
        note = synth.missing()
        assert note is None or "Run" in note
