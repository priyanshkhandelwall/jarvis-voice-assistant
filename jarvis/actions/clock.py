"""The time and the date, answered from the laptop clock
(L0), never online. "Tell me the current time please" and
"What is the Sydney time right now" went to Groq, which said it can't tell
the time.

Another city's time uses the Windows time-zone name in [clock.cities],
read from Windows' own registry through pywin32's win32timezone -- no
download, daylight saving included. Without pywin32 (R14) she says what
to install; a city that isn't listed gets the setting to add it to.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Callable, Optional

from jarvis.interpreter import Answer
from jarvis.platform import setup_message
from jarvis.settings import SETTINGS

_CFG = SETTINGS["clock"]
_CITIES = {" ".join(str(k).lower().split()): str(v) for k, v in _CFG.get("cities", {}).items()}
_MESSAGES = SETTINGS["setup_messages"]

# Matched against the cleaned command (no "please", "right now", "can you").
# Whisper's "Watch today's date" for "What's today's date".
_WHAT = r"(?:what(?:'s| is)?|whats|watch|wats|tell me|do you know|say|give me)"
_TIME_PATTERNS = [
    re.compile(rf"^(?:{_WHAT} )?(?:the )?(?:current |local )?time(?: is it)?(?: (?:in|at) (?P<city>[a-z ]+?))?(?: now| today)?$"),
    re.compile(rf"^(?:{_WHAT} )?(?:the )?(?:current |local )?time(?: (?:is it|it is))(?: (?:in|at) (?P<city>[a-z ]+?))?$"),
    re.compile(rf"^what time(?: is it| it is)?(?: (?:in|at) (?P<city>[a-z ]+?))?(?: now)?$"),
    re.compile(rf"^(?:{_WHAT} )?(?:the )?(?:current )?(?P<city>[a-z ]+?) time(?: now)?$"),
]
_DATE_PATTERNS = [
    re.compile(rf"^(?:{_WHAT} )?(?:the |today's |todays )?(?:current )?date(?: today| is it| is it today)?$"),
    re.compile(r"^what(?:'s| is)? today(?:'s date)?$"),
    re.compile(r"^(?:what day is it|what day is today|which day is it|what day it is)(?: today)?$"),
    re.compile(rf"^(?:{_WHAT} )?(?:the )?day(?: today)?$"),
    # "Which day of the week is it?" went online.
    re.compile(r"^(?:what|which)(?:'s| is)? (?:the )?day of the week(?: is it| it is)?(?: today)?$"),
]
# "What year is it?" went online ("this is the current year on your laptop").
_YEAR_PATTERNS = [
    re.compile(rf"^(?:{_WHAT} |which )?(?:the )?(?:current )?year(?: is it| it is| are we in| is this)?(?: now| today)?$"),
]
_DATE_AND_TIME = re.compile(rf"^(?:{_WHAT} )?(?:the |today's |todays )?(?:current )?(?:date and (?:the )?time|time and (?:the )?date)"
                            r"(?: now| today| is it| please)?$")
_NOT_A_CITY = {"current", "local", "the", "what", "whats", "what's", "exact", "right", "real"}
# "Got the time?" was "the time zone for Got The". An unknown
# "city" with one of these words in it is just a way of asking the time.
_NOT_CITY_WORDS = _NOT_A_CITY | {"got", "have", "has", "you", "your", "i", "me", "a", "an", "it", "is", "know", "tell", "say", "any",
                                 "please", "so", "ok", "okay", "hey", "um", "uh"}


def _clock_text(moment: datetime) -> str:
    if bool(_CFG["use_24_hour"]):
        return moment.strftime("%H:%M")
    return moment.strftime("%I:%M %p").lstrip("0")


def _city_time(city: str, now_utc: datetime) -> str:
    zone_name = _CITIES.get(city)
    if zone_name is None:
        return str(_MESSAGES["clock_city_unknown"]).format(city=city.title())
    try:
        import win32timezone
    except ImportError:
        return setup_message("clock_timezones_missing")
    try:
        moment = now_utc.astimezone(win32timezone.TimeZoneInfo(zone_name))
    except Exception:
        return str(_MESSAGES["clock_city_unknown"]).format(city=city.title())
    return f"It's {_clock_text(moment)} in {city.title()}, on {moment.strftime('%A')}."


class ClockMatcher:
    """Answers time and date questions from the laptop's clock, locally, including a named city's time
    zone."""
    def __init__(self, now: Callable[[], datetime] = lambda: datetime.now().astimezone()) -> None:
        self._now = now

    def __call__(self, normalized_text: str) -> Optional[Answer]:
        text = " ".join(re.sub(r"[^\w\s']", " ", normalized_text.lower()).split())
        now = self._now()
        if _DATE_AND_TIME.match(text):
            # "What's the date and time" was "the time zone for Date And".
            return Answer(f"Today is {now.strftime('%A')}, {now.day} {now.strftime('%B %Y')}, and it's {_clock_text(now)}.")
        for pattern in _TIME_PATTERNS:
            m = pattern.match(text)
            if m is None:
                continue
            city = (m.group("city") or "").strip()
            # A word is a city only when it is a known one, or comes after "in"/"at".
            after_in = city and re.search(rf"\b(?:in|at) {re.escape(city)}\b", text) is not None
            if city and city not in _CITIES and not after_in:
                return Answer(f"It's {_clock_text(now)}.")  # "got the time": still the time, here
            if city and (city in _CITIES or not set(city.split()) & _NOT_CITY_WORDS):
                return Answer(_city_time(city, now))
            return Answer(f"It's {_clock_text(now)}.")
        for pattern in _YEAR_PATTERNS:
            if pattern.match(text):
                return Answer(f"It's {now.year}.")
        for pattern in _DATE_PATTERNS:
            if pattern.match(text):
                if "day" in text and "date" not in text:
                    return Answer(f"It's {now.strftime('%A')}, {now.day} {now.strftime('%B')}.")
                return Answer(f"Today is {now.strftime('%A')}, {now.day} {now.strftime('%B %Y')}.")
        return None
