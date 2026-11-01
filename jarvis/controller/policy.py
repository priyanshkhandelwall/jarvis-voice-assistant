"""The policy engine (4.2). Deterministic, never asks a model.

Deny by default: an action not in the fixed action set is refused. A
trust level this build has not implemented a confirmation flow for is
also refused, even though it exists in TRUST_TABLE, because refusing is
safer than pretending to grant it.
"""

from __future__ import annotations

from dataclasses import dataclass

from jarvis.controller.trust import FIXED_ACTION_SET, IMPLEMENTED_LEVELS, TRUST_TABLE, TrustLevel


@dataclass(frozen=True)
class AuthDecision:
    """The policy's answer for one action: allowed or not, its fixed trust level, and the reason (shown in
    the log)."""
    allowed: bool
    trust_level: TrustLevel | None
    reason: str


def authorize(action_name: str) -> AuthDecision:
    """Look up `action_name` in the fixed action set and trust table (F7). Anything unknown is denied by
    default; a model never decides this."""
    if action_name not in FIXED_ACTION_SET:
        return AuthDecision(False, None, f"'{action_name}' is not a fixed action (deny by default)")

    trust_level = TRUST_TABLE.get(action_name)
    if trust_level is None:
        return AuthDecision(False, None, f"'{action_name}' has no trust level in the fixed table")

    if trust_level not in IMPLEMENTED_LEVELS:
        return AuthDecision(
            False,
            trust_level,
            f"'{action_name}' requires {trust_level.name}, which is not implemented in this build",
        )

    return AuthDecision(True, trust_level, "immediate: L0/L1 requires no confirmation")
