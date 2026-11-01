"""R1's shared online helper: one place that applies the timeout, the
single retry and the rate-limit cooldown to every online call.

A feature hands over two things: the call itself (which receives the
timeout to use) and a classifier that turns that library's own exceptions
into an OnlineErrorKind. Everything about *when* to retry and *when* to
stay off the network lives here, so no feature can get it subtly wrong.

- Retry: at most `max_retries` extra attempts, and only after a timeout or
  a connection failure. Never after a 429 -- retrying a rate limit just
  spends more of a free quota that is already gone.
- 429: the service is switched off until the cooldown ends (the server's
  own retry-after if it sent one, otherwise the settings default). Calls
  during the cooldown raise COOLING without touching the network.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable, Optional, TypeVar

from jarvis.online.status import OnlineStatus

T = TypeVar("T")


class OnlineErrorKind(Enum):
    """Every way an online call can fail; each has its own plain spoken message."""
    NO_KEY = auto()
    KEY_REJECTED = auto()
    LIBRARY_MISSING = auto()
    CANNOT_REACH = auto()
    TIMEOUT = auto()
    RATE_LIMITED = auto()  # a 429 just arrived: say so once (R1)
    COOLING = auto()  # still inside an earlier 429's cooldown: no network call made
    SERVICE_ERROR = auto()
    EMPTY_RESPONSE = auto()
    CUT_OFF = auto()  # stopped at max_tokens before even one sentence ended
    # F16 Stage 1 (live web search), all decided locally before any call:
    BUDGET_USED_UP = auto()  # today's [web] daily_token_budget is spent
    MINUTE_LIMIT = auto()  # too few tokens left this minute, reset too far off
    NO_RESULTS = auto()  # the search ran but found nothing to answer from


_RETRYABLE = (OnlineErrorKind.TIMEOUT, OnlineErrorKind.CANNOT_REACH)


@dataclass(eq=False)  # eq=False keeps the exception hashable, as exceptions are expected to be
class OnlineError(Exception):
    """An online failure, classified, with its detail (kept in the record only) and any cooldown time."""
    kind: OnlineErrorKind
    detail: str = ""
    retry_after_seconds: Optional[float] = None  # only from a 429 that said how long
    until_wall: Optional[float] = None  # RATE_LIMITED / COOLING: when the service is back

    def __str__(self) -> str:
        return f"{self.kind.name}: {self.detail}" if self.detail else self.kind.name


class OnlineService:
    """R1: the one way JARVIS goes online -- a timeout, one retry for transient errors, a cooldown after a
    rate limit, and the online indicator held while it runs."""
    def __init__(
        self,
        name: str,
        status: OnlineStatus,
        timeout_seconds: float,
        max_retries: int,
        cooldown_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.name = name
        self.timeout_seconds = timeout_seconds
        self._status = status
        self._max_retries = max_retries
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._cooling_until: Optional[float] = None  # monotonic

    def cooling_until_wall(self) -> Optional[float]:
        """Wall-clock end of the current cooldown, or None if not cooling."""
        with self._lock:
            if self._cooling_until is None:
                return None
            remaining = self._cooling_until - self._clock()
            if remaining <= 0:
                self._cooling_until = None
                return None
            return self._wall_clock() + remaining

    def call(self, fn: Callable[[float], T], classify: Callable[[Exception], OnlineError]) -> T:
        """Run fn(timeout) under R1's rules. Raises OnlineError on failure."""
        until = self.cooling_until_wall()
        if until is not None:
            raise OnlineError(OnlineErrorKind.COOLING, until_wall=until)

        attempts = 1 + max(0, self._max_retries)
        last_error: Optional[OnlineError] = None
        # Flow control & error handling: a bounded retry loop; every exception becomes a classified OnlineError.
        for attempt in range(attempts):
            with self._status.online(self.name):
                try:
                    return fn(self.timeout_seconds)
                except Exception as exc:  # noqa: BLE001 -- every failure is classified, none escapes raw
                    last_error = exc if isinstance(exc, OnlineError) else classify(exc)
            if last_error.kind == OnlineErrorKind.RATE_LIMITED:
                raise self._start_cooldown(last_error)
            if last_error.kind not in _RETRYABLE or attempt == attempts - 1:
                raise last_error
        raise last_error  # unreachable; keeps type checkers satisfied

    def _start_cooldown(self, error: OnlineError) -> OnlineError:
        seconds = error.retry_after_seconds if error.retry_after_seconds else self._cooldown_seconds
        with self._lock:
            self._cooling_until = self._clock() + seconds
        until_wall = self._wall_clock() + seconds
        self._status.set_limited(self.name, until_wall)
        error.until_wall = until_wall
        return error
