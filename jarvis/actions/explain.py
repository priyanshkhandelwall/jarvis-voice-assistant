"""F5/R13: "Why did you do that?" answered from the decision record.

A deterministic matcher, registered before F2's Groq conversation so the
question never goes online: the answer is Logbook.answer_why() read back
from the latest record, never a model's after-the-fact explanation (R13).

"Latest" skips earlier "why" questions, so asking twice explains the
same action again instead of explaining the previous "why".
it also skips everything that did nothing (thanks, small talk, the time,
local answers): "why" is about the last thing she DID.
"""

from __future__ import annotations

from typing import Iterable, Optional

from jarvis.actions.command_text import clean_command, normalize, strip_name
from jarvis.interpreter import Answer
from jarvis.logbook import Logbook
from jarvis.logbook.logbook import NOTHING_YET
from jarvis.settings import SETTINGS

_CFG = SETTINGS["explain"]


def _words(text: str) -> list[str]:
    """Filler and her name (any spelling) removed, so "Can you tell me why
    you did that, Javis?" reads "tell me why you did that"."""
    return strip_name(normalize(clean_command(text.replace("’", "'"))).split())


# Activity entries that aren't actions she took: what she overheard while
# asleep, ignored fragments, and the woke-up / went-to-sleep markers.
_NON_ACTION_KEYS = ("asleep_heard", "ignored", "event")


def _is_non_action(record) -> bool:
    understood = record.understood if isinstance(record.understood, dict) else {}
    return any(key in understood for key in _NON_ACTION_KEYS)


def did_something(record) -> bool:
    """(after "Thanks", "what did you do that" said "I
    answered because you asked: Thanks."): a record that DID something --
    opened, found, changed, remembered or forgot (an action), searched
    online, or listed what it found. Small talk, thanks, the time and local
    answers did nothing."""
    understood = record.understood if isinstance(record.understood, dict) else {}
    return bool(understood.get("action_name") or understood.get("ui_action") or record.online_destination or record.candidates)


class ExplainMatcher:
    """F5/R13: answers 'why did you do that?' from her own decision record, never from a model."""
    def __init__(
        self,
        logbook: Logbook,
        prefixes: Optional[Iterable[str]] = None,
        phrases: Optional[Iterable[str]] = None,
    ) -> None:
        self._logbook = logbook
        self._prefixes = [p.split() for p in (prefixes if prefixes is not None else _CFG["prefixes"])]
        self._phrases = {" ".join(p.split()) for p in (phrases if phrases is not None else _CFG["phrases"])}

    def is_why_question(self, text: Optional[str]) -> bool:
        """True if `text` asks why she did her last action."""
        words = _words(text or "")
        if not words:
            return False
        if " ".join(words) in self._phrases:
            return True
        return any(words[: len(prefix)] == prefix for prefix in self._prefixes)

    def _asked_why(self, text: Optional[str]) -> bool:
        """An earlier "why" question, in a fixed phrase or one the intent
        router understood as "why" (After "What was the reason
        for that?", "Explain what you just did" explained that question
        instead of the Notepad it was about)."""
        if self.is_why_question(text):
            return True
        if not text:
            return False
        from jarvis.router.router import IntentRouter  # the router imports actions

        verdict, routes = IntentRouter().decide(clean_command(text))
        return verdict == "run" and routes[0].intent.name == "why"

    def __call__(self, normalized_text: str) -> Optional[Answer]:
        if not self.is_why_question(normalized_text):
            return None
        earlier = [
            r
            for r in self._logbook.records()
            if r.completed and not _is_non_action(r) and did_something(r) and not self._asked_why(r.heard)
        ]
        if not earlier:
            return Answer(NOTHING_YET)
        return Answer(self._logbook.answer_why(earlier[-1].id))

    def register(self, interpreter) -> None:
        """Must be registered before the Groq conversation matcher."""
        interpreter.register("explain", self)
