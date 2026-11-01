"""F16 Stage 1: which questions need live information,
decided locally and deterministically -- never by the model -- and the
known-mishearing fixes applied before a question goes online.

A question is "live" if it contains a [web] live_words or time_words entry
(whole words), or a year from this year on. Everything else gets the plain
answer: no search tool, about a fifth of the tokens. Time, date and
weather never get here (their own local matchers come first).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Callable, Iterable, List, Optional, Tuple

from jarvis.settings import SETTINGS

_WEB = SETTINGS["web"]
_YEAR = re.compile(r"\b(20\d\d)\b")


def _phrase_pattern(phrases: Iterable[str]) -> Optional[re.Pattern]:
    words = sorted({" ".join(str(p).lower().split()) for p in phrases if str(p).strip()}, key=len, reverse=True)
    if not words:
        return None
    return re.compile(r"(?<![\w'])(?:" + "|".join(re.escape(w).replace(r"\ ", r"\s+") for w in words) + r")(?![\w'])")


_LIVE = _phrase_pattern([*_WEB["live_words"], *_WEB["time_words"]])


def needs_live_info(question: str, today: Callable[[], date] = date.today) -> bool:
    """True if `question` needs live web information (news, prices, scores, or this year or later)."""
    text = question.lower()
    if _LIVE is not None and _LIVE.search(text):
        return True
    return any(int(y) >= today().year for y in _YEAR.findall(text))


def _corrections() -> List[Tuple[re.Pattern, str]]:
    pairs = sorted(((str(k).lower(), str(v)) for k, v in _WEB.get("corrections", {}).items()), key=lambda kv: len(kv[0]), reverse=True)
    return [(re.compile(r"(?<![\w'])" + re.escape(k).replace(r"\ ", r"\s+") + r"(?![\w'])", re.IGNORECASE), v) for k, v in pairs]


_CORRECTIONS = _corrections()


def fix_mishearings(question: str) -> Tuple[str, List[Tuple[str, str]]]:
    """The question with [web.corrections] applied (longest first, each
    once), and what was changed, as (heard, sent) pairs for the log."""
    changes: List[Tuple[str, str]] = []
    text = question
    for pattern, replacement in _CORRECTIONS:
        match = pattern.search(text)
        if match is None:
            continue
        changes.append((match.group(0), replacement))
        text = pattern.sub(replacement, text)
    return text, changes


_RECENCY = _phrase_pattern(_WEB["recency_words"])


def needs_recency(question: str) -> bool:
    """A live question that wants the newest result ("latest", "score",
    "news", ...): [web] recency_words, whole words."""
    return _RECENCY is not None and _RECENCY.search(question.lower()) is not None


def spoken_date(when: date) -> str:
    """"1 March 2026"."""
    return f"{when.day} {when.strftime('%B %Y')}"


def with_date_note(question: str, today: date) -> str:
    """What is sent for a live question: the developer's words
    plus today's local date, added by JARVIS, never by the model -- and for
    a recency question, "most recent as of <date>"."""
    template = _WEB["recency_note"] if needs_recency(question) else _WEB["date_note"]
    return str(template).format(question=question, date=spoken_date(today))


def date_sentence(question: str, when: Optional[date], today: date, answer: str) -> Optional[str]:
    """The sentence about the result's date, said after a live answer:
    - no date on the page: say so;
    - a recency question whose result is older than [web] stale_days:
      "The most recent result I found is from <date>.";
    - otherwise "That result is from <date>.", unless the answer already
      gives that day and month."""
    if when is None:
        return str(_WEB["no_date_text"])
    if needs_recency(question) and (today - when).days > int(_WEB["stale_days"]):
        return str(_WEB["stale_result_text"]).format(date=spoken_date(when))
    lowered = answer.lower()
    month = when.strftime("%B").lower()
    if re.search(rf"\b{when.day}(?:st|nd|rd|th)?\b", lowered) and (month in lowered or re.search(rf"\b{month[:3]}\b", lowered)):
        return None  # the answer already says it
    return str(_WEB["result_date_text"]).format(date=spoken_date(when))
