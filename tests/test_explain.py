"""F5/R13: "why did you do that?" is answered locally from the latest
decision record (jarvis/actions/explain.py) and never reaches Groq.
Speech and Groq are faked; the controller, logbook and orchestrator are
real."""

from __future__ import annotations

from typing import List

import pytest

from jarvis.actions.conversation import Conversation
from jarvis.actions.explain import ExplainMatcher
from jarvis.controller import Controller, State
from jarvis.interpreter import ActionRequest, Answer, Interpreter
from jarvis.logbook import Logbook
from jarvis.logbook.logbook import NOTHING_YET
from jarvis.orchestrator import Orchestrator
from jarvis.speech import SpeechOutput
from jarvis.speech.qa import AnswerResult


class FakeSpeech(SpeechOutput):
    """Speech output stand-in: records what she would say; nothing is played."""
    def __init__(self) -> None:
        self.texts: List[str] = []

    def speak(self, text: str) -> None:
        """Fake speak for the tests."""
        self.texts.append(text)


class FakeAnswerer:
    """Stands in for Groq; records every question it is asked."""

    model = "test-model"

    def __init__(self, text: str) -> None:
        self.questions: List[str] = []
        self._text = text

    def answer(self, question: str, on_going_online=None) -> AnswerResult:
        """Fake answer for the tests."""
        self.questions.append(question)
        return AnswerResult(self._text, went_online=False)


@pytest.fixture
def make_rig():
    """Fixture: a factory that builds a test rig per call and shuts each one down afterwards."""
    controllers = []

    def _make(answerer: FakeAnswerer):
        c = Controller(start_state=State.AWAKE)
        logbook = Logbook()
        c.set_transition_listener(logbook.on_transition)
        c.start()
        controllers.append(c)
        interpreter = Interpreter()
        speech = FakeSpeech()
        orch = Orchestrator(c, interpreter, speech, logbook=logbook)
        return c, interpreter, speech, orch, logbook

    yield _make
    for c in controllers:
        c.shutdown()


def _register(interpreter, orch, logbook, answerer) -> None:
    """Same order as the real app: local matchers, then the explainer,
    then Groq last."""
    interpreter.register("intro", lambda t: Answer("Hello everyone.") if t == "introduce yourself" else None)
    interpreter.register(
        "open", lambda t: ActionRequest("open_app", {"target": "chrome"}) if t == "open chrome" else None
    )
    ExplainMatcher(logbook).register(interpreter)
    Conversation(answerer).register(interpreter, orch)


@pytest.mark.parametrize(
    "text",
    [
        "why did you do that",
        "Why did you do that?",
        "why did you open chrome",
        "jarvis, why did you do that?",
        "why did you do that jarvis",
        "explain that",
        "Explain that.",
    ],
)
def test_matches_why_phrases(text) -> None:
    """Matches why phrases."""
    assert ExplainMatcher(Logbook()).is_why_question(text)


@pytest.mark.parametrize(
    "text",
    ["why is the sky blue", "why did the chicken cross", "explain photosynthesis", "did you do that", "", "why"],
)
def test_ignores_other_questions(text) -> None:
    """Ignores other questions."""
    matcher = ExplainMatcher(Logbook())
    assert not matcher.is_why_question(text)
    assert matcher(text.lower()) is None


def test_no_previous_action_says_so_and_never_goes_to_groq(make_rig) -> None:
    """No previous action says so and never goes to Groq."""
    answerer = FakeAnswerer("a Groq guess")
    c, interpreter, speech, orch, logbook = make_rig(answerer)
    _register(interpreter, orch, logbook, answerer)
    orch.handle_typed_command("why did you do that?")
    assert speech.texts == [NOTHING_YET]
    assert answerer.questions == []


def test_a_local_answer_is_not_an_action_to_explain(make_rig) -> None:
    """"why" explains the last thing she DID; introducing
    herself did nothing (it used to say "I answered because you asked:
    introduce yourself")."""
    answerer = FakeAnswerer("a Groq guess")
    c, interpreter, speech, orch, logbook = make_rig(answerer)
    _register(interpreter, orch, logbook, answerer)
    orch.handle_typed_command("introduce yourself")
    orch.handle_typed_command("why did you do that")
    assert speech.texts[-1] == NOTHING_YET
    assert answerer.questions == []


def test_asking_twice_explains_the_same_action_not_the_previous_why(make_rig) -> None:
    """Asking twice explains the same action not the previous why."""
    answerer = FakeAnswerer("a Groq guess")
    c, interpreter, speech, orch, logbook = make_rig(answerer)
    _register(interpreter, orch, logbook, answerer)
    orch.handle_typed_command("introduce yourself")
    orch.handle_typed_command("why did you do that")
    orch.handle_typed_command("explain that")
    assert speech.texts[-1] == speech.texts[-2]
    assert answerer.questions == []


def test_unrelated_question_still_goes_to_groq(make_rig) -> None:
    """Unrelated question still goes to Groq."""
    answerer = FakeAnswerer("Because of Rayleigh scattering.")
    c, interpreter, speech, orch, logbook = make_rig(answerer)
    _register(interpreter, orch, logbook, answerer)
    orch.handle_typed_command("why is the sky blue")
    assert answerer.questions == ["why is the sky blue"]


def test_app_registers_the_explainer_before_groq() -> None:
    """The real wiring in app.py: explain must come before the Groq
    conversation matcher (first match wins)."""
    from pathlib import Path

    source = Path(__file__).resolve().parents[1].joinpath("jarvis", "dashboard", "app.py").read_text(encoding="utf-8")
    assert source.index("ExplainMatcher(logbook).register(interpreter)") < source.index(
        "conversation.register(interpreter, orchestrator)  # F2 -- must stay last"
    )
