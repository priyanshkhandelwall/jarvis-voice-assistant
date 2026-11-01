"""The interface every platform implementation provides."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional, Tuple


class Platform(ABC):
    """Operations that differ between operating systems. Every launch
    method returns True when the operating system accepted the request."""

    name: str = ""
    # What this platform can do at all; a False here means a plain
    # missing-setup sentence instead of the feature, never a crash.
    global_hotkeys = False
    screen_text = False
    window_list = False
    brightness = False

    @abstractmethod
    def installed_apps(self) -> List[Tuple[str, str]]:
        """(display name, launch target) for every installed app found."""

    @abstractmethod
    def find_chrome(self) -> Optional[str]:
        """Chrome's location, or None if it isn't installed."""

    @abstractmethod
    def open_app(self, target: str) -> bool:
        """Launch an installed app."""

    @abstractmethod
    def open_path(self, path: str) -> bool:
        """Open a file or folder with its default app."""

    @abstractmethod
    def open_url(self, url: str, browser: Optional[str]) -> bool:
        """Open a URL in `browser` (Chrome), or the default browser when it is None."""

    @abstractmethod
    def show_in_file_manager(self, path: str) -> bool:
        """Show a file selected in Explorer / Finder without running it."""

    @abstractmethod
    def system_backend(self):
        """The F22 SystemBackend (volume, brightness, Wi-Fi, lock, restart, shutdown)."""

    @staticmethod
    def home() -> Path:
        """The user's home folder."""
        return Path.home()
