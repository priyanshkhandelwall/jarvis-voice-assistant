"""The Windows implementation: the existing JARVIS code, unchanged.

The Windows calls themselves stay where they always were (app_finder,
open_app, find_file, system/backend) because the test suite replaces them
there; this class only exposes them through the shared interface.
"""

from __future__ import annotations

import os
import subprocess
from typing import List, Optional, Tuple

from jarvis.platform.base import Platform


class WindowsPlatform(Platform):
    """Windows 10/11: Start Menu apps, os.startfile, Explorer, pywin32/pycaw/WinRT system controls."""

    name = "windows"
    global_hotkeys = True
    screen_text = True
    window_list = True
    brightness = True

    def installed_apps(self) -> List[Tuple[str, str]]:
        """Start Menu shortcuts and Store apps (jarvis/actions/app_finder.py)."""
        from jarvis.actions.app_finder import enumerate_installed_apps

        return [(m.display_name, m.target) for m in enumerate_installed_apps()]

    def find_chrome(self) -> Optional[str]:
        """chrome.exe under Program Files or the user's local app data."""
        from jarvis.actions.app_finder import find_chrome

        return find_chrome()

    def open_app(self, target: str) -> bool:
        """os.startfile on the shortcut or app ID."""
        try:
            os.startfile(target)  # noqa: S606 -- fixed action, not a shell command
            return True
        except OSError:
            return False

    def open_path(self, path: str) -> bool:
        """os.startfile on the file."""
        return self.open_app(path)

    def open_url(self, url: str, browser: Optional[str]) -> bool:
        """Chrome with the URL, or the default browser."""
        try:
            if browser:
                subprocess.Popen([browser, url], shell=False)
            else:
                os.startfile(url)  # noqa: S606
            return True
        except OSError:
            return False

    def show_in_file_manager(self, path: str) -> bool:
        """Explorer with the file selected."""
        try:
            subprocess.Popen(["explorer.exe", f"/select,{os.path.normpath(path)}"], shell=False)
            return True
        except OSError:
            return False

    def system_backend(self):
        """pycaw, screen-brightness-control, WinRT radios, user32 and pywin32 (jarvis/system/backend.py)."""
        from jarvis.system.backend import WindowsSystem

        return WindowsSystem()
