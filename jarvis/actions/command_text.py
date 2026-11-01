"""Shared clean-up of what was heard, before the local matchers see it
.

- Her name, fuzzily: Whisper wrote "Javis", "Charvis", "Johnless" for
  "JARVIS". A word counts as her name if it is in [conversation]
  name_variants, or is within name_max_edit_distance letters of "jarvis".
- Politeness and filler ("please", "sir", "can you", "for me", "now"),
  dropped so "Open Chrome please" looks for Chrome, not "chrome please".
- Number words for picking from a list ("open one", "the second one",
  "number 2").

Pure text functions, no I/O.
"""

from __future__ import annotations

import re
import time
from typing import Callable, Iterable, List, Optional

from jarvis.settings import SETTINGS

_CONV = SETTINGS["conversation"]
_INTERP = SETTINGS["interpreter"]

_NAME = "jarvis"
_NAME_VARIANTS = frozenset(str(v).lower() for v in _CONV["name_variants"])
_NAME_MAX_DISTANCE = int(_CONV["name_max_edit_distance"])
_NAME_MIN_LENGTH = int(_CONV["name_min_length"])

_FILLER_PHRASES = [str(p).lower().split() for p in _INTERP["filler_phrases"]]
_FILLER_WORDS = frozenset(str(w).lower() for w in _INTERP["filler_words"])

# Words, keeping the characters that belong inside names ("passport.pdf",
# "c++", "pre-ange"); commas are kept as their own token so "Open Chrome,
# Jarvis" can be told apart from "find jarvis".
_TOKEN = re.compile(r"[\w'+#]+(?:[.\-][\w'+#]+)*|,")
_PUNCTUATION = re.compile(r"[^\w\s']")


def normalize(text: str) -> str:
    """Lower case, punctuation removed, single spaces: "Hey, JARVIS." ->
    "hey jarvis"."""
    return " ".join(_PUNCTUATION.sub(" ", text.lower()).split())


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance (insert, delete, substitute)."""
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def is_name(word: str) -> bool:
    """True if `word` is her name as Whisper might write it."""
    w = word.lower().strip("'")
    if w in _NAME_VARIANTS:
        return True
    return len(w) >= _NAME_MIN_LENGTH and edit_distance(w, _NAME) <= _NAME_MAX_DISTANCE


def strip_name(words: List[str]) -> List[str]:
    """Drop a name-like word from the start and the end ("good night javis"
    -> "good night")."""
    words = list(words)
    while words and is_name(words[0]):
        words = words[1:]
    while words and is_name(words[-1]):
        words = words[:-1]
    return words


def _remove_phrases(tokens: List[str], phrases: Iterable[List[str]]) -> List[str]:
    out: List[str] = []
    i = 0
    phrases = sorted(phrases, key=len, reverse=True)
    while i < len(tokens):
        for phrase in phrases:
            if phrase and tokens[i : i + len(phrase)] == phrase:
                i += len(phrase)
                break
        else:
            out.append(tokens[i])
            i += 1
    return out


_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}\b)")
_DOLLARS = re.compile(r"\$(\d+(?:\.\d+)?)")


def clean_command(text: str) -> str:
    """What the local matchers see. Lower case; commas, ! ? ; : and
    sentence-ending full stops gone; her name dropped at the start (any
    spelling) or at the end after a comma; filler phrases and words
    dropped. "Sir, Chat GPD on Chrome please." -> "chat gpd on chrome".
    If nothing is left, the plain lower-case text is returned."""
    lowered = text.lower().strip()
    # Whisper writes "$1,000"; the comma split it into "1 000" and
    # the dollar sign was lost ("the best laptops under 1 000").
    lowered = _THOUSANDS.sub("", lowered)
    lowered = _DOLLARS.sub(r"\1 dollars", lowered)
    tokens = _TOKEN.findall(lowered)
    # Her name at the start: any spelling ("Javis, open Chrome").
    while tokens and (tokens[0] == "," or is_name(tokens[0])):
        tokens = tokens[1:]
    # At the end only after a comma ("Open Chrome, Jarvis"), so "find
    # jarvis" keeps the word it is looking for.
    while len(tokens) >= 2 and is_name(tokens[-1]) and tokens[-2] == ",":
        tokens = tokens[:-2]
    words = [t for t in tokens if t != ","]
    words = _remove_phrases(words, _FILLER_PHRASES)
    words = [w for w in words if w not in _FILLER_WORDS]
    if not words:
        return " ".join(normalize(text).split())
    return " ".join(words)


# -- picking a number from a list ------------------------------------------------

_NUMBER_WORDS = {
    "one": 1, "first": 1, "1st": 1, "won": 1,
    "two": 2, "second": 2, "2nd": 2, "to": 2, "too": 2,
    "three": 3, "third": 3, "3rd": 3,
    "four": 4, "fourth": 4, "4th": 4, "for": 4,
    "five": 5, "fifth": 5, "5th": 5,
    "six": 6, "sixth": 6, "6th": 6,
    "seven": 7, "seventh": 7, "7th": 7,
    "eight": 8, "eighth": 8, "8th": 8,
    "nine": 9, "ninth": 9, "9th": 9,
    "ten": 10, "tenth": 10, "10th": 10,
}
# Words that may surround the number: "open number two", "the second one",
# "file 3", "option one".
_PICK_FILLER = {"number", "no", "the", "file", "folder", "app", "option", "item", "result", "match", "one", "please", "that"}


def pick_number(selector: str) -> Optional[int]:
    """The list number in "one", "2", "number two", "the second one",
    "first file" -- or None if the selector is not just a number. Words
    that sound like numbers ("to", "for", "won") count only when they are
    the whole selector, so "open google for me" is never a pick."""
    words = re.findall(r"[a-z0-9]+", selector.lower())
    if not words:
        return None
    if len(words) == 1:
        w = words[0]
        if w.isdigit():
            return int(w)
        return _NUMBER_WORDS.get(w)
    numbers = []
    for w in words:
        if w.isdigit():
            numbers.append(int(w))
        elif w in _NUMBER_WORDS and w not in ("to", "too", "for", "won", "one"):
            numbers.append(_NUMBER_WORDS[w])
        elif w == "one":
            continue  # "the second one": "one" is filler here
        elif w not in _PICK_FILLER:
            return None
    if len(numbers) == 1:
        return numbers[0]
    if not numbers and "one" in words and any(w in _COUNTING_WORDS for w in words):
        return 1  # "number one", "option one", "file one"
    return None


_COUNTING_WORDS = {"number", "no", "option", "item", "file", "folder", "app", "result", "match"}


class PickSlot:
    """Which on-screen list "open one" picks from: the one shown last (F4's
    file list or F3's "which app?" list). One per orchestrator; each list
    owner claims it when it shows a list and checks it before picking, so
    an older list can never answer for a newer one.

    A claim lasts [interpreter] pick_valid_seconds, so a
    list survives an unrelated command in between; after that nobody owns
    the slot. `clock` is for tests."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, valid_seconds: Optional[float] = None) -> None:
        self._owner: object = None
        self._claimed_at = 0.0
        self._clock = clock
        self.valid_seconds = float(_INTERP["pick_valid_seconds"] if valid_seconds is None else valid_seconds)

    def claim(self, owner: object) -> None:
        """Make `owner`'s list the one a pick ('the second one') answers, from now."""
        self._owner = owner
        self._claimed_at = self._clock()

    def release(self, owner: object) -> None:
        """Free the slot, but only if `owner` still holds it."""
        if self._owner is owner:
            self._owner = None

    def fresh(self, claimed_at: float) -> bool:
        """Is a list shown at `claimed_at` still pickable?"""
        return self._clock() - claimed_at <= self.valid_seconds

    def now(self) -> float:
        """The slot's clock (monotonic seconds; replaceable in tests)."""
        return self._clock()

    def owned_by(self, owner: object) -> bool:
        """True if `owner` holds the slot and its list is still fresh."""
        return self._owner is owner and self.fresh(self._claimed_at)

    def active(self) -> bool:
        """Is any list pickable right now?"""
        return self._owner is not None and self.fresh(self._claimed_at)


_FILE_LIST_WORDS = {"file", "files", "document", "documents", "pdf"}


def names_a_file(text: str) -> bool:
    """"open file number 1": the word "file" means the file list only."""
    return bool(_FILE_LIST_WORDS & set(re.findall(r"[a-z]+", text.lower())))


def is_pick_shaped(text: str) -> bool:
    """A pick by number and nothing else: "open number one", "open file
    number 1", "open one", "the second one", "number 2", "second". Used when
    no list is pending, so these are never searched for as an app."""
    return pick_index(text) is not None


def out_of_range_text(count: int, noun: str) -> str:
    """'There are only N <noun> on the list...' for a pick number that isn't on the list."""
    return f"There are only {count} {noun} on the list. Say 'open' and a number from 1 to {count}."


def not_on_list_text(count: int, noun: str) -> str:
    """A pick that matched nothing on the list: said locally, never sent online."""
    return f"I couldn't match that to the {noun} on the list. Say a number from 1 to {count}, or part of the name."


def name_pick(selector: str, names: List[str]) -> tuple:
    """(index, tied) for the list item whose name holds every selector word
    (whole words or word starts; "alex passport" ->
    "Alex_Passport.pdf"). Fewest extra words wins; (None, True) when two
    names fit equally well, (None, False) when none does."""
    wanted = re.findall(r"[a-z0-9]+", selector.lower())
    if not wanted:
        return None, False
    scored = []
    for i, name in enumerate(names):
        words = re.findall(r"[a-z0-9]+", name.lower())
        if all(any(w == x or (len(x) >= 3 and w.startswith(x)) for w in words) for x in wanted):
            scored.append((len(words) - len(wanted), i))
    if not scored:
        return None, False
    scored.sort()
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None, True
    return scored[0][1], False


# -- a command anywhere in the sentence --------------------

_VERBS = {" ".join(str(k).lower().split()): str(v) for k, v in _INTERP["command_verbs"].items()}
_LEADS = frozenset(str(w).lower() for w in _INTERP["command_leads"])
_NOT_AFTER = [str(w).lower() for w in _INTERP["command_not_after"]]
_VERB_PATTERN = re.compile(
    r"(?<![\w'])(" + "|".join(re.escape(v).replace(r"\ ", r"\s+") for v in sorted(_VERBS, key=len, reverse=True)) + r")(?![\w'])"
)
# Where a clause ends: sentence punctuation, or a comma not followed by "and"/"then".
_CLAUSE_START = re.compile(r"[.!?;:]|,(?!\s*(?:and|then)\b)")
_CLAUSE_END = re.compile(r"[.!?;:]|,(?!\s*(?:and|then)\b)")


def extract_command(text: str) -> Optional[str]:
    """The command inside a longer sentence, or None if there isn't one.

    "This is so annoying, nothing is working, open notepad" -> "open notepad";
    "AI. Dum. As. Can you please open chat GPT in Chrome" -> "open chat gpt in
    chrome"; "Launch Virtual Studio" -> "open virtual studio". A verb from
    [interpreter] command_verbs counts only where it starts a clause: at the
    start, after . ! ? ; : or a comma, or after nothing but command_leads
    words and her name ("can you please", "jarvis, ..."). So "how do I open
    Chrome", "I can't open it" and "find out who won" are never commands.
    The command runs up to the end of its clause; "open chrome, and then
    search X" keeps its second half."""
    lowered = text.lower().replace("’", "'")
    for match in _VERB_PATTERN.finditer(lowered):
        starts = [m.end() for m in _CLAUSE_START.finditer(lowered, 0, match.start())]
        before = lowered[starts[-1] if starts else 0 : match.start()]
        lead = re.findall(r"[\w']+", before)
        if any(w not in _LEADS and not is_name(w) for w in lead):
            continue
        rest = lowered[match.end():]
        end = _CLAUSE_END.search(rest)
        rest = (rest[: end.start()] if end else rest).strip()
        if not rest or any(rest == w or rest.startswith(w + " ") for w in _NOT_AFTER):
            continue
        verb = _VERBS[" ".join(match.group(1).split())]
        return f"{verb} {rest}"
    return None


# -- picking from a pending list --------------------------

# "opens second", "and the second file please", "pick number two": words that
# can surround a pick and say nothing about which item.
_PICK_WORDS = {"open", "opens", "opened", "pick", "choose", "select", "take", "want", "go", "with", "and", "the", "that",
               "this", "one", "please", "number", "no", "file", "folder", "app", "option", "item", "i", "it", "just", "my", "a"}


def pick_selector(text: str) -> str:
    """The words that may name an item: "and the second file please" ->
    "second"; "open alex passport" -> "alex passport"."""
    words = re.findall(r"[a-z0-9]+", text.lower().replace("'", ""))
    return " ".join(w for w in words if w not in _PICK_WORDS)


def pick_index(text: str) -> Optional[int]:
    """A list number from anywhere in a pick: "opens second", "the second
    file please", "number two", "2", "second one please". None if there is
    no number, or more than one."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    while len(words) > 1 and words[0] in ("open", "opens", "opened", "pick", "choose", "select", "and", "the"):
        words = words[1:]  # "open one", "pick the second"
    if len(words) == 1:
        return pick_number(words[0])
    if any(w not in _PICK_WORDS and not w.isdigit() and w not in _NUMBER_WORDS for w in words):
        return None  # other words: it's a name ("open resume_2024.docx"), not a number
    numbers = []
    for w in words:
        if w.isdigit():
            numbers.append(int(w))
        elif w in _NUMBER_WORDS and w not in ("to", "too", "for", "won", "one"):
            numbers.append(_NUMBER_WORDS[w])
    if not numbers and "one" in words and any(w in ("number", "no", "option", "item", "file") for w in words):
        return 1
    return numbers[0] if len(numbers) == 1 else None


def looks_like_a_pick(text: str) -> bool:
    """Did he try to pick from the list? A number or ordinal anywhere, or a
    pick-shaped start ("the …", "that one", "number …", "pick …", "and the
    …") -- as opposed to an unrelated command ("what time is it", "open
    chrome"), which lets the list expire."""
    if pick_index(text) is not None:
        return True
    words = re.findall(r"[a-z0-9]+", text.lower())
    while words and words[0] in ("and", "um", "uh", "ok", "okay"):
        words = words[1:]
    return bool(words) and words[0] in ("the", "that", "this", "number", "pick", "choose", "select")


# -- confirm or cancel inside a sentence ------------------

_CONFIRM_PHRASES = [str(w).lower().split() for w in _CONV["confirm_words"]]
_CANCEL_PHRASES = [str(w).lower().split() for w in _CONV["cancel_words"]]
_CONFIRM_FILLER = [str(w).lower().split() for w in _CONV["confirm_filler_words"]]

CONFIRM, CANCEL, BOTH = "confirm", "cancel", "both"


def confirm_intent(text: str) -> Optional[str]:
    """While a confirmation is pending: CONFIRM for "Please send it online.
    Confirm.", CANCEL for "no, cancel that", BOTH for "yes... no, cancel"
    (she asks again), None when anything else is said (a new command or
    chatter: ignored, as before). Her name anywhere is fine."""
    words = [w for w in re.findall(r"[a-z']+", text.lower().replace("’", "'")) if not is_name(w)]
    found = set()
    i = 0
    phrases = sorted([(p, CONFIRM) for p in _CONFIRM_PHRASES] + [(p, CANCEL) for p in _CANCEL_PHRASES] + [(p, None) for p in _CONFIRM_FILLER],
                     key=lambda item: len(item[0]), reverse=True)
    while i < len(words):
        for phrase, kind in phrases:
            if words[i : i + len(phrase)] == phrase:
                if kind is not None:
                    found.add(kind)
                i += len(phrase)
                break
        else:
            # "Icon Firm", "Con firm", "please confirm the wifi"
            # -- any close "confirm" counts when no cancel word is said.
            if not _says_confirm(words):
                return None
            return BOTH if _says_cancel(words) else CONFIRM
    if found == {CONFIRM}:
        return CONFIRM
    if found == {CANCEL}:
        return CANCEL
    return BOTH if found else None


_CONFIRM_TOKEN = "confirm"


def _is_confirm_like(token: str) -> bool:
    return token.startswith(_CONFIRM_TOKEN) or (len(token) >= 5 and edit_distance(token, _CONFIRM_TOKEN) <= 2)


def _says_confirm(words: List[str]) -> bool:
    """A word (or two run together: "icon firm", "con firm") within two
    letters of "confirm". Never after "say": that is her own prompt ("Say
    confirm.") heard back."""
    for i, word in enumerate(words):
        if i > 0 and words[i - 1] == "say":
            continue
        if _is_confirm_like(word) or (i + 1 < len(words) and _is_confirm_like(word + words[i + 1])):
            return True
    return False


def _says_cancel(words: List[str]) -> bool:
    return any(words[i : i + len(p)] == p for p in _CANCEL_PHRASES for i in range(len(words)))


# -- known app and site names -------------------------------

_APP_CFG = SETTINGS["app_finder"]


def known_names() -> List[str]:
    """Every app or site name JARVIS knows for sure: aliases, bare names,
    Windows' own apps and the known websites (lower case, as said)."""
    names = set()
    for section in ("aliases", "bare_names", "system_app_aliases", "website_fallbacks"):
        names.update(" ".join(str(k).lower().split()) for k in _APP_CFG.get(section, {}))
    return sorted(names, key=len, reverse=True)


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


_GLUED_VERB = re.compile(r"\b(open|launch|start)([a-z]{2,})\b", re.IGNORECASE)


def split_glued(text: str) -> str:
    """Whisper glues the verb to the name: "OpenNote Pad" / "opennotepad"
    -> "open notepad", "OpenChat GPT" -> "open chatgpt". Only when the rest
    (with up to two following words) is exactly a known name, squashed, so
    "opener" or "startup folder" are left alone."""
    names = {_squash(n): n for n in known_names()}
    out = text
    for match in list(_GLUED_VERB.finditer(text)):
        following = re.findall(r"[A-Za-z0-9]+", text[match.end():])[:2]
        for take in range(len(following), -1, -1):
            squashed = _squash(match.group(2) + "".join(following[:take]))
            if squashed in names:
                end = match.end()
                for word in following[:take]:
                    end = text.index(word, end) + len(word)
                out = out.replace(text[match.start():end], f"{match.group(1).lower()} {names[squashed]}", 1)
                break
    # "OpenZorblex.com", "OpenGetHub.com" -- unknown names never
    # reached the open command and went online (Groq then described a page
    # it never opened). Whisper's capital letter marks the word boundary.
    return _GLUED_CAMEL.sub(lambda m: m.group(1) + " ", out)


_GLUED_CAMEL = re.compile(r"\b(Open|Launch|open|launch)(?=[A-Z][a-z])")  # not Start: "StartUp", "StartMenu" are names


_DETERMINERS = {"your", "my", "the", "a", "an", "our"}


def strip_determiners(name: str) -> str:
    """"your youtube" -> "youtube", "the notepad app" stays "notepad app"."""
    words = name.split()
    while len(words) > 1 and words[0] in _DETERMINERS:
        words = words[1:]
    return " ".join(words)


def named_app_in(text: str) -> Optional[str]:
    """The one known app or site name in what he said ("... OpenNote Pad"
    -> "notepad"), or None when there is none or more than one. Used for
    the false-claim reply: only a command that is certainly right is
    suggested."""
    squashed = _squash(split_glued(text))
    found = []
    for name in known_names():
        key = _squash(name)
        if len(key) >= 4 and key in squashed and not any(key in _squash(f) for f in found):
            found.append(name)
    return found[0] if len(found) == 1 else None
