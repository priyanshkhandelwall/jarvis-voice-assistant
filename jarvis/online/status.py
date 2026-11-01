"""The LOCAL / ONLINE / LIMITED indicator (R3, F13, F6's Internet panel).

One shared, thread-safe object. Online services mark themselves busy while
a request is really in flight, and record a rate-limit cooldown when a
free tier says "no more for now" (R1). The dashboard only reads it, and
subscribes with add_listener() so the Internet panel changes the moment
the status does, not on its next refresh tick.

A request on its own is often under a second, which falls in the silence
between "Checking online" and the spoken answer -- seen on the laptop as
the panel never leaving LOCAL. So a feature can also hold() ONLINE for
the whole exchange it started (F2: from "Checking online" until the
answer finishes speaking) and release() it when that is over.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable, Iterator, List, Optional

logger = logging.getLogger(__name__)

# F16b's browser holds this service while its window is open.
BROWSER_SERVICE = "browser"


class ConnectionMode(Enum):
    """F13: LOCAL (nothing online), ONLINE (a call is running) or LIMITED (a service is cooling down)."""
    LOCAL = auto()  # nothing is going online right now
    ONLINE = auto()  # at least one request is in flight
    LIMITED = auto()  # a service is in its rate-limit cooldown


@dataclass(frozen=True)
class OnlineSnapshot:
    """The indicator's state at one moment."""
    mode: ConnectionMode
    service: Optional[str] = None  # the service that is online / limited
    limited_until: Optional[float] = None  # wall-clock time (time.time()) the cooldown ends
    browser_open: bool = False  # F16b: JARVIS's own browser is open ("browser in use")


class OnlineStatus:
    """ONLINE wins over LIMITED: if one service is cooling down while
    another is talking to the internet, the dashboard must still show that
    something is going online right now (R3)."""

    def __init__(self, wall_clock: Callable[[], float] = time.time) -> None:
        self._lock = threading.Lock()
        self._wall_clock = wall_clock
        self._active: dict[str, int] = {}
        self._limited_until: dict[str, float] = {}
        self._held: dict[str, int] = {}
        self._listeners: List[Callable[[OnlineSnapshot], None]] = []

    def add_listener(self, listener: Callable[[OnlineSnapshot], None]) -> None:
        """Called with the new snapshot after every change, on the thread that
        made it. A listener that raises is logged and never breaks the
        online call that triggered it."""
        with self._lock:
            self._listeners.append(listener)

    def _notify(self) -> None:
        # Never called while holding self._lock: a listener takes the
        # dashboard's ui_lock, and the dashboard calls snapshot() while
        # holding ui_lock, so holding both here could deadlock.
        with self._lock:
            listeners = list(self._listeners)
        snapshot = self.snapshot()
        for listener in listeners:
            try:
                listener(snapshot)
            except Exception:
                logger.exception("OnlineStatus listener raised")

    def hold(self, service: str) -> None:
        """Keep showing ONLINE for `service` until release(); counted, so
        overlapping holds each need their own release."""
        with self._lock:
            self._held[service] = self._held.get(service, 0) + 1
        self._notify()

    def release(self, service: str) -> None:
        """A call to `service` has finished."""
        with self._lock:
            if service not in self._held:
                return
            self._held[service] -= 1
            if self._held[service] <= 0:
                del self._held[service]
        self._notify()

    @contextmanager
    def online(self, service: str) -> Iterator[None]:
        """Mark `service` as going online for the duration of the block."""
        with self._lock:
            self._active[service] = self._active.get(service, 0) + 1
        self._notify()
        try:
            yield
        finally:
            with self._lock:
                self._active[service] -= 1
                if self._active[service] <= 0:
                    del self._active[service]
            self._notify()

    def set_limited(self, service: str, until_wall: float) -> None:
        """`service` is rate-limited until `until_wall` (wall-clock seconds)."""
        with self._lock:
            self._limited_until[service] = until_wall
        self._notify()

    def snapshot(self) -> OnlineSnapshot:
        """The current mode, for the dashboard."""
        now = self._wall_clock()
        with self._lock:
            busy = self._active or self._held
            if busy:
                browser = BROWSER_SERVICE in self._active or BROWSER_SERVICE in self._held
                return OnlineSnapshot(ConnectionMode.ONLINE, service=next(iter(busy)), browser_open=browser)
            for service, until in list(self._limited_until.items()):
                if until <= now:
                    del self._limited_until[service]
            if self._limited_until:
                service, until = min(self._limited_until.items(), key=lambda item: item[1])
                return OnlineSnapshot(ConnectionMode.LIMITED, service=service, limited_until=until)
        return OnlineSnapshot(ConnectionMode.LOCAL)
