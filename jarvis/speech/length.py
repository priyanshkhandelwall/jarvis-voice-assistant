"""How long an online answer may be ("Can you
explain black hole in one line" got three sentences).

- "in one line", "briefly", "in short", "one sentence" ... -> exactly one
  sentence;
- "in detail", "explain fully" ... -> up to [length] detail_sentences.

The words are [length] in settings. The model is told the limit (a line
added to that one request's system prompt) and JARVIS enforces it on the
answer itself, so a longer reply is cut to whole sentences, never
mid-sentence.

Pure text, no I/O.
"""

from __future__ import annotations

import re
from typing import Optional

from jarvis.settings import SETTINGS
from jarvis.speech.tts import split_sentences

_CFG = SETTINGS["length"]


def _pattern(phrases) -> re.Pattern:
    words = sorted((" ".join(str(p).lower().split()) for p in phrases), key=len, reverse=True)
    return re.compile(r"(?<![a-z])(?:" + "|".join(re.escape(w).replace(r"\ ", r"\s+") for w in words) + r")(?![a-z])")


_SHORT = _pattern(_CFG["one_sentence_phrases"])
_DETAIL = _pattern(_CFG["detail_phrases"])


def sentence_limit(question: str) -> Optional[int]:
    """1 for "in one line" / "briefly", [length] detail_sentences for "in
    detail", None when he didn't ask for a length."""
    lowered = " ".join(question.lower().replace("’", "'").split())
    if _SHORT.search(lowered) or _fuzzy(lowered, ("one", "a", "1", "single"), _CFG["short_words"]):
        return 1
    if _DETAIL.search(lowered) or _fuzzy(lowered, None, _CFG["detail_words"]):
        return int(_CFG["detail_sentences"])
    return None


def _fuzzy(lowered: str, counts, targets) -> bool:
    """("What is Machine Learning in One Life" got three
    sentences; "Explain how well can work and detail"): after "in" (or
    Whisper's "and"/"an"), a word one letter off a [length] short_words /
    detail_words target counts -- "in one life", "in one lie", "and detail".
    Only after "in", so "what is a life" is never a length request."""
    from jarvis.actions.command_text import edit_distance

    words = re.findall(r"[a-z0-9]+", lowered)
    step = 2 if counts else 1
    for i, w in enumerate(words[:-step]):
        if w not in ("in", "and", "an", "on"):
            continue
        if counts and words[i + 1] not in counts:
            continue
        said = words[i + step]
        if any(edit_distance(said, str(t)) <= (1 if len(str(t)) <= 6 else 2) for t in targets):
            return True
    return False


def instruction(limit: int) -> str:
    """The line added to that request's system prompt."""
    if limit == 1:
        return str(_CFG["one_sentence_instruction"])
    return str(_CFG["detail_instruction"]).format(count=limit)


def limit_sentences(text: str, limit: Optional[int]) -> str:
    """At most `limit` whole sentences of `text` (all of it when None)."""
    if not limit:
        return text
    sentences = split_sentences(text)
    if len(sentences) <= limit:
        return text
    return " ".join(s.strip() for s in sentences[:limit])
