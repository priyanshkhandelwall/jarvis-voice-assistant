"""F20/F21 news headlines from RSS (built).

- Every request goes through the shared OnlineService (R1: timeout, one
  retry, 429 cooldown) with urllib (shared rule 9), and every feed is
  parsed with defusedxml, never the plain XML parser (entity tricks in a
  hostile feed are refused).
- Topic headlines ([news] topic_source): "bing" asks Bing News search for
  the topic itself, one RSS feed per topic; "feeds" filters the general
  feeds by the topic's words. Bing's feed terms allow use "within an RSS
  aggregator for your personal, non-commercial use", which is what this is.
- A headline's identity is its article URL (Bing wraps it in a click link;
  the real URL inside is used), hashed. F20 stores only those hashes.
- R3: no provider name is ever shown or spoken; the caller records the
  destination in the decision record and debug.log only.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Iterable, List, Optional
from urllib.parse import parse_qs, quote_plus, urlparse

from jarvis.online import OnlineError, OnlineErrorKind, OnlineService, OnlineStatus
from jarvis.online.http import classify_http_error, get_bytes
from jarvis.settings import SETTINGS

SERVICE_NAME = "News"  # the OnlineStatus name while a feed is being read (ONLINE)
DESTINATION_TOPICS = "Bing News search RSS"  # decision record / debug.log only (R3)

_CFG = SETTINGS["news"]
_ONLINE = SETTINGS["online"]
_FEED_NAMES = {str(k).lower(): str(v) for k, v in _CFG.get("feed_names", {}).items()}


@dataclass(frozen=True)
class Headline:
    """One RSS item: title, link, source and publish time."""
    title: str
    link: str  # the article's own URL
    source: str  # the publisher ("Outlook India", "BBC News")
    published: Optional[datetime] = None

    @property
    def key(self) -> str:
        """What F20 remembers: a hash of the article URL, never its text."""
        return hashlib.sha256((self.link or self.title).strip().lower().encode("utf-8")).hexdigest()[:16]


def _local(tag: str) -> str:
    """"{namespace}Source" -> "source"."""
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(element, name: str) -> str:
    for child in element:
        if _local(child.tag) == name:
            return " ".join((child.text or "").split())
    return ""


def article_url(link: str) -> str:
    """Bing's click-through link carries the real article in `url=`."""
    parsed = urlparse(link)
    if parsed.netloc.endswith("bing.com") and "apiclick" in parsed.path:
        real = parse_qs(parsed.query).get("url")
        if real and real[0]:
            return real[0]
    return link


def feed_name(url: str) -> str:
    """"BBC News" for feeds.bbci.co.uk, else the feed's host."""
    host = urlparse(url).netloc.lower()
    return _FEED_NAMES.get(host, host)


def parse_rss(data: bytes, default_source: str = "") -> List[Headline]:
    """The items of an RSS 2.0 feed. A body that isn't safe, well-formed
    RSS is a service error (shared rule 4), never a crash."""
    try:
        from defusedxml import ElementTree
    except ImportError as exc:
        raise OnlineError(OnlineErrorKind.LIBRARY_MISSING, "defusedxml is not installed") from exc
    try:
        root = ElementTree.fromstring(data)
    except Exception as exc:  # ParseError, or defusedxml refusing entities/DTDs
        raise OnlineError(OnlineErrorKind.SERVICE_ERROR, f"not readable RSS: {type(exc).__name__}") from exc
    channel = next((c for c in root if _local(c.tag) == "channel"), None)
    if _local(root.tag) != "rss" or channel is None:
        raise OnlineError(OnlineErrorKind.SERVICE_ERROR, "not an RSS feed")
    headlines = []
    for item in channel:
        if _local(item.tag) != "item":
            continue
        title = _child_text(item, "title")
        if not title:
            continue
        published = None
        stamp = _child_text(item, "pubdate")
        if stamp:
            try:
                published = parsedate_to_datetime(stamp)
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                published = None
        source = _child_text(item, "source") or default_source
        headlines.append(Headline(title=title, link=article_url(_child_text(item, "link")), source=source, published=published))
    return headlines


def topic_words(topic: str) -> List[str]:
    """The words of a topic that matter for matching headlines (stop words removed)."""
    stop = {str(w).lower() for w in SETTINGS["memory"]["stop_words"]}
    return [w for w in re.findall(r"[a-z0-9']+", topic.lower()) if w not in stop] or re.findall(r"[a-z0-9']+", topic.lower())


def mentions(headline: Headline, topic: str) -> bool:
    """"feeds" mode: every word of the topic is in the title (whole words)."""
    title = set(re.findall(r"[a-z0-9']+", headline.title.lower()))
    return all(word in title for word in topic_words(topic))


def recent(headlines: Iterable[Headline], now: datetime, max_age_hours: float) -> List[Headline]:
    """Drop headlines older than the limit. One with no date is kept."""
    cutoff = now - timedelta(hours=max_age_hours)
    return [h for h in headlines if h.published is None or h.published >= cutoff]


Fetch = Callable[[str, float], bytes]


class NewsClient:
    """One shared online service for every feed (one cooldown for news)."""

    def __init__(self, service: OnlineService, fetch: Fetch = get_bytes, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.service = service
        self._fetch = fetch
        self._now = now

    def feed(self, url: str) -> List[Headline]:
        """The headlines of one RSS feed, fetched through the shared online helper and parsed safely
        (defusedxml)."""
        data = self.service.call(lambda timeout: self._fetch(url, timeout), classify_http_error)
        return parse_rss(data, default_source=feed_name(url))

    @staticmethod
    def topic_url(topic: str) -> str:
        """The news-search RSS URL for `topic`."""
        return str(_CFG["topic_search_url"]).format(query=quote_plus(topic))

    def topic(self, topic: str, general: Optional[List[Headline]] = None) -> List[Headline]:
        """Recent headlines about `topic`, newest first. In "feeds" mode
        `general` (already fetched) is filtered and no request is made."""
        max_age = float(_CFG["topic_max_age_hours"])
        if str(_CFG["topic_source"]) == "feeds":
            found = [h for h in (general or []) if mentions(h, topic)]
        else:
            found = self.feed(self.topic_url(topic))
        fresh = recent(found, self._now(), max_age)
        return sorted(fresh, key=lambda h: h.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    def general(self, urls: Optional[List[str]] = None) -> tuple:
        """(headlines from every feed that answered, names of feeds that failed)."""
        headlines: List[Headline] = []
        failed: List[str] = []
        errors: List[OnlineError] = []
        for url in urls if urls is not None else [str(u) for u in _CFG["general_feed_urls"]]:
            try:
                headlines.extend(self.feed(url))
            except OnlineError as error:
                failed.append(feed_name(url))
                errors.append(error)
        if errors and not headlines:
            raise errors[0]  # every feed failed: the first reason is the answer
        return headlines, failed


def default_news_service(status: Optional[OnlineStatus] = None) -> OnlineService:
    """The shared online helper (R1) configured for the news feeds."""
    return OnlineService(
        SERVICE_NAME,
        status or OnlineStatus(),
        timeout_seconds=float(_ONLINE["request_timeout_seconds"]),
        max_retries=int(_ONLINE["max_retries"]),
        cooldown_seconds=float(_ONLINE["rate_limit_cooldown_seconds"]),
    )
