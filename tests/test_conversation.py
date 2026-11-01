"""F2 end to end through the typed-command path: deterministic matchers
first, Groq last; "Checking online" said during UNDERSTANDING before the
request; R3 log step; failures say what to set up; Stop during the Groq
call drops the answer; rudeness prefixes the reply and still does the job.
Groq and speech are faked."""

from __future__ import annotations

import time
from typing import List, Optional, Tuple

import pytest

from jarvis.actions.conversation import Conversation
from jarvis.actions.greeting import GreetingMatcher
from jarvis.controller import Controller, State
from jarvis.interpreter import ActionRequest, Interpreter
from jarvis.logbook import Logbook
from jarvis.online import OnlineErrorKind
from jarvis.orchestrator import Orchestrator
from jarvis.settings import SETTINGS
from jarvis.speech import SpeechOutput
from jarvis.speech.qa import AnswerResult
from jarvis.speech.rudeness import RudenessFilter

_RUDE_REPLY = SETTINGS["rudeness"]["reply_text"]
_CHECKING = SETTINGS["speech"]["checking_online_text"]


class StateRecordingSpeech(SpeechOutput):
    """Speech output stand-in that also records the controller state at the moment each line is said."""
    def __init__(self, controller: Controller) -> None:
        self.spoken: List[Tuple[str, State]] = []
        self._controller = controller

    def speak(self, text: str) -> None:
        """Fake speak for the tests."""
        self.spoken.append((text, self._controller.state))

    @property
    def texts(self) -> List[str]:
        """Fake texts for the tests."""
        return [t for t, _ in self.spoken]


class FakeAnswerer:
    """Groq stand-in with a fixed reply or error; records the questions."""
    model = "test-model"

    def __init__(self, text: str = "An answer.", error: Optional[OnlineErrorKind] = None, goes_online: bool = True, during=None) -> None:
        self.questions: List[str] = []
        self._text, self._error, self._goes_online, self._during = text, error, goes_online, during

    def answer(self, question: str, on_going_online=None) -> AnswerResult:
        """Fake answer for the tests."""
        self.questions.append(question)
        if self._goes_online and on_going_online is not None:
            on_going_online()
        if self._during is not None:
            self._during()
        return AnswerResult(self._text, went_online=self._goes_online, error=self._error)


@pytest.fixture
def make_rig():
    """Fixture: a factory that builds a test rig per call and shuts each one down afterwards."""
    rigs = []

    def _make(answerer: FakeAnswerer, rude: bool = False):
        c = Controller(start_state=State.AWAKE)
        logbook = Logbook()
        c.set_transition_listener(logbook.on_transition)
        c.start()
        interpreter = Interpreter()
        speech = StateRecordingSpeech(c)
        rudeness = RudenessFilter(SETTINGS["rudeness"]["words"]) if rude else None
        orch = Orchestrator(c, interpreter, speech, logbook=logbook, rudeness=rudeness)
        rigs.append(c)
        return c, interpreter, speech, orch, logbook

    yield _make
    for c in rigs:
        c.shutdown()


def _register_all(interpreter: Interpreter, orch: Orchestrator, answerer: FakeAnswerer) -> None:
    interpreter.register("greeting", GreetingMatcher())
    interpreter.register(
        "open", lambda t: ActionRequest("open_app", {"target": "chrome"}) if t == "open chrome" else None
    )
    Conversation(answerer).register(interpreter, orch)


def test_unmatched_question_goes_to_groq_and_announces_during_understanding(make_rig) -> None:
    """Unmatched question goes to Groq and announces during understanding."""
    answerer = FakeAnswerer("Photosynthesis is how plants make food.")
    c, interpreter, speech, orch, logbook = make_rig(answerer)
    _register_all(interpreter, orch, answerer)
    orch.handle_typed_command("What is photosynthesis")
    assert answerer.questions == ["what is photosynthesis"]
    assert speech.spoken == [(_CHECKING, State.UNDERSTANDING), ("Photosynthesis is how plants make food.", State.SPEAKING)]
    record = logbook.last_completed()
    # R3 (amended): Live Activity shows the words sent, never the
    # provider or model; those are in the decision record / debug log only.
    assert 'searching online, sending "what is photosynthesis"' in record.progress
    assert not any("Groq" in s or "test-model" in s for s in record.progress)
    assert record.online_destination == "Groq, test-model"
    assert c.state == State.AWAKE


def test_deterministic_matchers_win_over_groq(make_rig) -> None:
    """Deterministic matchers win over Groq."""
    answerer = FakeAnswerer()
    c, interpreter, speech, orch, _ = make_rig(answerer)
    _register_all(interpreter, orch, answerer)
    orch.register_executor("open_app", lambda name, params: (True, "Opened Chrome."))
    orch.handle_typed_command("open chrome")
    orch.handle_typed_command("hello")
    assert answerer.questions == []  # neither reached Groq
    assert speech.texts[0] == "Opened Chrome."
    assert speech.texts[1].startswith("Good ")


def test_no_key_speaks_setup_message_without_checking_online(make_rig) -> None:
    """No key speaks setup message without checking online."""
    message = SETTINGS["setup_messages"]["groq_no_key"]
    answerer = FakeAnswerer(message, error=OnlineErrorKind.NO_KEY, goes_online=False)
    c, interpreter, speech, orch, logbook = make_rig(answerer)
    _register_all(interpreter, orch, answerer)
    orch.handle_typed_command("tell me a joke")
    assert speech.texts == [message]  # no "Checking online" -- nothing went online
    assert "online search: key not set up" in logbook.last_completed().progress


def test_stop_during_groq_call_drops_the_answer(make_rig) -> None:
    """Stop during Groq call drops the answer."""
    holder = {}
    answerer = FakeAnswerer("too late", during=lambda: (holder["c"].request_stop("test"), time.sleep(0.2)))
    c, interpreter, speech, orch, logbook = make_rig(answerer)
    holder["c"] = c
    _register_all(interpreter, orch, answerer)
    orch.handle_typed_command("what is the time in tokyo")
    assert "too late" not in speech.texts
    assert c.state == State.AWAKE
    assert logbook.records()[-1].result == "stopped before finishing"


def test_rude_question_prefixes_reply_and_sends_cleaned_text(make_rig) -> None:
    """Rude question prefixes reply and sends cleaned text."""
    answerer = FakeAnswerer("Here is a joke.")
    c, interpreter, speech, orch, logbook = make_rig(answerer, rude=True)
    _register_all(interpreter, orch, answerer)
    orch.handle_typed_command("tell me a joke you idiot")
    assert answerer.questions == ["tell me a joke"]
    assert speech.texts[-1] == f"{_RUDE_REPLY} Here is a joke."
    assert "noted: rude wording" in logbook.last_completed().progress


def test_rude_command_still_does_what_was_asked(make_rig) -> None:
    """Rude command still does what was asked."""
    answerer = FakeAnswerer()
    c, interpreter, speech, orch, _ = make_rig(answerer, rude=True)
    _register_all(interpreter, orch, answerer)
    ran = []
    orch.register_executor("open_app", lambda name, params: (ran.append(params) or True, "Opened Chrome."))
    orch.handle_typed_command("open chrome you idiot")
    assert ran == [{"target": "chrome"}]
    assert speech.texts == [f"{_RUDE_REPLY} Opened Chrome."]


def test_nothing_but_rudeness_gets_the_short_reply(make_rig) -> None:
    """Nothing but rudeness gets the short reply."""
    answerer = FakeAnswerer()
    c, interpreter, speech, orch, _ = make_rig(answerer, rude=True)
    _register_all(interpreter, orch, answerer)
    orch.handle_typed_command("you idiot")
    assert speech.texts == [SETTINGS["rudeness"]["reply_text_nothing_asked"]]
    assert answerer.questions == []
    assert c.state == State.AWAKE


def test_trimmed_answer_logs_answer_trimmed(make_rig) -> None:
    """Trimmed answer logs answer trimmed."""
    answerer = FakeAnswerer("Tip one. Tip two.")
    answerer.answer = lambda question, on_going_online=None: AnswerResult("Tip one. Tip two.", went_online=True, trimmed=True)
    c, interpreter, speech, orch, logbook = make_rig(answerer)
    _register_all(interpreter, orch, answerer)
    orch.handle_typed_command("give me 3 tips")
    assert speech.texts[-1] == "Tip one. Tip two."
    assert "answer trimmed" in logbook.last_completed().progress
