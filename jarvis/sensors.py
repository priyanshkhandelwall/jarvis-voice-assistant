"""Which capture sensors are on right now, for the dashboard's Sensor
Array (F8's Screen Capture row, F9's Camera row). R15: capture loops are
off unless asked for, and the row is on only while a capture runs.

Thread-safe; the capture code turns a sensor on with `active(name)` and the
dashboard's refresh loop reads `is_on(name)`. A capture can be shorter
than one refresh tick, so a sensor reads as on for at least
[dashboard] sensor_on_min_seconds after it started, so it is never missed.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Callable, Dict, Iterator

from jarvis.settings import SETTINGS

SCREEN = "screen"
CAMERA = "camera"


class SensorFlags:
    """The Sensor Array rows (camera, screen capture): on only while a capture runs, and shown for at least
    a moment so it can be seen."""
    def __init__(self, min_seconds: float | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._count: Dict[str, int] = {}
        self._last_on: Dict[str, float] = {}
        self._min = float(SETTINGS["dashboard"]["sensor_on_min_seconds"] if min_seconds is None else min_seconds)
        self._clock = clock

    # Generators: a generator-based context manager; `finally` after `yield` always turns the sensor off.
    @contextmanager
    def active(self, name: str) -> Iterator[None]:
        """Context manager: the sensor is on inside the `with` block, and off again even if it raises."""
        self.turn_on(name)
        try:
            yield
        finally:
            self.turn_off(name)

    def turn_on(self, name: str) -> None:
        """Mark the sensor as in use (counted, so overlapping uses nest)."""
        with self._lock:
            self._count[name] = self._count.get(name, 0) + 1
            self._last_on[name] = self._clock()

    def turn_off(self, name: str) -> None:
        """End one use of the sensor."""
        with self._lock:
            self._count[name] = max(0, self._count.get(name, 0) - 1)

    def is_on(self, name: str) -> bool:
        """True while the sensor is in use, or was in the last min_seconds."""
        with self._lock:
            if self._count.get(name, 0) > 0:
                return True
            started = self._last_on.get(name)
            return started is not None and self._clock() - started < self._min

    def really_on(self, name: str) -> bool:
        """On right now (tests, R15 checks), without the display minimum."""
        with self._lock:
            return self._count.get(name, 0) > 0
