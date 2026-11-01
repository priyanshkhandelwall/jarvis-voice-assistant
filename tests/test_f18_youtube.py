"""F18 YouTube tools. The transcript library, Groq and
Chrome are mocked; the real exception classes of youtube-transcript-api
1.2.4 are raised. Nothing is opened or sent anywhere."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import List

import pytest
import requests
import youtube_transcript_api as yta

import jarvis.youtube.commands as yt_mod
from jarvis.controller.trust import TRUST_TABLE, TrustLevel
from jarvis.dashboard.activity_feed import format_record
from jarvis.online.chunked import split_text, summarise_chunks
from jarvis.speech.qa import AnswerResult, search_result_urls
from jarvis.youtube.commands import Transcript, YouTubeCommands, video_id
from stretch_rig import CHROME, SpyAnswerer, build, service

VID = "dQw4w9WgXcQ"


@dataclass
class Snippet:
    """One transcript snippet: text, start time and duration."""
    text: str
    start: float
    duration: float


class Fetched(list):
    """A fetched transcript stand-in: a list of snippets with language and generated flag."""
    def __init__(self, snippets, language_code="en", is_generated=False):
        super().__init__(snippets)
        self.language_code = language_code
        self.is_generated = is_generated


class FakeTranscript:
    """youtube-transcript-api transcript stand-in."""
    def __init__(self, fetched, is_generated):
        self._fetched = fetched
        self.is_generated = is_generated

    def fetch(self):
        """Fake fetch for the tests."""
        return self._fetched


class FakeApi:
    """Stands in for YouTubeTranscriptApi. `error` is raised by fetch()."""

    made: List[dict] = []

    def __init__(self, fetched=None, error=None, listing=None):
        self.fetched = fetched
        self.error = error
        self.listing = listing or []
        self.fetch_calls = []

    def factory(self, session):
        """Fake factory for the tests."""
        FakeApi.made.append({"session": session, "thread": threading.current_thread().name})
        return self

    def fetch(self, vid, languages=("en",)):
        """Fake fetch for the tests."""
        self.fetch_calls.append((vid, list(languages)))
        if self.error is not None:
            raise self.error
        return self.fetched

    def list(self, vid):
        """Fake list for the tests."""
        return iter(self.listing)


def snippets(n_chars: int, per: int = 100, seconds: float = 10.0):
    """Transcript snippets adding up to about `n_chars` characters."""
    count = max(1, n_chars // (per + 1))
    return [Snippet("w" * per, i * seconds, seconds) for i in range(count)]


class SummaryAnswerer(SpyAnswerer):
    """Groq stand-in for F18: fixed summaries and video-search results, all recorded."""
    def __init__(self) -> None:
        super().__init__()
        self.summaries = []
        self.finds = []
        self.find_urls = ()
        self.summary_error = None

    def summarise(self, text, instructions, on_going_online=None):
        """Fake summarise for the tests."""
        self.summaries.append((text, instructions))
        if self.summary_error is not None:
            return AnswerResult("The online search had a problem. Try again in a moment.", True, self.summary_error)
        return AnswerResult("The video explains photosynthesis. Plants turn light into sugar. They release oxygen. "
                            "It ends with a quiz.", True)

    def find_youtube_video(self, words, on_going_online=None):
        """Fake find youtube video for the tests."""
        self.finds.append(words)
        if on_going_online is not None:
            on_going_online()
        return self.find_urls, AnswerResult("", True)


class Clock:
    """A clock the test moves by hand (sleep() advances it instantly)."""
    def __init__(self):
        self.now = 0.0
        self.slept = 0.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        """Fake sleep for the tests."""
        self.now += s
        self.slept += s


@pytest.fixture
def tube(tmp_path, monkeypatch):
    """Fixture: the production wiring with YouTube's transcript API and Groq mocked."""
    FakeApi.made = []
    groq = SummaryAnswerer()
    # At least [youtube] min_transcript_chars, or nothing is summarised.
    api = FakeApi(Fetched([Snippet("Plants use light from the sun to turn water and carbon dioxide into sugar, which they store as food.", 0, 4), Snippet("They make sugar in their leaves and release oxygen into the air, which animals and people need to breathe every day.", 4, 4)]))
    clock = Clock()
    youtube = YouTubeCommands(groq, service("YouTube"), api_factory=api.factory, sleep=clock.sleep, clock=clock, chrome=lambda: CHROME)
    rig = build(tmp_path, monkeypatch, groq=groq, youtube=youtube)
    rig.update(api=api, youtube=youtube, clock=clock)
    return rig


# -- play (L1, his Chrome) -----------------------------------------------------------------------------


def test_play_opens_youtube_search_results_in_chrome(tube) -> None:
    """Play opens YouTube search results in Chrome."""
    reply = tube["say"]("Play Bohemian Rhapsody on YouTube")
    assert reply == "Searching YouTube for bohemian rhapsody in Chrome."
    assert tube["chrome"] == [[CHROME, "https://www.youtube.com/results?search_query=bohemian+rhapsody"]]
    lines = format_record(tube["logbook"].last_completed()).lines
    assert 'searching YouTube for "bohemian rhapsody" in Chrome' in lines
    assert not any("http" in line for line in lines) and "http" not in reply  # never a URL
    assert tube["logbook"].last_completed().rule_or_trust_level == TrustLevel.L1


def test_play_anywhere_in_a_sentence(tube) -> None:
    """Play anywhere in a sentence."""
    assert tube["say"]("I'm bored, play lofi beats on YouTube please") == "Searching YouTube for lofi beats in Chrome."


def test_play_without_chrome_says_what_to_install(tube) -> None:
    """Play without Chrome says what to install."""
    tube["youtube"]._chrome = lambda: None
    assert tube["say"]("play jazz on youtube").endswith("Install Google Chrome, then try again.")
    assert tube["chrome"] == []


# -- link parsing ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("link", [f"https://www.youtube.com/watch?v={VID}&t=30s", f"https://youtu.be/{VID}",
                                  f"https://www.youtube.com/shorts/{VID}", f"https://www.youtube.com/embed/{VID}", VID])
def test_id_from_every_link_form(link) -> None:
    """ID from every link form."""
    assert video_id(link) == VID


@pytest.mark.parametrize("link", ["dQw4w9WgXc", "dQw4w9WgXcQQ", "https://www.youtube.com/playlist?list=PLabc"])
def test_bad_ids_are_rejected(link) -> None:
    """Bad IDs are rejected."""
    assert video_id(link) is None


def test_bad_link_is_r14(tube) -> None:
    """Bad link is R14."""
    assert tube["say"]("summarise this video https://www.youtube.com/playlist?list=PLabc") == \
        "I couldn't read a YouTube video in that link. Check it and try again."


# -- summarise a typed link (L0) -----------------------------------------------------------------------------


def test_summarise_a_typed_link(tube) -> None:
    """Summarise a typed link."""
    reply = tube["say"](f"summarise this video https://www.youtube.com/watch?v={VID}")
    assert reply == "The video explains photosynthesis. Plants turn light into sugar. They release oxygen."
    assert tube["api"].fetch_calls == [(VID, ["en"])]  # the bare, case-kept ID, never a URL
    lines = format_record(tube["logbook"].last_completed()).lines
    assert "transcript found (en, manual, 217 chars)" in lines and "summary ready" in lines
    assert any(line.startswith("summary: The video explains") for line in lines)  # the full summary is shown
    assert not any("http" in line for line in lines[1:])  # only his own typed line ("heard: …") has the link
    assert not any("http" in t for t in tube["speech"].texts)
    log = tube["debug"]()
    assert f"video {VID}" in log and "217 characters" in log and "sha256" in log
    assert tube["logbook"].last_completed().rule_or_trust_level == TrustLevel.L0  # no preview, no confirm


def test_one_api_instance_per_call_on_the_worker_thread_with_the_timeout_session(tube) -> None:
    """One API instance per call on the worker thread with the timeout session."""
    tube["say"](f"summarise this video youtu.be/{VID}")
    assert len(FakeApi.made) == 1
    session = FakeApi.made[0]["session"]
    assert isinstance(session, requests.Session) and type(session).__name__ == "TimeoutSession"
    assert FakeApi.made[0]["thread"] == threading.current_thread().name  # made where the command runs


def test_auto_generated_captions_are_flagged(tube) -> None:
    """Auto generated captions are flagged."""
    tube["api"].fetched = Fetched([Snippet("hello and welcome to my channel " * 8, 0, 3)], is_generated=True)
    assert tube["say"](f"summarise this video {VID}").endswith("These are auto-generated captions, so names and terms may be wrong.")


def test_no_transcript_found_falls_back_to_the_list(tube) -> None:
    """No transcript found falls back to the list."""
    tube["api"].error = yta.NoTranscriptFound(VID, ["en"], None)
    # Test data: a French ("hello everyone") auto-generated transcript and a
    # Spanish ("hello all friends") manual one; the manual one must be chosen.
    tube["api"].listing = [FakeTranscript(Fetched([Snippet("bonjour tout le monde " * 12, 0, 3)], "fr", True), True),
                           FakeTranscript(Fetched([Snippet("hola a todos amigos " * 12, 0, 3)], "es", False), False)]
    tube["say"](f"summarise this video {VID}")
    assert tube["groq"].summaries[0][0].startswith("hola")  # a manual one first


def test_empty_list_says_no_transcript_and_groq_is_not_called(tube) -> None:
    """Empty list says no transcript and Groq is not called."""
    tube["api"].error = yta.NoTranscriptFound(VID, ["en"], None)
    assert tube["say"](f"summarise this video {VID}") == "This video has no transcript, so I can't summarise it."
    assert tube["groq"].summaries == []


@pytest.mark.parametrize("error, reply_key", [
    (yta.TranscriptsDisabled(VID), "no_transcript"),
    (yta.VideoUnavailable(VID), "unavailable"),
    (yta.VideoUnplayable(VID, "private", []), "unplayable"),
    (yta.AgeRestricted(VID), "age_restricted"),
    (yta.InvalidVideoId(VID), "bad_link"),
    (yta.RequestBlocked(VID), "blocked"),
    (yta.IpBlocked(VID), "blocked"),
    (yta.PoTokenRequired(VID), "po_token"),
    (yta.YouTubeDataUnparsable(VID), "youtube_error"),
    (requests.exceptions.ConnectionError("offline"), "cannot_reach"),
])
def test_each_failure_has_its_own_sentence(tube, error, reply_key) -> None:
    """Each failure has its own sentence."""
    tube["api"].error = error
    assert tube["say"](f"summarise this video {VID}") == yt_mod._REPLIES[reply_key]
    assert tube["groq"].summaries == []


def test_youtube_429_cools_down(tube) -> None:
    """YouTube 429 cools down."""
    http = requests.exceptions.HTTPError("429 Client Error: Too Many Requests")
    tube["api"].error = yta.YouTubeRequestFailed(VID, http)
    assert tube["say"](f"summarise this video {VID}").startswith("YouTube's limit for transcripts is reached")
    calls = len(tube["api"].fetch_calls)
    tube["say"](f"summarise this video {VID}")
    assert len(tube["api"].fetch_calls) == calls  # in cooldown: no request


def test_no_groq_key_keeps_the_transcript_and_says_why(tube) -> None:
    """No Groq key keeps the transcript and says why."""
    from jarvis.online import OnlineErrorKind

    tube["groq"].summary_error = OnlineErrorKind.NO_KEY
    assert tube["say"](f"summarise this video {VID}").startswith("I got the transcript, but couldn't summarise it.")


def test_which_video_with_nothing_given(tube) -> None:
    """Which video with nothing given."""
    assert tube["say"]("summarise this video") == yt_mod._REPLIES["which_video"]


# -- chunking -------------------------------------------------------------------------------------------------


def test_30000_chars_is_3_chunks_and_a_combine_with_spacing(tube) -> None:
    """30000 chars is 3 chunks and a combine with spacing."""
    tube["api"].fetched = Fetched(snippets(30000))
    tube["say"](f"summarise this video {VID}")
    assert len(tube["groq"].summaries) == 4
    assert tube["clock"].slept >= 3 * 35  # spaced, clock mocked
    lines = format_record(tube["logbook"].last_completed()).lines
    assert "summarising online: part 3 of 3" in lines and "combining" in lines


def test_60000_chars_summarises_only_4_chunks_and_says_how_much(tube) -> None:
    """60000 chars summarises only 4 chunks and says how much."""
    tube["api"].fetched = Fetched(snippets(60000, seconds=10.0))  # 594 snippets = 99 minutes
    reply = tube["say"](f"summarise this video {VID}")
    assert len(tube["groq"].summaries) == 5  # 4 parts + combine
    assert sum(len(t) for t, _ in tube["groq"].summaries[:4]) <= 48000
    assert reply.endswith("I summarised the first 79 of 99 minutes.")


def test_stop_between_chunks_sends_nothing_more(tube) -> None:
    """Stop between chunks sends nothing more."""
    tube["api"].fetched = Fetched(snippets(30000))
    c = tube["c"]
    original = tube["groq"].summarise

    def stop_after_first(text, instructions, on_going_online=None):
        result = original(text, instructions)
        c.request_stop()
        return result

    tube["groq"].summarise = stop_after_first
    tube["say"](f"summarise this video {VID}")
    assert len(tube["groq"].summaries) == 1


def test_split_text_respects_the_limit() -> None:
    """Split text respects the limit."""
    text = "One sentence here. " * 2000
    chunks = split_text(text, 12000)
    assert all(len(c) <= 12000 for c in chunks) and len(chunks) == 4


def test_summarise_chunks_with_one_chunk_has_no_combine() -> None:
    """Summarise chunks with one chunk has no combine."""
    calls = []
    result = summarise_chunks(["a"], lambda t, i: calls.append(t) or AnswerResult("s", True), "p", "c", lambda: False, sleep=lambda s: None)
    assert calls == ["a"] and result.text == "s"


# -- "about X" -----------------------------------------------------------------------------------------------


def test_about_a_topic_uses_a_search_result_link_only(tube) -> None:
    """About a topic uses a search result link only."""
    tube["groq"].find_urls = ("https://en.wikipedia.org/wiki/Photosynthesis", f"https://www.youtube.com/watch?v={VID}")
    reply = tube["say"]("Summarise the YouTube video about photosynthesis")
    assert tube["groq"].finds == ["YouTube video about photosynthesis"]
    assert tube["api"].fetch_calls[0][0] == VID and reply.startswith("The video explains")
    lines = format_record(tube["logbook"].last_completed()).lines
    assert 'searching online, sending "YouTube video about photosynthesis"' in lines and "found a video on YouTube" in lines


def test_about_a_topic_with_no_video_in_the_results(tube) -> None:
    """About a topic with no video in the results."""
    tube["groq"].find_urls = ("https://en.wikipedia.org/wiki/Photosynthesis",)
    assert tube["say"]("summarise the youtube video about photosynthesis") == \
        "I couldn't find a YouTube video about photosynthesis."
    assert tube["api"].fetch_calls == []


def test_video_links_come_from_the_tools_never_the_model_text() -> None:
    """Video links come from the tools never the model text."""
    message = {"content": "Here: https://www.youtube.com/watch?v=AAAAAAAAAAA",
               "executed_tools": [{"type": "browser_search", "arguments": '{"query": "x"}',
                                   "search_results": {"results": [{"url": f"https://www.youtube.com/watch?v={VID}"}]}}]}
    assert search_result_urls(message) == (f"https://www.youtube.com/watch?v={VID}",)


def test_trust_is_l1_to_play_and_no_new_action_to_summarise() -> None:
    """Trust is L1 to play and no new action to summarise."""
    assert TRUST_TABLE["open_url"] == TrustLevel.L1
    assert "summarise_video" not in TRUST_TABLE


def test_the_session_video_is_never_written_to_jarvis_data(tube, tmp_path) -> None:
    """The session video is never written to JARVIS data."""
    tube["say"](f"summarise this video {VID}")
    written = [p for p in tmp_path.rglob("*") if p.is_file() and p.name != "debug.log"]
    assert not any(VID in p.read_text(encoding="utf-8", errors="ignore") for p in written)


def test_transcript_cut_reports_minutes() -> None:
    """Transcript cut reports minutes."""
    t = Transcript("a" * 100, "en", False, (0.0, 600.0), (50, 49), 1200.0)
    assert t.cut(60) == ("a" * 50, (10, 20))


def test_a_closing_quote_after_a_full_stop_ends_a_sentence() -> None:
    """Live check '…Give You Up.” It describes…' was one
    sentence, so 4 were spoken for a cap of 3."""
    from jarvis.speech.length import limit_sentences

    text = "It repeats “Never Gonna Give You Up.” It describes a promise. It is loyal. It ends."
    assert limit_sentences(text, 3) == "It repeats “Never Gonna Give You Up.” It describes a promise. It is loyal."


def test_a_link_that_is_not_youtube_says_so(tube) -> None:
    """A link that is not YouTube says so."""
    # It got "Which video? Type its link...".
    assert tube["say"]("summarize this video https://www.example.com/abc") == \
        "I couldn't read a YouTube video in that link. Check it and try again."
