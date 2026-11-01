"""F20 Topic monitor (built).

- "track <topic>" / "stop tracking <topic>" -> fixed action
  `edit_topic_list` (L0: his own words, local, read back after saving,
  R12). "what am I tracking" is answered locally.
- Stored in jarvis_data/topics.json ([topics] store_path): each topic, the
  day it was added, and hashes of the headline URLs already spoken (at most
  [topics] max_seen_per_topic). Headline text is never stored. At most
  [topics] max_topics topics. Writes are atomic and pass R9; a damaged
  file is kept as topics.corrupt-<time>.json, never silently deleted.
- Checked online ONLY on request: "any news on my topics" / "check my
  topics", or inside a briefing he asked for. The first-wake
  check was removed. No timer, no thread, no scheduler in this module.
- Topics are his own words (C5, R4's amendment): no preview. They are
  shown in Live Activity as the words sent (R3), but only for the session:
  debug.log gets type, size, destination, time and hash (R4), and the
  spoken headlines go to the record as a summary (Answer.logged).
- New = an article URL not in that topic's seen hashes. Hashes are added
  only after the headlines were spoken; a Stop means nothing is marked.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from jarvis.actions.command_text import edit_distance, pick_index
from jarvis.interpreter import ActionRequest, Answer
from jarvis.memory.sensitive import sensitive_type
from jarvis.news.feeds import DESTINATION_TOPICS, SERVICE_NAME, Headline, NewsClient
from jarvis.online import OnlineError, OnlineErrorKind
from jarvis.security import PathIntent, check_path
from jarvis.settings import SETTINGS

logger = logging.getLogger(__name__)

_CFG = SETTINGS["topics"]
_NEWS = SETTINGS["news"]
_REPLIES = _CFG["replies"]
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CHECKING_ONLINE_TEXT = str(SETTINGS["speech"]["checking_online_text"])


def default_topics_path() -> Path:
    """Where topics.json lives in jarvis_data."""
    return _PROJECT_ROOT / str(_CFG["store_path"])


def _phrase(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9']+", text.lower().replace("’", "'")))


def _prefixes(key: str) -> List[str]:
    return sorted({_phrase(p) for p in _CFG[key]}, key=len, reverse=True)


_ADD = _prefixes("add_prefixes")
_REMOVE = _prefixes("remove_prefixes")
_LIST = {_phrase(p) for p in _CFG["list_phrases"]}
_CHECK = {_phrase(p) for p in _CFG["check_phrases"]}
_NOT_TOPICS = {_phrase(p) for p in _CFG["not_topics"]}
_SOUND = {ch: group[0] for group in _CFG.get("sound_alike", []) for ch in str(group)}
_SEPARATORS = sorted((_phrase(s) for s in _CFG.get("list_separators", [])), key=len, reverse=True)


def _sound(word: str) -> str:
    """"cricket" -> "criket" (k/c folded, doubles collapsed); "gritted" -> "critet"."""
    folded = [_SOUND.get(ch, ch) for ch in word]
    return "".join(ch for i, ch in enumerate(folded) if i == 0 or ch != folded[i - 1])


def same_topic(said: str, tracked: str) -> bool:
    """"indian cricut" / "indian gritted" are "indian
    cricket". Word by word, Whisper's slips allowed ([topics] fuzzy_*)."""
    a, b = _phrase(said).split(), _phrase(tracked).split()
    if not a or len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if x == y:
            continue
        if min(len(x), len(y)) < int(_CFG.get("fuzzy_min_chars", 4)):
            return False
        sx, sy = _sound(x), _sound(y)
        if sx[0] != sy[0] or edit_distance(sx, sy) > int(_CFG.get("fuzzy_max_distance", 2)):
            return False
    return True


def split_topics(rest: str) -> List[str]:
    """"indian gritted and indian cricut" -> both; commas too."""
    text = " " + rest.replace(",", " , ") + " "
    for sep in _SEPARATORS + [","]:
        text = text.replace(f" {sep} ", " | ")
    return [" ".join(part.split()) for part in text.split("|") if part.strip()]


def hash_text(text: str) -> str:
    """R4 log line: a short hash, never the value."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def join_and(items: List[str], word: str = "and") -> str:
    """'a, b and c' (or with another joining `word`)."""
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" {word} " + items[-1]


def short_title(title: str, max_words: Optional[int] = None) -> str:
    """A headline short enough to say: no trailing " - Publisher", at most
    [topics] title_max_words whole words, no closing full stop."""
    limit = int(_CFG["title_max_words"]) if max_words is None else max_words
    title = re.sub(r"\s+[-|–]\s+[^-|–]{2,40}$", "", " ".join(title.split()))
    words = title.split()
    return " ".join(words[:limit]).rstrip(" .,;:")


@dataclass
class Topic:
    """One tracked topic: its words, the day it was added, and hashes of headlines already said."""
    topic: str
    added_on: str  # ISO date
    seen: List[str] = field(default_factory=list)  # article-URL hashes, oldest first


class TopicStore:
    """jarvis_data/topics.json. Thread-safe; R9-checked; atomic writes."""

    def __init__(self, path: Optional[Path] = None, now: Callable[[], datetime] = datetime.now) -> None:
        self.path = path or default_topics_path()
        self._now = now
        self._lock = threading.Lock()
        self._topics: Optional[List[Topic]] = None
        self.load_note: Optional[str] = None  # said once if the file was damaged
        self.max_topics = int(_CFG["max_topics"])

    def topics(self) -> List[Topic]:
        """Copies of every tracked topic."""
        with self._lock:
            return [Topic(t.topic, t.added_on, list(t.seen)) for t in self._loaded()]

    def names(self) -> List[str]:
        """The tracked topics' names, in the order they were added."""
        return [t.topic for t in self.topics()]

    def find(self, topic: str) -> Optional[Topic]:
        """The topic with exactly this name (case and punctuation ignored), or None."""
        wanted = _phrase(topic)
        return next((t for t in self.topics() if _phrase(t.topic) == wanted), None)

    def find_like(self, topic: str) -> Optional[Topic]:
        """The tracked topic he means: the exact one, or one Whisper misheard."""
        return self.find(topic) or next((t for t in self.topics() if same_topic(topic, t.topic)), None)

    def reload(self) -> None:
        """Forget the cached list, so the next read comes from the file (R12 read-back)."""
        with self._lock:
            self._topics = None

    def add(self, topic: str) -> None:
        """Add a topic and save the file."""
        with self._lock:
            topics = self._loaded()
            topics.append(Topic(topic, self._now().date().isoformat()))
            self._write(topics)

    def remove(self, topic: str) -> bool:
        """Remove a topic and save. False if it wasn't tracked."""
        with self._lock:
            topics = self._loaded()
            kept = [t for t in topics if _phrase(t.topic) != _phrase(topic)]
            if len(kept) == len(topics):
                return False
            self._write(kept)
            self._topics = kept
            return True

    def mark_seen(self, keys: Dict[str, List[str]]) -> None:
        """Add spoken headlines' hashes, per topic; oldest dropped past the cap."""
        cap = int(_CFG["max_seen_per_topic"])
        with self._lock:
            topics = self._loaded()
            for t in topics:
                for key in keys.get(t.topic, []):
                    if key not in t.seen:
                        t.seen.append(key)
                t.seen = t.seen[-cap:]
            self._write(topics)

    # -- file -----------------------------------------------------------------

    def _loaded(self) -> List[Topic]:
        if self._topics is None:
            self._topics = self._read()
        return self._topics

    def _read(self) -> List[Topic]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return [Topic(str(t["topic"]), str(t.get("added_on", "")), [str(s) for s in t.get("seen", [])]) for t in data["topics"]]
        except (OSError, ValueError, KeyError, TypeError):
            damaged = self.path.with_name(f"{self.path.stem}.corrupt-{self._now():%Y%m%d-%H%M%S}.json")
            try:
                allowed, reason = check_path(str(damaged), PathIntent.WRITE)  # R9: a rename is a change
                if not allowed:
                    raise PermissionError(reason)
                os.replace(self.path, damaged)  # kept, never deleted
            except OSError:
                damaged = self.path
            logger.warning("topics file damaged; kept as %s, starting an empty list", damaged.name)
            self.load_note = str(_REPLIES["corrupt"]).format(name=damaged.name)
            return []

    def _write(self, topics: List[Topic]) -> None:
        allowed, reason = check_path(str(self.path), PathIntent.WRITE)  # R9
        if not allowed:
            raise PermissionError(reason)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        partial = self.path.with_name(self.path.name + ".partial")
        body = {"version": 1, "topics": [{"topic": t.topic, "added_on": t.added_on, "seen": t.seen} for t in topics]}
        partial.write_text(json.dumps(body, indent=1), encoding="utf-8")
        os.replace(partial, self.path)


@dataclass
class CheckResult:
    """What one check found. `new` keeps the topics' order."""

    new: Dict[str, List[Headline]] = field(default_factory=dict)
    failed: List[str] = field(default_factory=list)
    error: Optional[OnlineError] = None  # set when no topic could be checked
    stopped: bool = False

    def keys(self) -> Dict[str, List[str]]:
        """The new headlines' keys per topic, to mark them as seen."""
        return {topic: [h.key for h in heads] for topic, heads in self.new.items()}

    @property
    def headline_count(self) -> int:
        """How many new headlines were found in total."""
        return sum(len(h) for h in self.new.values())


def spoken_summary(result: CheckResult) -> str:
    """"2 new headlines on Indian cricket: <title 1>. <title 2>. Nothing
    new on solar." -- or the failure sentence."""
    if result.error is not None and not result.new:
        return failure_text(result.error)
    parts = []
    quiet = []
    for topic, heads in result.new.items():
        if not heads:
            quiet.append(topic)
            continue
        titles = ". ".join(short_title(h.title) for h in heads)
        key = "new_one" if len(heads) == 1 else "new_many"
        parts.append(str(_REPLIES[key]).format(count=len(heads), topic=topic, titles=titles))
    if not parts and not result.failed:
        return str(_REPLIES["nothing_new_all"])
    if quiet:
        parts.append(str(_REPLIES["nothing_new_one"]).format(topics=join_and(quiet, "or")))
    if result.failed:
        parts.append(str(_REPLIES["some_failed"]).format(topics=join_and(result.failed)))
    return " ".join(parts)


def failure_text(error: OnlineError) -> str:
    """What she says when a news check fails (cooling down, offline, missing library, service error)."""
    if error.kind == OnlineErrorKind.COOLING or error.kind == OnlineErrorKind.RATE_LIMITED:
        until = datetime.fromtimestamp(error.until_wall).strftime("%H:%M") if error.until_wall else "later"
        return str(_REPLIES["cooling"]).format(until=until)
    if error.kind in (OnlineErrorKind.CANNOT_REACH, OnlineErrorKind.TIMEOUT):
        return str(_REPLIES["cannot_reach"])
    if error.kind == OnlineErrorKind.LIBRARY_MISSING:
        return str(SETTINGS["setup_messages"]["news_library_missing"])
    return str(_REPLIES["service_error"])


class TopicMonitor:
    """The online check, shared by "any news on my topics" and F21's
    briefing -- both only when he asks."""

    def __init__(self, store: TopicStore, client: NewsClient, sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], datetime] = datetime.now) -> None:
        self.store = store
        self.client = client
        self._sleep = sleep
        self._now = now

    def outbound(self) -> List[Tuple[str, str]]:
        """(destination, words sent) for each request a check would make --
        what a check sends."""
        if str(_NEWS["topic_source"]) == "feeds":
            return [("RSS", str(u)) for u in _NEWS["general_feed_urls"]]
        return [("News search RSS", t) for t in self.store.names()]

    def check(self, report: Callable[..., None], should_stop: Callable[[], bool],
              record: Callable[[str], None] = lambda _d: None) -> CheckResult:
        """One request per topic, [news] request_spacing_seconds apart, Stop
        checked between topics. `report(step, session_only=...)` is Live
        Activity; `record(detail)` the decision record (R3/R4)."""
        topics = self.store.topics()
        result = CheckResult()
        spacing = float(_NEWS["request_spacing_seconds"])
        mode_feeds = str(_NEWS["topic_source"]) == "feeds"
        general = None
        if mode_feeds:
            try:
                general, _failed = self.client.general()
            except OnlineError as error:
                result.error = error
                report(f"topics: {failure_text(error)}")
                return result
        details = []
        for n, topic in enumerate(topics, 1):
            if should_stop():
                result.stopped = True
                return result
            if n > 1 and not mode_feeds:
                self._sleep(spacing)
                if should_stop():
                    result.stopped = True
                    return result
            if not mode_feeds:
                # R3: the exact words sent, in Live Activity -- session only,
                # so the topic never reaches debug.log (R4).
                report(f'searching online, sending "{topic.topic}"', session_only=True)
                details.append(f"text, {len(topic.topic)} characters, sha256 {hash_text(topic.topic)}")
            try:
                found = self.client.topic(topic.topic, general)
            except OnlineError as error:
                report(f"topic {n}: unavailable ({error.kind.name.lower().replace('_', ' ')})")
                result.failed.append(topic.topic)
                if result.error is None:
                    result.error = error
                if error.kind in (OnlineErrorKind.COOLING, OnlineErrorKind.RATE_LIMITED, OnlineErrorKind.CANNOT_REACH,
                                  OnlineErrorKind.LIBRARY_MISSING):
                    # The same for every topic: don't knock again.
                    result.failed.extend(t.topic for t in topics[n:])
                    break
                continue
            seen = set(topic.seen)
            fresh, keys = [], set()
            for h in found:
                if h.key not in seen and h.key not in keys:
                    fresh.append(h)
                    keys.add(h.key)
            fresh = fresh[: int(_CFG["max_headlines_per_topic"])]
            result.new[topic.topic] = fresh
            report(f"topic {n}: {len(fresh)} new")
        stamp = self._now().strftime("%Y-%m-%d %H:%M:%S")
        destination = "general RSS feeds, filtered locally" if mode_feeds else DESTINATION_TOPICS
        record(f"{destination}; {len(topics)} topic(s) sent: {'; '.join(details) or 'none'}; {stamp}")
        if result.new and result.error is not None and len(result.failed) < len(topics):
            result.error = None  # some topics answered: say those, then which failed
        return result


class TopicCommands:
    """F20's matchers and the edit_topic_list executor."""

    def __init__(self, monitor: TopicMonitor, online_status=None) -> None:
        self.monitor = monitor
        self.store = monitor.store
        self._online_status = online_status
        self._orchestrator = None
        # "stop tracking" alone asks which topic;
        # when it was asked (his answer, a name or a number, comes next).
        self._asked_which_at: Optional[float] = None

    def register(self, interpreter, orchestrator) -> None:
        """Before F16a/F4/F3 and the online conversation."""
        self._orchestrator = orchestrator
        interpreter.register("topics_list_remove", self.list_remove_matcher, raw=True)
        interpreter.register("topics", self.matcher)
        orchestrator.register_executor("edit_topic_list", self.edit_executor)

    # -- matching ---------------------------------------------------------------

    def list_remove_matcher(self, raw: str):
        """"stop tracking A, B and C": the interpreter cuts a sentence at its
        first comma, so a list with commas is read from his words as said."""
        if "," not in raw:
            return None
        from jarvis.actions.command_text import clean_command
        from jarvis.router.router import heard_as

        sentence = heard_as(_phrase(clean_command(raw.replace(",", " and "))))
        if not any(sentence.startswith(prefix + " ") for prefix in _REMOVE):
            return None
        return self.matcher(sentence)

    def matcher(self, cleaned: str):
        """F20: track, stop tracking, list and check topics (with Whisper's slips fixed first)."""
        from jarvis.router.router import heard_as  # the router's mishearing table ("precking", "trackingindian")

        sentence = heard_as(_phrase(cleaned))
        if not sentence:
            return None
        if sentence in _LIST or any(sentence.endswith(" " + p) for p in _LIST):
            return self._list()
        if sentence in _CHECK or any(sentence.endswith(" " + p) for p in _CHECK):
            return self.check_now()
        which = self._which_answer(sentence)
        if which is not None:
            return which
        for prefix in _REMOVE:
            if sentence == prefix or (sentence.startswith(prefix + " ") and self._clean_topic(sentence[len(prefix) + 1:]) in _NOT_TOPICS):
                return self._ask_which()  # "stop tracking" / "stop tracking it": never "forget tracking"
            if sentence.startswith(prefix + " "):
                return self._remove(sentence[len(prefix) + 1:])
        for prefix in _ADD:
            if sentence.startswith(prefix + " "):
                return self._add(sentence[len(prefix) + 1:])
        return None

    @staticmethod
    def _clean_topic(rest: str) -> str:
        words = rest.split()
        while words and words[0] in ("the", "news", "on", "about", "topic"):
            words = words[1:]
        while words and words[-1] in ("news", "please", "for", "me"):
            words = words[:-1]
        return " ".join(words)

    def _add(self, rest: str):
        topic = self._clean_topic(rest)
        if not topic or topic in _NOT_TOPICS:
            return None
        kind = sensitive_type(topic)
        if kind is not None:
            return Answer(str(_REPLIES["sensitive"]).format(kind=kind))
        if len(topic) > int(_CFG["max_topic_chars"]):
            return Answer(str(_REPLIES["too_long"]).format(max=int(_CFG["max_topic_chars"])))
        same = self.store.find_like(topic)
        if same is not None:  # "indian cricut" is the "indian cricket" he tracks: never a near-duplicate
            return Answer(str(_REPLIES["already"]).format(topic=same.topic))
        if len(self.store.topics()) >= self.store.max_topics:
            return Answer(str(_REPLIES["full"]).format(max=self.store.max_topics))
        return ActionRequest("edit_topic_list", {"op": "add", "topic": topic, "display_name": topic})

    def _remove(self, rest: str):
        topic = self._clean_topic(rest)
        if not topic or topic in _NOT_TOPICS:
            return None
        names = self.store.names()
        number = pick_index(topic)
        if number is not None and 1 <= number <= len(names):
            return self._remove_found([names[number - 1]], [])  # "stop tracking the second one"
        found, missing = [], []
        for part in split_topics(topic):
            match = self.store.find_like(self._clean_topic(part))
            if match is None:
                missing.append(self._clean_topic(part))
            elif match.topic not in found:
                found.append(match.topic)
        if not found:
            return Answer(str(_REPLIES["not_tracked"]).format(topic=join_and(missing) or topic))
        return self._remove_found(found, missing)

    def _remove_found(self, found: List[str], missing: List[str]) -> ActionRequest:
        self._release_which()
        return ActionRequest("edit_topic_list", {"op": "remove", "topic": found[0], "topics": found, "missing": missing,
                                                 "display_name": join_and(found)})

    def _ask_which(self) -> Answer:
        names = self.store.names()
        if not names:
            return Answer(str(_REPLIES["list_none"]))
        self._asked_which_at = time.monotonic()
        slot = getattr(self._orchestrator, "picks", None)
        if slot is not None:
            slot.claim(self)  # "Second" is a pick now: never dropped as too short
        return Answer(str(_REPLIES["which_remove"]).format(topics=join_and(names)),
                      logged=f"asked which of {len(names)} tracked topic(s) to stop (names not logged)")

    def _release_which(self) -> None:
        self._asked_which_at = None
        slot = getattr(self._orchestrator, "picks", None)
        if slot is not None:
            slot.release(self)

    def _which_answer(self, sentence: str):
        """Right after "which one should I stop tracking?": "second", "the
        second one", "number 2", "2", or a topic's name (Whisper's spelling too)."""
        asked = self._asked_which_at
        if asked is None or time.monotonic() - asked > float(_CFG.get("which_seconds", 30)):
            self._asked_which_at = None
            return None
        for prefix in _REMOVE:  # "stop tracking the second one" answers it too
            if sentence.startswith(prefix + " "):
                sentence = sentence[len(prefix) + 1:]
                break
        names = self.store.names()
        number = pick_index(sentence)
        if number is not None:
            if 1 <= number <= len(names):
                return self._remove_found([names[number - 1]], [])
            return Answer(str(_REPLIES["which_out_of_range"]).format(count=len(names)))
        if any(self.store.find_like(self._clean_topic(p)) for p in split_topics(self._clean_topic(sentence))):
            return self._remove(sentence)
        self._release_which()  # something else: the question is over
        return None

    def _list(self) -> Answer:
        names = self.store.names()
        if not names:
            return Answer(str(_REPLIES["list_none"]))
        key = "list_one" if len(names) == 1 else "list_many"
        text = str(_REPLIES[key]).format(count=len(names), topics=join_and(names))
        return Answer(text, logged=f"listed {len(names)} tracked topic(s) (names not logged)")

    # -- executor (L0, R12) -------------------------------------------------------

    def edit_executor(self, action_name: str, params: dict):
        """Add or remove topics, then read the file back to verify (R12). Returns (verified, what to say)."""
        op, topic = str(params.get("op")), str(params.get("topic", ""))
        try:
            if op == "add":
                self.store.add(topic)
            elif op == "remove":
                for name in params.get("topics") or [topic]:
                    self.store.remove(str(name))
            else:
                return False, str(_REPLIES["write_failed"])
        except (OSError, PermissionError):
            logger.exception("topics file not saved")
            return False, str(_REPLIES["write_failed"])
        self.store.reload()  # R12: read the file back
        there = self.store.find(topic) is not None
        if op == "add" and there:
            self._report(f"topic added: {topic}")
            return True, str(_REPLIES["added"]).format(topic=topic)
        removed = [str(n) for n in params.get("topics") or [topic]]
        if op == "remove" and not any(self.store.find(n) is not None for n in removed):
            self._report(f"topic removed: {join_and(removed)}")
            missing = [str(m) for m in params.get("missing") or ()]
            if missing:
                return True, str(_REPLIES["removed_and_missing"]).format(topic=join_and(removed), missing=join_and(missing))
            return True, str(_REPLIES["removed"]).format(topic=join_and(removed))
        return False, str(_REPLIES["write_failed"])

    # -- checking (L0: his own words, C5) -------------------------------------------

    def check_now(self) -> Answer:
        """"any news on my topics" -- only when he asks."""
        if self.store.load_note:
            note, self.store.load_note = self.store.load_note, None
            return Answer(note)
        topics = self.store.names()
        if not topics:
            return Answer(str(_REPLIES["check_none"]))
        cooling = self.monitor.client.service.cooling_until_wall()
        if cooling is not None:
            return Answer(failure_text(OnlineError(OnlineErrorKind.COOLING, until_wall=cooling)))
        orch = self._orchestrator
        self._report(f"checking online: news for {len(topics)} topic{'s' if len(topics) != 1 else ''}")
        if orch is not None:
            orch.announce(_CHECKING_ONLINE_TEXT)
        if self._online_status is not None:
            self._online_status.hold(SERVICE_NAME)
        try:
            result = self.monitor.check(self._report, self._stopped, self._record)
        finally:
            self._release()
        if result.stopped or self._stopped():
            return Answer("")
        text = spoken_summary(result)
        keys = result.keys()
        if orch is not None and any(keys.values()):
            # Marked seen only once they have been said, and not after Stop.
            orch.when_command_finishes(lambda: None if orch.controller.is_stop_requested() else self.store.mark_seen(keys))
        elif orch is None:
            self.store.mark_seen(keys)
        logged = (f"told you {result.headline_count} new headline(s) on {len(result.new)} topic(s); "
                  f"{len(result.failed)} unavailable (topics and headlines not logged)")
        return Answer(text, logged=logged)

    # -- helpers ------------------------------------------------------------------

    def _report(self, step: str, session_only: bool = False) -> None:
        if self._orchestrator is not None:
            self._orchestrator.report_step(step, session_only=session_only)

    def _record(self, detail: str) -> None:
        if self._orchestrator is not None:
            self._orchestrator.record_online_destination(detail)

    def _stopped(self) -> bool:
        return self._orchestrator is not None and self._orchestrator.controller.is_stop_requested()

    def _release(self) -> None:
        if self._online_status is None:
            return
        release = lambda: self._online_status.release(SERVICE_NAME)  # noqa: E731
        if self._orchestrator is not None:
            self._orchestrator.when_command_finishes(release)
        else:
            release()
