"""F2 rudeness check: a plain word/phrase list (decision #10), not a model.

She says it wasn't a nice way to say it and **still does what was asked**
(F2). To do what was asked, the insult has to come out of the command
first -- "open chrome you idiot" must reach the interpreter as "open
chrome", or F3's matcher would search for an app called "chrome you".

Matching is whole-word and case-insensitive: "skillful" never matches
"kill", "class" never matches "ass". A leading "you" / "you're a" in front
of an insult is removed with it ("you idiot", "you're a moron").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

_LEADING_JOINERS = re.compile(r"^(?:and|then|so|now)\b\s*", re.IGNORECASE)
_EDGE_PUNCTUATION = " \t,;:.!?-"


@dataclass(frozen=True)
class RudenessCheck:
    """The result of checking a sentence: rude or not, the command left after the insult is removed, and
    the words found."""
    rude: bool
    cleaned: str  # the command with the rude words removed
    matched: Tuple[str, ...] = ()


class RudenessFilter:
    """F14/decision G: spots insults (whole words and swear roots). She says it wasn't nice, then still
    does what was asked."""
    def __init__(self, words: Iterable[str], roots: Optional[Iterable[str]] = None,
                 nothing_asked_words: Optional[Iterable[str]] = None) -> None:
        """`roots` / `nothing_asked_words` default to [rudeness] roots and
        nothing_asked_words."""
        from jarvis.settings import SETTINGS

        cfg = SETTINGS["rudeness"]
        roots = [str(r).lower() for r in (cfg.get("roots", []) if roots is None else roots) if str(r).strip()]
        self._root_pattern = (re.compile(r"(?<![\w'])[\w']*(?:" + "|".join(re.escape(r) for r in roots) + r")[\w']*(?![\w'])",
                                         re.IGNORECASE) if roots else None)
        self._root_exceptions = {str(w).lower() for w in cfg.get("root_exceptions", [])}
        self._praise_swears = {str(w).lower() for w in cfg.get("praise_intensifiers", [])}
        small = SETTINGS["small_talk"]
        self._praise = {str(w).lower() for w in small.get("praise_words", [])}
        self._softeners = {str(w).lower() for w in small.get("intensifiers", [])}
        self._nothing_words = {str(w).lower() for w in (cfg.get("nothing_asked_words", []) if nothing_asked_words is None
                                                         else nothing_asked_words)}
        # Longest first, so "dumbass" wins over "dumb".
        terms = sorted({w.strip().lower() for w in words if w.strip()}, key=len, reverse=True)
        self._pattern = None
        if terms:
            alternatives = "|".join(r"\s+".join(re.escape(part) for part in term.split()) for term in terms)
            insult_run = rf"(?:{alternatives})(?:\s+(?:{alternatives}))*"
            # "your"/"ur": Whisper's spelling of "you're" (
            # "Your useless open notepad" left "Your open notepad" behind).
            # "You are absolutely useless, you idiot"
            # left "You are absolutely" behind, which went online -- an
            # intensifier may sit between "you are" and the insult.
            you_prefix = (r"(?:(?:you(?:\s+are|\s*'re|\s+re|r)?|ur)(?:\s+(?:so|such|absolutely|totally|really|completely|utterly|"
                          r"very|truly|just|literally|an?))*\s+)?")
            self._pattern = re.compile(rf"(?<![\w']){you_prefix}{insult_run}(?![\w'])", re.IGNORECASE)
            self._term_pattern = re.compile(rf"(?<![\w'])(?:{alternatives})(?![\w'])", re.IGNORECASE)

    def check(self, text: str) -> RudenessCheck:
        """Check `text`. Returns a RudenessCheck; `cleaned` is empty when nothing was asked."""
        if self._pattern is None:
            return RudenessCheck(False, text.strip())
        matched = tuple(m.group(0).lower() for m in self._term_pattern.finditer(text))
        rooted = tuple(m.group(0).lower() for m in self._root_pattern.finditer(text)
                       if m.group(0).lower() not in self._root_exceptions) if self._root_pattern else ()
        if not matched and not rooted:
            return RudenessCheck(False, text.strip())
        matched = matched + tuple(w for w in rooted if w not in matched)
        if self._only_praising(text, matched):
            return RudenessCheck(False, text.strip())
        cleaned = self._pattern.sub(" ", text)
        if self._root_pattern is not None:
            cleaned = self._root_pattern.sub(lambda m: m.group(0) if m.group(0).lower() in self._root_exceptions else " ",
                                             cleaned)  # words containing a root
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(_EDGE_PUNCTUATION)
        cleaned = _LEADING_JOINERS.sub("", cleaned).strip(_EDGE_PUNCTUATION)
        # "what the you, ai" is nothing asked -- never sent online.
        left = re.findall(r"[a-z']+", cleaned.lower())
        if all(w in self._nothing_words for w in left) or not asks_something(cleaned):
            cleaned = ""
        return RudenessCheck(True, cleaned, matched)


    def _only_praising(self, text: str, matched) -> bool:
        """A swear used as an intensifier before praise ("that's damn great"): every rude word
        is a swear intensifier, and each one leads (past other intensifiers)
        to a praise word."""
        if not matched or not all(m in self._praise_swears for m in matched):
            return False
        words = re.findall(r"[a-z']+", text.lower())
        for i, w in enumerate(words):
            if w in self._praise_swears:
                rest = [x for x in words[i + 1:] if x not in self._softeners]
                if not rest or rest[0] not in self._praise:
                    return False
        return True


# A question: a "?" or a clause opening with a question word or an auxiliary.
_QUESTION = re.compile(r"\?|(?:^|[,.;:!]\s*)(?:what|what's|whats|who|whose|when|where|why|how|which|is|are|am|was|were|do|does|did|"
                       r"can|could|would|will|should|shall|may|might|have|has|tell me|explain|give me|show me|let me)\b",
                       re.IGNORECASE)


def asks_something(text: str) -> bool:
    """("No, I was just joking apart, you are idiot,
    stupid" -> the insults removed, "No, I was just joking apart" went
    online): what is left after an insult is sent on only if it asks for
    something -- a command (a command verb opening a clause, as the
    interpreter finds them) or a question. Otherwise nothing was asked."""
    from jarvis.actions.command_text import extract_command, known_names

    if not text.strip():
        return False
    bare = " ".join(w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in ("please", "now", "just", "the", "my"))
    return (extract_command(text) is not None or _QUESTION.search(text) is not None or bare in known_names()
            or _router().looks_local(text))  # a cue word of any local intent ("introduce yourself")


_ROUTER = []


def _router():
    from jarvis.router.router import IntentRouter

    if not _ROUTER:
        _ROUTER.append(IntentRouter())
    return _ROUTER[0]
