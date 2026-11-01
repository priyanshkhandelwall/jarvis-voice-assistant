"""What the central status pill shows, as a function of the controller's
real state transitions.

The controller can walk a whole command (WAITING -> LISTENING ->
UNDERSTANDING -> SPEAKING -> WAITING) in a few milliseconds. The dashboard
only looks a few times a second, so polling controller.state alone would
mostly see WAITING and never the states in between -- which is what the
pill did. Instead every accepted transition, delivered through
DashboardBridge, is queued here and each state is shown for at least
`min_dwell` seconds, in order. This only paces what is *drawn*; the
controller itself is never delayed (R8), and Stop skips the queue and shows
its result immediately.

Pure logic with an injectable clock, no Flet, so it is testable without a
GUI. Not thread-safe: owned by the dashboard's refresh thread alone.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Callable, Iterable

from jarvis.controller.controller import TransitionLogEntry
from jarvis.controller.states import State


class StateDisplay:
    """Shows each controller state long enough to be seen, even when the real state changes faster than the
    eye can follow."""
    def __init__(
        self,
        initial: State,
        *,
        min_dwell: float,
        max_dwell: float,
        chain_budget: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._shown = initial
        self._min_dwell = min_dwell
        self._max_dwell = max_dwell
        self._chain_budget = chain_budget
        self._clock = clock
        self._shown_since = clock()
        self._pending: deque[State] = deque()
        self._burst = 0  # states queued since the queue was last empty

    def feed(self, entries: Iterable[TransitionLogEntry]) -> None:
        """Queue the states from new controller transitions for display."""
        for entry in entries:
            if not entry.accepted or entry.to_state == entry.from_state:
                continue  # a rejected event changed nothing
            if entry.event_type is None:  # Stop: never queued behind a replay
                self._pending.clear()
                self._burst = 0
                self._show(entry.to_state)
                continue
            if not self._pending:
                self._burst = 0
            self._pending.append(entry.to_state)
            self._burst += 1

    def _dwell(self) -> float:
        """How long each state is held. A short command (WAITING -> LISTENING
        -> UNDERSTANDING -> SPEAKING -> WAITING) gets the readable maximum per
        state; a longer chain (an app launch adds ACTING and VERIFYING) is
        squeezed so the whole walk still fits chain_budget, but never below
        min_dwell, which is about the least a state name can be read in. The
        last state of a burst is where things come to rest, so it needs no
        hold of its own."""
        visible = max(self._burst - 1, 1)
        return min(self._max_dwell, max(self._min_dwell, self._chain_budget / visible))

    def current(self, actual: State) -> State:
        """Advance if the shown state has had its minimum time, then return
        what to draw. `actual` is the controller's real state right now: once
        every queued transition has been shown, the pill re-syncs to it, so a
        dropped bridge message can never leave the pill stale."""
        if self._clock() - self._shown_since >= self._dwell():
            if self._pending:
                self._show(self._pending.popleft())
            elif self._shown != actual:
                self._show(actual)
        return self._shown

    def _show(self, state: State) -> None:
        self._shown = state
        self._shown_since = self._clock()
