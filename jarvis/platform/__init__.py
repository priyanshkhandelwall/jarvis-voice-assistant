"""The platform layer: which operating system JARVIS is running on, and the
operations that differ between Windows and macOS.

`current()` returns the implementation for this computer, chosen from
`sys.platform` each time it is called (so tests can simulate a Mac by
setting `sys.platform = "darwin"`). The Windows implementation is the
existing code; the macOS one (`jarvis/platform/mac.py`) uses only standard
macOS tools (`open`, `osascript`, `pmset`, `networksetup`). Neither module
imports a platform-only library when it is loaded.
"""

from __future__ import annotations

import sys

from jarvis.platform.base import Platform


def is_mac() -> bool:
    """True when running on macOS."""
    return sys.platform == "darwin"


def is_windows() -> bool:
    """True when running on Windows."""
    return sys.platform == "win32"


def current() -> Platform:
    """The platform implementation for this computer."""
    if is_mac():
        from jarvis.platform.mac import MacPlatform

        return MacPlatform()
    from jarvis.platform.windows import WindowsPlatform

    return WindowsPlatform()


def setup_message(key: str) -> str:
    """A [setup_messages] sentence. On macOS a `<key>_mac` variant is used
    when settings.toml has one (macOS names different System Settings)."""
    from jarvis.settings import SETTINGS

    messages = SETTINGS["setup_messages"]
    if is_mac() and f"{key}_mac" in messages:
        return str(messages[f"{key}_mac"])
    return str(messages[key])
