"""The fixed set of JARVIS states (reference Section 4.3).

ASLEEP is defined here so the state machine has room for Sleep Mode later,
but no transition in this build ever enters or leaves it (Sleep Mode, F15,
is out of scope for this build).
"""

from __future__ import annotations

from enum import Enum, auto


class State(Enum):
    """F17: the eight states of the core state machine (see ARCHITECTURE.md)."""
    SLEEPING = auto()
    AWAKE = auto()
    LISTENING = auto()
    UNDERSTANDING = auto()
    CONFIRMING = auto()
    ACTING = auto()
    VERIFYING = auto()
    SPEAKING = auto()
