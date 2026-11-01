"""Two fixes.

1. While she waits for confirm or cancel, anything else she hears
   ("Console", "can find") gets "Say confirm or cancel." (cached audio), at
   most once per [conversation] confirm_reminder_seconds.
2. Camera follow-ups ("left or right", "where is the X", "how much is it")
   are always answered locally; with no look in the last two minutes she
   says "Ask me 'what do you see' first." They never go online.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from jarvis.controller import State
from jarvis.sensors import SensorFlags
from jarvis.settings import SETTINGS
from jarvis.speech.voice_cache import fixed_lines
from jarvis.vision.camera import Camera
from jarvis.vision.commands import VisionCommands
from jarvis.vision.detector import Detection
from jarvis.voice.listener import SOURCE_VOICE, VoiceListener
from stretch_rig import build, wait
from test_listener import FakeSTT, _utterance

REMINDER = SETTINGS["conversation"]["confirm_reminder_reply"]
LOOK_FIRST = SETTINGS["camera"]["replies"]["look_first"]


# == 1: "Say confirm or cancel." ===========================================================


def _confirming(tmp_path, monkeypatch):
    rig = build(tmp_path, monkeypatch)
    rig["say"]("remember that I like tea")
    rig["say"]("forget everything")  # L4: waits for confirm
    assert rig["c"].state == State.CONFIRMING
    return rig


def _listener(rig, stt):
    ignored = []
    listener = VoiceListener(rig["c"], rig["orch"].handle_command, stt=stt, ignored=lambda t, r: ignored.append((t, r)),
                             not_confirm_or_cancel=rig["orch"].remind_confirm_or_cancel)
    listener.attach()
    return listener, ignored


def test_the_reminder_is_a_cached_line() -> None:
    """The reminder is a cached line."""
    assert REMINDER == "Say confirm or cancel."
    assert REMINDER in fixed_lines()


@pytest.mark.parametrize("heard", ["Console.", "can find"])  # "Confine" is a confirm since (edit distance 2)
def test_a_mishearing_while_confirming_gets_the_reminder(tmp_path, monkeypatch, heard) -> None:
    """A mishearing while confirming gets the reminder."""
    rig = _confirming(tmp_path, monkeypatch)
    listener, ignored = _listener(rig, FakeSTT(heard))
    try:
        _utterance(listener)
        assert wait(lambda: rig["speech"].texts[-1] == REMINDER)
        assert ignored == [(heard.strip(), "waiting for confirm or cancel")]
        assert rig["c"].state == State.CONFIRMING and rig["c"].pending_confirmation() is not None
        assert rig["groq"].questions == []
    finally:
        listener.detach()
        rig["c"].shutdown()


def test_the_reminder_is_said_at_most_once_per_ten_seconds(tmp_path, monkeypatch) -> None:
    """The reminder is said at most once per ten seconds."""
    rig = _confirming(tmp_path, monkeypatch)
    now = [1000.0]
    rig["orch"]._clock = lambda: now[0]
    stt = FakeSTT("Console.")
    listener, ignored = _listener(rig, stt)
    try:
        _utterance(listener)
        assert wait(lambda: len(ignored) == 1)
        now[0] += 9.5
        stt.text = "can find"
        _utterance(listener)
        assert wait(lambda: len(ignored) == 2)
        assert rig["speech"].texts.count(REMINDER) == 1  # within 10 s: not again
        now[0] += 0.5  # 10 s after the first
        _utterance(listener)
        assert wait(lambda: len(ignored) == 3)
        assert wait(lambda: rig["speech"].texts.count(REMINDER) == 2)
    finally:
        listener.detach()
        rig["c"].shutdown()


def test_confirm_still_works_after_a_reminder(tmp_path, monkeypatch) -> None:
    """Confirm still works after a reminder."""
    rig = _confirming(tmp_path, monkeypatch)
    try:
        assert rig["confirming"]("can find") == REMINDER
        assert rig["c"].state == State.CONFIRMING
        rig["orch"].handle_typed_command("cancel")
        assert wait(lambda: rig["c"].state == State.AWAKE)
        assert rig["speech"].texts[-1] == "Cancelled."
    finally:
        rig["c"].shutdown()


def test_a_typed_mishearing_while_confirming_also_gets_it(tmp_path, monkeypatch) -> None:
    """A typed mishearing while confirming also gets it."""
    rig = _confirming(tmp_path, monkeypatch)
    try:
        started = time.monotonic()
        rig["orch"].handle_typed_command("open notepad")
        assert rig["speech"].texts[-1] == REMINDER
        assert time.monotonic() - started < 2  # no silent wait for LISTENING
        assert rig["c"].state == State.CONFIRMING and rig["launched"] == []
    finally:
        rig["c"].shutdown()


def test_no_reminder_when_nothing_waits(tmp_path, monkeypatch) -> None:
    """No reminder when nothing waits."""
    rig = build(tmp_path, monkeypatch)
    try:
        assert rig["orch"].remind_confirm_or_cancel() is False
        assert REMINDER not in rig["speech"].texts
    finally:
        rig["c"].shutdown()


def test_confirm_words_are_still_handed_over(tmp_path, monkeypatch) -> None:
    """Confirm words are still handed over."""
    rig = _confirming(tmp_path, monkeypatch)
    handed = []
    listener = VoiceListener(rig["c"], lambda t, s: handed.append((t, s)), stt=FakeSTT("Confirm."),
                             not_confirm_or_cancel=rig["orch"].remind_confirm_or_cancel)
    listener.attach()
    try:
        _utterance(listener)
        assert wait(lambda: handed == [("Confirm.", SOURCE_VOICE)])
        assert REMINDER not in rig["speech"].texts
    finally:
        listener.detach()
        rig["c"].shutdown()


# == 2: camera follow-ups are always local ===================================================


class _Capture:
    """Test helper: Capture."""
    def read(self):
        """Fake read for the tests."""
        return True, np.full((480, 640, 3), 120, dtype=np.uint8)

    def release(self) -> None:
        """Fake release for the tests."""
        pass


class _Detector:
    """Test helper: Detector."""
    def __init__(self) -> None:
        # A person in the middle, the fridge on the left, a bottle on the right.
        self.found = [Detection("person", 0.89, (250, 50, 400, 470)), Detection("refrigerator", 0.85, (10, 0, 160, 470)),
                      Detection("bottle", 0.80, (520, 300, 560, 400))]
        # 0.55/0.50 in that log -- below [camera] min_confidence
        # now, so raised here to keep testing the follow-ups.)

    def detect(self, frame):
        """Fake detect for the tests."""
        return list(self.found)

    def unload_if_idle(self):
        """Fake unload if idle for the tests."""
        return False


@pytest.fixture
def camera_rig(tmp_path, monkeypatch):
    """Fixture: production wiring with a fake camera and detector."""
    detector = _Detector()
    vision = VisionCommands(Camera(SensorFlags(min_seconds=0), opener=lambda index: _Capture() if index == 0 else None,
                                   blocked=lambda: False), detector)
    rig = build(tmp_path, monkeypatch, vision=vision)
    rig.update(vision=vision, detector=detector)
    yield rig
    vision.stop_stream()
    rig["c"].shutdown()


FOLLOW_UPS = ["Left or right?", "Is it on the left or right", "Where is the phone?", "Where is the refrigerator",
              "How much is it?", "How much does it cost", "how much is the bottle", "where are the bottles"]


@pytest.mark.parametrize("said", FOLLOW_UPS)
def test_a_follow_up_with_no_look_asks_for_one(camera_rig, said) -> None:
    """A follow up with no look asks for one."""
    assert camera_rig["say"](said) == LOOK_FIRST
    assert camera_rig["groq"].questions == []


@pytest.mark.parametrize("said", FOLLOW_UPS)
def test_a_follow_up_after_an_old_look_asks_for_a_new_one(camera_rig, said) -> None:
    """A follow up after an old look asks for a new one."""
    camera_rig["say"]("what do you see")
    camera_rig["vision"]._clock = lambda: time.monotonic() + 121
    assert camera_rig["say"](said) == LOOK_FIRST
    assert camera_rig["groq"].questions == []


@pytest.mark.parametrize("said", ["Left or right?", "Is it on the left or right"])
def test_left_or_right_answers_about_the_main_object(camera_rig, said) -> None:
    """Left or right answers about the main object."""
    camera_rig["say"]("what do you see")
    assert camera_rig["say"](said) == "From the camera's view, the refrigerator is on the left."
    assert camera_rig["groq"].questions == []


def test_how_much_is_it_prices_what_she_just_saw(camera_rig) -> None:
    """How much is it prices what she just saw."""
    camera_rig["say"]("what do you see")
    assert camera_rig["say"]("How much is it?").startswith("That looks like a refrigerator.")
    assert camera_rig["groq"].questions == []


def test_a_plural_finds_the_object(camera_rig) -> None:
    """A plural finds the object."""
    camera_rig["say"]("what do you see")
    assert camera_rig["say"]("where are the bottles") == "From the camera's view, the bottle is on the right."


def test_something_not_seen_is_answered_locally(camera_rig) -> None:
    """Something not seen is answered locally."""
    camera_rig["say"]("what do you see")
    reply = camera_rig["say"]("how much is the laptop")
    assert reply == SETTINGS["camera"]["replies"]["where_unknown"].format(thing="laptop")
    assert camera_rig["groq"].questions == []


def test_a_follow_up_after_a_look_at_nothing(camera_rig) -> None:
    """A follow up after a look at nothing."""
    camera_rig["detector"].found = []
    camera_rig["say"]("what do you see")
    assert camera_rig["say"]("left or right") == SETTINGS["camera"]["replies"]["follow_up_nothing"]
    assert camera_rig["groq"].questions == []


@pytest.mark.parametrize("said", ["where is Paris", "how much is a flight to Sydney"])
def test_questions_about_the_world_still_go_online(camera_rig, said) -> None:
    """Questions about the world still go online."""
    camera_rig["say"](said)
    assert len(camera_rig["groq"].questions) == 1


def test_the_look_first_line_is_cached() -> None:
    """The look first line is cached."""
    assert LOOK_FIRST in fixed_lines()
