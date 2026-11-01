"""The macOS implementation, using only tools every Mac has: `open`,
`osascript`, `pmset` and `networksetup`. Every command is a fixed argument
list (never a shell string), run through `_run` so the tests can
replace them; nothing here simulates input or changes window focus.
"""

from __future__ import annotations

import re
import subprocess
import threading
from pathlib import Path
from typing import List, Optional, Tuple

from jarvis.platform.base import Platform
from jarvis.system.backend import NoDevice, Refused, SystemBackend, TooLate

APP_FOLDERS = ("/Applications", "/Applications/Utilities", "/System/Applications", "/System/Applications/Utilities")
CHROME_APP = "Google Chrome"
_TIMEOUT = 10


def _run(args: List[str]) -> subprocess.CompletedProcess:
    """Run a fixed command and capture its output (replaced in tests)."""
    return subprocess.run(args, capture_output=True, text=True, timeout=_TIMEOUT, shell=False)


def _ok(args: List[str]) -> bool:
    """True when the command ran and exited with 0."""
    try:
        return _run(args).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def app_folders(home: Optional[Path] = None) -> List[Path]:
    """The folders apps are installed in, the user's own last."""
    return [Path(p) for p in APP_FOLDERS] + [(home or Path.home()) / "Applications"]


def list_apps(folders: List[Path]) -> List[Tuple[str, str]]:
    """(name, path) for every `.app` bundle directly inside `folders`."""
    found: List[Tuple[str, str]] = []
    seen = set()
    for folder in folders:
        try:
            bundles = sorted(folder.glob("*.app"))
        except OSError:
            continue
        for bundle in bundles:
            name = bundle.stem
            if name.lower() not in seen:
                seen.add(name.lower())
                found.append((name, str(bundle)))
    return found


def wifi_device(hardware_ports: str) -> Optional[str]:
    """The Wi-Fi device ("en0") from `networksetup -listallhardwareports` output."""
    match = re.search(r"Hardware Port:\s*(?:Wi-Fi|AirPort)\s*\nDevice:\s*(\S+)", hardware_ports)
    return match.group(1) if match else None


class MacSystem(SystemBackend):
    """F22 on macOS: osascript for volume and restart/shutdown, pmset to lock
    (display sleep), networksetup for Wi-Fi. Brightness has no free built-in
    command, so it reports NoDevice (a plain "not supported" reply)."""

    def __init__(self) -> None:
        self._pending: Optional[threading.Timer] = None

    @staticmethod
    def _osascript(script: str) -> str:
        done = _run(["osascript", "-e", script])
        if done.returncode != 0:
            raise NoDevice(done.stderr.strip())
        return done.stdout.strip()

    def get_volume(self) -> int:
        """The output volume, 0-100."""
        return int(float(self._osascript("output volume of (get volume settings)")))

    def set_volume(self, percent: int) -> None:
        """Set the output volume, 0-100."""
        self._osascript(f"set volume output volume {max(0, min(100, int(percent)))}")

    def get_mute(self) -> bool:
        """True if the output is muted."""
        return self._osascript("output muted of (get volume settings)").lower() == "true"

    def set_mute(self, muted: bool) -> None:
        """Mute or unmute the output."""
        self._osascript("set volume with output muted" if muted else "set volume without output muted")

    def get_brightness(self) -> int:
        """Not available with macOS's built-in tools."""
        raise NoDevice("brightness")

    def set_brightness(self, percent: int) -> None:
        """Not available with macOS's built-in tools."""
        raise NoDevice("brightness")

    @staticmethod
    def _device() -> str:
        try:
            done = _run(["networksetup", "-listallhardwareports"])
        except (OSError, subprocess.SubprocessError) as exc:
            raise NoDevice("networksetup") from exc
        device = wifi_device(done.stdout)
        if device is None:
            raise NoDevice("wifi")
        return device

    def get_wifi(self) -> bool:
        """True if the Wi-Fi radio is on."""
        done = _run(["networksetup", "-getairportpower", self._device()])
        return done.stdout.strip().lower().endswith(": on")

    def set_wifi(self, on: bool) -> None:
        """Turn the Wi-Fi radio on or off."""
        done = _run(["networksetup", "-setairportpower", self._device(), "on" if on else "off"])
        if done.returncode != 0:
            raise Refused(done.stderr.strip())

    def lock(self) -> bool:
        """Put the display to sleep, which locks the Mac when a password is required on wake."""
        return _ok(["pmset", "displaysleepnow"])

    def schedule_shutdown(self, seconds: int, reboot: bool, message: str) -> None:
        """Restart or shut down after `seconds` through System Events, which
        asks every app to quit normally (never forced)."""
        verb = "restart" if reboot else "shut down"

        def go() -> None:
            _ok(["osascript", "-e", f'tell application "System Events" to {verb}'])

        self.abort_shutdown()
        self._pending = threading.Timer(max(0, int(seconds)), go)
        self._pending.daemon = True
        self._pending.start()

    def abort_shutdown(self) -> None:
        """Cancel a scheduled restart or shutdown that hasn't started yet."""
        pending, self._pending = self._pending, None
        if pending is None:
            return
        if not pending.is_alive():
            raise TooLate()
        pending.cancel()

    def window_titles(self) -> List[str]:
        """Not read on macOS (that would need extra permissions)."""
        return []


class MacPlatform(Platform):
    """macOS: .app bundles, `open`, Finder, osascript/pmset/networksetup."""

    name = "mac"

    def installed_apps(self) -> List[Tuple[str, str]]:
        """Every .app in /Applications, /System/Applications and ~/Applications."""
        return list_apps(app_folders(self.home()))

    def find_chrome(self) -> Optional[str]:
        """Google Chrome.app, in /Applications or ~/Applications."""
        for folder in (Path("/Applications"), self.home() / "Applications"):
            app = folder / f"{CHROME_APP}.app"
            if app.exists():
                return str(app)
        return None

    def open_app(self, target: str) -> bool:
        """`open -a <app>`."""
        return _ok(["open", "-a", target])

    def open_path(self, path: str) -> bool:
        """`open <path>`: the file's default app."""
        return _ok(["open", path])

    def open_url(self, url: str, browser: Optional[str]) -> bool:
        """`open -a "Google Chrome" <url>`, else the default browser."""
        if browser and _ok(["open", "-a", browser, url]):
            return True
        return _ok(["open", url])

    def show_in_file_manager(self, path: str) -> bool:
        """`open -R <path>`: Finder with the file selected, never run."""
        return _ok(["open", "-R", path])

    def system_backend(self):
        """The osascript/pmset/networksetup backend."""
        return MacSystem()
