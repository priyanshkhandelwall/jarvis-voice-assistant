"""A second list of real commands (six items), with the exact
words. Side effects mocked as in test_regressions_core_rig."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from jarvis.actions import app_finder
from jarvis.actions.app_finder import ScoredMatch, find_app
from jarvis.actions.clock import ClockMatcher
from jarvis.actions.command_text import clean_command
from jarvis.dashboard.activity_feed import format_record
from jarvis.settings import SETTINGS
from jarvis.speech.rudeness import RudenessFilter
from jarvis.vision.detector import Detection
from jarvis.vision.prices import load_prices
from test_regressions_core_rig import PERSON, jarvis  # noqa: F401 -- the fixture

BOTTLE = Detection("bottle", 0.9, (40, 200, 120, 400))
NOW = datetime(2026, 11, 1, 17, 40, tzinfo=timezone.utc)


def _saw_a_bottle(rig) -> None:
    rig["detector"].frames = [[Detection("person", 0.9, PERSON), BOTTLE]] * 3
    rig["vision"]._prices = None  # the real price table
    assert rig["say"]("What can you see") == "I can see a person and a bottle."


# 1. the bottle's price ----------------------------------------------------------------------------


def test_bottle_price_is_two_to_six_dollars() -> None:
    """Bottle price is two to six dollars."""
    entry = load_prices()["bottle"]
    assert (entry.low, entry.high, entry.currency) == (2, 6, "AUD")


def test_how_much_is_this_bottle(jarvis) -> None:  # noqa: F811
    """How much is this bottle."""
    _saw_a_bottle(jarvis)
    assert jarvis["say"]("How much is this?") == "That looks like a bottle. Roughly $2 to $6."


# 2. date and time ----------------------------------------------------------------------------------


@pytest.mark.parametrize("said", ["What's the date and time", "date and time", "time and date"])
def test_whats_the_date_and_time(said) -> None:
    """Whats the date and time."""
    reply = ClockMatcher(now=lambda: NOW)(clean_command(said)).text
    assert reply.startswith("Today is Sunday, 1 November 2026, and it's ") and "time zone" not in reply


@pytest.mark.parametrize("said, reply", [("what is the sydney time", "in Sydney"), ("what time is it in leeds", "time zone for Leeds"),
                                         ("got the time", "It's ")])
def test_a_city_is_known_or_after_in(said, reply) -> None:
    """A city is known or after in."""
    assert reply in ClockMatcher(now=lambda: NOW)(clean_command(said)).text


# 3. a kind of app ---------------------------------------------------------------------------------


def test_get_the_music_app_going_opens_spotify(jarvis, monkeypatch) -> None:  # noqa: F811
    """Get the music app going opens spotify."""
    apps = [ScoredMatch("notepad", 0.0, "app", "notepad.exe"), ScoredMatch("Spotify", 0.0, "app", r"C:\Start\Spotify.lnk")]
    monkeypatch.setattr(app_finder, "enumerate_installed_apps", lambda store_apps=None: list(apps))
    said = "Get the music app going"
    jarvis["classifier"].answers[said.lower()] = '{"intent": "open_app", "slots": {"rest": "music app"}, "confidence": 0.9}'
    assert jarvis["say"](said) == "Did you mean 'open music app'?"
    assert jarvis["say"]("Yes") == "Opened Spotify."
    assert jarvis["launched"] == [r"C:\Start\Spotify.lnk"]


@pytest.mark.parametrize("kind, app", [("music app", "Spotify"), ("browser", "Google Chrome"), ("notes app", "Notepad"),
                                       ("calculator app", "Calculator"), ("text editor", "Notepad"), ("photos app", "Photos")])
def test_kinds_of_app_are_data(kind, app) -> None:
    """Kinds of app are data."""
    pool = [ScoredMatch(name, 0.0, "app", name) for name in ("Spotify", "Google Chrome", "Notepad", "Calculator", "Photos")]
    assert find_app(kind, pool)[0].display_name == app
    assert app in SETTINGS["app_finder"]["categories"][kind]


def test_a_kind_with_nothing_installed_is_searched_as_before() -> None:
    """A kind with nothing installed is searched as before."""
    assert find_app("music app", [ScoredMatch("Notepad", 0.0, "app", "n")]) == []


# 4. names for what she saw -----------------------------------------------------------------------


def test_how_much_is_the_water_after_seeing_a_bottle(jarvis) -> None:  # noqa: F811
    """How much is the water after seeing a bottle."""
    _saw_a_bottle(jarvis)
    assert jarvis["say"]("How much is the water") == "That looks like a bottle. Roughly $2 to $6."
    assert jarvis["groq"].questions == [] and jarvis["classifier"].calls == []


@pytest.mark.parametrize("word, seen, label", [("drink", ["bottle"], "bottle"), ("coffee", [], "cup"), ("mug", [], "cup"),
                                               ("mobile", [], "cell phone"), ("screen", ["laptop"], "laptop"), ("monitor", [], "tv")])
def test_object_names_are_data(word, seen, label) -> None:
    """Object names are data."""
    from jarvis.vision.commands import fuzzy_label

    assert fuzzy_label(word, seen) == label


# 5. an insult with nothing asked ------------------------------------------------------------------


def test_just_joking_you_are_idiot_stupid_is_answered_locally(jarvis) -> None:  # noqa: F811
    """Just joking you are idiot stupid is answered locally."""
    jarvis["orch"]._rudeness = RudenessFilter(SETTINGS["rudeness"]["words"])
    assert jarvis["say"]("No, I was just joking apart, you are idiot, stupid") == "That wasn't a nice way to say it."
    assert jarvis["groq"].questions == [] and jarvis["classifier"].calls == []


@pytest.mark.parametrize("said, left", [("open notepad you stupid machine", "open notepad machine"),
                                        ("that is a stupid question, what is gravity", "that is a question, what is gravity"),
                                        ("damn, notepad please", "notepad please"), ("this stupid printer never works", "")])
def test_only_a_request_survives_an_insult(said, left) -> None:
    """Only a request survives an insult."""
    assert RudenessFilter(SETTINGS["rudeness"]["words"]).check(said).cleaned == left


# 6. "Yes" after "Did you mean ...?" -----------------------------------------------------------------


def test_yes_shows_the_confirmed_target_not_yes(jarvis) -> None:  # noqa: F811
    """Yes shows the confirmed target not yes."""
    assert jarvis["say"]("Open Cloud") == "Did you mean Claude?"
    jarvis["say"]("Yes")
    lines = format_record(jarvis["logbook"].last_completed()).lines
    assert "searching for Claude" in lines and "searching for Yes" not in lines
