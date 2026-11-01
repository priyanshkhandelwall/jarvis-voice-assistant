"""Internet panel follows OnlineStatus by events, and ONLINE stays up for a
whole Groq exchange (The request alone lasted under
a second, between "Checking online" and the spoken answer, so the panel
was only ever seen on LOCAL)."""

from __future__ import annotations

import time
from typing import List, Optional, Tuple

import pytest

from jarvis.actions.conversation import Conversation
from jarvis.controller import Controller, State
from jarvis.dashboard.internet_status import InternetPanelBinding
from jarvis.interpreter import Interpreter
from jarvis.online import ConnectionMode, OnlineErrorKind, OnlineStatus
from jarvis.orchestrator import Orchestrator
from jarvis.settings import SETTINGS
from jarvis.speech import NullSpeechOutput, SpeechOutput
from jarvis.speech.qa import SERVICE_NAME, AnswerResult

_CHECKING = SETTINGS["speech"]["checking_online_text"]


def _bound(status: OnlineStatus) -> Tuple[InternetPanelBinding, List[str]]:
    shown: List[str] = []
    return InternetPanelBinding(status, lambda display: shown.append(display.value)), shown


# -- the dashboard callback ------------------------------------------------


def test_dashboard_callback_fires_on_online_and_on_local() -> None:
    """Dashboard callback fires on online and on local."""
    status = OnlineStatus()
    _, shown = _bound(status)
    with status.online("Groq"):
        assert shown == ["ONLINE"]  # already redrawn while the request is in flight, no tick needed
    assert shown == ["ONLINE", "LOCAL"]


def test_callback_fires_on_hold_and_release_without_repeats() -> None:
    """Callback fires on hold and release without repeats."""
    status = OnlineStatus()
    _, shown = _bound(status)
    status.hold("Groq")
    with status.online("Groq"):
        pass
    assert shown == ["ONLINE"]  # the request inside the hold changes nothing on screen
    status.release("Groq")
    assert shown == ["ONLINE", "LOCAL"]


def test_release_without_hold_changes_nothing() -> None:
    """Release without hold changes nothing."""
    status = OnlineStatus()
    _, shown = _bound(status)
    status.release("Groq")
    assert shown == []
    assert status.snapshot().mode == ConnectionMode.LOCAL


def test_limited_cooldown_running_out_is_caught_by_refresh() -> None:
    """Limited cooldown running out is caught by refresh."""
    now = [1000.0]
    status = OnlineStatus(wall_clock=lambda: now[0])
    binding, shown = _bound(status)
    status.set_limited("Groq", until_wall=1060.0)
    assert shown[-1].startswith("LIMITED until")
    now[0] = 1061.0
    binding.refresh()
    assert shown[-1] == "LOCAL"


def test_a_failing_listener_never_breaks_the_online_call() -> None:
    """A failing listener never breaks the online call."""
    status = OnlineStatus()

    def broken(_snapshot) -> None:
        raise RuntimeError("dashboard gone")

    status.add_listener(broken)
    with status.online("Groq"):
        result = "answered"
    assert result == "answered"
    assert status.snapshot().mode == ConnectionMode.LOCAL


# -- ONLINE held for the whole Groq exchange -------------------------------


class ModeRecordingSpeech(SpeechOutput):
    """Records what the Internet panel would show at the moment each line is spoken."""

    def __init__(self, status: OnlineStatus) -> None:
        self.spoken: List[Tuple[str, ConnectionMode]] = []
        self._status = status

    def speak(self, text: str) -> None:
        """Fake speak for the tests."""
        self.spoken.append((text, self._status.snapshot().mode))


class FakeAnswerer:
    """Groq stand-in with a fixed reply or error; records the questions."""
    model = "test-model"

    def __init__(self, status: OnlineStatus, error: Optional[OnlineErrorKind] = None, goes_online: bool = True, during=None) -> None:
        self._status, self._error, self._goes_online, self._during = status, error, goes_online, during

    def answer(self, question: str, on_going_online=None) -> AnswerResult:
        """Fake answer for the tests."""
        if self._goes_online:
            on_going_online()
            with self._status.online(SERVICE_NAME):  # the real request, short
                if self._during is not None:
                    self._during()
        return AnswerResult("Plants make food from light.", went_online=self._goes_online, error=self._error)


@pytest.fixture
def rig():
    """Fixture: the components this file's tests drive, with every side effect mocked."""
    controllers = []

    def _make(**answerer_kwargs):
        status = OnlineStatus()
        c = Controller(start_state=State.AWAKE)
        c.start()
        controllers.append(c)
        speech = ModeRecordingSpeech(status)
        interpreter = Interpreter()
        orch = Orchestrator(c, interpreter, speech)
        Conversation(FakeAnswerer(status, **answerer_kwargs), status).register(interpreter, orch)
        return c, status, speech, orch

    yield _make
    for c in controllers:
        c.shutdown()


def test_checking_online_and_the_spoken_answer_both_show_online(rig) -> None:
    """Checking online and the spoken answer both show online."""
    c, status, speech, orch = rig()
    _, shown = _bound(status)
    orch.handle_typed_command("what is photosynthesis")
    assert speech.spoken == [
        (_CHECKING, ConnectionMode.ONLINE),
        ("Plants make food from light.", ConnectionMode.ONLINE),
    ]
    assert shown == ["ONLINE", "LOCAL"]  # one switch on, one switch off, after speaking
    assert status.snapshot().mode == ConnectionMode.LOCAL


def test_stop_during_the_groq_call_still_returns_to_local(rig) -> None:
    """Stop during the Groq call still returns to local."""
    holder = {}
    c, status, speech, orch = rig(during=lambda: (holder["c"].request_stop("test"), time.sleep(0.2)))
    holder["c"] = c
    orch.handle_typed_command("what is photosynthesis")
    assert status.snapshot().mode == ConnectionMode.LOCAL


def test_rate_limit_hands_over_to_limited_after_the_reply(rig) -> None:
    """Rate limit hands over to limited after the reply."""
    c, status, speech, orch = rig(error=OnlineErrorKind.RATE_LIMITED, during=lambda: status.set_limited(SERVICE_NAME, time.time() + 300))
    orch.handle_typed_command("what is photosynthesis")
    assert speech.spoken[-1][1] == ConnectionMode.ONLINE  # the limit message is part of the exchange
    assert status.snapshot().mode == ConnectionMode.LIMITED


def test_nothing_sent_means_never_online(rig) -> None:
    """Nothing sent means never online."""
    c, status, speech, orch = rig(error=OnlineErrorKind.NO_KEY, goes_online=False)
    _, shown = _bound(status)
    orch.handle_typed_command("what is photosynthesis")
    assert shown == []
    assert all(mode == ConnectionMode.LOCAL for _, mode in speech.spoken)


# -- the orchestrator hook it relies on -------------------------------------


def test_when_command_finishes_runs_after_the_reply_is_spoken() -> None:
    """When command finishes runs after the reply is spoken."""
    c = Controller(start_state=State.AWAKE)
    c.start()
    try:
        events: List[str] = []

        class Speech(SpeechOutput):
            def speak(self, text: str) -> None:
                events.append(f"spoke {text}")

        interpreter = Interpreter()
        orch = Orchestrator(c, interpreter, Speech())

        def matcher(text: str):
            from jarvis.interpreter import Answer

            orch.when_command_finishes(lambda: events.append("finished"))
            orch.when_command_finishes(lambda: (_ for _ in ()).throw(RuntimeError("bad callback")))
            orch.when_command_finishes(lambda: events.append("still runs"))
            return Answer("hi")

        interpreter.register("t", matcher)
        orch.handle_typed_command("hello")
        assert events == ["spoke hi", "finished", "still runs"]
    finally:
        c.shutdown()


def test_when_command_finishes_outside_a_command_runs_at_once() -> None:
    """When command finishes outside a command runs at once."""
    c = Controller(start_state=State.AWAKE)
    orch = Orchestrator(c, Interpreter(), NullSpeechOutput())
    ran: List[bool] = []
    orch.when_command_finishes(lambda: ran.append(True))
    assert ran == [True]
