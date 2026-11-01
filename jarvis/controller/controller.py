"""The single controller (R6, F17): one state, one event queue, one thread.

Every module sends Events here. Only this class changes JARVIS's state,
issues permits and pending confirmations (F7), and sets the origin tag.
Stop (R8) is a priority interrupt handled outside the normal queue, so it
works even while an event is being processed or is waiting in line.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from jarvis.controller.events import Event, EventType
from jarvis.controller.permits import OriginTag, PendingConfirmation, Permit, PermitState
from jarvis.controller.policy import authorize
from jarvis.controller.trust import STANDING_APPROVAL_ACTIONS
from jarvis.controller.states import State
from jarvis.controller.transitions import ALLOWED_TRANSITIONS
from jarvis.controller.trust import CONFIRMATION_REQUIRED_LEVELS, CONFIRMATIONS_NEEDED, EVENT_TRUST_LEVELS, TrustLevel
from jarvis.settings import SETTINGS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TransitionLogEntry:
    """One row of controller history. F5 reads this to build decision records."""

    timestamp: float
    from_state: State
    to_state: State
    event_type: EventType | None  # None for a Stop-forced transition
    accepted: bool
    reason: str
    payload: dict[str, Any] = field(default_factory=dict)
    # Set only on entries where a trust decision was actually made (an
    # authorized action or an answered question) -- see F5/R13. None on
    # plain state hops (e.g. SPEECH_STARTED) that carry no trust
    # decision of their own.
    trust_level: TrustLevel | None = None
    origin: OriginTag | None = None


class Controller:
    """F17: the single owner of JARVIS's state (R6). Events go in through submit(); a worker thread applies
    the fixed transition table, issues permits and confirmations, and tells listeners about every
    transition."""
    def __init__(self, start_state: State = State.SLEEPING) -> None:
        cfg = SETTINGS["controller"]
        self._permit_timeout = float(cfg["permit_timeout_seconds"])
        self._confirmation_timeout = float(cfg["confirmation_timeout_seconds"])
        self._stop_poll_interval = float(cfg["stop_poll_interval_seconds"])
        self._queue_get_timeout = float(cfg["queue_get_timeout_seconds"])
        self._transition_listener_warn_seconds = float(cfg["transition_listener_warn_seconds"])

        self._state = start_state
        self._queue: queue.Queue[Event] = queue.Queue()
        # Two separate flags on purpose. _stop_requested is an internal,
        # transient trigger: the run loop clears it right after draining
        # the queue, so the loop doesn't busy-spin re-handling the same
        # stop forever. _stopped is the sticky, externally-visible signal
        # is_stop_requested() reads -- it must stay True until a new
        # interaction genuinely begins (leaving AWAKE or SLEEPING), not just until the controller's own
        # bookkeeping finishes a few milliseconds later. Collapsing these
        # into one flag lets a long-running worker poll a window so short
        # it can miss the stop entirely; see test_is_stop_requested_is_
        # the_polling_hook_for_long_running_work.
        self._stop_requested = threading.Event()
        self._stopped = threading.Event()
        # Who asked for the last Stop (button, Esc, hotkey, spoken
        # or typed "stop"), put on the Stop entry so every stop says why.
        self._stop_source = "unknown"
        self._state_lock = threading.RLock()
        self._current_permit: PermitState | None = None
        self._pending_confirmation: PendingConfirmation | None = None
        # Section 5 origin for the interaction under way: set here, and only
        # here, from the event that started it (STARTUP_TASK_REQUESTED ->
        # JARVIS_INITIATED; anything else -> DEVELOPER_REQUESTED).
        self._interaction_origin = OriginTag.DEVELOPER_REQUESTED
        # Section 5's standing approval (F21): (action_name, params) -> does
        # the exact outbound list match the approved hash? None: no standing
        # approval exists, every L3 action asks.
        self._standing_approval: Callable[[str, dict], bool] | None = None
        self.transition_log: list[TransitionLogEntry] = []
        # A list, not a single slot: set_transition_listener (F5/F6's
        # existing combined bridge+logbook callback in app.py) and
        # add_transition_listener (for a self-contained pluggable module
        # like F4's FileFinder, which needs to know about Stop without
        # requiring app.py to be touched to wire it in) must be able to
        # coexist without one silently overwriting the other.
        self._transition_listeners: list[Callable[[TransitionLogEntry], None]] = []

        self._thread: threading.Thread | None = None
        self._running = threading.Event()

    # -- public API -------------------------------------------------

    @property
    def state(self) -> State:
        """The current State (thread-safe read)."""
        with self._state_lock:
            return self._state

    def set_transition_listener(self, callback: Callable[[TransitionLogEntry], None] | None) -> None:
        """F5/F6's single combined callback (app.py wires bridge+logbook
        together into one function and sets it here) -- replaces
        whatever listeners were there before. For a pluggable module
        that needs to observe transitions without requiring a change to
        whatever already called this (F4's FileFinder, watching for
        Stop), use add_transition_listener instead, which coexists."""
        with self._state_lock:
            self._transition_listeners = [] if callback is None else [callback]

    def add_transition_listener(self, callback: Callable[[TransitionLogEntry], None]) -> None:
        """Adds an additional listener without disturbing any already
        registered via set_transition_listener or a prior add call."""
        with self._state_lock:
            self._transition_listeners.append(callback)

    def remove_transition_listener(self, callback: Callable[[TransitionLogEntry], None]) -> None:
        """Stop calling `callback` for transitions (no error if it was never added)."""
        with self._state_lock:
            if callback in self._transition_listeners:
                self._transition_listeners.remove(callback)

    def start(self) -> None:
        """Start the event worker thread. Calling it twice does nothing."""
        with self._state_lock:
            if self._thread is not None:
                return
            self._running.set()
            self._thread = threading.Thread(target=self._run_loop, name="jarvis-controller", daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        """Stop the event worker thread and wait for it to finish (clean exit)."""
        with self._state_lock:
            self._running.clear()
            thread = self._thread
            self._thread = None
        # Joined outside the lock: the worker thread may itself need the
        # lock (e.g. mid-_handle_event) to finish its current iteration
        # and notice self._running is clear -- joining while holding the
        # lock would deadlock against that.
        if thread is not None:
            thread.join(timeout=2.0)

    def submit(self, event: Event) -> None:
        """Any thread may call this. Events are handled one at a time, in order."""
        self._queue.put(event)

    def set_standing_approval(self, check: Callable[[str, dict], bool] | None) -> None:
        """Section 5 / F21: the one standing approval. `check(action_name,
        params)` answers whether a JARVIS-started request's exact outbound
        list matches the approved hash; it is consulted only for
        trust.STANDING_APPROVAL_ACTIONS and only when the controller itself
        tagged the request JARVIS_INITIATED."""
        with self._state_lock:
            self._standing_approval = check

    def is_stop_requested(self) -> bool:
        """The polling hook long-running work must use (R8).

        Anything that takes more than an instant -- a model generating an
        answer, waiting for an app window to appear, a future Chrome
        automation step -- must run on its own worker thread and check
        this between chunks of work (between tokens, between poll
        iterations), aborting as soon as it returns True. It must never
        run as one uninterruptible blocking call on the controller
        thread, or Stop cannot take effect promptly. Long-running work
        (an online answer, a chunked summary, a live web search, the
        briefing) checks it this way.
        """
        return self._stopped.is_set()

    def request_stop(self, source: str = "unknown") -> None:
        """R8: top priority, works from every state, jumps the queue.

        Callable from any thread (voice thread, hotkey handler, dashboard).
        This method itself never touches self._state -- it only sets
        thread-safe flags. The actual state change happens in
        _handle_stop(), which only ever runs on the single controller
        worker thread (_run_loop), so state is still mutated by exactly
        one thread, per 4.2.
        """
        logger.info("Stop requested by %s", source)
        self._stop_source = source
        self._stop_requested.set()
        self._stopped.set()

    def current_permit(self) -> Permit | None:
        """Read-only view of the pending permit, if any (e.g. for tests/executors)."""
        with self._state_lock:
            return None if self._current_permit is None else self._current_permit.permit

    def pending_confirmation(self) -> PendingConfirmation | None:
        """Read-only view of the pending L2/L3/L4 confirmation, if any --
        F6's preview panel reads this to show what's waiting on "confirm"."""
        with self._state_lock:
            return self._pending_confirmation

    def transition_count(self) -> int:
        """Lock-protected length of transition_log. Callers polling for
        a new entry from another thread (e.g. orchestrator.py) must use
        this, not len(controller.transition_log) directly -- reading the
        list without the lock relies on CPython's GIL making list.append/
        len individually atomic, which is true today but is an implicit
        interpreter guarantee, not an explicit one this codebase should
        depend on."""
        with self._state_lock:
            return len(self.transition_log)

    def last_transition(self) -> TransitionLogEntry | None:
        """Lock-protected read of the newest transition_log entry."""
        with self._state_lock:
            return self.transition_log[-1] if self.transition_log else None

    def redeem_permit(self, permit_id: str, action_name: str, params: dict[str, Any]) -> tuple[bool, str]:
        """Executors call this before running an action. Never reusable."""
        with self._state_lock:
            ps = self._current_permit
            if ps is None or ps.permit.id != permit_id:
                return False, "no matching permit pending"
            if ps.used:
                return False, "permit already used"
            if self._state != State.ACTING:
                return False, "controller is not in ACTING state"
            if not ps.permit.matches(action_name, params):
                return False, "action or parameters do not match the issued permit"
            if ps.permit.is_expired():
                return False, "permit expired"
            ps.used = True
            return True, "ok"

    # -- internal loop ------------------------------------------------

    def _run_loop(self) -> None:
        while self._running.is_set():
            if self._stop_requested.is_set():
                self._handle_stop()
                continue
            try:
                event = self._queue.get(timeout=self._queue_get_timeout)
            except queue.Empty:
                continue
            if self._stop_requested.is_set():
                # Stop fired while we were mid-get; drop the dequeued event too.
                self._handle_stop()
                continue
            self._handle_event(event)

    def _handle_stop(self) -> None:
        entry = self._handle_stop_locked()
        self._notify_listener(entry)
        # Only the internal trigger clears here -- self._stopped (the
        # externally-visible flag) stays set until the next interaction
        # genuinely begins; see the comment in __init__.
        self._stop_requested.clear()

    def _handle_stop_locked(self) -> TransitionLogEntry:
        # State + log append happen atomically inside the lock; the
        # listener is notified afterwards, from _handle_stop, once the
        # lock is released. Splitting these two steps matters: without
        # it, a reader could observe the new state via the `state`
        # property (which only needs the lock briefly) before the
        # matching log entry has actually been appended, since appending
        # used to happen after the lock was released.
        with self._state_lock:
            discarded = 0
            while True:
                try:
                    self._queue.get_nowait()
                    discarded += 1
                except queue.Empty:
                    break
            from_state = self._state
            self._current_permit = None
            self._pending_confirmation = None
            # F15: Stop never wakes her. SLEEPING is already safe and inert,
            # and only WAKE may leave it; from anywhere else the safe state
            # is AWAKE.
            safe_state = State.SLEEPING if from_state == State.SLEEPING else State.AWAKE
            self._state = safe_state
            entry = TransitionLogEntry(
                timestamp=time.monotonic(),
                from_state=from_state,
                to_state=safe_state,
                event_type=None,
                accepted=True,
                reason=f"Stop (R8): {discarded} pending event(s) discarded",
                payload={"source": self._stop_source},
            )
            self.transition_log.append(entry)
            return entry

    def _handle_event(self, event: Event) -> None:
        entry = self._handle_event_locked(event)
        if entry is not None:
            self._notify_listener(entry)

    def _handle_event_locked(self, event: Event) -> TransitionLogEntry | None:
        """Same split as _handle_stop_locked: appends to transition_log
        atomically with whatever state change it makes, still inside the
        lock; the caller notifies the listener afterwards, unlocked."""
        with self._state_lock:
            from_state = self._state
            key = (from_state, event.type)
            target = ALLOWED_TRANSITIONS.get(key)

            if target is None:
                entry = TransitionLogEntry(
                    timestamp=time.monotonic(),
                    from_state=from_state,
                    to_state=from_state,
                    event_type=event.type,
                    accepted=False,
                    reason="deny by default: no such transition in the fixed table",
                    payload=event.payload,
                )
                self.transition_log.append(entry)
                return entry

            if event.type == EventType.ACTION_REQUESTED:
                action_name = event.payload.get("action_name", "")
                params = event.payload.get("params", {})
                # Origin (Section 5): only the controller may set this tag,
                # and it derives it from the event that started this
                # interaction (F20/F21): STARTUP_TASK_REQUESTED
                # -> JARVIS_INITIATED, a spoken/typed command ->
                # DEVELOPER_REQUESTED. Anything a caller puts in
                # event.payload["origin"] is ignored on purpose: no module
                # can claim an origin for itself.
                origin = self._interaction_origin
                decision = authorize(action_name)
                if not decision.allowed:
                    fail_target = ALLOWED_TRANSITIONS[(from_state, EventType.UNDERSTAND_FAILED)]
                    self._state = fail_target
                    entry = TransitionLogEntry(
                        timestamp=time.monotonic(),
                        from_state=from_state,
                        to_state=fail_target,
                        event_type=event.type,
                        accepted=False,
                        reason=f"policy denied: {decision.reason}",
                        payload=event.payload,
                        trust_level=decision.trust_level,
                        origin=origin,
                    )
                    self.transition_log.append(entry)
                    return entry

                standing = (
                    action_name in STANDING_APPROVAL_ACTIONS
                    and origin == OriginTag.JARVIS_INITIATED
                    and self._standing_approval is not None
                    and self._standing_approval(action_name, dict(params))
                )
                if decision.trust_level in CONFIRMATION_REQUIRED_LEVELS and not standing:
                    # F7 L2/L3/L4: preview issued, no permit yet. Reroute
                    # to CONFIRMING via its table entry, the same
                    # borrowed-table-entry technique used for the denial
                    # branch above.
                    confirm_target = ALLOWED_TRANSITIONS[(from_state, EventType.CONFIRMATION_REQUIRED)]
                    required = CONFIRMATIONS_NEEDED[decision.trust_level]
                    pending = PendingConfirmation.issue(
                        action_name,
                        params,
                        origin,
                        decision.trust_level,  # type: ignore[arg-type]
                        self._confirmation_timeout,
                        required,
                    )
                    self._pending_confirmation = pending
                    self._state = confirm_target
                    entry = TransitionLogEntry(
                        timestamp=time.monotonic(),
                        from_state=from_state,
                        to_state=confirm_target,
                        event_type=event.type,
                        accepted=True,
                        reason=(
                            f"preview issued at {decision.trust_level.name}: "  # type: ignore[union-attr]
                            f"{required} confirmation(s) needed"
                        ),
                        payload=event.payload,
                        trust_level=decision.trust_level,
                        origin=origin,
                    )
                    self.transition_log.append(entry)
                    return entry

                permit = Permit.issue(
                    action_name,
                    params,
                    origin,
                    decision.trust_level,  # type: ignore[arg-type]
                    from_state,
                    self._permit_timeout,
                )
                self._current_permit = PermitState(permit=permit)
                self._state = target
                entry = TransitionLogEntry(
                    timestamp=time.monotonic(),
                    from_state=from_state,
                    to_state=target,
                    event_type=event.type,
                    accepted=True,
                    reason=(f"permit {permit.id} issued at {decision.trust_level.name}"  # type: ignore[union-attr]
                            + (" under the standing approval (outbound list hash matches)" if standing else "")),
                    payload=event.payload,
                    trust_level=decision.trust_level,
                    origin=origin,
                )
                self.transition_log.append(entry)
                return entry

            if event.type == EventType.CONFIRM_RECEIVED:
                # Only reachable when from_state == CONFIRMING (the table
                # denies it by default from anywhere else) -- see
                # transitions.py for how that answers "confirm with
                # nothing pending" outside CONFIRMING.
                confirm_action_name = event.payload.get("action_name", "")
                confirm_params = event.payload.get("params", {})
                pending = self._pending_confirmation
                if pending is None:
                    mismatch_reason = "nothing pending to confirm"
                elif pending.is_expired():
                    mismatch_reason = "confirmation expired"
                elif not pending.matches(confirm_action_name, confirm_params):
                    mismatch_reason = "confirm did not match the pending action"
                else:
                    mismatch_reason = None

                if mismatch_reason is not None:
                    # An expired (or otherwise stale) confirmation can't
                    # be reused -- clear it so a later, unrelated confirm
                    # doesn't accidentally match leftover state.
                    if pending is not None and pending.is_expired():
                        self._pending_confirmation = None
                    fail_target = ALLOWED_TRANSITIONS[(from_state, EventType.UNDERSTAND_FAILED)]
                    self._state = fail_target
                    entry = TransitionLogEntry(
                        timestamp=time.monotonic(),
                        from_state=from_state,
                        to_state=fail_target,
                        event_type=event.type,
                        accepted=False,
                        reason=mismatch_reason,
                        payload=event.payload,
                    )
                    self.transition_log.append(entry)
                    return entry

                pending.confirmations_received += 1
                if not pending.is_fully_confirmed():
                    # L4's first of two confirmations: stay in CONFIRMING
                    # and ask again. State does not move.
                    entry = TransitionLogEntry(
                        timestamp=time.monotonic(),
                        from_state=from_state,
                        to_state=from_state,
                        event_type=event.type,
                        accepted=True,
                        reason=(
                            f"confirmation {pending.confirmations_received}/"
                            f"{pending.confirmations_required} received, confirm again"
                        ),
                        payload=event.payload,
                        trust_level=pending.trust_level,
                        origin=pending.origin,
                    )
                    self.transition_log.append(entry)
                    return entry

                # Fully confirmed: issue the real one-time permit now.
                permit = Permit.issue(
                    pending.action_name,
                    dict(pending.params),
                    pending.origin,
                    pending.trust_level,
                    from_state,
                    self._permit_timeout,
                )
                self._current_permit = PermitState(permit=permit)
                self._pending_confirmation = None
                self._state = target
                entry = TransitionLogEntry(
                    timestamp=time.monotonic(),
                    from_state=from_state,
                    to_state=target,
                    event_type=event.type,
                    accepted=True,
                    reason=(
                        f"fully confirmed ({pending.confirmations_received}/"
                        f"{pending.confirmations_required}); permit {permit.id} issued"
                    ),
                    payload=event.payload,
                    trust_level=pending.trust_level,
                    origin=pending.origin,
                )
                self.transition_log.append(entry)
                return entry

            if event.type == EventType.ANSWER_READY:
                # Q&A (F2) never enters the permit pipeline (trust.py
                # docstring) but F5's decision record still needs a trust
                # level and origin, fixed here exactly like the permit
                # path above -- never guessed, never from a model.
                self._state = target
                entry = TransitionLogEntry(
                    timestamp=time.monotonic(),
                    from_state=from_state,
                    to_state=target,
                    event_type=event.type,
                    accepted=True,
                    reason="L0 answer, immediate, no permit needed",
                    payload=event.payload,
                    trust_level=EVENT_TRUST_LEVELS[EventType.ANSWER_READY],
                    origin=self._interaction_origin,
                )
                self.transition_log.append(entry)
                return entry

            if from_state == State.ACTING and target != State.ACTING:
                self._current_permit = None

            if event.type == EventType.CANCEL_RECEIVED:
                # "Cancel" (typed, or spoken while awake): the pending action
                # and any permit for it are gone; a later "confirm" finds
                # nothing to confirm.
                self._pending_confirmation = None
                self._current_permit = None

            if from_state in (State.AWAKE, State.SLEEPING) and target != from_state:
                # A new interaction is genuinely starting -- a command from
                # AWAKE, or waking from SLEEPING (the only way out of it).
                # Keyed on "left a resting state" rather than one event type,
                # so a future entry point can't forget it. Any earlier Stop
                # is no longer relevant to whatever runs next.
                self._stopped.clear()
                # Section 5 origin, from the event type alone (F17).
                self._interaction_origin = (
                    OriginTag.JARVIS_INITIATED if event.type == EventType.STARTUP_TASK_REQUESTED else OriginTag.DEVELOPER_REQUESTED
                )

            self._state = target
            entry = TransitionLogEntry(
                timestamp=time.monotonic(),
                from_state=from_state,
                to_state=target,
                event_type=event.type,
                accepted=True,
                reason="allowed by fixed transition table",
                payload=event.payload,
                origin=self._interaction_origin if event.type == EventType.STARTUP_TASK_REQUESTED else None,
            )
            self.transition_log.append(entry)
            return entry

    def _notify_listener(self, entry: TransitionLogEntry) -> None:
        """Called only after transition_log.append(entry) has already
        happened inside the lock (see _handle_event_locked/_handle_stop_
        locked) -- so by the time any reader observes the new state,
        the matching log entry is already there too, and this call
        itself never runs while holding the lock (R14/R15: a slow or
        buggy listener must not block current_permit()/pending_
        confirmation()/the state property for every other caller)."""
        logger.info(
            "%s -> %s via %s (%s): %s",
            entry.from_state.name,
            entry.to_state.name,
            entry.event_type.name if entry.event_type else "STOP",
            "accepted" if entry.accepted else "rejected",
            entry.reason,
        )
        with self._state_lock:
            listeners = list(self._transition_listeners)
        for listener in listeners:
            self._notify_one_listener(listener, entry)

    def _notify_one_listener(self, listener: Callable[[TransitionLogEntry], None], entry: TransitionLogEntry) -> None:
        # R14: a bug in a listener (F5's logbook today, F4's FileFinder
        # Stop-awareness now too) must never take the controller down
        # with it, and never stop the OTHER listeners from running --
        # the listener runs on the same worker thread that owns state,
        # permits and Stop, so an uncaught exception here would silently
        # kill that thread and freeze JARVIS. Caught and logged instead.
        started = time.monotonic()
        try:
            listener(entry)
        except Exception:
            logger.exception("Transition listener raised; ignoring and continuing")
        elapsed = time.monotonic() - started
        if elapsed > self._transition_listener_warn_seconds:
            # Not a timeout -- the call already ran to completion by the
            # time we can measure it. This only gives visibility that a
            # listener is slow (R15); it cannot itself stop a listener
            # that hangs. Listeners are expected to be fast, synchronous,
            # in-memory work (F5's logbook is); anything slower belongs
            # on its own thread, not in this callback.
            logger.warning(
                "Transition listener took %.3fs (budget %.3fs) -- Stop's promptness depends on this staying fast",
                elapsed,
                self._transition_listener_warn_seconds,
            )
