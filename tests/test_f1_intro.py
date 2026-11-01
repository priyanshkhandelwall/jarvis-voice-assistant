"""F1: Self Introduction.

Done-when criteria (project specification): the settings text file
holds exactly the locked text, the text passed to speech and the text
recorded in the log match that file character for character, and a test
confirms the text is identical across runs (speech mocked).
"""

from __future__ import annotations

from typing import List

import pytest

from jarvis.actions.intro import INTRO_COMMAND_PHRASE, intro_matcher, load_intro_text
from jarvis.controller import Controller, OriginTag, State, TrustLevel
from jarvis.interpreter import Answer, Interpreter
from jarvis.logbook import Logbook
from jarvis.orchestrator import Orchestrator
from jarvis.speech import SpeechOutput

_LOCKED_TEXT = (
    "Hello everyone. I am JARVIS, which stands for Just A Rather Very "
    "Intelligent System. Think of me as a personal assistant with a stage "
    "name, and a creator, Priyansh, who wanted a computer that listens. I "
    "listen for my name, open apps, find files, read the screen, look "
    "through the camera, and remember what matters. Every move I make "
    "appears live on this screen, and anything risky needs his permission "
    "first. Ready? I am."
)


class RecordingSpeech(SpeechOutput):
    """Speech output stand-in that records every line she would say."""
    def __init__(self) -> None:
        self.spoken: List[str] = []

    def speak(self, text: str) -> None:
        """Fake speak for the tests."""
        self.spoken.append(text)


def test_intro_text_file_matches_the_locked_text_exactly() -> None:
    """Intro text file matches the locked text exactly."""
    assert load_intro_text() == _LOCKED_TEXT


def test_intro_matcher_recognizes_the_phrase() -> None:
    """Intro matcher recognizes the phrase."""
    result = intro_matcher(INTRO_COMMAND_PHRASE)
    assert isinstance(result, Answer)
    assert result.text == _LOCKED_TEXT


@pytest.mark.parametrize(
    "text",
    ["introduce yourself please", "hello", "who are you", "", "introduce  yourself"],
)
def test_intro_matcher_ignores_everything_else(text: str) -> None:
    """Intro matcher ignores everything else."""
    assert intro_matcher(text) is None


@pytest.fixture
def rig():
    """Fixture: the components this file's tests drive, with every side effect mocked."""
    c = Controller(start_state=State.AWAKE)
    c.start()
    logbook = Logbook()
    c.set_transition_listener(logbook.on_transition)
    interpreter = Interpreter()
    interpreter.register("intro", intro_matcher)
    speech = RecordingSpeech()
    orch = Orchestrator(c, interpreter, speech)
    yield c, logbook, speech, orch
    c.shutdown()


def test_spoken_logged_and_file_text_are_all_character_for_character_identical(rig) -> None:
    """Spoken logged and file text are all character for character identical."""
    c, logbook, speech, orch = rig
    file_text = load_intro_text()

    orch.handle_typed_command("introduce yourself")
    assert c.state == State.AWAKE

    assert speech.spoken == [file_text]

    record = logbook.last_completed()
    assert record.understood == {"answer": file_text}
    assert record.result == "answered"

    # Character-for-character, not just equal-looking: same length, same
    # bytes, not just visually similar strings.
    assert speech.spoken[0] == file_text
    assert record.understood["answer"] == file_text
    assert len(speech.spoken[0]) == len(file_text)


def test_intro_is_l0_immediate_developer_requested(rig) -> None:
    """Intro is L0 immediate developer requested."""
    c, logbook, speech, orch = rig
    orch.handle_typed_command("introduce yourself")
    record = logbook.last_completed()
    assert record.rule_or_trust_level == TrustLevel.L0
    assert record.origin == OriginTag.DEVELOPER_REQUESTED
    # L0 never touches the permit pipeline (see trust.py) -- no action,
    # no permit, immediate.
    assert c.current_permit() is None


def test_intro_text_is_identical_across_many_runs(rig) -> None:
    """Same command, same controller, run repeatedly -- the exact same
    text every time, matching the file every time."""
    c, logbook, speech, orch = rig
    file_text = load_intro_text()

    for _ in range(5):
        orch.handle_typed_command("introduce yourself")
        assert c.state == State.AWAKE

    assert speech.spoken == [file_text] * 5
    records = [r for r in logbook.records() if r.choice == "answer the question"]
    assert len(records) == 5
    assert all(r.understood == {"answer": file_text} for r in records)


def test_intro_identical_across_fresh_controllers_too() -> None:
    """Not cached at import time -- a fresh process/controller each time
    still reads the same file and produces the same text."""
    file_text = load_intro_text()
    results = []
    for _ in range(3):
        c = Controller(start_state=State.AWAKE)
        c.start()
        logbook = Logbook()
        c.set_transition_listener(logbook.on_transition)
        interpreter = Interpreter()
        interpreter.register("intro", intro_matcher)
        speech = RecordingSpeech()
        orch = Orchestrator(c, interpreter, speech)
        try:
            orch.handle_typed_command("introduce yourself")
            results.append(speech.spoken[0])
        finally:
            c.shutdown()

    assert results == [file_text, file_text, file_text]


def test_answer_why_for_intro_reads_only_from_the_record(rig) -> None:
    """Answer why for intro reads only from the record."""
    c, logbook, speech, orch = rig
    orch.handle_typed_command("introduce yourself")
    record = logbook.last_completed()
    answer = logbook.answer_why()
    assert answer == f"I answered because you asked: {record.heard}. It was a level 0 action, so it ran straight away."
