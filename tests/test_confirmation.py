"""F7 full trust levels: the L2/L3/L4 confirmation engine.

Per the developer's review decisions for this step:
- L2 is tested through the real pipeline using a real fixed action
  (lock_screen) that's genuinely mapped to L2 in trust.py.
- L3 has no real fixed action mapped to it (F8/F9/F16 don't exist -- see
  trust.py's docstring). Tested as "pure mechanism": a PendingConfirmation
  is constructed and attached to the controller directly, never going
  through authorize()/FIXED_ACTION_SET/TRUST_TABLE at all.
- L4 also has no real fixed action mapped to it (move_file was removed --
  it was an unreviewed guess, not "delete"). Tested through the real
  pipeline using a transient, obviously-fake action name monkeypatched
  into FIXED_ACTION_SET/TRUST_TABLE for a single test only, never
  committed to trust.py.

Nothing here is a real F8-F16 trigger. Everything is mocked/fake per
Section 8, as agreed before this step started.
"""

from __future__ import annotations

import time

import pytest

import jarvis.controller.policy as policy_module
from jarvis.controller import Controller, Event, EventType, OriginTag, PendingConfirmation, State, TrustLevel
from jarvis.settings import SETTINGS


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture
def controller():
    """Fixture: a started Controller in AWAKE, shut down after the test."""
    c = Controller(start_state=State.AWAKE)
    c.start()
    yield c
    c.shutdown()


def _drive_to_understanding(c: Controller, text: str) -> None:
    c.submit(Event(EventType.SPEECH_STARTED))
    assert _wait_until(lambda: c.state == State.LISTENING)
    c.submit(Event(EventType.COMMAND_CAPTURED, {"text": text}))
    assert _wait_until(lambda: c.state == State.UNDERSTANDING)


# -- L2: real action (lock_screen), full pipeline --------------------------


def test_l2_preview_then_single_confirm_completes_the_action(controller: Controller) -> None:
    """L2 preview then single confirm completes the action."""
    c = controller
    _drive_to_understanding(c, "lock the laptop")
    c.submit(Event(EventType.ACTION_REQUESTED, {"action_name": "lock_screen", "params": {"text": "hello"}}))
    assert _wait_until(lambda: c.state == State.CONFIRMING)

    pending = c.pending_confirmation()
    assert pending is not None
    assert pending.action_name == "lock_screen"
    assert pending.trust_level == TrustLevel.L2
    assert pending.confirmations_required == 1
    assert pending.confirmations_received == 0
    assert c.current_permit() is None  # no permit until fully confirmed

    c.submit(Event(EventType.CONFIRM_RECEIVED, {"action_name": "lock_screen", "params": {"text": "hello"}}))
    assert _wait_until(lambda: c.state == State.ACTING)

    assert c.pending_confirmation() is None
    permit = c.current_permit()
    assert permit is not None
    assert permit.action_name == "lock_screen"
    assert permit.trust_level == TrustLevel.L2

    ok, reason = c.redeem_permit(permit.id, "lock_screen", {"text": "hello"})
    assert ok, reason
    c.submit(Event(EventType.ACTION_DONE, {"result": "typed"}))
    assert _wait_until(lambda: c.state == State.VERIFYING)
    c.submit(Event(EventType.VERIFICATION_COMPLETE, {"verified": True}))
    assert _wait_until(lambda: c.state == State.SPEAKING)
    c.submit(Event(EventType.SPEECH_FINISHED))
    assert _wait_until(lambda: c.state == State.AWAKE)


def test_l2_confirm_with_mismatched_params_is_rejected(controller: Controller) -> None:
    """L2 confirm with mismatched params is rejected."""
    c = controller
    _drive_to_understanding(c, "lock the laptop")
    c.submit(Event(EventType.ACTION_REQUESTED, {"action_name": "lock_screen", "params": {"text": "hello"}}))
    assert _wait_until(lambda: c.state == State.CONFIRMING)

    # Confirming the wrong text -- Section 5: tied to the exact params.
    c.submit(Event(EventType.CONFIRM_RECEIVED, {"action_name": "lock_screen", "params": {"text": "goodbye"}}))
    assert _wait_until(lambda: c.state == State.SPEAKING)
    last = c.transition_log[-1]
    assert last.accepted is False
    assert "did not match" in last.reason
    assert c.current_permit() is None

    # Leaving CONFIRMING (even via a rejection) means a later confirm has
    # nothing to attach to -- deny by default, no table entry from SPEAKING.
    c.submit(Event(EventType.CONFIRM_RECEIVED, {"action_name": "lock_screen", "params": {"text": "hello"}}))
    time.sleep(0.1)
    assert c.state == State.SPEAKING
    assert c.transition_log[-1].accepted is False


def test_l2_confirmation_expires(monkeypatch) -> None:
    """L2 confirmation expires."""
    monkeypatch.setitem(SETTINGS["controller"], "confirmation_timeout_seconds", 0.05)
    c = Controller(start_state=State.AWAKE)
    c.start()
    try:
        _drive_to_understanding(c, "lock the laptop")
        c.submit(Event(EventType.ACTION_REQUESTED, {"action_name": "lock_screen", "params": {"text": "hello"}}))
        assert _wait_until(lambda: c.state == State.CONFIRMING)

        time.sleep(0.15)
        c.submit(Event(EventType.CONFIRM_RECEIVED, {"action_name": "lock_screen", "params": {"text": "hello"}}))
        assert _wait_until(lambda: c.state == State.SPEAKING)
        last = c.transition_log[-1]
        assert last.accepted is False
        assert "expired" in last.reason
        assert c.pending_confirmation() is None  # expired confirmation is cleared, unlike a mismatch
    finally:
        c.shutdown()


def test_confirm_with_nothing_pending_says_so(controller: Controller) -> None:
    """Confirm with nothing pending says so."""
    # Bare "confirm" with no preceding action request at all.
    controller.submit(Event(EventType.CONFIRM_RECEIVED, {"action_name": "lock_screen", "params": {}}))
    time.sleep(0.2)
    assert controller.state == State.AWAKE
    assert controller.transition_log[-1].accepted is False
    assert "deny by default" in controller.transition_log[-1].reason


def test_stop_cancels_pending_confirmation(controller: Controller) -> None:
    """Stop cancels pending confirmation."""
    c = controller
    _drive_to_understanding(c, "lock the laptop")
    c.submit(Event(EventType.ACTION_REQUESTED, {"action_name": "lock_screen", "params": {"text": "hello"}}))
    assert _wait_until(lambda: c.state == State.CONFIRMING)
    assert c.pending_confirmation() is not None

    c.request_stop(source="test")
    assert _wait_until(lambda: c.state == State.AWAKE)
    assert c.pending_confirmation() is None

    # A confirm for the now-cancelled action has nothing to attach to.
    c.submit(Event(EventType.CONFIRM_RECEIVED, {"action_name": "lock_screen", "params": {"text": "hello"}}))
    time.sleep(0.1)
    assert c.state == State.AWAKE
    assert c.transition_log[-1].accepted is False


# -- L3: pure mechanism, no real action, no TRUST_TABLE/FIXED_ACTION_SET involved --


def _controller_confirming_with(pending: PendingConfirmation) -> Controller:
    c = Controller(start_state=State.CONFIRMING)
    c._pending_confirmation = pending  # test-only direct injection, see module docstring
    c.start()
    return c


def test_l3_single_confirm_mechanism() -> None:
    """L3 single confirm mechanism."""
    pending = PendingConfirmation.issue(
        "__l3_mechanism_test__",
        {"target": "example.com"},
        OriginTag.DEVELOPER_REQUESTED,
        TrustLevel.L3,
        timeout_seconds=999,
        confirmations_required=1,
    )
    c = _controller_confirming_with(pending)
    try:
        c.submit(
            Event(
                EventType.CONFIRM_RECEIVED,
                {"action_name": "__l3_mechanism_test__", "params": {"target": "example.com"}},
            )
        )
        assert _wait_until(lambda: c.state == State.ACTING)
        permit = c.current_permit()
        assert permit is not None
        assert permit.trust_level == TrustLevel.L3
        assert c.pending_confirmation() is None
    finally:
        c.shutdown()


def test_l3_expiry_mechanism() -> None:
    """L3 expiry mechanism."""
    pending = PendingConfirmation.issue(
        "__l3_mechanism_test__",
        {},
        OriginTag.DEVELOPER_REQUESTED,
        TrustLevel.L3,
        timeout_seconds=0.05,
        confirmations_required=1,
    )
    c = _controller_confirming_with(pending)
    try:
        time.sleep(0.15)
        c.submit(Event(EventType.CONFIRM_RECEIVED, {"action_name": "__l3_mechanism_test__", "params": {}}))
        assert _wait_until(lambda: c.state == State.SPEAKING)
        assert "expired" in c.transition_log[-1].reason
        assert c.pending_confirmation() is None
    finally:
        c.shutdown()


def test_l3_param_change_voids_the_confirmation() -> None:
    """L3 param change voids the confirmation."""
    pending = PendingConfirmation.issue(
        "__l3_mechanism_test__",
        {"target": "example.com"},
        OriginTag.DEVELOPER_REQUESTED,
        TrustLevel.L3,
        timeout_seconds=999,
        confirmations_required=1,
    )
    c = _controller_confirming_with(pending)
    try:
        c.submit(
            Event(
                EventType.CONFIRM_RECEIVED,
                {"action_name": "__l3_mechanism_test__", "params": {"target": "different.com"}},
            )
        )
        assert _wait_until(lambda: c.state == State.SPEAKING)
        assert "did not match" in c.transition_log[-1].reason
        assert c.pending_confirmation().confirmations_received == 0  # unchanged, not advanced
    finally:
        c.shutdown()


# -- L4: double confirmation, through the real pipeline via a transient placeholder --


@pytest.fixture
def l4_placeholder_action(monkeypatch):
    """Registers '__test_only_l4_action__' as a real L4 fixed action for
    the duration of one test only (monkeypatch reverts both automatically).
    Never touches trust.py permanently -- see the module docstring."""
    fake_action = "__test_only_l4_action__"
    monkeypatch.setattr(policy_module, "FIXED_ACTION_SET", policy_module.FIXED_ACTION_SET | {fake_action})
    monkeypatch.setitem(policy_module.TRUST_TABLE, fake_action, TrustLevel.L4)
    return fake_action


def test_l4_requires_two_confirmations(controller: Controller, l4_placeholder_action: str) -> None:
    """L4 requires two confirmations."""
    c = controller
    action_name = l4_placeholder_action
    _drive_to_understanding(c, "do the risky thing")
    c.submit(Event(EventType.ACTION_REQUESTED, {"action_name": action_name, "params": {"path": "x"}}))
    assert _wait_until(lambda: c.state == State.CONFIRMING)

    pending = c.pending_confirmation()
    assert pending.confirmations_required == 2
    assert pending.confirmations_received == 0

    # First confirm: not enough yet, stays in CONFIRMING.
    c.submit(Event(EventType.CONFIRM_RECEIVED, {"action_name": action_name, "params": {"path": "x"}}))
    assert _wait_until(lambda: c.pending_confirmation() is not None and c.pending_confirmation().confirmations_received == 1)
    assert c.state == State.CONFIRMING
    assert c.current_permit() is None
    assert "1/2" in c.transition_log[-1].reason

    # Second confirm: now fully confirmed, permit issued.
    c.submit(Event(EventType.CONFIRM_RECEIVED, {"action_name": action_name, "params": {"path": "x"}}))
    assert _wait_until(lambda: c.state == State.ACTING)
    assert c.pending_confirmation() is None
    permit = c.current_permit()
    assert permit is not None
    assert permit.trust_level == TrustLevel.L4


def test_l4_third_confirm_after_completion_has_nothing_pending(controller: Controller, l4_placeholder_action: str) -> None:
    """L4 third confirm after completion has nothing pending."""
    c = controller
    action_name = l4_placeholder_action
    _drive_to_understanding(c, "do the risky thing")
    c.submit(Event(EventType.ACTION_REQUESTED, {"action_name": action_name, "params": {}}))
    assert _wait_until(lambda: c.state == State.CONFIRMING)
    c.submit(Event(EventType.CONFIRM_RECEIVED, {"action_name": action_name, "params": {}}))
    assert _wait_until(lambda: c.state == State.CONFIRMING and c.pending_confirmation().confirmations_received == 1)
    c.submit(Event(EventType.CONFIRM_RECEIVED, {"action_name": action_name, "params": {}}))
    assert _wait_until(lambda: c.state == State.ACTING)

    c.submit(Event(EventType.CONFIRM_RECEIVED, {"action_name": action_name, "params": {}}))
    time.sleep(0.1)
    assert c.state == State.ACTING  # unchanged -- no table entry for ACTING + CONFIRM_RECEIVED
    assert c.transition_log[-1].accepted is False
