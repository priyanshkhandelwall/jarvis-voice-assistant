"""F21 Morning briefing (built).

When: ONLY on request -- "brief me", "morning briefing", "give me my
briefing" ([briefing] on_demand_phrases, or the router's "briefing"
intent). The first-wake-of-the-day trigger was removed
completely ("started by JARVIS: morning briefing (first wake of
the day)" right after "wake up"). Nothing here runs by itself.

Order (about [briefing] max_spoken_seconds, estimated from word count):
greeting (the wake greeting, or a time-of-day one on demand) -> the date ->
the weather for [weather] default_city (F16a's own WeatherClient, never
other weather code) -> the top general headlines (RSS) -> F20's new topic
headlines. Nothing from earlier sessions (F10 never stores conversations).

Trust (Section 5): he asked, and everything sent is his own words or
settings he typed for an online purpose (R4's amendment, C5): L0, no
preview. (The JARVIS-started `briefing_online` L3 action and its standing
approval existed only for the first wake, and went with it.)

Stop, or typed "skip" while it runs, ends it at once. Topic hashes are
marked seen only after they were said -- never after a crash or Stop.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from jarvis.actions.wake_greeting import time_band
from jarvis.actions.weather import CREDIT_LINE, WeatherClient, WeatherRequest, answer_text, choose_place
from jarvis.interpreter import Answer
from jarvis.news.feeds import SERVICE_NAME, Headline, NewsClient, feed_name
from jarvis.news.topics import CheckResult, TopicCommands, failure_text, short_title
from jarvis.online import OnlineError, OnlineErrorKind
from jarvis.settings import SETTINGS

logger = logging.getLogger(__name__)

_CFG = SETTINGS["briefing"]
_TEXTS = _CFG["texts"]
_NEWS = SETTINGS["news"]
_TOPIC_REPLIES = SETTINGS["topics"]["replies"]
_CHECKING_ONLINE_TEXT = str(SETTINGS["speech"]["checking_online_text"])
_OFFLINE = (OnlineErrorKind.CANNOT_REACH, OnlineErrorKind.TIMEOUT)

def _phrase(text: str) -> str:
    return " ".join(text.lower().replace("’", "'").replace("?", " ").replace(".", " ").replace(",", " ").split())


_ON_DEMAND = {_phrase(p) for p in _CFG["on_demand_phrases"]}
SKIP_WORDS = frozenset(_phrase(p) for p in _CFG["skip_words"])


def _ordinal(day: int) -> str:
    suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def date_sentence(now: datetime) -> str:
    """"It's Wednesday the 28th of October." -- the laptop's own clock."""
    return str(_TEXTS["date"]).format(weekday=f"{now:%A}", day=_ordinal(now.day), month=f"{now:%B}")


def greeting_sentence(now: datetime) -> str:
    """The briefing's opening greeting for the time of day, with his name."""
    return str(_TEXTS[f"greeting_{time_band(now.hour)}"]).format(name=SETTINGS["user"]["name"])


def words(text: str) -> int:
    """How many words `text` has."""
    return len(text.split())


@dataclass
class OnlinePart:
    """What the online part found, ready to be fitted into the time cap."""

    weather: Optional[str] = None
    weather_error: Optional[OnlineError] = None
    headlines: List[Headline] = field(default_factory=list)
    headlines_error: Optional[OnlineError] = None
    topics: Optional[CheckResult] = None


class Briefing:
    """F21: the morning briefing (greeting, date, weather, headlines, his topics), only when he asks for
    it."""
    def __init__(
        self,
        weather: WeatherClient,
        news: NewsClient,
        topics: Optional[TopicCommands] = None,
        now: Callable[[], datetime] = datetime.now,
        online_status=None,
    ) -> None:
        self.weather = weather
        self.news = news
        self.topics = topics
        self._now = now
        self._online_status = online_status
        self._orchestrator = None

    # -- registration --------------------------------------------------------------

    def register(self, interpreter, orchestrator) -> None:
        """Before F16a ("morning briefing" is not a weather question) and Groq."""
        self._orchestrator = orchestrator
        interpreter.register("briefing", self.matcher)
        orchestrator.add_skip_words(SKIP_WORDS)

    # -- the outbound list (Section 5) ----------------------------------------------------------

    def outbound(self) -> List[str]:
        """Every destination and every string the online part sends, in
        order. Hosts, not brand names."""
        lines: List[str] = []
        city = str(SETTINGS["weather"]["default_city"]).strip()
        if bool(_CFG["include_weather"]) and city:
            lines.append(f'api.open-meteo.com (weather): "{city}"')
        if bool(_CFG["include_headlines"]) or (bool(_CFG["include_topics"]) and str(_NEWS["topic_source"]) == "feeds"):
            lines.extend(f"{url} (headlines, no query)" for url in _NEWS["general_feed_urls"])
        if bool(_CFG["include_topics"]) and self.topics is not None and str(_NEWS["topic_source"]) != "feeds":
            lines.extend(f'www.bing.com/news (news search): "{t}"' for t in self.topics.store.names())
        return lines

    # -- on demand (L0) -------------------------------------------------------------------

    def matcher(self, cleaned: str):
        """'brief me' / 'morning briefing': build and say the briefing. None for anything else."""
        if _phrase(cleaned) not in _ON_DEMAND:
            return None
        now = self._now()
        intro = f"{greeting_sentence(now)} {date_sentence(now)}"
        if not self.outbound():
            return Answer(intro)
        part = self._fetch_online()
        if self._stopped():
            return Answer("")
        spoken, logged, keys = self._compose(intro, part)
        self._after_spoken(keys)
        if self._orchestrator is not None:
            self._orchestrator.skippable_until_spoken()  # "skip" while she says it ends it
        return Answer(spoken, logged=logged)

    # -- the online part -----------------------------------------------------------------------

    def _fetch_online(self) -> OnlinePart:
        if self._orchestrator is None:
            return self._fetch_online_inner()
        with self._orchestrator.skippable():  # typed "skip" = Stop while this runs
            return self._fetch_online_inner()

    def _fetch_online_inner(self) -> OnlinePart:
        orch = self._orchestrator
        part = OnlinePart()
        if orch is not None:
            orch.announce(_CHECKING_ONLINE_TEXT)  # R3, once
        if self._online_status is not None:
            self._online_status.hold(SERVICE_NAME)
        try:
            city = str(SETTINGS["weather"]["default_city"]).strip()
            if bool(_CFG["include_weather"]) and city and not self._stopped():
                self._report("checking online: weather, default city")
                try:
                    part.weather = self._weather(city)
                    self._report("weather ok")
                    self._report(CREDIT_LINE)
                except OnlineError as error:
                    part.weather_error = error
                    self._report(f"weather: {error.kind.name.lower().replace('_', ' ')}")
            if bool(_CFG["include_headlines"]) and not self._stopped():
                urls = [str(u) for u in _NEWS["general_feed_urls"]]
                self._report(f"checking online: headlines ({len(urls)} feeds)")
                try:
                    found, failed = self.news.general(urls)
                    for name in failed:
                        self._report(f"headlines: {name} unavailable")
                    part.headlines = self._top_general(found)
                    for h in part.headlines:
                        self._report(f"{h.source}: {h.title}")
                except OnlineError as error:
                    part.headlines_error = error
                    self._report(f"headlines: {error.kind.name.lower().replace('_', ' ')}")
            if bool(_CFG["include_topics"]) and self.topics is not None and self.topics.store.names() and not self._stopped():
                names = self.topics.store.names()
                self._report(f"checking online: news for {len(names)} topic{'s' if len(names) != 1 else ''}")
                part.topics = self.topics.monitor.check(self._report_any, self._stopped, lambda _d: None)
                for topic, heads in part.topics.new.items():
                    for h in heads:
                        self._report(f"{topic}: {h.title}", session_only=True)
        finally:
            self._release()
        self._record(part)
        return part

    def _weather(self, city: str) -> str:
        """F16a's own client and answer text (the one weather client)."""
        places = self.weather.geocode(city)
        place = choose_place(places, float(SETTINGS["weather"]["dominant_population_ratio"])) or (places[0] if places else None)
        if place is None:
            raise OnlineError(OnlineErrorKind.SERVICE_ERROR, "default city not found")
        self.weather.remember_default(city, place)
        return answer_text(WeatherRequest(kind="weather", day=0, city=None), place.name, self.weather.forecast(place))

    @staticmethod
    def _top_general(found: List[Headline]) -> List[Headline]:
        """The first headlines of each feed in turn (BBC 1, NPR 1, BBC 2,
        ...), so one feed doesn't fill the list."""
        by_source: Dict[str, List[Headline]] = {}
        for h in found:
            by_source.setdefault(h.source, []).append(h)
        picked: List[Headline] = []
        # A 10-word cut read "...the Trump
        # administration still sends pregnant migrant minors." as if whole --
        # a different claim. Headlines that fit whole come first in each
        # feed; a longer one is cut only when a feed has nothing shorter.
        limit = int(_CFG["general_title_max_words"])

        def fits(h: Headline) -> bool:
            return len(short_title(h.title, 10_000).split()) <= limit

        queues = [[h for h in q if fits(h)] + [h for h in q if not fits(h)] for q in by_source.values()]
        while queues and len(picked) < int(_CFG["max_general_headlines"]):
            for q in list(queues):
                if q and len(picked) < int(_CFG["max_general_headlines"]):
                    picked.append(q.pop(0))
                if not q:
                    queues.remove(q)
        return picked

    def _compose(self, intro: Optional[str], part: OnlinePart, already_said: int = 0) -> Tuple[str, str, Dict[str, List[str]]]:
        """(spoken, logged, topic keys spoken). Parts are added in order
        while they fit in the time cap (`already_said` words were spoken
        before); whatever doesn't fit is in Live Activity only, and topic
        headlines not said stay new."""
        budget = int(float(_CFG["max_spoken_seconds"]) * float(_CFG["words_per_second"]))
        spoken: List[str] = [intro] if intro else []
        used = already_said + sum(words(s) for s in spoken)
        trimmed = False

        def add(sentence: str) -> bool:
            nonlocal used
            if used + words(sentence) > budget:
                return False
            spoken.append(sentence)
            used += words(sentence)
            return True

        if self._offline(part):
            spoken.append(str(_TEXTS["offline"]))
            return " ".join(spoken), "briefing: offline (greeting and date only)", {}
        if part.weather:
            trimmed |= not add(part.weather)
        elif part.weather_error is not None:
            spoken.append(str(_TEXTS["weather_failed"]))
        elif bool(_CFG["include_weather"]) and not str(SETTINGS["weather"]["default_city"]).strip():
            spoken.append(str(_TEXTS["no_city"]))

        if part.headlines:
            titles: List[str] = []
            for h in part.headlines:
                title = short_title(h.title, int(_CFG["general_title_max_words"]))
                if used + words(str(_TEXTS["headlines"]).format(titles=". ".join(titles + [title]))) > budget:
                    trimmed = True
                    break
                titles.append(title)
            if titles:
                add(str(_TEXTS["headlines"]).format(titles=". ".join(titles)))
        elif part.headlines_error is not None:
            if part.headlines_error.kind in (OnlineErrorKind.COOLING, OnlineErrorKind.RATE_LIMITED):
                until = datetime.fromtimestamp(part.headlines_error.until_wall).strftime("%H:%M") if part.headlines_error.until_wall else "later"
                spoken.append(str(_TEXTS["cooling"]).format(until=until))
            else:
                spoken.append(str(_TEXTS["headlines_failed"]))

        keys: Dict[str, List[str]] = {}
        if part.topics is not None:
            if part.topics.error is not None and not part.topics.new:
                spoken.append(failure_text(part.topics.error))
            for topic, heads in part.topics.new.items():
                said: List[Headline] = []
                for h in heads:
                    if used + words(self._topic_sentence(topic, said + [h])) > budget:
                        trimmed = True
                        break
                    said.append(h)
                if said and add(self._topic_sentence(topic, said)):
                    keys[topic] = [x.key for x in said]
            if part.topics.failed:
                spoken.append(str(_TOPIC_REPLIES["some_failed"]).format(topics=", ".join(part.topics.failed)))
        if trimmed:
            spoken.append(str(_TEXTS["more_in_activity"]))
            self._report("briefing cut to fit the time limit; the rest is listed above")
        text = " ".join(spoken)
        topic_count = sum(len(k) for k in keys.values())
        logged = (f"briefing: weather {'yes' if part.weather else 'no'}, {len(part.headlines)} headline(s), "
                  f"{topic_count} topic headline(s) (topics not logged), {words(text)} words")
        return text, logged, keys

    @staticmethod
    def _offline(part: OnlinePart) -> bool:
        """Every source that was tried failed for want of a connection."""
        errors = [part.weather_error, part.headlines_error, part.topics.error if part.topics is not None else None]
        got_something = bool(part.weather or part.headlines or (part.topics is not None and part.topics.new))
        tried = [e for e in errors if e is not None]
        return bool(tried) and not got_something and all(e.kind in _OFFLINE for e in tried)

    @staticmethod
    def _topic_sentence(topic: str, heads: List[Headline]) -> str:
        key = "new_one" if len(heads) == 1 else "new_many"
        return str(_TOPIC_REPLIES[key]).format(count=len(heads), topic=topic, titles=". ".join(short_title(h.title) for h in heads))

    def _after_spoken(self, keys: Dict[str, List[str]]) -> None:
        """Topic hashes are saved only once it has been said, never after
        Stop."""
        orch = self._orchestrator

        def finish() -> None:
            if orch is not None and orch.controller.is_stop_requested():
                return
            if keys and self.topics is not None:
                self.topics.store.mark_seen(keys)

        if orch is not None:
            orch.when_command_finishes(finish)
        else:
            finish()

    def _record(self, part: OnlinePart) -> None:
        """R3/R4: destinations in the record and debug.log, values hashed."""
        if self._orchestrator is None:
            return
        city = str(SETTINGS["weather"]["default_city"]).strip()
        bits = []
        if bool(_CFG["include_weather"]) and city:
            bits.append(f"Open-Meteo (default city: text, {len(city)} characters, sha256 {hashlib.sha256(city.encode()).hexdigest()[:16]})")
        if bool(_CFG["include_headlines"]):
            bits.append("RSS " + ", ".join(feed_name(str(u)) for u in _NEWS["general_feed_urls"]))
        if part.topics is not None:
            bits.append(f"Bing News search RSS ({len(self.topics.store.names())} topic(s), hashed)")
        self._orchestrator.record_online_destination(f"briefing: {'; '.join(bits)}; {self._now():%Y-%m-%d %H:%M:%S}")

    # -- helpers ------------------------------------------------------------------------------

    def _report(self, step: str, session_only: bool = False) -> None:
        if self._orchestrator is not None:
            self._orchestrator.report_step(step, session_only=session_only)

    def _report_any(self, step: str, session_only: bool = False) -> None:
        self._report(step, session_only=session_only)

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
