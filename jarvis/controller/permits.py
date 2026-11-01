"""One-time permits and the origin tag (R7, Section 5).

A permit is valid only for the exact action name and exact parameters it
was issued for. It is consumed on first successful redemption and becomes
invalid if the action completes, fails, is cancelled, if its parameters
change, if the state moves on, or if it times out (F17).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

from jarvis.controller.states import State
from jarvis.controller.trust import TrustLevel


class OriginTag(Enum):
    """Only the controller sets this, from the spoken/typed command (Section 5)."""

    DEVELOPER_REQUESTED = auto()
    JARVIS_INITIATED = auto()


# Classes & Objects: a frozen dataclass, so a permit can't be altered after it is issued.
@dataclass(frozen=True)
class Permit:
    """F7: a one-time, short-lived permission to run exactly one action with exactly these params.
    Executors refuse to run without a matching permit."""
    id: str
    action_name: str
    params: tuple[tuple[str, Any], ...]  # sorted dict items, hashable & orderless-safe
    origin: OriginTag
    trust_level: TrustLevel
    issued_at: float
    issued_in_state: State
    timeout_seconds: float

    @staticmethod
    def issue(
        action_name: str,
        params: dict[str, Any],
        origin: OriginTag,
        trust_level: TrustLevel,
        issued_in_state: State,
        timeout_seconds: float,
    ) -> "Permit":
        """Create a permit for `action_name` with `params` (stored sorted, so it can be compared), stamped
        with the time it was issued."""
        return Permit(
            id=str(uuid.uuid4()),
            action_name=action_name,
            params=tuple(sorted(params.items())),
            origin=origin,
            trust_level=trust_level,
            issued_at=time.monotonic(),
            issued_in_state=issued_in_state,
            timeout_seconds=timeout_seconds,
        )

    def matches(self, action_name: str, params: dict[str, Any]) -> bool:
        """True if this permit is for exactly this action and these params."""
        return self.action_name == action_name and self.params == tuple(sorted(params.items()))

    def is_expired(self, now: float | None = None) -> bool:
        """True once the permit is older than its timeout (`now` defaults to the monotonic clock)."""
        current = now if now is not None else time.monotonic()
        return (current - self.issued_at) > self.timeout_seconds


@dataclass
class PermitState:
    """Mutable wrapper so redemption can flip `used` without breaking the
    frozen, hashable Permit identity used for equality checks in tests."""

    permit: Permit
    used: bool = False


@dataclass
class PendingConfirmation:
    """F7 L2-L4 (Section 5): the not-yet-permitted action waiting on one
    or more "confirm"s. Mutable (unlike Permit) because confirmations_
    received advances in place as CONFIRM_RECEIVED events arrive -- there
    is at most one of these live at a time, held directly by the
    controller, never exposed for a caller to mutate directly.

    "Any change to the action voids it" (Section 5) holds structurally:
    the state machine has no transition that lets a second ACTION_
    REQUESTED overwrite a pending confirmation, so the only ways out are
    a matching confirm, expiry, or Stop.
    """

    id: str
    action_name: str
    params: tuple[tuple[str, Any], ...]
    origin: OriginTag
    trust_level: TrustLevel
    issued_at: float
    timeout_seconds: float
    confirmations_required: int
    confirmations_received: int = 0

    @staticmethod
    def issue(
        action_name: str,
        params: dict[str, Any],
        origin: OriginTag,
        trust_level: TrustLevel,
        timeout_seconds: float,
        confirmations_required: int,
    ) -> "PendingConfirmation":
        """Create the confirmation an L2-L4 action waits on: what will run, who asked, and how many
        confirms it needs."""
        return PendingConfirmation(
            id=str(uuid.uuid4()),
            action_name=action_name,
            params=tuple(sorted(params.items())),
            origin=origin,
            trust_level=trust_level,
            issued_at=time.monotonic(),
            timeout_seconds=timeout_seconds,
            confirmations_required=confirmations_required,
        )

    def matches(self, action_name: str, params: dict[str, Any]) -> bool:
        """True if this confirmation is for exactly this action and these params."""
        return self.action_name == action_name and self.params == tuple(sorted(params.items()))

    def is_expired(self, now: float | None = None) -> bool:
        """True once the confirmation has waited longer than its timeout."""
        current = now if now is not None else time.monotonic()
        return (current - self.issued_at) > self.timeout_seconds

    def is_fully_confirmed(self) -> bool:
        """True when it has had every confirm it needs (one for L2/L3, two for L4)."""
        return self.confirmations_received >= self.confirmations_required
