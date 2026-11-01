"""F10 commands, all local (R2) and never sent online (R4):

- "remember (that) X" / "don't forget X" -> "Got it." (action `remember`,
  L0); Live Activity shows "Remembered: X". A password, PIN, card, bank or
  ID number is refused: "I won't store that. It looks like a <type>."
- "actually X" (a correction) -> updates the one clear match ("Updated."),
  otherwise stores X as new.
- "what do you know / remember about Y" -> up to [memory] recall_max
  matches, newest first: "You told me X. You said that on <date>."
- "what do you remember" -> how many, the newest [memory] list_spoken_max
  spoken; the full list in Live Activity.
- "forget Y" -> one match is deleted ("Forgotten.", action
  `forget_memory`, L1); several -> a numbered list to pick from.
- "forget everything" -> action `forget_all_memory`, L4: two "confirm"s.

What he said is stored with its first person intact ("my exam is on
tuesday"); recall turns it round ("You told me your exam is on tuesday").
"""

from __future__ import annotations

import re
import time
from typing import Callable, List, Optional

from jarvis.actions.command_text import PickSlot, extract_command, is_name, pick_index
from jarvis.interpreter import ActionRequest, Answer
from jarvis.logbook.records import Candidate
from jarvis.memory.sensitive import refusal_text, sensitive_type
from jarvis.memory.store import MemoryFull, MemoryItem, MemoryStore, keywords
from jarvis.settings import SETTINGS

_CFG = SETTINGS["memory"]
_REPLIES = _CFG["replies"]

_LEAD_FILLER = {str(w).lower() for w in _CFG["lead_filler"]}
_PUNCT_EDGES = re.compile(r"^[\s,.;:!?]+|[\s,.;:!?]+$")


def _phrases(key: str) -> List[List[str]]:
    """Settings phrases as word lists, longest first."""
    return sorted((str(p).lower().split() for p in _CFG[key]), key=len, reverse=True)


_REMEMBER = _phrases("remember_prefixes")
_CORRECTION = _phrases("correction_prefixes")
_RECALL = _phrases("recall_prefixes")
_LIST = {" ".join(p) for p in _phrases("list_phrases")}
_FORGET_ALL = {" ".join(p) for p in _phrases("forget_all_phrases")}
_FORGET = _phrases("forget_prefixes")
_ABOUT_THE_MEMORY = {str(w).lower() for w in _CFG["forget_all_tail_words"]}
_NOT_A_FORGET_TARGET = {" ".join(str(p).lower().split()) for p in _CFG["not_forget_targets"]}
_NOT_A_CORRECTION = {" ".join(str(p).lower().split()) for p in _CFG["not_corrections"]}
_QUESTION_STARTS = {str(w).lower() for w in _CFG["question_starts"]}
# "Actually, it is on Wednesday now" -> "it" is the
# subject of the memory just saved or recalled ("my meeting").
_PRONOUNS = {str(w).lower() for w in _CFG.get("correction_pronouns", [])}
_PRONOUN_VERBS = {str(w).lower() for w in _CFG.get("pronoun_verbs", [])}
_PRONOUN_LEAD = {str(w).lower() for w in _CFG.get("pronoun_lead_words", [])}
_PRONOUN_WINDOW = float(_CFG.get("pronoun_window_seconds", 120))
_PRONOUN_SIGNALS = {str(w).lower() for w in _CFG.get("pronoun_signal_words", [])}
# The pronoun with its verb folded in: "it's on wednesday" -> "<subject> is on wednesday".
_PRONOUN_VERB_OF = {"it's": "is", "its": "is", "that's": "is", "this's": "is", "it'll": "will"}

# His own facts are never asked online (personal_matcher).
_PERSONAL_LEAD = {"but", "so", "and", "ok", "okay", "hey", "um", "uh", "well", "then"}
_WHEN_MISHEARD = {"ven", "wen", "van", "vhen", "whenn"}


def _router_spellings_of(word: str) -> set:
    """The intent router's mishearing table
    (jarvis/settings/intents.toml) is the one list of Whisper's spellings."""
    try:
        from jarvis.router.router import _MISHEARD
    except Exception:  # the router missing never breaks memory
        return set()
    return {heard for heard, meant in _MISHEARD.items() if meant == word and " " not in heard}


_WHEN_MISHEARD |= _router_spellings_of("when")


def _router_document_words() -> set:
    """The nouns that make "where is my X" a file to find --
    the router's find_file vocabulary (intents.toml), the one list."""
    try:
        from jarvis.router.router import _INTENTS
    except Exception:  # the router missing never breaks memory
        return set()
    for intent in _INTENTS:
        if intent.name == "find_file" and len(intent.need) > 1:
            return {" ".join(p) for p in intent.need[1]} - {"my"}
    return set()


_DOCUMENT_WORDS = _router_document_words()
_THING = r"(?P<thing>[a-z0-9' -]+?)"
_PERSONAL_TAIL = r"(?:\s+(?:due|now|again|exactly|happening|scheduled|today|tomorrow|this week|going to be|on|at))*"
_PERSONAL_QUESTION = re.compile(
    r"(?:do you know\s+|tell me\s+|remind me\s+)?"
    r"(?:when|what|where|what time|what day|which day)(?:'s|\s+is|\s+was|\s+are|\s+were)?\s+(?:is\s+|was\s+)?my\s+"
    + _THING + _PERSONAL_TAIL + r"(?:\s+is)?")
_TOLD_YOU = re.compile(r"i\s+(?:already\s+)?told\s+you\s+(?:that\s+)?my\s+" + _THING + r"\s+(?:is|was|are|were|will be)\b.*")

# Spoken back to him: his first person becomes "you". Longest first.
_TURN_ROUND = [(re.compile(r"(?<![\w'])" + re.escape(a) + r"(?![\w'])", re.IGNORECASE), b) for a, b in (
    ("i am", "you are"), ("i'm", "you're"), ("i was", "you were"), ("i've", "you've"), ("i'll", "you'll"),
    ("i'd", "you'd"), ("myself", "yourself"), ("mine", "yours"), ("my", "your"), ("me", "you"), ("i", "you"),
)]


def turn_round(text: str) -> str:
    """"my exam is on tuesday" -> "your exam is on tuesday"."""
    out = text
    placeholders = []
    for n, (pattern, replacement) in enumerate(_TURN_ROUND):
        token = f"\x00{n}\x00"
        out = pattern.sub(token, out)
        placeholders.append((token, replacement))
    for token, replacement in placeholders:
        out = out.replace(token, replacement)
    return out


def spoken_date(item: MemoryItem) -> str:
    """When an item was saved, as said aloud, e.g. '3 March 2026'."""
    when = item.saved_at
    return f"{when.day} {when:%B %Y}"


def _words(text: str) -> List[str]:
    """What he said, as words: lower case, outer punctuation gone, her name
    and polite lead words ("please", "can you") dropped from the start."""
    words = [w.strip(",.;:!?\"") for w in text.lower().replace("’", "'").split()]
    words = [w for w in words if w]
    while words and (is_name(words[0]) or words[0] in _LEAD_FILLER):
        words = words[1:]
    while words and is_name(words[-1]):
        words = words[:-1]
    return words


def _after(words: List[str], prefixes: List[List[str]]) -> Optional[str]:
    """The rest of the sentence after the first matching prefix, or None."""
    for prefix in prefixes:
        if words[: len(prefix)] == prefix:
            return " ".join(words[len(prefix):])
    return None


def _clean_text(rest: str) -> str:
    return _PUNCT_EDGES.sub("", rest)


_FACT = re.compile(r"^(?:my|our)\s+(?P<subject>[a-z0-9' -]+?)\s+(?:is|are|was|were|will be|has been|have been|got|moved|changed)\b")
_NOT_TAIL = re.compile(r",?\s+(?:and\s+)?not\s+(?:on\s+|at\s+|in\s+)?[a-z0-9']+(?:\s+[a-z0-9']+)?$")
_NOW_TAIL = re.compile(r"\s+(?:now|anymore|instead)$")
_CHANGED = re.compile(r"\s+(?:has\s+)?(?:changed|moved)\s+to\s+")
# "what days do I go to the gym", "when do I have my dentist", "where am I meeting Tom".
_HIS_SCHEDULE = re.compile(r"^(?:(?:what|which)\s+(?:days?|times?|dates?|day of the week)|when|where|what time)\s+(?:do|did|am|have|will)\s+i\b")
_REMEMBER_TAIL = re.compile(r"[,.!]?\s+(?:please\s+)?(?:" + "|".join(
    re.escape(" ".join(p)) + r"(?:\s+(?:it|this|that))?" for p in _REMEMBER) + r")(?:\s+please)?[.!]?$")


def clean_fact(text: str) -> str:
    """"my exam is on wednesday now" -> "my exam is on wednesday"; "my exam is
    on tuesday, not wednesday" -> "my exam is on tuesday"; "my exam changed to
    friday" -> "my exam is friday"."""
    text = " ".join(text.split())
    # "my gym days are now tuesday and friday.
    # Remember that." was stored with "are now" and "remember that" in it.
    text = _REMEMBER_TAIL.sub("", text)
    text = _CHANGED.sub(" is ", text)
    text = re.sub(r"\b(is|are) now\b", r"\1", text)
    without_not = _NOT_TAIL.sub("", text)
    if not re.search(r"\b(?:is|are|was|were|be)$", without_not):  # "my colour is not blue" keeps its "not"
        text = without_not
    text = _NOW_TAIL.sub("", text)
    return text.strip(" ,.")


def fact_subject(text: str) -> str:
    """The subject of a "my X is ..." fact ("exam", "dentist appointment"),
    plural folded; "" for anything else ("to call mum")."""
    match = _FACT.match(" ".join(text.lower().split()))
    if match is None:
        return ""
    return " ".join(sorted(keywords(match.group("subject"))))


def newest_per_subject(items: List[MemoryItem]) -> List[MemoryItem]:
    """"When is my exam" said three different days. Of several
    facts on one subject only the newest is true."""
    seen, out = set(), []
    order = {id(item): n for n, item in enumerate(items)}
    # Newest first; saved in the same second, the later one in the file wins.
    for item in sorted(items, key=lambda i: (i.saved_at, order[id(i)]), reverse=True):
        subject = fact_subject(item.text)
        if subject and subject in seen:
            continue
        seen.add(subject) if subject else None
        out.append(item)
    return out


_ALL_OF = re.compile(r"^(?:all(?:\s+of)?|every|everything(?:\s+about)?|each)\s+(?:the\s+)?(?:things?\s+about\s+)?")


class MemoryCommands:
    """The matchers and executors. `store` is shared with nothing else."""

    def __init__(self, store: MemoryStore, report: Callable[[str], None] = lambda _t: None,
                 slot: Optional[PickSlot] = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.store = store
        self._report = report
        self._slot = slot
        self._clock = clock
        self._pending_forget: Optional[List[MemoryItem]] = None
        # The subject of the memory last saved or recalled ("my meeting") and when.
        self._last_subject: Optional[str] = None
        self._last_subject_at = 0.0

    # -- registration ----------------------------------------------------------

    def register(self, interpreter, orchestrator) -> None:
        """The pick matcher must be registered with the other picks (first);
        the rest after the clock/explain/sleep matchers and before weather,
        files and apps, so "remember to open chrome at 5" is a memory."""
        self._report = orchestrator.report_step
        if self._slot is None:
            self._slot = getattr(orchestrator, "picks", None)
        interpreter.register("memory", self.matcher, raw=True)
        orchestrator.register_executor("remember", self.remember_executor)
        orchestrator.register_executor("forget_memory", self.forget_executor)
        orchestrator.register_executor("forget_all_memory", self.forget_all_executor)
        orchestrator.register_executor("forget_memories", self.forget_many_executor)
        orchestrator.controller.add_transition_listener(self.on_transition)

    def on_transition(self, entry) -> None:
        """On Stop, drop a pending 'which one to forget?' list."""
        if entry.event_type is None:  # Stop: an old list never decides what is deleted
            self._pending_forget = None

    # -- matching --------------------------------------------------------------

    @staticmethod
    def _is_forget_all(words: List[str]) -> bool:
        """"Forget everything you remember about me"
        was "forget <me>": one item went at L1 with no confirm. A forget-all
        phrase followed only by words about the memory or about him is still
        everything (L4, two confirms)."""
        sentence = " ".join(words)
        for phrase in sorted(_FORGET_ALL, key=len, reverse=True):
            if sentence.startswith(phrase + " "):
                rest = sentence[len(phrase) + 1:].split()
                return bool(rest) and set(rest) <= _ABOUT_THE_MEMORY
        return False

    def matcher(self, raw_text: str):
        """F10: remember, correct, recall, list and forget commands. Returns an Answer, an ActionRequest,
        or None."""
        words = _words(raw_text)
        if not words:
            return None
        sentence = " ".join(words)

        if sentence in _LIST:
            return self._list_all()
        if sentence in _FORGET_ALL or self._is_forget_all(words):
            return self._forget_all()
        topic = _after(words, _RECALL)
        if topic is not None:
            topic = _clean_text(topic)
            return self._list_all() if topic in ("me", "myself") else self._recall(topic)
        rest = _after(words, _REMEMBER)
        if rest is not None:
            return self._remember(_clean_text(rest), correction=False)
        about_it = self._resolve_pronoun(words)
        if about_it is not None and not raw_text.rstrip().endswith("?"):
            return self._remember(about_it, correction=True)
        rest = _after(words, _FORGET)
        if rest is not None:
            rest = _clean_text(rest)
            if rest and rest not in _NOT_A_FORGET_TARGET:
                return self._forget(rest)
            return None
        rest = _after(words, _CORRECTION)
        if rest is not None:
            rest = _clean_text(rest)
            if self._is_correction(rest, raw_text):
                return self._remember(rest, correction=True)
        return None

    def _about_himself(self, words: List[str]):
        """"What days do I go to the gym?" went
        online although "my gym days are tuesday and friday" was stored. A
        question about himself ("... do I ...") that contains every keyword
        of a stored fact's subject is answered from memory; anything else
        goes on to the other matchers, unchanged."""
        if not _HIS_SCHEDULE.match(" ".join(words)):
            return None  # "when should I start revising for an exam" is a real question
        asked = keywords(" ".join(words))
        found = [item for item in newest_per_subject(self.store.items())
                 if fact_subject(item.text) and set(fact_subject(item.text).split()) <= asked]
        if not found:
            return None
        found = found[: int(_CFG["recall_max"])]
        for item in found:
            self._report(f"recalled: {item.text} ({spoken_date(item)})")
        self._note_subject(found[0].text)
        return Answer(" ".join(
            str(_REPLIES["recall_item"]).format(text=turn_round(item.text), date=spoken_date(item)) for item in found))

    def personal_matcher(self, raw_text: str):
        """"When is my presentation due" went online and Groq
        made up "Wednesday, November 8th". A question about HIS own thing
        ("when/what/where is my X", "I told you my X is ...") is answered
        from memory only, never online. Registered just before the online
        conversation, so every other local command is tried first."""
        words = _words(raw_text)
        while words and words[0] in _PERSONAL_LEAD:
            words = words[1:]
        if words and words[0] in _WHEN_MISHEARD:
            words = ["when"] + words[1:]
        sentence = " ".join(words)
        match = _PERSONAL_QUESTION.fullmatch(sentence) or _TOLD_YOU.fullmatch(sentence)
        if match is None:
            return self._about_himself(words)
        thing = " ".join(w for w in match.group("thing").split() if w not in ("the", "a", "an"))
        if not thing:
            return None
        found = newest_per_subject(self.store.search(thing))[: int(_CFG["recall_max"])]
        if not found and words[0] == "where" and _DOCUMENT_WORDS & set(thing.split()):
            return None  # "where is my budget report": a file to find (the router's F4 intent), not a memory
        if not found:
            told = match.re is _TOLD_YOU
            return Answer(str(_REPLIES["personal_told_none" if told else "personal_none"]).format(thing=thing))
        for item in found:
            self._report(f"recalled: {item.text} ({spoken_date(item)})")
        self._note_subject(found[0].text)
        return Answer(" ".join(
            str(_REPLIES["recall_item"]).format(text=turn_round(item.text), date=spoken_date(item)) for item in found))

    # -- "it" -----------------------------------------------------------------

    def _note_subject(self, text: str) -> None:
        """Remember what "it" means now: the subject of a fact just saved or
        recalled ("my meeting is on monday" -> "my meeting")."""
        match = _FACT.match(" ".join(text.lower().split()))
        if match is not None:
            self._last_subject = f"{text.split()[0].lower()} {match.group('subject').strip()}"
            self._last_subject_at = self._clock()

    def _resolve_pronoun(self, words: List[str]) -> Optional[str]:
        """"actually it is on wednesday now" -> "my meeting is on wednesday
        now", within [memory] pronoun_window_seconds of that memory; else None."""
        if self._last_subject is None or self._clock() - self._last_subject_at > _PRONOUN_WINDOW:
            return None
        if not _PRONOUN_SIGNALS & set(words):
            return None  # "it is cold today" says nothing changed
        while words and words[0] in _PRONOUN_LEAD:
            words = words[1:]
        if len(words) < 2 or words[0] not in _PRONOUNS:
            return None
        verb = _PRONOUN_VERB_OF.get(words[0])
        if verb is None and words[1] not in _PRONOUN_VERBS:
            return None  # "it doesn't matter", "this one"
        if extract_command(" ".join(words[1:])) is not None:
            return None
        return " ".join([self._last_subject] + ([verb] if verb else []) + words[1:])

    def _is_correction(self, rest: str, raw_text: str) -> bool:
        """"actually my exam is on tuesday" -- not "actually, open chrome",
        "actually never mind" or "actually what time is it"."""
        words = rest.split()
        if len(words) < int(_CFG["min_correction_words"]) or rest in _NOT_A_CORRECTION:
            return False
        if words[0] in _QUESTION_STARTS or raw_text.rstrip().endswith("?"):
            return False
        return extract_command(raw_text) is None

    # -- remember / correct ---------------------------------------------------

    def _remember(self, text: str, correction: bool):
        if not text:
            return Answer(str(_REPLIES["remember_empty"]))
        about_it = self._resolve_pronoun(text.split())
        if about_it is not None:
            text, correction = about_it, True
        kind = sensitive_type(text)
        if kind is not None:
            # Refused: never stored, never logged (redact_sensitive hides the
            # heard line, and nothing below reports the text).
            return Answer(refusal_text(kind))
        # "Actually, my exam is on Wednesday now" was stored next
        # to "my exam is on monday" and "... tuesday". A fact about one
        # subject ("my exam is ...") replaces every item on that subject:
        # the newest is updated, the rest are removed. No contradictions.
        text = clean_fact(text)
        subject = fact_subject(text)
        if subject:
            same = [i for i in self.store.items() if fact_subject(i.text) == subject]
            if same:
                newest = max(enumerate(same), key=lambda pair: (pair[1].saved_at, pair[0]))[1]
                others = [i.id for i in same if i.id != newest.id]
                return ActionRequest("remember", {"text": text, "replaces": newest.id, "removes": others, "display_name": text},
                                     candidates=[Candidate(i.text, 1.0) for i in same])
        if correction:
            match = self._clear_match(text)
            if match is not None:
                return ActionRequest("remember", {"text": text, "replaces": match.id, "display_name": text},
                                     candidates=[Candidate(match.text, 1.0)])
        if self.store.count() >= self.store.max_items:
            return Answer(str(_REPLIES["full"]).format(max=self.store.max_items))
        return ActionRequest("remember", {"text": text, "replaces": "", "display_name": text})

    def _clear_match(self, text: str) -> Optional[MemoryItem]:
        """The one stored item a correction is about: the most keywords in
        common, at least [memory] correction_min_overlap of the new text's
        keywords, and no other item as close."""
        new = keywords(text)
        if not new:
            return None
        scored = sorted(((len(new & keywords(i.text)), i) for i in self.store.items()), key=lambda s: s[0], reverse=True)
        if not scored or scored[0][0] == 0:
            return None
        best, item = scored[0]
        if best / len(new) < float(_CFG["correction_min_overlap"]):
            return None
        if len(scored) > 1 and scored[1][0] == best:
            return None  # two equally close: not a clear match
        return item

    def remember_executor(self, action_name: str, params: dict):
        """Save (or update) one memory, then read the file back to verify it (R12). Returns (verified, what
        to say)."""
        text, replaces = str(params["text"]), str(params.get("replaces") or "")
        try:
            item = self.store.update(replaces, text) if replaces else self.store.add(text)
        except MemoryFull:
            return False, str(_REPLIES["full"]).format(max=self.store.max_items)
        except OSError:
            return False, str(_REPLIES["write_failed"])
        if item is None:
            item = self.store.add(text)
        for old in params.get("removes") or ():
            self.store.delete(str(old))
        # R12: read the file back and check the item is really there.
        self.store.reload()
        stored = self.store.get(item.id)
        if stored is None or stored.text != text:
            return False, str(_REPLIES["write_failed"])
        if any(self.store.get(str(old)) is not None for old in params.get("removes") or ()):
            return False, str(_REPLIES["write_failed"])
        self._report(f"Remembered: {text}")
        self._note_subject(text)
        return True, str(_REPLIES["updated"] if replaces else _REPLIES["got_it"])

    # -- recall -----------------------------------------------------------------

    def _recall(self, topic: str):
        found = newest_per_subject(self.store.search(topic))[: int(_CFG["recall_max"])]
        if not found:
            return Answer(str(_REPLIES["recall_none"]).format(topic=turn_round(topic)))
        for item in found:
            self._report(f"recalled: {item.text} ({spoken_date(item)})")
        self._note_subject(found[0].text)
        return Answer(" ".join(
            str(_REPLIES["recall_item"]).format(text=turn_round(item.text), date=spoken_date(item)) for item in found))

    def _list_all(self):
        items = self.store.items()
        if not items:
            return Answer(str(_REPLIES["list_none"]))
        for n, item in enumerate(items, 1):
            self._report(f"{n}. {item.text} ({spoken_date(item)})")
        newest = items[: int(_CFG["list_spoken_max"])]
        spoken = "; ".join(turn_round(i.text) for i in newest)
        head = str(_REPLIES["list_one" if len(items) == 1 else "list_head"]).format(count=len(items), shown=len(newest))
        return Answer(f"{head} {spoken}.")

    # -- forget -----------------------------------------------------------------

    def _forget(self, topic: str):
        # "Forget all my exams" asked which one to forget.
        all_of = _ALL_OF.match(topic)
        if all_of is not None and topic[all_of.end():].strip():
            topic = topic[all_of.end():].strip()
        found = self.store.search(topic)
        if not found:
            return Answer(str(_REPLIES["forget_none"]).format(topic=turn_round(topic)))
        if len(found) == 1:
            return self._forget_request(found[0])
        if all_of is not None:
            for n, item in enumerate(found, 1):
                self._report(f"{n}. {item.text}")
            listing = " ".join(f"{n}, {turn_round(i.text)}." for n, i in enumerate(found, 1))
            return ActionRequest("forget_memories", {"ids": [i.id for i in found], "count": len(found),
                                                     "display_name": f"{len(found)} things about {topic}"},
                                 candidates=[Candidate(i.text, 1.0) for i in found],
                                 prompt=str(_REPLIES["forget_many_prompt"]).format(count=len(found), topic=turn_round(topic),
                                                                                    items=listing))
        self._pending_forget = found
        if self._slot is not None:
            self._slot.claim(self)
        for n, item in enumerate(found, 1):
            self._report(f"{n}. {item.text}")
        listing = " ".join(f"{n}, {turn_round(i.text)}." for n, i in enumerate(found, 1))
        return Answer(str(_REPLIES["forget_pick"]).format(count=len(found), topic=turn_round(topic), items=listing))

    def _forget_request(self, item: MemoryItem) -> ActionRequest:
        return ActionRequest("forget_memory", {"id": item.id, "display_name": item.text},
                             candidates=[Candidate(item.text, 1.0)])

    def pick_matcher(self, normalized_text: str):
        """"2", "the second one", "forget number two" right after the list."""
        pending, self._pending_forget = self._pending_forget, None
        if pending is not None and self._slot is not None and not self._slot.owned_by(self):
            pending = None
        if pending is None:
            return None
        words = [w for w in normalized_text.split() if w not in ("forget", "delete", "remove")]
        number = pick_index(" ".join(words))
        if number is None:
            return None  # something else: the list expires
        if not 1 <= number <= len(pending):
            self._pending_forget = pending
            return Answer(str(_REPLIES["forget_out_of_range"]).format(count=len(pending)))
        return self._forget_request(pending[number - 1])

    def forget_executor(self, action_name: str, params: dict):
        """Delete one memory and verify it is gone. Returns (verified, what to say)."""
        item_id = str(params["id"])
        if not self.store.delete(item_id):
            return False, str(_REPLIES["forget_gone"])
        self.store.reload()
        if self.store.get(item_id) is not None:  # R12
            return False, str(_REPLIES["write_failed"])
        self._report(f"Forgotten: {params.get('display_name', '')}")
        return True, str(_REPLIES["forgotten"])

    def forget_many_executor(self, action_name: str, params: dict):
        """Delete several memories at once and verify they are gone."""
        ids = [str(i) for i in params.get("ids") or ()]
        for item_id in ids:
            self.store.delete(item_id)
        self.store.reload()
        if any(self.store.get(i) is not None for i in ids):  # R12
            return False, str(_REPLIES["write_failed"])
        self._report(f"Forgotten: {params.get('display_name', '')}")
        return True, str(_REPLIES["forgotten"])

    def _forget_all(self):
        count = self.store.count()
        if count == 0:
            return Answer(str(_REPLIES["forget_all_empty"]))
        return ActionRequest("forget_all_memory", {"count": count, "display_name": "everything I remember"},
                             prompt=str(_REPLIES["forget_all_prompt"]).format(count=count))

    def forget_all_executor(self, action_name: str, params: dict):
        """L4: delete every memory (after two confirms) and verify the store is empty."""
        removed = self.store.clear()
        self.store.reload()
        if self.store.count() != 0:  # R12
            return False, str(_REPLIES["write_failed"])
        self._report(f"Forgotten: all {removed} remembered items")
        return True, str(_REPLIES["forget_all_done"])
