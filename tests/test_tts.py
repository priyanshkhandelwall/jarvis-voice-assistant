"""F2 text-to-speech (jarvis/speech/tts.py), with the pyttsx3 engine faked:
voice chosen by name, English fallback with a setup note, text-only when
speech can't start, and speak() never blocking forever."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import List

from jarvis.settings import SETTINGS
from jarvis.speech.tts import Pyttsx3SpeechOutput, choose_voice

_MESSAGES = SETTINGS["setup_messages"]


@dataclass
class FakeVoice:
    """Voice stand-in for the dashboard (levels, microphone switch, close)."""
    id: str
    name: str
    languages: List[str] = field(default_factory=list)


ZIRA = FakeVoice("zira-id", "Microsoft Zira Desktop", ["en-US"])
DAVID = FakeVoice("david-id", "Microsoft David Desktop", ["en-US"])
HAZEL = FakeVoice("hazel-id", "Microsoft Hazel Desktop", ["en-GB"])
HORTENSE = FakeVoice("hortense-id", "Microsoft Hortense Desktop", ["fr-FR"])


class FakeEngine:
    """Mimics pyttsx3's external-loop API: say() queues, iterate() pumps,
    and the finished-utterance callback fires when the utterance ends --
    unless `block` is given and not yet set (an engine that hangs)."""

    def __init__(self, voices: List[FakeVoice], block: threading.Event | None = None) -> None:
        self.voices = voices
        self.props: dict = {}
        self.said: List[str] = []
        self.threads: set = set()
        self.loop_started = False
        self._block = block
        self._callbacks: dict = {}
        self._pending = False

    def getProperty(self, name):
        """Fake getproperty for the tests."""
        return self.voices if name == "voices" else self.props.get(name)

    def setProperty(self, name, value):
        """Fake setproperty for the tests."""
        self.props[name] = value

    def connect(self, topic, callback):
        """Fake connect for the tests."""
        self._callbacks[topic] = callback

    def startLoop(self, use_driver_loop=True):
        """Fake startloop for the tests."""
        assert use_driver_loop is False  # the external loop, never runAndWait's driver loop
        self.loop_started = True

    def say(self, text):
        """Fake say for the tests."""
        self.threads.add(threading.get_ident())
        self.said.append(text)
        self._pending = True

    def iterate(self):
        """Fake iterate for the tests."""
        self.threads.add(threading.get_ident())
        if self._pending and (self._block is None or self._block.is_set()):
            self._pending = False
            self._callbacks["finished-utterance"](name=None, completed=True)


def _tts(engine_or_exc, **kwargs) -> Pyttsx3SpeechOutput:
    def factory():
        if isinstance(engine_or_exc, Exception):
            raise engine_or_exc
        return engine_or_exc

    return Pyttsx3SpeechOutput(engine_factory=factory, startup_timeout_seconds=2, **kwargs)


def test_zira_is_chosen_by_exact_name() -> None:
    """Zira is chosen by exact name."""
    engine = FakeEngine([DAVID, ZIRA])
    tts = _tts(engine)
    assert tts.status.ready and tts.status.voice_name == "Microsoft Zira Desktop" and tts.status.note is None
    assert engine.props["voice"] == "zira-id"


def test_missing_zira_falls_back_to_first_english_voice_with_note() -> None:
    """Missing zira falls back to first english voice with note."""
    engine = FakeEngine([HORTENSE, HAZEL, DAVID])
    tts = _tts(engine)
    assert tts.status.ready and tts.status.voice_name == "Microsoft Hazel Desktop"
    assert tts.status.note == _MESSAGES["tts_zira_missing"].format(voice="Microsoft Hazel Desktop")
    assert "Add voices" in tts.status.note


def test_no_english_voice_goes_text_only_with_setup_note() -> None:
    """No english voice goes text only with setup note."""
    tts = _tts(FakeEngine([HORTENSE]))
    assert not tts.status.ready
    assert tts.status.note == _MESSAGES["tts_no_english_voice"]
    tts.speak("hello")  # must not raise or block


def test_missing_pyttsx3_says_how_to_install() -> None:
    """Missing pyttsx3 says how to install."""
    tts = _tts(ImportError("No module named 'pyttsx3'"))
    assert not tts.status.ready
    assert "pip install -r requirements.txt" in tts.status.note


def test_engine_start_failure_says_what_to_check() -> None:
    """Engine start failure says what to check."""
    tts = _tts(RuntimeError("SAPI broke"))
    assert not tts.status.ready and tts.status.note == _MESSAGES["tts_engine_failed"]


def test_speak_waits_for_the_engine_and_runs_on_one_worker_thread() -> None:
    """Speak waits for the engine and runs on one worker thread."""
    engine = FakeEngine([ZIRA])
    tts = _tts(engine)
    tts.speak("first")
    tts.speak("second")
    assert engine.said == ["first", "second"]
    assert len(engine.threads) == 1 and threading.get_ident() not in engine.threads


def test_speak_returns_after_its_timeout_if_the_engine_hangs() -> None:
    """Speak returns after its timeout if the engine hangs."""
    hang = threading.Event()
    tts = _tts(FakeEngine([ZIRA], block=hang), speak_timeout_seconds=0.2)
    done = threading.Event()
    threading.Thread(target=lambda: (tts.speak("stuck"), done.set()), daemon=True).start()
    assert done.wait(2), "speak() must give up after speak_timeout_seconds"
    hang.set()


def test_blank_text_is_not_spoken() -> None:
    """Blank text is not spoken."""
    engine = FakeEngine([ZIRA])
    tts = _tts(engine)
    tts.speak("   ")
    assert engine.said == []


def test_windows_style_voice_name_still_counts_as_zira() -> None:
    """Windows reports "Microsoft Zira Desktop - English (United States)"
    (seen on this laptop) -- that must count as Zira, not fall
    back to David."""
    real_zira = FakeVoice("zira-id", "Microsoft Zira Desktop - English (United States)", ["en-US"])
    real_david = FakeVoice("david-id", "Microsoft David Desktop - English (United States)", ["en-US"])
    engine = FakeEngine([real_david, real_zira])
    tts = _tts(engine)
    assert engine.props["voice"] == "zira-id" and tts.status.note is None


def test_uses_external_loop_not_run_and_wait() -> None:
    """Uses external loop not run and wait."""
    engine = FakeEngine([ZIRA])
    _tts(engine).speak("hello")
    assert engine.loop_started and engine.said == ["hello"]


def test_choose_voice_prefers_exact_name_then_language() -> None:
    """Choose voice prefers exact name then language."""
    assert choose_voice([HAZEL, ZIRA], "Microsoft Zira Desktop", "en") == (ZIRA, True)
    assert choose_voice([HORTENSE, HAZEL], "Microsoft Zira Desktop", "en") == (HAZEL, False)
    assert choose_voice([HORTENSE], "Microsoft Zira Desktop", "en") == (None, False)


# -- Stop cuts speech (R8, F2 Stage 3) --------------------------------------------


class StoppableEngine(FakeEngine):
    """An utterance that keeps playing until stop() is called on it; records
    which thread called stop() (COM objects belong to their own thread)."""

    def __init__(self, voices):
        super().__init__(voices, block=threading.Event())
        self.stop_threads: list = []
        self.speaking = threading.Event()

    def say(self, text):
        """Fake say for the tests."""
        super().say(text)
        self.speaking.set()

    def stop(self):
        """Fake stop for the tests."""
        self.stop_threads.append(threading.get_ident())
        self._pending = False
        self._callbacks["finished-utterance"](name=None, completed=False)


def _speak_in_background(tts, text):
    t = threading.Thread(target=tts.speak, args=(text,), daemon=True)
    t.start()
    return t


def test_stop_cuts_the_current_utterance_on_the_speech_thread() -> None:
    """Stop cuts the current utterance on the speech thread."""
    engine = StoppableEngine([ZIRA])
    tts = _tts(engine, stop_method="engine_stop")
    t = _speak_in_background(tts, "A very long sentence that would play for ages.")
    assert engine.speaking.wait(2)
    import time as _time

    _time.sleep(0.05)
    tts.stop()
    t.join(2)
    assert not t.is_alive()  # speak() returned promptly
    assert len(engine.stop_threads) == 1
    assert engine.stop_threads[0] == tts._thread.ident  # called on the engine's own thread, not the caller's
    assert tts.last_cut_at is not None


def test_stop_discards_queued_utterances() -> None:
    """Stop discards queued utterances."""
    engine = StoppableEngine([ZIRA])
    tts = _tts(engine, stop_method="engine_stop")
    first = _speak_in_background(tts, "First, playing now.")
    assert engine.speaking.wait(2)
    queued = [_speak_in_background(tts, f"Queued {i}.") for i in range(3)]
    import time as _time

    _time.sleep(0.1)
    tts.stop()
    for t in [first, *queued]:
        t.join(2)
        assert not t.is_alive()
    assert engine.said == ["First, playing now."]  # the queued ones were never said


def test_stop_when_idle_does_not_cut_the_next_utterance() -> None:
    """Stop when idle does not cut the next utterance."""
    engine = FakeEngine([ZIRA])
    tts = _tts(engine, stop_method="engine_stop")
    tts.stop()  # nothing playing
    tts.speak("Hello there.")
    assert engine.said == ["Hello there."]
    assert tts.last_cut_at is None


def test_sentence_fallback_stops_between_sentences() -> None:
    """Sentence fallback stops between sentences."""
    engine = FakeEngine([ZIRA])
    tts = _tts(engine, stop_method="sentence")
    original_say = engine.say

    def say_and_stop_after_first(text):
        original_say(text)
        if len(engine.said) == 1:
            tts.stop()  # Stop arrives while sentence one is playing

    engine.say = say_and_stop_after_first
    tts.speak("Sentence one. Sentence two. Sentence three.")
    assert engine.said == ["Sentence one."]  # no further sentences after the flag


def test_split_sentences() -> None:
    """Split sentences."""
    from jarvis.speech.tts import split_sentences

    assert split_sentences("One. Two! Three? Four") == ["One.", "Two!", "Three?", "Four"]
    assert split_sentences("  ") == []


def test_active_stop_method_is_the_one_the_laptop_test_chose() -> None:
    """Active stop method is the one the laptop test chose."""
    assert SETTINGS["speech"]["stop_method"] == "engine_stop"
    assert SETTINGS["speech"]["stop_settle_seconds"] > 0


def test_null_speech_output_has_a_harmless_stop() -> None:
    """Null speech output has a harmless stop."""
    from jarvis.speech import NullSpeechOutput

    NullSpeechOutput().stop()
