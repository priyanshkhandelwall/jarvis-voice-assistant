"""F2 rudeness: whole-word, case-insensitive list from settings; the insult
is removed so the command can still be carried out."""

from __future__ import annotations

import hashlib

import pytest

from jarvis.settings import SETTINGS
from jarvis.speech.rudeness import RudenessFilter

_FILTER = RudenessFilter(SETTINGS["rudeness"]["words"])


def test_settings_list_is_exactly_the_approved_words() -> None:
    """Settings list is exactly the approved words."""
    # The approved list is checked by its fingerprint so the strong words
    # live only in settings.toml, never in the tests.
    words = SETTINGS["rudeness"]["words"]
    assert len(words) == 20
    assert words[:9] == ["idiot", "stupid", "dumb", "moron", "useless", "shut up", "dumbass", "loser", "pathetic"]
    assert words[-2:] == ["damn", "crap"]
    assert hashlib.sha256("|".join(words).encode()).hexdigest() == (
        "8642c1a63345f05001ee5b45fdb2568554323ee27f93c927c836ae936beeb9e7")
    assert SETTINGS["rudeness"]["reply_text"] == "That wasn't a nice way to say it, but here you go."


@pytest.mark.parametrize(
    "text, cleaned",
    [
        ("open chrome you idiot", "open chrome"),
        ("open chrome, you stupid", "open chrome"),
        ("OPEN CHROME YOU IDIOT", "OPEN CHROME"),
        ("shut up and introduce yourself", "introduce yourself"),
        ("what is photosynthesis, stupid", "what is photosynthesis"),
        ("you're a moron, find my resume", "find my resume"),
        ("open the stupid chrome", "open the chrome"),
        ("open chrome you useless idiot", "open chrome"),
        ("tell me a joke you dumbass", "tell me a joke"),
        ("damn, open notepad", "open notepad"),
    ],
)
def test_rude_words_are_detected_and_removed(text: str, cleaned: str) -> None:
    """Rude words are detected and removed."""
    check = _FILTER.check(text)
    assert check.rude is True
    assert check.cleaned == cleaned


@pytest.mark.parametrize(
    "text",
    ["open chrome", "what is a dumbbell", "the class is skillful", "scrap that", "shuttle up the hill", "crappie fishing"],
)
def test_whole_words_only(text: str) -> None:
    """Whole words only."""
    check = _FILTER.check(text)
    assert check.rude is False
    assert check.cleaned == text


def test_only_rude_words_leaves_nothing() -> None:
    """Only rude words leaves nothing."""
    check = _FILTER.check("you idiot!")
    assert check.rude is True and check.cleaned == ""


def test_longest_phrase_wins() -> None:
    """Longest phrase wins."""
    assert _FILTER.check("dumbass").matched == ("dumbass",)


def test_a_word_containing_a_root_is_rude() -> None:
    """A word containing a root is rude."""
    check = RudenessFilter(["idiot"], roots=["idiot"]).check("open notepad you idiotic bot")
    assert check.rude and "idiotic" in check.matched


def test_empty_list_never_flags() -> None:
    """Empty list never flags."""
    check = RudenessFilter([]).check("you idiot")
    assert check.rude is False and check.cleaned == "you idiot"
