"""R15: dashboard animations pause while the JARVIS window is minimised or
hidden, and resume when it is shown again. Gauges keep sampling.

Flet 1.0.0 (checked in the installed package) reports
this two ways, and both are used:
- `page.window.on_event` delivers WindowEventType MINIMIZE / RESTORE /
  HIDE / SHOW the moment they happen;
- `page.app_visible`, driven by the app-lifecycle events, and the
  `page.window.minimized` property, read on the slow tick as a fallback in
  case an event is missed.

The window counts as hidden if either source says so. Pure state, no Flet,
so it is testable without a GUI; nothing here ever minimises, restores,
moves or focuses the window.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_HIDING = {"MINIMIZE", "HIDE"}
_SHOWING = {"RESTORE", "SHOW", "MAXIMIZE", "UNMAXIMIZE", "FOCUS"}


class WindowVisibility:
    """R15: tracks whether the window is minimised or hidden, so animations pause and save CPU."""
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._event_hidden = False
        self._polled_hidden = False

    def on_window_event(self, event_type) -> None:
        """A Flet WindowEventType (or its name)."""
        name = getattr(event_type, "name", str(event_type)).upper()
        with self._lock:
            before = self._hidden()
            if name in _HIDING:
                self._event_hidden = True
            elif name in _SHOWING:
                self._event_hidden = False
                self._polled_hidden = False  # the window is plainly back
            self._log_change(before)

    def poll(self, minimized: Optional[bool], app_visible: Optional[bool]) -> None:
        """Slow-tick fallback: page.window.minimized, page.app_visible."""
        with self._lock:
            before = self._hidden()
            self._polled_hidden = bool(minimized) or app_visible is False
            self._log_change(before)

    @property
    def hidden(self) -> bool:
        """True while the window is minimised or hidden."""
        with self._lock:
            return self._hidden()

    def _hidden(self) -> bool:
        return self._event_hidden or self._polled_hidden

    def _log_change(self, before: bool) -> None:
        now = self._hidden()
        if now != before:
            logger.info("Dashboard animations %s (window %s)", "paused" if now else "resumed", "hidden or minimised" if now else "shown")
