"""F22 System settings. Every OS call goes to FakeSystem;
tests/conftest.py refuses the real ones, so no test can change the volume,
brightness or Wi-Fi, lock the laptop or shut it down."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import jarvis.system.backend as backend_mod
import jarvis.system.commands as commands_mod
from jarvis.controller import State
from jarvis.controller.trust import TRUST_TABLE, TrustLevel
from jarvis.dashboard.activity_feed import format_record
from jarvis.system.backend import NoDevice, Refused, SystemBackend, SystemUnavailable, TooLate
from jarvis.system.commands import SystemCommands, parse, to_number
from stretch_rig import build, wait


class FakeSystem(SystemBackend):
    """The laptop, in memory. `lag` read-backs return the old value first."""

    def __init__(self) -> None:
        self.volume, self.muted, self.brightness, self.wifi = 30, False, 50, True
        self.calls = []
        self.volume_sticks_at = None  # the speakers refuse to go past this
        self.brightness_step = 1  # panels that only move in steps
        self.wifi_refused = False
        self.wifi_stuck = False
        self.no_brightness = False
        self.no_speakers = False
        self.privilege_refused = False
        self.too_late = False
        self.missing = None
        self.titles = ["Report.docx - Word", "a protected window"]

    def _check(self, what):
        """Fake check for the tests."""
        if self.missing == what:
            raise SystemUnavailable(what)

    def get_volume(self):
        """Fake get volume for the tests."""
        self._check("pycaw")
        if self.no_speakers:
            raise NoDevice("speakers")
        return self.volume

    def set_volume(self, percent):
        """Fake set volume for the tests."""
        self.calls.append(("set_volume", percent))
        self.volume = percent if self.volume_sticks_at is None else min(percent, self.volume_sticks_at)

    def get_mute(self):
        """Fake get mute for the tests."""
        return self.muted

    def set_mute(self, muted):
        """Fake set mute for the tests."""
        self.calls.append(("set_mute", muted))
        self.muted = muted

    def get_brightness(self):
        """Fake get brightness for the tests."""
        if self.no_brightness:
            raise NoDevice("brightness")
        return self.brightness

    def set_brightness(self, percent):
        """Fake set brightness for the tests."""
        self.calls.append(("set_brightness", percent))
        self.brightness = (percent // self.brightness_step) * self.brightness_step

    def get_wifi(self):
        """Fake get wi-fi for the tests."""
        self._check("winrt-Windows.Devices.Radios")
        return self.wifi

    def set_wifi(self, on):
        """Fake set wi-fi for the tests."""
        self.calls.append(("set_wifi", on))
        if self.wifi_refused:
            raise Refused("radio access")
        if not self.wifi_stuck:
            self.wifi = on

    def lock(self):
        """Fake lock for the tests."""
        self.calls.append(("lock",))
        return True

    def schedule_shutdown(self, seconds, reboot, message):
        """Fake schedule shutdown for the tests."""
        self.calls.append(("schedule_shutdown", seconds, reboot, message))
        if self.privilege_refused:
            raise Refused("privilege")

    def abort_shutdown(self):
        """Fake abort shutdown for the tests."""
        self.calls.append(("abort_shutdown",))
        if self.too_late:
            raise TooLate("already shutting down")

    def window_titles(self):
        """Fake window titles for the tests."""
        return list(self.titles)


class Clock:
    """A clock the test moves by hand (sleep() advances it instantly)."""
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        """Fake sleep for the tests."""
        self.now += seconds


@pytest.fixture
def pc(tmp_path, monkeypatch):
    """Fixture: system commands on an in-memory fake laptop (nothing real changes)."""
    fake = FakeSystem()
    clock = Clock()
    system = SystemCommands(fake, clock=clock, sleep=clock.sleep)
    rig = build(tmp_path, monkeypatch, system=system)
    rig.update(fake=fake, system=system, clock=clock)
    return rig


# -- trust levels (fixed table) ---------------------------------------------------------------------


def test_trust_levels_are_the_developers() -> None:
    """Trust levels are the developers."""
    assert [TRUST_TABLE[a] for a in ("set_volume", "set_mute", "set_brightness")] == [TrustLevel.L1] * 3
    assert TRUST_TABLE["lock_screen"] == TrustLevel.L2
    assert TRUST_TABLE["set_wifi"] == TrustLevel.L3
    assert TRUST_TABLE["restart"] == TRUST_TABLE["shutdown"] == TrustLevel.L4


# -- volume and mute (L1, verified) ------------------------------------------------------------------


def test_volume_up_down_and_set_are_immediate_and_read_back(pc) -> None:
    """Volume up down and set are immediate and read back."""
    assert pc["say"]("turn the volume up") == "Volume is now 40%."
    assert pc["say"]("volume down by 20") == "Volume is now 20%."
    assert pc["say"]("set the volume to forty five percent") == "Volume is now 45%."
    assert pc["fake"].volume == 45
    lines = format_record(pc["logbook"].last_completed()).lines
    assert "set volume 45%: immediate (L1)" in lines and "volume now 45%" in lines and "saying: Volume is now 45%." in lines


def test_volume_anywhere_in_a_sentence_but_never_in_a_question(pc) -> None:
    """Volume anywhere in a sentence but never in a question."""
    assert pc["say"]("This is way too loud, turn the volume down.") == "Volume is now 20%."
    pc["say"]("how do I turn the volume up")
    assert pc["fake"].volume == 20 and pc["groq"].questions == ["how do i turn the volume up"]  # a question: answered, not done


def test_volume_up_while_muted_unmutes(pc) -> None:
    """Volume up while muted unmutes."""
    pc["fake"].muted = True
    pc["say"]("volume up")
    assert pc["fake"].muted is False


def test_mute_and_unmute(pc) -> None:
    """Mute and unmute."""
    assert pc["say"]("mute") == "Muted."
    assert pc["fake"].muted is True
    assert pc["say"]("unmute the sound") == "Unmuted. Volume is 30%."
    pc["say"]("the TV is on mute")  # a statement, not a command
    assert pc["fake"].muted is False


@pytest.mark.parametrize("said, reply", [
    ("set the volume to 150", "Volume goes from 0 to 100."),
    ("volume up by 80", "That's a big jump. Change it by at most 50 at a time."),
    ("set brightness to 120", "Brightness goes from 0 to 100."),
])
def test_out_of_range_is_refused_before_policy(pc, said, reply) -> None:
    """Out of range is refused before policy."""
    assert pc["say"](said) == reply
    assert pc["fake"].calls == []
    assert pc["logbook"].last_completed().rule_or_trust_level == TrustLevel.L0  # an answer, never an action


def test_parameters_are_checked_before_policy() -> None:
    """Parameters are checked before policy."""
    from jarvis.interpreter import Answer

    sys_ = SystemCommands(FakeSystem())
    for kind, direction, number in (("volume", "set", -5), ("volume", "set", 150), ("brightness", "set", 101), ("volume", "up", 80)):
        assert isinstance(sys_._level_request(kind, direction, number), Answer)


def test_mismatch_retries_once_then_reports_the_real_value(pc) -> None:
    """Mismatch retries once then reports the real value."""
    pc["fake"].volume_sticks_at = 35
    reply = pc["say"]("set the volume to 60")
    assert reply == "I set the volume to 60%, but it's reading 35%."
    assert [c for c in pc["fake"].calls if c[0] == "set_volume"] == [("set_volume", 60), ("set_volume", 60)]
    assert "Troubleshooting the issue." in pc["speech"].texts


def test_no_speakers(pc) -> None:
    """No speakers."""
    pc["fake"].no_speakers = True
    assert pc["say"]("volume up") == "No speakers found. Plug in or turn on your speakers, then try again."


def test_missing_library_names_it(pc) -> None:
    """Missing library names it."""
    pc["fake"].missing = "pycaw"
    assert pc["say"]("volume up") == ("That needs the pycaw package, which isn't installed. Run pip install -r requirements.txt "
                                      "in the JARVIS folder, then restart JARVIS.")


# -- brightness (L1) --------------------------------------------------------------------------------------


def test_brightness_within_tolerance_is_success(pc) -> None:
    """Brightness within tolerance is success."""
    pc["fake"].brightness_step = 4  # 63 -> 60: within the 5% tolerance
    assert pc["say"]("set brightness to 63") == "Brightness is now 60%."
    assert pc["say"]("dim the screen") == "Brightness is now 48%."


def test_brightness_unsupported_display(pc) -> None:
    """Brightness unsupported display."""
    pc["fake"].no_brightness = True
    assert pc["say"]("brightness up") == "Brightness isn't controllable on this display."


# -- Wi-Fi (L3) -------------------------------------------------------------------------------------------


def test_wifi_off_needs_one_confirm_and_says_what_it_cuts(pc) -> None:
    """Wi-Fi off needs one confirm and says what it cuts."""
    pc["say"]("turn off wi-fi")
    assert pc["c"].state == State.CONFIRMING
    assert pc["speech"].texts[-1] == "Turn Wi-Fi off? Every online feature stops working until it's back on. Say confirm."
    assert pc["fake"].calls == []
    assert pc["confirming"]("confirm") == "Wi-Fi is now off."
    assert pc["fake"].wifi is False


def test_wifi_cancel_changes_nothing(pc) -> None:
    """Wi-Fi cancel changes nothing."""
    pc["say"]("turn the wifi off")
    assert pc["confirming"]("cancel") == "Cancelled."
    assert pc["fake"].calls == [] and pc["fake"].wifi is True


def test_wifi_refused_by_windows(pc) -> None:
    """Wi-Fi refused by Windows."""
    pc["fake"].wifi_refused = True
    pc["say"]("turn wifi off")
    assert pc["confirming"]("confirm") == "Windows doesn't allow JARVIS to switch Wi-Fi on this laptop."


def test_wifi_held_by_a_switch_is_reported_honestly(pc) -> None:
    """Wi-Fi held by a switch is reported honestly."""
    pc["fake"].wifi_stuck = True
    pc["say"]("disable wifi")
    reply = pc["confirming"]("confirm")
    assert reply.startswith("I asked Windows to turn Wi-Fi off, but it's still on.")


def test_wifi_already_in_that_state_asks_nothing(pc) -> None:
    """Wi-Fi already in that state asks nothing."""
    assert pc["say"]("turn wifi on") == "Wi-Fi is already on."
    assert pc["c"].state == State.AWAKE


# -- lock (L2) --------------------------------------------------------------------------------------------


def test_lock_needs_one_confirm_and_is_only_requested(pc) -> None:
    """Lock needs one confirm and is only requested."""
    pc["say"]("lock the screen")
    assert pc["c"].state == State.CONFIRMING and pc["fake"].calls == []
    assert pc["confirming"]("confirm") == "Screen lock requested."
    assert pc["fake"].calls == [("lock",)]


# -- restart and shutdown (L4) --------------------------------------------------------------------------------


def test_restart_needs_two_confirms_and_one_does_nothing(pc) -> None:
    """Restart needs two confirms and one does nothing."""
    pc["say"]("restart the laptop")
    assert pc["speech"].texts[-1] == ("Restart in 30 seconds. Apps won't be force-closed. Unsaved work in another app may stop "
                                      "the restart. Say confirm twice.")
    assert pc["system"].preview_titles == ["Report.docx - Word", "a protected window"]
    pc["confirming"]("confirm")
    assert pc["c"].state == State.CONFIRMING and pc["fake"].calls == []
    reply = pc["confirming"]("confirm")
    assert reply == "Restart requested. It starts in 30 seconds unless an app stops it. Say stop to cancel."
    assert "done" not in reply.lower()
    assert pc["fake"].calls == [("schedule_shutdown", 30, True, commands_mod._CFG["shutdown_message"])]


def test_shutdown_is_never_forced(pc) -> None:
    """Shutdown is never forced."""
    pc["say"]("shut down the computer")
    pc["confirming"]("confirm")
    pc["confirming"]("confirm")
    _, seconds, reboot, _msg = pc["fake"].calls[0]
    assert reboot is False and seconds == 30
    # force=False is hard-coded in the real backend's one call:
    source = Path(backend_mod.__file__).read_text(encoding="utf-8")
    assert "InitiateSystemShutdown(None, message, seconds, False, reboot)" in source


def test_stop_during_the_countdown_aborts_it(pc) -> None:
    """Stop during the countdown aborts it."""
    pc["say"]("restart")
    pc["confirming"]("confirm")
    pc["confirming"]("confirm")
    pc["c"].request_stop()
    assert wait(lambda: ("abort_shutdown",) in pc["fake"].calls)
    assert wait(lambda: pc["speech"].texts[-1] == "Restart cancelled.")


def test_cancel_during_the_countdown_aborts_it(pc) -> None:
    """Cancel during the countdown aborts it."""
    pc["say"]("shut down")
    pc["confirming"]("confirm")
    pc["confirming"]("confirm")
    pc["orch"].handle_typed_command("cancel")
    assert pc["speech"].texts[-1] == "Shutdown cancelled." and ("abort_shutdown",) in pc["fake"].calls


def test_stop_after_windows_started_is_reported_honestly(pc) -> None:
    """Stop after Windows started is reported honestly."""
    pc["say"]("restart")
    pc["confirming"]("confirm")
    pc["confirming"]("confirm")
    pc["fake"].too_late = True
    assert pc["say"]("cancel the restart") == "It's too late to cancel: Windows is already restarting."


def test_after_the_countdown_there_is_nothing_to_cancel(pc) -> None:
    """After the countdown there is nothing to cancel."""
    pc["say"]("restart")
    pc["confirming"]("confirm")
    pc["confirming"]("confirm")
    pc["clock"].now += 31
    assert pc["say"]("cancel the restart") == "There's no restart or shutdown to cancel."
    assert ("abort_shutdown",) not in pc["fake"].calls


def test_privilege_refused(pc) -> None:
    """Privilege refused."""
    pc["fake"].privilege_refused = True
    pc["say"]("restart")
    pc["confirming"]("confirm")
    assert pc["confirming"]("confirm") == "Windows didn't allow me to restart, so no restart was requested."


# -- R10 and the real backend --------------------------------------------------------------------------------


def test_no_terminal_shell_or_subprocess_in_f22() -> None:
    """No terminal shell or subprocess in F22."""
    for module in (backend_mod, commands_mod):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert "subprocess" not in imported, module.__name__
        calls = {f"{getattr(n.func.value, 'id', '')}.{n.func.attr}" for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert not {"os.system", "os.popen", "os.startfile"} & calls
        docstring = (ast.get_docstring(tree) or "").lower()
        assert Path(module.__file__).read_text(encoding="utf-8").lower().count("netsh") == docstring.count("netsh")


def test_the_guard_refuses_every_real_setter(no_real_opening) -> None:
    """The guard refuses every real setter."""
    real = backend_mod.WindowsSystem()
    for call in (lambda: real.set_volume(10), lambda: real.set_mute(True), lambda: real.set_brightness(10),
                 lambda: real.set_wifi(False), real.lock, lambda: real.schedule_shutdown(30, True, "x"), real.abort_shutdown):
        with pytest.raises(Exception) as refused:
            call()
        assert "RealOpenAttempted" in type(refused.value).__name__
    assert len(no_real_opening) == 7
    no_real_opening.clear()


# -- parsing --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("said, parsed", [
    ("turn the volume up", ("volume", "up", None)),
    ("set the volume to forty five percent", ("volume", "set", 45)),
    ("volume 30", ("volume", "set", 30)),
    ("unmute the sound", ("mute", "off", None)),
    ("dim the screen", ("brightness", "down", None)),
    ("turn the wifi back on", ("wifi", "on", None)),
    ("reboot my computer", ("restart", "", None)),
    ("shut down the laptop", ("shutdown", "", None)),
    ("restart chrome", None),
    ("turn off the camera", None),
    ("the tv is on mute", None),
])
def test_parse(said, parsed) -> None:
    """Parse."""
    assert parse(said) == parsed


def test_number_words() -> None:
    """Number words."""
    assert [to_number(w) for w in ("40", "forty five", "a hundred", "seven", "ninety nine", "lots")] == [40, 45, 100, 7, 99, None]
