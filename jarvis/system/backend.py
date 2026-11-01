"""F22: the only code that touches the real system settings. Everything else uses a SystemBackend, so tests swap in a fake and
can never change the laptop's volume, brightness, Wi-Fi, lock it or shut
it down (tests/conftest.py also refuses these methods outright).

No terminal command, shell or subprocess anywhere here (R10): pycaw for
the speakers, screen-brightness-control (WMI) for the panel, WinRT
Windows.Devices.Radios for Wi-Fi, user32.LockWorkStation (a session call,
not input or focus), and pywin32's InitiateSystemShutdown /
AbortSystemShutdown, never with force.

Libraries are imported only when a method runs, so a missing one turns off
only that control (R14: SystemUnavailable names the package). COM-based
calls initialise COM on the calling (worker) thread first.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional


class SystemUnavailable(Exception):
    """A library is missing (`library` names it) -- R14."""

    def __init__(self, library: str) -> None:
        super().__init__(library)
        self.library = library


class NoDevice(Exception):
    """No speakers / no Wi-Fi radio / a display without brightness control."""


class Refused(Exception):
    """Windows said no (radio access denied, shutdown privilege refused)."""


class TooLate(Exception):
    """AbortSystemShutdown failed: the machine is already shutting down."""


class SystemBackend:
    """The interface F22 uses. Percentages are 0-100 ints."""

    def get_volume(self) -> int:
        """The speakers' volume, 0-100."""

    def set_volume(self, percent: int) -> None:
        """Set the speakers' volume, 0-100."""

    def get_mute(self) -> bool:
        """True if the speakers are muted."""

    def set_mute(self, muted: bool) -> None:
        """Mute or unmute the speakers."""

    def get_brightness(self) -> int:
        """The screen brightness, 0-100."""

    def set_brightness(self, percent: int) -> None:
        """Set the screen brightness, 0-100."""

    def get_wifi(self) -> bool:
        """True if Wi-Fi is on."""

    def set_wifi(self, on: bool) -> None:
        """Turn Wi-Fi on or off."""

    def lock(self) -> bool:
        """Lock the laptop. True if Windows accepted it."""

    def schedule_shutdown(self, seconds: int, reboot: bool, message: str) -> None:
        """Shut down (or restart) in `seconds`, showing `message`."""

    def abort_shutdown(self) -> None:
        """Cancel a scheduled shutdown or restart."""

    def window_titles(self) -> List[str]:
        """The titles of the open windows (to warn before a shutdown)."""


class _Com:
    """CoInitialize on this thread for the duration (pycaw is COM)."""

    def __enter__(self):
        try:
            import comtypes
        except ImportError as exc:
            raise SystemUnavailable("comtypes") from exc
        comtypes.CoInitialize()
        self._comtypes = comtypes
        return self

    def __exit__(self, *_exc) -> None:
        self._comtypes.CoUninitialize()


def _endpoint():
    try:
        from pycaw.pycaw import AudioUtilities
    except ImportError as exc:
        raise SystemUnavailable("pycaw") from exc
    speakers = AudioUtilities.GetSpeakers()
    if speakers is None:
        raise NoDevice("speakers")
    return speakers.EndpointVolume


def _sbc():
    try:
        import screen_brightness_control as sbc
    except ImportError as exc:
        raise SystemUnavailable("screen-brightness-control") from exc
    return sbc


async def _wifi_radio():
    try:
        from winrt.windows.devices.radios import Radio, RadioKind
    except ImportError as exc:
        raise SystemUnavailable("winrt-Windows.Devices.Radios") from exc
    for radio in await Radio.get_radios_async():
        if radio.kind == RadioKind.WI_FI:
            return radio
    raise NoDevice("wifi")


async def _set_wifi(on: bool) -> None:
    from winrt.windows.devices.radios import Radio, RadioAccessStatus, RadioState

    radio = await _wifi_radio()
    if await Radio.request_access_async() != RadioAccessStatus.ALLOWED:
        raise Refused("radio access")
    if await radio.set_state_async(RadioState.ON if on else RadioState.OFF) != RadioAccessStatus.ALLOWED:
        raise Refused("radio state")


async def _get_wifi() -> bool:
    from winrt.windows.devices.radios import RadioState

    return (await _wifi_radio()).state == RadioState.ON


class WindowsSystem(SystemBackend):
    """The real laptop. Never used by tests."""

    def get_volume(self) -> int:
        """The speakers' volume through pycaw, 0-100."""
        with _Com():
            return round(_endpoint().GetMasterVolumeLevelScalar() * 100)

    def set_volume(self, percent: int) -> None:
        """Set the speakers' volume through pycaw."""
        with _Com():
            _endpoint().SetMasterVolumeLevelScalar(max(0, min(100, percent)) / 100.0, None)

    def get_mute(self) -> bool:
        """Whether the speakers are muted (pycaw)."""
        with _Com():
            return bool(_endpoint().GetMute())

    def set_mute(self, muted: bool) -> None:
        """Mute or unmute through pycaw."""
        with _Com():
            _endpoint().SetMute(1 if muted else 0, None)

    def get_brightness(self) -> int:
        """The laptop panel's brightness (screen-brightness-control)."""
        sbc = _sbc()
        try:
            values = sbc.get_brightness()
        except Exception as exc:  # no WMI / DDC/CI support on this display
            raise NoDevice("brightness") from exc
        if not values:
            raise NoDevice("brightness")
        return int(values[0])

    def set_brightness(self, percent: int) -> None:
        """Set the laptop panel's brightness (screen-brightness-control)."""
        sbc = _sbc()
        try:
            sbc.set_brightness(max(0, min(100, percent)), display=0)
        except Exception as exc:
            raise NoDevice("brightness") from exc

    def get_wifi(self) -> bool:
        """Whether the Wi-Fi radio is on (WinRT Radios)."""
        return asyncio.run(_get_wifi())

    def set_wifi(self, on: bool) -> None:
        """Turn the Wi-Fi radio on or off (WinRT Radios)."""
        asyncio.run(_set_wifi(on))

    def lock(self) -> bool:
        """Lock the workstation (LockWorkStation)."""
        import ctypes

        return bool(ctypes.windll.user32.LockWorkStation())

    def schedule_shutdown(self, seconds: int, reboot: bool, message: str) -> None:
        """Schedule a shutdown or restart with Windows' own countdown (InitiateSystemShutdown)."""
        try:
            import win32api
            import win32con
            import win32security
        except ImportError as exc:
            raise SystemUnavailable("pywin32") from exc
        try:
            token = win32security.OpenProcessToken(win32api.GetCurrentProcess(),
                                                   win32con.TOKEN_ADJUST_PRIVILEGES | win32con.TOKEN_QUERY)
            privilege = win32security.LookupPrivilegeValue(None, win32security.SE_SHUTDOWN_NAME)
            win32security.AdjustTokenPrivileges(token, False, [(privilege, win32security.SE_PRIVILEGE_ENABLED)])
            # force=False always: an app with unsaved work can stop it.
            win32api.InitiateSystemShutdown(None, message, seconds, False, reboot)
        except Exception as exc:  # pywintypes.error: privilege or API refused
            raise Refused(str(exc)) from exc

    def abort_shutdown(self) -> None:
        """Cancel the countdown (AbortSystemShutdown)."""
        try:
            import win32api
        except ImportError as exc:
            raise SystemUnavailable("pywin32") from exc
        try:
            win32api.AbortSystemShutdown(None)
        except Exception as exc:
            raise TooLate(str(exc)) from exc

    def window_titles(self) -> List[str]:
        """Read-only (F8's listing): titles of the windows that are open,
        protected ones replaced by "a protected window"."""
        try:
            from jarvis.screen.capture import list_windows
        except ImportError:
            return []
        titles = []
        for w in list_windows():
            if w.system or not w.title.strip():
                continue
            titles.append("a protected window" if w.protected else w.title.strip())
        return titles


def default_backend() -> Optional[SystemBackend]:
    """The real backend for this computer: WindowsSystem, or MacSystem on macOS."""
    from jarvis.platform import current, is_mac

    if is_mac():
        return current().system_backend()
    return WindowsSystem()
