"""Shared test rig for F18-F22: the production matcher
order (register_matchers) with every opener, sensor and network call
mocked. Nothing here opens an app, a URL or a file, touches the network,
or changes a system setting; tests/conftest.py fails any test that tries.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from email.utils import format_datetime
from typing import Callable, List, Optional
from xml.sax.saxutils import escape

from jarvis.actions import app_finder, find_file, open_app
from jarvis.actions.file_index import FileIndex
from jarvis.actions.find_file import FileFinder
from jarvis.actions.wake_greeting import WakeGreeter
from jarvis.browser import commands as browser_commands
from jarvis.browser.commands import BrowserCommands
from jarvis.controller import Controller, Event, EventType, State
from jarvis.dashboard.app import register_matchers
from jarvis.interpreter import Interpreter
from jarvis.logbook import Logbook
from jarvis.logbook.debug_log import DebugLog
from jarvis.memory.commands import MemoryCommands
from jarvis.memory.store import MemoryStore
from jarvis.online import OnlineService, OnlineStatus
from jarvis.orchestrator import Orchestrator
from jarvis.security import paths
from jarvis.speech import SpeechOutput
from jarvis.speech.qa import AnswerResult

CHROME = r"C:\Chrome\chrome.exe"


def wait(pred, timeout: float = 3.0) -> bool:
    """Poll `pred` until it is true or `timeout` seconds pass. Returns its last value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class FakeSpeech(SpeechOutput):
    """Speech output stand-in: records what she would say; nothing is played."""
    def __init__(self) -> None:
        self.texts: List[str] = []

    def speak(self, text: str) -> None:
        """Fake speak for the tests."""
        self.texts.append(text)


class SpyAnswerer:
    """Stands in for Groq; records what would have gone online."""

    model = "openai/gpt-oss-20b"

    def __init__(self) -> None:
        self.questions: List[str] = []
        self.reply = "an online answer"

    def today(self):
        """Fake today for the tests."""
        return datetime(2026, 10, 28).date()

    def answer(self, question, on_going_online=None, length_rule=None):
        """Fake answer for the tests."""
        self.questions.append(question)
        if on_going_online is not None:
            on_going_online()
        return AnswerResult(self.reply, went_online=True)

    def answer_live(self, question, on_going_online=None, should_stop=None, on_waiting=None):
        """Fake answer live for the tests."""
        return self.answer(question, on_going_online)


class NoStoreApps:
    """Store-app list stand-in: no Store apps and no background read."""
    def prefetch(self) -> None:
        """Fake prefetch for the tests."""
        pass

    def apps(self, wait_seconds: float = 0.0):
        """Fake apps for the tests."""
        return []


def rss(items, channel_title: str = "Feed", source_tag: bool = False) -> bytes:
    """A small RSS 2.0 feed. `items` are (title, link, published datetime or
    None[, source])."""
    parts = []
    for item in items:
        title, link, published = item[0], item[1], item[2]
        source = item[3] if len(item) > 3 else None
        date = f"<pubDate>{format_datetime(published)}</pubDate>" if published else ""
        src = f"<News:Source>{escape(source)}</News:Source>" if source_tag and source else ""
        parts.append(f"<item><title>{escape(title)}</title><link>{escape(link)}</link>{date}{src}</item>")
    ns = ' xmlns:News="https://www.bing.com/news/search?q=&amp;format=rss"' if source_tag else ""
    return (f'<?xml version="1.0" encoding="utf-8"?><rss version="2.0"{ns}><channel><title>{escape(channel_title)}</title>'
            f"{''.join(parts)}</channel></rss>").encode("utf-8")


def service(name: str = "test", status: Optional[OnlineStatus] = None, clock: Callable[[], float] = time.monotonic) -> OnlineService:
    """An OnlineService for tests (1 s timeout, one retry, 5-minute cooldown)."""
    return OnlineService(name, status or OnlineStatus(), timeout_seconds=1.0, max_retries=1, cooldown_seconds=300, clock=clock)


def now_utc() -> datetime:
    """The current time in UTC."""
    return datetime.now(timezone.utc)


def build(tmp_path, monkeypatch, *, first_wake: bool = False, start_state: State = State.AWAKE, **components):
    """The production wiring. `components` go to register_matchers (topics=,
    ...). Returns a dict with say(), confirming(), wake() and the parts."""
    launched: List[str] = []
    chrome_runs: List[list] = []
    monkeypatch.setattr(paths, "APPROVED_FOLDERS", paths.APPROVED_FOLDERS + [tmp_path.resolve()])
    monkeypatch.setattr(app_finder, "enumerate_installed_apps", lambda store_apps=None: [])
    monkeypatch.setattr(open_app, "STORE_APPS", NoStoreApps())
    monkeypatch.setattr(open_app, "find_chrome", lambda: CHROME)
    monkeypatch.setattr(browser_commands, "find_chrome", lambda: CHROME)
    monkeypatch.setattr(open_app.os, "startfile", launched.append)
    monkeypatch.setattr(find_file.os, "startfile", launched.append)
    monkeypatch.setattr(open_app.subprocess, "Popen", lambda args, shell=False: chrome_runs.append(args))
    verify = lambda launch, timeout, poll: (launch(), False)  # noqa: E731
    monkeypatch.setattr(open_app, "launch_and_verify_with_retry", verify)
    monkeypatch.setattr(find_file, "launch_and_verify_with_retry", verify)

    index = components.pop("index", None)
    if index is None:
        index = FileIndex(roots=[tmp_path / "files"])
        (tmp_path / "files").mkdir(exist_ok=True)
        index.rebuild_now()
    c = Controller(start_state=start_state)
    logbook = Logbook(debug_log=DebugLog(path=tmp_path / "debug.log"))
    c.set_transition_listener(logbook.on_transition)
    c.start()
    interpreter = Interpreter()
    speech = FakeSpeech()
    greeter = WakeGreeter(pools={"anytime": ["Hello."]}, state_path=None)
    if not first_wake:
        greeter._state.day = greeter._day_of(datetime.now())
    orch = Orchestrator(c, interpreter, speech, logbook=logbook, greeter=greeter)
    groq = components.pop("groq", None) or SpyAnswerer()
    register_matchers(interpreter, orch, logbook, FileFinder(index), groq, components.pop("online_status", None),
                      memory=MemoryCommands(MemoryStore(tmp_path / "memory.json")), browser=BrowserCommands(), **components)

    def say(text: str) -> str:
        c.submit(Event(EventType.SPEECH_STARTED, {"source": "voice"}))
        assert wait(lambda: c.state == State.LISTENING)
        orch.handle_command(text, "voice")
        assert wait(lambda: c.state in (State.AWAKE, State.SLEEPING, State.CONFIRMING))
        return speech.texts[-1] if speech.texts else ""

    def confirming(text: str) -> str:
        assert c.state == State.CONFIRMING
        orch.handle_command(text, "voice")
        assert wait(lambda: c.state in (State.AWAKE, State.CONFIRMING))
        wait(lambda: c.state == State.AWAKE, timeout=1.0)
        return speech.texts[-1]

    def wake() -> None:
        orch.wake("test")
        wait(lambda: c.state in (State.AWAKE, State.CONFIRMING))

    def debug_text() -> str:
        path = tmp_path / "debug.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    return dict(c=c, say=say, confirming=confirming, wake=wake, speech=speech, logbook=logbook, orch=orch, groq=groq,
                launched=launched, chrome=chrome_runs, debug=debug_text, greeter=greeter, interpreter=interpreter)
