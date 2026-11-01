"""Conversation mode (merges F2 and F15): the local wake and
sleep phrases.

Both are matched here, on the device, from the typed text or the local
transcript -- "go to sleep" is never sent online, and "wake up JARVIS" is
recognised without any network call.

Fuzzy because in testing Whisper wrote "Wake up,
Johnless.", "Make up Jarvis.", "Welcome, Charvis!" and "Good night, Javis"
for the real phrases. Her name is matched by command_text.is_name (a list
of spellings plus an edit distance), and "make up" / "welcome" count as
"wake up" when a name-like word goes with them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional

from jarvis.actions.command_text import edit_distance, is_name, normalize, strip_name
from jarvis.interpreter import Answer
from jarvis.settings import SETTINGS

__all__ = ["normalize", "is_wake_phrase", "SleepMatcher", "SleepRequest", "AlreadyAwakeMatcher"]

_CFG = SETTINGS["conversation"]
_WAKE_VERBS = frozenset(str(w).lower() for w in _CFG["wake_verbs"])
_GREETING_WORDS = frozenset(str(w).lower() for w in _CFG["wake_greeting_words"])
_AWAKE_QUESTIONS = [["are", "you", "awake"], ["you", "awake"], ["are", "you", "up"], ["are", "you", "awake", "now"],
                    ["are", "you", "still", "awake"], ["you", "still", "awake"]]


@dataclass(frozen=True)
class SleepRequest:
    """The interpreter's result for "go to sleep": she says `reply`, then
    the controller moves her to SLEEPING."""

    reply: str


class SleepMatcher:
    """"go to sleep", "good night [name]", "goodnight", "sleep [name]" --
    her name in any spelling at either end ("Good night, Javis").

    Near-misses (A typed "got to sleep" went online and
    Groq answered "Going to sleep now" without her sleeping): a phrase that
    contains "sleep" also matches within [conversation]
    sleep_max_edit_distance letters, as long as what was said has a word
    starting with "sle" ("goto sleep", "go to sleap"). Phrases without
    "sleep" ("good night") stay exact, so "good light" never sleeps.

    Anywhere in the sentence ("Thank you, go to sleep" and
    "Hijar, VS Code to Sleep" went online): the phrase may END a longer
    sentence ("ok jarvis go to sleep now"), and any sentence ending "... to
    sleep" sleeps, except "I need/want to sleep". A sentence that starts
    like a question or another command ([conversation]
    sleep_question_starts: "how much should I sleep", "what is sleep mode",
    "remind me to go to sleep") never does."""

    def __init__(self, phrases: Optional[Iterable[str]] = None, reply: Optional[str] = None, max_distance: Optional[int] = None) -> None:
        default = [*_CFG["sleep_phrases"], *_CFG.get("sleep_mishearings", [])]  # "gota slide"
        self._phrases = {normalize(p) for p in (phrases if phrases is not None else default)}
        self._loose = [p for p in self._phrases if "sleep" in p.split()]
        self._max_distance = int(max_distance if max_distance is not None else _CFG.get("sleep_max_edit_distance", 0))
        self._reply = str(reply if reply is not None else _CFG["sleep_reply"])
        self._anywhere = phrases is None  # an explicit list (tests) means whole phrases only
        self._tail_ok = {normalize(w) for w in _CFG.get("sleep_tail_words", [])}
        self._question = {normalize(w).replace(" ", "") for w in _CFG.get("sleep_question_starts", [])}
        self._not_after = {normalize(w) for w in _CFG.get("sleep_not_after", [])}

    def _matches(self, said: str) -> bool:
        if said in self._phrases:
            return True
        if self._max_distance > 0 and any(w.startswith("sle") for w in said.split()):
            return any(edit_distance(said, p) <= self._max_distance for p in self._loose)
        return False

    def __call__(self, normalized_text: str) -> Optional[SleepRequest]:
        said = " ".join(strip_name(normalize(normalized_text).split()))
        if self._matches(said):
            return SleepRequest(self._reply)
        if not self._anywhere:
            return None
        return SleepRequest(self._reply) if self._ends_with_sleep(normalize(normalized_text).split()) else None

    def _ends_with_sleep(self, words: List[str]) -> bool:
        """A sleep phrase as the last part of a longer sentence."""
        words = [w for w in words if not is_name(w)]
        while len(words) > 2 and words[1] == "you" and words[0] in ("can", "could", "would", "will"):
            words = words[2:]  # "can you go to sleep" is an order, not a question
        if len(words) < 2 or words[0].replace("'", "") in self._question:
            return False
        while len(words) > 1 and words[-1] in self._tail_ok:
            words = words[:-1]
        for size in range(min(len(words), 5), 0, -1):
            tail, start = words[-size:], len(words) - size
            joined = " ".join(tail)
            # Whole phrases only for short tails: "to sleep" is not "go sleep".
            exact = joined in self._phrases and joined != "sleep"
            loose = size >= 3 and self._matches(joined)
            if not (exact or loose):
                continue
            # Who is to sleep: "i need to sleep", "the baby went to sleep".
            lead = words[:start]
            if self._about_someone_else(lead, tail):
                return False
            return True
        if words[-2:] == ["to", "sleep"]:
            return not self._about_someone_else(words[:-2], ["to", "sleep"])
        return False

    def _about_someone_else(self, lead: List[str], tail: List[str]) -> bool:
        """The words before the phrase say it isn't an order to her."""
        if not lead:
            return False
        last = lead[-1]
        if last in self._not_after:
            return True
        if tail[0] == "to" or last == "to":
            before = lead[-2] if last == "to" and len(lead) > 1 else last
            return before in self._not_after
        return False


def _wake_form_at(words: List[str], i: int) -> bool:
    """A fuzzy wake form starting at words[i]."""
    rest = words[i:]
    if len(rest) >= 3 and rest[0] in _WAKE_VERBS and rest[1] == "up" and is_name(rest[2]):
        return True  # "wake up jarvis", "make up jarvis", "wake up johnless"
    if len(rest) >= 3 and is_name(rest[0]) and rest[1] in _WAKE_VERBS and rest[2] == "up":
        return True  # "jarvis wake up"
    if len(rest) >= 2 and rest[0] in _GREETING_WORDS and is_name(rest[1]):
        return True  # "hey javis ...", "welcome charvis"
    return False


def is_wake_phrase(text: str, phrases: Optional[Iterable[str]] = None) -> bool:
    """True if the text is, or starts with, a wake phrase ("wake up JARVIS",
    "Hey JARVIS, are you there?"), allowing her name to be misheard and
    one word before it ("okay, wake up Jarvis"). Plain "wake up" or "hey"
    without a name-like word never counts."""
    said = normalize(text)
    if not said:
        return False
    for phrase in phrases if phrases is not None else _CFG["wake_phrases"]:
        wanted = normalize(phrase)
        if said == wanted or said.startswith(wanted + " "):
            return True
    if phrases is not None:
        return False  # an explicit list (tests) means exact phrases only
    words = said.split()
    return any(_wake_form_at(words, i) for i in range(min(2, len(words))))


class AlreadyAwakeMatcher:
    """"Wake up" while she is AWAKE: answered locally ("I'm already
    awake."), never sent online. Registered before the greeting, so "hey
    jarvis, wake up" is this, not a hello."""

    def __init__(self, reply: Optional[str] = None) -> None:
        self._reply = str(reply if reply is not None else _CFG["already_awake_reply"])

    def __call__(self, normalized_text: str) -> Optional[Answer]:
        words = strip_name(normalize(normalized_text).split())
        while words and words[0] in _GREETING_WORDS - {"welcome"}:
            words = strip_name(words[1:])
        if words == ["wake", "up"] or words in _AWAKE_QUESTIONS:
            return Answer(self._reply)  # "Are you awake" went online
        if len(words) == 2 and words[0] in _WAKE_VERBS and words[1] == "up" and is_wake_phrase(normalized_text):
            return Answer(self._reply)  # "make up, jarvis": a misheard "wake up" only with her name
        if words == ["welcome"] and is_wake_phrase(normalized_text):
            return Answer(self._reply)
        return None
