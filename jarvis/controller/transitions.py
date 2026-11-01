"""The fixed table of allowed state changes (R6, 4.2).

Deny by default: a (state, event) pair that is not a key here is refused
by the controller, not silently allowed. Stop (R8) is the one interrupt
that bypasses this table entirely -- see Controller.request_stop.

Conversation mode (developer-approved; merges F2 and F15):
- SLEEPING is the startup state. Only WAKE leaves it (the wake word,
  "wake up JARVIS", the wake hotkey or the typed wake phrase): she greets
  (SPEAKING), then she is AWAKE.
- AWAKE needs no wake word: any utterance (or a typed command) starts a
  command with SPEECH_STARTED.
- "Go to sleep": she says "Goodnight." (SPEAKING), then GO_TO_SLEEP
  returns her to SLEEPING. GO_TO_SLEEP from AWAKE is the optional
  auto-sleep timer.
- "Cancel" while CONFIRMING (approved table change) drops the pending
  action.
- Stop while SLEEPING leaves her SLEEPING: it is already a safe, inert
  state, and only WAKE may leave it (see Controller._handle_stop_locked).
"""

from __future__ import annotations

from jarvis.controller.events import EventType
from jarvis.controller.states import State

ALLOWED_TRANSITIONS: dict[tuple[State, EventType], State] = {
    (State.SLEEPING, EventType.WAKE): State.SPEAKING,  # the time-of-day greeting
    (State.SPEAKING, EventType.GO_TO_SLEEP): State.SLEEPING,  # after "Goodnight."
    (State.AWAKE, EventType.GO_TO_SLEEP): State.SLEEPING,  # optional auto-sleep timer
    (State.AWAKE, EventType.SPEECH_STARTED): State.LISTENING,
    (State.LISTENING, EventType.COMMAND_CAPTURED): State.UNDERSTANDING,
    (State.LISTENING, EventType.LISTEN_TIMEOUT): State.AWAKE,
    (State.UNDERSTANDING, EventType.UNDERSTAND_FAILED): State.SPEAKING,
    (State.UNDERSTANDING, EventType.ANSWER_READY): State.SPEAKING,
    (State.UNDERSTANDING, EventType.ACTION_REQUESTED): State.ACTING,
    (State.ACTING, EventType.ACTION_DONE): State.VERIFYING,
    (State.VERIFYING, EventType.VERIFICATION_COMPLETE): State.SPEAKING,
    (State.SPEAKING, EventType.SPEECH_FINISHED): State.AWAKE,
    # F17 (approved): JARVIS-started work -- F20's first-wake
    # topic check and F21's briefing -- starts only from AWAKE.
    (State.AWAKE, EventType.STARTUP_TASK_REQUESTED): State.UNDERSTANDING,
    # F7 L2-L4 (Section 5): an ACTION_REQUESTED at L2/L3/L4 is rerouted here
    # by the controller instead of straight to ACTING -- the same "borrow
    # another event's table entry" technique already used to reroute a
    # policy-denied ACTION_REQUESTED to UNDERSTAND_FAILED's target.
    # CONFIRMATION_REQUIRED is never submitted by a caller; it exists only
    # so this reroute has a table entry to look up (deny-by-default still
    # governs every state actually reachable from outside).
    (State.UNDERSTANDING, EventType.CONFIRMATION_REQUIRED): State.CONFIRMING,
    # "Confirm" while genuinely pending. The controller only applies this
    # target once the required number of confirmations (1 for L2/L3, 2 for
    # L4) has been reached -- otherwise it stays in CONFIRMING and asks
    # again. A CONFIRM_RECEIVED from any other state has no table entry,
    # so it is denied by default -- that's how "confirm with nothing
    # pending" is answered when not even in CONFIRMING.
    (State.CONFIRMING, EventType.CONFIRM_RECEIVED): State.ACTING,
    # A confirm that doesn't match the pending action, or has expired,
    # reroutes here -- reusing UNDERSTAND_FAILED's meaning ("nothing valid
    # to act on") rather than inventing a new spoken-response path.
    (State.CONFIRMING, EventType.UNDERSTAND_FAILED): State.SPEAKING,
    # "Cancel" drops the pending action; she says so (SPEAKING).
    (State.CONFIRMING, EventType.CANCEL_RECEIVED): State.SPEAKING,
}
