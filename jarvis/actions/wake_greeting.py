"""The wake greeting (redesigned): short, warm, personal,
time-aware and varied -- one line picked at random, locally (never online),
from [greeting.pools] in settings.toml.

Which pool, first match wins:
1. welcome_back -- woken within [greeting] welcome_back_minutes of going
   to sleep;
2. late_night   -- 21:00-04:59 (a gentle line, even on the first wake);
3. first_wake   -- the first wake of the day (a day starts at
   early_morning_from, so 00:30 still belongs to the night before);
4. the time band: early_morning, morning, afternoon or evening.

None of the last [greeting] no_repeat_count greetings is said again. The
pools hold 2-3 lines each, so when all of a pool's lines were said
recently the time-neutral `anytime` pool fills in. A missing or empty pool
gives [greeting] fallback.

Remembered in [greeting] state_path (data folder, R18) so variety and
"first wake of the day" survive a restart: the last few greetings, the day
of the last wake, and when she last went to sleep. Nothing she heard.
"""

from __future__ import annotations

import json
import logging
import random
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

from jarvis.settings import SETTINGS

logger = logging.getLogger(__name__)

_CFG = SETTINGS["greeting"]
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

WELCOME_BACK = "welcome_back"
LATE_NIGHT = "late_night"
FIRST_WAKE = "first_wake"
ANYTIME = "anytime"


def default_state_path() -> Path:
    """Where the greeting state (last line used, last day) is saved in jarvis_data."""
    return _PROJECT_ROOT / str(_CFG["state_path"])


def time_band(hour: int, cfg: Optional[dict] = None) -> str:
    """The pool name for this hour of the day."""
    cfg = cfg or _CFG
    if hour >= int(cfg["late_night_from"]) or hour < int(cfg["early_morning_from"]):
        return LATE_NIGHT
    if hour < int(cfg["morning_from"]):
        return "early_morning"
    if hour < int(cfg["afternoon_from"]):
        return "morning"
    if hour < int(cfg["evening_from"]):
        return "afternoon"
    return "evening"


@dataclass
class _State:
    """The saved wake state: which greeting-day was last used and when she last slept."""
    day: Optional[str] = None  # the greeting-day of the last wake
    last_sleep: Optional[float] = None  # wall-clock time she last went to sleep
    recent: List[str] = field(default_factory=list)  # newest last


class WakeGreeter:
    """F14: a varied greeting when she wakes (by time of day and how long she slept), never the same line
    twice in a row."""
    def __init__(
        self,
        pools: Optional[Dict[str, List[str]]] = None,
        name: Optional[str] = None,
        now: Callable[[], datetime] = datetime.now,
        rng: Optional[random.Random] = None,
        state_path: Optional[Path] = None,
    ) -> None:
        """`state_path=None` keeps the state in memory only (tests); the app
        passes default_state_path()."""
        self._pools = dict(_CFG.get("pools", {}) if pools is None else pools)
        self._name = str(SETTINGS["user"]["name"] if name is None else name)
        self._now = now
        self._rng = rng or random.Random()
        self._path = state_path
        self._no_repeat = int(_CFG["no_repeat_count"])
        self._welcome_back = timedelta(minutes=float(_CFG["welcome_back_minutes"]))
        self._lock = threading.Lock()
        self._state = self._load()
        # Was the last wake the first of its greeting-day? F20/F21 run their
        # once-a-day work on that wake (no timer, no scheduler).
        self.last_wake_was_first_of_day = False

    # -- what happened -------------------------------------------------------

    def note_sleep(self, when: Optional[datetime] = None) -> None:
        """She just went to sleep (command or auto-sleep)."""
        with self._lock:
            self._state.last_sleep = (when or self._now()).timestamp()
            self._save()

    # -- choosing ------------------------------------------------------------

    def pool_for(self, now: datetime) -> str:
        """Which pool applies right now (see the module docstring)."""
        state = self._state
        if state.last_sleep is not None:
            slept = datetime.fromtimestamp(state.last_sleep)
            if timedelta(0) <= now - slept <= self._welcome_back:
                return WELCOME_BACK
        band = time_band(now.hour)
        if band == LATE_NIGHT:
            return LATE_NIGHT
        if state.day != self._day_of(now):
            return FIRST_WAKE
        return band

    def choose(self) -> str:
        """The greeting for this wake. Records it, so it isn't repeated."""
        with self._lock:
            now = self._now()
            lines = self._lines(self.pool_for(now))
            if not lines:
                text = self._format(str(_CFG["fallback"]))
            else:
                recent = self._state.recent[-self._no_repeat :] if self._no_repeat > 0 else []
                fresh = [line for line in lines if line not in recent]
                if not fresh:
                    fresh = [line for line in self._lines(ANYTIME) if line not in recent]
                if not fresh:
                    # Not enough lines anywhere: the one said longest ago.
                    fresh = [min(lines, key=lambda line: max((i for i, r in enumerate(recent) if r == line), default=-1))]
                text = self._rng.choice(fresh)
            self._state.recent = (self._state.recent + [text])[-max(self._no_repeat, 1) :]
            self.last_wake_was_first_of_day = self._state.day != self._day_of(now)
            self._state.day = self._day_of(now)
            self._save()
            return text

    # -- internals -----------------------------------------------------------

    def _format(self, line: str) -> str:
        return line.replace("{name}", self._name).strip()

    def _lines(self, pool: str) -> List[str]:
        return [self._format(str(line)) for line in (self._pools.get(pool) or []) if str(line).strip()]

    @staticmethod
    def _day_of(now: datetime) -> str:
        """A greeting-day starts at early_morning_from, not midnight."""
        return (now - timedelta(hours=int(_CFG["early_morning_from"]))).date().isoformat()

    def _load(self) -> _State:
        if self._path is None or not self._path.exists():
            return _State()
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return _State(
                day=data.get("day"),
                last_sleep=data.get("last_sleep"),
                recent=[str(r) for r in data.get("recent", [])][-max(self._no_repeat, 1) :],
            )
        except (OSError, ValueError, AttributeError):
            logger.warning("Greeting state unreadable; starting fresh", exc_info=True)
            return _State()

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"day": self._state.day, "last_sleep": self._state.last_sleep, "recent": self._state.recent}),
                encoding="utf-8",
            )
        except OSError:
            logger.warning("Could not save the greeting state (R14: carrying on)", exc_info=True)
