"""Tests for the typed-command orchestrator (the typed-command box's
backend, 2.4's minimum-mode backup)."""

from __future__ import annotations

from typing import List, Tuple

import pytest

from jarvis.controller import Controller, State
from jarvis.interpreter import ActionRequest, Answer, Interpreter
from jarvis.orchestrator import Orchestrator
from jarvis.speech import SpeechOutput


class RecordingSpeech(SpeechOutput):
    """Speech output stand-in that records every line she would say."""
    def __init__(self) -> None:
        self.spoken: List[str] = []

    def speak(self, text: str) -> None:
        """Fake speak for the tests."""
        self.spoken.append(text)


@pytest.fixture
def rig():
    """Fixture: the components this file's tests drive, with every side effect mocked."""
    c = Controller(start_state=State.AWAKE)
    c.start()
    interpreter = Interpreter()
    speech = RecordingSpeech()
    orch = Orchestrator(c, interpreter, speech)
    yield c, interpreter, speech, orch
    c.shutdown()


def test_unmatched_command_speaks_not_understood(rig) -> None:
    """Unmatched command speaks not understood."""
    c, interpreter, speech, orch = rig
    orch.handle_typed_command("gibberish nonsense")
    assert c.state == State.AWAKE
    assert speech.spoken == ["I don't understand that yet."]


def test_answer_command_speaks_the_answer(rig) -> None:
    """Answer command speaks the answer."""
    c, interpreter, speech, orch = rig
    interpreter.register("test_answer", lambda t: Answer("42") if t == "meaning of life" else None)
    orch.handle_typed_command("meaning of life")
    assert c.state == State.AWAKE
    assert speech.spoken == ["42"]


def test_l1_action_runs_immediately_via_registered_executor(rig) -> None:
    """L1 action runs immediately via registered executor."""
    c, interpreter, speech, orch = rig
    interpreter.register(
        "test_open", lambda t: ActionRequest("open_app", {"name": "chrome"}) if t == "open chrome" else None
    )
    calls: List[Tuple[str, dict]] = []

    def fake_open_app(action_name: str, params: dict) -> Tuple[bool, str]:
        calls.append((action_name, params))
        return True, "opened chrome"

    orch.register_executor("open_app", fake_open_app)
    orch.handle_typed_command("open chrome")
    assert c.state == State.AWAKE
    assert calls == [("open_app", {"name": "chrome"})]
    assert speech.spoken == ["opened chrome"]


def test_l2_action_previews_then_confirms_then_runs(rig) -> None:
    """L2 action previews then confirms then runs."""
    c, interpreter, speech, orch = rig
    interpreter.register(
        "test_type",
        lambda t: ActionRequest("lock_screen", {"text": "hi"}) if t == "lock it" else None,
    )
    calls: List[Tuple[str, dict]] = []

    def fake_lock_screen(action_name: str, params: dict) -> Tuple[bool, str]:
        calls.append((action_name, params))
        return True, "typed hi"

    orch.register_executor("lock_screen", fake_lock_screen)

    orch.handle_typed_command("lock it")
    assert c.state == State.CONFIRMING  # preview issued, waiting on "confirm"
    assert calls == []  # not executed yet

    orch.handle_typed_command("confirm")
    assert c.state == State.AWAKE
    assert calls == [("lock_screen", {"text": "hi"})]
    assert speech.spoken == ["typed hi"]


def test_confirm_with_nothing_pending_speaks_so(rig) -> None:
    """Confirm with nothing pending speaks so."""
    c, interpreter, speech, orch = rig
    orch.handle_typed_command("confirm")
    assert c.state == State.AWAKE
    assert len(speech.spoken) == 1
    assert "nothing to confirm" in speech.spoken[0].lower()


def test_denied_action_speaks_the_reason(rig) -> None:
    """Denied action speaks the reason."""
    c, interpreter, speech, orch = rig
    interpreter.register(
        "test_bad", lambda t: ActionRequest("delete_everything", {}) if t == "delete everything" else None
    )
    orch.handle_typed_command("delete everything")
    assert c.state == State.AWAKE
    assert len(speech.spoken) == 1
    assert "can't do that" in speech.spoken[0].lower()
