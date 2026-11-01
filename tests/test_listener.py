"""Conversation mode: the voice listener (jarvis/voice/
listener.py) with the real controller and orchestrator. The microphone, the
wake-word model and Whisper are fakes: frames are fed in directly, nothing
touches a real device."""

from __future__ import annotations

import os
import time
from typing import List

import numpy as np
import pytest

from jarvis.controller import Controller, Event, EventType, State
from jarvis.controller.permits import OriginTag, PendingConfirmation
from jarvis.controller.trust import TrustLevel
from jarvis.interpreter import Answer, Interpreter
from jarvis.logbook import Logbook
from jarvis.orchestrator import Orchestrator
from jarvis.settings import SETTINGS
from jarvis.speech import SpeechOutput
from jarvis.voice.listener import SOURCE_HOTKEY, SOURCE_PUSH_TO_TALK, SOURCE_VOICE, SOURCE_WAKE_WORD, VoiceListener, fragment_reason
from jarvis.voice.stt import Transcript

_LOUD = np.full(1280, 3000, dtype=np.int16)
_QUIET = np.zeros(1280, dtype=np.int16)
_FRAMES_PER_SECOND = 16000 / 1280  # 12.5 frames of 80 ms per second


def _wait(pred, timeout=3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class FakeDetector:
    """Detector stand-in returning canned detections; counts its calls."""
    def __init__(self):
        self.frames = 0
        self.resets = 0

    def feed(self, frame):
        """Fake feed for the tests."""
        self.frames += 1

    def reset(self):
        """Fake reset for the tests."""
        self.resets += 1


class FakeSTT:
    """Speech-to-text stand-in returning a fixed transcript."""
    def __init__(self, text="open chrome", no_speech_prob=0.05):
        self.text = text
        self.no_speech_prob = no_speech_prob
        self.calls = 0

    def transcribe_detailed(self, audio):
        """Fake transcribe detailed for the tests."""
        self.calls += 1
        return Transcript(self.text, self.no_speech_prob)


class FakeSpeech(SpeechOutput):
    """Speech output stand-in: records what she would say; nothing is played."""
    def __init__(self):
        self.texts: List[str] = []
        self.speaking = False

    def speak(self, text):
        """Fake speak for the tests."""
        self.texts.append(text)

    def is_speaking(self):
        """Fake is speaking for the tests."""
        return self.speaking


class Clock:
    """A clock the test moves by hand (sleep() advances it instantly)."""
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def rig():
    """Fixture: the components this file's tests drive, with every side effect mocked."""
    made = []

    def _make(stt=None, start_state=State.AWAKE, handle=None, speech=None, clock=None):
        c = Controller(start_state=start_state)
        logbook = Logbook()
        c.set_transition_listener(logbook.on_transition)
        c.start()
        made.append(c)
        out = {"commands": [], "woke": [], "asleep": [], "ignored": [], "notes": []}
        handler = handle or (lambda text, source: out["commands"].append((text, source)))
        detector = FakeDetector()
        speech = speech or FakeSpeech()
        listener = VoiceListener(
            c,
            handler,
            detector=detector,
            stt=stt,
            notify=out["notes"].append,
            stt_missing_message="STT MISSING",
            wake=out["woke"].append,
            heard_while_asleep=out["asleep"].append,
            ignored=lambda text, reason: out["ignored"].append((text, reason)),
            is_speaking=speech.is_speaking,
            clock=clock or time.monotonic,
        )
        listener.attach()
        return c, listener, detector, out, logbook

    yield _make
    for c in made:
        c.shutdown()


def _utterance(listener, speech_frames=6, quiet_frames=None):
    quiet_frames = quiet_frames if quiet_frames is not None else int(SETTINGS["listen"]["end_silence_seconds"] * _FRAMES_PER_SECOND) + 2
    for _ in range(speech_frames):
        listener.on_frames(_LOUD)
    for _ in range(quiet_frames):
        listener.on_frames(_QUIET)


# -- SLEEPING -----------------------------------------------------------------------------


def test_asleep_speech_is_shown_but_never_acted_on_or_kept(rig, tmp_path, monkeypatch) -> None:
    """Asleep speech is shown but never acted on or kept."""
    monkeypatch.chdir(tmp_path)
    stt = FakeSTT("open chrome")
    c, listener, detector, out, logbook = rig(stt=stt, start_state=State.SLEEPING)
    _utterance(listener)
    assert _wait(lambda: out["asleep"] == ["open chrome"])
    assert out["commands"] == [] and out["woke"] == []  # nothing acted on
    assert c.state == State.SLEEPING
    assert os.listdir(tmp_path) == []  # nothing written to disk
    assert listener.levels() == []  # sleeping audio is never drawn


def test_asleep_frames_also_go_to_the_wake_word(rig) -> None:
    """Asleep frames also go to the wake word."""
    c, listener, detector, out, logbook = rig(stt=FakeSTT(), start_state=State.SLEEPING)
    for _ in range(10):
        listener.on_frames(_QUIET)
    assert detector.frames == 10


def test_wake_word_wakes_her(rig) -> None:
    """Wake word wakes her."""
    c, listener, detector, out, logbook = rig(stt=FakeSTT(), start_state=State.SLEEPING)
    listener.on_wake_word(0.93)
    assert out["woke"] == [SOURCE_WAKE_WORD]


def test_wake_word_while_awake_does_nothing(rig) -> None:
    """Wake word while awake does nothing."""
    c, listener, detector, out, logbook = rig(stt=FakeSTT())
    listener.on_wake_word(0.93)
    assert out["woke"] == []


def test_awake_frames_never_go_to_the_wake_word(rig) -> None:
    """Awake frames never go to the wake word."""
    c, listener, detector, out, logbook = rig(stt=FakeSTT())
    for _ in range(5):
        listener.on_frames(_QUIET)
    assert detector.frames == 0


@pytest.mark.parametrize("said", ["Wake up, JARVIS.", "wake up jarvis", "Hey JARVIS, are you there?", "JARVIS, wake up!"])
def test_spoken_wake_phrase_wakes_her(rig, said) -> None:
    """Spoken wake phrase wakes her."""
    c, listener, detector, out, logbook = rig(stt=FakeSTT(said), start_state=State.SLEEPING)
    _utterance(listener)
    assert _wait(lambda: out["woke"] == ["wake phrase"])
    assert out["asleep"] == []


def test_press_to_talk_while_asleep_wakes_her(rig) -> None:
    """Press to talk while asleep wakes her."""
    c, listener, detector, out, logbook = rig(stt=FakeSTT(), start_state=State.SLEEPING)
    listener.push_to_talk()
    assert out["woke"] == [SOURCE_HOTKEY]


# -- AWAKE: every utterance is a command ------------------------------------------------------


def test_awake_utterance_becomes_a_command_without_a_wake_word(rig) -> None:
    """Awake utterance becomes a command without a wake word."""
    c, listener, detector, out, logbook = rig(stt=FakeSTT("open chrome"))
    _utterance(listener)
    assert _wait(lambda: out["commands"] == [("open chrome", SOURCE_VOICE)])
    assert c.state == State.LISTENING  # the fake handler doesn't run the command


def test_levels_exist_only_while_recording_a_command(rig) -> None:
    """Levels exist only while recording a command."""
    c, listener, detector, out, logbook = rig(stt=FakeSTT())
    listener.on_frames(_LOUD)
    assert listener.levels() and max(listener.levels()) > 0.5
    _utterance(listener, speech_frames=1)
    assert _wait(lambda: listener._recording is None)
    assert listener.levels() == []


@pytest.mark.parametrize(
    "text, prob, reason",
    [("Banana.", 0.05, "too short"), ("open chrome", 0.9, "unclear"), ("", 0.0, "nothing understood")],
)
def test_fragments_are_ignored_and_never_sent(rig, text, prob, reason) -> None:
    """Fragments are ignored and never sent."""
    c, listener, detector, out, logbook = rig(stt=FakeSTT(text, prob))
    _utterance(listener)
    assert _wait(lambda: c.state == State.AWAKE and listener._recording is None and listener.transcriptions == 1)
    time.sleep(0.05)
    assert out["commands"] == []
    if text:
        assert out["ignored"] == [(text, reason)]
    assert c.last_transition().event_type == EventType.LISTEN_TIMEOUT


@pytest.mark.parametrize("word", ["Confirm.", "cancel", "Goodnight.", "hello", "Thanks!"])
def test_known_one_word_commands_are_not_fragments(word) -> None:
    """Known one word commands are not fragments."""
    assert fragment_reason(word, 0.05) is None


def test_spoken_stop_is_a_top_priority_stop(rig) -> None:
    """Spoken stop is a top priority stop."""
    c, listener, detector, out, logbook = rig(stt=FakeSTT("Stop."))
    _utterance(listener)
    assert _wait(lambda: c.is_stop_requested() and c.state == State.AWAKE)
    assert out["commands"] == []


def test_no_speech_after_push_to_talk_times_out(rig) -> None:
    """No speech after push to talk times out."""
    stt = FakeSTT()
    c, listener, detector, out, logbook = rig(stt=stt)
    listener.push_to_talk()
    assert _wait(lambda: c.state == State.LISTENING)
    for _ in range(int(SETTINGS["listen"]["no_speech_timeout_seconds"] * _FRAMES_PER_SECOND) + 2):
        listener.on_frames(_QUIET)
    assert _wait(lambda: c.state == State.AWAKE)
    assert stt.calls == 0 and out["commands"] == []


def test_push_to_talk_while_awake_opens_one_command(rig) -> None:
    """Push to talk while awake opens one command."""
    c, listener, detector, out, logbook = rig(stt=FakeSTT("what time is it"))
    listener.push_to_talk()
    assert _wait(lambda: c.state == State.LISTENING and listener._recording is not None)
    _utterance(listener)
    assert _wait(lambda: out["commands"] == [("what time is it", SOURCE_PUSH_TO_TALK)])


def test_push_to_talk_during_speaking_is_rejected(rig) -> None:
    """Push to talk during speaking is rejected."""
    c, listener, detector, out, logbook = rig(stt=FakeSTT(), start_state=State.SPEAKING)
    listener.push_to_talk()
    assert _wait(lambda: c.transition_count() >= 1)
    assert c.state == State.SPEAKING and c.last_transition().accepted is False


def test_push_to_talk_without_speech_to_text_says_what_to_set_up(rig) -> None:
    """Push to talk without speech to text says what to set up."""
    c, listener, detector, out, logbook = rig(stt=None)
    listener.push_to_talk()
    assert out["notes"] == ["STT MISSING"]
    assert c.transition_count() == 0


def test_recording_stops_at_max_command_length(rig) -> None:
    """Recording stops at max command length."""
    c, listener, detector, out, logbook = rig(stt=FakeSTT("a very long command"))
    for _ in range(int(SETTINGS["listen"]["max_command_seconds"] * _FRAMES_PER_SECOND) + 2):
        listener.on_frames(_LOUD)  # never goes quiet
    assert _wait(lambda: out["commands"] == [("a very long command", SOURCE_VOICE)])


def test_stop_while_recording_drops_the_audio(rig) -> None:
    """Stop while recording drops the audio."""
    stt = FakeSTT()
    c, listener, detector, out, logbook = rig(stt=stt)
    listener.on_frames(_LOUD)
    assert _wait(lambda: c.state == State.LISTENING)
    c.request_stop("test")
    assert _wait(lambda: c.state == State.AWAKE and listener._recording is None)
    time.sleep(0.3)
    assert stt.calls == 0 and out["commands"] == []


def test_voice_command_runs_through_the_orchestrator(rig) -> None:
    """Voice command runs through the orchestrator."""
    holder = {}
    c, listener, detector, out, logbook = rig(stt=FakeSTT("Introduce yourself."), handle=lambda t, s: holder["orch"].handle_command(t, s))
    interpreter = Interpreter()
    interpreter.register("intro", lambda t: Answer("Hello everyone.") if t == "introduce yourself" else None)
    speech = FakeSpeech()
    holder["orch"] = Orchestrator(c, interpreter, speech, logbook=logbook)
    _utterance(listener)
    assert _wait(lambda: speech.texts == ["Hello everyone."])
    assert _wait(lambda: c.state == State.AWAKE)
    assert logbook.last_completed().heard == "Introduce yourself"


# -- CONFIRMING: spoken confirm / cancel (approved table change) ---------------------------------


def _pending(c):
    with c._state_lock:
        c._pending_confirmation = PendingConfirmation.issue(
            action_name="lock_screen",
            params={"text": "hi"},
            origin=OriginTag.DEVELOPER_REQUESTED,
            trust_level=TrustLevel.L2,
            timeout_seconds=60,
            confirmations_required=1,
        )


@pytest.mark.parametrize("word", ["Confirm.", "Cancel."])
def test_spoken_confirm_or_cancel_is_handed_over_while_confirming(rig, word) -> None:
    """Spoken confirm or cancel is handed over while confirming."""
    c, listener, detector, out, logbook = rig(stt=FakeSTT(word), start_state=State.CONFIRMING)
    _utterance(listener)
    assert _wait(lambda: out["commands"] == [(word, SOURCE_VOICE)])
    assert c.state == State.CONFIRMING  # the listener never enters LISTENING here


def test_other_speech_while_confirming_is_ignored(rig) -> None:
    """Other speech while confirming is ignored."""
    c, listener, detector, out, logbook = rig(stt=FakeSTT("what's the weather"), start_state=State.CONFIRMING)
    _utterance(listener)
    assert _wait(lambda: out["ignored"] == [("what's the weather", "waiting for confirm or cancel")])
    assert out["commands"] == []


def test_spoken_cancel_drops_the_pending_action(rig) -> None:
    """Spoken cancel drops the pending action."""
    holder = {}
    c, listener, detector, out, logbook = rig(stt=FakeSTT("Cancel."), start_state=State.CONFIRMING, handle=lambda t, s: holder["orch"].handle_command(t, s))
    _pending(c)
    speech = FakeSpeech()
    holder["orch"] = Orchestrator(c, Interpreter(), speech, logbook=logbook)
    _utterance(listener)
    assert _wait(lambda: speech.texts == [SETTINGS["conversation"]["cancelled_reply"]])
    assert _wait(lambda: c.state == State.AWAKE)
    assert c.pending_confirmation() is None


# -- Self-echo guard ---------------------------------------------------------------------------------


def test_microphone_is_ignored_while_she_speaks_and_for_the_tail(rig) -> None:
    """Microphone is ignored while she speaks and for the tail."""
    clock = Clock()
    speech = FakeSpeech()
    stt = FakeSTT("open chrome")
    c, listener, detector, out, logbook = rig(stt=stt, speech=speech, clock=clock)
    speech.speaking = True
    for _ in range(20):
        listener.on_frames(_LOUD)  # her own voice
    speech.speaking = False
    clock.now += SETTINGS["speech"]["echo_tail_seconds"] - 0.05  # still inside the tail
    for _ in range(20):
        listener.on_frames(_LOUD)
    time.sleep(0.2)
    assert listener._recording is None and stt.calls == 0 and out["commands"] == []
    assert c.state == State.AWAKE


def test_after_the_tail_she_listens_again_and_the_wake_model_is_reset(rig) -> None:
    """After the tail she listens again and the wake model is reset."""
    clock = Clock()
    speech = FakeSpeech()
    c, listener, detector, out, logbook = rig(stt=FakeSTT("open chrome"), speech=speech, clock=clock)
    speech.speaking = True
    listener.on_frames(_LOUD)
    speech.speaking = False
    clock.now += SETTINGS["speech"]["echo_tail_seconds"] + 0.01
    _utterance(listener)
    assert _wait(lambda: out["commands"] == [("open chrome", SOURCE_VOICE)])
    assert detector.resets == 1


def test_echo_tail_default_is_raised_to_0_6() -> None:
    """Echo tail default is raised to 0 6."""
    assert SETTINGS["speech"]["echo_tail_seconds"] == 0.6


def test_speaking_state_alone_mutes_the_microphone(rig) -> None:
    """Speaking state alone mutes the microphone."""
    c, listener, detector, out, logbook = rig(stt=FakeSTT(), start_state=State.SPEAKING)
    listener.on_frames(_LOUD)
    assert listener._recording is None and c.transition_count() == 0


def test_confirm_during_speaking_is_rejected() -> None:
    """Section 5: a "confirm" arriving while she is SPEAKING never counts."""
    c = Controller(start_state=State.SPEAKING)
    c.start()
    try:
        c.submit(Event(EventType.CONFIRM_RECEIVED, {"action_name": "lock_screen", "params": {}}))
        assert _wait(lambda: c.transition_count() >= 1)
        assert c.last_transition().accepted is False and c.state == State.SPEAKING
    finally:
        c.shutdown()


def test_her_own_voice_mid_command_ends_listening(rig) -> None:
    """A notice spoken while a command
    was being recorded cancelled the recording but left her LISTENING --
    deaf -- for five minutes, until Stop."""
    speech = FakeSpeech()
    c, listener, detector, out, logbook = rig(stt=FakeSTT(), speech=speech)
    listener.on_frames(_LOUD)
    assert _wait(lambda: c.state == State.LISTENING)
    speech.speaking = True  # a notice starts while he is still talking
    listener.on_frames(_LOUD)
    assert _wait(lambda: c.state == State.AWAKE)
    assert out["commands"] == []


def test_a_notice_waits_until_she_is_idle() -> None:
    """A notice waits until she is idle."""
    from jarvis.dashboard.app import wait_until_idle

    c = Controller(start_state=State.LISTENING)
    assert wait_until_idle(c, timeout=0.2, poll=0.05) is False
    c2 = Controller(start_state=State.AWAKE)
    assert wait_until_idle(c2, timeout=0.2) is True
