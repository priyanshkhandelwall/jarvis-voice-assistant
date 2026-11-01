"""F21 Morning briefing (only on request).
Open-Meteo and the RSS feeds are mocked; the controller, policy and permits
are real. Nothing is sent anywhere."""

from __future__ import annotations

import urllib.error
from datetime import datetime
from pathlib import Path

import pytest

import jarvis.news.briefing as briefing_mod
from jarvis.actions.weather import WeatherClient
from jarvis.controller import State
from jarvis.controller.permits import OriginTag
from jarvis.controller.trust import STANDING_APPROVAL_ACTIONS, TRUST_TABLE
from jarvis.dashboard.activity_feed import format_record
from jarvis.news.briefing import Briefing
from jarvis.news.feeds import NewsClient
from jarvis.news.topics import TopicCommands, TopicMonitor, TopicStore
from stretch_rig import build, now_utc, rss, service, wait

BBC = "https://feeds.bbci.co.uk/news/rss.xml"
NPR = "https://feeds.npr.org/1001/rss.xml"
MORNING = datetime(2026, 10, 28, 7, 30)


def _geo():
    return {"results": [{"name": "Sydney", "latitude": -33.87, "longitude": 151.2, "admin1": "New South Wales", "country": "Australia",
                         "country_code": "AU", "population": 5638830, "timezone": "Australia/Sydney"}]}


def _forecast():
    return {"current": {"temperature_2m": 20.2, "apparent_temperature": 19.8, "weather_code": 1, "wind_speed_10m": 15.0},
            "daily": {"weather_code": [1, 2], "temperature_2m_max": [24.0, 25.0], "temperature_2m_min": [16.0, 17.0],
                      "precipitation_probability_max": [10, 20]}}


class Net:
    """Every online call of the briefing, recorded. `down` = no internet."""

    def __init__(self) -> None:
        self.weather_calls = []
        self.feed_calls = []
        self.down = False
        self.pages = {
            BBC: rss([(f"BBC story {i}", f"https://bbc.co.uk/{i}", now_utc()) for i in range(1, 4)], "BBC News"),
            NPR: rss([(f"NPR story {i}", f"https://npr.org/{i}", now_utc()) for i in range(1, 4)], "NPR"),
        }

    def weather(self, url, timeout):
        """Fake weather for the tests."""
        self.weather_calls.append(url)
        if self.down:
            raise urllib.error.URLError(OSError("offline"))
        return _geo() if "geocoding" in url else _forecast()

    def feed(self, url, timeout):
        """Fake feed for the tests."""
        self.feed_calls.append(url)
        if self.down:
            raise urllib.error.URLError(OSError("offline"))
        if url in self.pages:
            return self.pages[url]
        return rss([("Solar record set", "https://example.com/solar/1", now_utc(), "Example")], source_tag=True)

    @property
    def calls(self) -> int:
        """Fake calls for the tests."""
        return len(self.weather_calls) + len(self.feed_calls)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """Fixture: a factory for a briefing rig with chosen topics and start state."""
    def make(first_wake=True, topics=("solar",), start_state=State.SLEEPING):
        net = Net()
        store = TopicStore(tmp_path / "topics.json")
        news = NewsClient(service("News"), fetch=net.feed)
        topic_cmds = TopicCommands(TopicMonitor(store, news, sleep=lambda s: None))
        weather = WeatherClient(service("Open-Meteo"), fetch=net.weather)
        briefing = Briefing(weather, news, topic_cmds, now=lambda: MORNING)
        rig = build(tmp_path, monkeypatch, first_wake=first_wake, start_state=start_state, topics=topic_cmds, briefing=briefing)
        for t in topics:
            store.add(t)
        rig.update(net=net, briefing=briefing, store=store)
        return rig

    return make


def _wait_quiet(rig):
    c = rig["c"]
    assert wait(lambda: c.state in (State.AWAKE, State.CONFIRMING))


# -- when it runs: ONLY on request ----------------------------------------------------


def test_no_briefing_action_and_no_standing_approval_left() -> None:
    """No briefing action and no standing approval left."""
    assert "briefing_online" not in TRUST_TABLE
    assert STANDING_APPROVAL_ACTIONS == frozenset()


def test_woke_up_never_starts_a_morning_briefing(setup) -> None:
    """A real session: "woke up" -> "started by JARVIS: morning
    briefing (first wake of the day)". The first wake of the day now says the
    greeting and nothing else: no date, no confirm, nothing sent."""
    rig = setup(first_wake=True)
    rig["wake"]()
    _wait_quiet(rig)
    assert rig["c"].state == State.AWAKE
    assert len(rig["speech"].texts) == 1 and rig["net"].calls == 0
    assert not any("morning briefing" in " ".join(format_record(r).lines) for r in rig["logbook"].records())


def test_the_first_wake_hook_is_gone() -> None:
    """The first wake hook is gone."""
    from jarvis.news import topics as topics_mod
    from jarvis.orchestrator import Orchestrator

    assert not hasattr(Orchestrator, "on_first_wake_of_day")
    assert not hasattr(Briefing, "first_wake_work") and not hasattr(Briefing, "due_today")
    assert not hasattr(topics_mod.TopicCommands, "first_wake_work")
    assert "enabled" not in briefing_mod._CFG and "state_path" not in briefing_mod._CFG


@pytest.mark.parametrize("said", ["brief me", "morning briefing", "give me my briefing"])
def test_it_runs_when_he_asks(setup, said) -> None:
    """It runs when he asks."""
    rig = setup(start_state=State.AWAKE)
    assert rig["say"](said).startswith("Good morning, Priyansh. It's Wednesday the 28th of October. In Sydney")
    assert rig["logbook"].last_completed().origin == OriginTag.DEVELOPER_REQUESTED


# -- offline, failures, length -----------------------------------------------------------------------


def test_offline_gives_greeting_date_and_the_offline_sentence(setup) -> None:
    """Offline gives greeting date and the offline sentence."""
    rig = setup(start_state=State.AWAKE)
    rig["net"].down = True
    reply = rig["say"]("brief me")
    assert reply == "Good morning, Priyansh. It's Wednesday the 28th of October. I can't reach the internet for weather and news right now."


def test_weather_fails_news_works(setup) -> None:
    """Weather fails news works."""
    rig = setup(start_state=State.AWAKE)
    rig["net"].weather = lambda url, timeout: (_ for _ in ()).throw(urllib.error.HTTPError(url, 500, "x", {}, None))
    rig["briefing"].weather._fetch = rig["net"].weather
    reply = rig["say"]("brief me")
    assert "I couldn't get the weather." in reply and "In the news:" in reply


def test_spoken_length_is_capped_at_about_30_seconds(setup) -> None:
    """Spoken length is capped at about 30 seconds."""
    rig = setup(topics=("solar", "cricket", "electric buses", "the matildas"), start_state=State.AWAKE)
    long_items = [(" ".join(["word"] * 30) + f" {i}", f"https://x/{i}", now_utc(), "X") for i in range(3)]
    rig["net"].feed = lambda url, timeout: rss(long_items, source_tag=True)
    rig["briefing"].news._fetch = rig["net"].feed
    reply = rig["say"]("brief me")
    budget = 30 * 2.5
    assert len(reply.split()) <= budget + len("More in Live Activity.".split())
    assert reply.endswith("More in Live Activity.")


def test_weather_comes_from_the_f16a_client_only() -> None:
    """Weather comes from the F16A client only."""
    root = Path(briefing_mod.__file__).resolve().parents[1]
    hits = [str(p.relative_to(root)) for p in root.rglob("*.py") if "open-meteo.com/v1" in p.read_text(encoding="utf-8")]
    assert hits == []  # the URLs live in settings and are used by jarvis/actions/weather.py alone
    assert "WeatherClient" in Path(briefing_mod.__file__).read_text(encoding="utf-8")


def test_no_recap_of_earlier_sessions(setup) -> None:
    """No recap of earlier sessions."""
    rig = setup(start_state=State.AWAKE)
    rig["say"]("remember that my exam is on tuesday")
    reply = rig["say"]("morning briefing")
    assert "exam" not in reply.lower() and "tuesday" not in reply.lower()


# -- on demand and stop ------------------------------------------------------------------------------


def test_on_demand_runs_without_a_preview(setup) -> None:
    """On demand runs without a preview."""
    rig = setup(start_state=State.AWAKE)
    reply = rig["say"]("give me my morning briefing")
    assert reply.startswith("Good morning, Priyansh. It's Wednesday the 28th of October. In Sydney")
    assert rig["c"].state == State.AWAKE
    assert rig["logbook"].last_completed().origin == OriginTag.DEVELOPER_REQUESTED


def test_stop_during_the_online_part_ends_it_and_marks_nothing(setup) -> None:
    """Stop during the online part ends it and marks nothing."""
    rig = setup(start_state=State.AWAKE)
    c = rig["c"]

    def stop_on_feed(url, timeout):
        c.request_stop()
        return rig["net"].pages[BBC]

    rig["briefing"].news._fetch = stop_on_feed
    rig["say"]("brief me")
    assert wait(lambda: c.state == State.AWAKE)
    assert not any("In the news" in t for t in rig["speech"].texts)
    assert rig["store"].topics()[0].seen == []


def test_typed_skip_while_it_runs_is_a_stop(setup) -> None:
    """Typed skip while it runs is a stop."""
    rig = setup(start_state=State.AWAKE)
    orch, c = rig["orch"], rig["c"]

    def skip_on_feed(url, timeout):
        orch.handle_typed_command("skip")
        return rig["net"].pages[BBC]

    rig["briefing"].news._fetch = skip_on_feed
    rig["say"]("brief me")
    assert wait(lambda: c.state == State.AWAKE)
    assert not any("In the news" in t for t in rig["speech"].texts)


def test_typed_skip_while_she_says_the_briefing_is_a_stop(setup) -> None:
    """"brief me" was skippable only while fetching;
    "skip" typed while she was saying it did nothing."""
    rig = setup(start_state=State.AWAKE)
    orch, c, speech = rig["orch"], rig["c"], rig["speech"]
    real_speak = speech.speak

    def speak(text):
        real_speak(text)
        if "In the news" in text:
            orch.handle_typed_command("skip")

    speech.speak = speak
    rig["say"]("brief me")
    assert wait(lambda: c.is_stop_requested())
    assert not orch._task_running.is_set()  # and nothing is skippable once it ends
    rig["say"]("skip")
    assert c.state == State.AWAKE


def test_skip_outside_a_briefing_is_not_a_stop(setup) -> None:
    """Skip outside a briefing is not a stop."""
    rig = setup(start_state=State.AWAKE)
    rig["say"]("skip")
    assert not rig["c"].is_stop_requested()


# "Catch me up!" asked "Did you mean...?"
# and "What's my daily update?" was answered as a memory question.
@pytest.mark.parametrize("said", ["Catch me up!", "What's my daily update?", "Give me the rundown."])
def test_more_ways_to_ask_for_the_briefing(setup, said) -> None:
    """More ways to ask for the briefing."""
    rig = setup(first_wake=False, start_state=State.AWAKE)
    reply = rig["say"](said)
    assert "Saturday" in reply or "Good" in reply, reply
    assert "haven't told me" not in reply and "Did you mean" not in reply
