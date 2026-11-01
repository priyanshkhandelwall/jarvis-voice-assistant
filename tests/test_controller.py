"""Tests for the core state machine (F17).

Per Section 8: trust-level decisions, permit reuse/invalidation, Stop from
every state, and every invalid state change are required tests.
"""

from __future__ import annotations

import itertools
import threading
import time

import pytest

from jarvis.controller import Controller, Event, EventType, OriginTag, PendingConfirmation, State, TrustLevel
from jarvis.controller.transitions import ALLOWED_TRANSITIONS


@pytest.fixture
def controller() -> Controller:
    """Fixture: a started Controller in AWAKE, shut down after the test."""
    c = Controller(start_state=State.AWAKE)
    c.start()
    yield c
    c.shutdown()


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_happy_path_l1_action_open_app(controller: Controller) -> None:
    """Happy path L1 action open app."""
    controller.submit(Event(EventType.SPEECH_STARTED))
    assert _wait_until(lambda: controller.state == State.LISTENING)

    controller.submit(Event(EventType.COMMAND_CAPTURED, {"text": "open chrome"}))
    assert _wait_until(lambda: controller.state == State.UNDERSTANDING)

    controller.submit(
        Event(EventType.ACTION_REQUESTED, {"action_name": "open_app", "params": {"name": "chrome"}})
    )
    assert _wait_until(lambda: controller.state == State.ACTING)

    permit = controller.current_permit()
    assert permit is not None
    assert permit.action_name == "open_app"
    assert permit.origin == OriginTag.DEVELOPER_REQUESTED
    assert permit.trust_level == TrustLevel.L1

    ok, reason = controller.redeem_permit(permit.id, "open_app", {"name": "chrome"})
    assert ok, reason

    controller.submit(Event(EventType.ACTION_DONE, {"result": "opened"}))
    assert _wait_until(lambda: controller.state == State.VERIFYING)

    controller.submit(Event(EventType.VERIFICATION_COMPLETE, {"verified": True}))
    assert _wait_until(lambda: controller.state == State.SPEAKING)

    controller.submit(Event(EventType.SPEECH_FINISHED))
    assert _wait_until(lambda: controller.state == State.AWAKE)

    # Every hop must be in the log, in order.
    kinds = [e.event_type for e in controller.transition_log]
    assert kinds == [
        EventType.SPEECH_STARTED,
        EventType.COMMAND_CAPTURED,
        EventType.ACTION_REQUESTED,
        EventType.ACTION_DONE,
        EventType.VERIFICATION_COMPLETE,
        EventType.SPEECH_FINISHED,
    ]
    assert all(e.accepted for e in controller.transition_log)


def test_happy_path_l0_answer_question(controller: Controller) -> None:
    """Happy path L0 answer question."""
    controller.submit(Event(EventType.SPEECH_STARTED))
    assert _wait_until(lambda: controller.state == State.LISTENING)
    controller.submit(Event(EventType.COMMAND_CAPTURED, {"text": "how are you"}))
    assert _wait_until(lambda: controller.state == State.UNDERSTANDING)

    # L0 answers never touch the permit pipeline -- see trust.py docstring.
    controller.submit(Event(EventType.ANSWER_READY, {"text": "I'm doing well, thanks."}))
    assert _wait_until(lambda: controller.state == State.SPEAKING)
    assert controller.current_permit() is None

    controller.submit(Event(EventType.SPEECH_FINISHED))
    assert _wait_until(lambda: controller.state == State.AWAKE)


def test_understand_failed_returns_to_speaking_then_waiting(controller: Controller) -> None:
    """Understand failed returns to speaking then waiting."""
    controller.submit(Event(EventType.SPEECH_STARTED))
    assert _wait_until(lambda: controller.state == State.LISTENING)
    controller.submit(Event(EventType.COMMAND_CAPTURED, {"text": "asdkjfh"}))
    assert _wait_until(lambda: controller.state == State.UNDERSTANDING)
    controller.submit(Event(EventType.UNDERSTAND_FAILED, {}))
    assert _wait_until(lambda: controller.state == State.SPEAKING)


def test_listen_timeout_returns_to_waiting(controller: Controller) -> None:
    """Listen timeout returns to waiting."""
    controller.submit(Event(EventType.SPEECH_STARTED))
    assert _wait_until(lambda: controller.state == State.LISTENING)
    controller.submit(Event(EventType.LISTEN_TIMEOUT))
    assert _wait_until(lambda: controller.state == State.AWAKE)


def test_answer_ready_entry_carries_l0_trust_and_origin(controller: Controller) -> None:
    """F5/R13: even though Q&A never touches the permit pipeline, the log
    entry must still carry the trust level and origin a decision record
    needs -- fixed by the controller, not guessed after the fact."""
    controller.submit(Event(EventType.SPEECH_STARTED))
    assert _wait_until(lambda: controller.state == State.LISTENING)
    controller.submit(Event(EventType.COMMAND_CAPTURED, {"text": "how are you"}))
    assert _wait_until(lambda: controller.state == State.UNDERSTANDING)
    controller.submit(Event(EventType.ANSWER_READY, {"text": "I'm doing well."}))
    assert _wait_until(lambda: controller.state == State.SPEAKING)

    entry = controller.transition_log[-1]
    assert entry.trust_level == TrustLevel.L0
    assert entry.origin == OriginTag.DEVELOPER_REQUESTED


def test_origin_tag_cannot_be_supplied_by_caller(controller: Controller) -> None:
    """Section 5: only the controller sets the origin tag. A module trying
    to claim JARVIS_INITIATED for itself must be ignored."""
    controller.submit(Event(EventType.SPEECH_STARTED))
    assert _wait_until(lambda: controller.state == State.LISTENING)
    controller.submit(Event(EventType.COMMAND_CAPTURED, {"text": "open chrome"}))
    assert _wait_until(lambda: controller.state == State.UNDERSTANDING)

    controller.submit(
        Event(
            EventType.ACTION_REQUESTED,
            {
                "action_name": "open_app",
                "params": {"name": "chrome"},
                "origin": OriginTag.JARVIS_INITIATED,  # attempted override
            },
        )
    )
    assert _wait_until(lambda: controller.state == State.ACTING)

    permit = controller.current_permit()
    assert permit is not None
    assert permit.origin == OriginTag.DEVELOPER_REQUESTED
    assert controller.transition_log[-1].origin == OriginTag.DEVELOPER_REQUESTED


# -- invalid transitions: deny by default -----------------------------


@pytest.mark.parametrize(
    "start_events,bad_event",
    [
        # "Hey JARVIS" (wake word) does nothing while ASLEEP: no transition defined.
        ([], EventType.SPEECH_STARTED),
    ],
)
def test_invalid_transition_from_asleep(controller: Controller, start_events, bad_event) -> None:
    """Invalid transition from asleep."""
    controller.shutdown()
    controller = Controller(start_state=State.SLEEPING)
    controller.start()
    try:
        controller.submit(Event(bad_event))
        time.sleep(0.2)
        assert controller.state == State.SLEEPING
        assert controller.transition_log[-1].accepted is False
    finally:
        controller.shutdown()


def test_every_state_event_pair_matches_the_fixed_table_exactly() -> None:
    """Table-driven: for all 8x9 (state, event) combinations, a transition
    happens if and only if it is a key in ALLOWED_TRANSITIONS, and it
    always lands on exactly the table's target. This is the authoritative
    check that deny-by-default holds everywhere, not just in the specific
    cases spelled out above."""
    for state, event_type in itertools.product(State, EventType):
        c = Controller(start_state=state)
        if event_type == EventType.ACTION_REQUESTED:
            # An action the policy engine allows, so that when the
            # table *does* permit this pair, the transition isn't
            # separately blocked by policy -- isolating the table
            # check from the policy check.
            payload = {"action_name": "open_app", "params": {"name": "chrome"}}
        elif event_type == EventType.CONFIRM_RECEIVED:
            # CONFIRM_RECEIVED has its own business-logic gate (a
            # matching pending confirmation), same idea as
            # ACTION_REQUESTED's policy gate above -- pre-seed one so
            # the table's promise (CONFIRMING + CONFIRM_RECEIVED ->
            # ACTING) can actually be reached when the table allows it.
            # Set before start() (not after) so this direct-injection
            # write happens-before the worker thread exists at all --
            # no cross-thread write race, per the developer's
            # threading-audit review.
            payload = {"action_name": "__table_test_confirm__", "params": {}}
            if state == State.CONFIRMING:
                c._pending_confirmation = PendingConfirmation.issue(
                    "__table_test_confirm__",
                    {},
                    OriginTag.DEVELOPER_REQUESTED,
                    TrustLevel.L2,
                    timeout_seconds=999,
                    confirmations_required=1,
                )
        else:
            payload = {}
        c.start()
        try:
            c.submit(Event(event_type, payload))
            assert _wait_until(lambda: c.transition_count() == 1)
            entry = c.last_transition()
            expected_target = ALLOWED_TRANSITIONS.get((state, event_type))
            if expected_target is None:
                assert entry.accepted is False, f"{state} + {event_type} should be denied by default"
                assert c.state == state
            else:
                assert entry.accepted is True, f"{state} + {event_type} should be allowed by the table"
                assert c.state == expected_target
        finally:
            c.shutdown()


def test_acting_while_waiting_is_rejected(controller: Controller) -> None:
    """Acting while waiting is rejected."""
    # No COMMAND_CAPTURED/UNDERSTANDING happened -- acting from WAITING is invalid.
    controller.submit(
        Event(EventType.ACTION_REQUESTED, {"action_name": "open_app", "params": {"name": "chrome"}})
    )
    time.sleep(0.2)
    assert controller.state == State.AWAKE
    assert controller.transition_log[-1].accepted is False
    assert "deny by default" in controller.transition_log[-1].reason


def test_action_not_in_fixed_set_is_denied_and_logged(controller: Controller) -> None:
    """Action not in fixed set is denied and logged."""
    controller.submit(Event(EventType.SPEECH_STARTED))
    assert _wait_until(lambda: controller.state == State.LISTENING)
    controller.submit(Event(EventType.COMMAND_CAPTURED, {"text": "delete everything"}))
    assert _wait_until(lambda: controller.state == State.UNDERSTANDING)

    controller.submit(Event(EventType.ACTION_REQUESTED, {"action_name": "delete_everything", "params": {}}))
    # Denied action is rerouted to SPEAKING (same target as UNDERSTAND_FAILED), never ACTING.
    assert _wait_until(lambda: controller.state == State.SPEAKING)
    assert controller.current_permit() is None
    last = controller.transition_log[-1]
    assert last.accepted is False
    assert "not a fixed action" in last.reason


# L2 action routing (preview + confirm, not immediate) is covered in
# tests/test_confirmation.py, along with the rest of F7's L2-L4 engine.


# -- permits: reuse, mismatch, expiry, staleness -----------------------


def _drive_to_acting(controller: Controller, action_name="open_app", params=None):
    params = params or {"name": "chrome"}
    controller.submit(Event(EventType.SPEECH_STARTED))
    assert _wait_until(lambda: controller.state == State.LISTENING)
    controller.submit(Event(EventType.COMMAND_CAPTURED, {"text": "open chrome"}))
    assert _wait_until(lambda: controller.state == State.UNDERSTANDING)
    controller.submit(Event(EventType.ACTION_REQUESTED, {"action_name": action_name, "params": params}))
    assert _wait_until(lambda: controller.state == State.ACTING)
    return controller.current_permit()


def test_permit_cannot_be_reused(controller: Controller) -> None:
    """Permit cannot be reused."""
    permit = _drive_to_acting(controller)
    ok1, _ = controller.redeem_permit(permit.id, "open_app", {"name": "chrome"})
    assert ok1
    ok2, reason2 = controller.redeem_permit(permit.id, "open_app", {"name": "chrome"})
    assert not ok2
    assert "already used" in reason2


def test_permit_rejects_parameter_mismatch(controller: Controller) -> None:
    """Permit rejects parameter mismatch."""
    permit = _drive_to_acting(controller, params={"name": "chrome"})
    ok, reason = controller.redeem_permit(permit.id, "open_app", {"name": "firefox"})
    assert not ok
    assert "do not match" in reason


def test_permit_expires(controller: Controller) -> None:
    """Permit expires."""
    permit = _drive_to_acting(controller)
    # Force expiry without sleeping the test for the full timeout.
    object.__setattr__(permit, "issued_at", permit.issued_at - permit.timeout_seconds - 1)
    ok, reason = controller.redeem_permit(permit.id, "open_app", {"name": "chrome"})
    assert not ok
    assert "expired" in reason


def test_stale_permit_invalidated_when_state_moves_on(controller: Controller) -> None:
    """Stale permit invalidated when state moves on."""
    permit = _drive_to_acting(controller)
    # State moves on via ACTION_DONE without ever redeeming the permit.
    controller.submit(Event(EventType.ACTION_DONE, {}))
    assert _wait_until(lambda: controller.state == State.VERIFYING)
    ok, reason = controller.redeem_permit(permit.id, "open_app", {"name": "chrome"})
    assert not ok
    assert "no matching permit" in reason


def test_redeem_with_nothing_pending_does_nothing(controller: Controller) -> None:
    """Redeem with nothing pending does nothing."""
    ok, reason = controller.redeem_permit("nonexistent-id", "open_app", {"name": "chrome"})
    assert not ok
    assert "no matching permit" in reason


# -- Stop: works from every state, is highest priority -----------------


@pytest.mark.parametrize(
    "state",
    [
        State.AWAKE,
        State.LISTENING,
        State.UNDERSTANDING,
        State.CONFIRMING,
        State.ACTING,
        State.VERIFYING,
        State.SPEAKING,
    ],
)
def test_stop_returns_to_waiting_from_every_state(state: State) -> None:
    """Stop returns to waiting from every state."""
    c = Controller(start_state=state)
    c.start()
    try:
        c.request_stop(source="test")
        assert _wait_until(lambda: c.state == State.AWAKE)
        assert c.current_permit() is None
        assert c.pending_confirmation() is None
    finally:
        c.shutdown()


def test_stop_discards_queued_events(controller: Controller) -> None:
    """Stop discards queued events."""
    controller.submit(Event(EventType.SPEECH_STARTED))
    assert _wait_until(lambda: controller.state == State.LISTENING)
    # Queue several events, then stop before the loop drains them.
    for _ in range(5):
        controller.submit(Event(EventType.COMMAND_CAPTURED, {"text": "spam"}))
    controller.request_stop(source="test")
    assert _wait_until(lambda: controller.state == State.AWAKE)
    # None of the spammed COMMAND_CAPTURED events should have been processed
    # after the stop -- state stays WAITING, it doesn't bounce to UNDERSTANDING.
    time.sleep(0.2)
    assert controller.state == State.AWAKE


def test_stop_cancels_pending_permit(controller: Controller) -> None:
    """Stop cancels pending permit."""
    permit = _drive_to_acting(controller)
    controller.request_stop(source="test")
    assert _wait_until(lambda: controller.state == State.AWAKE)
    ok, reason = controller.redeem_permit(permit.id, "open_app", {"name": "chrome"})
    assert not ok


def test_is_stop_requested_is_the_polling_hook_for_long_running_work(controller: Controller) -> None:
    """R8: this is the flag a future long-running worker (LLM generation,
    waiting on an app) must poll between chunks of work. Prove a
    simulated chunked worker aborts promptly instead of running to
    completion once Stop is requested mid-way."""
    assert controller.is_stop_requested() is False

    aborted_after = None

    def simulated_long_running_worker(total_chunks: int = 50, chunk_seconds: float = 0.02) -> int:
        for i in range(total_chunks):
            if controller.is_stop_requested():
                return i
            time.sleep(chunk_seconds)
        return total_chunks

    def stop_soon() -> None:
        time.sleep(0.05)
        controller.request_stop(source="test")

    t = threading.Thread(target=stop_soon)
    t.start()
    aborted_after = simulated_long_running_worker()
    t.join()

    # Stopped well before all 50 chunks ran (would take ~1s uninterrupted).
    assert aborted_after < 50
    assert _wait_until(lambda: controller.state == State.AWAKE)


def test_is_stop_requested_stays_true_until_the_next_wake_word(controller: Controller) -> None:
    """The flag must not be cleared by the controller's own bookkeeping a
    few milliseconds after Stop -- a slow-to-poll worker must still see
    it. It only resets once a new interaction genuinely begins.

    The reset condition is "any accepted event that leaves WAITING", not
    "specifically SPEECH_STARTED" -- SPEECH_STARTED is simply the
    event this test drives. The typed-command entry point gets the same
    reset with no controller change, because this generalized condition
    already covers it."""
    controller.request_stop(source="test")
    assert _wait_until(lambda: controller.state == State.AWAKE)
    # Give the worker plenty of time to finish its own internal handling.
    time.sleep(0.3)
    assert controller.is_stop_requested() is True

    controller.submit(Event(EventType.SPEECH_STARTED))
    assert _wait_until(lambda: controller.state == State.LISTENING)
    assert controller.is_stop_requested() is False


def test_stop_races_with_concurrent_submits_from_several_threads() -> None:
    """Stress test for R8/4.2: many threads hammer submit() while another
    thread calls request_stop() mid-storm. The worker thread must remain
    the only thing that ever mutates state, and after everything settles
    the controller must be safely at WAITING with no leftover permit and
    an internally consistent log (no corrupted from/to chaining)."""
    c = Controller(start_state=State.AWAKE)
    c.start()
    try:
        stop_fired = threading.Event()

        def submitter() -> None:
            while not stop_fired.is_set():
                c.submit(Event(EventType.SPEECH_STARTED))
                c.submit(Event(EventType.LISTEN_TIMEOUT))

        def stopper() -> None:
            time.sleep(0.02)
            c.request_stop(source="race-test")
            stop_fired.set()

        threads = [threading.Thread(target=submitter) for _ in range(4)]
        threads.append(threading.Thread(target=stopper))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3.0)

        # A submitter may have queued one more event after stop_fired was
        # set but before it noticed -- settle deterministically with one
        # more explicit stop rather than asserting on a racy tail state.
        c.request_stop(source="final-settle")
        # Wait for the Stop itself to be processed, not just for AWAKE: the
        # state is usually AWAKE already, so waiting on it alone let the log
        # be read before the worker thread had handled the Stop (a flake
        # seen 1 in ~6 runs under load).
        assert _wait_until(lambda: any(e.event_type is None for e in list(c.transition_log)))
        assert _wait_until(lambda: c.state == State.AWAKE)
        assert c.current_permit() is None

        stop_entries = [e for e in c.transition_log if e.event_type is None]
        assert len(stop_entries) >= 1, "at least one Stop must have been processed"
        assert all(e.to_state == State.AWAKE for e in stop_entries)

        log = c.transition_log
        for prev, cur in zip(log, log[1:]):
            if cur.accepted:
                assert cur.from_state == prev.to_state
    finally:
        c.shutdown()


# -- transition listener (F5's logbook) must never take the controller down -----


def test_listener_exception_does_not_stop_the_controller(controller: Controller) -> None:
    """Listener exception does not stop the controller."""
    calls = []

    def flaky_listener(entry) -> None:
        calls.append(entry)
        raise RuntimeError("logbook bug")

    controller.set_transition_listener(flaky_listener)

    controller.submit(Event(EventType.SPEECH_STARTED))
    assert _wait_until(lambda: controller.state == State.LISTENING)
    controller.submit(Event(EventType.LISTEN_TIMEOUT))
    assert _wait_until(lambda: controller.state == State.AWAKE)

    # The listener was called (and blew up) on both transitions, but the
    # controller kept processing events and its own history is intact.
    assert len(calls) == 2
    assert len(controller.transition_log) == 2
    assert controller.transition_log[-1].accepted is True


def test_slow_listener_logs_a_warning_but_does_not_block_forever(controller: Controller, monkeypatch, caplog) -> None:
    """Slow listener logs a warning but does not block forever."""
    monkeypatch.setattr(controller, "_transition_listener_warn_seconds", 0.001)

    def slow_listener(entry) -> None:
        time.sleep(0.05)

    controller.set_transition_listener(slow_listener)

    with caplog.at_level("WARNING", logger="jarvis.controller.controller"):
        controller.submit(Event(EventType.SPEECH_STARTED))
        assert _wait_until(lambda: controller.state == State.LISTENING)
        # State flips to LISTENING before the listener runs (it's
        # notified from within _record, after the state assignment), so
        # give the still-sleeping slow_listener time to finish and log.
        time.sleep(0.1)

    assert any("took" in r.getMessage().lower() for r in caplog.records)


# -- single queue / single thread: concurrent submits stay ordered -----


def test_concurrent_submits_are_processed_sequentially(controller: Controller) -> None:
    """Concurrent submits are processed sequentially."""
    controller.submit(Event(EventType.SPEECH_STARTED))
    assert _wait_until(lambda: controller.state == State.LISTENING)

    results = []

    def submit_many():
        for _ in range(20):
            controller.submit(Event(EventType.LISTEN_TIMEOUT))
            controller.submit(Event(EventType.SPEECH_STARTED))

    threads = [threading.Thread(target=submit_many) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    time.sleep(0.5)
    # No corruption: every logged transition's from_state must equal the
    # to_state of the entry before it (a single, consistent history).
    log = controller.transition_log
    for prev, cur in zip(log, log[1:]):
        if cur.accepted:
            assert cur.from_state == prev.to_state


# -- threading audit follow-ups (developer's review after step 7) -----------


def test_transition_count_and_last_transition_are_lock_protected_accessors(controller: Controller) -> None:
    """These exist so external callers (orchestrator.py) never touch
    controller.transition_log directly from another thread."""
    assert controller.transition_count() == 0
    assert controller.last_transition() is None

    controller.submit(Event(EventType.SPEECH_STARTED))
    assert _wait_until(lambda: controller.transition_count() == 1)
    last = controller.last_transition()
    assert last is not None
    assert last.event_type == EventType.SPEECH_STARTED
    assert last is controller.transition_log[-1]


def test_start_is_safe_when_called_concurrently_from_multiple_threads() -> None:
    """TOCTOU race fixed in start(): must never spawn two worker threads
    for one Controller."""
    c = Controller(start_state=State.AWAKE)
    try:
        threads = [threading.Thread(target=c.start) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2.0)

        # Exactly one worker thread exists and the controller still
        # behaves correctly (no duplicate/racing event processing).
        c.submit(Event(EventType.SPEECH_STARTED))
        assert _wait_until(lambda: c.state == State.LISTENING)
        assert c.transition_count() == 1  # processed exactly once, not twice
    finally:
        c.shutdown()


def test_set_transition_listener_is_lock_protected(controller: Controller) -> None:
    """Set transition listener is lock protected."""
    calls = []
    controller.set_transition_listener(lambda entry: calls.append(entry))
    controller.submit(Event(EventType.SPEECH_STARTED))
    assert _wait_until(lambda: len(calls) == 1)
    assert calls[0].event_type == EventType.SPEECH_STARTED
