""""got to sleep" (typed at 09:57) went online, Groq said
"Going to sleep now. Good night." and she stayed awake. Now near-miss
sleep phrases are local, and an online answer may never claim an action."""

from __future__ import annotations

import time
from datetime import date, datetime
from types import SimpleNamespace
from typing import List

import groq
import pytest

from jarvis.actions import app_finder, open_app
from jarvis.actions.file_index import FileIndex
from jarvis.actions.find_file import FileFinder
from jarvis.actions.sleep_wake import SleepMatcher, SleepRequest
from jarvis.actions.web_query import needs_live_info
from jarvis.controller import Controller, State
from jarvis.dashboard.activity_feed import format_record
from jarvis.dashboard.app import register_matchers
from jarvis.interpreter import Interpreter
from jarvis.logbook import Logbook
from jarvis.logbook.debug_log import DebugLog
from jarvis.online import OnlineService, OnlineStatus
from jarvis.orchestrator import Orchestrator
from jarvis.settings import SETTINGS
from jarvis.speech import SpeechOutput
from jarvis.speech.claims import ClaimCheck
from jarvis.speech.qa import GroqAnswerer, build_system_prompt

_CLAIMS = SETTINGS["claims"]
CHECK = ClaimCheck(_CLAIMS)


# -- near-miss sleep phrases, matched locally -------------------------------------------


@pytest.mark.parametrize("said", ["got to sleep", "goto sleep", "go sleep", "time to sleep", "Got to sleep.", "go to sleap",
                                  "gone to sleep", "got to sleep jarvis", "Good night, Javis", "go to sleep"])
def test_near_miss_sleep_phrases_sleep(said) -> None:
    """Near miss sleep phrases sleep."""
    assert isinstance(SleepMatcher()(said), SleepRequest)


@pytest.mark.parametrize("said", ["good light", "what is sleep mode", "how much sleep do i need", "i need to sleep",
                                  "sleep apnea causes", "sleeping", "open sleep cycle app", "slept well"])
def test_other_sentences_with_sleep_do_not(said) -> None:
    """Other sentences with sleep do not."""
    assert SleepMatcher()(said) is None


def test_the_loose_match_needs_a_sleep_word() -> None:
    """The loose match needs a sleep word."""
    m = SleepMatcher(phrases=["good night", "go to sleep"], max_distance=2)
    assert m("good nite") is None  # "good night" has no "sleep": exact only
    assert isinstance(m("go to sleeep"), SleepRequest)


# -- the claim check ------------------------------------------------------------------------


@pytest.mark.parametrize("answer, question, name, reply", [
    ("Going to sleep now. Good night.", "got to sleep", "sleep", "I can't do that from here. Try saying 'go to sleep'."),
    ("Okay, I'm off to sleep.", "nap time", "sleep", "I can't do that from here. Try saying 'go to sleep'."),
    ("I've opened Chrome for you.", "could you start chrome", "open", "I can't do that from here. Try saying 'open chrome'."),
    ("Opening Spotify now!", "spotify", "open", "I can't do that from here. Try saying 'open spotify'."),
    ("Sure, I'm opening YouTube.", "youtube", "open", "I can't do that from here. Try saying 'open youtube'."),
    ("Let me look that up for you.", "Who won the match?", "search", "I can't do that from here. Try saying 'search for who won the match'."),
    ("I've closed the window.", "close it", "close", "I can't do that yet."),
    ("Got it, I'll remember that your birthday is in May.", "keep in mind my birthday", "remember",
     "I can't do that from here. Say 'remember that', then what you want me to remember."),
    ("I've set a timer for 10 minutes.", "set a timer", "reminder", "I can't do that yet."),
    ("I'm playing some music for you.", "play music", "play", "I can't do that yet."),
    ("I've turned the volume down.", "quieter", "settings", "I can't do that from here. Try saying 'set the volume to 50'."),  # F22 is built
])
def test_false_claims_are_found_and_replaced(answer, question, name, reply) -> None:
    """False claims are found and replaced."""
    claim = CHECK.find(answer, question)
    assert claim is not None and claim.name == name and claim.replacement == reply


@pytest.mark.parametrize("answer", [
    "Photosynthesis is how plants make food. The stomata keep opening and closing to let gases in.",
    "A sleep cycle lasts about 90 minutes, and most adults need seven to nine hours of sleep.",
    "You can open Chrome by clicking its icon.",
    "The store is opening at 9 am tomorrow.",
    "Sleep mode saves power when your laptop is idle.",
    "Hamlet was written by William Shakespeare.",
    "To set a timer on Windows, open the Clock app.",
])
def test_ordinary_answers_are_left_alone(answer) -> None:
    """Ordinary answers are left alone."""
    assert CHECK.find(answer, "q") is None


def test_a_live_answer_may_say_it_searched_but_not_that_it_opened_something() -> None:
    """A live answer may say it searched but not that it opened something."""
    assert CHECK.find("I searched and found the score.", "q", live=True) is None
    assert CHECK.find("Opening bcci.tv for you.", "q", live=True).name == "open"


def test_search_suggestion_really_searches() -> None:
    """Search suggestion really searches."""
    # The suggested "search for <question>" goes to the live web search.
    assert needs_live_info("search for who won the match", today=lambda: date(2026, 10, 23))
    assert needs_live_info("look up the population of peru", today=lambda: date(2026, 10, 23))


def test_every_online_prompt_carries_the_rule() -> None:
    """Every online prompt carries the rule."""
    assert _CLAIMS["prompt_rule"] in build_system_prompt()
    assert _CLAIMS["prompt_rule"] in build_system_prompt([("open apps", True)])


# -- end to end through the production matcher order ----------------------------------------


class FakeSpeech(SpeechOutput):
    """Speech output stand-in: records what she would say; nothing is played."""
    def __init__(self) -> None:
        self.texts: List[str] = []

    def speak(self, text: str) -> None:
        """Fake speak for the tests."""
        self.texts.append(text)


def _reply(content):
    message = SimpleNamespace(content=content, executed_tools=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=SimpleNamespace(total_tokens=600))


class FakeGroq:
    """Groq client stand-in: records each request and returns queued replies."""
    calls: List[dict] = []
    replies: List[object] = []

    def __init__(self, **kw):
        def create(**k):
            FakeGroq.calls.append(k)
            return FakeGroq.replies.pop(0) if FakeGroq.replies else _reply("ok")

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Fixture: the components this file's tests drive, with every side effect mocked."""
    FakeGroq.calls, FakeGroq.replies = [], []
    monkeypatch.setattr(groq, "Groq", FakeGroq)
    monkeypatch.setattr(open_app, "STORE_APPS", SimpleNamespace(prefetch=lambda: None, apps=lambda wait_seconds=0: []))
    monkeypatch.setattr(app_finder, "enumerate_installed_apps", lambda store_apps=None: [])
    c = Controller(start_state=State.AWAKE)
    logbook = Logbook(debug_log=DebugLog(path=tmp_path / "debug.log"))
    c.set_transition_listener(logbook.on_transition)
    c.start()
    interpreter = Interpreter()
    speech = FakeSpeech()
    orch = Orchestrator(c, interpreter, speech, logbook=logbook)
    index = FileIndex(roots=[tmp_path / "empty"])
    index.rebuild_now()
    status = OnlineStatus()
    answerer = GroqAnswerer(api_key="k", service=OnlineService("Groq", status, 10, 1, 300), now=lambda: datetime(2026, 10, 23, 9, 57))
    register_matchers(interpreter, orch, logbook, FileFinder(index), answerer, status)
    yield SimpleNamespace(c=c, logbook=logbook, speech=speech, orch=orch)
    c.shutdown()


def _typed(rig, text):
    rig.orch.handle_typed_command(text)


def test_typed_got_to_sleep_sleeps_locally(rig) -> None:
    """Typed got to sleep sleeps locally."""
    _typed(rig, "got to sleep")  # a real typed command
    assert FakeGroq.calls == []
    assert rig.c.state == State.SLEEPING and rig.speech.texts == ["Goodnight."]


def test_groq_claiming_to_sleep_is_replaced(rig) -> None:
    """Groq claiming to sleep is replaced."""
    FakeGroq.replies = [_reply("Going to sleep now. Good night.")]
    # "please have a little nap" now gets "Did you mean go to sleep?" (the router).
    _typed(rig, "tell me a bedtime story")  # not a sleep phrase: it goes online
    assert len(FakeGroq.calls) == 1
    assert rig.speech.texts[-1] == "I can't do that from here. Try saying 'go to sleep'."
    assert rig.c.state == State.AWAKE  # and nothing pretends otherwise
    lines = format_record(rig.logbook.last_completed()).lines
    assert 'removed a claim she can\'t back up: "Going to sleep"' in lines
    assert lines[-1] == "saying: I can't do that from here. Try saying 'go to sleep'."


def test_honest_answers_are_spoken_unchanged(rig) -> None:
    """Honest answers are spoken unchanged."""
    FakeGroq.replies = [_reply("Photosynthesis is how plants turn light into food.")]
    _typed(rig, "what is photosynthesis")
    assert rig.speech.texts[-1] == "Photosynthesis is how plants turn light into food."


def test_the_prompt_sent_carries_the_rule(rig) -> None:
    """The prompt sent carries the rule."""
    _typed(rig, "tell me a joke")
    assert _CLAIMS["prompt_rule"] in FakeGroq.calls[0]["messages"][0]["content"]
