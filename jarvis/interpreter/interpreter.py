"""The interpreter (4.1): turns heard/typed text into a structured
intent. Deterministic first, a model only if needed (4.2) -- this build
only has deterministic matching; the "if needed" model path arrives with
F2's free-form Q&A.

It never returns code, shell strings or a trust level (4.2) -- only plain
data. The controller's policy engine separately authorizes whatever this
returns; the interpreter has no say in that.

F1, F3 and F4 register their matchers here (see jarvis/actions/).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Tuple, Union

from jarvis.logbook.records import Candidate

Matcher = Callable[[str], Optional[Union["Answer", "ActionRequest"]]]


@dataclass(frozen=True)
class Answer:
    """A direct spoken answer, no OS action involved (L0).

    `candidates` is optional, for an answer that itself reports what was
    considered (F4's "here are the likely matches" listing) -- most
    answers (F1's intro, plain Q&A) leave it empty.
    """

    text: str
    candidates: List[Candidate] = field(default_factory=list)
    # R4 (F20): when set, the decision record and debug.log
    # hold this instead of `text` (which names his stored topics); the
    # spoken text is shown in Live Activity for the session only.
    logged: Optional[str] = None
    # F19: what Live Activity's "saying:" line shows instead of
    # `text` (which holds a file's content: said aloud, shown nowhere).
    shown: Optional[str] = None


@dataclass(frozen=True)
class ActionRequest:
    """A request to run one of the fixed actions (4.2), at whatever trust
    level policy.py assigns it -- the interpreter does not decide that.

    `candidates` is what the matcher considered and how well each
    matched (F5's decision record field) -- optional, since not every
    action involves picking among options (F1's intro doesn't; F3/F4 do).
    """

    action_name: str
    params: dict[str, Any]
    candidates: List[Candidate] = field(default_factory=list)
    # Said when the action needs a confirmation (L2-L4) and the preview is
    # up, e.g. F10's "This deletes all 4 things I remember ... Say confirm,
    # then confirm again." Empty: nothing extra is said.
    prompt: str = ""


class Interpreter:
    """Turns text into an Answer or an ActionRequest by trying the registered matchers in order; the first
    match wins (4.1)."""
    def __init__(self) -> None:
        self._matchers: list[Tuple[str, Matcher, object]] = []

    def register(self, name: str, matcher: Matcher, raw: bool = False, keep_case: bool = False) -> None:
        """Add a matcher (every feature registers its own). `name` is
        only for debugging/introspection; matching order is registration
        order, first match wins.

        Local matchers get the cleaned command (command_text.clean_command:
        no "please"/"sir"/"can you", no leading name). `raw=True` -- only
        F2's online conversation -- gets the words as said, lower case, so
        what is sent online is exactly what the developer asked.
        `keep_case=True` (F18) gets the words exactly as typed,
        case kept: a YouTube video ID is case-sensitive."""
        self._matchers.append((name, matcher, "case" if keep_case else raw))

    def interpret(self, text: str, skip: frozenset = frozenset()) -> Answer | ActionRequest | None:
        """`skip`: matcher names left out (The intent router
        re-runs its rewritten command without itself and the conversation)."""
        from jarvis.actions.command_text import clean_command, extract_command, split_glued  # actions imports interpreter

        text = text.replace("’", "'")  # Whisper's curly apostrophe ("Why’d you do that?")
        normalized = text.strip().lower()
        # A command anywhere in the sentence ("this is so
        # annoying, open notepad") is what the local matchers see, so it runs
        # here instead of going online. The online conversation still gets
        # the words as said -- and only if no local matcher took them.
        # A verb glued to a known name ("OpenNote Pad") is split
        # first.
        unglued = split_glued(text)
        command = extract_command(unglued)
        cleaned = clean_command(command if command is not None else unglued)
        for _name, matcher, raw in self._matchers:
            if _name in skip:
                continue
            result = matcher(text.strip() if raw == "case" else normalized if raw else cleaned)
            if result is not None:
                return result
        return None
