"""F2: greeting by time of day (developer decision F).

She greets only when greeted ("hello", "good morning", "hi jarvis"), and
answers locally from the system clock -- no Groq call. The time-of-day
boundaries are F2's DEFAULT settings (morning until 12:00, afternoon
until 17:00, evening after).
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Iterable, Optional

from jarvis.actions.command_text import normalize, strip_name
from jarvis.interpreter import Answer
from jarvis.settings import SETTINGS

_CFG = SETTINGS["greeting"]
_GREETING_TAIL = {str(w).lower() for w in _CFG.get("tail_words", [])}


def period_of_day(hour: int, morning_ends: int, afternoon_ends: int) -> str:
    """'morning', 'afternoon' or 'evening' for `hour`, using the configured boundaries."""
    if hour < morning_ends:
        return "morning"
    if hour < afternoon_ends:
        return "afternoon"
    return "evening"


class SmallTalkMatcher:
    """Thanks get a varied
    "You're welcome." and a bare acknowledgement a short "Okay." -- local,
    cached audio, never online. An utterance counts when every word left
    (her name and [small_talk] padding words dropped) is a thanks word, with
    at least one thanks root in it; or it is only acknowledgement words."""

    def __init__(self, choose: Callable = None) -> None:
        import random

        cfg = SETTINGS["small_talk"]
        self._thanks = {str(w).lower() for w in cfg["thanks_words"]}
        self._roots = tuple(str(w).lower() for w in cfg["thanks_roots"])
        self._padding = {str(w).lower() for w in cfg["padding_words"]}
        self._acks = {str(w).lower() for w in cfg["ack_words"]}
        self._welcome = [str(x) for x in cfg["welcome_replies"]]
        self._ok = [str(x) for x in cfg["ack_replies"]]
        self._how_are_you = {" ".join(str(p).lower().split()) for p in cfg["how_are_you_phrases"]}
        self._how_tail = {str(w).lower() for w in cfg["how_are_you_tail_words"]}
        self._fine = [str(x) for x in cfg["how_are_you_replies"]]
        # Compliments ("You are so awesome" went online).
        self._compliments = {" ".join(normalize(str(p)).split()) for p in cfg["compliment_phrases"]}
        self._compliment_starts = [normalize(str(p)).split() for p in cfg["compliment_starts"]]
        self._praise = {str(w).lower() for w in cfg["praise_words"]}
        self._intensifiers = {str(w).lower() for w in cfg["intensifiers"]}
        name = str(SETTINGS["user"]["name"])
        self._thank_you = [str(x).format(name=name) for x in cfg["compliment_replies"]]
        self._choose = choose or random.choice
        self._last = None

    def _pick(self, pool):
        options = [p for p in pool if p != self._last] or pool
        self._last = self._choose(options)
        return self._last

    def _known(self, word: str) -> str:
        """A stray
        plural "s" on a word she knows is that word."""
        known = self._padding | self._acks | self._thanks
        return word[:-1] if word not in known and word.endswith("s") and word[:-1] in known else word

    def __call__(self, normalized_text: str) -> Optional[Answer]:
        said = [self._known(w) for w in strip_name(normalize(normalized_text).split())]
        # "How are you doing today?" was a live web
        # search ("today"), which failed and opened Google in Chrome.
        asked = list(said)
        while asked and asked[-1] in self._how_tail:
            asked = asked[:-1]
        while asked and asked[0] in ("hey", "hi", "hello", "so", "and", "oh"):
            asked = strip_name(asked[1:])  # "hey jarvis, how's it going"
        if " ".join(asked) in self._how_are_you:
            return Answer(self._pick(self._fine))
        if self._is_compliment(said):
            return Answer(self._pick(self._thank_you))
        words = [w for w in said if w not in self._padding]
        if not words:
            return Answer(self._pick(self._ok)) if said and all(w in self._acks for w in said) else None
        if all(w in self._thanks for w in words) and any(w.startswith(self._roots) for w in words):
            return Answer(self._pick(self._welcome))
        if all(w in self._acks for w in words):
            return Answer(self._pick(self._ok))
        return None

    def _is_compliment(self, said) -> bool:
        """"you are so awesome", "good job jarvis", "you're the best": the
        whole sentence is praise -- "you are wrong" or "you are so awesome,
        open notepad" are not."""
        words = [w for w in said if w not in self._intensifiers and w not in ("oh", "wow", "hey", "and", "just", "honestly", "man", "mate")]
        # "great work today", "good job tonight": time words at the end change nothing.
        while words and (words[-1] in self._padding or words[-1] in self._how_tail) and words[-1] not in self._praise:
            words = words[:-1]
        if " ".join(words) in self._compliments:
            return True
        for start in self._compliment_starts:
            rest = words[len(start):]
            if words[: len(start)] == start and rest and all(w in self._praise for w in rest) and any(
                    w not in ("the", "a", "an") for w in rest):
                return True
        return False


class GreetingMatcher:
    """F2: answers 'hello', 'good morning' and similar greetings locally, by the time of day."""
    def __init__(self, phrases: Optional[Iterable[str]] = None, now: Callable[[], datetime] = datetime.now) -> None:
        self._phrases = {p.strip().lower() for p in (phrases or _CFG["phrases"])}
        self._now = now

    def __call__(self, normalized_text: str) -> Optional[Answer]:
        # Her name in any spelling ("hello javis") is not part of the greeting.
        text = strip_name(normalize(normalized_text).split())
        # Words
        # after the greeting that change nothing.
        while len(text) > 1 and " ".join(text) not in self._phrases and text[-1] in _GREETING_TAIL:
            text = strip_name(text[:-1])
        if " ".join(text) not in self._phrases:
            return None
        period = period_of_day(self._now().hour, int(_CFG["morning_ends_hour"]), int(_CFG["afternoon_ends_hour"]))
        return Answer(str(_CFG["reply"]).format(period=period))
