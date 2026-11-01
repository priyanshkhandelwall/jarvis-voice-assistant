"""Shared R12 verification helpers: did launching something actually do
anything? Used by F3 (open_app.py) and F4 (find_file.py) so this logic
exists in exactly one place.

Best-effort and honest about its limit: with pywin32 available, checks
for any new top-level window title that wasn't there before -- a real,
if approximate, signal (a coincidental unrelated window appearing in the
same window would also count as "verified"). Without pywin32 (R14), it
falls back to a coarser "did the process count go up" check.
"""

from __future__ import annotations

import sys
import time
from typing import Callable, Optional, Set, Tuple

WIN32GUI_AVAILABLE = False
if sys.platform == "win32":
    try:
        import win32gui

        WIN32GUI_AVAILABLE = True
    except ImportError:  # R14: degrade, don't crash
        pass


def visible_window_titles() -> Set[str]:
    """The titles of every visible top-level window (pywin32)."""
    titles: Set[str] = set()

    def _collect(hwnd, _extra) -> None:
        if win32gui.IsWindowVisible(hwnd):
            text = win32gui.GetWindowText(hwnd)
            if text:
                titles.add(text)

    win32gui.EnumWindows(_collect, None)
    return titles


def process_count() -> int:
    """How many processes are running (the fallback check when pywin32 isn't installed)."""
    import psutil

    return len(psutil.pids())


def snapshot() -> Tuple[Optional[Set[str]], Optional[int]]:
    """(window titles, process count) before a launch, so the check afterwards can see what changed."""
    if WIN32GUI_AVAILABLE:
        return visible_window_titles(), None
    try:
        return None, process_count()
    except Exception:
        return None, None


def wait_for_new_activity(
    before_titles: Optional[Set[str]], before_count: Optional[int], timeout: float, poll_interval: float
) -> bool:
    """R12: wait up to `timeout` seconds for a new window (or more processes) to appear. True if something
    new appeared."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if WIN32GUI_AVAILABLE and before_titles is not None:
            if visible_window_titles() - before_titles:
                return True
        elif before_count is not None:
            if process_count() > before_count:
                return True
        time.sleep(poll_interval)
    return False


def launch_and_verify_with_retry(
    launch_once: Callable[[], bool], timeout: float, poll_interval: float
) -> Tuple[bool, bool]:
    """Runs `launch_once` (which should attempt the launch and return
    whether the launch call itself succeeded, e.g. no OSError), verifies,
    and retries exactly once on failure (R12). Returns (verified,
    needed_retry). On macOS the check is `open`'s own exit status: it
    fails when the app or file can't be opened."""

    def attempt() -> bool:
        if sys.platform == "darwin":
            return launch_once()
        before_titles, before_count = snapshot()
        if not launch_once():
            return False
        return wait_for_new_activity(before_titles, before_count, timeout, poll_interval)

    if attempt():
        return True, False
    return attempt(), True
