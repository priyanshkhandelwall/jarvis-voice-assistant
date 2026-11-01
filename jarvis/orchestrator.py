"""Wires one command -- typed, or heard after the wake word or push-to-
talk -- through the pipeline in 4.1: Controller -> Interpreter -> policy/
permit/confirmation -> a registered executor -> decision record + speech.
Typed and voice commands share this one path (handle_command); the only
difference is that a voice command arrives with the controller already in
LISTENING, because the wake word (or push-to-talk) put it there.

Executors are registered per action_name (F3/F4 add real ones in later
steps); nothing here knows how to actually open an app or find a file --
only how to run whatever executor is registered, through the trust/permit/
confirmation machinery.
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
import time
from datetime import datetime
from typing import Callable, Dict, Optional, Tuple

from jarvis.actions.command_text import BOTH, CANCEL, CONFIRM, PickSlot, confirm_intent
from jarvis.actions.wake_greeting import WakeGreeter
from jarvis.actions.sleep_wake import SleepRequest, is_wake_phrase, normalize
from jarvis.controller import Controller, Event, EventType, State
from jarvis.interpreter import ActionRequest, Answer, Interpreter
from jarvis.logbook import Logbook
from jarvis.settings import SETTINGS
from jarvis.speech import SpeechOutput
from jarvis.speech.mood import MoodWatcher
from jarvis.speech.rudeness import RudenessFilter

# (action_name, params) -> (verified, result_text). Runs after a permit is
# granted and redeemed; R12 (retry once, report plainly) is each
# executor's own responsibility, not the orchestrator's. An executor may
# return a third item, what goes in the decision record and debug log
# instead of the spoken text (F8: the screen's text is spoken, never logged).
Executor = Callable[[str, Dict], Tuple]

logger = logging.getLogger(__name__)

_NOT_UNDERSTOOD_TEXT = SETTINGS["interpreter"]["not_understood_text"]
_CONFIRM_WORD = SETTINGS["interpreter"]["confirm_word"]
_RUDE_REPLY = SETTINGS["rudeness"]["reply_text"]
_RUDE_REPLY_NOTHING_ASKED = SETTINGS["rudeness"]["reply_text_nothing_asked"]

_CONVERSATION = SETTINGS["conversation"]
_CANCEL_WORD = str(_CONVERSATION["cancel_word"])

SOURCE_TYPED = "typed"


def _is_confirm(text: str) -> bool:
    """"confirm" exactly; Whisper usually adds a full stop ("Confirm.")."""
    return normalize(text) == _CONFIRM_WORD


def _is_cancel(text: str) -> bool:
    return normalize(text) == _CANCEL_WORD


class Orchestrator:
    """Runs one interaction end to end: rudeness filter, interpreter, controller events, the confirm flow,
    the executor, then speech and the log. It owns no state; the controller does."""
    def __init__(
        self,
        controller: Controller,
        interpreter: Interpreter,
        speech: SpeechOutput,
        logbook: Optional[Logbook] = None,
        rudeness: Optional[RudenessFilter] = None,
        notice: Optional[Callable[[str], None]] = None,
        clock_hour: Optional[Callable[[], int]] = None,
        greeter: Optional[WakeGreeter] = None,
        mood: Optional[MoodWatcher] = None,
    ) -> None:
        """`greeter` picks the wake greeting (the app passes one that
        remembers across restarts). Without one, an in-memory greeter is
        used; `clock_hour` (tests) fixes the hour it sees. `mood` is F14's
        check-in (optional: without it, no mood is guessed)."""
        self._controller = controller
        # Shows a short notice on the dashboard without logging it (used for
        # typed text while asleep, which F15 says must not be logged).
        self._notice = notice
        if greeter is None:
            now = datetime.now if clock_hour is None else (lambda: datetime.now().replace(hour=clock_hour(), minute=0))
            greeter = WakeGreeter(now=now)
        self._greeter = greeter
        # Welcome-back needs to know when she went to sleep, by any route
        # ("go to sleep" or the auto-sleep timer).
        controller.add_transition_listener(self._note_sleep)
        # 65 s after "Turn Wi-Fi off?" she was still
        # CONFIRMING -- an expired confirmation was only noticed when a
        # "confirm" came, and until then only confirm or cancel was heard.
        controller.add_transition_listener(self._watch_confirmation)
        self._confirmation_seconds = float(SETTINGS["controller"]["confirmation_timeout_seconds"])
        self._interpreter = interpreter
        self._speech = speech
        self._logbook = logbook  # optional: only needed to forward ActionRequest.candidates (F5)
        self._rudeness = rudeness  # optional: F2's word list; without it, text passes through unchanged
        self._mood = mood
        self._executors: Dict[str, Executor] = {}
        # Per command thread: callbacks queued by when_command_finishes().
        self._command = threading.local()
        # One wake at a time: see wake().
        self._wake_lock = threading.Lock()
        # Which list "open one" picks from (F4's files or F3's apps): the
        # one shown last.
        self.picks = PickSlot()
        # What was looked at last ("screen" find or "camera"
        # look), so "where is it" follows the newest of the two.
        self._topic: Optional[str] = None
        # F21: while JARVIS-started work runs, these words end it like Stop.
        self._skip_words: set = set()
        self._task_running = threading.Event()
        # F22: "cancel" with no confirmation pending asks these first (a
        # restart countdown); each returns the sentence to say, or None.
        self._cancel_hooks: list = []
        # "Say confirm or cancel.": when it was last said (rate limit).
        self._clock = time.monotonic
        self._last_reminder: Optional[float] = None
        self._reminder_lock = threading.Lock()
        self._confirm_timer: Optional[threading.Timer] = None
        # ("confirm, confirm" on "Forget everything" ->
        # "Forgotten: all 3", THEN "Say confirm once more", then "Forgotten.").
        # The second confirm ran on another thread while the first one's prompt
        # was still being said. A confirm prompt is now said in full before the
        # next confirm/cancel is handled; one still queued when a confirm or
        # cancel arrives is dropped. Re-entrant: an executor may ask again.
        self._answer_lock = threading.RLock()
        self._answers_waiting = 0
        self._answers_count_lock = threading.Lock()

    def _note_sleep(self, entry) -> None:
        if entry.event_type == EventType.GO_TO_SLEEP and entry.accepted:
            self._greeter.note_sleep()

    def _watch_confirmation(self, entry) -> None:
        """Entering CONFIRMING arms one timer for that confirmation (controller
        thread: kept quick, the timer does the work). Leaving
        CONFIRMING (confirmed, cancelled, stopped) cancels that timer."""
        if not entry.accepted:
            return
        if entry.from_state == State.CONFIRMING and entry.to_state != State.CONFIRMING:
            timer, self._confirm_timer = self._confirm_timer, None
            if timer is not None:
                timer.cancel()
            return
        if not (entry.to_state == State.CONFIRMING and entry.from_state != State.CONFIRMING):
            return
        pending = self._controller.pending_confirmation()
        if pending is None:
            return
        timer = threading.Timer(self._confirmation_seconds + 0.5, self._expire_if_stale, args=(pending,))
        timer.daemon = True
        self._confirm_timer = timer
        timer.start()

    def _expire_if_stale(self, pending, timeout: float = 5.0) -> None:
        """Still waiting on the same, now expired, confirmation: it is
        cancelled the way a spoken "cancel" is, and she says why."""
        c = self._controller
        if c.state != State.CONFIRMING or c.pending_confirmation() is not pending or not pending.is_expired():
            return
        c.submit(Event(EventType.CANCEL_RECEIVED, {"reason": "confirmation expired"}))
        if not _wait_for(c, State.SPEAKING, timeout):
            return
        reply = str(_CONVERSATION["confirmation_expired_reply"])
        self.report_step(f"saying: {reply}", closing=True)
        self._speech.speak(reply)
        c.submit(Event(EventType.SPEECH_FINISHED))
        _wait_for(c, State.AWAKE, timeout)

    def set_notice(self, notice: Callable[[str], None]) -> None:
        """Where unlogged on-screen notices go (the dashboard sets this)."""
        self._notice = notice

    @property
    def controller(self) -> Controller:
        """So a pluggable module's own register() (F4's FileFinder) can
        subscribe to the controller directly -- e.g. for Stop-awareness --
        without needing a separate reference threaded through everywhere."""
        return self._controller

    def report_step(self, text: str, replaces: str | None = None, closing: bool = False, session_only: bool = False) -> None:
        """For pluggable matchers/executors: say what is happening right now
        (shown live in the dashboard's activity panel). `closing` shows it
        after what she said; `session_only` keeps it out of the debug log
        (see Logbook.add_step). A no-op without a logbook."""
        if self._logbook is not None:
            self._logbook.add_step(text, replaces=replaces, closing=closing, session_only=session_only)

    def record_online_destination(self, destination: str) -> None:
        """R3: where a request went online (provider, model) -- decision
        record and debug log only, never the Live Activity panel."""
        if self._logbook is not None:
            self._logbook.set_online_destination(destination)

    def announce(self, text: str) -> None:
        """Say a short line while a command is still being understood --
        R3's "Checking online" (developer decision D: spoken during
        UNDERSTANDING). Skipped if Stop has already been pressed."""
        if not self._controller.is_stop_requested():
            self._speech.speak(text)

    def when_command_finishes(self, callback: Callable[[], None]) -> None:
        """Run `callback` once the command being handled on this thread is
        over -- after its reply has been spoken, or after Stop, a timeout or
        an error. F2 uses it to keep the Internet panel on ONLINE until the
        answer has finished speaking. Outside a command it runs at once."""
        pending = getattr(self._command, "finishers", None)
        if pending is None:
            callback()
        else:
            pending.append(callback)

    def register_executor(self, action_name: str, executor: Executor) -> None:
        """Register the function that runs `action_name` once the controller has issued its permit."""
        self._executors[action_name] = executor

    def handle_typed_command(self, text: str, timeout: float = 5.0) -> None:
        """The typed-command box (2.4's backup when the microphone fails)."""
        self.handle_command(text, SOURCE_TYPED, timeout)

    def handle_command(self, text: str, source: str = "typed", timeout: float = 5.0) -> None:
        """One command, from any source. `source` is "typed", or the voice
        source ("wake word" / "push-to-talk") whose SPEECH_STARTED
        already moved the controller to LISTENING."""
        self._in_command_scope(lambda: self._handle(text, source, timeout))

    def _in_command_scope(self, run: Callable[[], None]) -> None:
        """Runs one interaction, then its when_command_finishes callbacks.
        Re-entrant: an outer interaction's callbacks survive an inner one."""
        outer = getattr(self._command, "finishers", None)
        self._command.finishers = []
        self._command.check_in = None  # F14: never carried over from an earlier command
        try:
            run()
        finally:
            finishers, self._command.finishers = self._command.finishers, outer
            for finish in finishers:
                try:
                    finish()
                except Exception:
                    logger.exception("command-finished callback raised")

    def note_topic(self, topic: str) -> None:
        """A screen find or a camera look just happened; a
        following "where is it" is about the newest one."""
        self._topic = topic

    def latest_topic(self) -> Optional[str]:
        """What was looked at last ('screen' or 'camera'), so 'where is it' follows the newest one."""
        return self._topic

    def add_cancel_hook(self, hook: Callable[[], Optional[str]]) -> None:
        """F22: a plain "cancel" with nothing to confirm is offered to
        `hook` (the restart/shutdown countdown) before "nothing to cancel"."""
        self._cancel_hooks.append(hook)

    def say_outside_command(self, text: str) -> None:
        """F22: said after a Stop that cancelled a restart countdown ("Restart
        cancelled.") -- no command is running then. Shown in Live Activity
        as its own row with a "saying:" line."""
        if self._logbook is not None:
            self._logbook.record_status(f"saying: {_one_line(text)}")
        self._speech.speak(text)

    def remind_confirm_or_cancel(self) -> bool:
        """Something other than confirm or cancel was heard
        (or typed) while a confirmation waits ("Confine", "can find"). She
        says "Say confirm or cancel." (cached audio), at most once per
        [conversation] confirm_reminder_seconds. True if she said it."""
        pending = self._controller.pending_confirmation()
        if self._controller.state != State.CONFIRMING or pending is None:
            return False
        with self._reminder_lock:
            now = self._clock()
            if self._last_reminder is not None and now - self._last_reminder < float(_CONVERSATION["confirm_reminder_seconds"]):
                self.report_step(f"reminder not repeated (said {now - self._last_reminder:.0f} s ago)")
                return False
            self._last_reminder = now
        return self._say_while_pending(str(_CONVERSATION["confirm_reminder_reply"]), pending)

    def _still_pending(self, pending) -> bool:
        c = self._controller
        return (c.state == State.CONFIRMING and c.pending_confirmation() is pending and not c.is_stop_requested()
                and self._answers_waiting == 0)  # a confirm or cancel already heard: the prompt is moot

    def _answering(self, handle: Callable[[], None]) -> None:
        """A confirm or cancel: handled one at a time, after any confirm prompt
        being said has finished (see _answer_lock)."""
        with self._answers_count_lock:
            self._answers_waiting += 1
        with self._answer_lock:
            with self._answers_count_lock:
                self._answers_waiting -= 1
            handle()

    def _say_while_pending(self, text: str, pending) -> bool:
        """("Say confirm or cancel." after the screen was
        sent; "Say confirm once more" around "Forgotten."): a confirm prompt
        is said only while THAT confirmation is still pending -- checked
        when it would be heard, so one queued behind other speech is
        dropped once the flow has finished or been cancelled. Order:
        prompt -> (reminder) -> result, never a prompt after the result.
        Shown in Live Activity only if it was said."""
        if not self._speech.speak_while(text, lambda: self._still_pending(pending)):
            self.report_step(f"not said (the confirmation was already over): {text}")
            return False
        # The reminder was said but never shown in Live Activity.
        # In time order, not as a closing line -- the
        # flow goes on after a prompt, so it must show before its result.
        self.report_step(f"saying: {text}")
        return True

    def add_skip_words(self, words) -> None:
        """F21: "skip" while a JARVIS-started task runs ends it at once (a
        Stop, R8). Outside such a task the words mean nothing special."""
        self._skip_words |= {normalize(w) for w in words}

    def run_startup_task(self, label: str, work: Callable[[], object], timeout: float = 5.0) -> bool:
        """F17's JARVIS-started work: STARTUP_TASK_REQUESTED (AWAKE ->
        UNDERSTANDING), so the controller tags it JARVIS_INITIATED. `work()`
        returns what to do -- an Answer to speak, an ActionRequest that goes
        through policy and permits like any command (F21's L3 confirm), or
        None for nothing to say. False if she wasn't AWAKE."""
        c = self._controller
        if c.state != State.AWAKE:
            return False
        c.submit(Event(EventType.STARTUP_TASK_REQUESTED, {"task": label}))
        if not _wait_for(c, State.UNDERSTANDING, timeout):
            return False

        def run() -> None:
            result = work()
            if c.is_stop_requested():
                return
            if isinstance(result, ActionRequest):
                self._request_action(result.action_name, result.params, timeout, result.candidates, "", result.prompt)
            elif isinstance(result, Answer) and result.text:
                self._speak_answer(result.text, timeout, result.candidates, logged=result.logged, shown=result.shown)
            else:
                self._speak_answer("", timeout)

        with self.skippable():
            self._in_command_scope(run)
        return True

    @contextlib.contextmanager
    def skippable(self):
        """While inside, a typed "skip" ([briefing] skip_words) is a Stop.
        Re-entrant: the briefing's fetch, inside the first-wake
        task, used to clear it on the way out, so the briefing could no
        longer be skipped once she started saying it."""
        was_set = self._task_running.is_set()
        self._task_running.set()
        try:
            yield
        finally:
            if not was_set:
                self._task_running.clear()

    def skippable_until_spoken(self) -> None:
        """F21: the rest of this command -- speaking
        its answer included -- ends on a typed "skip", like Stop. "Brief me"
        was skippable only while fetching, never while she said it."""
        if getattr(self._command, "finishers", None) is None:
            return  # not inside a command: nothing left to skip
        self._task_running.set()
        self.when_command_finishes(self._task_running.clear)

    def wake(self, source: str, timeout: float = 5.0) -> bool:
        """SLEEPING -> greeting -> AWAKE. `source` says how she was woken
        (wake word, wake phrase, hotkey, typed wake phrase). Returns True if
        she woke; False if she wasn't asleep (the table refuses WAKE).

        Every route comes through here, and exactly one greeting is spoken
        per wake. Two routes can fire for one phrase ("Hey JARVIS, wake up":
        the wake word AND the transcript); before the refused
        second WAKE still saw SPEAKING, spoke a second greeting and sent its
        own SPEECH_FINISHED, which could end whatever was speaking. Now a
        wake that finds her not SLEEPING does nothing. The greeting (picked
        locally by WakeGreeter, never online) is put on the WAKE event, so
        the activity log shows what she said."""
        c = self._controller
        with self._wake_lock:
            if c.state != State.SLEEPING:
                return False
            greeting = self._greeter.choose()
            c.submit(Event(EventType.WAKE, {"source": source, "greeting": greeting}))
            if not _wait_for(c, State.SPEAKING, timeout):
                return False
        self._speech.speak(greeting)
        c.submit(Event(EventType.SPEECH_FINISHED))
        # Nothing else runs on a wake -- the briefing and the
        # topic check used to start by themselves on the first wake of the
        # day ("started by JARVIS: morning briefing"); now only when asked.
        _wait_for(c, State.AWAKE, timeout)
        return True

    def _handle(self, text: str, source: str, timeout: float) -> None:
        c = self._controller
        voice = source != SOURCE_TYPED
        # Whisper writes sentences ("Open Chrome."); the deterministic
        # matchers expect the words alone, as typed.
        stripped = text.strip().rstrip(" .!?,") if voice else text.strip()

        if self._task_running.is_set() and normalize(stripped) in self._skip_words:
            # F21: "skip" during the briefing ends it at once, like Stop.
            c.request_stop(source=f'"{normalize(stripped)}" during the briefing')
            return

        if not voice and normalize(stripped) == "stop" and c.state != State.CONFIRMING:
            # A typed "stop" went online as a question,
            # so "Say stop to cancel" did not cancel a restart from the
            # keyboard. Spoken "stop" is already a Stop (the listener);
            # while confirming, "stop" stays a cancel word.
            c.request_stop(source="typed stop")
            return

        if c.state == State.SLEEPING:
            # F15: asleep, only a wake phrase does anything. Other typed text
            # is discarded without being logged; a short notice says how to
            # wake her. (Spoken words while asleep never reach here: the
            # voice listener shows them as "heard while asleep".)
            if is_wake_phrase(stripped):
                self.wake(f"{source} wake phrase", timeout)
            elif self._notice is not None:
                self._notice(str(_CONVERSATION["asleep_typed_notice"]))
            return

        # While a confirmation is pending, a sentence counts if
        # its only command words are confirm words ("Please send it online.
        # Confirm.") or only cancel words; both kinds: she asks again.
        intent = confirm_intent(stripped) if c.state == State.CONFIRMING else None
        short = normalize(stripped).split()
        if intent is None and len(short) <= 3 and any(w.startswith(("conf", "canc")) for w in short):
            # "Yes, Confirmer" with nothing pending went
            # online and Groq said "Confirmed." A short confirm or cancel (a
            # Whisper spelling included) is answered here; a longer sentence
            # ("how do I cancel a subscription") is still a question.
            intent = confirm_intent(stripped)
        if c.state == State.CONFIRMING:
            # "Turn off Wi-Fi" and "Restart the laptop" were
            # cancelled with nothing in the log -- what is heard while she
            # waits, and what it counted as, is now on the pending record.
            self.report_step(f'heard while waiting: "{_one_line(stripped)}" -> {intent or "not confirm or cancel"}')
        if intent == BOTH:
            self._speech.speak(str(_CONVERSATION["confirm_or_cancel_reply"]))
            return

        if intent == CANCEL or _is_cancel(stripped):
            if voice and c.state == State.LISTENING:
                c.submit(Event(EventType.LISTEN_TIMEOUT, {"reason": "spoken cancel"}))
                _wait_for(c, State.AWAKE, timeout)
            self._answering(lambda: self._handle_cancel(timeout))
            return

        if intent == CONFIRM or _is_confirm(stripped):
            if voice and c.state == State.LISTENING:
                # A spoken "confirm" with nothing pending arrives in
                # LISTENING. Close the listen first; _handle_confirm then
                # says nothing is pending. (With a confirmation pending she
                # is in CONFIRMING, and the listener hands "confirm" over
                # without entering LISTENING.)
                c.submit(Event(EventType.LISTEN_TIMEOUT, {"reason": "spoken confirm"}))
                _wait_for(c, State.AWAKE, timeout)
            # Always routed here, even when nothing is pending -- Section
            # 5: "confirm with nothing pending does nothing, and she says
            # so", not "I don't understand". _handle_confirm covers both
            # the case where CONFIRM_RECEIVED is legal (from CONFIRMING)
            # and where it's deny-by-defaulted (from anywhere else).
            self._answering(lambda: self._handle_confirm(timeout))
            return

        if c.state == State.CONFIRMING:
            # Neither confirm nor cancel (typed; the listener never hands
            # these over): a reminder instead of a silent wait.
            self.remind_confirm_or_cancel()
            return

        if not voice:
            c.submit(Event(EventType.SPEECH_STARTED, {"source": SOURCE_TYPED}))
        if not _wait_for(c, State.LISTENING, timeout):
            return
        c.submit(Event(EventType.COMMAND_CAPTURED, {"text": stripped, "source": source}))
        if not _wait_for(c, State.UNDERSTANDING, timeout):
            return

        # F2 rudeness: she says it wasn't nice and still does what was asked,
        # so the insult is taken out before the command is interpreted.
        prefix = ""
        to_interpret = stripped
        rude = False
        if self._rudeness is not None:
            check = self._rudeness.check(stripped)
            if check.rude:
                rude = True
                self.report_step("noted: rude wording")
                if not check.cleaned:
                    self._speak_answer(_RUDE_REPLY_NOTHING_ASKED, timeout)
                    return
                prefix = _RUDE_REPLY
                to_interpret = check.cleaned

        # F14: a guess at how he sounds. It never changes what is interpreted
        # or done -- at most one short question is said after the answer.
        # Rudeness wins: a rude command is not scored at all.
        self._command.check_in = None
        if self._mood is not None and not rude:
            fine_reply = self._mood.reply_to_check_in(stripped)
            if fine_reply is not None:
                self._speak_answer(fine_reply, timeout)
                return
            note = self._mood.hear(stripped)
            if note.noticed:
                self.report_step(note.noticed, session_only=True)
            self._command.check_in = note.check_in

        result = self._interpreter.interpret(to_interpret)
        if c.is_stop_requested():
            # Stop arrived while the command was being understood (for
            # example during a Groq call): the controller is already back
            # in WAITING, so whatever came back is dropped, never spoken.
            return
        if isinstance(result, SleepRequest):
            self._go_to_sleep(result.reply, timeout, prefix)
        elif isinstance(result, Answer):
            self._speak_answer(result.text, timeout, result.candidates, prefix, logged=result.logged, shown=result.shown)
        elif isinstance(result, ActionRequest):
            self._request_action(result.action_name, result.params, timeout, result.candidates, prefix, result.prompt)
        else:
            self._understand_failed(timeout, prefix)

    # -- internal ------------------------------------------------------

    @staticmethod
    def _with_prefix(prefix: str, text: str) -> str:
        return f"{prefix} {text}" if prefix else text

    def _say(self, prefix: str, text: str) -> str:
        """What she says to finish this command: the rude-reply prefix, the
        answer, then F14's check-in if one is due. The check-in comes only
        after the request is done, and never after a reply that is itself a
        question ("Hamilton in Ontario, or ...?"): one question at a time."""
        spoken = self._with_prefix(prefix, text)
        check_in = getattr(self._command, "check_in", None)
        self._command.check_in = None
        # A numbered list to pick from ("1. paint\n2.
        # Paint 3D\nSay 'open' and the number...") is a question too.
        if not check_in or self._mood is None or text.rstrip().endswith("?") or re.search(r"(^|\n)\s*1[.)] ", text):
            return spoken
        self.report_step(f"checking in: {check_in}", closing=True, session_only=True)
        self._mood.checked_in(check_in)
        return f"{spoken} {check_in}"

    def _speak_answer(self, text: str, timeout: float, candidates=None, prefix: str = "", logged: Optional[str] = None,
                      shown: Optional[str] = None) -> None:
        """`logged` (R4, F20): what the decision record and debug.log hold
        instead of the spoken text; the spoken text is then shown in Live
        Activity for this session only. `shown` (F19): what Live Activity's
        "saying:" line shows instead of the spoken text, which then appears
        nowhere but in her voice (a file's content)."""
        c = self._controller
        if self._logbook is not None and candidates:
            self._logbook.add_candidates(candidates)
        c.submit(Event(EventType.ANSWER_READY, {"text": logged or shown or text}))
        if not _wait_for(c, State.SPEAKING, timeout):
            return
        if shown and text:
            self.report_step(f"saying: {_one_line(shown)}", closing=True)
        elif logged and text:
            self.report_step(f"saying: {_one_line(self._with_prefix(prefix, text))}", closing=True, session_only=True)
        if text:
            self._speech.speak(self._say(prefix, text))
        c.submit(Event(EventType.SPEECH_FINISHED))
        _wait_for(c, State.AWAKE, timeout)

    def _go_to_sleep(self, reply: str, timeout: float, prefix: str = "") -> None:
        """"Goodnight." then SLEEPING -- entirely local, never online."""
        c = self._controller
        c.submit(Event(EventType.ANSWER_READY, {"text": reply}))
        if not _wait_for(c, State.SPEAKING, timeout):
            return
        self._speech.speak(self._with_prefix(prefix, reply))
        c.submit(Event(EventType.GO_TO_SLEEP, {"source": "command"}))
        _wait_for(c, State.SLEEPING, timeout)

    def _handle_cancel(self, timeout: float) -> None:
        """"Cancel" drops a pending confirmation (Section 5 still applies to
        any later "confirm": there is nothing left to confirm)."""
        c = self._controller
        if c.state != State.CONFIRMING or c.pending_confirmation() is None:
            for hook in self._cancel_hooks:
                said = hook()
                if said:
                    self.say_outside_command(said)
                    return
            self._speech.speak(str(_CONVERSATION["nothing_to_cancel_reply"]))
            return
        c.submit(Event(EventType.CANCEL_RECEIVED, {}))
        if not _wait_for(c, State.SPEAKING, timeout):
            return
        self.report_step(f"saying: {_CONVERSATION['cancelled_reply']}", closing=True)
        self._speech.speak(str(_CONVERSATION["cancelled_reply"]))
        c.submit(Event(EventType.SPEECH_FINISHED))
        _wait_for(c, State.AWAKE, timeout)

    def _understand_failed(self, timeout: float, prefix: str = "") -> None:
        c = self._controller
        c.submit(Event(EventType.UNDERSTAND_FAILED, {}))
        if not _wait_for(c, State.SPEAKING, timeout):
            return
        self._speech.speak(self._say(prefix, _NOT_UNDERSTOOD_TEXT))
        c.submit(Event(EventType.SPEECH_FINISHED))
        _wait_for(c, State.AWAKE, timeout)

    def _request_action(
        self, action_name: str, params: Dict, timeout: float, candidates=None, prefix: str = "", prompt: str = ""
    ) -> None:
        c = self._controller
        if self._logbook is not None and candidates:
            # Must happen before ACTION_REQUESTED is submitted -- see
            # Logbook.add_candidates's own docstring.
            self._logbook.add_candidates(candidates)
        c.submit(Event(EventType.ACTION_REQUESTED, {"action_name": action_name, "params": params}))
        # Lands on CONFIRMING (L2/L3/L4 -- wait for "confirm"), SPEAKING
        # (denied -- nothing more to do here), or ACTING (L0/L1 -- runs
        # immediately).
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if c.state in (State.CONFIRMING, State.SPEAKING, State.ACTING):
                break
            time.sleep(0.01)

        if c.state == State.SPEAKING:
            # Denied by policy -- speak the reason and finish.
            last = c.last_transition()
            reason = last.reason if last is not None else "unknown"
            self._speak_result(prefix, f"I can't do that: {reason}", False, timeout)
            return
        if c.state == State.CONFIRMING:
            # Preview issued -- the dashboard shows it and waits for
            # "confirm". The action's own prompt says what will happen.
            # Shown too -- "Turn off Wi-Fi" left only its
            # "heard:" line, so nothing showed she was waiting.
            # The prompt is shown, and said, BEFORE the
            # waiting line -- the log showed it after.
            pending = c.pending_confirmation()
            if prompt:
                self.report_step(f"saying: {_one_line(self._with_prefix(prefix, prompt))}")
            if pending is not None:
                self.report_step(f"waiting for confirm ({pending.trust_level.name}, {pending.confirmations_required} needed)")
            if prompt:
                with self._answer_lock:  # said in full before his confirm or cancel is handled
                    self._speech.speak(self._with_prefix(prefix, prompt))
            return
        if c.state == State.ACTING:
            self._run_executor(action_name, params, timeout, prefix)

    def _handle_confirm(self, timeout: float) -> None:
        c = self._controller
        pending = c.pending_confirmation()
        payload = (
            {"action_name": pending.action_name, "params": dict(pending.params)}
            if pending is not None
            else {"action_name": "", "params": {}}
        )
        log_len_before = c.transition_count()
        c.submit(Event(EventType.CONFIRM_RECEIVED, payload))

        # Waiting for a specific target STATE doesn't work here: a
        # confirm submitted from anywhere other than CONFIRMING is
        # deny-by-defaulted by the table, which leaves state completely
        # unchanged (no reroute to SPEAKING happens for that case, unlike
        # a mismatch/expiry while genuinely CONFIRMING). Waiting for the
        # log to grow and reading the newest entry covers both shapes.
        # transition_count()/last_transition() are lock-protected
        # accessors -- never touch controller.transition_log directly
        # from here (see the developer's threading-audit review).
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and c.transition_count() <= log_len_before:
            time.sleep(0.005)
        if c.transition_count() <= log_len_before:
            return  # timed out; nothing safe left to do

        last = c.last_transition()
        if last is None:
            return
        if not last.accepted:
            # "deny by default: no such transition in the
            # fixed table" was spoken -- the controller's own wording, not hers.
            if last.reason.startswith("deny by default"):
                self._speech.speak(str(_CONVERSATION["nothing_to_confirm_reply"]))
            else:
                self._speech.speak(f"Nothing to confirm: {last.reason}")
            if c.state == State.SPEAKING:
                # Rerouted through SPEAKING (mismatch/expired while
                # genuinely CONFIRMING) -- finish that interaction.
                c.submit(Event(EventType.SPEECH_FINISHED))
                _wait_for(c, State.AWAKE, timeout)
            return
        if c.state == State.CONFIRMING:
            # L4's first of two: she asks for the second -- only while it is
            # still the one pending (a quick second "confirm" may finish it first).
            if pending is not None:
                self._say_while_pending(str(_CONVERSATION["confirm_again_reply"]), c.pending_confirmation())
            return
        if c.state == State.ACTING and pending is not None:
            self._run_executor(pending.action_name, dict(pending.params), timeout)

    def _run_executor(self, action_name: str, params: Dict, timeout: float, prefix: str = "") -> None:
        c = self._controller
        permit = c.current_permit()
        if permit is None:
            return
        ok, redeem_reason = c.redeem_permit(permit.id, action_name, params)
        logged: list = []
        if not ok:
            # Should not happen if the state machine is consistent, but
            # never silently do nothing with an action we said would run.
            verified, result_text = False, f"could not run: {redeem_reason}"
        else:
            executor = self._executors.get(action_name)
            if executor is None:
                verified, result_text = False, "no executor registered for this action"
            else:
                verified, result_text, *logged = executor(action_name, params)

        c.submit(Event(EventType.ACTION_DONE, {"result": logged[0] if logged else result_text}))
        _wait_for(c, State.VERIFYING, timeout)
        c.submit(Event(EventType.VERIFICATION_COMPLETE, {"verified": verified}))

        # Every result is spoken AND shown as "saying:..." in
        # Live Activity (searches, screen, camera, explain). When the spoken
        # text holds laptop data (the executor logged something else, F8),
        # the line is shown for this session only, never written to debug.log.
        private = bool(logged) and logged[0] != result_text
        # F19: an executor may return a 4th item, what Live Activity shows
        # instead of what she says (a file's content is said, never shown).
        shown = logged[1] if len(logged) > 1 else None
        self._speak_result(prefix, result_text, private, timeout, shown)

    def _speak_result(self, prefix: str, text: str, private: bool, timeout: float, shown: Optional[str] = None) -> None:
        c = self._controller
        if not _wait_for(c, State.SPEAKING, timeout):
            if c.is_stop_requested():
                return  # R8: Stop wins, nothing more is said
            # Never silent: if the state machine didn't reach
            # SPEAKING in time, the result is still said and shown.
            logger.warning("result not in SPEAKING after %.1fs; saying it anyway", timeout)
        spoken = self._with_prefix(prefix, text)
        if shown:
            self.report_step(f"saying: {_one_line(shown)}", closing=True)
        else:
            self.report_step(f"saying: {_one_line(spoken)}", closing=True, session_only=private)
        self._speech.speak(self._say(prefix, text))
        if c.state == State.SPEAKING:
            c.submit(Event(EventType.SPEECH_FINISHED))
        _wait_for(c, State.AWAKE, timeout)


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _wait_for(controller: Controller, state: State, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if controller.state == state:
            return True
        time.sleep(0.005)
    return controller.state == state
