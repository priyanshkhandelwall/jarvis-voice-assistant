"""Bridges the controller's worker thread to the dashboard's own thread.

R8/4.2: the dashboard must never do UI work on the controller thread. The
transition listener registered here only pushes onto a bounded, non-
blocking queue -- a fast, constant-time operation -- so a slow or even
completely stalled dashboard consumer can never delay Stop or any other
state change. If the queue is full (the consumer has fallen behind), the
oldest queued message is dropped and a warning is logged; push() never
blocks and never raises.

Only the controller's single worker thread ever calls push() (it is
registered directly as the transition listener), so there is no
concurrent-push race to guard against here -- drain() is the only other
caller, and it belongs to the dashboard's own thread alone.
"""

from __future__ import annotations

import logging
import queue
from typing import List

from jarvis.controller.controller import TransitionLogEntry
from jarvis.settings import SETTINGS

logger = logging.getLogger(__name__)


class DashboardBridge:
    """A thread-safe queue of controller transitions for the dashboard to drain on its own thread (push
    never blocks the controller)."""
    def __init__(self) -> None:
        max_size = int(SETTINGS["dashboard"]["ui_queue_max_size"])
        self._queue: "queue.Queue[TransitionLogEntry]" = queue.Queue(maxsize=max_size)
        self.dropped_count = 0

    def push(self, entry: TransitionLogEntry) -> None:
        """Registered as controller.set_transition_listener. Must stay
        O(1) and non-blocking."""
        try:
            self._queue.put_nowait(entry)
            return
        except queue.Full:
            pass
        try:
            self._queue.get_nowait()  # drop the oldest
            self.dropped_count += 1
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait(entry)
        except queue.Full:
            pass  # extremely unlikely (single-caller invariant), but never raise
        logger.warning("Dashboard UI queue full; dropped the oldest queued message")

    def drain(self) -> List[TransitionLogEntry]:
        """Call from the dashboard's own thread only."""
        items: List[TransitionLogEntry] = []
        while True:
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return items
