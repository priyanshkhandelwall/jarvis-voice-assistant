"""Tests for F3's app-name resolution (jarvis/actions/app_finder.py).

Scoring/matching logic is tested against an injected candidate list so
these tests don't depend on what's actually in the Start Menu on the
machine running them -- important since that varies machine to machine,
including the tutor's (2.2).
"""

from __future__ import annotations

import pytest

from jarvis.actions.app_finder import (
    MATCH_THRESHOLD,
    ScoredMatch,
    _score,
    find_app,
    find_chrome,
    find_website_fallback,
)

_FAKE_APPS = [
    ScoredMatch("Google Chrome", 0.0, "app", r"C:\Start Menu\Google Chrome.lnk"),
    ScoredMatch("Notepad++", 0.0, "app", r"C:\Start Menu\Notepad++.lnk"),
    ScoredMatch("Visual Studio Code", 0.0, "app", r"C:\Start Menu\Visual Studio Code.lnk"),
    ScoredMatch("Spotify", 0.0, "app", r"C:\Start Menu\Spotify.lnk"),
]


def test_score_exact_match_is_one() -> None:
    """Score exact match is one."""
    assert _score("chrome", "chrome") == 1.0


def test_score_substring_scores_high() -> None:
    """Score substring scores high."""
    assert _score("chrome", "google chrome") >= 0.85


def test_score_unrelated_scores_low() -> None:
    """Score unrelated scores low."""
    assert _score("chrome", "spotify") < MATCH_THRESHOLD


def test_score_empty_query_is_zero() -> None:
    """Score empty query is zero."""
    assert _score("", "chrome") == 0.0


def test_find_app_picks_best_match_first() -> None:
    """Find app picks best match first."""
    results = find_app("chrome", candidates=_FAKE_APPS)
    assert results
    assert results[0].display_name == "Google Chrome"


def test_find_app_returns_nothing_below_threshold() -> None:
    """Find app returns nothing below threshold."""
    results = find_app("completely unrelated xyz", candidates=_FAKE_APPS)
    assert results == []


def test_find_app_respects_limit() -> None:
    """Find app respects limit."""
    results = find_app("o", candidates=_FAKE_APPS, limit=2)
    assert len(results) <= 2


def test_find_app_is_case_insensitive() -> None:
    """Find app is case insensitive."""
    results = find_app("CHROME", candidates=_FAKE_APPS)
    assert results and results[0].display_name == "Google Chrome"


def test_find_website_fallback_matches_youtube() -> None:
    """Find website fallback matches YouTube."""
    match = find_website_fallback("youtube")
    assert match is not None
    assert match.kind == "website"
    assert match.target == "https://www.youtube.com"


def test_find_website_fallback_none_for_unrelated_name() -> None:
    """Find website fallback none for unrelated name."""
    assert find_website_fallback("completely unrelated xyz") is None


def test_find_chrome_returns_a_real_path_or_none() -> None:
    """Find Chrome returns a real path or none."""
    # Whichever it is, it must be an honest answer, not a guess: either a
    # real file that exists, or None (R14 -- never assume it's installed).
    result = find_chrome()
    if result is not None:
        from pathlib import Path

        assert Path(result).is_file()


# -- "open vs code" opened Discord ------------------------

from jarvis.actions.app_finder import confident, shares_a_word  # noqa: E402

# The real Start Menu names involved (scores measured on the laptop:
# "vs code" vs Discord 0.571, vs Steps Recorder 0.571, vs Visual Studio Code 0.560).
_LAPTOP_APPS = [
    ScoredMatch("Discord", 0.0, "app", r"C:\Start Menu\Discord.lnk"),
    ScoredMatch("Steps Recorder", 0.0, "app", r"C:\Start Menu\Steps Recorder.lnk"),
    ScoredMatch("Visual Studio Code", 0.0, "app", r"C:\Start Menu\Programs\Visual Studio Code.lnk"),
    ScoredMatch("Visual Studio Code", 0.0, "app", r"C:\ProgramData\Start Menu\Visual Studio Code.lnk"),
    ScoredMatch("Visual Studio Installer", 0.0, "app", r"C:\Start Menu\Visual Studio Installer.lnk"),
    ScoredMatch("Android Studio", 0.0, "app", r"C:\Start Menu\Android Studio.lnk"),
    ScoredMatch("Task Scheduler", 0.0, "app", r"C:\Start Menu\Task Scheduler.lnk"),
    ScoredMatch("Google Chrome", 0.0, "app", r"C:\Start Menu\Google Chrome.lnk"),
]


def test_the_bug_letter_similarity_alone_ranked_discord_first() -> None:
    """Evidence for the cause: on letters alone, Discord beat VS Code."""
    assert _score("vs code", "Discord") > _score("vs code", "Visual Studio Code")


def test_vs_code_aliases_resolve_to_visual_studio_code() -> None:
    """VS code aliases resolve to visual studio code."""
    for spoken in ("vs code", "VS Code", "vscode", "code", "visual studio code"):
        results = find_app(spoken, candidates=_LAPTOP_APPS)
        assert results[0].display_name == "Visual Studio Code", spoken
        assert results[0].score == 1.0
        assert confident(results), spoken


def test_duplicate_shortcuts_count_as_one_app() -> None:
    """Duplicate shortcuts count as one app."""
    results = find_app("vs code", candidates=_LAPTOP_APPS)
    assert [m.display_name for m in results].count("Visual Studio Code") == 1


@pytest.mark.parametrize(
    "spoken",
    ["vs code", "vscode", "code", "chrome", "google chrome", "studio", "visual studio", "task", "steps", "cord", "record"],
)
def test_nothing_resolves_to_discord_unless_asked(spoken) -> None:
    """Nothing resolves to discord unless asked."""
    results = find_app(spoken, candidates=_LAPTOP_APPS)
    assert all(m.display_name != "Discord" for m in results), (spoken, results)


def test_discord_still_opens_when_asked_for() -> None:
    """Discord still opens when asked for."""
    results = find_app("discord", candidates=_LAPTOP_APPS)
    assert results[0].display_name == "Discord" and confident(results)


def test_an_app_needs_a_word_in_common() -> None:
    """An app needs a word in common."""
    assert shares_a_word("vs code", "Visual Studio Code") is True  # "code"
    assert shares_a_word("vs code", "Discord") is False
    assert shares_a_word("chrome", "Google Chrome") is True
    assert shares_a_word("calc", "Calculator") is True  # 3+ letter prefix
    assert shares_a_word("vs", "Vsomething") is False  # 2-letter words must match whole


def test_close_matches_are_not_confident() -> None:
    """Close matches are not confident."""
    results = find_app("visual studio", candidates=_LAPTOP_APPS)
    names = [m.display_name for m in results]
    assert names[:2] == ["Visual Studio Code", "Visual Studio Installer"]
    assert confident(results) is False  # both 0.85: she must ask


def test_low_best_match_is_not_confident() -> None:
    """Low best match is not confident."""
    assert confident([ScoredMatch("Android Studio", 0.6, "app", "x")]) is False
    assert confident([]) is False
