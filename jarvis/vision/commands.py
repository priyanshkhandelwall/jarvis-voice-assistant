"""F9 commands (the developer's F9 decisions).

- "what do you see" / "look through the camera" / "what's this" -> one look
  (fixed action `capture_camera`, L1): warm-up, black-frame check, the
  camera released before analysis, local detection: "I can see a laptop
  and a cup." Nothing goes online.
- "how much is this" -> the same look, then the price table: "That looks
  like a laptop. Roughly $800 to $2,500. Those are rough figures."
- "turn on the camera" -> continuous preview in the Webcam Feed panel with
  periodic detections in Live Activity (fixed action `camera_stream`, L3:
  one "confirm", spec F9 "Continuous"). "turn off the camera", Stop, Sleep
  and closing JARVIS end it.

Frames are never saved. Live Activity shows what was detected and how sure
the detector was (spec: "with its confidence"); she says the names only.
"""

from __future__ import annotations

import re
import threading
import time
from collections import Counter, deque
from datetime import date
from typing import Callable, List, Optional, Sequence

from jarvis.actions.command_text import clean_command
from jarvis.interpreter import ActionRequest, Answer
from jarvis.settings import SETTINGS
from jarvis.vision.camera import Camera, CameraProblem, CameraStream
from jarvis.vision.detector import COCO_CLASSES, Detection, DetectorMissing, ObjectDetector, vote
from jarvis.vision.prices import load_prices, price_sentence

_CFG = SETTINGS["camera"]
_REPLIES = _CFG["replies"]

_NUMBERS = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine"}
_PLURALS = {"person": "people", "knife": "knives", "mouse": "mice", "bus": "buses", "couch": "couches",
            "glass": "glasses", "wine glass": "wine glasses", "skis": "pairs of skis", "scissors": "pairs of scissors",
            "sandwich": "sandwiches", "dining table": "dining tables", "toothbrush": "toothbrushes", "bench": "benches"}


_ARTICLES = {"the", "a", "an", "my", "your"}


def phrase_key(text: str) -> str:
    """The same clean-up the interpreter gives a command, then without
    articles: "What can you see" and "can you please turn on camera" were
    sent online on because "can you" is removed before the
    camera matcher sees them ("what see", "turn on camera")."""
    cleaned = clean_command(str(text).replace("’", "'")).rstrip(".!?")
    return " ".join(w for w in cleaned.split() if w not in _ARTICLES)


def _phrases(*keys: str) -> set:
    return {phrase_key(p) for key in keys for p in _CFG.get(key, [])}


_LOOK, _PRICE = _phrases("look_phrases", "look_more_phrases"), _phrases("price_phrases")
# ("What do you see in front of you?" asked "Did you
# mean...?", "Have a look at this" read the SCREEN): words after a look
# phrase that change nothing.
_LOOK_TAIL = re.compile(r"(?:\s+(?:in front of (?:you|me|camera|webcam)|right now|now|here|there|please|for me|"
                        r"at (?:this|that|it|me)|on desk|around me|in room|through (?:camera|webcam)|with (?:camera|webcam)))+$")
_ON, _OFF = _phrases("stream_on_phrases"), _phrases("stream_off_phrases")
_SYNONYMS = {" ".join(str(k).lower().split()): str(v) for k, v in _CFG.get("synonyms", {}).items()}
_FOLLOW_UP_NAMES = {str(k).lower(): [str(v) for v in vs] for k, vs in _CFG.get("follow_up_names", {}).items()}
_HOW_MUCH = re.compile(r"^(?:how much (?:does|do|is|are|would|will|did) (?P<a>.+?)(?: cost| be| worth)?|what (?:does|do) (?P<b>.+?) cost|"
                       r"what(?:'s| is) (?P<c>.+?) worth|price of (?P<d>.+?))$")
_WHERE = re.compile(r"^(?:where(?:'s| is| are) (?P<a>.+?)(?: on (?:my )?(?:right|left).*| (?:left|right).*)?|"
                    r"is (?P<b>.+?) on (?:my )?(?:the )?(?:left|right).*|(?P<c>.+?) (?:left|right) or (?:left|right)|"
                    r"(?:on )?(?:my |the )?(?:left|right) or (?:on )?(?:my |the )?(?:left|right))$")
# What a follow-up may be about -- "it"/"this", or something
# the detector knows. "where is Paris" is still an ordinary question.
_PRONOUNS = {"", "it", "this", "that", "they", "them", "these", "those"}
_KNOWN = set(COCO_CLASSES) | set(_SYNONYMS.values())


# Looser follow-ups ("How much is this cost, the cell phone").
_PRICE_WORDS = re.compile(r"\b(?:how much|cost|costs|price|priced|pricing|worth)\b")
_WHERE_WORDS = re.compile(r"\b(?:where|left|right)\b")
_JUST_SAW = re.compile(r"\b(?:you\s+)?(?:just\s+)?(?:saw|see|seen|spotted|found|detected)\b")
_WHO = re.compile(r"\b(?:tell me about|who(?:'s| is| are)?|describe|recogni[sz]e|identify|know)\s+(?:that|this|the)\s+"
                  r"(?:person|man|woman|guy|girl|boy|lady|people|face)\b|\bwho(?:'s| is) (?:that|this|he|she)\b")
_LABELS_LONGEST = sorted(set(_KNOWN) | set(_SYNONYMS), key=len, reverse=True)
# What the detector can never tell ([camera] brand_words: the
# brand -> the object it is a brand of), and judging people.
_BRAND_LABEL = {str(k).lower(): str(v) for k, v in _CFG.get("brand_words", {}).items()}
_BRAND = re.compile(r"\b(?:" + "|".join(sorted((re.escape(b) for b in _BRAND_LABEL), key=len, reverse=True)) + r")\b") \
    if _BRAND_LABEL else re.compile(r"(?!)")
_GENERIC_BRAND = {str(w).lower() for w in _CFG.get("generic_brand_words", [])}
_ABOUT_THE_CAMERA = {"it", "this", "that", "holding", "hold", "held", "hand", "see", "saw", "seen", "camera", "front", "showing",
                     "these", "those", "am"}
_JUDGE_PERSON = re.compile(r"\b(?:person|people|man|woman|guy|girl|boy|he|she|him|her|they|them)\b.*\b(?:good|bad|nice|kind|happy|sad|"
                           r"angry|old|young|pretty|ugly|handsome|beautiful|name|age|safe|dangerous|friendly|mood|feeling|"
                           r"trustworthy|smart|rich|famous)\b|\b(?:good|bad|nice|kind|old|young|pretty|safe|dangerous|happy|sad|"
                           r"angry|friendly)\b.*\b(?:person|man|woman|guy|girl|boy|he|she)\b|"
                           # "is the person boy or girl" -- gender, age, race.
                           r"\b(?:person|people|he|she|they)\b.*\b(?:boy|girl|man|woman|male|female|gender|sex|race|"
                           r"ethnicity|years|look like)\b")
_THE_PERSON = re.compile(r"\b(?:the|that|this)\s+(?:person|man|woman|guy|girl|boy)\b")
_TELL_ABOUT = re.compile(r"(?:tell me|talk|say something|tell)\s+(?:more\s+)?about\s+(?P<thing>.+?)(?:\s+you\s+(?:just\s+)?(?:saw|see|seen))?")
# (after a look: "Make it Max" -> "I can tell it's a
# refrigerator, but not the brand or model"; "Make it to 90", "Make it louder"
# -> "Ask me 'what do you see' first"). "make" is a brand word ("what make is
# it?") and "it" pointed at the last look, so an ORDER was read as a camera
# question. An order verb opening the sentence, or a volume/brightness word
# anywhere, is never a camera follow-up: the verb wins over the pronoun.
_ORDER = re.compile(r"^(?:(?:can|could|would|will) you |please |just |now )*(?:make|turn|set|put|crank|bump|pump|raise|lower|"
                    r"increase|decrease|reduce|boost|change|adjust|switch|mute|unmute|open|close|play|stop|start)\b|"
                    r"\b(?:louder|quieter|softer|volume|sound|brightness|brighter|dimmer|darker|max|maximum|minimum|percent)\b|%")
# Visual questions about the last look stay local ("Where is the
# cap? Left or right" went online and Groq said "The cap is on the left").
_IS_THERE = re.compile(r"(?:is there|are there|do you see|can you see|did you see|you see)\s+(?:a |an |any |the |my )?"
                       r"(?P<thing>[a-z][a-z ]{0,30}?)(?:\s+(?:there|here|in front of you|on my desk|in the picture|anywhere))?")


def _named_label(sentence: str) -> Optional[str]:
    """The detector's label for an object named anywhere in the sentence
    ("the cell phone", "phones", "bottle"), or None. Never "person"."""
    for name in _LABELS_LONGEST:
        if re.search(rf"\b{re.escape(name)}(?:s|es)?\b", sentence):
            label = _SYNONYMS.get(name, name)
            return None if label == "person" else label
    return None


def _osa(a: str, b: str) -> int:
    """Edit distance where swapping two neighbouring letters is one edit
    ("bottel" -> "bottle")."""
    d = [[i + j if i * j == 0 else 0 for j in range(len(b) + 1)] for i in range(len(a) + 1)]
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def _close(said: str, name: str) -> bool:
    """A mishearing of a detector name: same first letter, one edit (two
    for 8+ letters). "cap" -> "cup", "bottel" -> "bottle"."""
    if said == name:
        return True
    if said[:1] != name[:1] or abs(len(said) - len(name)) > 2 or len(said) < 3:
        return False
    return _osa(said, name) <= (2 if len(name) >= 8 else 1)


def fuzzy_label(words: str, prefer: Sequence[str] = ()) -> Optional[str]:
    """The detector label his words most likely name: an exact name first,
    then a mishearing, where a label from `prefer` (the last look) wins --
    "cap" is the cup she saw rather than a cat she didn't. None if nothing
    is close. Never "person"."""
    words = " ".join(w for w in words.split() if w not in _ARTICLES and w not in ("this", "that", "it", "any", "some"))
    exact = _named_label(words) if words else None
    if exact is not None:
        return exact
    for word in words.split():  # "the water" -> the bottle she saw ([camera.follow_up_names])
        options = _FOLLOW_UP_NAMES.get(word) or _FOLLOW_UP_NAMES.get(word.rstrip("s"))
        if options:
            return next((o for o in options if o in prefer), options[0])
    tokens = words.split()
    chunks = [" ".join(tokens[i:i + n]) for n in (2, 1) for i in range(len(tokens) - n + 1)]
    chunks += [c[:-1] for c in chunks if c.endswith("s")]
    hits = [_SYNONYMS.get(name, name) for chunk in chunks for name in _LABELS_LONGEST
            if (" " in name) == (" " in chunk) and _close(chunk, name)]
    hits = [h for h in hits if h != "person"]
    if not hits:
        return None
    return next((h for h in hits if h in prefer), hits[0])


def _label_for(words: str) -> str:
    words = " ".join(w for w in words.split() if w not in _ARTICLES and w not in ("this", "that", "it"))
    return _SYNONYMS.get(words, words[:-1] if words.endswith("s") and words[:-1] in _KNOWN else words)


def with_article(label: str) -> str:
    """'a bottle' / 'an apple'."""
    return f"{'an' if label[0] in 'aeiou' else 'a'} {label}"


def plural(label: str) -> str:
    """The plural of a detector label ('cups', 'people')."""
    return _PLURALS.get(label, label + ("es" if label.endswith(("s", "x", "ch", "sh")) else "s"))


def describe(detections: List[Detection]) -> str:
    """"a laptop, two cups and a person" (most confident first)."""
    # Collections: Counter tallies detections per label ('two cups').
    counts = Counter(d.label for d in detections)
    order = list(dict.fromkeys(d.label for d in detections))
    parts = [with_article(label) if counts[label] == 1 else f"{_NUMBERS.get(counts[label], counts[label])} {plural(label)}"
             for label in order]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


class VisionCommands:
    """F9: 'what do you see' (one look, detected locally), follow-ups about what she saw, rough prices, and
    the live preview."""
    def __init__(self, camera: Optional[Camera] = None, detector: Optional[ObjectDetector] = None,
                 status: Callable[[str], None] = lambda _t: None, today: Callable[[], date] = date.today,
                 price_table: Optional[dict] = None) -> None:
        self.camera = camera or Camera()
        self.detector = detector or ObjectDetector()
        self._status = status  # Live Activity lines outside a command (the stream's detections)
        self._today = today
        self._prices = price_table
        self.stream: Optional[CameraStream] = None
        self._orchestrator = None
        self._unload_timer: Optional[threading.Timer] = None
        # The last look: (when, detections, frame width). Labels and boxes
        # only -- the frame itself is never kept.
        self._last: Optional[tuple] = None
        self._clock = time.monotonic
        # The live camera's last few detections, voted on like a look's frames.
        self._stream_recent: deque = deque(maxlen=max(1, int(_CFG["look_frames"])))

    def register(self, interpreter, orchestrator) -> None:
        """Register the camera matcher and its executors."""
        self._orchestrator = orchestrator
        interpreter.register("camera", self.matcher)
        orchestrator.register_executor("capture_camera", self.look_executor)
        orchestrator.register_executor("camera_stream", self.stream_executor)
        orchestrator.controller.add_transition_listener(self.on_transition)

    def _report(self, step: str) -> None:
        if self._orchestrator is not None:
            self._orchestrator.report_step(step)

    def on_transition(self, entry) -> None:
        """Stop and Sleep end the continuous camera (spec F9)."""
        from jarvis.controller import EventType

        if entry.event_type is None or (entry.event_type == EventType.GO_TO_SLEEP and entry.accepted):
            self.stop_stream()

    def stop_stream(self) -> bool:
        """Stop the live preview if it runs. True if it was running."""
        if self.stream is None or not self.stream.running:
            return False
        self.stream.stop()
        self.stream = None
        self._status("camera off")
        return True

    # -- matching -------------------------------------------------------------------------

    def matcher(self, cleaned: str):
        """Camera commands and follow-ups about the last look. None otherwise."""
        sentence = phrase_key(cleaned)
        if sentence in _OFF:
            return Answer(str(_REPLIES["stream_off" if self.stop_stream() else "already_off"]))
        if sentence in _ON:
            if self.stream is not None and self.stream.running:
                return Answer(str(_REPLIES["already_on"]))
            return ActionRequest("camera_stream", {"display_name": "the camera, kept on with a live preview"},
                                 prompt=str(_REPLIES["stream_prompt"]))
        if sentence in _PRICE:
            return ActionRequest("capture_camera", {"purpose": "price", "display_name": "the camera"})
        if sentence in _LOOK or _LOOK_TAIL.sub("", sentence) in _LOOK:
            return ActionRequest("capture_camera", {"purpose": "look", "display_name": "the camera"})
        said =" ".join(cleaned.lower().replace("’", "'").split()).rstrip(".!?")
        if _ORDER.search(said):
            return None  # "make it louder": the volume's, never the camera's
        return self.follow_up(said)

    # -- follow-ups about the last look --------------------------------------

    def recent(self) -> Optional[tuple]:
        """(detections, frame width) from a look in the last [camera]
        follow_up_seconds, else None."""
        if self._last is None or self._clock() - self._last[0] > float(_CFG["follow_up_seconds"]):
            return None
        return self._last[1], self._last[2]

    def _find(self, label: str, detections: List[Detection]) -> Optional[Detection]:
        squashed = label.replace(" ", "")
        found = [d for d in detections if d.label == label or d.label.replace(" ", "") == squashed]
        return max(found, key=lambda d: d.confidence) if found else None

    def follow_up(self, sentence: str):
        """"how much does the cellphone cost", "how much is it", "left or
        right", "where is the refrigerator? on my right or my left" -- about
        the last look, always answered locally: with no look
        in the last follow_up_seconds she says to look first, and never
        sends it online. Only "where is Paris"-style questions about
        something the detector can't know go on as ordinary questions."""
        sentence = sentence.replace("?", " ").replace(",", " ")
        sentence = " ".join(sentence.split())
        if _WHO.search(sentence):
            return Answer(str(_REPLIES["no_faces"]))  # No face recognition, ever
        limited = self._limits(sentence)
        if limited is not None:
            return limited
        price = _HOW_MUCH.fullmatch(sentence)
        match = price or _WHERE.fullmatch(sentence)
        if match is None:
            return self._is_there(sentence) or self._loose_follow_up(sentence)
        said_thing = next((g for g in match.groups() if g), "")
        label = _label_for(said_thing)
        if label not in _PRONOUNS and label not in _KNOWN and self.recent() is not None:
            # "How much is the water" after a look at a bottle went
            # online -- a name for something she saw ([camera.follow_up_names]).
            seen = fuzzy_label(said_thing, [d.label for d in self.recent()[0]])
            label = seen if seen is not None and self._find(seen, self.recent()[0]) is not None else label
        if price is not None and label not in _PRONOUNS and label not in _KNOWN:
            # ("How much does the portal cost" after a
            # look at a bottle went online): "the <word she doesn't know>" right
            # after a look with ONE main object is that object (Whisper's slip).
            main = self._only_object(said_thing)
            if main is not None:
                return Answer(self._price([main]))
        if label not in _PRONOUNS and label not in _KNOWN:
            visual = self._visual_where(said_thing) if price is None else None
            return visual if visual is not None else self._loose_follow_up(sentence)
        recent = self.recent()
        if recent is None:
            return Answer(str(_REPLIES["look_first"]))
        detections, width = recent
        if label in _PRONOUNS:
            if not detections:
                return Answer(str(_REPLIES["follow_up_nothing"]))
            if price is not None:
                return Answer(self._price(detections))
            objects = [d for d in detections if d.label != "person"] or detections
            hit = max(objects, key=lambda d: (d.confidence, d.area))  # what he is most likely asking about
        else:
            hit = self._find(label, detections)
            if hit is None:
                return Answer(str(_REPLIES["where_unknown"]).format(thing=label))
            if price is not None:
                return Answer(self._price([hit]))
        return Answer(str(_REPLIES["where"]).format(thing=hit.label, side=self._side(hit, width)))

    def _only_object(self, said_thing: str) -> Optional[Detection]:
        """The one thing (not a person) of the last look, when he names it
        with "the/this/that/my" and one or two words; None otherwise."""
        recent = self.recent()
        words = said_thing.split()
        if recent is None or len(words) < 2 or len(words) > 3 or words[0] not in ("the", "this", "that", "my"):
            return None
        objects = [d for d in recent[0] if d.label != "person"]
        if len({d.label for d in objects}) != 1:
            return None
        return max(objects, key=lambda d: (d.confidence, d.area))

    def _from_last_look(self, thing: str):
        """A visual question about an object, answered from the
        last look: the name matched to what the detector knows ("cap" -> the
        cup she saw, "bottel" -> bottle). (hit, width, label or his words),
        or None without a look in the last follow_up_seconds."""
        recent = self.recent()
        thing = " ".join(w for w in thing.split() if w not in _ARTICLES)
        if recent is None or not thing or len(thing.split()) > 3:
            return None
        detections, width = recent
        label = fuzzy_label(thing, [d.label for d in detections])
        hit = self._find(label, detections) if label is not None else None
        if hit is not None and hit.label != thing:
            self._report(f"understood '{thing}' as {hit.label} (from the last look)")
        return hit, width, label or thing

    def _visual_where(self, thing: str):
        """"Where is the cap? Left or right" within follow_up_seconds of a
        look (it went online and Groq said "The cap is on the left"): local,
        always. Not seen -> "I didn't see a cap in my last look."."""
        found = self._from_last_look(thing)
        if found is None:
            return None
        hit, width, named = found
        if hit is None:
            return Answer(str(_REPLIES["where_unknown"]).format(thing=named))
        return Answer(str(_REPLIES["where"]).format(thing=hit.label, side=self._side(hit, width)))

    def _is_there(self, sentence: str):
        """"is there a bottle", "do you see a cup" after a look: from the
        look, locally."""
        match = _IS_THERE.fullmatch(sentence)
        found = self._from_last_look(match.group("thing").strip()) if match is not None else None
        if found is None:
            return None
        hit, width, named = found
        if hit is None:
            return Answer(str(_REPLIES["where_unknown"]).format(thing=named))
        where = str(_REPLIES["where"]).format(thing=hit.label, side=self._side(hit, width))
        return Answer(str(_REPLIES["just_saw"]).format(thing=with_article(hit.label), where=where))

    def _limits(self, sentence: str):
        """"Is it iPhone", "Am I
        holding an iPhone" -> she can tell the object, never its brand or
        model; "is the person good" -> she never judges or identifies
        people; "Tell me about the cell phone" -> what she saw, where, and a
        rough price. Only about the camera: a brand needs "it/this/holding/
        see..." with it ("what's the newest iPhone" is a normal question)."""
        words = set(sentence.split())
        recent = self.recent()
        brand = _BRAND.search(sentence)
        generic = words & _GENERIC_BRAND
        # An
        # object from the last look, named, is about the camera too.
        named = _named_label(sentence) if recent is not None else None
        seen = self._find(named, recent[0]) if named is not None else None
        if (brand is not None or generic) and (words & _ABOUT_THE_CAMERA or seen is not None):
            if recent is None:
                return Answer(str(_REPLIES["look_first"]))
            if brand is not None:
                label = _BRAND_LABEL[brand.group(0)]
                hit = self._find(label, recent[0])
            elif seen is not None:
                hit, label = seen, seen.label
            else:  # "what brand is it": the biggest thing she saw that isn't a person
                things = [d for d in recent[0] if d.label != "person"]
                if not things:
                    return Answer(str(_REPLIES["no_faces"]))
                hit = max(things, key=lambda d: d.area)
                label = hit.label
            if hit is None:
                return Answer(str(_REPLIES["where_unknown"]).format(thing=label))
            return Answer(str(_REPLIES["brand_unknown"]).format(thing=with_article(hit.label)))
        if _JUDGE_PERSON.search(sentence) and (recent is not None or _THE_PERSON.search(sentence)):
            return Answer(str(_REPLIES["no_faces"]))
        about = _TELL_ABOUT.fullmatch(sentence)
        if about is not None:
            label = _named_label(about.group("thing"))
            if label is None or recent is None:
                return None  # "tell me about the train strike" with no look is an ordinary question
            detections, width = recent
            hit = self._find(label, detections)
            if hit is None:
                return Answer(str(_REPLIES["where_unknown"]).format(thing=label))
            where = str(_REPLIES["where"]).format(thing=hit.label, side=self._side(hit, width))
            prices = self._prices if self._prices is not None else load_prices()
            entry = prices.get(hit.label)
            price = price_sentence(entry, self._today()) if entry is not None else ""
            return Answer(" ".join(str(_REPLIES["tell_about"]).format(thing=with_article(hit.label), where=where, price=price).split()))
        return None

    def _loose_follow_up(self, sentence: str):
        """"How much is this cost, the cell phone",
        "where it is left or right, the cell phone", "The cell phone you
        just see". Price words or left/right/where with "it"/"this" or an
        object the detector knows; "the <object> you just saw". About a
        named object she says to look first; a bare "it" with no recent
        look is left for the other matchers."""
        label = _named_label(sentence)
        pronoun = bool(set(sentence.split()) & (_PRONOUNS - {""}))
        price = _PRICE_WORDS.search(sentence) is not None
        where = _WHERE_WORDS.search(sentence) is not None
        just_saw = label is not None and _JUST_SAW.search(sentence) is not None
        if not (price or where or just_saw) or (label is None and not pronoun):
            return None
        recent = self.recent()
        if recent is None:
            return None  # no look in the last two minutes: "where is the train station" is not about the camera
        detections, width = recent
        if label is None:
            if not detections:
                return Answer(str(_REPLIES["follow_up_nothing"]))
            if price:
                return Answer(self._price(detections))
            objects = [d for d in detections if d.label != "person"] or detections
            hit = max(objects, key=lambda d: (d.confidence, d.area))
            return Answer(str(_REPLIES["where"]).format(thing=hit.label, side=self._side(hit, width)))
        hit = self._find(label, detections)
        if hit is None:
            return Answer(str(_REPLIES["where_unknown"]).format(thing=label))
        if price:
            return Answer(self._price([hit]))
        where_text = str(_REPLIES["where"]).format(thing=hit.label, side=self._side(hit, width))
        if just_saw and not where:
            return Answer(str(_REPLIES["just_saw"]).format(thing=with_article(hit.label), where=where_text))
        return Answer(where_text)

    @staticmethod
    def _side(hit: Detection, width: float) -> str:
        """Left, right or middle of the frame, from the box centre (the
        camera's own view: not mirrored)."""
        share = float(_CFG["side_share"])
        centre = (hit.box[0] + hit.box[2]) / 2 / max(1.0, float(width))
        if centre < share:
            return str(_REPLIES["side_left"])
        if centre > 1 - share:
            return str(_REPLIES["side_right"])
        return str(_REPLIES["side_middle"])

    # -- one look (L1) -----------------------------------------------------------------------

    def _steady(self, per_frame: List[List[Detection]]) -> List[Detection]:
        """Only what was found in most frames ([camera] look_min_votes of
        look_frames) at [camera] min_confidence or more; fewer frames than
        votes (a camera that gave one frame) needs every frame."""
        votes = min(int(_CFG["look_min_votes"]), len(per_frame))
        found = vote(per_frame, votes, float(_CFG["min_confidence"]))
        return found[: int(_CFG["max_objects_spoken"])]

    def _detect_frames(self, frames) -> List[Detection]:
        per_frame = [self.detector.detect(frame) for frame in frames]
        found = self._steady(per_frame)
        kept = {d.label for d in found}
        flickers = sorted({f"{d.label} {d.confidence:.2f}" for frame in per_frame for d in frame if d.label not in kept})
        if flickers:
            self._report(f"not steady across {len(frames)} frames, dropped: " + ", ".join(flickers))
        return found

    def _stream_detect(self, frame) -> List[Detection]:
        """The live camera's detections are the last look too (labels and
        boxes only), so follow-ups and "what can you see" use them. Voted
        over its last few detections, like a look's frames."""
        self._stream_recent.append(self.detector.detect(frame))
        found = self._steady(list(self._stream_recent))
        width = float(frame.shape[1]) if hasattr(frame, "shape") else 1.0
        self._last = (self._clock(), list(found), width)
        return found

    def look_executor(self, action_name: str, params: dict):
        """L1: take one look (a few frames), detect objects locally, and say what is there or its price."""
        if self.stream is not None and self.stream.running:
            # "What can you see" with the camera on said "turn off
            # the camera first". It now answers from the live stream's latest look.
            recent = self.recent()
            if recent is None:
                text = str(_REPLIES["stream_no_look_yet"])
                return False, text, text
            detections = recent[0]
            self._report("from the live camera: " + (", ".join(f"{d.label} {d.confidence:.2f}" for d in detections) or "nothing I know"))
            text = self._price(detections) if params.get("purpose") == "price" else self._seen(detections)
            return True, text, text
        try:
            look = self.camera.one_look()
        except CameraProblem as problem:
            return False, str(problem), str(problem)
        for event in self.camera.events:
            self._report(event)
        self.camera.events.clear()
        frames = look.frames or [look.frame]
        self._report(f"{_CFG['warmup_frames']} warm-up frames thrown away, {len(frames)} kept; camera released before analysis")
        try:
            width = float(look.frame.shape[1]) if hasattr(look.frame, "shape") else 1.0
            detections = self._detect_frames(frames)
        except DetectorMissing as missing:
            return False, str(missing), str(missing)
        finally:
            look.frame = None  # the frames are never kept
            look.frames = []
            del frames
            self._schedule_unload()
        self._last = (self._clock(), list(detections), width)  # labels and boxes only, for follow-ups
        note = getattr(self._orchestrator, "note_topic", None)
        if note is not None:
            note("camera")
        self._report("detected: " + (", ".join(f"{d.label} {d.confidence:.2f}" for d in detections) or "nothing I know"))
        text = self._price(detections) if params.get("purpose") == "price" else self._seen(detections)
        return True, text, text

    def _seen(self, detections: List[Detection]) -> str:
        """Steady but below [camera] sure_confidence -> "I think
        I see ..."."""
        if not detections:
            return str(_REPLIES["nothing"])
        sure_at = float(_CFG["sure_confidence"])
        unsure_labels = {d.label for d in detections if d.label not in {s.label for s in detections if s.confidence >= sure_at}}
        sure = [d for d in detections if d.label not in unsure_labels]
        unsure = [d for d in detections if d.label in unsure_labels]
        if not unsure:
            return str(_REPLIES["seen"]).format(things=describe(sure))
        if not sure:
            return str(_REPLIES["seen_unsure"]).format(things=describe(unsure))
        return str(_REPLIES["seen_mixed"]).format(sure=describe(sure), unsure=describe(unsure))

    def _price(self, detections: List[Detection]) -> str:
        objects = [d for d in detections if d.label != "person"]
        if not objects:
            return str(_REPLIES["price_only_person" if detections else "price_nothing"])
        main = max(objects, key=lambda d: (d.confidence, d.area))  # what he is most likely holding up
        prices = self._prices if self._prices is not None else load_prices()
        entry = prices.get(main.label)
        if entry is None:
            return str(_REPLIES["price_unknown"]).format(thing=with_article(main.label))
        self._report(f"price table: {main.label}, {entry.low:g}-{entry.high:g} {entry.currency}, {entry.updated or 'no date'}, {entry.source}")
        return f"{str(_REPLIES['looks_like']).format(thing=with_article(main.label))} {price_sentence(entry, self._today())}"

    def _schedule_unload(self) -> None:
        """R15: drop the detector from memory once it hasn't been used for a while."""
        if self._unload_timer is not None:
            self._unload_timer.cancel()
        self._unload_timer = threading.Timer(float(_CFG["unload_after_idle_seconds"]) + 1, self.detector.unload_if_idle)
        self._unload_timer.daemon = True
        self._unload_timer.start()

    # -- continuous (L3) ----------------------------------------------------------------------

    def stream_executor(self, action_name: str, params: dict):
        """L3: start the live preview after his confirm."""
        problems: List[str] = []
        stream = CameraStream(
            self.camera,
            detect=self._stream_detect,
            on_detections=lambda found: self._status(
                "camera sees: " + (", ".join(f"{d.label} {d.confidence:.2f}" for d in found) or "nothing I know")),
            on_problem=problems.append,
        )
        stream.start()
        # R12: on only once a frame has really arrived.
        waited = 0.0
        while waited < float(_CFG["stream_start_timeout_seconds"]) and stream.latest_jpeg() is None and stream.running:
            threading.Event().wait(0.05)
            waited += 0.05
        if stream.latest_jpeg() is None:
            stream.stop()
            text = problems[0] if problems else str(SETTINGS["setup_messages"]["camera_no_frame"])
            return False, text, text
        self.stream = stream
        self._report("camera on: live preview on the dashboard, frames never saved")
        text = str(_REPLIES["stream_on"])
        return True, text, text
