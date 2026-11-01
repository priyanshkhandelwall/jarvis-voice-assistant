"""Real voice commands (F20 tracking, a
swear used as a compliment, a misheard camera price), with his EXACT words. Side
effects mocked as in test_regressions_core_rig."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jarvis.actions.command_text import normalize
from jarvis.news.topics import TopicStore, same_topic, split_topics
from jarvis.router.router import IntentRouter, heard_as
from jarvis.settings import SETTINGS
from jarvis.speech.rudeness import RudenessFilter
from jarvis.vision.detector import Detection
from jarvis.voice.listener import VoiceListener, fragment_reason
from test_regressions_core_rig import PERSON, jarvis  # noqa: F401 -- the fixture

THANK_YOU = [str(x).format(name=SETTINGS["user"]["name"]) for x in SETTINGS["small_talk"]["compliment_replies"]]


def _local(rig) -> None:
    assert rig["groq"].questions == [] and rig["classifier"].calls == []


def _tracking(rig, *topics) -> None:
    for topic in topics:
        assert rig["say"](f"track {topic}") == f"Tracking {topic}."


def _tracked(rig):
    return TopicStore(rig["memory"].parent / "topics.json").names()


# 1. no near-duplicates -------------------------------------------------------------------------------


def test_track_indian_cricut_is_the_indian_cricket_already_tracked(jarvis) -> None:  # noqa: F811
    """Track indian cricut is the indian cricket already tracked."""
    _tracking(jarvis, "indian cricket")
    assert jarvis["say"]("Track, Indian Cricut") == "I'm already tracking indian cricket."
    assert jarvis["say"]("track indian gritted") == "I'm already tracking indian cricket."
    assert _tracked(jarvis) == ["indian cricket"]


@pytest.mark.parametrize("said, same", [("indian cricut", True), ("indian gritted", True), ("Indian Cricket", True),
                                        ("indian hockey", False), ("english cricket", False), ("cricket", False)])
def test_same_topic_is_word_by_word_with_whispers_slips(said, same) -> None:
    """Same topic is word by word with whispers slips."""
    assert same_topic(said, "indian cricket") is same


# 2. "tracking" misheard or glued ----------------------------------------------------------------------


def test_what_i_am_precking_lists_the_topics_locally(jarvis) -> None:  # noqa: F811
    """What i am precking lists the topics locally."""
    _tracking(jarvis, "indian cricket")
    assert jarvis["say"]("What I am Precking") == "You're tracking one topic: indian cricket."
    _local(jarvis)


def test_mishearings_are_data() -> None:
    """Mishearings are data."""
    assert heard_as("stop trackingindian gritted") == "stop tracking indian gritted"
    assert heard_as("what i am precking") == "what i am tracking" and heard_as("what am i trekking") == "what am i tracking"
    assert IntentRouter().decide("What I am Precking")[1][0].intent.name == "topics_list"


def test_stop_trackingindian_gritted_removes_indian_cricket(jarvis) -> None:  # noqa: F811
    """Stop trackingindian gritted removes indian cricket."""
    _tracking(jarvis, "indian cricket", "stock market")
    assert jarvis["say"]("stop trackingindian gritted") == "Stopped tracking indian cricket."
    assert _tracked(jarvis) == ["stock market"]
    _local(jarvis)


# 3. the answer to "which one?" -------------------------------------------------------------------------


@pytest.mark.parametrize("answer", ["Second", "the second one", "number 2", "2", "Stop tracking the second one", "stock markit"])
def test_which_one_is_answered_from_the_list(jarvis, answer) -> None:  # noqa: F811
    """Which one is answered from the list."""
    _tracking(jarvis, "indian cricket", "stock market")
    assert jarvis["say"]("Stop tracking").endswith("Which one should I stop tracking?")
    assert jarvis["say"](answer) == "Stopped tracking stock market."
    assert _tracked(jarvis) == ["indian cricket"]


def test_second_is_never_dropped_as_too_short_while_asked(jarvis) -> None:  # noqa: F811
    """Second is never dropped as too short while asked."""
    _tracking(jarvis, "indian cricket", "stock market")
    jarvis["say"]("Stop tracking")
    assert fragment_reason("Second", 0.0) == "too short"  # alone it would be dropped ...
    listener = SimpleNamespace(_pick_pending=jarvis["orch"].picks.active)
    assert VoiceListener._is_pick(listener, "Second")  # ... but the question makes it a pick


# 4. several at once --------------------------------------------------------------------------------------


def test_stop_tracking_indian_gritted_and_indian_cricut(jarvis) -> None:  # noqa: F811
    """Stop tracking indian gritted and indian cricut."""
    _tracking(jarvis, "indian cricket", "stock market")
    assert jarvis["say"]("stop tracking indian gritted and indian cricut") == "Stopped tracking indian cricket."
    assert _tracked(jarvis) == ["stock market"]


def test_stop_tracking_two_topics_says_both(jarvis) -> None:  # noqa: F811
    """Stop tracking two topics says both."""
    _tracking(jarvis, "indian cricket", "stock market", "formula one")
    assert jarvis["say"]("stop tracking indian cricut, stock market and the weather") == (
        "Stopped tracking indian cricket and stock market. I'm not tracking weather.")
    assert _tracked(jarvis) == ["formula one"]


def test_split_topics() -> None:
    """Split topics."""
    assert split_topics("a b, c d and e f") == ["a b", "c d", "e f"]


# 6. a sworn compliment -------------------------------------------------------------------------------------


@pytest.mark.parametrize("said", ["You are damn awesome", "that's damn great"])
def test_a_swear_before_praise_is_a_compliment(jarvis, said) -> None:  # noqa: F811
    """A swear before praise is a compliment."""
    jarvis["orch"]._rudeness = RudenessFilter(SETTINGS["rudeness"]["words"])
    assert jarvis["say"](said) in THANK_YOU
    _local(jarvis)


@pytest.mark.parametrize("said", ["you are damn useless", "you are damn stupid"])
def test_a_swear_before_an_insult_is_still_rude(said) -> None:
    """A swear before an insult is still rude."""
    assert RudenessFilter(SETTINGS["rudeness"]["words"]).check(said).rude


# 7. "the portal" after a look at a bottle --------------------------------------------------------------------


def test_how_much_does_the_portal_cost_after_seeing_a_bottle(jarvis) -> None:  # noqa: F811
    """How much does the portal cost after seeing a bottle."""
    jarvis["detector"].frames = [[Detection("person", 0.9, PERSON), Detection("bottle", 0.9, (40, 200, 120, 400))]] * 3
    jarvis["vision"]._prices = None  # the real price table
    assert jarvis["say"]("What can you see") == "I can see a person and a bottle."
    assert jarvis["say"]("How much does the portal cost") == "That looks like a bottle. Roughly $2 to $6."
    _local(jarvis)


def test_an_unknown_word_with_two_things_seen_is_not_guessed(jarvis) -> None:  # noqa: F811
    """An unknown word with two things seen is not guessed."""
    jarvis["detector"].frames = [[Detection("bottle", 0.9, (40, 200, 120, 400)), Detection("cup", 0.9, (300, 200, 380, 400))]] * 3
    jarvis["say"]("What can you see")
    assert "bottle" not in jarvis["say"]("How much does the portal cost")


def test_normalize_keeps_second() -> None:
    """Normalize keeps second."""
    assert normalize("Second.") == "second"
