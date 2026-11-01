"""F6 Internet panel: turns an OnlineSnapshot into what the panel shows.

Pure formatting, no Flet, so it is testable without a GUI. Colours are
the locked design's own tokens (developer-approved):
LOCAL = cyan, ONLINE = green, LIMITED = amber.

InternetPanelBinding drives the panel from OnlineStatus's change events,
so it switches the moment the status does (A
refresh-tick-only panel was never seen leaving LOCAL).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from jarvis.dashboard import theme
from jarvis.online import ConnectionMode, OnlineSnapshot, OnlineStatus


@dataclass(frozen=True)
class InternetDisplay:
    """What the F13 indicator shows: LOCAL / ONLINE / LIMITED, a detail line and its colour."""
    value: str  # JetBrains Mono, coloured
    detail: str  # small dim line under it
    color: str


def internet_display(snapshot: OnlineSnapshot) -> InternetDisplay:
    """Turn an OnlineStatus snapshot into the indicator's text and colour."""
    if snapshot.mode == ConnectionMode.ONLINE:
        # F16b: JARVIS's own browser is open (same ONLINE, same colour).
        detail = "browser in use" if snapshot.browser_open else "online search in use"
        return InternetDisplay("ONLINE", detail, theme.STATUS_OK)
    if snapshot.mode == ConnectionMode.LIMITED:
        until = datetime.fromtimestamp(snapshot.limited_until).strftime("%H:%M") if snapshot.limited_until else "later"
        return InternetDisplay(f"LIMITED until {until}", "free online limit reached", theme.STATUS_ELEVATED)
    return InternetDisplay("LOCAL", "Nothing is going online", theme.CYAN_ACCENT)


class InternetPanelBinding:
    """Calls `apply` with the new InternetDisplay whenever what the panel
    should show changes, and only then.

    `apply` is called with this binding's lock held, so two threads can't
    deliver changes out of order. It may take the dashboard's ui_lock, so
    never call refresh() while holding ui_lock.
    """

    def __init__(self, status: OnlineStatus, apply: Callable[[InternetDisplay], None]) -> None:
        self._status = status
        self._apply = apply
        self._lock = threading.Lock()
        self._shown: Optional[InternetDisplay] = None
        status.add_listener(self._on_change)

    def _on_change(self, snapshot: OnlineSnapshot) -> None:
        self._show(internet_display(snapshot))

    def refresh(self) -> None:
        """For changes no event announces: a LIMITED cooldown running out."""
        self._show(internet_display(self._status.snapshot()))

    def _show(self, display: InternetDisplay) -> None:
        with self._lock:
            if display == self._shown:
                return
            self._shown = display
            self._apply(display)
