"""Tests for F5: decision records and the live log.

Required by Section 8 / the developer's step-2 instructions: every F5
field is populated, "why did you do that" answers only from record
fields, and sensitive markers never end up stored.
"""

from __future__ import annotations

import time

import pytest

from jarvis.controller import Controller, Event, EventType, OriginTag, State, TrustLevel
from jarvis.logbook import Candidate, Logbook
from jarvis.settings import SETTINGS


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture
def wired() -> tuple[Controller, Logbook]:
    """A started Controller with a Logbook listening to it."""
    c = Controller(start_state=State.AWAKE)
    log = Logbook()
    c.set_transition_listener(log.on_transition)
    c.start()
    yield c, log
    c.shutdown()


def _run_l1_action_flow(c: Controller, log: Logbook) -> None:
    c.submit(Event(EventType.SPEECH_STARTED))
    assert _wait_until(lambda: c.state == State.LISTENING)
    c.submit(Event(EventType.COMMAND_CAPTURED, {"text": "open chrome"}))
    assert _wait_until(lambda: c.state == State.UNDERSTANDING)

    log.add_candidates([Candidate("Google Chrome", 0.97), Candidate("Chromium", 0.41)])

    c.submit(Event(EventType.ACTION_REQUESTED, {"action_name": "open_app", "params": {"name": "chrome"}}))
    assert _wait_until(lambda: c.state == State.ACTING)
    permit = c.current_permit()
    ok, _ = c.redeem_permit(permit.id, "open_app", {"name": "chrome"})
    assert ok

    c.submit(Event(EventType.ACTION_DONE, {"result": "opened Google Chrome"}))
    assert _wait_until(lambda: c.state == State.VERIFYING)
    c.submit(Event(EventType.VERIFICATION_COMPLETE, {"verified": True}))
    assert _wait_until(lambda: c.state == State.SPEAKING)
    c.submit(Event(EventType.SPEECH_FINISHED))
    assert _wait_until(lambda: c.state == State.AWAKE)


def test_decision_record_has_every_f5_field_populated(wired) -> None:
    """Decision record has every F5 field populated."""
    c, log = wired
    _run_l1_action_flow(c, log)

    records = log.records()
    assert len(records) == 1
    r = records[0]

    assert r.completed is True
    assert r.heard == "open chrome"
    assert r.understood == {"action_name": "open_app", "params": {"name": "chrome"}}
    assert [c.label for c in r.candidates] == ["Google Chrome", "Chromium"]
    assert r.candidates[0].score == 0.97
    assert r.choice == "open_app"
    assert r.rule_or_trust_level == TrustLevel.L1
    assert r.origin == OriginTag.DEVELOPER_REQUESTED
    assert r.result == "opened Google Chrome"
    assert r.verification_result == "True"
    assert isinstance(r.timestamp, float)
    assert r.id


def test_answer_question_record(wired) -> None:
    """Answer question record."""
    c, log = wired
    c.submit(Event(EventType.SPEECH_STARTED))
    assert _wait_until(lambda: c.state == State.LISTENING)
    c.submit(Event(EventType.COMMAND_CAPTURED, {"text": "how are you"}))
    assert _wait_until(lambda: c.state == State.UNDERSTANDING)
    c.submit(Event(EventType.ANSWER_READY, {"text": "I'm doing well, thanks."}))
    assert _wait_until(lambda: c.state == State.SPEAKING)
    c.submit(Event(EventType.SPEECH_FINISHED))
    assert _wait_until(lambda: c.state == State.AWAKE)

    r = log.last_completed()
    assert r.heard == "how are you"
    assert r.understood == {"answer": "I'm doing well, thanks."}
    assert r.choice == "answer the question"
    assert r.rule_or_trust_level == TrustLevel.L0
    assert r.origin == OriginTag.DEVELOPER_REQUESTED
    assert r.result == "answered"


def test_denied_action_still_produces_a_record(wired) -> None:
    """Denied action still produces a record."""
    c, log = wired
    c.submit(Event(EventType.SPEECH_STARTED))
    assert _wait_until(lambda: c.state == State.LISTENING)
    c.submit(Event(EventType.COMMAND_CAPTURED, {"text": "delete everything"}))
    assert _wait_until(lambda: c.state == State.UNDERSTANDING)
    c.submit(Event(EventType.ACTION_REQUESTED, {"action_name": "delete_everything", "params": {}}))
    assert _wait_until(lambda: c.state == State.SPEAKING)
    c.submit(Event(EventType.SPEECH_FINISHED))
    assert _wait_until(lambda: c.state == State.AWAKE)

    r = log.last_completed()
    assert r.heard == "delete everything"
    assert r.choice == "delete_everything"
    assert "denied" in r.result
    assert "not a fixed action" in r.result


def test_rejected_confirmation_shows_up_in_the_record(wired) -> None:
    """F7: a mismatched confirm needs to explain itself in the record,
    same as a denied action already does."""
    c, log = wired
    c.submit(Event(EventType.SPEECH_STARTED))
    assert _wait_until(lambda: c.state == State.LISTENING)
    c.submit(Event(EventType.COMMAND_CAPTURED, {"text": "lock the laptop"}))
    assert _wait_until(lambda: c.state == State.UNDERSTANDING)
    c.submit(Event(EventType.ACTION_REQUESTED, {"action_name": "lock_screen", "params": {"text": "hello"}}))
    assert _wait_until(lambda: c.state == State.CONFIRMING)

    c.submit(Event(EventType.CONFIRM_RECEIVED, {"action_name": "lock_screen", "params": {"text": "wrong"}}))
    assert _wait_until(lambda: c.state == State.SPEAKING)
    c.submit(Event(EventType.SPEECH_FINISHED))
    assert _wait_until(lambda: c.state == State.AWAKE)

    r = log.last_completed()
    assert r.choice == "lock_screen"
    assert r.rule_or_trust_level == TrustLevel.L2
    assert "denied" in r.result
    assert "did not match" in r.result


def test_expired_confirmation_shows_a_distinct_reason_in_the_record(monkeypatch) -> None:
    """A different sub-case of the same rejection path (F7's mismatch/
    expired/nothing-pending all borrow UNDERSTAND_FAILED's target state,
    but must stay distinguishable in the stored record -- confirmed with
    the developer before starting step 4)."""
    from jarvis.settings import SETTINGS

    monkeypatch.setitem(SETTINGS["controller"], "confirmation_timeout_seconds", 0.05)
    c = Controller(start_state=State.AWAKE)
    log = Logbook()
    c.set_transition_listener(log.on_transition)
    c.start()
    try:
        c.submit(Event(EventType.SPEECH_STARTED))
        assert _wait_until(lambda: c.state == State.LISTENING)
        c.submit(Event(EventType.COMMAND_CAPTURED, {"text": "lock the laptop"}))
        assert _wait_until(lambda: c.state == State.UNDERSTANDING)
        c.submit(Event(EventType.ACTION_REQUESTED, {"action_name": "lock_screen", "params": {"text": "hello"}}))
        assert _wait_until(lambda: c.state == State.CONFIRMING)

        import time as _time

        _time.sleep(0.15)
        c.submit(Event(EventType.CONFIRM_RECEIVED, {"action_name": "lock_screen", "params": {"text": "hello"}}))
        assert _wait_until(lambda: c.state == State.SPEAKING)
        c.submit(Event(EventType.SPEECH_FINISHED))
        assert _wait_until(lambda: c.state == State.AWAKE)

        r = log.last_completed()
        assert "expired" in r.result
        assert "did not match" not in r.result
    finally:
        c.shutdown()


def test_genuine_understand_failure_is_distinct_from_a_rejected_confirm(wired) -> None:
    """The two rejection paths share UNDERSTAND_FAILED's target STATE
    (SPEAKING) but must never be confused with each other in the record:
    a real "couldn't understand the command" is a different event_type
    (UNDERSTAND_FAILED) from a rejected confirm (CONFIRM_RECEIVED,
    accepted=False), and produces different stored text."""
    c, log = wired
    c.submit(Event(EventType.SPEECH_STARTED))
    assert _wait_until(lambda: c.state == State.LISTENING)
    c.submit(Event(EventType.COMMAND_CAPTURED, {"text": "asdkjfh"}))
    assert _wait_until(lambda: c.state == State.UNDERSTANDING)
    c.submit(Event(EventType.UNDERSTAND_FAILED, {}))
    assert _wait_until(lambda: c.state == State.SPEAKING)
    c.submit(Event(EventType.SPEECH_FINISHED))
    assert _wait_until(lambda: c.state == State.AWAKE)

    r = log.last_completed()
    assert r.result == "could not understand"
    assert "denied" not in r.result


def test_stop_finalizes_the_in_progress_record(wired) -> None:
    """Stop finalizes the in progress record."""
    c, log = wired
    c.submit(Event(EventType.SPEECH_STARTED))
    assert _wait_until(lambda: c.state == State.LISTENING)
    c.submit(Event(EventType.COMMAND_CAPTURED, {"text": "open chrome"}))
    assert _wait_until(lambda: c.state == State.UNDERSTANDING)
    c.submit(Event(EventType.ACTION_REQUESTED, {"action_name": "open_app", "params": {"name": "chrome"}}))
    assert _wait_until(lambda: c.state == State.ACTING)

    c.request_stop(source="test")
    assert _wait_until(lambda: c.state == State.AWAKE)

    r = log.last_completed()
    assert r is not None
    assert r.heard == "open chrome"
    assert r.result == "stopped before finishing"
    assert r.completed is True


# -- R11/F15: sensitive markers never end up stored ---------------------


def test_sensitive_heard_text_is_redacted(wired) -> None:
    """Sensitive heard text is redacted."""
    c, log = wired
    c.submit(Event(EventType.SPEECH_STARTED))
    assert _wait_until(lambda: c.state == State.LISTENING)
    c.submit(Event(EventType.COMMAND_CAPTURED, {"text": "my password is hunter2"}))
    assert _wait_until(lambda: c.state == State.UNDERSTANDING)
    c.submit(Event(EventType.UNDERSTAND_FAILED, {}))
    assert _wait_until(lambda: c.state == State.SPEAKING)
    c.submit(Event(EventType.SPEECH_FINISHED))
    assert _wait_until(lambda: c.state == State.AWAKE)

    r = log.last_completed()
    assert "hunter2" not in r.heard
    assert "password" not in r.heard
    assert r.heard == "[redacted]"


def test_sensitive_answer_text_is_redacted(wired) -> None:
    """Sensitive answer text is redacted."""
    c, log = wired
    c.submit(Event(EventType.SPEECH_STARTED))
    assert _wait_until(lambda: c.state == State.LISTENING)
    c.submit(Event(EventType.COMMAND_CAPTURED, {"text": "what is my api key"}))
    assert _wait_until(lambda: c.state == State.UNDERSTANDING)
    c.submit(Event(EventType.ANSWER_READY, {"text": "your api key is sk-12345"}))
    assert _wait_until(lambda: c.state == State.SPEAKING)
    c.submit(Event(EventType.SPEECH_FINISHED))
    assert _wait_until(lambda: c.state == State.AWAKE)

    r = log.last_completed()
    assert "sk-12345" not in str(r.understood)
    assert r.understood == {"answer": "[redacted]"}


def test_sensitive_text_nested_in_action_params_is_redacted(wired) -> None:
    """redact_value() recurses into `params`, not just the top-level
    understood dict -- e.g. an L2 action's params. lock_screen is L2 (F7), so
    this exercises the preview stage, not just an immediate action."""
    c, log = wired
    c.submit(Event(EventType.SPEECH_STARTED))
    assert _wait_until(lambda: c.state == State.LISTENING)
    c.submit(Event(EventType.COMMAND_CAPTURED, {"text": "type my password is hunter2"}))
    assert _wait_until(lambda: c.state == State.UNDERSTANDING)
    c.submit(
        Event(
            EventType.ACTION_REQUESTED,
            {"action_name": "lock_screen", "params": {"text": "my password is hunter2"}},
        )
    )
    assert _wait_until(lambda: c.state == State.CONFIRMING)

    # No executor is registered in this rig -- Stop
    # ends the interaction and finalizes the record as it stands. The
    # record's `understood` was set as soon as the preview was issued,
    # before any confirmation, so redaction must already have happened.
    c.request_stop(source="test")
    assert _wait_until(lambda: c.state == State.AWAKE)

    r = log.last_completed()
    assert r.understood == {"action_name": "lock_screen", "params": {"text": "[redacted]"}}
    assert "hunter2" not in str(r.understood)


def test_sensitive_candidate_label_is_redacted(wired) -> None:
    """Sensitive candidate label is redacted."""
    c, log = wired
    c.submit(Event(EventType.SPEECH_STARTED))
    assert _wait_until(lambda: c.state == State.LISTENING)
    c.submit(Event(EventType.COMMAND_CAPTURED, {"text": "find my notes"}))
    assert _wait_until(lambda: c.state == State.UNDERSTANDING)
    log.add_candidates([Candidate("my password vault.txt", 0.8)])
    c.submit(Event(EventType.UNDERSTAND_FAILED, {}))
    assert _wait_until(lambda: c.state == State.SPEAKING)
    c.submit(Event(EventType.SPEECH_FINISHED))
    assert _wait_until(lambda: c.state == State.AWAKE)

    r = log.last_completed()
    assert r.candidates[0].label == "[redacted]"


# -- "why did you do that" answers only from record fields --------------


def test_answer_why_contains_only_values_from_the_record(wired) -> None:
    """Answer why contains only values from the record."""
    c, log = wired
    _run_l1_action_flow(c, log)
    r = log.last_completed()

    answer = log.answer_why()

    # Every fact in the spoken answer traces back to the record (R13):
    # what was heard, the candidates and scores, the trust level, and the
    # verification -- said as plain sentences, not raw field values.
    assert r.heard in answer
    assert "you asked me to" in answer  # origin DEVELOPER_REQUESTED
    assert f"{r.candidates[0].label} was the closest match at 97 percent" in answer
    assert f"ahead of {r.candidates[1].label} at 41 percent" in answer
    assert "level 1 action" in answer  # rule_or_trust_level L1
    assert "I checked" in answer  # verification_result True


def test_answer_why_by_id_picks_that_record_not_the_latest(wired) -> None:
    """Answer why by ID picks that record not the latest."""
    c, log = wired
    _run_l1_action_flow(c, log)
    first = log.last_completed()

    c.submit(Event(EventType.SPEECH_STARTED))
    assert _wait_until(lambda: c.state == State.LISTENING)
    c.submit(Event(EventType.COMMAND_CAPTURED, {"text": "how are you"}))
    assert _wait_until(lambda: c.state == State.UNDERSTANDING)
    c.submit(Event(EventType.ANSWER_READY, {"text": "fine"}))
    assert _wait_until(lambda: c.state == State.SPEAKING)
    c.submit(Event(EventType.SPEECH_FINISHED))
    assert _wait_until(lambda: c.state == State.AWAKE)

    answer_for_first = log.answer_why(first.id)
    assert first.heard in answer_for_first
    assert "how are you" not in answer_for_first


def test_answer_why_with_no_records_says_so() -> None:
    """Answer why with no records says so."""
    log = Logbook()
    assert log.answer_why() == "I haven’t done anything yet."


def test_max_records_caps_memory_use(monkeypatch) -> None:
    """Max records caps memory use."""
    # A small cap so the test doesn't have to run hundreds of command
    # cycles to prove the rolling-drop behaviour (R15: bounded memory).
    monkeypatch.setitem(SETTINGS["logbook"], "max_records", 3)
    c = Controller(start_state=State.AWAKE)
    log = Logbook()
    c.set_transition_listener(log.on_transition)
    c.start()
    try:
        for i in range(5):
            c.submit(Event(EventType.SPEECH_STARTED))
            assert _wait_until(lambda: c.state == State.LISTENING)
            c.submit(Event(EventType.COMMAND_CAPTURED, {"text": f"command {i}"}))
            assert _wait_until(lambda: c.state == State.UNDERSTANDING)
            c.submit(Event(EventType.UNDERSTAND_FAILED, {}))
            assert _wait_until(lambda: c.state == State.SPEAKING)
            c.submit(Event(EventType.SPEECH_FINISHED))
            assert _wait_until(lambda: c.state == State.AWAKE)

        assert len(log.records()) == 3
        # oldest dropped, newest kept
        assert log.records()[-1].heard == "command 4"
        assert log.records()[0].heard == "command 2"
    finally:
        c.shutdown()


# -- "why" answers are plain spoken sentences: no dicts, brackets, quotes
# or raw field names (developer instruction). ---------------

from jarvis.logbook.records import DecisionRecord  # noqa: E402

_UNSPOKEN_CHARS = set("{}[]'")


def _record(heard, understood, result=None, verified=None, level=TrustLevel.L1, candidates=(), origin=OriginTag.DEVELOPER_REQUESTED):
    r = DecisionRecord.start(heard=heard)
    r.understood = understood
    r.choice = "x"
    r.rule_or_trust_level = level
    r.origin = origin
    r.result = result
    r.verification_result = verified
    r.candidates = list(candidates)
    r.completed = True
    return r


_SHAPES = {
    "opened, verified": _record(
        "open chrome",
        {"action_name": "open_app", "params": {"target": r"C:\x\Chrome.lnk", "display_name": "Google Chrome"}},
        "Opened Google Chrome.", "True", candidates=[Candidate("Google Chrome", 0.97), Candidate("Chromium", 0.41)],
    ),
    "not verified": _record(
        "open notepad", {"action_name": "open_app", "params": {"display_name": "Notepad"}}, "I couldn't confirm it opened.", "False"
    ),
    "url": _record("open youtube", {"action_name": "open_url", "params": {"url": "https://www.youtube.com"}}, "Opened.", "True"),
    "file": _record("find resume", {"action_name": "open_file", "params": {"display_name": "resume.pdf"}}, "Opened.", "True"),
    "denied": _record("lock the laptop", {"action_name": "lock_screen", "params": {"text": "hello"}}, "denied: confirmation [expired]", level=TrustLevel.L2),
    "stopped": _record("open youtube", None, "stopped before finishing"),
    "answer": _record("what's the time?", {"answer": "It's {noon} ['really']"}, "answered", level=TrustLevel.L0),
    "not understood": _record("blorp", None, "could not understand"),
    "ui action": _record(None, {"ui_action": "copied activity log"}, "copied activity log", level=TrustLevel.L0),
    "redacted heard": _record("[redacted]", {"answer": "[redacted]"}, "answered", level=TrustLevel.L0),
    "jarvis started": _record(
        "watch for gestures", {"action_name": "capture_camera", "params": {}}, "done", "True",
        level=TrustLevel.L3, origin=OriginTag.JARVIS_INITIATED,
    ),
    "L4": _record("delete it", {"action_name": "delete_file", "params": {"path": "C:\a'b"}}, "done", level=TrustLevel.L4),
}


@pytest.mark.parametrize("shape", list(_SHAPES))
def test_why_answer_has_no_dict_bracket_or_quote_characters(shape) -> None:
    """Why answer has no dict bracket or quote characters."""
    log = Logbook()
    log._records.append(_SHAPES[shape])
    answer = log.answer_why()
    assert answer
    assert not (_UNSPOKEN_CHARS & set(answer)), answer
    assert '"' not in answer
    for raw in ("action_name", "params", "ui_action", "understood", "DEVELOPER_REQUESTED", "JARVIS_INITIATED", "True", "False", "L1", "None"):
        assert raw not in answer, (raw, answer)


def test_why_answer_matches_the_developer_example() -> None:
    """Why answer matches the developer example."""
    log = Logbook()
    record = _SHAPES["opened, verified"]
    record.candidates = []
    log._records.append(record)
    assert log.answer_why() == (
        "I opened Google Chrome because you asked me to open chrome. "
        "It was a level 1 action and I checked the window appeared."
    )


def test_why_answer_wordings_per_outcome() -> None:
    """Why answer wordings per outcome."""
    def why(shape: str) -> str:
        log = Logbook()
        log._records.append(_SHAPES[shape])
        return log.answer_why()

    assert why("not verified") == (
        "I tried to open Notepad because you asked me to open notepad. "
        "It was a level 1 action, but I could not confirm that it worked."
    )
    assert why("denied").startswith("I did not run lock screen, because it was refused: confirmation expired.")
    assert "needed your confirmation" in why("denied")
    assert why("stopped") == "You said open youtube, but it was stopped before I finished."
    assert why("answer") == "I answered because you asked: what\u2019s the time. It was a level 0 action, so it ran straight away."
    assert why("not understood") == "You said blorp. I could not work out what you meant, so I did nothing."
    assert why("ui action") == "You did that from the dashboard: copied activity log. It was a level 0 action."
    assert "something private" in why("redacted heard")
    assert "I started it myself" in why("jarvis started")
    assert "needed two confirmations" in why("L4")


def test_nothing_yet_message_has_no_apostrophe() -> None:
    """Nothing yet message has no apostrophe."""
    assert not (_UNSPOKEN_CHARS & set(Logbook().answer_why()))
