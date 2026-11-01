"""F5: live activity log and decision records.

Subscribes to the controller's transition stream (Controller.set_transition_
listener) and assembles one DecisionRecord per command -- from the moment
a command is captured to the moment JARVIS finishes speaking about it, or
until Stop cuts it short. Kept in memory for the session only (OPEN #8,
per the developer's instruction for this build).

"Why did you do that?" (answer_why) is built purely by reading a stored
record's fields back in a fixed template -- never by asking a model to
explain itself after the fact (R13).
"""

from __future__ import annotations

import dataclasses
import threading
from collections import deque

from jarvis.controller.controller import TransitionLogEntry
from jarvis.controller.events import EventType
from jarvis.controller.permits import OriginTag
from jarvis.controller.states import State
from jarvis.controller.trust import TrustLevel
from jarvis.logbook.debug_log import DebugLog
from jarvis.logbook.records import Candidate, DecisionRecord, redact_answer, redact_sensitive, redact_value
from jarvis.settings import SETTINGS

NOTHING_YET = "I haven’t done anything yet."


class Logbook:
    """F5: listens to every controller transition and builds the decision records Live Activity shows and
    'why' explains."""
    def __init__(self, debug_log: DebugLog | None = None) -> None:
        # Optional: raw per-record detail goes here for troubleshooting so
        # the dashboard's activity panel can stay clean (see debug_log.py).
        self._debug_log = debug_log
        max_records = int(SETTINGS["logbook"]["max_records"])
        # Collections: a deque with maxlen keeps only the newest records; the oldest fall off in O(1).
        self._records: deque[DecisionRecord] = deque(maxlen=max_records)
        self._in_progress: DecisionRecord | None = None
        self._lock = threading.RLock()

    # -- feeding from the controller ---------------------------------

    def on_transition(self, entry: TransitionLogEntry) -> None:
        """Registered via controller.set_transition_listener. Runs on the
        controller's single worker thread, same as every other transition
        side effect."""
        with self._lock:
            if entry.event_type is None:
                self._handle_stop(entry)
                return
            command_scoped_rejections = (EventType.ACTION_REQUESTED, EventType.CONFIRM_RECEIVED)
            if not entry.accepted and entry.event_type not in command_scoped_rejections:
                return  # a bare deny-by-default rejection isn't part of any command
            handler = self._HANDLERS.get(entry.event_type)
            if handler is not None:
                handler(self, entry)

    def _handle_command_captured(self, entry: TransitionLogEntry) -> None:
        self._in_progress = DecisionRecord.start(heard=entry.payload.get("text"))

    def _handle_startup_task(self, entry: TransitionLogEntry) -> None:
        """F17: work JARVIS started herself (first-wake topics, F21's
        briefing). The origin comes from the controller's entry."""
        self._in_progress = DecisionRecord.start(heard=None)
        self._in_progress.started_by_jarvis = redact_sensitive(str(entry.payload.get("task") or "a task"))
        self._in_progress.origin = entry.origin

    def _handle_action_requested(self, entry: TransitionLogEntry) -> None:
        if self._in_progress is None:
            return
        action_name = entry.payload.get("action_name")
        params = entry.payload.get("params", {})
        self._in_progress.understood = redact_value({"action_name": action_name, "params": params})
        self._in_progress.choice = redact_sensitive(action_name)
        self._in_progress.rule_or_trust_level = entry.trust_level
        self._in_progress.origin = entry.origin
        if not entry.accepted:
            self._in_progress.result = redact_sensitive(f"denied: {entry.reason}")

    def _handle_answer_ready(self, entry: TransitionLogEntry) -> None:
        if self._in_progress is None:
            return
        # Her answer is checked for real secrets only (redact_answer).
        answer = entry.payload.get("text")
        self._in_progress.understood = {"answer": redact_answer(answer) if isinstance(answer, str) else redact_value(answer)}
        self._in_progress.choice = redact_sensitive("answer the question")
        self._in_progress.rule_or_trust_level = entry.trust_level
        self._in_progress.origin = entry.origin
        self._in_progress.result = redact_sensitive("answered")

    def _handle_confirm_received(self, entry: TransitionLogEntry) -> None:
        """F7: a rejected confirm (mismatch/expired) needs to show up in
        the record's result, same as a denied action already does. A
        successful confirm -- partial (L4's first of two) or final --
        doesn't set `result` here; the real outcome still comes from
        ACTION_DONE once the action actually runs."""
        if self._in_progress is None:
            return
        if not entry.accepted:
            self._in_progress.result = redact_sensitive(f"denied: {entry.reason}")

    def _handle_understand_failed(self, entry: TransitionLogEntry) -> None:
        if self._in_progress is None:
            return
        self._in_progress.choice = redact_sensitive("none -- could not understand the command")
        self._in_progress.result = redact_sensitive("could not understand")

    def _handle_action_done(self, entry: TransitionLogEntry) -> None:
        if self._in_progress is None:
            return
        self._in_progress.result = redact_sensitive(str(entry.payload.get("result", "done")))

    def _handle_verification_complete(self, entry: TransitionLogEntry) -> None:
        if self._in_progress is None:
            return
        self._in_progress.verification_result = redact_sensitive(str(entry.payload.get("verified")))

    def _handle_speech_finished(self, entry: TransitionLogEntry) -> None:
        self._finalize()

    def _handle_wake(self, entry: TransitionLogEntry) -> None:
        """Conversation mode: waking is its own entry ("woke up: wake word")."""
        payload = entry.payload or {}
        self._append_event(f"woke up ({payload.get('source', 'unknown')})", said=payload.get("greeting"))

    def _handle_go_to_sleep(self, entry: TransitionLogEntry) -> None:
        self._finalize()  # the "go to sleep" command itself, if any
        self._append_event("went to sleep")

    def _handle_stop(self, entry: TransitionLogEntry) -> None:
        if self._in_progress is None and self._debug_log is not None:
            # A Stop with no command running left no trace, yet it
            # clears every pending list -- debug.log now says it happened.
            record = DecisionRecord.start(heard=None)
            record.understood = {"event": "stop while idle", "source": (entry.payload or {}).get("source", "unknown")}
            record.result = entry.reason
            record.completed = True
            self._debug_log.write(record)
            return
        if self._in_progress is not None:
            # Answers were "stopped before finishing" and nothing
            # said what stopped them. Every Stop now names its source.
            source = (entry.payload or {}).get("source", "unknown")
            self._in_progress.progress.append(f"stopped by: {source} (while {entry.from_state.name})")
        cut_off_answer = (entry.from_state == State.SPEAKING and self._in_progress is not None
                          and "answer" in (self._in_progress.understood or {}))
        if self._in_progress is not None and (self._in_progress.result is None or cut_off_answer):
            # An answer stopped mid-sentence was recorded
            # as "answered". An action that already ran keeps its own result.
            self._in_progress.result = "stopped before finishing"
        self._finalize()

    def _finalize(self) -> None:
        if self._in_progress is None:
            return
        self._in_progress.completed = True
        self._records.append(self._in_progress)
        if self._debug_log is not None:
            self._debug_log.write(self._in_progress)
        self._in_progress = None

    _HANDLERS = {
        EventType.COMMAND_CAPTURED: _handle_command_captured,
        EventType.STARTUP_TASK_REQUESTED: _handle_startup_task,
        EventType.ACTION_REQUESTED: _handle_action_requested,
        EventType.CONFIRM_RECEIVED: _handle_confirm_received,
        EventType.ANSWER_READY: _handle_answer_ready,
        EventType.UNDERSTAND_FAILED: _handle_understand_failed,
        EventType.ACTION_DONE: _handle_action_done,
        EventType.VERIFICATION_COMPLETE: _handle_verification_complete,
        EventType.SPEECH_FINISHED: _handle_speech_finished,
        EventType.WAKE: _handle_wake,
        EventType.GO_TO_SLEEP: _handle_go_to_sleep,
    }

    def _append_event(self, text: str, said: str | None = None) -> None:
        """A state change worth showing (woke up, went to sleep), with what
        she said for it (the wake greeting). Caller holds the lock."""
        record = DecisionRecord.start(heard=None)
        record.understood = {"event": text, "said": redact_sensitive(said)} if said else {"event": text}
        record.choice = text
        record.rule_or_trust_level = TrustLevel.L0
        record.origin = OriginTag.DEVELOPER_REQUESTED
        record.result = text
        record.completed = True
        self._records.append(record)
        if self._debug_log is not None:
            self._debug_log.write(record)

    def record_status(self, text: str) -> None:
        """A startup step finishing in the background ("voice ready:
        listening on …"): its own row in Live Activity and the
        debug log, like "woke up". Fixed wording only, never anything heard."""
        with self._lock:
            self._append_event(redact_sensitive(text) or "")

    def record_heard_while_asleep(self, text: str) -> None:
        """F15 (conversation mode): what she heard while SLEEPING, shown in
        the activity log for this session only. Never written to the debug
        log or anywhere else on disk, never acted on, never sent online."""
        self._append_session_only({"asleep_heard": redact_sensitive(text) or ""})

    def record_ignored(self, text: str, reason: str) -> None:
        """An utterance too short or unclear to be a command ("ignored: too
        short/unclear"). Session only, like the asleep transcript: it was
        never meant for JARVIS, so it isn't kept."""
        self._append_session_only({"ignored": redact_sensitive(text) or "", "reason": reason})

    def _append_session_only(self, understood: dict) -> None:
        record = DecisionRecord.start(heard=None)
        record.understood = understood
        record.completed = True
        with self._lock:
            self._records.append(record)  # deliberately NOT written to the debug log

    def record_ui_action(self, text: str) -> None:
        """A developer action taken from the dashboard itself, not a spoken or
        typed command (e.g. "copied activity log"): L0, logged as its own
        completed record so it shows in the activity panel and debug log."""
        text = redact_sensitive(text) or ""
        record = DecisionRecord.start(heard=None)
        record.understood = {"ui_action": text}
        record.choice = text
        record.rule_or_trust_level = TrustLevel.L0
        record.origin = OriginTag.DEVELOPER_REQUESTED
        record.result = text
        record.completed = True
        with self._lock:
            self._records.append(record)
        if self._debug_log is not None:
            self._debug_log.write(record)

    # -- reading back --------------------------------------------------

    def add_step(self, text: str, replaces: str | None = None, closing: bool = False, session_only: bool = False) -> None:
        """A module reports something that is really happening right now
        ("searching files..."). Shown live on the dashboard. With `replaces`,
        a previous step starting with that prefix is overwritten in place
        instead of appended to, so a running count ("found 1 match" ->
        "found 2 matches") stays one line. `closing` puts the line after
        what she said (a credit line, a check-in). `session_only` keeps it
        out of the debug log. Ignored if no command is in progress."""
        with self._lock:
            record = self._in_progress
            if record is None:
                return
            step = (redact_answer(text) if text.startswith("saying: ") else redact_sensitive(text)) or ""
            if session_only:
                record.session_only.append(step)
            if closing:
                record.closing.append(step)
            elif replaces and record.progress and record.progress[-1].startswith(replaces):
                record.progress[-1] = step
            else:
                record.progress.append(step)

    def set_online_destination(self, destination: str) -> None:
        """F2/R3: where the command in progress was sent online (provider,
        model). Record and debug log only, never Live Activity."""
        with self._lock:
            if self._in_progress is not None:
                self._in_progress.online_destination = redact_sensitive(destination)

    def live_snapshot(self) -> DecisionRecord | None:
        """A copy of the command currently in progress (None if idle), safe
        to read from the dashboard thread while the controller keeps
        writing to the original."""
        with self._lock:
            if self._in_progress is None:
                return None
            return dataclasses.replace(
                self._in_progress,
                progress=list(self._in_progress.progress),
                closing=list(self._in_progress.closing),
                session_only=list(self._in_progress.session_only),
                candidates=list(self._in_progress.candidates),
            )

    def add_candidates(self, candidates: list[Candidate]) -> None:
        """F3/F4 call this (before submitting ACTION_REQUESTED) to attach
        what was considered and how well each matched."""
        with self._lock:
            if self._in_progress is not None:
                self._in_progress.candidates = [
                    Candidate(redact_sensitive(c.label) or "", c.score) for c in candidates
                ]

    def records(self) -> list[DecisionRecord]:
        """Every record kept this session, oldest first."""
        with self._lock:
            return list(self._records)

    def last_completed(self) -> DecisionRecord | None:
        """The newest finished record, or None."""
        with self._lock:
            for record in reversed(self._records):
                if record.completed:
                    return record
            return None

    def find(self, record_id: str) -> DecisionRecord | None:
        """The record with this id, or None."""
        with self._lock:
            for record in self._records:
                if record.id == record_id:
                    return record
            return None

    def answer_why(self, record_id: str | None = None) -> str:
        """Built only from a stored record's own fields (R13) -- no model
        call, no text generated after the fact."""
        record = self.find(record_id) if record_id else self.last_completed()
        if record is None:
            return NOTHING_YET
        return _describe(record)


# Spoken "why" answers (R13): plain sentences built only from the record's
# own fields. No dicts, brackets, quotes or raw field names reach speech.
_REDACTED = str(SETTINGS["logbook"]["redacted_placeholder"])
_UNSPEAKABLE = str.maketrans({"{": None, "}": None, "[": None, "]": None, '"': None, "`": None, "'": "’"})
_STOPPED = "stopped before finishing"
_DENIED_PREFIX = "denied: "
# How each fixed action reads in "I opened X" / "I tried to open X".
_ACTION_VERBS = {"open_app": ("opened", "open"), "open_file": ("opened", "open"), "open_url": ("opened", "open"),
                 "remember": ("remembered", "remember"), "forget_memory": ("forgot", "forget"),
                 "forget_all_memory": ("forgot", "forget"),
                 "browser_open_read": ("searched the web in my browser for", "search the web in my browser for")}
_VERIFIED_TEXT = {"open_app": "I checked the window appeared", "open_url": "I checked the window appeared"}


def _plain(value: object) -> str:
    """A record value made safe to speak: a redacted field is described,
    not read out, and quote/bracket characters are dropped (an apostrophe
    becomes a typographic one, which reads the same)."""
    text = str(value).strip()
    if text == _REDACTED:
        return "something private"
    return " ".join(text.translate(_UNSPEAKABLE).split())


def _heard_phrase(record: DecisionRecord) -> str:
    return _plain(record.heard).rstrip(".?! ")


def _level_sentence(record: DecisionRecord) -> str:
    level = record.rule_or_trust_level
    if not isinstance(level, TrustLevel):
        return ""
    number = level.name[1:]
    if level in (TrustLevel.L0, TrustLevel.L1):
        return f"It was a level {number} action"
    confirmations = "two confirmations" if level == TrustLevel.L4 else "your confirmation"
    return f"It was a level {number} action, so it needed {confirmations}"


def _candidates_sentence(record: DecisionRecord) -> str:
    if len(record.candidates) < 2:
        return ""
    best, runner_up = record.candidates[0], record.candidates[1]
    return (
        f"{_plain(best.label)} was the closest match at {round(best.score * 100)} percent, "
        f"ahead of {_plain(runner_up.label)} at {round(runner_up.score * 100)} percent."
    )


def _origin_reason(record: DecisionRecord, request: str) -> str:
    if record.origin == OriginTag.JARVIS_INITIATED:
        return "I started it myself"
    return request


def _describe_action(record: DecisionRecord, understood: dict) -> str:
    action_name = str(understood.get("action_name") or "")
    params = understood.get("params") or {}
    target = _plain(params.get("display_name") or params.get("url") or action_name.replace("_", " "))
    past, base = _ACTION_VERBS.get(action_name, (None, None))
    reason = _origin_reason(record, f"you asked me to {_heard_phrase(record)}")
    level = _level_sentence(record)
    result = str(record.result or "")

    if result.startswith(_DENIED_PREFIX):
        doing = f"{base} {target}" if base else f"run {target}"
        why_not = _plain(result[len(_DENIED_PREFIX):]).rstrip(". ")
        first = f"I did not {doing}, because it was refused: {why_not}."
        return " ".join(p for p in (first, f"{level}." if level else "") if p)
    if result == _STOPPED:
        return f"You asked me to {_heard_phrase(record)}, but it was stopped before I finished."

    if record.verification_result == "False":
        doing = f"tried to {base} {target}" if base else f"tried to run {target}"
        first = f"I {doing} because {reason}."
        second = f"{level}, but I could not confirm that it worked." if level else "I could not confirm that it worked."
    else:
        doing = f"{past} {target}" if past else f"ran {target}"
        first = f"I {doing} because {reason}."
        checked = _VERIFIED_TEXT.get(action_name, "I checked that it worked")
        if record.verification_result == "True":
            second = f"{level} and {checked}." if level else f"{checked}."
        else:
            second = f"{level}." if level else ""
    return " ".join(p for p in (first, _candidates_sentence(record), second) if p)


def _describe(record: DecisionRecord) -> str:
    understood = record.understood if isinstance(record.understood, dict) else {}
    level = _level_sentence(record)

    if "ui_action" in understood:
        first = f"You did that from the dashboard: {_plain(understood['ui_action'])}."
        return " ".join(p for p in (first, f"{level}." if level else "") if p)
    if not record.heard:
        return "That was not tied to a spoken or typed command."
    if understood.get("action_name"):
        return _describe_action(record, understood)
    if "answer" in understood:
        # R13: whether it went online is part of the record. The provider
        # is not spoken (R3, amended).
        verb = ("searched online and answered" if record.online_destination
                else "looked it up and listed what I found" if len(record.candidates) > 1 else "answered")
        first = f"I {verb} because {_origin_reason(record, f'you asked: {_heard_phrase(record)}')}."
        return " ".join(p for p in (first, f"{level}, so it ran straight away." if level else "") if p)
    if record.result == _STOPPED:
        return f"You said {_heard_phrase(record)}, but it was stopped before I finished."
    if record.result:
        return f"You said {_heard_phrase(record)}. I could not work out what you meant, so I did nothing."
    return f"You said {_heard_phrase(record)}, and nothing else happened."
