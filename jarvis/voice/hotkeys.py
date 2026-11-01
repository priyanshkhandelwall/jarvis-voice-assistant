"""F2 Stage 3 / R8: system-wide hotkeys -- press-to-talk and Stop.

Windows' RegisterHotKey reports ONLY the registered combinations, and only
as a WM_HOTKEY message when one is pressed; it never watches or reads any
other key. The keyboard is never polled. One thread registers the hotkeys
and then blocks in a GetMessage loop, so a WM_HOTKEY arrives on that same
thread (RegisterHotKey with no window posts to the registering thread).

If a combination can't be registered (usually because another app already
owns it), the status says exactly what to change -- never "not available"
-- and everything else keeps working: the wake word still works, and the
window's own Esc key and STOP button still stop her.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
if sys.platform == "win32":
    from ctypes import wintypes
else:  # not Windows: these calls are never made there
    wintypes = None
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from jarvis.settings import SETTINGS

logger = logging.getLogger(__name__)

_MESSAGES = SETTINGS["setup_messages"]

_MODIFIERS = {"ctrl": 0x0002, "control": 0x0002, "shift": 0x0004, "alt": 0x0001, "win": 0x0008}
MOD_NOREPEAT = 0x4000  # holding the keys down fires once, not repeatedly
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012
_ERROR_HOTKEY_ALREADY_REGISTERED = 1409

_NAMED_KEYS = {
    "space": 0x20,
    "enter": 0x0D,
    "tab": 0x09,
    "escape": 0x1B,
    "esc": 0x1B,
    "pause": 0x13,
    "insert": 0x2D,
    "delete": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
}


def parse_combo(combo: str) -> Optional[Tuple[int, int]]:
    """"Ctrl+Shift+Space" -> (modifier flags, virtual-key code). None if it
    isn't a combination this module understands. At least one modifier is
    required, so a single ordinary key can never be taken over."""
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if len(parts) < 2:
        return None
    *mods, key = parts
    flags = 0
    for mod in mods:
        if mod not in _MODIFIERS:
            return None
        flags |= _MODIFIERS[mod]
    if key in _NAMED_KEYS:
        vk = _NAMED_KEYS[key]
    elif len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    else:
        return None
    return flags, vk


@dataclass(frozen=True)
class HotkeyBinding:
    """One global hotkey: its settings key, the key combination, and what pressing it does."""
    setting_key: str  # the [hotkeys] key name, e.g. "push_to_talk"
    combo: str  # e.g. "Ctrl+Shift+Space"
    on_press: Callable[[], None]


@dataclass(frozen=True)
class HotkeyStatus:
    """Whether one hotkey could be registered, and why not if it couldn't."""
    setting_key: str
    combo: str
    registered: bool
    message: Optional[str] = None  # missing-setup sentence when not registered


class _User32:
    """The four user32 calls this module uses -- nothing else."""

    def __init__(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.RegisterHotKey = user32.RegisterHotKey
        self.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        self.RegisterHotKey.restype = wintypes.BOOL
        self.UnregisterHotKey = user32.UnregisterHotKey
        self.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self.UnregisterHotKey.restype = wintypes.BOOL
        self.GetMessageW = user32.GetMessageW
        self.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        self.GetMessageW.restype = wintypes.BOOL
        self.PostThreadMessageW = user32.PostThreadMessageW
        self.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.PostThreadMessageW.restype = wintypes.BOOL


class GlobalHotkeys:
    """Registers the hotkeys with Windows' RegisterHotKey on one thread (no keyboard hook; only these
    combinations are reported)."""
    def __init__(self, bindings: List[HotkeyBinding], api: Optional[object] = None) -> None:
        self._bindings = bindings
        self._api = api
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._registered: Dict[int, HotkeyBinding] = {}
        self._ready = threading.Event()
        self.statuses: List[HotkeyStatus] = []

    def start(self, timeout: float = 3.0) -> List[HotkeyStatus]:
        """Register every binding on the hotkey thread and wait until that is
        done. Returns one status per binding; never raises."""
        if sys.platform != "win32" and self._api is None:
            self.statuses = [HotkeyStatus(b.setting_key, b.combo, False, None) for b in self._bindings]
            return self.statuses
        self._thread = threading.Thread(target=self._run, name="jarvis-hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self.statuses

    def stop(self) -> None:
        """Unregister the hotkeys and end their thread."""
        if self._thread_id is not None and self._api is not None:
            self._api.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _register_all(self) -> None:
        statuses = []
        for hotkey_id, binding in enumerate(self._bindings, start=1):
            parsed = parse_combo(binding.combo)
            if parsed is None:
                message = _MESSAGES["hotkey_invalid"].format(combo=binding.combo, key=binding.setting_key)
                statuses.append(HotkeyStatus(binding.setting_key, binding.combo, False, message))
                logger.warning(message)
                continue
            flags, vk = parsed
            if self._api.RegisterHotKey(None, hotkey_id, flags | MOD_NOREPEAT, vk):
                self._registered[hotkey_id] = binding
                statuses.append(HotkeyStatus(binding.setting_key, binding.combo, True))
                logger.info("Registered hotkey %s (%s)", binding.combo, binding.setting_key)
            else:
                error = ctypes.get_last_error() if self._api_is_real() else _ERROR_HOTKEY_ALREADY_REGISTERED
                message = _MESSAGES["hotkey_in_use"].format(combo=binding.combo, key=binding.setting_key)
                statuses.append(HotkeyStatus(binding.setting_key, binding.combo, False, message))
                logger.warning("%s (Windows error %s)", message, error)
        self.statuses = statuses

    def _api_is_real(self) -> bool:
        return isinstance(self._api, _User32)

    def _run(self) -> None:
        try:
            if self._api is None:
                self._api = _User32()
            self._thread_id = threading.get_native_id()
            self._register_all()
        except Exception:
            logger.exception("Hotkey setup failed")
            self.statuses = [
                HotkeyStatus(b.setting_key, b.combo, False, _MESSAGES["hotkey_in_use"].format(combo=b.combo, key=b.setting_key))
                for b in self._bindings
            ]
            self._ready.set()
            return
        self._ready.set()
        if not self._registered:
            return
        msg = wintypes.MSG()
        try:
            # Blocks until a registered hotkey is pressed (or stop() posts
            # WM_QUIT). GetMessage returns 0 for WM_QUIT, -1 on error.
            while self._api.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == _WM_HOTKEY:
                    self._dispatch(int(msg.wParam))
        finally:
            for hotkey_id in list(self._registered):
                self._api.UnregisterHotKey(None, hotkey_id)
            self._registered.clear()

    def _dispatch(self, hotkey_id: int) -> None:
        binding = self._registered.get(hotkey_id)
        if binding is None:
            return
        logger.info("Hotkey pressed: %s (%s)", binding.combo, binding.setting_key)
        try:
            binding.on_press()
        except Exception:
            logger.exception("Hotkey handler for %s failed", binding.setting_key)
