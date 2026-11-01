"""F16 Stage 1: keeping Groq's free plan in budget.

Two limits:
- per day: every Groq call (live and plain) adds its `usage.total_tokens`
  to a local ledger, reset on the local date. Live searches stop once
  [web] daily_token_budget is used. Groq sends no tokens-per-day header,
  so this ledger is the only way to see the day's use.
- per minute: Groq's own `x-ratelimit-remaining-tokens` and
  `x-ratelimit-reset-tokens` headers from the last reply. Before a live
  search, too few tokens left -> wait for the reset if it is short.

The ledger file holds a date and a number, nothing else (R18 data folder).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import date
from pathlib import Path
from typing import Callable, Mapping, Optional

from jarvis.settings import SETTINGS

logger = logging.getLogger(__name__)

_WEB = SETTINGS["web"]
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DURATION = re.compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?$")


def default_usage_path() -> Path:
    """Where the daily token ledger lives in jarvis_data."""
    return _PROJECT_ROOT / str(_WEB["usage_path"])


def parse_reset(value: Optional[str]) -> Optional[float]:
    """Groq's reset headers: "4.777s", "1m26.4s", "28.042s", "250ms"."""
    if not value:
        return None
    m = _DURATION.match(value.strip())
    if m is None or not any(m.groups()):
        return None
    h, mins, s, ms = (float(g) if g else 0.0 for g in m.groups())
    return h * 3600 + mins * 60 + s + ms / 1000


class TokenLedger:
    """F16: counts Groq tokens used per day (numbers only), so she can stop before the free limit."""
    def __init__(
        self,
        path: Optional[Path] = None,
        daily_budget: Optional[int] = None,
        today: Callable[[], date] = date.today,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """`path=None` keeps the count in memory only (tests); the app
        passes default_usage_path()."""
        self._path = path
        self.daily_budget = int(_WEB["daily_token_budget"] if daily_budget is None else daily_budget)
        self._today = today
        self._clock = clock
        self._lock = threading.Lock()
        self._day = today().isoformat()
        self._used = 0
        self._minute_remaining: Optional[int] = None
        self._minute_reset_at: Optional[float] = None
        self._load()

    # -- per day ---------------------------------------------------------------

    def used_today(self) -> int:
        """Tokens used today."""
        with self._lock:
            self._roll()
            return self._used

    def left_today(self) -> int:
        """Tokens left in today's budget."""
        return max(0, self.daily_budget - self.used_today())

    def add(self, tokens: Optional[int]) -> None:
        """Add `tokens` to today's total and save it."""
        if not tokens:
            return
        with self._lock:
            self._roll()
            self._used += int(tokens)
            self._save()

    # -- per minute ------------------------------------------------------------

    def note_headers(self, headers: Mapping[str, str]) -> None:
        """Remember what Groq said is left this minute."""
        try:
            remaining = headers.get("x-ratelimit-remaining-tokens")
            reset = parse_reset(headers.get("x-ratelimit-reset-tokens"))
        except Exception:  # an odd header never breaks an answer
            return
        with self._lock:
            self._minute_remaining = int(remaining) if remaining is not None and str(remaining).isdigit() else None
            self._minute_reset_at = None if reset is None else self._clock() + reset

    def minute_wait(self, needed: int) -> float:
        """Seconds to wait before `needed` tokens are free this minute (0 if
        they are, or if Groq hasn't said)."""
        with self._lock:
            if self._minute_remaining is None or self._minute_reset_at is None:
                return 0.0
            wait = self._minute_reset_at - self._clock()
            if wait <= 0 or self._minute_remaining >= needed:
                return 0.0
            return wait

    # -- file --------------------------------------------------------------------

    def _roll(self) -> None:
        today = self._today().isoformat()
        if today != self._day:
            self._day, self._used = today, 0

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if data.get("date") == self._day:
                self._used = int(data.get("used", 0))
        except (OSError, ValueError, TypeError, AttributeError):
            logger.warning("Groq usage ledger unreadable; starting from 0 today", exc_info=True)

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps({"date": self._day, "used": self._used}), encoding="utf-8")
        except OSError:
            logger.warning("Could not save the Groq usage ledger (R14: carrying on)", exc_info=True)
