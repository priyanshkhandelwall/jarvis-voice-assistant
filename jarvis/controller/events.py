"""Events that modules may send to the controller.

Modules never change state directly (R6, 4.2). They build an Event and
submit it to the controller, which alone decides whether it is allowed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any


class EventType(Enum):
    # Conversation mode: SLEEPING -> WAKE -> (greeting) -> AWAKE.
    """Every event the controller understands. Only the transition table decides what each one does in each
    state."""
    WAKE = auto()  # "Hey JARVIS", "wake up JARVIS", the wake hotkey or the typed wake phrase
    GO_TO_SLEEP = auto()  # "go to sleep" after "Goodnight.", or the optional auto-sleep timer
    SPEECH_STARTED = auto()  # a command begins while AWAKE: speech heard, press-to-talk, or typed
    LISTEN_TIMEOUT = auto()
    COMMAND_CAPTURED = auto()
    UNDERSTAND_FAILED = auto()
    ANSWER_READY = auto()
    ACTION_REQUESTED = auto()
    ACTION_DONE = auto()
    VERIFICATION_COMPLETE = auto()
    SPEECH_FINISHED = auto()
    # F7 L2-L4 confirmation engine.
    CONFIRMATION_REQUIRED = auto()  # controller-internal only -- see transitions.py
    CONFIRM_RECEIVED = auto()  # "confirm", submitted the same way any command is
    CANCEL_RECEIVED = auto()  # "cancel" while CONFIRMING (typed, or spoken while awake)
    # F17's JARVIS-started work (approved, needed by F20/F21):
    # the first-wake topic check and the morning briefing. The controller
    # tags everything in that interaction JARVIS_INITIATED from this event
    # type alone; a payload claiming an origin is ignored.
    STARTUP_TASK_REQUESTED = auto()


@dataclass(frozen=True)
class Event:
    """One event for the controller: its type and an optional payload (for example the action name and
    params)."""
    type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
