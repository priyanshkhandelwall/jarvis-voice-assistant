"""Batch-fixes item 1a: native Windows 11 title-bar recoloring via
DWMWA_CAPTION_COLOR (DWM attribute 35). This is reachable even though Flet
1.0 never hands the Python side a window handle -- the same trick Flet's own
`flet_desktop.win_taskbar` module uses to stamp taskbar properties: the
Flet client (`flet.exe`) is a direct child process of this Python process
(started internally by `ft.run()`/`open_flet_view_async`), so its top-level
window can be found by PID + Flutter's own window class name via
EnumWindows, with no change to Flet or its window needed.

Kept out of app.py because it's Windows/DWM-specific ctypes plumbing, not
UI layout -- app.py just calls apply_caption_color_async() once.

Best-effort only: silently gives up on non-Windows platforms, on Windows
builds older than 11 (22000), or if the client window never appears in
time. Never required for the app to run -- this is cosmetic polish on top
of the already-native, already-working title bar (drag/resize/min/max/
close all keep working regardless of whether this succeeds).
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
if sys.platform == "win32":
    from ctypes import wintypes
else:  # not Windows: these calls are never made there
    wintypes = None

logger = logging.getLogger(__name__)

_DWMWA_CAPTION_COLOR = 35
_DWMWA_TEXT_COLOR = 36

# Window class registered by the Flet client's runner (win32_window.cpp) --
# same constant flet_desktop.win_taskbar matches on.
_FLUTTER_WINDOW_CLASS = "FLUTTER_RUNNER_WIN32_WINDOW"
_GW_OWNER = 4


def _colorref(hex_color: str) -> int:
    """Windows COLORREF is 0x00BBGGRR, not the #RRGGBB this codebase's
    theme.py otherwise uses everywhere -- converted here, at the one place
    that needs the Windows byte order, rather than changing theme.py."""
    hex_color = hex_color.lstrip("#")
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    return (b << 16) | (g << 8) | r


def _find_client_pid(timeout: float) -> int | None:
    """The Flet client is a direct child of this process; psutil (already a
    dependency, see gauges.py) is the simplest way to find its PID without
    Flet exposing one."""
    try:
        import psutil
    except ImportError:
        return None
    own = psutil.Process(os.getpid())
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for child in own.children(recursive=True):
            try:
                if child.name().lower().startswith("flet"):
                    return child.pid
            except psutil.Error:
                continue
        time.sleep(0.1)
    return None


def _find_top_window(pid: int, user32: ctypes.WinDLL) -> int | None:
    """Mirrors flet_desktop.win_taskbar._find_top_window: matches by window
    class (not visibility) so a not-yet-shown or FLET_HIDE_WINDOW_ON_START
    window is still found, and the class check keeps a recycled PID from
    ever being matched."""
    found: list[int] = []

    def cb(hwnd: int, _lparam: int) -> bool:
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid and not user32.GetWindow(hwnd, _GW_OWNER):
            buf = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, buf, 64)
            if buf.value == _FLUTTER_WINDOW_CLASS:
                found.append(hwnd)
        return True

    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(cb)
    user32.EnumWindows(enum_proc, 0)
    return found[0] if found else None


def apply_caption_color_async(caption_hex: str, text_hex: str | None = None, timeout: float = 10.0) -> None:
    """Waits (on a daemon thread) for the Flet client's window to appear,
    then recolors its native title bar. Fire-and-forget: callers don't need
    the result, since there is nothing else to do differently either way."""
    if os.name != "nt":
        return

    def worker() -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetClassNameW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]

        pid = _find_client_pid(timeout)
        if pid is None:
            logger.debug("native title bar: could not find the Flet client process (psutil missing or process not seen in time)")
            return

        deadline = time.monotonic() + timeout
        hwnd = None
        while time.monotonic() < deadline:
            hwnd = _find_top_window(pid, user32)
            if hwnd:
                break
            time.sleep(0.1)
        if not hwnd:
            logger.debug("native title bar: client window never appeared")
            return

        # OleDLL auto-raises OSError on a failing HRESULT (e.g. Windows
        # <11/22000, where DWMWA_CAPTION_COLOR doesn't exist) -- same
        # pattern win_taskbar.py uses for its own DWM/shell calls.
        dwmapi = ctypes.OleDLL("dwmapi")
        try:
            caption = ctypes.c_int(_colorref(caption_hex))
            dwmapi.DwmSetWindowAttribute(hwnd, _DWMWA_CAPTION_COLOR, ctypes.byref(caption), ctypes.sizeof(caption))
            if text_hex:
                text_color = ctypes.c_int(_colorref(text_hex))
                dwmapi.DwmSetWindowAttribute(hwnd, _DWMWA_TEXT_COLOR, ctypes.byref(text_color), ctypes.sizeof(text_color))
            logger.info("native title bar: recolored via DWMWA_CAPTION_COLOR")
        except OSError as e:
            logger.info(f"native title bar: DWMWA_CAPTION_COLOR unsupported on this Windows build ({e})")

    threading.Thread(target=worker, daemon=True).start()
