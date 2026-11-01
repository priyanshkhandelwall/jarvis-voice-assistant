"""F2 greeting (decision F): only when greeted, by time of day, local."""

from __future__ import annotations

from datetime import datetime

import pytest

from jarvis.actions.greeting import GreetingMatcher, period_of_day


@pytest.mark.parametrize("hour, period", [(0, "morning"), (11, "morning"), (12, "afternoon"), (16, "afternoon"), (17, "evening"), (23, "evening")])
def test_period_boundaries_come_from_settings_defaults(hour: int, period: str) -> None:
    """Period boundaries come from settings defaults."""
    assert period_of_day(hour, 12, 17) == period


@pytest.mark.parametrize("text", ["hello", "hi", "hey jarvis", "good morning", "hello, jarvis!", "good evening"])
def test_greetings_are_answered_by_time_of_day(text: str) -> None:
    """Greetings are answered by time of day."""
    matcher = GreetingMatcher(now=lambda: datetime(2026, 10, 3, 9, 30))
    answer = matcher(text)
    assert answer is not None
    assert answer.text == "Good morning. How can I help?"


def test_reply_follows_the_clock_not_the_words() -> None:
    """Reply follows the clock not the words."""
    matcher = GreetingMatcher(now=lambda: datetime(2026, 10, 3, 19, 0))
    assert matcher("good morning").text == "Good evening. How can I help?"


@pytest.mark.parametrize("text", ["hello world", "what is a hello", "hi there how are you", "open chrome"])
def test_non_greetings_fall_through(text: str) -> None:
    """Non greetings fall through."""
    assert GreetingMatcher()(text) is None
