"""F22 System settings (built).

| Say | Action | Trust |
|---|---|---|
| "volume up/down", "set the volume to 40", "mute", "unmute" | set_volume / set_mute | L1 |
| "brightness up/down", "set brightness to 60" | set_brightness | L1 |
| "lock the screen" | lock_screen | L2 (one confirm) |
| "turn Wi-Fi on/off" | set_wifi | L3 (one confirm) |
| "restart", "shut down the laptop" | restart / shutdown | L4 (two confirms) |

- Parameters are checked before policy (0-100; a step of at most
  [system] max_step_percent), so a bad value never reaches a permit.
- R12: every change is read back. A mismatch -> "Troubleshooting the
  issue", one retry, then the real value is reported. Lock, restart and
  shutdown can't be fully verified: she says "requested", never "done".
- Restart/shutdown use the OS's own [system] shutdown_grace_seconds
  countdown, never force. Stop (button, hotkey, spoken), "cancel" or
  "cancel the restart" during it calls AbortSystemShutdown: "Restart
  cancelled."
- Wi-Fi refused by Windows -> "Windows doesn't allow JARVIS to switch Wi-Fi
  on this laptop." Never netsh, never any terminal command (R10): this
  package imports no subprocess, os.system or shell.
- A missing library turns off only that control and names it (R14).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable, List, Optional, Tuple

from jarvis.interpreter import ActionRequest, Answer
from jarvis.platform import is_mac
from jarvis.settings import SETTINGS
from jarvis.system.backend import NoDevice, Refused, SystemBackend, SystemUnavailable, TooLate

logger = logging.getLogger(__name__)

_CFG = SETTINGS["system"]
_REPLIES = _CFG["replies"]
_SETUP = SETTINGS["setup_messages"]

ACTIONS = ("set_volume", "set_mute", "set_brightness", "set_wifi", "lock_screen", "restart", "shutdown")

# -- numbers ("forty five", "40") -----------------------------------------------------------

_UNITS = {w: n for n, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
                                      "fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}


# "Set the volume to max", "Adjust the volume to
# 4p" / "for P" (Whisper's "forty").
_LEVEL_WORDS = {"max": 100, "maximum": 100, "full": 100, "highest": 100, "half": 50, "halfway": 50, "min": 0,
                "minimum": 0, "lowest": 0, "nothing": 0,
                # Whisper wrote "Make it max" as "Make it Max-er".
                "max er": 100, "maxer": 100, "maxed": 100, "maxed out": 100, "max out": 100, "maxx": 100, "all the way": 100,
                "full blast": 100}
_FORTY = re.compile(r"(?:for|four|fore|4)\s?p")
_TENS_P = re.compile(r"(?P<d>[1-9])\s?p")


def to_number(text: str) -> Optional[int]:
    """"40", "40 percent", "forty five", "a hundred", "max", "half", "4p"
    -> int, else None."""
    words = [w for w in text.replace("-", " ").split() if w not in ("percent", "per", "cent", "a", "and", "the")]
    if not words:
        return None
    said = " ".join(words)
    if said in _LEVEL_WORDS:
        return _LEVEL_WORDS[said]
    if _FORTY.fullmatch(said):
        return 40
    tens = _TENS_P.fullmatch(said)
    if tens is not None:
        return int(tens.group("d")) * 10
    if len(words) == 1 and words[0].lstrip("-").isdigit():
        return int(words[0])
    if words == ["hundred"] or words == ["one", "hundred"]:
        return 100
    if "hundred" in words:
        # "two hundred" was read as 2 -- so "the sound to two
        # hundred" said "Did you mean 'set the volume to 2'?" (not 0-100).
        at = words.index("hundred")
        # Recursion: 'two hundred and five' -> to_number('two') * 100 + to_number('five').
        before = to_number(" ".join(words[:at])) if at else 1
        after = to_number(" ".join(words[at + 1:])) if words[at + 1:] else 0
        if before is None or after is None or not 1 <= before <= 9 or after >= 100:
            return None
        return before * 100 + after
    total = 0
    for w in words:
        if w in _TENS and total == 0:
            total = _TENS[w]
        elif w in _UNITS and (total == 0 or (total % 10 == 0 and total >= 20 and _UNITS[w] < 10)):
            total += _UNITS[w]
        else:
            return None
    return total


# -- what was said --------------------------------------------------------------------------

_N = r"(?P<n>[a-z0-9 \-]+?)(?:\s+percent)?"
_THE = r"(?:the\s+|my\s+)?"
_VOL = r"(?:volume|sound)"
_BRI = r"(?:screen\s+)?brightness"
_WIFI = r"(?:wifi|wi\s?fi|wireless)"
_DEVICE = r"(?:computer|laptop|pc|system|machine|windows)"
# "Can you be set the volume to 4p": a polite start Whisper garbled.
_ASK = r"(?:(?:can|could|would|will)\s+you\s+)?(?:be\s+|please\s+|just\s+)?"

# (kind, direction or value, pattern), each matched against the whole command.
_PATTERNS: List[Tuple[str, str, re.Pattern]] = [(k, d, re.compile(p)) for k, d, p in [
    # "Make the volume 200%" went online -- "to" was
    # required. Any number now reaches _level_request, which says the range.
    ("volume", "set", rf"{_ASK}(?:set|change|put|turn|adjust|make)\s+{_THE}{_VOL}\s+(?:(?:to|at)\s+)?{_N}"),
    # "Make it Max", "Make it to 90": "it" with a level is the volume (his
    # decision), never the camera's last look.
    ("volume", "set", rf"{_ASK}(?:set|put|turn|make|crank)\s+it\s+(?:(?:to|at|up to)\s+)?{_N}"),
    ("volume", "up", rf"{_ASK}(?:turn|crank|bump|pump)\s+it\s+up(?:\s+by\s+{_N})?"),
    ("volume", "down", rf"{_ASK}turn\s+it\s+down(?:\s+by\s+{_N})?"),
    ("volume", "set", rf"{_ASK}(?P<n>max|maximum|full)\s+{_VOL}"),
    ("volume", "set", rf"{_VOL}\s+(?:to\s+)?(?P<n>max|maximum|full)"),
    ("volume", "set", rf"{_VOL}\s+(?:to\s+)?(?P<n>\d+)(?:\s+percent)?"),
    ("volume", "up", rf"(?:turn|crank)\s+{_THE}{_VOL}\s+up(?:\s+by\s+{_N})?"),
    ("volume", "up", rf"(?:turn|crank)\s+up\s+{_THE}{_VOL}(?:\s+by\s+{_N})?"),
    ("volume", "up", rf"(?:increase|raise)\s+{_THE}{_VOL}(?:\s+by\s+{_N})?"),
    ("volume", "up", rf"{_VOL}\s+up(?:\s+by\s+{_N})?"),
    ("volume", "up", rf"{_ASK}(?:make\s+it\s+)?louder"),
    ("volume", "down", rf"turn\s+{_THE}{_VOL}\s+down(?:\s+by\s+{_N})?"),
    ("volume", "down", rf"turn\s+down\s+{_THE}{_VOL}(?:\s+by\s+{_N})?"),
    ("volume", "down", rf"(?:decrease|lower|reduce)\s+{_THE}{_VOL}(?:\s+by\s+{_N})?"),
    ("volume", "down", rf"{_VOL}\s+down(?:\s+by\s+{_N})?"),
    ("volume", "down", rf"{_ASK}(?:make\s+it\s+)?(?:quieter|softer)"),
    ("mute", "on", rf"mute(?:\s+{_THE}(?:volume|sound|audio|speakers|laptop|computer))?"),
    ("mute", "off", rf"unmute(?:\s+{_THE}(?:volume|sound|audio|speakers|laptop|computer))?"),
    ("mute", "off", rf"turn\s+{_THE}(?:sound|audio)\s+(?:back\s+)?on"),
    ("brightness", "set", rf"{_ASK}(?:set|change|put|turn|adjust|make)\s+{_THE}{_BRI}\s+(?:(?:to|at)\s+)?{_N}"),
    ("brightness", "set", rf"{_BRI}\s+(?:to\s+)?(?P<n>\d+)(?:\s+percent)?"),
    ("brightness", "up", rf"(?:turn\s+up|increase|raise)\s+{_THE}{_BRI}(?:\s+by\s+{_N})?"),
    ("brightness", "up", rf"turn\s+{_THE}{_BRI}\s+up(?:\s+by\s+{_N})?"),
    ("brightness", "up", rf"{_BRI}\s+up(?:\s+by\s+{_N})?"),
    ("brightness", "up", rf"(?:make\s+{_THE}screen\s+)?brighter"),
    ("brightness", "down", rf"(?:turn\s+down|decrease|lower|reduce)\s+{_THE}{_BRI}(?:\s+by\s+{_N})?"),
    ("brightness", "down", rf"turn\s+{_THE}{_BRI}\s+down(?:\s+by\s+{_N})?"),
    ("brightness", "down", rf"{_BRI}\s+down(?:\s+by\s+{_N})?"),
    ("brightness", "down", rf"dim\s+{_THE}screen|(?:make\s+{_THE}screen\s+)?dimmer"),
    ("wifi", "on", rf"(?:turn|switch)\s+{_THE}{_WIFI}\s+(?:back\s+)?on|(?:turn|switch)\s+on\s+{_THE}{_WIFI}|enable\s+{_THE}{_WIFI}|{_WIFI}\s+on"),
    ("wifi", "off", rf"(?:turn|switch)\s+{_THE}{_WIFI}\s+off|(?:turn|switch)\s+off\s+{_THE}{_WIFI}|disable\s+{_THE}{_WIFI}|{_WIFI}\s+off"),
    ("lock", "", rf"lock\s+{_THE}(?:screen|computer|laptop|pc|workstation)|lock\s+screen"),
    ("cancel_shutdown", "", r"(?:cancel|abort|stop)\s+(?:the\s+)?(?:restart|reboot|shutdown|shut\s+down)"),
    ("restart", "", rf"(?:restart|reboot)(?:\s+{_THE}{_DEVICE})?"),
    ("shutdown", "", rf"(?:shut\s?down|power\s+off)(?:\s+{_THE}{_DEVICE})?|(?:turn|switch)\s+off\s+{_THE}{_DEVICE}"),
]]
def _sentence(text: str) -> str:
    return " ".join(text.lower().replace("’", "'").replace("%", " percent").replace("wi-fi", "wifi").split()).strip(" .!?,")


def parse(text: str) -> Optional[Tuple[str, str, Optional[int]]]:
    """(kind, direction, number) for a whole settings command, or None.
    A command later in a sentence ("this is too loud, turn the volume
    down") reaches here on its own: the interpreter's extract_command finds
    it where a verb from [interpreter] command_verbs starts a clause, and
    never in a question ("how do I turn the volume up") or a statement
    ("the TV is on mute")."""
    s = _sentence(text)
    for kind, direction, pattern in _PATTERNS:
        match = pattern.fullmatch(s)
        if match is None:
            continue
        raw = match.groupdict().get("n")
        number = to_number(raw) if raw else None
        if raw and number is None:
            continue
        return kind, direction, number
    return None


class SystemCommands:
    """F22: volume, mute, brightness, Wi-Fi, lock, restart and shutdown, each read back afterwards to
    verify (R12). No terminal commands (R10)."""
    def __init__(self, backend: Optional[SystemBackend] = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if backend is None:
            from jarvis.system.backend import default_backend

            backend = default_backend()
        self.backend = backend
        self._clock = clock
        self._sleep = sleep
        self._orchestrator = None
        self._lock = threading.Lock()
        self._countdown: Optional[Tuple[str, float]] = None  # (kind, monotonic end)
        # The open windows' titles for the restart/shutdown card (memory
        # only; never logged).
        self.preview_titles: List[str] = []

    # -- registration --------------------------------------------------------------------------

    def register(self, interpreter, orchestrator) -> None:
        """Before F3 ("turn off the computer" is not an app) and Groq."""
        self._orchestrator = orchestrator
        interpreter.register("system_settings", self.matcher)
        for action in ACTIONS:
            orchestrator.register_executor(action, getattr(self, f"_{action}"))
        orchestrator.controller.add_transition_listener(self.on_transition)
        orchestrator.add_cancel_hook(self.cancel_countdown)

    def on_transition(self, entry) -> None:
        """Stop during a restart/shutdown countdown aborts it."""
        if entry.event_type is None and self._countdown_kind() is not None:
            threading.Thread(target=self._abort_and_say, name="jarvis-abort-shutdown", daemon=True).start()

    # -- matching -------------------------------------------------------------------------------

    def matcher(self, cleaned: str):
        """Turn a system command into an action request (or a local reply for out-of-range values). None
        otherwise."""
        parsed = parse(cleaned)
        if parsed is None:
            return None
        kind, direction, number = parsed
        if kind == "cancel_shutdown":
            return Answer(self.cancel_countdown() or str(_REPLIES["nothing_scheduled"]))
        if kind in ("volume", "brightness"):
            return self._level_request(kind, direction, number)
        if kind == "mute":
            return ActionRequest("set_mute", {"muted": direction == "on", "display_name": "mute" if direction == "on" else "unmute"})
        if kind == "wifi":
            return self._wifi_request(direction == "on")
        if kind == "lock":
            return ActionRequest("lock_screen", {"display_name": "lock the screen"}, prompt=str(_REPLIES["lock_prompt"]))
        seconds = int(_CFG["shutdown_grace_seconds"])
        self.preview_titles = self._titles()
        prompt = str(_REPLIES[f"{kind}_prompt"]).format(seconds=seconds)
        return ActionRequest(kind, {"seconds": seconds, "display_name": kind}, prompt=prompt)

    def _level_request(self, kind: str, direction: str, number: Optional[int]):
        what = "Volume" if kind == "volume" else "Brightness"
        action = "set_volume" if kind == "volume" else "set_brightness"
        if direction == "set":
            if number is None or not 0 <= number <= 100:
                return Answer(str(_REPLIES["out_of_range"]).format(what=what))
            return ActionRequest(action, {"percent": number, "display_name": f"{kind} {number}%"})
        step = number if number is not None else int(_CFG[f"{kind}_step_percent"])
        if not 0 < step <= int(_CFG["max_step_percent"]):
            return Answer(str(_REPLIES["step_too_big"]).format(max=int(_CFG["max_step_percent"])))
        delta = step if direction == "up" else -step
        return ActionRequest(action, {"delta": delta, "display_name": f"{kind} {direction}"})

    def _wifi_request(self, on: bool):
        state = "on" if on else "off"
        try:
            if self.backend.get_wifi() == on:  # read-only
                return Answer(str(_REPLIES["wifi_already"]).format(state=state))
        except SystemUnavailable as missing:
            return Answer(self._missing(missing))
        except NoDevice:
            return Answer(str(_REPLIES["wifi_no_radio"]))
        except Exception:
            logger.info("Wi-Fi state unreadable before asking; asking anyway", exc_info=True)
        return ActionRequest("set_wifi", {"on": on, "display_name": f"Wi-Fi {state}"},
                             prompt=str(_REPLIES["wifi_prompt_on" if on else "wifi_prompt_off"]))

    # -- executors (each R12-verified) ------------------------------------------------------------

    def _set_volume(self, action_name: str, params: dict):
        return self._set_level("volume", self.backend.get_volume, self.backend.set_volume, params)

    def _set_brightness(self, action_name: str, params: dict):
        return self._set_level("brightness", self.backend.get_brightness, self.backend.set_brightness, params)

    def _set_level(self, kind: str, read, write, params: dict):
        tolerance = int(_CFG[f"{kind}_tolerance_percent"])
        try:
            before = read()
            target = int(params["percent"]) if "percent" in params else max(0, min(100, before + int(params["delta"])))
            self._report(f"set {kind} {target}%: immediate (L1)")
            if kind == "volume" and target > 0 and self.backend.get_mute():
                # Turning it up while muted would change nothing he can hear.
                self.backend.set_mute(False)
                self._report("unmuted")
            actual = self._apply(lambda: write(target), read, lambda v: abs(v - target) <= tolerance)
        except SystemUnavailable as missing:
            return False, self._missing(missing)
        except NoDevice:
            if kind != "volume" and is_mac():
                return False, str(_REPLIES["brightness_unsupported_mac"])
            return False, str(_REPLIES["no_speakers" if kind == "volume" else "brightness_unsupported"])
        if abs(actual - target) > tolerance:
            return False, str(_REPLIES[f"mismatch_{kind}"]).format(target=target, actual=actual)
        self._report(f"{kind} now {actual}%")
        return True, str(_REPLIES[f"{kind}_now"]).format(value=actual)

    def _set_mute(self, action_name: str, params: dict):
        muted = bool(params["muted"])
        try:
            actual = self._apply(lambda: self.backend.set_mute(muted), self.backend.get_mute, lambda v: v == muted)
            if actual != muted:
                verb = "mute" if muted else "unmute"
                return False, str(_REPLIES["mismatch_mute"]).format(verb=verb, state="unmuted" if muted else "muted")
            self._report("muted" if muted else "unmuted")
            return True, str(_REPLIES["muted"]) if muted else str(_REPLIES["unmuted"]).format(value=self.backend.get_volume())
        except SystemUnavailable as missing:
            return False, self._missing(missing)
        except NoDevice:
            return False, str(_REPLIES["no_speakers"])

    def _set_wifi(self, action_name: str, params: dict):
        on = bool(params["on"])
        try:
            actual = self._apply(lambda: self.backend.set_wifi(on), self.backend.get_wifi, lambda v: v == on)
        except SystemUnavailable as missing:
            return False, self._missing(missing)
        except Refused:
            self._report("Wi-Fi: Windows refused access")
            return False, str(_REPLIES["wifi_refused"])
        except NoDevice:
            return False, str(_REPLIES["wifi_no_radio"])
        state = "on" if on else "off"
        if actual != on:
            return False, str(_REPLIES["wifi_mismatch"]).format(target=state, actual="on" if actual else "off")
        self._report(f"Wi-Fi now {state}")
        return True, str(_REPLIES["wifi_now"]).format(state=state)

    def _lock_screen(self, action_name: str, params: dict):
        try:
            ok = self.backend.lock()
        except Exception:
            logger.exception("LockWorkStation failed")
            ok = False
        # R12's limit: a locked session can't check itself. "requested", never "done".
        return (True, str(_REPLIES["lock_requested"])) if ok else (False, str(_REPLIES["lock_failed"]))

    def _restart(self, action_name: str, params: dict):
        return self._schedule("restart", reboot=True, seconds=int(params.get("seconds", _CFG["shutdown_grace_seconds"])))

    def _shutdown(self, action_name: str, params: dict):
        return self._schedule("shutdown", reboot=False, seconds=int(params.get("seconds", _CFG["shutdown_grace_seconds"])))

    def _schedule(self, kind: str, reboot: bool, seconds: int):
        self.preview_titles = []
        for handler in logging.getLogger().handlers:  # the logbook and debug log are kept
            try:
                handler.flush()
            except Exception:
                pass
        verb, noun = ("restart", "restart") if reboot else ("shut down", "shutdown")
        try:
            self.backend.schedule_shutdown(seconds, reboot, str(_CFG["shutdown_message"]))
        except SystemUnavailable as missing:
            return False, self._missing(missing)
        except Refused:
            return False, str(_REPLIES["privilege_refused"]).format(verb=verb, noun=noun)
        with self._lock:
            self._countdown = (kind, self._clock() + seconds)
        self._report(f"{kind} scheduled in {seconds} s (apps not forced)")
        return True, str(_REPLIES[f"{kind}_requested"]).format(seconds=seconds)

    # -- cancelling a countdown ---------------------------------------------------------------------

    def _countdown_kind(self) -> Optional[str]:
        with self._lock:
            if self._countdown is None:
                return None
            kind, until = self._countdown
            if self._clock() >= until:
                self._countdown = None
                return None
            return kind

    def cancel_countdown(self) -> Optional[str]:
        """AbortSystemShutdown during the countdown. The sentence to say,
        or None when nothing is counting down."""
        kind = self._countdown_kind()
        if kind is None:
            return None
        with self._lock:
            self._countdown = None
        noun = "restart" if kind == "restart" else "shutdown"
        try:
            self.backend.abort_shutdown()
        except TooLate:
            self._report(f"{noun}: too late to cancel")
            return str(_REPLIES["too_late"]).format(doing="restarting" if kind == "restart" else "shutting down")
        except SystemUnavailable as missing:
            return self._missing(missing)
        self._report(f"{noun} cancelled")
        return str(_REPLIES["cancelled"]).format(Noun=noun.capitalize())

    def _abort_and_say(self) -> None:
        text = self.cancel_countdown()
        if text and self._orchestrator is not None:
            self._orchestrator.say_outside_command(text)

    # -- helpers -------------------------------------------------------------------------------------

    def _apply(self, write: Callable[[], None], read: Callable[[], object], ok: Callable[[object], bool]):
        """Write, then read back until it matches or [system]
        verify_timeout_seconds; on a mismatch say "Troubleshooting the
        issue" and try once more (R12). Returns the last value read."""
        actual = None
        for attempt in range(2):
            write()
            deadline = self._clock() + float(_CFG["verify_timeout_seconds"])
            while True:
                actual = read()
                if ok(actual) or self._clock() >= deadline:
                    break
                self._sleep(float(_CFG["verify_poll_interval_seconds"]))
            if ok(actual):
                return actual
            if attempt == 0:
                self._report("troubleshooting: the read-back didn't match, trying once more")
                if self._orchestrator is not None:
                    self._orchestrator.announce(str(_REPLIES["troubleshooting"]))
        return actual

    def _titles(self) -> List[str]:
        try:
            return self.backend.window_titles()
        except Exception:
            return []

    @staticmethod
    def _missing(missing: SystemUnavailable) -> str:
        return str(_SETUP["system_library_missing"]).format(library=missing.library)

    def _report(self, step: str) -> None:
        if self._orchestrator is not None:
            self._orchestrator.report_step(step)
