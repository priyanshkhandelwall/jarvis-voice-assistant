"""F18 YouTube tools (built).

- "play <song or video> on YouTube" -> YouTube's search results for it,
  opened in his own Chrome through F3's open_url (L1, no automation):
  "Searching YouTube for <x> in Chrome."
- "summarise this video <link>" (typed: a spoken link isn't possible) or
  "summarise the YouTube video about <x>" -> the video's public transcript
  (youtube-transcript-api 1.2.4) -> the shared chunked summary online. The
  transcript is internet content, not laptop data: no R4 preview (L0). R3
  still applies: "Searching online", ONLINE, and the decision record /
  debug.log get the destination, the video ID, the size and a hash.
- "about <x>": one web search for his words (L0); the video is taken only
  from the search tool's own result links, never from a link the model
  wrote, so an invented link can't be summarised.
- URLs are never spoken or shown in Live Activity (F3).
- Every failure has its own sentence (R14): no transcript, private or
  unavailable, age-restricted, blocked, no internet, a bad link, the library
  missing. She never summarises from a title or guesses.

youtube-transcript-api reads YouTube's own web endpoints. It is not an
official API and can break when YouTube changes (spec F18, R19).
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple
from urllib.parse import parse_qs, quote_plus, urlparse

from jarvis.actions.app_finder import find_chrome
from jarvis.interpreter import ActionRequest, Answer
from jarvis.online import OnlineError, OnlineErrorKind, OnlineService, OnlineStatus
from jarvis.online.chunked import split_text, summarise_chunks
from jarvis.settings import SETTINGS
from jarvis.speech.length import limit_sentences
from jarvis.speech.qa import message_for

logger = logging.getLogger(__name__)

SERVICE_NAME = "YouTube"
_CFG = SETTINGS["youtube"]
_REPLIES = _CFG["replies"]
_GROQ = SETTINGS["groq"]
_ONLINE = SETTINGS["online"]
_CHECKING_ONLINE_TEXT = str(SETTINGS["speech"]["checking_online_text"])

_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_LINK = re.compile(r"(?:https?://)?(?:[a-z0-9-]+\.)*(?:youtube\.com|youtu\.be)/\S*", re.IGNORECASE)
_ANY_LINK = re.compile(r"https?://\S+|\bwww\.\S+", re.IGNORECASE)
_SUMMARISE = re.compile(r"\bsummari[sz]e\b", re.IGNORECASE)
_ABOUT = re.compile(r"\bsummari[sz]e\s+(?:the\s+|a\s+)?(?:youtube|you\s+tube)\s+video\s+(?:about|on)\s+(?P<topic>.+?)[\s.!?]*$", re.IGNORECASE)
_THIS_VIDEO = re.compile(r"\bsummari[sz]e\s+(?:this|that|the|my)\s+(?:youtube\s+)?video\b", re.IGNORECASE)
_PLAY = [re.compile(str(p)) for p in _CFG["play_patterns"]]


def video_id(link: str) -> Optional[str]:
    """The 11-character ID in a watch?v=, youtu.be/, shorts/ or embed/
    link, or a bare ID. None for anything else (a playlist-only link, a
    10- or 12-character "ID")."""
    link = link.strip().rstrip(".,;:!?)\"'")
    if _ID.fullmatch(link):
        return link
    if not re.match(r"https?://", link, re.IGNORECASE):
        link = "https://" + link
    parsed = urlparse(link)
    host = parsed.netloc.lower()
    candidate = None
    if host.endswith("youtu.be"):
        candidate = parsed.path.strip("/").split("/")[0]
    elif host.endswith("youtube.com"):
        if parsed.path.rstrip("/") == "/watch":
            candidate = (parse_qs(parsed.query).get("v") or [None])[0]
        else:
            parts = parsed.path.strip("/").split("/")
            if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v"):
                candidate = parts[1]
    return candidate if candidate and _ID.fullmatch(candidate) else None


def search_url(query: str) -> str:
    """The YouTube search URL for `query`."""
    return str(_CFG["search_url_template"]).format(query=quote_plus(query))


@dataclass(frozen=True)
class Transcript:
    """A video's transcript: text, language, whether it was auto-generated, and its timing."""
    text: str
    language: str
    generated: bool
    starts: Tuple[float, ...]  # each snippet's start, seconds
    lengths: Tuple[int, ...]  # each snippet's character count (text joins them with a space)
    end_seconds: float  # last snippet's start + duration

    def cut(self, max_chars: int) -> Tuple[str, Optional[Tuple[int, int]]]:
        """(the text to summarise, (covered minutes, total minutes) or None
        when all of it fits)."""
        if len(self.text) <= max_chars:
            return self.text, None
        used, count = 0, 0
        for n in self.lengths:
            if used + n + (1 if count else 0) > max_chars:
                break
            used += n + (1 if count else 0)
            count += 1
        covered_seconds = self.starts[count] if count < len(self.starts) else self.end_seconds
        minutes = lambda s: max(1, round(s / 60))  # noqa: E731
        return self.text[:used], (minutes(covered_seconds), minutes(self.end_seconds))


def _timeout_session(timeout: float):
    """youtube-transcript-api has no timeout parameter: its requests are
    made through this Session, which applies [online]
    request_timeout_seconds to every request (spec F18)."""
    import requests

    class TimeoutSession(requests.Session):
        def request(self, method, url, **kwargs):
            kwargs.setdefault("timeout", timeout)
            return super().request(method, url, **kwargs)

    return TimeoutSession()


def classify_youtube_error(exc: Exception) -> OnlineError:
    """The library's and requests' exceptions as OnlineErrors. Which
    transcript problem it was is kept in `detail` (the class name)."""
    name = type(exc).__name__
    try:
        import requests
    except ImportError:
        requests = None
    if requests is not None and isinstance(exc, requests.exceptions.Timeout):
        return OnlineError(OnlineErrorKind.TIMEOUT, name)
    if requests is not None and isinstance(exc, requests.exceptions.ConnectionError):
        return OnlineError(OnlineErrorKind.CANNOT_REACH, name)
    if name == "YouTubeRequestFailed" and "429" in str(getattr(exc, "reason", "")):
        return OnlineError(OnlineErrorKind.RATE_LIMITED, name)
    return OnlineError(OnlineErrorKind.SERVICE_ERROR, name)


_PROBLEM_REPLIES = {
    "TranscriptsDisabled": "no_transcript",
    "NoTranscriptFound": "no_transcript",
    "VideoUnavailable": "unavailable",
    "InvalidVideoId": "bad_link",
    "VideoUnplayable": "unplayable",
    "AgeRestricted": "age_restricted",
    "RequestBlocked": "blocked",
    "IpBlocked": "blocked",
    "PoTokenRequired": "po_token",
    "YouTubeRequestFailed": "youtube_error",
    "YouTubeDataUnparsable": "youtube_error",
}


def problem_text(error: OnlineError) -> str:
    """What she says when getting a transcript fails."""
    if error.kind in (OnlineErrorKind.CANNOT_REACH, OnlineErrorKind.TIMEOUT):
        return str(_REPLIES["cannot_reach"])
    if error.kind in (OnlineErrorKind.RATE_LIMITED, OnlineErrorKind.COOLING):
        from datetime import datetime

        until = datetime.fromtimestamp(error.until_wall).strftime("%H:%M") if error.until_wall else "later"
        return str(_REPLIES["rate_limited"]).format(until=until)
    if error.kind == OnlineErrorKind.LIBRARY_MISSING:
        return str(SETTINGS["setup_messages"]["system_library_missing"]).format(library="youtube-transcript-api")
    return str(_REPLIES[_PROBLEM_REPLIES.get(error.detail, "youtube_error")])


def default_api_factory(session):
    """One YouTubeTranscriptApi per call, made on the worker thread: the
    library says an instance is not thread-safe."""
    from youtube_transcript_api import YouTubeTranscriptApi

    return YouTubeTranscriptApi(http_client=session)


def default_youtube_service(status: Optional[OnlineStatus] = None) -> OnlineService:
    """The shared online helper (R1) configured for YouTube transcripts."""
    return OnlineService(SERVICE_NAME, status or OnlineStatus(), timeout_seconds=float(_ONLINE["request_timeout_seconds"]),
                         max_retries=int(_ONLINE["max_retries"]), cooldown_seconds=float(_ONLINE["rate_limit_cooldown_seconds"]))


class YouTubeCommands:
    """F18: 'play X on YouTube' opens a search in his Chrome; 'summarise <link or topic>' summarises a
    transcript online."""
    def __init__(self, answerer, service: Optional[OnlineService] = None, api_factory: Callable = default_api_factory,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 chrome: Callable[[], Optional[str]] = find_chrome) -> None:
        self._answerer = answerer
        self.service = service or default_youtube_service()
        self._api_factory = api_factory
        self._sleep = sleep
        self._clock = clock
        self._chrome = chrome
        self._orchestrator = None

    def register(self, interpreter, orchestrator) -> None:
        """Before F16b ("search youtube for X" is not a Google search), F4
        ("summarise <file>" is F19's) and Groq."""
        self._orchestrator = orchestrator
        interpreter.register("youtube_summary", self.summary_matcher, keep_case=True)
        interpreter.register("youtube_play", self.play_matcher)
        interpreter.register("youtube_play_whole", self.whole_play_matcher, raw=True)

    def whole_play_matcher(self, normalized: str):
        """Whisper heard "Play Arrogit, sing on
        YouTube"; the command extractor stopped at the comma ("play arrogit")
        and it went online. The whole sentence, commas dropped, still says
        play ... on YouTube."""
        from jarvis.actions.command_text import clean_command

        if "," not in normalized:
            return None
        return self.play_matcher(clean_command(normalized.replace(",", " ")))

    # -- play (L1, his Chrome) ----------------------------------------------------------------------

    def play_matcher(self, cleaned: str):
        """'play X on YouTube' anywhere in the sentence: open the search in Chrome (L1)."""
        sentence = " ".join(cleaned.lower().split()).rstrip(".!?")
        for pattern in _PLAY:
            match = pattern.fullmatch(sentence)
            if match is None:
                continue
            what = match.group("what").strip()
            if not what:
                return None
            chrome = self._chrome()
            if chrome is None:
                return Answer(str(_REPLIES["no_chrome"]).format(query=what))
            spoken = str(_REPLIES["searching"]).format(query=what)
            return ActionRequest("open_url", {"url": search_url(what), "browser_path": chrome, "display_name": "YouTube",
                                              "youtube_search": what, "spoken": spoken})
        return None

    # -- summarise (L0) ------------------------------------------------------------------------------

    def summary_matcher(self, text: str):
        """'summarise <video link or topic>': get the transcript and summarise it."""
        if not _SUMMARISE.search(text):
            return None
        link = _LINK.search(text)
        if link is not None:
            found = video_id(link.group(0))
            return self._summarise(found) if found else Answer(str(_REPLIES["bad_link"]))
        about = _ABOUT.search(text)
        if about is not None:
            return self._summarise_about(about.group("topic").strip())
        bare = re.search(r"\bsummari[sz]e\s+(?:this|that|the)?\s*(?:youtube\s+)?video\s+(\S{11})\s*$", text, re.IGNORECASE)
        if bare is not None and _ID.fullmatch(bare.group(1)):
            return self._summarise(bare.group(1))
        if _ANY_LINK.search(text):
            # A link that isn't YouTube got "Which video?
            # Type its link..." -- he had typed one. Say what is wrong with it.
            return Answer(str(_REPLIES["bad_link"]))
        if _THIS_VIDEO.search(text):
            return Answer(str(_REPLIES["which_video"]))
        return None

    def _summarise_about(self, topic: str) -> Answer:
        words = str(_CFG["find_query_template"]).format(topic=topic)
        orch = self._orchestrator

        def going_online() -> None:
            self._report(f'searching online, sending "{words}"')
            if orch is not None:
                orch.record_online_destination(f"Groq, {self._answerer.model}, browser_search (Exa): find a YouTube video")
                orch.announce(_CHECKING_ONLINE_TEXT)

        urls, result = self._answerer.find_youtube_video(words, on_going_online=going_online)
        if self._stopped():
            return Answer("")
        if result.error is not None:
            return Answer(result.text)
        found = next((v for v in (video_id(u) for u in urls if _LINK.search(u)) if v), None)
        if found is None:
            self._report("no YouTube video in the search results")
            return Answer(str(_REPLIES["not_found"]).format(topic=topic))
        self._report("found a video on YouTube")
        return self._summarise(found, announced=True)

    def _summarise(self, vid: str, announced: bool = False) -> Answer:
        orch = self._orchestrator
        if not announced and orch is not None:
            orch.announce(_CHECKING_ONLINE_TEXT)
        self._report(f"checking online: YouTube transcript (video {vid[:4]}…)")
        try:
            transcript = self.service.call(lambda timeout: self._fetch(vid, timeout), classify_youtube_error)
        except OnlineError as error:
            self._report(f"transcript: {error.detail or error.kind.name.lower()}")
            return Answer(problem_text(error))
        if self._stopped():
            return Answer("")
        kind = "auto-generated" if transcript.generated else "manual"
        self._report(f"transcript found ({transcript.language}, {kind}, {len(transcript.text):,} chars)")
        if len(transcript.text.strip()) < int(_CFG.get("min_transcript_chars", 200)):
            # A few words of captions were "summarised"
            # into a made-up summary. Too little to summarise: nothing goes online.
            self._report("transcript too short to summarise")
            return Answer(str(_REPLIES["too_short"]))
        limit = int(_GROQ["max_chars_per_request"]) * int(_GROQ["max_chunks_per_task"])
        text, coverage = transcript.cut(limit)
        chunks = split_text(text)[: int(_GROQ["max_chunks_per_task"])]
        sent = sum(len(c) for c in chunks)
        digest = hashlib.sha256("".join(chunks).encode("utf-8")).hexdigest()
        if orch is not None:
            # R3: destination, size, the video ID and a hash -- the record and debug.log only.
            orch.record_online_destination(f"YouTube transcript (video {vid}); Groq, {self._answerer.model}: "
                                           f"text, {sent} characters, sha256 {digest[:16]}")
        result = summarise_chunks(chunks, lambda t, i: self._answerer.summarise(t, i), str(_CFG["summary_instructions"]),
                                  str(_CFG["combine_instructions"]), self._stopped, self._report, self._sleep, self._clock)
        if result.stopped or self._stopped():
            return Answer("")
        if result.error is not None:
            return Answer(str(_REPLIES["summary_failed"]).format(reason=result.text or message_for(OnlineError(result.error))))
        self._report("summary ready")
        self._report(f"summary: {result.text}")
        parts = [limit_sentences(result.text, int(_CFG["spoken_summary_sentences"]))]
        if transcript.generated:
            parts.append(str(_REPLIES["auto_captions"]))
        if coverage is not None:
            parts.append(str(_REPLIES["coverage"]).format(covered=coverage[0], total=coverage[1]))
        return Answer(" ".join(parts))

    def _fetch(self, vid: str, timeout: float) -> Transcript:
        """On the worker thread: a new API instance (not thread-safe), the
        timeout Session, fetch() with the preferred languages and the bare
        ID; NoTranscriptFound -> any manual transcript, then any generated
        one, in the order list() gives them."""
        try:
            api = self._api_factory(_timeout_session(timeout))
        except ImportError as exc:
            raise OnlineError(OnlineErrorKind.LIBRARY_MISSING, "youtube-transcript-api") from exc
        languages = [str(code) for code in _CFG["preferred_transcript_languages"]]
        try:
            fetched = api.fetch(vid, languages=languages)
        except Exception as exc:
            if type(exc).__name__ != "NoTranscriptFound":
                raise
            available = list(api.list(vid))
            chosen = next((t for t in available if not t.is_generated), None) or next(iter(available), None)
            if chosen is None:
                raise
            fetched = chosen.fetch()
        snippets = [s for s in fetched if str(s.text).strip()]
        if not snippets:
            raise OnlineError(OnlineErrorKind.SERVICE_ERROR, "NoTranscriptFound")
        texts = [" ".join(str(s.text).split()) for s in snippets]
        last = snippets[-1]
        return Transcript(text=" ".join(texts), language=str(fetched.language_code), generated=bool(fetched.is_generated),
                          starts=tuple(float(s.start) for s in snippets), lengths=tuple(len(t) for t in texts),
                          end_seconds=float(last.start) + float(last.duration))

    # -- helpers -------------------------------------------------------------------------------------

    def _report(self, step: str) -> None:
        if self._orchestrator is not None:
            self._orchestrator.report_step(step)

    def _stopped(self) -> bool:
        return self._orchestrator is not None and self._orchestrator.controller.is_stop_requested()
