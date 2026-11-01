"""F14 mood: how upset the developer sounds, 0 to 1, from his words only,
on the laptop (R2) -- a weighted word/phrase list in settings.toml [mood],
with negation and intensity handling. No model, no network, no package.

What it may do (spec F14):
- After she has finished a request, and only when the score is at least
  [mood] threshold, she asks -- never tells -- with one short line from
  [mood] check_in_pool. At most once per [mood] cooldown_seconds.
- If he answers that he is fine, she doesn't ask again this session.
- If he says he is tired or stressed, her online answers are asked to be
  shorter and gentler for the rest of the session.
- The guess never changes which action she takes, and is never saved: it
  lives in this object only, and its Live Activity lines are session-only
  (never written to the debug log).
- Rudeness (F2) wins: a rude command gets the rude reply, never a check-in.
  The orchestrator doesn't score a rude command at all.

The score is a guess (R19). The numbers were set by testing 30 labelled
sentences (tests/test_f14_mood.py); voice loudness
is not used, because it hasn't been tested to work (spec: "if testing
shows it works").
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass
from typing import Callable, Iterable, List, Mapping, Optional, Sequence, Tuple

_WORD = re.compile(r"[a-z]+(?:'[a-z]+)?|!")


def _words(text: str) -> List[str]:
    # Curly apostrophes (typed or from Whisper) count as straight ones.
    return _WORD.findall(text.lower().replace("’", "'"))


@dataclass(frozen=True)
class MoodReading:
    """F14: a frustration score for one sentence and the cue words that produced it."""
    score: float  # 0 calm .. 1 very upset
    matched: Tuple[str, ...] = ()  # the cues that counted, for tests and tuning


class MoodScorer:
    """Noisy-OR of the matched cues: each cue alone gives its weight, and
    every extra cue pushes the score further toward 1 without passing it.

    - Cues are matched as whole words, longest phrase first, never
      overlapping ("what the hell" counts once, not also as "the hell").
    - A negation within `negation_window` words before a cue scales it by
      `negation_factor` ("I'm not frustrated").
    - An intensifier right before a cue multiplies it by `intensity_boost`
      ("so annoying"), never past `max_cue_weight`.
    - Any positive word ("thanks", "haha", "impressive") scales the total
      by `positive_damping`: swearing in delight isn't anger.
    - Each "!" adds `exclaim_boost` to a non-zero score, up to `exclaim_max`.
    """

    def __init__(
        self,
        weights: Mapping[str, float],
        intensifiers: Iterable[str] = (),
        negations: Iterable[str] = (),
        positives: Iterable[str] = (),
        intensity_boost: float = 1.0,
        negation_factor: float = 1.0,
        negation_window: int = 2,
        positive_damping: float = 1.0,
        exclaim_boost: float = 0.0,
        exclaim_max: int = 0,
        max_cue_weight: float = 1.0,
        sweeping: Iterable[str] = (),
        sweeping_boost: float = 1.0,
        sweeping_window: int = 3,
    ) -> None:
        cues = [(tuple(_words(str(phrase))), float(w)) for phrase, w in weights.items()]
        self._cues = sorted((c for c in cues if c[0]), key=lambda c: len(c[0]), reverse=True)
        self._intensifiers = {w.lower() for w in intensifiers}
        self._negations = {w.lower() for w in negations}
        self._positives = [tuple(_words(p)) for p in positives if _words(p)]
        self._intensity_boost = intensity_boost
        self._negation_factor = negation_factor
        self._negation_window = negation_window
        self._positive_damping = positive_damping
        self._exclaim_boost = exclaim_boost
        self._exclaim_max = exclaim_max
        self._max_cue_weight = max_cue_weight
        # a sweeping word shortly before a cue ("everything ... broken",
        # "nothing ... works", "keeps crashing") makes it stronger.
        self._sweeping = {w.lower() for w in sweeping}
        self._sweeping_boost = sweeping_boost
        self._sweeping_window = sweeping_window

    def score(self, text: str) -> MoodReading:
        """Score `text`: cue words and phrases, boosted by intensifiers, softened by negation. Returns a
        MoodReading."""
        tokens = _words(text)
        words = [t for t in tokens if t != "!"]
        used = [False] * len(words)
        remaining = 1.0
        matched: List[str] = []
        for phrase, weight in self._cues:
            n = len(phrase)
            for i in range(len(words) - n + 1):
                if tuple(words[i : i + n]) != phrase or any(used[i : i + n]):
                    continue
                for j in range(i, i + n):
                    used[j] = True
                before = words[max(0, i - self._negation_window) : i]
                w = weight
                if any(b in self._negations for b in before):
                    w *= self._negation_factor
                elif i > 0 and words[i - 1] in self._intensifiers:
                    w = min(self._max_cue_weight, w * self._intensity_boost)
                if self._sweeping and any(b in self._sweeping for b in words[max(0, i - self._sweeping_window) : i])                         and not any(b in self._negations for b in before):
                    w = min(self._max_cue_weight, w * self._sweeping_boost)
                remaining *= 1.0 - w
                matched.append(" ".join(phrase))
        score = 1.0 - remaining
        if score > 0 and any(self._contains(words, p) for p in self._positives):
            score *= self._positive_damping
        if score > 0:
            score += self._exclaim_boost * min(tokens.count("!"), self._exclaim_max)
        return MoodReading(round(min(score, 1.0), 2), tuple(matched))

    @staticmethod
    def _contains(words: Sequence[str], phrase: Tuple[str, ...]) -> bool:
        n = len(phrase)
        return any(tuple(words[i : i + n]) == phrase for i in range(len(words) - n + 1))


def scorer_from_settings(cfg: Mapping) -> MoodScorer:
    """A MoodScorer built from the [mood] settings."""
    return MoodScorer(
        weights=cfg["weights"],
        intensifiers=cfg["intensifiers"],
        negations=cfg["negations"],
        positives=cfg["positives"],
        intensity_boost=float(cfg["intensity_boost"]),
        negation_factor=float(cfg["negation_factor"]),
        negation_window=int(cfg["negation_window"]),
        positive_damping=float(cfg["positive_damping"]),
        exclaim_boost=float(cfg["exclaim_boost"]),
        exclaim_max=int(cfg["exclaim_max"]),
        max_cue_weight=float(cfg["max_cue_weight"]),
        sweeping=cfg.get("sweeping", ()),
        sweeping_boost=float(cfg.get("sweeping_boost", 1.0)),
        sweeping_window=int(cfg.get("sweeping_window", 3)),
    )


@dataclass(frozen=True)
class MoodNote:
    """What the orchestrator should do for one command."""

    score: float
    noticed: Optional[str] = None  # the Live Activity line, when at or over the threshold
    check_in: Optional[str] = None  # the line to say after the answer, if she should ask now


class MoodWatcher:
    """One per session, held by the orchestrator. Nothing here is written
    anywhere: it is gone when JARVIS closes (F10: the mood guess is session
    data)."""

    def __init__(
        self,
        cfg: Mapping,
        clock: Callable[[], float] = time.monotonic,
        choose: Callable[[Sequence[str]], str] = random.choice,
    ) -> None:
        self._cfg = cfg
        self._scorer = scorer_from_settings(cfg)
        self._clock = clock
        self._choose = choose
        self._last_check_in: Optional[float] = None
        self._last_line: Optional[str] = None
        self._awaiting_reply = False  # the next command may answer the check-in
        self._said_fine = False  # "I'm fine": no more check-ins this session
        self.gentle = False  # he said he is tired or stressed

    @property
    def enabled(self) -> bool:
        """True if mood check-ins are switched on in settings."""
        return bool(self._cfg["enabled"])

    @property
    def threshold(self) -> float:
        """The score above which a sentence counts as frustrated."""
        return float(self._cfg["threshold"])

    def score(self, text: str) -> MoodReading:
        """Score one sentence (words only, never voice loudness)."""
        return self._scorer.score(text)

    def reply_to_check_in(self, text: str) -> Optional[str]:
        """Right after a check-in, "I'm fine" gets a short local reply and
        ends check-ins for the session. Anything else is a normal command.
        Only the one command straight after the check-in counts."""
        if not self._awaiting_reply:
            return None
        self._awaiting_reply = False
        if _has_phrase(text, self._cfg["fine_phrases"]):
            self._said_fine = True
            return str(self._cfg["fine_reply"])
        return None

    def hear(self, text: str) -> MoodNote:
        """Score one (non-rude) command and decide whether she may ask
        after it. The cooldown starts only when the check-in is really
        said (checked_in), so a stopped command doesn't use it up."""
        if not self.enabled:
            return MoodNote(0.0)
        if _has_phrase(text, self._cfg["gentle_triggers"]):
            self.gentle = True
        reading = self._scorer.score(text)
        if reading.score < self.threshold:
            return MoodNote(reading.score)
        noticed = str(self._cfg["noticed_text"]).format(score=reading.score)
        if self._said_fine:
            return MoodNote(reading.score, noticed)
        cooldown = float(self._cfg["cooldown_seconds"])
        if self._last_check_in is not None and self._clock() - self._last_check_in < cooldown:
            return MoodNote(reading.score, noticed)
        # Only lines that end as a question -- she asks, never tells.
        asks = [str(line) for line in self._cfg["check_in_pool"] if str(line).rstrip().endswith("?")]
        pool = [line for line in asks if line != self._last_line] or asks
        return MoodNote(reading.score, noticed, self._choose(pool) if pool else None)

    def checked_in(self, line: str) -> None:
        """She has just said `line`: start the cooldown, listen for "I'm fine"."""
        self._last_check_in = self._clock()
        self._last_line = line
        self._awaiting_reply = True

    def prompt_hint(self) -> Optional[str]:
        """Extra system-prompt line for online answers while he's tired or
        stressed; None otherwise."""
        return str(self._cfg["gentle_prompt"]) if self.gentle and self.enabled else None


def _has_phrase(text: str, phrases: Iterable[str]) -> bool:
    words = _words(text)
    return any(MoodScorer._contains(words, tuple(_words(p))) for p in phrases if _words(p))
