"""Live-search recency. On "the score of India vs
West Indies" was answered with the 1 March World Cup match, and no date was
said. Now JARVIS adds today's date to the words sent, reads the result's
date from the page itself, says it, and says when it is old."""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from jarvis.actions.web_query import date_sentence, needs_recency, spoken_date, with_date_note
from jarvis.dashboard.activity_feed import format_record
from jarvis.settings import SETTINGS
from jarvis.speech.qa import first_date_in, result_date

# The F16 test rig (production matcher order, Groq faked at the SDK level).
from test_f16_live_search import BCCI, FakeGroq, VENDUS, _response, fake_groq, rig  # noqa: F401

_WEB = SETTINGS["web"]
TODAY = date(2026, 10, 16)  # the rig's clock


# -- the words sent ------------------------------------------------------------------------


@pytest.mark.parametrize("question", ["what was the score of india vs west indies", "latest news about nasa", "who won last night",
                                      "what happened today", "top headlines"])
def test_recency_questions_ask_for_the_most_recent(question) -> None:
    """Recency questions ask for the most recent."""
    assert needs_recency(question)
    assert with_date_note(question, TODAY) == f"{question} (most recent as of 16 October 2026)"


def test_other_live_questions_still_get_todays_date() -> None:
    """Other live questions still get todays date."""
    q = "what's happening with the mars rover"
    assert not needs_recency(q)
    assert with_date_note(q, TODAY) == f"{q} (today is 16 October 2026)"


def test_spoken_date_format() -> None:
    """Spoken date format."""
    assert spoken_date(date(2026, 3, 1)) == "1 March 2026"


# -- the result's date, read from the page ---------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("IND vs WI: India vs West Indies, Match 52, 1 Mar 2026 - Scorecard ... T20I Sun, 1 Mar 2026", date(2026, 3, 1)),
    ("Published October 14, 2026. Next game Nov 2, 2026", date(2026, 10, 14)),
    ("updated 2026-10-15 09:00", date(2026, 10, 15)),
    ("Fixture: 30 Oct 2026", None),  # a future date is never the result's date
    ("no dates here, 1999 runs", None),
    ("on the 3rd of nothing; 14th October 2026 report", date(2026, 10, 14)),
])
def test_first_date_in_the_page(text, expected) -> None:
    """First date in the page."""
    assert first_date_in(text, TODAY, 3000) == expected


def test_dates_past_the_search_window_are_ignored() -> None:
    """Dates past the search window are ignored."""
    assert first_date_in("x" * 3000 + " 1 Mar 2026", TODAY, 3000) is None


def test_the_page_the_answer_names_wins() -> None:
    """The page the answer names wins."""
    opened = [("https://www.icc-cricket.com/x", "ICC page, 2 Oct 2026"), (BCCI, "Scorecard 14 Oct 2026")]
    assert result_date(opened, "according to the BCCI match centre", TODAY, 3000) == (date(2026, 10, 14), "bcci.tv")
    assert result_date(opened, "the score was 199 for 5", TODAY, 3000) == (date(2026, 10, 2), "icc-cricket.com")
    assert result_date([(BCCI, "no date")], "x", TODAY, 3000) == (None, None)


# -- the sentence she adds -------------------------------------------------------------------


def test_a_fresh_result_says_its_date() -> None:
    """A fresh result says its date."""
    assert date_sentence("score of india vs wi", date(2026, 10, 14), TODAY, "West Indies won.") == "That result is from 14 October 2026."


def test_an_old_result_for_a_latest_question_says_so() -> None:
    """An old result for a latest question says so."""
    assert date_sentence("score of india vs wi", date(2026, 3, 1), TODAY, "India won.") == "The most recent result I found is from 1 March 2026."
    assert date_sentence("latest news", date(2026, 10, 8), TODAY, "x") == "The most recent result I found is from 8 October 2026."  # 8 days
    assert date_sentence("latest news", date(2026, 10, 9), TODAY, "x") == "That result is from 9 October 2026."  # 7 days: still fresh


def test_an_old_result_for_a_non_recency_question_just_gives_the_date() -> None:
    """An old result for a non recency question just gives the date."""
    assert date_sentence("what's happening with the mars rover", date(2026, 3, 1), TODAY, "x") == "That result is from 1 March 2026."


def test_no_repeat_when_the_answer_already_says_the_date() -> None:
    """No repeat when the answer already says the date."""
    assert date_sentence("score", date(2026, 10, 14), TODAY, "On 14 October, West Indies won.") is None
    assert date_sentence("score", date(2026, 10, 14), TODAY, "On Oct 14th, West Indies won.") is None


def test_no_date_on_the_page_is_said_plainly() -> None:
    """No date on the page is said plainly."""
    assert date_sentence("score", None, TODAY, "x") == _WEB["no_date_text"]


# -- end to end ------------------------------------------------------------------------------------


def _tools_with_page(text):
    search = SimpleNamespace(type="browser_search", arguments=json.dumps({"query": "India vs West Indies T20 score"}),
                             output="", search_results=SimpleNamespace(results=[SimpleNamespace(url=BCCI, title="Scorecard")]))
    page = SimpleNamespace(type="browser.open", arguments=json.dumps({"id": 0}), output=f"URL:\n{BCCI}\n{text}",
                           search_results=SimpleNamespace(results=[]))
    return [search, page]


def test_old_match_is_flagged_end_to_end(rig) -> None:
    """Old match is flagged end to end."""
    FakeGroq.replies = [_response(content="India scored 199 for 5 and won by 5 wickets, according to bcci.tv.",
                                  tools=_tools_with_page("IND vs WI, Match 52, 1 Mar 2026 - Scorecard"))]
    rig.say(VENDUS)
    lines = format_record(rig.logbook.last_completed()).lines
    assert lines[2] == 'searching online, sending "what was the score of india vs. west indies t20 (most recent as of 16 October 2026)"'
    assert "result date: 1 March 2026 (bcci.tv)" in lines
    assert rig.speech.texts[-1].endswith("according to bcci.tv. The most recent result I found is from 1 March 2026.")


def test_page_without_a_date_says_so_end_to_end(rig) -> None:
    """Page without a date says so end to end."""
    FakeGroq.replies = [_response(content="West Indies won, according to bcci.tv.", tools=_tools_with_page("Scorecard, no date shown"))]
    rig.say(VENDUS)
    assert "result date: not shown on the page" in format_record(rig.logbook.last_completed()).lines
    assert rig.speech.texts[-1].endswith(_WEB["no_date_text"])


def test_plain_questions_get_no_date_note(rig) -> None:
    """Plain questions get no date note."""
    FakeGroq.replies = [_response(content="Photosynthesis is how plants make food.", tools=[], total=600)]
    rig.say("What is photosynthesis")
    assert FakeGroq.calls[0]["messages"][1]["content"] == "what is photosynthesis"
