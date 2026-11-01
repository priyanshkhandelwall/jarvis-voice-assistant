"""F16 Stage 1: live questions via Groq's browser_search.

Groq is faked at the SDK level (`groq.Groq`), with responses shaped like
real responses (executed_tools, search_results, rate limit headers).
Realistic failed requests are the test cases.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime
from types import SimpleNamespace
from typing import List

import httpx
import groq
import pytest

from jarvis.actions import app_finder, open_app
from jarvis.actions.file_index import FileIndex
from jarvis.actions.find_file import FileFinder
from jarvis.actions.web_query import fix_mishearings, needs_live_info
from jarvis.controller import Controller, Event, EventType, State
from jarvis.dashboard.activity_feed import format_record, format_session_log
from jarvis.dashboard.app import register_matchers
from jarvis.interpreter import Interpreter
from jarvis.logbook import Logbook
from jarvis.logbook.debug_log import DebugLog
from jarvis.online import OnlineErrorKind, OnlineService, OnlineStatus
from jarvis.online.budget import TokenLedger, parse_reset
from jarvis.orchestrator import Orchestrator
from jarvis.settings import SETTINGS
from jarvis.speech import SpeechOutput
from jarvis.speech.qa import GroqAnswerer, read_executed_tools

_WEB = SETTINGS["web"]
VENDUS = "What was the score of India vs. Vendus T"
WEST_IN_DS = "Tell me the score of India vs West in D's for T20"
BCCI = "https://www.bcci.tv/matches/3385ec17/india-vs-west-indies/scorecard"
ANSWER = "West Indies won by 24 runs, 247 for 3 against India's 223, according to bcci.tv.【0†L5-L7】"
PROVIDER_WORDS = ("groq", "gpt-oss", "browser_search", "exa")


def _tools(query="India vs West Indies T20 score", open_id=0, as_dicts=False):
    results = [{"url": BCCI, "title": "Scorecard | IND v WI, 4th T20I"}, {"url": "https://www.icc-cricket.com/news/x", "title": "ICC"}]
    search = {"type": "browser_search", "arguments": json.dumps({"query": query, "topn": 5, "source": "news"}),
              "output": "URL: https://exa.ai/search?q=x", "search_results": {"results": results}}
    page = {"type": "browser.open", "arguments": json.dumps({"cursor": 0, "id": open_id}),
            "output": f"URL:\n{BCCI}\nIND vs WI, 4th T20I, Tue, 14 Oct 2026 - Scorecard", "search_results": {"results": [{"url": BCCI}]}}
    if as_dicts:
        return [search, page]

    def ns(d):
        sr = SimpleNamespace(results=[SimpleNamespace(**r) for r in d["search_results"]["results"]])
        return SimpleNamespace(**{**d, "search_results": sr})

    return [ns(search), ns(page)]


def _response(content=ANSWER, tools=None, total=2802, finish="stop"):
    message = SimpleNamespace(content=content, executed_tools=_tools() if tools is None else tools)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)],
                           usage=SimpleNamespace(total_tokens=total, prompt_tokens=total - 140))


class FakeGroq:
    """Stands in for groq.Groq. Records every request; replies from a queue."""

    calls: List[dict] = []
    replies: List[object] = []
    headers = {"x-ratelimit-remaining-tokens": "7363", "x-ratelimit-reset-tokens": "4.777s"}

    def __init__(self, api_key=None, timeout=None, max_retries=None):
        outer = self

        class Raw:
            def __init__(self, reply):
                self.reply = reply
                self.headers = FakeGroq.headers

            def parse(self):
                return self.reply

        def create(**kw):
            FakeGroq.calls.append(kw)
            reply = FakeGroq.replies.pop(0) if FakeGroq.replies else _response()
            if isinstance(reply, Exception):
                raise reply
            return reply

        def raw_create(**kw):
            return Raw(create(**kw))

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create, with_raw_response=SimpleNamespace(create=raw_create)))


@pytest.fixture(autouse=True)
def fake_groq(monkeypatch):
    """Fixture: replaces the groq package's client with FakeGroq."""
    FakeGroq.calls, FakeGroq.replies = [], []
    FakeGroq.headers = {"x-ratelimit-remaining-tokens": "7363", "x-ratelimit-reset-tokens": "4.777s"}
    monkeypatch.setattr(groq, "Groq", FakeGroq)
    yield FakeGroq


def _service(status=None):
    return OnlineService("Groq", status or OnlineStatus(), timeout_seconds=10, max_retries=1, cooldown_seconds=300)


def _answerer(**kw):
    kw.setdefault("ledger", TokenLedger())
    return GroqAnswerer(api_key="k", service=_service(), now=lambda: datetime(2026, 10, 16, 23, 5), **kw)


# -- deciding live vs plain (locally, never the model) ---------------------------------------


@pytest.mark.parametrize("question", [VENDUS, WEST_IN_DS, "who won the election", "what's happening with the stock market",
                                      "latest news about nasa", "is it raining in sydney right now", "what happened in 2026"])
def test_live_questions(question) -> None:
    """Live questions."""
    assert needs_live_info(question, today=lambda: date(2026, 10, 16))


@pytest.mark.parametrize("question", ["what is photosynthesis", "tell me a joke", "explain how a rainbow forms",
                                      "who wrote hamlet", "what happened in 1999", "define matchless"])
def test_plain_questions(question) -> None:
    """Plain questions."""
    assert not needs_live_info(question, today=lambda: date(2026, 10, 16))


def test_known_mishearings_are_fixed_before_sending() -> None:
    """Known mishearings are fixed before sending."""
    assert fix_mishearings(VENDUS.lower())[0] == "what was the score of india vs. west indies t20"
    text, changes = fix_mishearings(WEST_IN_DS)
    assert text == "Tell me the score of India vs west indies for T20" and changes == [("West in D's", "west indies")]
    assert fix_mishearings("india vs west indies t20")[1] == []  # nothing to fix, nothing changed


# -- the live answer --------------------------------------------------------------------------


def test_live_call_sends_the_search_tool_and_the_rules() -> None:
    """Live call sends the search tool and the rules."""
    result = _answerer().answer_live("what was the score of india vs west indies t20")
    call = FakeGroq.calls[0]
    assert call["tools"] == [{"type": "browser_search"}] and call["tool_choice"] == "required"
    assert call["max_tokens"] == _WEB["answer_max_tokens"]
    system = call["messages"][0]["content"]
    assert "open at most ONE result page" in system and "Friday 16 October 2026, 23:05" in system
    # Only what the source says ("chased" was inferred, and wrong).
    assert "Report results exactly as the source states; don't infer who batted first or chased unless the source says so." in system
    assert result.searched_for == "India vs West Indies T20 score"
    assert result.pages == ("bcci.tv",) and result.sources == (BCCI,)
    assert result.tokens == 2802
    assert "【" not in result.text and result.text.endswith("according to bcci.tv.")


def test_plain_questions_never_send_the_search_tool() -> None:
    """Plain questions never send the search tool."""
    _answerer().answer("what is photosynthesis")
    assert "tools" not in FakeGroq.calls[0]


@pytest.mark.parametrize("as_dicts", [False, True])
def test_reading_the_tool_records(as_dicts) -> None:
    """Reading the tool records."""
    query, opened = read_executed_tools(SimpleNamespace(executed_tools=_tools(as_dicts=as_dicts)))
    assert query == "India vs West Indies T20 score" and opened == (BCCI,)
    assert read_executed_tools(SimpleNamespace(executed_tools=None)) == (None, ())


def test_if_the_tool_never_ran_the_model_answer_is_not_passed_off_as_live() -> None:
    """If the tool never ran the model answer is not passed off as live."""
    FakeGroq.replies = [_response(content="India won easily.", tools=[])]
    result = _answerer().answer_live("score of india vs west indies")
    assert result.error == OnlineErrorKind.NO_RESULTS
    assert result.text == _WEB["nothing_found_text"]


# -- budget -------------------------------------------------------------------------------------


def test_every_call_is_added_to_the_daily_ledger(tmp_path) -> None:
    """Every call is added to the daily ledger."""
    ledger = TokenLedger(tmp_path / "usage.json", today=lambda: date(2026, 10, 16))
    a = _answerer(ledger=ledger)
    a.answer_live("score of india vs west indies today")
    FakeGroq.replies = [_response(total=640)]
    a.answer("what is photosynthesis")
    assert ledger.used_today() == 2802 + 640
    assert json.loads((tmp_path / "usage.json").read_text()) == {"date": "2026-10-16", "used": 3442}
    again = TokenLedger(tmp_path / "usage.json", today=lambda: date(2026, 10, 16))
    assert again.used_today() == 3442  # survives a restart
    tomorrow = TokenLedger(tmp_path / "usage.json", today=lambda: date(2026, 10, 17))
    assert tomorrow.used_today() == 0


def test_budget_used_up_makes_no_call_and_says_so() -> None:
    """Budget used up makes no call and says so."""
    ledger = TokenLedger(daily_budget=150000)
    ledger.add(149000)
    result = _answerer(ledger=ledger).answer_live("latest news")
    assert FakeGroq.calls == []
    assert result.error == OnlineErrorKind.BUDGET_USED_UP and result.text == _WEB["budget_used_up_text"]


def test_short_minute_reset_waits_then_asks() -> None:
    """Short minute reset waits then asks."""
    slept: List[float] = []
    ledger = TokenLedger()
    ledger.note_headers({"x-ratelimit-remaining-tokens": "900", "x-ratelimit-reset-tokens": "0.3s"})
    waited = []
    result = _answerer(ledger=ledger, sleep=lambda s: (slept.append(s), time.sleep(s))).answer_live("latest news", on_waiting=waited.append)
    assert waited and slept and len(FakeGroq.calls) == 1 and result.error is None


def test_long_minute_reset_does_not_call() -> None:
    """Long minute reset does not call."""
    ledger = TokenLedger()
    ledger.note_headers({"x-ratelimit-remaining-tokens": "500", "x-ratelimit-reset-tokens": "40s"})
    result = _answerer(ledger=ledger).answer_live("latest news")
    assert FakeGroq.calls == [] and result.error == OnlineErrorKind.MINUTE_LIMIT
    assert "Try again in 40 seconds" in result.text


def test_stop_while_waiting_sends_nothing() -> None:
    """Stop while waiting sends nothing."""
    ledger = TokenLedger()
    ledger.note_headers({"x-ratelimit-remaining-tokens": "500", "x-ratelimit-reset-tokens": "5s"})
    result = _answerer(ledger=ledger).answer_live("latest news", should_stop=lambda: True)
    assert FakeGroq.calls == [] and result.text == ""


def test_429_cools_down_without_retrying() -> None:
    """429 cools down without retrying."""
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    FakeGroq.replies = [groq.RateLimitError("limit", response=httpx.Response(429, headers={"retry-after": "60"}, request=request), body=None)]
    a = _answerer()
    first = a.answer_live("latest news")
    second = a.answer_live("latest news")
    assert len(FakeGroq.calls) == 1  # no retry, and no call during the cooldown
    assert first.error == OnlineErrorKind.RATE_LIMITED and second.error == OnlineErrorKind.COOLING


@pytest.mark.parametrize("value, seconds", [("4.777s", 4.777), ("1m26.4s", 86.4), ("28.042s", 28.042), ("250ms", 0.25), ("", None), ("soon", None)])
def test_reset_header_parsing(value, seconds) -> None:
    """Reset header parsing."""
    assert parse_reset(value) == (pytest.approx(seconds) if seconds is not None else None)


# -- end to end through the production matcher order ----------------------------------------


class FakeSpeech(SpeechOutput):
    """Speech output stand-in: records what she would say; nothing is played."""
    def __init__(self) -> None:
        self.texts: List[str] = []

    def speak(self, text: str) -> None:
        """Fake speak for the tests."""
        self.texts.append(text)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Fixture: the components this file's tests drive, with every side effect mocked."""
    monkeypatch.setattr(open_app, "STORE_APPS", SimpleNamespace(prefetch=lambda: None, apps=lambda wait_seconds=0: []))
    monkeypatch.setattr(app_finder, "enumerate_installed_apps", lambda store_apps=None: [
        app_finder.ScoredMatch("Google Chrome", 0.0, "app", r"C:\Start\Google Chrome.lnk")])
    launched = []
    monkeypatch.setattr(open_app.os, "startfile", launched.append)
    monkeypatch.setattr(open_app, "launch_and_verify_with_retry", lambda launch, t, p: (launch(), False))
    c = Controller(start_state=State.AWAKE)
    logbook = Logbook(debug_log=DebugLog(path=tmp_path / "debug.log"))
    c.set_transition_listener(logbook.on_transition)
    c.start()
    interpreter = Interpreter()
    speech = FakeSpeech()
    orch = Orchestrator(c, interpreter, speech, logbook=logbook)
    index = FileIndex(roots=[tmp_path / "empty"])
    index.rebuild_now()
    register_matchers(interpreter, orch, logbook, FileFinder(index), _answerer(), OnlineStatus())

    def say(text):
        c.submit(Event(EventType.SPEECH_STARTED, {"source": "voice"}))
        deadline = time.monotonic() + 3
        while c.state != State.LISTENING and time.monotonic() < deadline:
            time.sleep(0.01)
        orch.handle_command(text, "voice")

    yield SimpleNamespace(c=c, logbook=logbook, speech=speech, say=say, launched=launched, debug=tmp_path / "debug.log")
    c.shutdown()


def test_vendus_t_end_to_end(rig) -> None:
    """Vendus t end to end."""
    rig.say(VENDUS)
    sent = FakeGroq.calls[0]["messages"][1]["content"]
    # JARVIS adds today's date, and "most recent" for a score question.
    assert sent == "what was the score of india vs. west indies t20 (most recent as of 16 October 2026)"
    record = rig.logbook.last_completed()
    lines = format_record(record).lines
    assert lines[:5] == [
        f"heard: {VENDUS}",
        'fixed mishearing: "vendus t" -> "west indies t20"',
        f'searching online, sending "{sent}"',
        "searched for: India vs West Indies T20 score",
        "read 1 page: bcci.tv",
    ]
    assert lines[5] == "result date: 14 October 2026 (bcci.tv)"  # read from the page, not the model
    spoken = ("I searched for India vs West Indies T20 score. West Indies won by 24 runs, 247 for 3 against India's 223, "
              "according to bcci.tv. That result is from 14 October 2026.")
    assert rig.speech.texts == ["Searching online.", spoken]
    assert lines[6] == f"saying: {spoken}"
    shown = "\n".join(lines).lower() + format_session_log(rig.logbook.records()).lower()
    assert not any(word in shown for word in PROVIDER_WORDS)
    assert record.online_destination == f"Groq, openai/gpt-oss-20b, browser_search (Exa); usage 2802; sources {BCCI}"
    assert "browser_search (Exa); usage 2802" in rig.debug.read_text(encoding="utf-8")


def test_west_in_ds_end_to_end(rig) -> None:
    """West in ds end to end."""
    rig.say(WEST_IN_DS)
    assert FakeGroq.calls[0]["messages"][1]["content"] == "tell me the score of india vs west indies for t20 (most recent as of 16 October 2026)"
    assert FakeGroq.calls[0]["tools"] == [{"type": "browser_search"}]
    assert rig.speech.texts[-1].startswith("I searched for India vs West Indies T20 score.")


def test_why_after_a_live_answer_says_it_searched_without_the_provider(rig) -> None:
    """Why after a live answer says it searched without the provider."""
    rig.say(VENDUS)
    rig.say("Tell me why did you do that")
    assert "searched online" in rig.speech.texts[-1] and "groq" not in rig.speech.texts[-1].lower()


def test_open_chrome_and_search_never_goes_to_groq(rig) -> None:
    """Open Chrome and search never goes to Groq."""
    rig.say("open Chrome and then search chat GPT")
    assert FakeGroq.calls == [] and rig.launched == [r"C:\Start\Google Chrome.lnk"]


def test_plain_question_end_to_end_has_no_tool(rig) -> None:
    """Plain question end to end has no tool."""
    FakeGroq.replies = [_response(content="Photosynthesis is how plants make food.", tools=[], total=600)]
    rig.say("What is photosynthesis")
    assert "tools" not in FakeGroq.calls[0]
    assert rig.speech.texts[-1] == "Photosynthesis is how plants make food."


def test_time_question_never_goes_online(rig) -> None:
    """Time question never goes online."""
    rig.say("what is the time right now")
    assert FakeGroq.calls == [] and rig.speech.texts[-1].startswith("It's ")
