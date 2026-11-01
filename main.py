# RUN THIS FILE: JARVIS entry point. See README.txt / README.md for setup.
"""Entry point: `python main.py` opens JARVIS as her own native window (F6).

R14: if flet isn't installed, there is no dashboard to show -- report
that plainly instead of a raw traceback. Everything else (the controller,
F5's log, F7's trust levels) still works and is exercised by `pytest`
even without flet installed.
"""

from __future__ import annotations

import os
import sys


def _relaunch_windowless() -> None:
    """Windows only. `python.exe` is a console-subsystem executable, so it
    always has a console attached: one it inherited from an existing
    terminal, or -- when launched with no parent console (double-click, a
    shortcut, Start-Process) -- a brand new one Windows allocates just for
    it. On Windows 11 that new console is hosted by Windows Terminal, which
    is why launching the dashboard opened a second, fully visible terminal
    window alongside the JARVIS window: every fresh launch got its own
    private console host window, not a leftover from anything explicitly
    spawned in this codebase.

    Tried FreeConsole() first (detach from the console without changing
    executable) -- confirmed by testing it does NOT close a Windows-
    Terminal-hosted console: WT only closes a tab when its process exits,
    not when that process merely detaches, so the window stayed. The
    actual fix is to never allocate a console in the first place, by
    re-executing this same script under `pythonw.exe` (the GUI-subsystem
    build that ships alongside `python.exe` in every venv/install) instead
    of `python.exe`. os.execv() replaces this process in place -- no
    console is ever created for the new one, confirmed by testing. Only
    called after the flet import succeeds, so a missing-dependency user
    still sees the install message below in whatever console they
    launched from, and only when we're not already running under
    pythonw.exe, to avoid re-exec looping."""
    if sys.platform != "win32":
        return
    if os.path.basename(sys.executable).lower() != "python.exe":
        return  # already pythonw.exe (or something else) -- nothing to do
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        return  # best-effort; a stray console window is cosmetic, never fatal
    try:
        os.execv(pythonw, [pythonw, os.path.abspath(__file__), *sys.argv[1:]])
    except OSError:
        pass


def _disable_power_throttling() -> None:
    """Windows only. The reported bug (clock skips seconds -- 10, then 12,
    never 11 -- while JARVIS sits unfocused in the background) matches
    Windows' EcoQoS/process power throttling: an unfocused/background
    process's threads can have their scheduling and timer resolution
    throttled for power saving, so refresh_loop's own time.sleep(0.2)-based
    thread can get woken far less often than requested. Each time it does
    wake, it reads the real current wall-clock time (datetime.now()), which
    has moved by more than a second since the last successful tick -- so
    the display jumps straight to whatever time it is now, skipping the
    seconds in between, rather than the app ever getting a repeat/stuck
    value.

    SetProcessInformation(ProcessPowerThrottling, ...) with StateMask=0
    tells Windows this process opts OUT of execution-speed throttling. This
    only ever touches this process's own scheduling state via a handle to
    itself (GetCurrentProcess()) -- it never focuses, moves, or sends input
    to any window, so it does not conflict with the permanent no-input-
    injection rule. Applied once at startup, before the dashboard window is
    created."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
            _fields_ = [
                ("Version", ctypes.c_ulong),
                ("ControlMask", ctypes.c_ulong),
                ("StateMask", ctypes.c_ulong),
            ]

        PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
        PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
        PROCESS_POWER_THROTTLING_INFORMATION_CLASS = 4  # ProcessPowerThrottling

        state = PROCESS_POWER_THROTTLING_STATE(
            Version=PROCESS_POWER_THROTTLING_CURRENT_VERSION,
            ControlMask=PROCESS_POWER_THROTTLING_EXECUTION_SPEED,
            StateMask=0,  # 0 == not throttled -- opt this process out
        )
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetCurrentProcess()
        kernel32.SetProcessInformation(
            handle,
            PROCESS_POWER_THROTTLING_INFORMATION_CLASS,
            ctypes.byref(state),
            ctypes.sizeof(state),
        )
    except Exception:
        pass  # best-effort; worst case the throttling bug persists, nothing fatal


UNSUPPORTED_SYSTEM_MESSAGE = (
    "JARVIS runs on Windows 10/11 and macOS. Please run it on a Windows PC or a Mac. "
    "A live demo is shown in the Week 13 presentation."
)


def main() -> None:
    """Start JARVIS: check this is Windows or macOS and flet is installed, re-launch without a console
    (Windows), opt out of power throttling (Windows), then open the dashboard."""
    if sys.platform not in ("win32", "darwin"):
        print(UNSUPPORTED_SYSTEM_MESSAGE)
        sys.exit(1)
    try:
        import flet  # noqa: F401
    except ImportError:
        print(
            "The dashboard (F6) needs the 'flet' package, which is not installed.\n"
            "Install it with: pip install -r requirements.txt\n"
            "Everything else in this project still works without it -- run "
            "'pytest' to exercise the controller, log and trust-level engine directly."
        )
        sys.exit(1)

    _relaunch_windowless()
    _disable_power_throttling()

    from jarvis import startup_timing

    startup_timing.mark("python running")
    from jarvis.dashboard import run

    startup_timing.mark("code imported (flet, voice, online libraries)")
    run()


if __name__ == "__main__":
    main()
