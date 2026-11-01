"""The status pill must show every real state transition, in order, each held
long enough to read, and the whole walk must still feel responsive."""

from __future__ import annotations

import time

from jarvis.controller import Controller, Event, EventType, State
from jarvis.controller.controller import TransitionLogEntry
from jarvis.dashboard.bridge import DashboardBridge
from jarvis.dashboard.state_display import StateDisplay

MIN, MAX, BUDGET = 0.15, 0.30, 0.8
_SHORT_CHAIN = [State.LISTENING, State.UNDERSTANDING, State.SPEAKING, State.AWAKE]
_LONG_CHAIN = [State.LISTENING, State.UNDERSTANDING, State.ACTING, State.VERIFYING, State.SPEAKING, State.AWAKE]


class FakeClock:
    """A clock the test moves by hand."""
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _display(clock: FakeClock) -> StateDisplay:
    return StateDisplay(State.AWAKE, min_dwell=MIN, max_dwell=MAX, chain_budget=BUDGET, clock=clock)


def _entry(frm: State, to: State, event=EventType.SPEECH_STARTED, accepted: bool = True) -> TransitionLogEntry:
    return TransitionLogEntry(0.0, frm, to, event, accepted, "r")


def _chain(states: list[State]) -> list[TransitionLogEntry]:
    entries, prev = [], State.AWAKE
    for s in states:
        entries.append(_entry(prev, s, EventType.SPEECH_STARTED))
        prev = s
    return entries


def _walk(d: StateDisplay, clock: FakeClock, actual: State, seconds: float = 3.0):
    """Advance time in 10ms steps; returns [(state, first_seen_at)]."""
    seen = [(d.current(actual), clock.t)]
    while clock.t < seconds:
        clock.t = round(clock.t + 0.01, 3)
        s = d.current(actual)
        if s != seen[-1][0]:
            seen.append((s, clock.t))
    return seen


def test_short_chain_shows_every_state_in_order_and_fits_the_budget() -> None:
    """Short chain shows every state in order and fits the budget."""
    clock = FakeClock()
    d = _display(clock)
    d.feed(_chain(_SHORT_CHAIN))
    seen = _walk(d, clock, State.AWAKE)
    assert [s for s, _ in seen] == [State.AWAKE, *_SHORT_CHAIN]
    times = [t for _, t in seen]
    holds = [b - a for a, b in zip(times[1:], times[2:])]  # LISTENING, UNDERSTANDING, SPEAKING
    assert all(MIN - 0.011 <= h <= MAX + 0.011 for h in holds)
    assert times[-1] - times[1] <= BUDGET + 0.03  # LISTENING first shown -> back at WAITING


def test_short_chain_states_are_held_long_enough_to_read() -> None:
    """Short chain states are held long enough to read."""
    clock = FakeClock()
    d = _display(clock)
    d.feed(_chain(_SHORT_CHAIN))
    seen = _walk(d, clock, State.AWAKE)
    times = [t for _, t in seen]
    assert min(b - a for a, b in zip(times[1:], times[2:])) >= 0.25


def test_long_chain_is_squeezed_to_the_budget_but_never_below_the_minimum() -> None:
    """Long chain is squeezed to the budget but never below the minimum."""
    clock = FakeClock()
    d = _display(clock)
    d.feed(_chain(_LONG_CHAIN))
    seen = _walk(d, clock, State.AWAKE)
    assert [s for s, _ in seen] == [State.AWAKE, *_LONG_CHAIN]
    times = [t for _, t in seen]
    holds = [b - a for a, b in zip(times[1:], times[2:])]
    assert all(h >= MIN - 0.011 for h in holds)
    assert times[-1] - times[1] <= BUDGET + 0.06


def test_a_slow_flow_gets_the_readable_maximum_per_state() -> None:
    """A slow flow gets the readable maximum per state."""
    # Transitions arriving one at a time (a real spoken command): nothing to squeeze.
    clock = FakeClock()
    d = _display(clock)
    d.feed([_entry(State.AWAKE, State.LISTENING)])
    seen = _walk(d, clock, State.LISTENING, seconds=1.0)
    assert [s for s, _ in seen] == [State.AWAKE, State.LISTENING]  # never leaves it: it is the real state


def test_rejected_transitions_are_ignored() -> None:
    """Rejected transitions are ignored."""
    clock = FakeClock()
    d = _display(clock)
    d.feed([_entry(State.AWAKE, State.AWAKE, EventType.SPEECH_FINISHED, accepted=False)])
    clock.t = 1.0
    assert d.current(State.AWAKE) == State.AWAKE


def test_stop_skips_the_queue_immediately() -> None:
    """Stop skips the queue immediately."""
    clock = FakeClock()
    d = _display(clock)
    d.feed(_chain([State.LISTENING, State.UNDERSTANDING]))
    d.feed([_entry(State.UNDERSTANDING, State.AWAKE, event=None)])
    assert d.current(State.AWAKE) == State.AWAKE
    clock.t = 5.0
    assert d.current(State.AWAKE) == State.AWAKE  # nothing stale left queued


def test_resyncs_to_the_real_state_when_nothing_is_queued() -> None:
    """Resyncs to the real state when nothing is queued."""
    clock = FakeClock()
    d = _display(clock)
    clock.t = 1.0
    assert d.current(State.ACTING) == State.ACTING  # e.g. a bridge message was dropped


def test_end_to_end_with_the_real_controller_and_bridge() -> None:
    """End to end with the real controller and bridge."""
    c = Controller(start_state=State.AWAKE)
    bridge = DashboardBridge()
    c.set_transition_listener(bridge.push)
    c.start()
    try:
        c.submit(Event(EventType.SPEECH_STARTED))
        c.submit(Event(EventType.COMMAND_CAPTURED, {"text": "hi"}))
        c.submit(Event(EventType.ANSWER_READY, {"text": "hello"}))
        c.submit(Event(EventType.SPEECH_FINISHED))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and c.transition_count() < 4:
            time.sleep(0.005)
    finally:
        c.shutdown()
    clock = FakeClock()
    d = _display(clock)
    d.feed(bridge.drain())
    seen = _walk(d, clock, c.state)
    assert [s for s, _ in seen] == [State.AWAKE, *_SHORT_CHAIN]
