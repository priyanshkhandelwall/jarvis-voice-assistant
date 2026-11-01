"""The wake greeting (redesigned): varied, time-aware,
context-aware, chosen locally from [greeting.pools], never online."""

from __future__ import annotations

import json
import random
import time
from datetime import datetime, timedelta

import pytest

from jarvis.actions.conversation import Conversation
from jarvis.actions.wake_greeting import WakeGreeter, time_band
from jarvis.controller import Controller, State
from jarvis.dashboard.activity_feed import format_record
from jarvis.interpreter import Interpreter
from jarvis.logbook import Logbook
from jarvis.orchestrator import Orchestrator
from jarvis.settings import SETTINGS
from jarvis.speech import SpeechOutput

NAME = SETTINGS["user"]["name"]
POOLS = SETTINGS["greeting"]["pools"]
DAY = datetime(2026, 10, 16)


def lines(pool: str) -> list[str]:
    """The greeting pool's lines with his name filled in."""
    return [line.replace("{name}", NAME) for line in POOLS[pool]]


class Clock:
    """A clock the test moves by hand (sleep() advances it instantly)."""
    def __init__(self, when: datetime) -> None:
        self.when = when

    def __call__(self) -> datetime:
        return self.when


def greeter(when: datetime, seed: int = 0, **kw) -> tuple[WakeGreeter, Clock]:
    """A WakeGreeter at `when` with a seeded random choice. Returns (greeter, clock)."""
    clock = Clock(when)
    return WakeGreeter(now=clock, rng=random.Random(seed), **kw), clock


def already_woke_today(g: WakeGreeter, clock: Clock) -> None:
    """One earlier wake today, so the next one is not the first of the day."""
    keep = clock.when
    clock.when = keep.replace(hour=10, minute=0)
    g.choose()
    clock.when = keep


@pytest.mark.parametrize(
    "hhmm, band",
    [
        ("04:59", "late_night"), ("05:00", "early_morning"), ("08:59", "early_morning"),
        ("09:00", "morning"), ("11:59", "morning"), ("12:00", "afternoon"),
        ("16:59", "afternoon"), ("17:00", "evening"), ("20:59", "evening"), ("21:00", "late_night"),
    ],
)
def test_band_at_each_boundary(hhmm, band) -> None:
    """Band at each boundary."""
    hour, minute = map(int, hhmm.split(":"))
    assert time_band(hour) == band
    g, clock = greeter(DAY.replace(hour=hour, minute=minute))
    if band != "late_night":
        already_woke_today(g, clock)
    assert g.pool_for(clock.when) == band
    assert g.choose() in lines(band)


def test_first_wake_of_the_day_is_a_fresh_start_line() -> None:
    """First wake of the day is a fresh start line."""
    g, clock = greeter(DAY.replace(hour=9, minute=15))
    assert g.choose() in lines("first_wake")
    clock.when += timedelta(hours=2)
    assert g.choose() in lines("morning")  # not first any more
    clock.when = DAY.replace(hour=9) + timedelta(days=1)
    assert g.choose() in lines("first_wake")  # a new day


def test_a_day_starts_at_five_so_after_midnight_is_still_the_night_before() -> None:
    """A day starts at five so after midnight is still the night before."""
    g, clock = greeter(DAY.replace(hour=23, minute=30))
    assert g.choose() in lines("late_night")
    clock.when = DAY.replace(hour=0, minute=40) + timedelta(days=1)
    assert g.choose() in lines("late_night")  # gentle late-night line, not "fresh start"
    clock.when = DAY.replace(hour=9) + timedelta(days=1)
    assert g.choose() in lines("first_wake")  # the morning after is the first wake


def test_welcome_back_within_30_minutes_of_sleeping() -> None:
    """Welcome back within 30 minutes of sleeping."""
    g, clock = greeter(DAY.replace(hour=14))
    g.choose()
    g.note_sleep(clock.when)
    clock.when += timedelta(minutes=29)
    assert g.pool_for(clock.when) == "welcome_back"
    assert g.choose() in lines("welcome_back")
    g.note_sleep(clock.when)
    clock.when += timedelta(minutes=31)
    assert g.choose() in lines("afternoon")


def test_welcome_back_beats_late_night_and_late_night_beats_first_wake() -> None:
    """Welcome back beats late night and late night beats first wake."""
    g, clock = greeter(DAY.replace(hour=23))
    g.note_sleep(clock.when - timedelta(minutes=5))
    assert g.pool_for(clock.when) == "welcome_back"
    fresh, clock2 = greeter(DAY.replace(hour=2))  # never woken: still late night
    assert fresh.pool_for(clock2.when) == "late_night"


@pytest.mark.parametrize("hour", [6, 10, 14, 19, 23])
def test_never_repeats_any_of_the_last_five_over_50_wakes(hour) -> None:
    """Never repeats any of the last five over 50 wakes."""
    g, clock = greeter(DAY.replace(hour=hour), seed=hour)
    said = []
    for i in range(50):
        if i % 7 == 3:
            g.note_sleep(clock.when)  # mix in some welcome-backs
            clock.when += timedelta(minutes=10)
        else:
            clock.when += timedelta(seconds=30)
        said.append(g.choose())
    for i, text in enumerate(said):
        assert text not in said[max(0, i - 5) : i], (i, text, said[max(0, i - 5) : i + 1])
    assert len(set(said)) >= 6  # actually varied


def test_name_comes_from_settings_and_lines_stay_short() -> None:
    """Name comes from settings and lines stay short."""
    g, _ = greeter(DAY.replace(hour=10), name="Sam")
    assert "Sam" in g.choose() and NAME not in g.choose()  # both first_wake lines name the user
    for pool in POOLS.values():
        for line in pool:
            assert len(line.replace("{name}", NAME).split()) <= 12, line
    assert all("Sam" in line for line in WakeGreeter(name="Sam", now=lambda: DAY.replace(hour=10))._lines("first_wake"))


@pytest.mark.parametrize("pools", [{}, {"first_wake": []}, {"first_wake": ["   "]}])
def test_missing_or_empty_pool_falls_back(pools) -> None:
    """Missing or empty pool falls back."""
    g, _ = greeter(DAY.replace(hour=9), pools=pools)
    assert g.choose() == f"Hello, {NAME}. How can I help?"


def test_variety_and_first_wake_survive_a_restart(tmp_path) -> None:
    """Variety and first wake survive a restart."""
    path = tmp_path / "greeting_state.json"
    g, clock = greeter(DAY.replace(hour=9), state_path=path)
    first = g.choose()
    again, _ = greeter(DAY.replace(hour=10), state_path=path)
    assert again.pool_for(DAY.replace(hour=10)) == "morning"  # not a second "fresh start"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["recent"] == [first] and set(data) == {"day", "last_sleep", "recent"}


def test_unreadable_state_file_starts_fresh(tmp_path) -> None:
    """Unreadable state file starts fresh."""
    path = tmp_path / "greeting_state.json"
    path.write_text("{not json", encoding="utf-8")
    g, _ = greeter(DAY.replace(hour=9), state_path=path)
    assert g.choose() in lines("first_wake")


# -- through the real orchestrator --------------------------------------------------


class FakeSpeech(SpeechOutput):
    """Speech output stand-in: records what she would say; nothing is played."""
    def __init__(self) -> None:
        self.texts: list[str] = []

    def speak(self, text: str) -> None:
        """Fake speak for the tests."""
        self.texts.append(text)


class SpyGroq:
    """Groq stand-in that records questions (should stay empty for local answers)."""
    model = "spy"

    def __init__(self) -> None:
        self.questions: list[str] = []

    def answer(self, question, on_going_online=None):
        """Fake answer for the tests."""
        self.questions.append(question)
        raise AssertionError("the greeting must never go online")


def _wait(pred, timeout=3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not pred():
        time.sleep(0.01)
    return pred()


@pytest.fixture
def rig():
    """Fixture: the components this file's tests drive, with every side effect mocked."""
    made = []

    def _make(when: datetime):
        c = Controller(start_state=State.SLEEPING)
        logbook = Logbook()
        c.set_transition_listener(logbook.on_transition)
        c.start()
        made.append(c)
        interpreter = Interpreter()
        from jarvis.actions.sleep_wake import SleepMatcher

        interpreter.register("sleep", SleepMatcher())
        speech = FakeSpeech()
        clock = Clock(when)
        orch = Orchestrator(c, interpreter, speech, logbook=logbook, greeter=WakeGreeter(now=clock, rng=random.Random(1)))
        groq = SpyGroq()
        Conversation(groq).register(interpreter, orch)
        return c, orch, speech, logbook, groq, clock

    yield _make
    for c in made:
        c.shutdown()


@pytest.mark.parametrize("route", ["wake word", "wake phrase", "wake hotkey", "typed"])
def test_every_route_greets_locally_and_shows_it(rig, route) -> None:
    """Every route greets locally and shows it."""
    c, orch, speech, logbook, groq, clock = rig(DAY.replace(hour=19))
    if route == "typed":
        orch.handle_typed_command("wake up jarvis")
    else:
        assert orch.wake(route)
    assert _wait(lambda: c.state == State.AWAKE)
    assert len(speech.texts) == 1 and speech.texts[0] in lines("first_wake")
    assert groq.questions == []
    assert format_record(logbook.records()[-1]).lines[-1] == f"saying: {speech.texts[0]}"


def test_going_to_sleep_then_waking_soon_is_welcome_back(rig) -> None:
    """Going to sleep then waking soon is welcome back."""
    c, orch, speech, logbook, groq, clock = rig(DAY.replace(hour=15))
    orch.wake("wake word")
    assert _wait(lambda: c.state == State.AWAKE)
    orch.handle_typed_command("go to sleep")
    assert _wait(lambda: c.state == State.SLEEPING)
    clock.when += timedelta(minutes=5)
    orch.wake("wake hotkey")
    assert _wait(lambda: c.state == State.AWAKE)
    assert speech.texts[-1] in lines("welcome_back")
    assert groq.questions == []
