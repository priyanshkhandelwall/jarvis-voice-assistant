"""The general intent router.

JARVIS kept failing because each command was a fixed phrase list: anything
said differently fell through to Groq, which can only talk. The router sits
after every fixed matcher and before the online conversation:

1. The fixed local matchers run first, as before (interpreter order).
2. This router scores what is left against EVERY local intent in
   jarvis/settings/intents.toml, offline: cue phrases (Whisper's mishearings
   tolerated by edit distance and a mishearing table), then a slot from the
   leftover words (an app name, a memory subject, a number, an object, a
   city). One clear winner is rewritten into its template -- a command the
   fixed matchers already understand -- and run through the normal pipeline
   (controller, policy, trust levels). Two close: "Did you mean A or B?".
3. Only if that finds nothing and the words look like a local command at
   all, an online CLASSIFIER (never an answerer) may name the intent: his
   words and the intent list go out, strict JSON comes back, is validated,
   and is run locally exactly like step 2. The model never assigns trust or
   runs anything.
4. Everything else ("general_question") goes to the normal online answer.

Live Activity shows "understood as: <intent>" and never names the service
(R3). No personal data and no memory contents are ever sent to classify.
"""

from __future__ import annotations

import json
import re
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from jarvis.actions.command_text import edit_distance, is_name, pick_index
from jarvis.interpreter import Answer

INTENTS_PATH = Path(__file__).resolve().parents[1] / "settings" / "intents.toml"
GENERAL = "general_question"

Phrase = Tuple[str, ...]


@dataclass(frozen=True)
class Intent:
    """One local intent from intents.toml: its cue groups, vetoes, slot kind and the command template it
    becomes."""
    name: str
    say: str
    description: str
    need: Tuple[Tuple[Phrase, ...], ...]
    also: Tuple[Tuple[Phrase, ...], ...]
    avoid: Tuple[Phrase, ...]
    slot: str
    slot_required: bool
    template: str
    base: float
    at_start: bool = False  # the first need group must open the sentence (an order, not a question)
    no_statements: bool = False  # ignored when a copula comes before the cue ("the TV is on mute")
    about_me: bool = False  # only about her: "you"/"your" or her name is said ("what does JARVIS stand for")


@dataclass(frozen=True)
class Route:
    """One scored match of an utterance against an intent, with the slot and the rewritten command."""
    intent: Intent
    score: float
    slot: str
    command: str


def _phrase(text: str) -> Phrase:
    return tuple(re.findall(r"[a-z0-9']+", str(text).lower().replace("’", "'")))


def _groups(raw) -> Tuple[Tuple[Phrase, ...], ...]:
    return tuple(tuple(sorted((_phrase(p) for p in group if _phrase(p)), key=len, reverse=True)) for group in raw or [])


def load_intents(path: Optional[Path] = None) -> Tuple[dict, List[Intent]]:
    """Read intents.toml. Returns ([router] settings, the list of Intents), cue phrases sorted longest
    first."""
    with open(path or INTENTS_PATH, "rb") as f:
        data = tomllib.load(f)
    intents = [
        Intent(
            name=str(i["name"]), say=str(i.get("say", i["name"])), description=str(i.get("description", "")),
            need=_groups(i.get("need")), also=_groups(i.get("also")),
            avoid=tuple(sorted((_phrase(p) for p in i.get("avoid", []) if _phrase(p)), key=len, reverse=True)),
            slot=str(i.get("slot", "")), slot_required=bool(i.get("slot_required", False)), template=str(i["template"]),
            base=float(i.get("base", 0.6)), at_start=bool(i.get("at_start", False)),
            no_statements=bool(i.get("no_statements", False)), about_me=bool(i.get("about_me", False)),
        )
        for i in data.get("intent", [])
    ]
    return data.get("router", {}), intents


_CFG, _INTENTS = load_intents()
# (privacy; "...tell me what photos is in detail" ->
# "understood as: camera look" and the camera opened): the online classifier
# may never choose a camera intent. The camera opens only from his own words
# matched locally (see / look / camera / in front of you).
NEVER_CLASSIFIED = frozenset(str(n) for n in _CFG.get("never_classified", []))
CLASSIFIER_INTENTS = [(i.name, i.slot, i.description) for i in _INTENTS if i.name not in NEVER_CLASSIFIED]
# "what is photosynthesis", "<anything> in detail": a question, never a
# command -- the classifier is not asked.
_PLAIN_QUESTION = re.compile(r"^(?:(?:so|now|ok|okay|and|then|please|jarvis|can|could|would|will|you|tell|me|explain)\s+)*"
                             r"(?:what|who|why)\s+(?:is|are|was|were)\b|\bin\s+(?:more\s+)?details?$|\bin\s+depth$")


# -- words -----------------------------------------------------------------------------------


def unglue(words: List[str]) -> List[str]:
    """"trackingindian" -> "tracking", "indian" ([router] glued_prefixes)."""
    out: List[str] = []
    for w in words:
        prefix = next((p for p in _GLUED if w.startswith(p) and len(w) >= len(p) + 3), None)
        out.extend([prefix, w[len(prefix):]] if prefix else [w])
    return out


def heard_as(text: str) -> str:
    """His words with Whisper's slips fixed (glued words split, the
    mishearing table applied) -- her name and "'s" kept, for matchers that
    compare whole phrases (F20's "what am i tracking")."""
    words = unglue(re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text.lower().replace("’", "'")))
    out: List[str] = []
    i = 0
    while i < len(words):
        pair = " ".join(words[i:i + 2])
        if len(words) > i + 1 and pair in _MISHEARD:
            out.extend(_MISHEARD[pair].split())
            i += 2
            continue
        out.extend(_MISHEARD.get(words[i], words[i]).split())
        i += 1
    return " ".join(out)


def words_of(text: str, mishearings: Optional[Dict[str, str]] = None) -> List[str]:
    """Lower-case words; "what's" -> "what", her name dropped, Whisper's
    spellings fixed (one or two words: "wi fi" -> "wifi", "venn" -> "when")."""
    lowered = text.lower().replace("’", "'")
    raw = unglue(re.findall(r"[a-z0-9]+(?:'[a-z]+)?", lowered))
    words = []
    for w in raw:
        if w.endswith("'s"):
            w = w[:-2]
        if w and not is_name(w):
            words.append(w)
    fixes = mishearings if mishearings is not None else _MISHEARD
    out: List[str] = []
    i = 0
    while i < len(words):
        pair = " ".join(words[i:i + 2])
        if len(words) > i + 1 and pair in fixes:
            out.extend(fixes[pair].split())
            i += 2
            continue
        out.extend(fixes.get(words[i], words[i]).split())
        i += 1
    return out


_MISHEARD = {" ".join(_phrase(k)): str(v) for k, v in _CFG.get("mishearings", {}).items()}
_GLUED = sorted((str(p).lower() for p in _CFG.get("glued_prefixes", [])), key=len, reverse=True)
_STOP = {str(w) for w in _CFG.get("stop_words", [])}
_COMMON = {str(w) for w in _CFG.get("common_words", [])}
_ABOUT_ME = {"you", "your", "yourself", "u", "ur"}
_STATEMENT = {str(w) for w in _CFG.get("statement_words", [])}
_QUESTION_STARTS = [" ".join(_phrase(q)) for q in _CFG.get("question_starts", [])]


def same_word(said: str, cue: str) -> bool:
    """Equal, or a Whisper slip: one letter off for 5-7 letter cues, two for
    longer ones. Short cues must match exactly ("on" is never "in"). A slip
    keeps the first letter ("brigthness", "volumn"): "introduce
    yourself to my friends here" ran as a memory recall because "here" was
    one letter off "where"."""
    if said == cue:
        return True
    if len(cue) < 5 or len(said) < 4 or abs(len(said) - len(cue)) > 2 or said[0] != cue[0]:
        return False
    return edit_distance(said, cue) <= (2 if len(cue) >= 8 else 1)


def _find(words: Sequence[str], phrase: Phrase, used: set) -> Optional[range]:
    n = len(phrase)
    for i in range(len(words) - n + 1):
        span = range(i, i + n)
        if any(j in used for j in span):
            continue
        if all(same_word(words[i + k], phrase[k]) for k in range(n)):
            return span
    return None


def _find_exact(words: Sequence[str], phrase: Phrase) -> bool:
    n = len(phrase)
    return any(tuple(words[i:i + n]) == phrase for i in range(len(words) - n + 1))


def _match_group(words, group, used) -> Optional[range]:
    """The group's phrase that appears first in the sentence (the longest
    one when two start at the same word)."""
    best = None
    for phrase in group:  # longest first
        span = _find(words, phrase, used)
        if span is not None and (best is None or span.start < best.start):
            best = span
    return best


# -- slots ------------------------------------------------------------------------------------


def _trim(words: List[str]) -> List[str]:
    while words and words[0] in _STOP:
        words = words[1:]
    while words and words[-1] in _STOP:
        words = words[:-1]
    return words


_SUBJECT_TAIL = {"due", "now", "again", "exactly", "today", "tomorrow", "on", "at", "happening", "scheduled", "going", "be"}


def _number_slot(words: List[str]) -> str:
    from jarvis.system.commands import to_number

    for size in (3, 2, 1):
        for i in range(len(words) - size + 1):
            chunk = " ".join(words[i:i + size])
            # A number starts and ends on a number word -- "the
            # two" (of "the sound to two hundred") was read as 2.
            if to_number(words[i]) is None or (size > 1 and words[i + size - 1] in ("a", "and", "the", "percent", "per", "cent")):
                continue
            number = to_number(chunk)
            # Up to 1000, so "the sound to 200" reaches the local
            # "Volume goes from 0 to 100." instead of going online.
            if number is not None and 0 <= number <= 1000 and chunk not in ("a", "one"):
                return str(number)
    return ""


def _object_slot(text: str) -> str:
    from jarvis.vision.commands import _LABELS_LONGEST, _SYNONYMS

    sentence = " ".join(words_of(text))
    if re.search(r"\b(?:person|people|man|woman|guy|girl|boy|him|her)\b", sentence):
        return "person"
    for name in _LABELS_LONGEST:
        if re.search(rf"\b{re.escape(name)}(?:s|es)?\b", sentence):
            return _SYNONYMS.get(name, name)
    return ""


def _city_slot(words: List[str]) -> str:
    for i, w in enumerate(words):
        if w in ("in", "for", "at") and i + 1 < len(words):
            rest = [x for x in words[i + 1:] if x not in ("today", "tomorrow", "tonight", "now", "this", "week", "outside")]
            if rest:
                return " ".join(rest)
    return ""


# -- the router ------------------------------------------------------------------------------


class IntentRouter:
    """Scores an utterance against every local intent, offline (cue words with Whisper's slips tolerated,
    then a slot)."""
    def __init__(self, intents: Optional[List[Intent]] = None, cfg: Optional[dict] = None) -> None:
        self.intents = intents if intents is not None else list(_INTENTS)
        self.cfg = cfg if cfg is not None else dict(_CFG)
        self._distinctive = {w for i in self.intents for g in i.need for p in g for w in p} - _COMMON - _STOP

    def scores(self, text: str, about_me_only: bool = False) -> List[Route]:
        """Every intent that matches `text`, best score first. `about_me_only` limits it to the questions
        about her."""
        words = words_of(text)
        intents = [i for i in self.intents if i.about_me] if about_me_only else self.intents
        routes = [r for r in (self._score(i, words, text) for i in intents) if r is not None]
        return sorted(routes, key=lambda r: r.score, reverse=True)

    def is_question(self, text: str) -> bool:
        """"how do I turn the volume up": a question about doing it, never an order."""
        said = " ".join(words_of(text))
        return any(said == q or said.startswith(q + " ") for q in _QUESTION_STARTS)

    @staticmethod
    def is_plain_question(text: str) -> bool:
        """"what is <word>", "<word> in detail": asks for
        information, so the online classifier is never asked about it."""
        return _PLAIN_QUESTION.search(" ".join(words_of(text))) is not None

    def ruled_out(self, intent: Intent, text: str) -> bool:
        """The local vetoes, for a label the online classifier chose: a
        how-to question, an avoided phrase, a statement before a state
        change, a verb that doesn't open the sentence. "I need
        some sleep tonight" was scored "none" here (sleep avoids "i need"),
        then classified online as sleep, and she went to sleep."""
        if self.is_question(text):
            return True
        words = words_of(text)
        if any(_find_exact(words, p) for p in intent.avoid):
            return True
        span = _match_group(words, intent.need[0], set()) if intent.need else None
        if span is not None:
            if intent.at_start and any(w not in _STOP for w in words[: span.start]):
                return True
            if intent.no_statements and any(w in _STATEMENT for w in words[: span.start]):
                return True
        return False

    def has_cue(self, intent: Intent, text: str) -> bool:
        """Any of the intent's own cue words (its first need group) in his
        words. Without one, an online classification is only
        asked about, never run ("Have a look, key." was classified as
        "open key" and opened the On-Screen Keyboard)."""
        return not intent.need or _match_group(words_of(text), intent.need[0], set()) is not None

    def looks_local(self, text: str) -> bool:
        """Any cue word of a local intent (not "what", "my", ...): only then
        is the online classifier worth a call."""
        return any(same_word(w, cue) for w in words_of(text) for cue in self._distinctive)

    def decide(self, text: str, about_me_only: bool = False) -> Tuple[str, List[Route]]:
        """("run", [route]) | ("ask", [one or two routes]) | ("none", []).
        `about_me_only`: only the questions about her, on his words as said
        "what can you do" is cleaned to "what do")."""
        if self.is_question(text):
            return "none", []
        routes = self.scores(text, about_me_only)
        if not routes:
            return "none", []
        run, ask, margin = float(self.cfg["run_score"]), float(self.cfg["ask_score"]), float(self.cfg["margin"])
        best = routes[0]
        close = [r for r in routes[1:] if r.score >= ask and best.score - r.score < margin and r.command != best.command]
        if best.score >= run and not close:
            return "run", [best]
        if best.score >= ask:
            return "ask", [best] + close[:1]
        return "none", []

    def _score(self, intent: Intent, words: List[str], text: str) -> Optional[Route]:
        used: set = set()
        if any(_find_exact(words, p) for p in intent.avoid):
            return None  # exact: "time" must not count as the avoided "times"
        if intent.about_me and not (_ABOUT_ME & set(words) or any(is_name(w) for w in re.findall(r"[a-z]+", text.lower()))):
            return None  # "what does NASA stand for" is not about her
        for n, group in enumerate(intent.need):
            span = _match_group(words, group, used)
            if span is None and n > 0:
                # One word may satisfy two groups when it is listed in both
                # ("make it louder": "louder" is both the thing and the direction).
                shared = tuple(p for p in group if all(p in g for g in intent.need[:n]))
                span = _match_group(words, shared, set())
            if span is None:
                return None
            if n == 0 and intent.at_start and any(w not in _STOP for w in words[: span.start]):
                return None  # "when should I start revising": the verb isn't an order here
            if n == 0 and intent.no_statements and any(w in _STATEMENT for w in words[: span.start]):
                return None  # "the TV is on mute" says something; it asks for nothing
            used.update(span)
        matched_also = 0
        for group in intent.also:
            span = _match_group(words, group, used)
            if span is not None:
                matched_also += 1
                used.update(span)
        # Why this formula: every need group matched gives the intent's base
        # (0.6 by default: under run_score, so when the intent lists optional
        # context and none of it is there, she asks "Did you mean ...?"
        # instead of running it); each matched `also` group
        # lifts it towards 1.0. An intent with no `also` groups is fully
        # described by its cue, so it gets a fixed +0.25 (0.85: it runs).
        score = intent.base + ((1 - intent.base) * matched_also / len(intent.also) if intent.also else 0.25)
        left = [w for j, w in enumerate(words) if j not in used]
        slot = self._slot(intent, words, used, left, text)
        if intent.slot_required and not slot:
            return None
        if intent.slot not in ("rest", "subject", "after"):
            slot_words = set(slot.split())
            extra = [w for w in left if w not in _STOP and w not in slot_words]
            score -= float(self.cfg["leftover_penalty"]) * len(extra)
        return Route(intent, round(score, 3), slot, self._command(intent, slot, words))

    def _slot(self, intent: Intent, words, used, left, text) -> str:
        kind = intent.slot
        if kind == "after":
            after = words[max(used) + 1:] if used else list(words)
            while after and after[-1] in _STOP:
                after = after[:-1]
            return " ".join(after)
        if kind == "rest":
            return " ".join(_trim([w for j, w in enumerate(words) if j not in used]))
        if kind == "subject":
            after = [w for j, w in enumerate(words) if j not in used]
            if "my" in after:
                after = after[after.index("my") + 1:]
            subject = _trim(after)
            while subject and subject[-1] in _SUBJECT_TAIL:
                subject = subject[:-1]
            if subject and subject[0] in ("is", "are", "was"):
                subject = subject[1:]
            return " ".join(subject)
        if kind == "number":
            return _number_slot(left)
        if kind == "object":
            return _object_slot(text)
        if kind == "city":
            return _city_slot(words)
        return ""

    @staticmethod
    def _command(intent: Intent, slot: str, words: List[str]) -> str:
        if intent.slot == "city":
            day = " tomorrow" if "tomorrow" in words else ""
            return intent.template.format(slot=(f" in {slot}" if slot else "") + day)
        return " ".join(intent.template.format(slot=slot).split())


# -- "Did you mean ...?" ------------------------------------------------------------------------


_YES = {"yes", "yeah", "yep", "yup", "sure", "correct", "right", "exactly", "ok", "okay", "please", "do it", "that one", "affirmative"}
_NO = {"no", "nope", "neither", "cancel", "nah", "never mind", "nevermind", "none"}


@dataclass
class Clarifier:
    """Holds the last "Did you mean ...?" for [router] clarify_seconds. Each
    option is (how she names it, the command to run or a ready request)."""

    run: Callable[[str], object]
    slot: object = None  # the orchestrator's PickSlot: the newest question or list answers
    clock: Callable[[], float] = time.monotonic
    options: List[Tuple[str, object]] = field(default_factory=list)
    asked_at: float = 0.0

    def ask(self, options: List[Tuple[str, object]]) -> Answer:
        """Ask 'Did you mean A?' or 'Did you mean A, or B?' and remember the options for the next answer."""
        self.options, self.asked_at = list(options), self.clock()
        if self.slot is not None:
            self.slot.claim(self)
        if len(options) == 1:
            return Answer(f"Did you mean {options[0][0]}?")
        return Answer(f"Did you mean {options[0][0]}, or {options[1][0]}?")

    def matcher(self, cleaned: str):
        """The answer to the last 'Did you mean ...?': yes, no, 'the first', or an option's name. None if
        no question is waiting."""
        options, self.options = self.options, []
        if not options:
            return None
        if self.slot is not None and self.slot.owned_by(self):
            self.slot.release(self)  # answered or not, this question is over (a later "1" is not for it)
        elif self.slot is not None:
            return None  # a newer list was shown since
        if self.clock() - self.asked_at > float(_CFG.get("clarify_seconds", 30)):
            return None
        said = " ".join(re.findall(r"[a-z0-9']+", cleaned.lower()))
        words = set(said.split())
        if said in _NO or (words & _NO and not words & _YES):
            return Answer("Okay.")
        number = pick_index(said)
        chosen = None
        if number is not None and 1 <= number <= len(options):
            chosen = options[number - 1]
        elif said in _YES or (words & _YES and len(words) <= 3):
            if len(options) == 1:
                chosen = options[0]
            else:
                self.options = options  # still waiting: which one?
                if self.slot is not None:
                    self.slot.claim(self)
                return Answer(f"Say the first, {options[0][0]}, or the second, {options[1][0]}.")
        else:
            hits = [o for o in options if words & (set(_phrase(o[0])) - _STOP - {"the", "your", "a"})]
            if len(hits) == 1:
                chosen = hits[0]
        if chosen is None:
            return None  # something else: a new command
        target = chosen[1]
        return self.run(target) if isinstance(target, str) else target


# -- the online classifier's answer ------------------------------------------------------------


def classifier_prompt(intents: Sequence[Intent]) -> str:
    """The system prompt for the online classifier: the intent list and the strict JSON reply it must give."""
    lines = [f"- {i.name}" + (f"({i.slot})" if i.slot else "") + f": {i.description}" for i in intents
             if i.name not in NEVER_CLASSIFIED]
    return (
        "You route ONE spoken command for a voice assistant called JARVIS that runs on a Windows laptop. "
        "Pick the one intent below that the words ask for, or general_question when they ask for information, "
        "an explanation or chat instead. The words may be misheard by speech recognition: guess what was meant. "
        "Reply with JSON only, no other text: "
        '{"intent": "<one name from the list, or general_question>", "slots": {"<slot>": "<value>"}, "confidence": <0 to 1>}. '
        "Slots: name = the app or website name; rest = the words the intent acts on; subject = the thing after 'my'; "
        "number = 0-100; object = a thing the camera can see; city = a city name. Intents:\n" + "\n".join(lines)
    )


def validate_classification(raw: str, intents: Sequence[Intent]) -> Optional[Tuple[str, Dict[str, str], float]]:
    """Strict: a JSON object with a listed intent (or general_question), a
    flat slots object of short strings/numbers, and confidence 0-1. Anything
    else is None (treated as a general question)."""
    if not raw:
        return None
    found = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if found is None:
        return None
    try:
        data = json.loads(found.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    name = data.get("intent")
    names = {i.name for i in intents if i.name not in NEVER_CLASSIFIED} | {GENERAL}
    if not isinstance(name, str) or name not in names:
        return None
    slots = data.get("slots", {}) or {}
    if not isinstance(slots, dict):
        return None
    clean: Dict[str, str] = {}
    for key, value in slots.items():
        if not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return None
        value = " ".join(str(value).split())
        if len(value) > 60 or re.search(r"[{}<>\\]", value):
            return None
        clean[key] = value
    try:
        confidence = float(data.get("confidence"))
    except (TypeError, ValueError):
        return None
    if not 0.0 <= confidence <= 1.0:
        return None
    return name, clean, confidence
