"""The decision record (F5) and the sensitive-text guard it uses (R11, F15).

A decision record is JARVIS's own structured data about one command, not
model reasoning text (R13). "Why did you do that?" must be answerable by
reading these fields back, never by asking a model to explain itself
after the fact.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from jarvis.controller.permits import OriginTag
from jarvis.controller.trust import TrustLevel
from jarvis.settings import SETTINGS

_LOGBOOK_CFG = SETTINGS["logbook"]
_BOUNDARY_MARKERS: tuple[str, ...] = tuple(m.lower() for m in _LOGBOOK_CFG["sensitive_markers_boundary"])
_PHRASE_MARKERS: tuple[str, ...] = tuple(m.lower() for m in _LOGBOOK_CFG["sensitive_markers_phrase"])
_REDACTED_PLACEHOLDER: str = _LOGBOOK_CFG["redacted_placeholder"]

_BOUNDARY_PATTERN = (
    re.compile(r"\b(?:" + "|".join(re.escape(m) for m in _BOUNDARY_MARKERS) + r")\b")
    if _BOUNDARY_MARKERS
    else None
)

# What each placeholder in a [setup_messages] template may be filled with.
# Kept narrow on purpose: a clock time, or a Windows voice name (one line,
# no full stop), so nothing else can ride along inside an exempt message.
_SETUP_PLACEHOLDERS = {
    "until": r"(?:\d{1,2}:\d{2}|later)",
    "voice": r"[^.\n]{1,100}",
}


def _setup_message_pattern(template: str) -> re.Pattern[str]:
    parts = re.split(r"\{(\w+)\}", template)
    # re.split with a group alternates literal text and placeholder names.
    regex = "".join(
        re.escape(part) if i % 2 == 0 else _SETUP_PLACEHOLDERS.get(part, r"(?!)")
        for i, part in enumerate(parts)
    )
    return re.compile(regex)


_SETUP_MESSAGE_PATTERNS = tuple(_setup_message_pattern(t) for t in SETTINGS["setup_messages"].values())


def is_setup_message(text: str) -> bool:
    """True only if the whole text is one of JARVIS's own missing-setup
    messages (settings.toml [setup_messages]). Those are fixed sentences
    written by the developer -- they name a setting ("Add GROQ_API_KEY to
    the .env file"), never its value, and hold no laptop data -- so the
    redactor can let them through and the activity log shows the real fix
    instead of "[redacted]"."""
    return any(p.fullmatch(text) for p in _SETUP_MESSAGE_PATTERNS)


def redact_sensitive(text: str | None) -> str | None:
    """R11/F15: replace the whole field, not just the matched word, since
    we cannot be sure where a secret starts or ends inside it.

    Two checks: a word-boundary regex for short markers that plain
    substring matching would trip on inside ordinary words ("otp" inside
    "hotpot"), and plain substring containment for longer, more
    distinctive markers, per settings.toml's comment on this trade-off.

    The one exception is an exact missing-setup message (is_setup_message):
    anything added to it, even one word, makes it ordinary text again.
    """
    if text is None:
        return None
    if is_setup_message(text):
        return text
    lowered = text.lower()
    if _BOUNDARY_PATTERN is not None and _BOUNDARY_PATTERN.search(lowered):
        return _REDACTED_PLACEHOLDER
    if any(marker in lowered for marker in _PHRASE_MARKERS):
        return _REDACTED_PLACEHOLDER
    # F10's never-store check: card numbers (Luhn), bank and ID numbers. What
    # memory refuses to store is never written anywhere else either.
    from jarvis.memory.sensitive import sensitive_type

    if sensitive_type(text) is not None:
        return _REDACTED_PLACEHOLDER
    return text


# (a volcano answer showed as "saying: [redacted]"
# because "secret" matched inside "secrete"). Her answers are checked for
# real secrets only -- a key-shaped string, a sensitive word WITH a value
# ("password is hunter2", "pin: 4821"), or a card/bank/ID number -- never
# for a bare word.
_KEY_SHAPED = re.compile(r"\b(?:gsk|sk|pk|rk|ghp|gho|xoxb|xoxp|AKIA)[_-]?[A-Za-z0-9_-]{12,}|\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{32,}\b")
_MARKER_WITH_VALUE = re.compile(r"\b(?:password|passcode|pin|otp|api[ _]key|secret|token)s?\b\s*(?:is|was|=|:)\s*['\"]?[A-Za-z0-9!@#$%^&*_-]*\d[A-Za-z0-9!@#$%^&*_-]*",
                                re.IGNORECASE)


def redact_answer(text: str | None) -> str | None:
    """What SHE says (an answer, a "saying:" line): redacted only for a real
    secret, the whole field as R11 asks. His own words keep redact_sensitive."""
    if text is None:
        return None
    if is_setup_message(text):
        return text
    if _KEY_SHAPED.search(text) or _MARKER_WITH_VALUE.search(text):
        return _REDACTED_PLACEHOLDER
    from jarvis.memory.sensitive import sensitive_type

    if sensitive_type(text) is not None:
        return _REDACTED_PLACEHOLDER
    return text


# Recursion: walks nested dicts/lists of any depth and redacts every string inside.
def redact_value(value: Any) -> Any:
    """Recursively apply redact_sensitive to every string inside a
    structure (dict/list/str); other types pass through unchanged. Used
    for fields like `understood` that can hold nested free text (e.g.
    action parameters)."""
    if isinstance(value, str):
        return redact_sensitive(value)
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    return value


@dataclass(frozen=True)
class Candidate:
    """One thing JARVIS considered while resolving a command (F3/F4 fill
    this in; empty for commands that never needed to pick among options)."""

    label: str
    score: float  # 0.0-1.0, how well it matched


@dataclass
class DecisionRecord:
    """One row per command, per F5's required fields."""

    id: str
    timestamp: float  # wall-clock (time.time()), not monotonic -- F6 shows this as a real clock time
    heard: str | None = None  # what she heard or was typed
    understood: dict[str, Any] | None = None  # intent + typed parameters
    candidates: list[Candidate] = field(default_factory=list)
    choice: str | None = None  # what she decided to do/say
    rule_or_trust_level: TrustLevel | str | None = None
    origin: OriginTag | None = None
    result: str | None = None
    verification_result: str | None = None
    # Steps reported while the command is still running ("searching
    # files...", "found 2 matches"), in the order they really happened.
    # The dashboard shows them live; see Logbook.add_step.
    progress: list[str] = field(default_factory=list)
    # Where an online request went ("Groq, openai/gpt-oss-20b"). Kept in the
    # record and the debug log only, never shown in Live Activity (R3,
    # amended).
    online_destination: str | None = None
    # Lines shown after what she said, in order: F16a's credit line, F14's
    # check-in. See Logbook.add_step(closing=True).
    closing: list[str] = field(default_factory=list)
    # Steps (in `progress` or `closing`) shown in Live Activity for this
    # session only and never written to the debug log -- F14: the mood
    # guess is never saved (F10 lists it as session data).
    session_only: list[str] = field(default_factory=list)
    # F17's JARVIS-started work (F20's first-wake topic check, F21's
    # briefing): what she started, shown instead of "heard:". None for a
    # command he gave.
    started_by_jarvis: str | None = None
    completed: bool = False

    @staticmethod
    def start(heard: str | None) -> "DecisionRecord":
        """A new record for what was heard (None for an event she started herself)."""
        return DecisionRecord(
            id=str(uuid.uuid4()),
            timestamp=time.time(),
            heard=redact_sensitive(heard),
        )
