"""F2 Stage 3 / R8: global hotkeys (jarvis/voice/hotkeys.py). Windows'
RegisterHotKey is replaced by a fake -- no real hotkey is registered and
no key is ever pressed or simulated."""

from __future__ import annotations

import re
import threading
from pathlib import Path

import pytest

from jarvis.settings import SETTINGS
from jarvis.voice import hotkeys
from jarvis.voice.hotkeys import MOD_NOREPEAT, GlobalHotkeys, HotkeyBinding, parse_combo

_MESSAGES = SETTINGS["setup_messages"]
_HOTKEYS_SOURCE = Path(hotkeys.__file__).read_text(encoding="utf-8")


class FakeUser32:
    """Stands in for user32: records registrations; GetMessageW delivers
    queued WM_HOTKEY messages, then WM_QUIT (returns 0)."""

    def __init__(self, taken=()):
        self.taken = set(taken)
        self.registered = {}
        self.unregistered = []
        self.messages = []
        self.has_messages = threading.Event()

    def RegisterHotKey(self, hwnd, hotkey_id, flags, vk):
        """Fake registerhotkey for the tests."""
        if (flags & ~MOD_NOREPEAT, vk) in self.taken:
            return 0
        self.registered[hotkey_id] = (flags, vk)
        return 1

    def UnregisterHotKey(self, hwnd, hotkey_id):
        """Fake unregisterhotkey for the tests."""
        self.unregistered.append(hotkey_id)
        return 1

    def GetMessageW(self, msg_pointer, hwnd, low, high):
        """Fake getmessagew for the tests."""
        self.has_messages.wait(2)
        if not self.messages:
            return 0
        message, wparam = self.messages.pop(0)
        msg = msg_pointer._obj
        msg.message, msg.wParam = message, wparam
        if not self.messages:
            self.messages.append(None)  # then quit
        if self.messages[0] is None:
            self.messages.clear()
        return 1

    def PostThreadMessageW(self, thread_id, message, wparam, lparam):
        """Fake postthreadmessagew for the tests."""
        self.has_messages.set()
        return 1


def test_parse_combo() -> None:
    """Parse combo."""
    assert parse_combo("Ctrl+Shift+Space") == (0x0002 | 0x0004, 0x20)
    assert parse_combo("ctrl + shift + x") == (0x0006, ord("X"))
    assert parse_combo("Alt+F4") == (0x0001, 0x73)
    assert parse_combo("Space") is None  # a modifier is always required
    assert parse_combo("Ctrl+Shift+Banana") is None
    assert parse_combo("Hyper+X") is None


def test_settings_combinations_are_the_developers_and_differ() -> None:
    """Settings combinations are the developers and differ."""
    assert SETTINGS["hotkeys"]["push_to_talk"] == "Ctrl+Shift+Space"
    assert SETTINGS["hotkeys"]["stop"] == "Ctrl+Shift+X"
    assert parse_combo(SETTINGS["hotkeys"]["push_to_talk"]) != parse_combo(SETTINGS["hotkeys"]["stop"])


def test_registers_both_with_norepeat() -> None:
    """Registers both with norepeat."""
    api = FakeUser32()
    hk = GlobalHotkeys([HotkeyBinding("push_to_talk", "Ctrl+Shift+Space", lambda: None), HotkeyBinding("stop", "Ctrl+Shift+X", lambda: None)], api=api)
    statuses = hk.start()
    hk.stop()
    assert [s.registered for s in statuses] == [True, True]
    assert all(flags & MOD_NOREPEAT for flags, _vk in api.registered.values())


def test_registration_failure_gives_the_exact_message_and_no_crash() -> None:
    """Registration failure gives the exact message and no crash."""
    api = FakeUser32(taken={(0x0006, ord("X"))})
    hk = GlobalHotkeys([HotkeyBinding("push_to_talk", "Ctrl+Shift+Space", lambda: None), HotkeyBinding("stop", "Ctrl+Shift+X", lambda: None)], api=api)
    statuses = hk.start()
    hk.stop()
    ptt, stop = statuses
    assert ptt.registered is True
    assert stop.registered is False
    assert stop.message == "Ctrl+Shift+X is already used by another app. Change [hotkeys] stop in settings.toml, then restart JARVIS."
    assert "not available" not in stop.message


def test_push_to_talk_conflict_message_names_its_own_key() -> None:
    """Push to talk conflict message names its own key."""
    api = FakeUser32(taken={(0x0006, 0x20)})
    statuses = GlobalHotkeys([HotkeyBinding("push_to_talk", "Ctrl+Shift+Space", lambda: None)], api=api).start()
    assert statuses[0].message == _MESSAGES["hotkey_in_use"].format(combo="Ctrl+Shift+Space", key="push_to_talk")
    assert "Change [hotkeys] push_to_talk in settings.toml, then restart JARVIS." in statuses[0].message


def test_invalid_combination_says_how_to_fix_it() -> None:
    """Invalid combination says how to fix it."""
    statuses = GlobalHotkeys([HotkeyBinding("stop", "Ctrl+Shift+Banana", lambda: None)], api=FakeUser32()).start()
    assert statuses[0].registered is False
    assert "[hotkeys] stop" in statuses[0].message and "settings.toml" in statuses[0].message


def test_wm_hotkey_dispatches_to_the_right_handler() -> None:
    """Wm hotkey dispatches to the right handler."""
    api = FakeUser32()
    pressed = []
    hk = GlobalHotkeys(
        [HotkeyBinding("push_to_talk", "Ctrl+Shift+Space", lambda: pressed.append("ptt")), HotkeyBinding("stop", "Ctrl+Shift+X", lambda: pressed.append("stop"))],
        api=api,
    )
    api.messages = [(0x0312, 2), (0x0312, 1)]
    api.has_messages.set()
    hk.start()
    hk._thread.join(2)
    assert pressed == ["stop", "ptt"]
    assert sorted(api.unregistered) == [1, 2]  # cleaned up when the loop ends


def test_a_handler_that_raises_does_not_kill_the_loop() -> None:
    """A handler that raises does not kill the loop."""
    api = FakeUser32()
    pressed = []

    def broken():
        raise RuntimeError("boom")

    hk = GlobalHotkeys([HotkeyBinding("stop", "Ctrl+Shift+X", broken), HotkeyBinding("push_to_talk", "Ctrl+Shift+Space", lambda: pressed.append(1))], api=api)
    api.messages = [(0x0312, 1), (0x0312, 2)]
    api.has_messages.set()
    hk.start()
    hk._thread.join(2)
    assert pressed == [1]


@pytest.mark.parametrize("banned", ["GetAsyncKeyState", "GetKeyState", "SendInput", "keybd_event", "mouse_event", "SetWindowsHookEx", "GetKeyboardState"])
def test_hotkeys_module_never_polls_or_injects_keys(banned) -> None:
    """Hotkeys module never polls or injects keys."""
    assert banned not in _HOTKEYS_SOURCE


def test_hotkeys_module_uses_only_listening_user32_calls() -> None:
    """Hotkeys module uses only listening user32 calls."""
    calls = set(re.findall(r"user32\.(\w+)", _HOTKEYS_SOURCE))
    assert calls == {"RegisterHotKey", "UnregisterHotKey", "GetMessageW", "PostThreadMessageW"}
