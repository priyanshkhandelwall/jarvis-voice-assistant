"""F20 Topic monitor. Network mocked (synthetic RSS),
temporary data folder, no timers. Nothing is opened or sent anywhere."""

from __future__ import annotations

import ast
import json
import urllib.error
from datetime import timedelta
from pathlib import Path

import pytest

import jarvis.news.feeds as feeds_mod
import jarvis.news.topics as topics_mod
from jarvis.controller import State
from jarvis.controller.permits import OriginTag
from jarvis.controller.trust import TRUST_TABLE, TrustLevel
from jarvis.dashboard.activity_feed import format_record
from jarvis.news.feeds import Headline, NewsClient, article_url, parse_rss
from jarvis.news.topics import CheckResult, TopicCommands, TopicMonitor, TopicStore, short_title, spoken_summary
from jarvis.online import OnlineError, OnlineErrorKind
from stretch_rig import build, now_utc, rss, service, wait

NEWS_FILES = [Path(feeds_mod.__file__), Path(topics_mod.__file__)]


@pytest.fixture(autouse=True)
def approve_tmp(tmp_path, monkeypatch):
    """R9: the temporary folder stands in for jarvis_data (an approved folder)."""
    from jarvis.security import paths

    monkeypatch.setattr(paths, "APPROVED_FOLDERS", paths.APPROVED_FOLDERS + [tmp_path.resolve()])


def test_writes_outside_approved_folders_are_refused(tmp_path, monkeypatch) -> None:
    """Writes outside approved folders are refused."""
    from jarvis.security import paths

    monkeypatch.setattr(paths, "APPROVED_FOLDERS", [])
    with pytest.raises(PermissionError):
        TopicStore(tmp_path / "t.json").add("x")


class FakeFeeds:
    """URL -> RSS bytes (or an exception). Records every URL asked for."""

    def __init__(self) -> None:
        self.pages = {}
        self.calls = []
        self.error = None

    def set_topic(self, topic: str, items) -> None:
        """Fake set topic for the tests."""
        self.pages[NewsClient.topic_url(topic)] = rss(items, source_tag=True)

    def __call__(self, url: str, timeout: float) -> bytes:
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        if url not in self.pages:
            raise urllib.error.HTTPError(url, 500, "no page", {}, None)
        return self.pages[url]


@pytest.fixture
def news(tmp_path, monkeypatch):
    """Fixture: topic monitor with fake RSS feeds and a temporary topics file."""
    fake = FakeFeeds()
    sleeps = []
    store = TopicStore(tmp_path / "topics.json")
    monitor = TopicMonitor(store, NewsClient(service("News"), fetch=fake), sleep=sleeps.append)
    topics = TopicCommands(monitor)
    rig = build(tmp_path, monkeypatch, topics=topics)
    rig.update(fake=fake, store=store, topics=topics, sleeps=sleeps, path=tmp_path / "topics.json")
    return rig


def _items(prefix: str, n: int, hours_ago: float = 1):
    when = now_utc() - timedelta(hours=hours_ago)
    return [(f"{prefix} headline {i}", f"https://example.com/{prefix}/{i}", when, "Example News") for i in range(1, n + 1)]


# -- managing the list (local, L0) -------------------------------------------------------


def test_track_list_stop_round_trip(news) -> None:
    """Track list stop round trip."""
    assert news["say"]("Track Indian cricket") == "Tracking indian cricket."
    assert news["say"]("track solar power") == "Tracking solar power."
    assert news["say"]("What am I tracking?") == "You're tracking 2 topics: indian cricket and solar power."
    assert news["say"]("stop tracking solar power") == "Stopped tracking solar power."
    assert json.loads(news["path"].read_text())["topics"][0]["topic"] == "indian cricket"
    assert news["say"]("what am I tracking") == "You're tracking one topic: indian cricket."
    assert news["fake"].calls == []  # managing the list never goes online


def test_commands_work_anywhere_in_the_sentence(news) -> None:
    """Commands work anywhere in the sentence."""
    assert news["say"]("This is cool, track the Matildas please.") == "Tracking matildas."
    assert news["say"]("Jarvis, can you please stop tracking the Matildas") == "Stopped tracking matildas."
    assert news["say"]("ok so what am I tracking") == "You aren't tracking any topics. Say track and a topic, for example track Indian cricket."


def test_edit_topic_list_is_l0_and_tagged_his(news) -> None:
    """Edit topic list is L0 and tagged his."""
    assert TRUST_TABLE["edit_topic_list"] == TrustLevel.L0
    news["say"]("track electric buses")
    record = news["logbook"].last_completed()
    assert record.rule_or_trust_level == TrustLevel.L0 and record.origin == OriginTag.DEVELOPER_REQUESTED
    assert "topic added: electric buses" in format_record(record).lines


def test_duplicates_are_rejected_ignoring_case(news) -> None:
    """Duplicates are rejected ignoring case."""
    news["say"]("track Indian Cricket")
    assert news["say"]("track INDIAN cricket") == "I'm already tracking indian cricket."
    assert len(news["store"].topics()) == 1


def test_cap_of_20_topics(news) -> None:
    """Cap of 20 topics."""
    for n in range(20):
        news["store"].add(f"topic {n}")
    assert news["say"]("track one more thing") == "You're tracking 20 topics, the most I keep. Say stop tracking and a topic first."
    assert len(news["store"].topics()) == 20


def test_long_topic_is_refused(news) -> None:
    """Long topic is refused."""
    assert news["say"]("track " + "word " * 20).startswith("That topic is too long.")
    assert news["store"].topics() == []


def test_sensitive_topic_is_refused_and_never_written(news) -> None:
    """Sensitive topic is refused and never written."""
    reply = news["say"]("track my card number 4111 1111 1111 1111")
    assert reply.startswith("I won't send that online.")
    assert not news["path"].exists()
    assert "4111" not in news["debug"]()


def test_stop_tracking_something_not_tracked(news) -> None:
    """Stop tracking something not tracked."""
    assert news["say"]("stop tracking the weather") == "I'm not tracking weather."


def test_not_topics_and_ordinary_sentences_are_left_alone(news) -> None:
    """Not topics and ordinary sentences are left alone."""
    news["say"]("I lost track of time")
    assert news["store"].topics() == []
    news["say"]("track it")
    assert news["store"].topics() == []


# -- checking (on demand, L0) --------------------------------------------------------------


def test_on_demand_check_speaks_only_new_headlines(news) -> None:
    """On demand check speaks only new headlines."""
    news["store"].add("indian cricket")
    news["fake"].set_topic("indian cricket", _items("cricket", 2))
    reply = news["say"]("any news on my topics")
    assert reply == "2 new headlines on indian cricket: cricket headline 1. cricket headline 2."
    # Said once: the same headlines are not new next time.
    assert news["say"]("check my topics") == "No new headlines on your topics."
    assert len(news["fake"].calls) == 2


def test_at_most_three_per_topic(news) -> None:
    """At most three per topic."""
    news["store"].add("solar")
    news["fake"].set_topic("solar", _items("solar", 6))
    reply = news["say"]("any news on my topics")
    assert reply.startswith("3 new headlines on solar:") and "solar headline 4" not in reply


def test_old_headlines_are_not_new(news) -> None:
    """Old headlines are not new."""
    news["store"].add("solar")
    news["fake"].set_topic("solar", _items("solar", 2, hours_ago=200))
    assert news["say"]("any news on my topics") == "No new headlines on your topics."


def test_several_topics_and_a_quiet_one(news) -> None:
    """Several topics and a quiet one."""
    for t in ("indian cricket", "solar", "the matildas"):
        news["store"].add(t)
    news["fake"].set_topic("indian cricket", _items("cricket", 1))
    news["fake"].set_topic("solar", [])
    news["fake"].set_topic("the matildas", [])
    reply = news["say"]("any news on my topics")
    assert reply == "1 new headline on indian cricket: cricket headline 1. Nothing new on solar or the matildas."
    assert news["sleeps"] == [1.5, 1.5]  # spacing between requests, clock mocked


def test_r3_words_in_live_activity_never_in_debug_log(news) -> None:
    """R3 words in live activity never in debug log."""
    news["store"].add("electric buses")
    news["fake"].set_topic("electric buses", _items("bus", 1))
    news["say"]("any news on my topics")
    lines = format_record(news["logbook"].last_completed()).lines
    assert 'searching online, sending "electric buses"' in lines
    assert any(line.startswith("saying: 1 new headline on electric buses") for line in lines)
    log = news["debug"]()
    assert "electric buses" not in log and "bus headline" not in log  # R4: never the value
    assert "Bing News search RSS" in log and "sha256" in log and "14 characters" in log
    assert "Bing" not in " ".join(lines)  # R3: no provider in Live Activity
    assert "Searching online." in news["speech"].texts or any("online" in t.lower() for t in news["speech"].texts)


def test_no_topics_means_no_request(news) -> None:
    """No topics means no request."""
    assert news["say"]("any news on my topics") == "You aren't tracking any topics yet. Say track and a topic first."
    assert news["fake"].calls == []


def test_offline_is_r14_and_no_crash(news) -> None:
    """Offline is R14 and no crash."""
    news["store"].add("solar")
    news["store"].add("cricket")
    news["fake"].error = urllib.error.URLError(OSError("no network"))
    reply = news["say"]("any news on my topics")
    assert reply == "I can't reach the news right now. Check your internet connection, then try again."
    assert news["store"].topics()[0].seen == []
    assert len(news["fake"].calls) == 2  # one try + R1's single retry, then no knocking for topic 2


def test_one_topic_failing_says_which(news) -> None:
    """One topic failing says which."""
    news["store"].add("solar")
    news["store"].add("cricket")
    news["fake"].set_topic("solar", _items("solar", 1))  # cricket's page is a 500
    reply = news["say"]("any news on my topics")
    assert reply == "1 new headline on solar: solar headline 1. I couldn't check cricket."


def test_429_cools_down_and_the_next_check_makes_no_request(news) -> None:
    """429 cools down and the next check makes no request."""
    news["store"].add("solar")
    news["fake"].error = urllib.error.HTTPError("u", 429, "slow down", {"Retry-After": "120"}, None)
    assert news["say"]("any news on my topics").startswith("The free limit for news is reached; it is off until")
    calls = len(news["fake"].calls)
    assert news["say"]("any news on my topics").startswith("The free limit for news is reached")
    assert len(news["fake"].calls) == calls


def test_malformed_feed_is_a_service_error_not_a_crash(news) -> None:
    """Malformed feed is a service error not a crash."""
    news["store"].add("solar")
    news["fake"].pages[NewsClient.topic_url("solar")] = b"<html>not rss"
    assert news["say"]("any news on my topics").startswith("The news service didn't answer properly")


def test_stop_during_check_marks_nothing(news, monkeypatch) -> None:
    """Stop during check marks nothing."""
    for t in ("a1", "b2"):
        news["store"].add(t)
    news["fake"].set_topic("a1", _items("a", 1))
    news["fake"].set_topic("b2", _items("b", 1))
    c = news["c"]
    monkeypatch.setattr(news["topics"].monitor, "_sleep", lambda s: c.request_stop())
    news["say"]("any news on my topics")
    assert wait(lambda: c.state == State.AWAKE)
    assert all(t.seen == [] for t in news["store"].topics())
    assert len(news["fake"].calls) == 1


def test_seen_hashes_are_capped(tmp_path) -> None:
    """Seen hashes are capped."""
    store = TopicStore(tmp_path / "t.json")
    store.add("x")
    store.mark_seen({"x": [f"k{n}" for n in range(250)]})
    seen = store.topics()[0].seen
    assert len(seen) == 200 and seen[-1] == "k249" and seen[0] == "k50"


def test_headline_text_is_never_stored(news) -> None:
    """Headline text is never stored."""
    news["store"].add("solar")
    news["fake"].set_topic("solar", _items("solar", 2))
    news["say"]("any news on my topics")
    raw = news["path"].read_text(encoding="utf-8")
    assert "headline" not in raw and "example.com" not in raw
    assert len(json.loads(raw)["topics"][0]["seen"]) == 2


def test_corrupt_file_is_renamed_not_deleted(tmp_path) -> None:
    """Corrupt file is renamed not deleted."""
    path = tmp_path / "topics.json"
    path.write_text("{ not json", encoding="utf-8")
    store = TopicStore(path)
    assert store.topics() == []
    kept = list(tmp_path.glob("topics.corrupt-*.json"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == "{ not json"
    assert "kept it as topics.corrupt-" in store.load_note


# -- topics are checked only on request (the first-wake check is gone) ------------------


def test_the_first_wake_of_the_day_checks_nothing(tmp_path, monkeypatch) -> None:
    """The first wake of the day checks nothing."""
    fake = FakeFeeds()
    store = TopicStore(tmp_path / "topics.json")
    topics = TopicCommands(TopicMonitor(store, NewsClient(service("News"), fetch=fake), sleep=lambda s: None))
    rig = build(tmp_path, monkeypatch, first_wake=True, start_state=State.SLEEPING, topics=topics)
    store.add("solar")
    fake.set_topic("solar", _items("solar", 1))
    rig["wake"]()
    assert wait(lambda: rig["c"].state == State.AWAKE)
    assert len(rig["speech"].texts) == 1  # the greeting only
    assert fake.calls == []
    assert rig["say"]("any news on my topics") == "1 new headline on solar: solar headline 1."


def test_no_timer_thread_or_scheduler_in_the_news_code() -> None:
    """No timer thread or scheduler in the news code."""
    for path in NEWS_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {n.attr if isinstance(n, ast.Attribute) else getattr(n, "id", "") for n in ast.walk(tree)}
        modules = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        modules |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert "Timer" not in names and "Thread" not in names and "sched" not in modules and "schedule" not in modules, path


def test_importing_makes_no_network_call(monkeypatch) -> None:
    """Importing makes no network call."""
    import importlib
    import urllib.request

    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: calls.append(a))
    importlib.reload(feeds_mod)
    importlib.reload(topics_mod)
    assert calls == []


# -- parsing ------------------------------------------------------------------------------------------


def test_bing_click_links_resolve_to_the_article() -> None:
    """Bing click links resolve to the article."""
    link = "http://www.bing.com/news/apiclick.aspx?ref=FexRss&aid=&tid=x&url=https%3a%2f%2fwww.outlookindia.com%2fsports%2fa&c=1&mkt=en-au"
    assert article_url(link) == "https://www.outlookindia.com/sports/a"


def test_parse_reads_source_and_date() -> None:
    """Parse reads source and date."""
    when = now_utc()
    heads = parse_rss(rss([("T1", "https://a.com/1", when, "Outlook India")], source_tag=True))
    assert heads[0].title == "T1" and heads[0].source == "Outlook India" and heads[0].published is not None


def test_entity_bombs_are_refused() -> None:
    """Entity bombs are refused."""
    bomb = (b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
            b"<rss><channel><item><title>&b;</title></item></channel></rss>")
    with pytest.raises(OnlineError) as error:
        parse_rss(bomb)
    assert error.value.kind == OnlineErrorKind.SERVICE_ERROR


def test_short_titles() -> None:
    """Short titles."""
    assert short_title("India win the series - Fox Sports") == "India win the series"
    assert short_title(" ".join(f"w{n}" for n in range(30))).split() == [f"w{n}" for n in range(14)]


def test_summary_for_no_reachable_topics() -> None:
    """Summary for no reachable topics."""
    result = CheckResult(failed=["a"], error=OnlineError(OnlineErrorKind.TIMEOUT))
    assert spoken_summary(result) == "I can't reach the news right now. Check your internet connection, then try again."


def test_feeds_mode_filters_the_general_feeds(monkeypatch) -> None:
    """Feeds mode filters the general feeds."""
    monkeypatch.setitem(feeds_mod._CFG, "topic_source", "feeds")
    general = [Headline("Solar farm opens in Sydney", "https://a/1", "BBC News", now_utc()),
               Headline("Cricket news", "https://a/2", "BBC News", now_utc())]
    client = NewsClient(service("News"), fetch=lambda u, t: pytest.fail("no request in feeds mode"))
    assert [h.title for h in client.topic("solar farm", general)] == ["Solar farm opens in Sydney"]


# -- F17: STARTUP_TASK_REQUESTED -------------------------------------------------------------------


@pytest.mark.parametrize("state", [s for s in State if s != State.AWAKE])
def test_startup_task_is_refused_outside_awake(state) -> None:
    """Startup task is refused outside awake."""
    from jarvis.controller import Controller, Event, EventType

    c = Controller(start_state=state)
    c.start()
    c.submit(Event(EventType.STARTUP_TASK_REQUESTED, {"task": "x"}))
    assert wait(lambda: c.transition_count() >= 1)
    assert c.last_transition().accepted is False and c.state == state


def test_origin_comes_from_the_event_type_never_the_payload() -> None:
    """Origin comes from the event type never the payload."""
    from jarvis.controller import Controller, Event, EventType

    c = Controller(start_state=State.AWAKE)
    c.start()
    c.submit(Event(EventType.STARTUP_TASK_REQUESTED, {"task": "x", "origin": OriginTag.DEVELOPER_REQUESTED}))
    assert wait(lambda: c.state == State.UNDERSTANDING)
    c.submit(Event(EventType.ACTION_REQUESTED, {"action_name": "edit_topic_list", "params": {}, "origin": OriginTag.DEVELOPER_REQUESTED}))
    assert wait(lambda: c.state == State.ACTING)
    assert c.last_transition().origin == OriginTag.JARVIS_INITIATED
    c.request_stop()
    assert wait(lambda: c.state == State.AWAKE)
    # A command he gives afterwards is his again, whatever the payload says.
    c.submit(Event(EventType.SPEECH_STARTED, {"source": "typed", "origin": OriginTag.JARVIS_INITIATED}))
    c.submit(Event(EventType.COMMAND_CAPTURED, {"text": "track x"}))
    assert wait(lambda: c.state == State.UNDERSTANDING)
    c.submit(Event(EventType.ACTION_REQUESTED, {"action_name": "edit_topic_list", "params": {}, "origin": OriginTag.JARVIS_INITIATED}))
    assert wait(lambda: c.state == State.ACTING)
    assert c.last_transition().origin == OriginTag.DEVELOPER_REQUESTED
