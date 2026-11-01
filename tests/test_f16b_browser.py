"""F16b searches: "search X", "google X" and "open chrome
and search X" open a Google search for X in the developer's own Chrome,
exactly like opening a website (open_url, L1) -- no automation, no
JARVIS browser, no DuckDuckGo. A failed live question opens the Google
results in Chrome too. Chrome is faked; nothing is launched."""

from __future__ import annotations

import time
from pathlib import Path
from typing import List

import pytest

from jarvis.actions import open_app
from jarvis.actions.file_index import FileIndex
from jarvis.actions.find_file import FileFinder
from jarvis.browser import commands as browser_commands
from jarvis.browser.commands import BrowserCommands, parse_search, search_url
from jarvis.controller import Controller, Event, EventType, State
from jarvis.controller.trust import FIXED_ACTION_SET, TRUST_TABLE, TrustLevel
from jarvis.dashboard.activity_feed import format_record
from jarvis.dashboard.app import register_matchers
from jarvis.interpreter import Interpreter
from jarvis.logbook import Logbook
from jarvis.logbook.debug_log import DebugLog
from jarvis.memory.commands import MemoryCommands
from jarvis.memory.store import MemoryStore
from jarvis.online import OnlineErrorKind, OnlineStatus
from jarvis.orchestrator import Orchestrator
from jarvis.settings import SETTINGS
from jarvis.speech import SpeechOutput
from jarvis.speech.qa import AnswerResult

REPLIES = SETTINGS["browser"]["replies"]
CHROME = r"C:\Chrome\chrome.exe"
PROJECT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("cleaned, expected", [
    ("open chrome and then search chat gpt", "chat gpt"),
    ("open chrome and search chatgpt", "chatgpt"),
    ("open chrome and search for cheap flights to perth", "cheap flights to perth"),
    ("search the web for python tutorials", "python tutorials"),
    ("search for cricket scores", "cricket scores"),
    ("search online for cricket scores", "cricket scores"),
    ("search cats", "cats"),
    ("google best pizza near me", "best pizza near me"),
    ("google chrome", None),
    ("google docs", None),
    ("open chrome", None),
    ("search for the word invoice on my screen", None),  # F8's "find on my screen"
    ("what is photosynthesis", None),
])
def test_parse_search(cleaned, expected) -> None:
    """Parse search."""
    assert parse_search(cleaned) == expected


def test_the_search_is_a_google_url() -> None:
    """The search is a google URL."""
    assert search_url("chat gpt") == "https://www.google.com/search?q=chat+gpt"
    assert search_url("india vs nz") == "https://www.google.com/search?q=india+vs+nz"


def test_searching_is_open_url_and_the_old_browser_action_is_gone() -> None:
    """Searching is open URL and the old browser action is gone."""
    assert TRUST_TABLE["open_url"] == TrustLevel.L1
    assert "browser_open_read" not in FIXED_ACTION_SET and "browser_open_read" not in TRUST_TABLE


def test_playwright_and_duckduckgo_are_gone_from_code_settings_and_requirements() -> None:
    """Playwright and duckduckgo are gone from code settings and requirements."""
    assert not (PROJECT / "jarvis" / "browser" / "automation.py").exists()
    requirements = (PROJECT / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "playwright==" not in requirements and "greenlet==" not in requirements and "pyee==" not in requirements
    settings = (PROJECT / "jarvis" / "settings" / "settings.toml").read_text(encoding="utf-8").lower()
    assert "duckduckgo.com" not in settings and "ms-playwright" not in settings and "playwright 1" not in settings
    for py in (PROJECT / "jarvis").rglob("*.py"):
        text = py.read_text(encoding="utf-8").lower()
        assert "import playwright" not in text and "from playwright" not in text, py
    assert "playwright" not in (PROJECT / "scripts" / "download_models.py").read_text(encoding="utf-8").lower()


# -- end to end through the real matcher order ----------------------------------------------------


class FakeSpeech(SpeechOutput):
    """Speech output stand-in: records what she would say; nothing is played."""
    def __init__(self) -> None:
        self.texts: List[str] = []

    def speak(self, text: str) -> None:
        """Fake speak for the tests."""
        self.texts.append(text)


class SpyAnswerer:
    """Groq stand-in: records each question that would have gone online and returns a fixed reply."""
    model = "openai/gpt-oss-20b"

    def __init__(self, error=None) -> None:
        self.questions: List[str] = []
        self.error = error

    def today(self):
        """Fake today for the tests."""
        from datetime import date

        return date(2026, 10, 27)

    def answer(self, question, on_going_online=None, **_kwargs):
        """Fake answer for the tests."""
        self.questions.append(question)
        if on_going_online is not None:
            on_going_online()
        return AnswerResult("" if self.error else "an online answer", went_online=True, error=self.error)

    def answer_live(self, question, on_going_online=None, should_stop=None, on_waiting=None):
        """Fake answer live for the tests."""
        return self.answer(question, on_going_online)


def _wait(pred, timeout=3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


@pytest.fixture
def app(tmp_path, monkeypatch):
    """Fixture: the app components with this feature's side effects mocked."""
    runs: list = []
    monkeypatch.setattr(open_app.subprocess, "Popen", lambda args, shell=False: runs.append(args))
    # App launches are mocked too. Without this, "google chrome"
    # (F3's app path) really opened Chrome on the developer's screen.
    monkeypatch.setattr(open_app.os, "startfile", lambda target: runs.append(["startfile", target]))
    monkeypatch.setattr(open_app, "launch_and_verify_with_retry", lambda launch, timeout, poll: (launch(), False))

    def build(chrome=True, groq_error=None):
        monkeypatch.setattr(browser_commands, "find_chrome", lambda: CHROME if chrome else None)
        monkeypatch.setattr(open_app, "find_chrome", lambda: CHROME if chrome else None)
        status = OnlineStatus()
        c = Controller(start_state=State.AWAKE)
        logbook = Logbook(debug_log=DebugLog(path=tmp_path / "debug.log"))
        c.set_transition_listener(logbook.on_transition)
        c.start()
        interpreter, speech = Interpreter(), FakeSpeech()
        orch = Orchestrator(c, interpreter, speech, logbook=logbook)
        groq = SpyAnswerer(groq_error)
        register_matchers(interpreter, orch, logbook, FileFinder(FileIndex(roots=[tmp_path / "f"])), groq, status,
                          memory=MemoryCommands(MemoryStore(tmp_path / "m.json")), browser=BrowserCommands())

        def say(text):
            c.submit(Event(EventType.SPEECH_STARTED, {"source": "voice"}))
            assert _wait(lambda: c.state == State.LISTENING)
            orch.handle_command(text, "voice")
            assert _wait(lambda: c.state == State.AWAKE)
            return speech.texts[-1]

        built.append(c)
        return dict(say=say, speech=speech, groq=groq, logbook=logbook, c=c, runs=runs, debug=tmp_path / "debug.log")

    built: list = []
    yield build
    for c in built:
        c.shutdown()


def test_open_chrome_and_search_chatgpt_from_the_27_oct_log(app) -> None:
    """Open Chrome and search chatgpt from the 27 oct log."""
    rig = app()
    reply = rig["say"]("Open Chrome and search ChatGPT")
    assert rig["groq"].questions == []  # never online, never JARVIS's own browser
    assert rig["runs"] == [[CHROME, "https://www.google.com/search?q=chatgpt"]]
    assert reply == "Searching Google for chatgpt in Chrome."
    record = rig["logbook"].last_completed()
    assert record.rule_or_trust_level == TrustLevel.L1 and record.verification_result == "True"
    lines = format_record(record).lines
    assert 'searching Google for "chatgpt" in Chrome' in lines
    assert "saying: Searching Google for chatgpt in Chrome." in lines
    assert not any("http" in line for line in lines)  # never a URL on screen
    assert "duckduckgo" not in " ".join(lines).lower() and "read 6 results" not in lines


@pytest.mark.parametrize("said, query", [("google best pizza near me", "best pizza near me"),
                                         ("search for cheap flights to perth", "cheap flights to perth")])
def test_search_and_google_open_the_results_in_chrome(app, said, query) -> None:
    """Search and google open the results in Chrome."""
    rig = app()
    assert rig["say"](said) == f"Searching Google for {query} in Chrome."
    assert rig["runs"][-1][1] == search_url(query)


def test_without_chrome_she_says_so(app) -> None:
    """Without Chrome she says so."""
    rig = app(chrome=False)
    reply = rig["say"]("open chrome and search chat gpt")
    assert reply == REPLIES["no_chrome"].format(query="chat gpt") and rig["runs"] == []


def test_google_chrome_still_opens_the_app_not_a_search(app) -> None:
    """Google Chrome still opens the app not a search."""
    rig = app()
    rig["say"]("google chrome")
    assert not any("google.com/search" in " ".join(run) for run in rig["runs"])

def test_the_score_question_from_the_27_oct_log_falls_back_to_chrome(app) -> None:
    """The score question from the 27 oct log falls back to Chrome."""
    rig = app(groq_error=OnlineErrorKind.SERVICE_ERROR)
    reply = rig["say"]("Tell me the score of India vs New Zealand")
    assert len(rig["groq"].questions) == 1  # the online search was tried first (R3)
    assert rig["runs"] == [[CHROME, search_url("tell me the score of india vs new zealand")]]
    assert reply == "I couldn't get a quick answer, so I've opened the results in Chrome."
    lines = format_record(rig["logbook"].last_completed()).lines
    assert "online search: service error" in lines
    assert "saying: I couldn't get a quick answer, so I've opened the results in Chrome." in lines


@pytest.mark.parametrize("error", [OnlineErrorKind.NO_KEY, OnlineErrorKind.BUDGET_USED_UP, OnlineErrorKind.COOLING])
def test_no_key_allowance_or_cooldown_never_open_chrome(app, error) -> None:
    """No key allowance or cooldown never open Chrome."""
    rig = app(groq_error=error)
    rig["say"]("what is the latest cricket score")
    assert rig["runs"] == []


def test_fallback_without_chrome_says_so(app) -> None:
    """Fallback without Chrome says so."""
    rig = app(chrome=False, groq_error=OnlineErrorKind.TIMEOUT)
    assert rig["say"]("what is the latest cricket score") == REPLIES["no_chrome_fallback"]
