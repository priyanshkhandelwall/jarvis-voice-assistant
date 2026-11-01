"""Conversational Q&A / small talk for F2, via Groq's free-tier API.

Corrected The original plan was a local llama-cpp-python GGUF
model for this piece; the developer replaced that with Groq before any
model weights were downloaded (F2 in the project specification).

R2 (local-first) still applies to everything else in F2. Only this
"answer questions and small talk" piece goes online, and only when called.

R16: the key comes from the environment (GROQ_API_KEY, or the git-ignored
.env file), never from code.

R1/R14 and the developer's missing-setup rule: every failure
returns a spoken sentence that says exactly what to set up and where, and
nothing here ever raises. The timeout, the single retry and the 429
cooldown are applied by the shared OnlineService, not re-implemented here.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Iterable, Optional, Tuple

from jarvis.actions.intro import identity_prompt_rule
from jarvis.online import OnlineError, OnlineErrorKind, OnlineService, OnlineStatus
from jarvis.online.budget import TokenLedger
from jarvis.settings import SETTINGS
from jarvis.speech.plain_text import to_speakable

logger = logging.getLogger(__name__)

SERVICE_NAME = "Groq"

_GROQ = SETTINGS["groq"]
_ONLINE = SETTINGS["online"]
_MESSAGES = SETTINGS["setup_messages"]
_WEB = SETTINGS["web"]
_PROVIDER_HOSTS = tuple(str(h).lower() for h in _WEB.get("provider_hosts", []))
# What the record says a live search used (debug log only, never Live
# Activity). Exa runs Groq's browser_search (Groq docs, checked).
LIVE_TOOL = "browser_search (Exa)"
# Groq's citation markers: 【0†L3-L7】 or 【0†Scorecard | ...†www.bcci.tv】.
_CITATION = re.compile(r"【[^】]*】")

# [groq] opener_rule (a short first sentence, so her voice
# starts in about a second) sits right after the length rule.
_BASE_PROMPT = (
    "You are JARVIS, a female AI assistant who runs on the developer's Windows laptop. You are confident, warm "
    "and concise. Your answers are read aloud, not displayed, so never use markdown, lists, headings or links. "
    f"Answer in about three sentences unless the developer asks for more. {_GROQ['opener_rule']} "
    "If he asks for a number of items (for "
    "example three tips), give exactly that many, each complete, one sentence each. Describe what you can do in "
    "plain words; never mention internal names such as feature numbers, modules or trust levels."
)

# An online answer only talks -- it must never claim an action
# (Groq said "Going to sleep now" while she stayed awake). The local
# post-check is jarvis/speech/claims.py.
_BASE_PROMPT = f"{_BASE_PROMPT} {SETTINGS['claims']['prompt_rule']}"

# Groq expanded JARVIS as "Just A Reasonable
# Intelligent Virtual Assistant". The exact expansion and creator, from intro.txt.
_BASE_PROMPT = f"{_BASE_PROMPT} {identity_prompt_rule()}"

# A sentence ends at . ! or ?, optionally followed by closing quotes or
# brackets, then whitespace or the end of the text.
_SENTENCE_END = re.compile(r"[.!?][\"')\]’”]*(?=\s|$)")


def trim_to_last_sentence(text: str) -> str:
    """Safety net for an answer Groq cut off at max_tokens: keep only the
    complete sentences, so she never stops speaking mid-word. Returns ""
    if not even one sentence finished."""
    ends = list(_SENTENCE_END.finditer(text))
    if not ends:
        return ""
    return text[: ends[-1].end()].strip()

_dotenv_loaded = False


def _load_dotenv_once() -> None:
    """Best-effort .env loader, run once per process. A real environment
    variable always wins over .env, and a missing python-dotenv or .env
    file is fine (R14)."""
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True
    try:
        from dotenv import load_dotenv

        env_path = Path(__file__).resolve().parent.parent.parent / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)
    except Exception:
        pass


def build_system_prompt(capabilities: Iterable[Tuple[str, bool]] = ()) -> str:
    """F2: the prompt lists what JARVIS can and cannot do yet, built from the
    Modules panel at startup, so she never claims a feature that isn't
    built (R19). `capabilities` is (plain description, built?) pairs, e.g.
    ("open apps installed on this laptop", True) -- see [groq.abilities]."""
    items = list(capabilities)
    if not items:
        return _BASE_PROMPT
    built = ", ".join(name for name, ok in items if ok) or "none yet"
    pending = ", ".join(name for name, ok in items if not ok) or "none"
    return (
        f"{_BASE_PROMPT}\n"
        f"Things you can do right now: {built}.\n"
        f"Things you cannot do yet (not built): {pending}.\n"
        "If asked to do something that is not built, say plainly that you can't do that yet. "
        "Never claim you did something you cannot do."
    )


def clock_sentence(now: datetime) -> str:
    """Groq said "I can't tell the time". Every request
    now carries the laptop's own local date and time (L0: the clock is not
    laptop data under R4, and a time question is answered locally first)."""
    zone = now.astimezone().tzname() or "local time"
    return (
        f"The current local date and time on the developer's laptop is {now.strftime('%A %d %B %Y, %H:%M')} "
        f"({zone}). Use it whenever the date or time matters; never say you cannot tell the time or date."
    )


def _clock_label(wall_time: Optional[float]) -> str:
    if wall_time is None:
        return "later"
    return datetime.fromtimestamp(wall_time).strftime("%H:%M")


def message_for(error: OnlineError) -> str:
    """The exact sentence she says for each failure (missing-setup rule)."""
    kind = error.kind
    if kind == OnlineErrorKind.NO_KEY:
        return _MESSAGES["groq_no_key"]
    if kind == OnlineErrorKind.KEY_REJECTED:
        return _MESSAGES["groq_key_rejected"]
    if kind == OnlineErrorKind.LIBRARY_MISSING:
        return _MESSAGES["groq_library_missing"]
    if kind == OnlineErrorKind.CANNOT_REACH:
        return _MESSAGES["groq_cannot_reach"]
    if kind == OnlineErrorKind.TIMEOUT:
        return _MESSAGES["groq_timeout"]
    if kind == OnlineErrorKind.RATE_LIMITED:
        return _MESSAGES["groq_rate_limited"].format(until=_clock_label(error.until_wall))
    if kind == OnlineErrorKind.COOLING:
        return _MESSAGES["groq_cooling"].format(until=_clock_label(error.until_wall))
    if kind == OnlineErrorKind.EMPTY_RESPONSE:
        return _MESSAGES["groq_empty"]
    if kind == OnlineErrorKind.CUT_OFF:
        return _MESSAGES["groq_cut_off"]
    if kind == OnlineErrorKind.BUDGET_USED_UP:
        return str(_WEB["budget_used_up_text"])
    if kind == OnlineErrorKind.MINUTE_LIMIT:
        return str(_WEB["minute_limit_text"]).format(seconds=round(error.retry_after_seconds or 60))
    if kind == OnlineErrorKind.NO_RESULTS:
        return str(_WEB["nothing_found_text"])
    return _MESSAGES["groq_service_error"]


def classify_groq_error(exc: Exception) -> OnlineError:
    """Map the groq SDK's own exceptions (groq 1.7.0) onto our kinds.
    APITimeoutError is checked before APIConnectionError: it subclasses it."""
    try:
        import groq
    except ImportError:
        return OnlineError(OnlineErrorKind.LIBRARY_MISSING, str(exc))
    if isinstance(exc, groq.RateLimitError):
        retry_after: Optional[float] = None
        try:
            retry_after = float(exc.response.headers.get("retry-after"))
        except (AttributeError, TypeError, ValueError):
            retry_after = None
        return OnlineError(OnlineErrorKind.RATE_LIMITED, str(exc), retry_after_seconds=retry_after)
    if isinstance(exc, (groq.AuthenticationError, groq.PermissionDeniedError)):
        return OnlineError(OnlineErrorKind.KEY_REJECTED, str(exc))
    if isinstance(exc, groq.APITimeoutError):
        return OnlineError(OnlineErrorKind.TIMEOUT, str(exc))
    if isinstance(exc, groq.APIConnectionError):
        return OnlineError(OnlineErrorKind.CANNOT_REACH, str(exc))
    # The status and Groq's own error code are kept (the
    # record and debug.log only), so a "service error" says what it was.
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    error = body.get("error", body) if isinstance(body, dict) else {}
    code = error.get("code") or error.get("type") if isinstance(error, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    detail = " ".join(str(part) for part in (f"HTTP {status}" if status else type(exc).__name__, code, message or exc) if part)
    return OnlineError(OnlineErrorKind.SERVICE_ERROR, detail)


def default_groq_service(status: Optional[OnlineStatus] = None) -> OnlineService:
    """The shared online helper (R1) configured for Groq."""
    return OnlineService(
        SERVICE_NAME,
        status or OnlineStatus(),
        timeout_seconds=float(_ONLINE["request_timeout_seconds"]),
        max_retries=int(_ONLINE["max_retries"]),
        cooldown_seconds=float(_ONLINE["rate_limit_cooldown_seconds"]),
    )


@dataclass(frozen=True)
class AnswerResult:
    """The outcome of one online answer: the text, whether it went online, any error, and what the web
    search used."""
    text: str
    # True the moment a real network call to Groq was attempted, whether or
    # not it succeeded -- what R3's log line keys off.
    went_online: bool
    error: Optional[OnlineErrorKind] = None
    # True when Groq stopped at max_tokens (finish_reason "length") and the
    # answer was cut back to its last complete sentence.
    trimmed: bool = False
    # Live searches only (F16 Stage 1), all read from the search tool's own
    # records, never from the model's text:
    searched_for: Optional[str] = None  # the query the tool actually ran
    pages: Tuple[str, ...] = ()  # hosts of the pages it opened ("bcci.tv")
    sources: Tuple[str, ...] = ()  # URLs of those pages (record/debug log)
    tokens: Optional[int] = None  # usage.total_tokens of the call
    # The date of the result, read by JARVIS from the page the model opened
    # (never from the model's text), and that page's host.
    result_date: Optional[date] = None
    result_page: Optional[str] = None
    # What the service actually said when it failed ("HTTP 400
    # tool_use_failed: ..."), for the decision record and debug.log only --
    # a real "service error" once had no cause recorded anywhere.
    error_detail: Optional[str] = None


def _host(url: str) -> str:
    host = re.sub(r"^[a-z]+://", "", url.strip().lower()).split("/")[0].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def is_provider_host(host: str) -> bool:
    """The search provider itself ("exa.ai", "api.exa.ai"), never a source."""
    host = host.lower()
    return any(host == p or host.endswith("." + p) for p in _PROVIDER_HOSTS)


def source_hosts(urls) -> Tuple[str, ...]:
    """The real websites read.
    A provider link stands for the site in its ?url=... when it has one,
    and is dropped otherwise."""
    from urllib.parse import parse_qsl, urlsplit

    hosts = []
    for url in urls:
        host = _host(url)
        if is_provider_host(host):
            inner = [v for _k, v in parse_qsl(urlsplit(url).query) if v.lower().startswith(("http://", "https://"))]
            host = _host(inner[0]) if inner else ""
            if not host or is_provider_host(host):
                continue
        hosts.append(host)
    return tuple(dict.fromkeys(hosts))


def _get(obj, key):
    """Groq's SDK objects, or the same data as plain dicts."""
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def _walk_tools(message) -> Tuple[Optional[str], list]:
    """(the query browser_search ran, [(url, page text) for each page
    browser.open read]), from Groq's `executed_tools`. A page opened by `id`
    is that entry of the search's own result list."""
    query: Optional[str] = None
    results: list = []
    opened: list = []
    for tool in _get(message, "executed_tools") or []:
        tool_type = str(_get(tool, "type") or _get(tool, "name") or "")
        try:
            args = json.loads(_get(tool, "arguments") or "{}")
        except (TypeError, ValueError):
            args = {}
        if not isinstance(args, dict):
            args = {}
        found = _get(_get(tool, "search_results") or {}, "results") or []
        output = str(_get(tool, "output") or "")
        if tool_type == "browser_search":
            if query is None:
                query = str(args.get("query") or "").strip() or None
            results = [str(_get(r, "url") or "") for r in found]
        elif tool_type in ("browser.open", "open"):
            index = args.get("id")
            if isinstance(index, int) and 0 <= index < len(results) and results[index]:
                opened.append((results[index], output))
            else:
                url = re.search(r"https?://\S+", output)
                if url and "exa.ai" not in url.group(0):
                    opened.append((url.group(0), output))
    return query, [(u, text) for u, text in opened if u]


def search_result_urls(message) -> Tuple[str, ...]:
    """Every URL browser_search returned and every page browser.open read,
    in order -- from the tool's own records, never from the model's text
    (F18: a video link is taken only from these)."""
    urls: list = []
    for tool in _get(message, "executed_tools") or []:
        for r in _get(_get(tool, "search_results") or {}, "results") or []:
            urls.append(str(_get(r, "url") or ""))
    _query, opened = _walk_tools(message)
    urls.extend(u for u, _ in opened)
    return tuple(dict.fromkeys(u for u in urls if u))


def read_executed_tools(message) -> Tuple[Optional[str], Tuple[str, ...]]:
    """(the query browser_search ran, URLs of the pages browser.open read)."""
    query, opened = _walk_tools(message)
    return query, tuple(dict.fromkeys(u for u, _ in opened))


_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MONTH = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DATE_PATTERNS = [
    (re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MONTH}\.?,?\s+(\d{{4}})\b", re.IGNORECASE), ("d", "m", "y")),
    (re.compile(rf"\b{_MONTH}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.IGNORECASE), ("m", "d", "y")),
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), ("y", "mn", "d")),
]


def first_date_in(text: str, today: date, within_chars: int) -> Optional[date]:
    """The first real date in the page text (headline first), never one in
    the future. Formats: "1 Mar 2026", "March 1, 2026", "2026-03-01"."""
    head = text[:within_chars]
    found = []
    for pattern, order in _DATE_PATTERNS:
        for match in pattern.finditer(head):
            parts = dict(zip(order, match.groups()))
            try:
                month = int(parts["mn"]) if "mn" in parts else _MONTHS[parts["m"][:3].lower()]
                when = date(int(parts["y"]), month, int(parts["d"]))
            except (KeyError, ValueError):
                continue
            if when <= today:
                found.append((match.start(), when))
    return min(found)[1] if found else None


def result_date(opened: list, answer: str, today: date, within_chars: int) -> Tuple[Optional[date], Optional[str]]:
    """The result's date and the page it came from: the page the answer
    names ("according to bcci.tv"), else the first page that shows a date."""
    lowered = answer.lower()
    dated = [(url, first_date_in(text, today, within_chars)) for url, text in opened]
    dated = [(url, when) for url, when in dated if when is not None]
    if not dated:
        return None, None
    for url, when in dated:
        if _host(url).split(".")[0] in lowered:
            return when, (source_hosts([url]) or (None,))[0]
    return dated[0][1], (source_hosts([dated[0][0]]) or (None,))[0]  # never the provider's own host


def _total_tokens(response) -> Optional[int]:
    total = getattr(getattr(response, "usage", None), "total_tokens", None)
    return total if isinstance(total, int) else None


class GroqAnswerer:
    """F2/F16: answers questions through Groq's API with the JARVIS system prompt; live web questions use
    its search tool. A missing key or package gives a setup message, never a crash."""
    def __init__(
        self,
        api_key: Optional[str] = None,
        service: Optional[OnlineService] = None,
        capabilities: Iterable[Tuple[str, bool]] = (),
        now: Callable[[], datetime] = datetime.now,
        ledger: Optional[TokenLedger] = None,
        sleep: Callable[[float], None] = time.sleep,
        style_hint: Optional[Callable[[], Optional[str]]] = None,
    ) -> None:
        _load_dotenv_once()
        self._now = now
        # F14: "keep it short and gentle" while he's said he is tired or
        # stressed (MoodWatcher.prompt_hint); None adds nothing.
        self._style_hint = style_hint
        # Every call's tokens go here (F16 Stage 1). In memory unless the
        # app passes the persistent one.
        self.ledger = ledger or TokenLedger()
        self._sleep = sleep
        self._api_key = api_key or os.environ.get("GROQ_API_KEY")
        self._service = service or default_groq_service()
        self._system_prompt = build_system_prompt(capabilities)

    def today(self) -> date:
        """The laptop's local date, as this answerer sees it (tests fix it)."""
        return self._now().date()

    @property
    def model(self) -> str:
        """The Groq model name from settings."""
        return str(_GROQ["model"])

    def cooling_until(self) -> Optional[float]:
        """Wall-clock end of a 429 cooldown, or None (F19: no preview is
        offered while summaries can't be sent)."""
        return self._service.cooling_until_wall()

    @property
    def system_prompt(self) -> str:
        """The prompt as sent: the fixed part, the current clock, and F14's
        gentler-replies line when it applies."""
        hint = self._style_hint() if self._style_hint is not None else None
        prompt = f"{self._system_prompt}\n{clock_sentence(self._now())}"
        return f"{prompt}\n{hint}" if hint else prompt

    def answer(self, question: str, on_going_online: Optional[Callable[[], None]] = None,
               length_rule: Optional[str] = None) -> AnswerResult:
        """`on_going_online()` runs only when a request is really about to
        leave the laptop -- not for a missing key, a missing library or a
        cooldown -- so "Checking online" is never said falsely (R3)."""
        if not self._api_key:
            return self._failed(OnlineError(OnlineErrorKind.NO_KEY), went_online=False)
        try:
            from groq import Groq  # lazy: a missing package degrades, never breaks the app (R14)
        except ImportError as exc:
            return self._failed(OnlineError(OnlineErrorKind.LIBRARY_MISSING, str(exc)), went_online=False)
        cooling_until = self._service.cooling_until_wall()
        if cooling_until is not None:
            return self._failed(OnlineError(OnlineErrorKind.COOLING, until_wall=cooling_until), went_online=False)

        if on_going_online is not None:
            on_going_online()

        def ask(timeout: float):
            # max_retries=0: the shared OnlineService does R1's one retry;
            # the SDK's own default (2 retries, 60 s) must not stack on top.
            client = Groq(api_key=self._api_key, timeout=timeout, max_retries=0)
            return client.chat.completions.create(
                model=self.model,
                messages=[
                    # "in one line" / "in detail" adds its own length rule.
                    {"role": "system", "content": f"{self.system_prompt}\n{length_rule}" if length_rule else self.system_prompt},
                    {"role": "user", "content": question},
                ],
                max_tokens=int(_GROQ["answer_max_tokens"]),
                reasoning_effort=str(_GROQ["reasoning_effort"]),
            )

        try:
            response = self._service.call(ask, classify_groq_error)
        except OnlineError as error:
            return self._failed(error, went_online=True)

        self.ledger.add(_total_tokens(response))
        choice = response.choices[0]
        text = to_speakable((choice.message.content or "").strip())
        if not text:
            return self._failed(OnlineError(OnlineErrorKind.EMPTY_RESPONSE), went_online=True)
        if getattr(choice, "finish_reason", None) == "length":
            trimmed = trim_to_last_sentence(text)
            logger.info("Groq answer trimmed: hit max_tokens, kept %d of %d characters", len(trimmed), len(text))
            if not trimmed:
                return self._failed(OnlineError(OnlineErrorKind.CUT_OFF), went_online=True)
            return AnswerResult(text=trimmed, went_online=True, trimmed=True)
        return AnswerResult(text=text, went_online=True)

    def answer_screen(
        self,
        question: str,
        screen_text: str,
        jpeg: Optional[bytes],
        model: str,
        instructions: str,
        on_going_online: Optional[Callable[[], None]] = None,
    ) -> AnswerResult:
        """F8 "explain my screen", AFTER the developer confirmed the R4
        preview: his question, the text read off the screen and (when
        `jpeg` is given) the blacked-out screenshot, exactly as previewed.
        `model` must accept images when `jpeg` is given ([screen]
        vision_model); without an image the normal text model is used.
        Same R1 rules, key handling and token ledger as every Groq call."""
        if not self._api_key:
            return self._failed(OnlineError(OnlineErrorKind.NO_KEY), went_online=False)
        try:
            from groq import Groq
        except ImportError as exc:
            return self._failed(OnlineError(OnlineErrorKind.LIBRARY_MISSING, str(exc)), went_online=False)
        cooling_until = self._service.cooling_until_wall()
        if cooling_until is not None:
            return self._failed(OnlineError(OnlineErrorKind.COOLING, until_wall=cooling_until), went_online=False)
        if on_going_online is not None:
            on_going_online()

        user_text = f"Text read from the screen:\n{screen_text or '(no text could be read)'}\n\nQuestion: {question}"
        if jpeg is not None:
            import base64

            content = [{"type": "text", "text": user_text},
                       {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")}}]
        else:
            content = user_text

        def ask(timeout: float):
            client = Groq(api_key=self._api_key, timeout=timeout, max_retries=0)
            return client.chat.completions.create(
                model=model if jpeg is not None else self.model,
                messages=[{"role": "system", "content": f"{self.system_prompt}\n{instructions}"},
                          {"role": "user", "content": content}],
                max_tokens=int(_GROQ["answer_max_tokens"]),
            )

        try:
            response = self._service.call(ask, classify_groq_error)
        except OnlineError as error:
            return self._failed(error, went_online=True)
        self.ledger.add(_total_tokens(response))
        choice = response.choices[0]
        # A reasoning model may put its thinking in <think>...</think>: never spoken.
        raw = re.sub(r"<think>.*?</think>", "", choice.message.content or "", flags=re.DOTALL)
        text = to_speakable(raw.strip())
        if not text:
            return self._failed(OnlineError(OnlineErrorKind.EMPTY_RESPONSE), went_online=True)
        if getattr(choice, "finish_reason", None) == "length":
            trimmed = trim_to_last_sentence(text)
            if not trimmed:
                return self._failed(OnlineError(OnlineErrorKind.CUT_OFF), went_online=True)
            return AnswerResult(text=trimmed, went_online=True, trimmed=True)
        return AnswerResult(text=text, went_online=True)

    def summarise(self, text: str, instructions: str, on_going_online: Optional[Callable[[], None]] = None) -> AnswerResult:
        """F18/F19: one summary request (the shared chunked method calls it
        once per chunk, then once to combine). `text` is sent exactly as
        given -- for F19, only after his R4 preview and confirm. Same key,
        cooldown, R1 and ledger handling as answer(); [groq]
        summary_max_tokens caps the reply."""
        if not self._api_key:
            return self._failed(OnlineError(OnlineErrorKind.NO_KEY), went_online=False)
        try:
            from groq import Groq
        except ImportError as exc:
            return self._failed(OnlineError(OnlineErrorKind.LIBRARY_MISSING, str(exc)), went_online=False)
        cooling_until = self._service.cooling_until_wall()
        if cooling_until is not None:
            return self._failed(OnlineError(OnlineErrorKind.COOLING, until_wall=cooling_until), went_online=False)
        if on_going_online is not None:
            on_going_online()

        def ask(timeout: float):
            client = Groq(api_key=self._api_key, timeout=timeout, max_retries=0)
            return client.chat.completions.create(
                model=self.model,
                # The short-opener rule goes last, after the summary's own
                # instructions (live check Placed only in the base
                # prompt, the first sentence came back 18 words long).
                messages=[{"role": "system", "content": f"{self.system_prompt}\n{instructions}\n{_GROQ['opener_rule']}"},
                          {"role": "user", "content": text}],
                max_tokens=int(_GROQ["summary_max_tokens"]),
                reasoning_effort=str(_GROQ["reasoning_effort"]),
            )

        try:
            response = self._service.call(ask, classify_groq_error)
        except OnlineError as error:
            return self._failed(error, went_online=True)
        self.ledger.add(_total_tokens(response))
        choice = response.choices[0]
        result = to_speakable((choice.message.content or "").strip())
        if not result:
            return self._failed(OnlineError(OnlineErrorKind.EMPTY_RESPONSE), went_online=True)
        if getattr(choice, "finish_reason", None) == "length":
            trimmed = trim_to_last_sentence(result)
            if not trimmed:
                return self._failed(OnlineError(OnlineErrorKind.CUT_OFF), went_online=True)
            return AnswerResult(text=trimmed, went_online=True, trimmed=True)
        return AnswerResult(text=result, went_online=True)

    def classify(self, words: str, system_prompt: str, max_tokens: int) -> Tuple[Optional[str], AnswerResult]:
        """The intent router's classifier -- never an answerer.
        Only his words and the list of local intents go out (no personal
        data, no memory). Short prompt, no tools, low reasoning, counted in
        the token ledger. Returns (the model's reply text, the result);
        the text is validated by jarvis/router/router.py, never trusted."""
        if not self._api_key:
            return None, self._failed(OnlineError(OnlineErrorKind.NO_KEY), went_online=False)
        try:
            from groq import Groq
        except ImportError as exc:
            return None, self._failed(OnlineError(OnlineErrorKind.LIBRARY_MISSING, str(exc)), went_online=False)
        if self._service.cooling_until_wall() is not None:
            return None, self._failed(OnlineError(OnlineErrorKind.COOLING), went_online=False)
        if self.ledger.left_today() < max_tokens * 4:
            return None, self._failed(OnlineError(OnlineErrorKind.BUDGET_USED_UP), went_online=False)

        def ask(timeout: float):
            client = Groq(api_key=self._api_key, timeout=timeout, max_retries=0)
            return client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": words}],
                max_tokens=max_tokens,
                reasoning_effort="low",
            )

        try:
            response = self._service.call(ask, classify_groq_error)
        except OnlineError as error:
            return None, self._failed(error, went_online=True)
        tokens = _total_tokens(response)
        self.ledger.add(tokens)
        return (response.choices[0].message.content or "").strip(), AnswerResult(text="", went_online=True, tokens=tokens)

    def find_youtube_video(self, words: str, on_going_online: Optional[Callable[[], None]] = None) -> Tuple[Tuple[str, ...], AnswerResult]:
        """F18 "summarise the YouTube video about X": one browser_search
        for his words. Returns (every result URL the search tool itself
        returned, the result). The caller picks a YouTube link from those
        URLs only -- never a link the model wrote (it could be invented)."""
        if not self._api_key:
            return (), self._failed(OnlineError(OnlineErrorKind.NO_KEY), went_online=False)
        try:
            from groq import Groq
        except ImportError as exc:
            return (), self._failed(OnlineError(OnlineErrorKind.LIBRARY_MISSING, str(exc)), went_online=False)
        cooling_until = self._service.cooling_until_wall()
        if cooling_until is not None:
            return (), self._failed(OnlineError(OnlineErrorKind.COOLING, until_wall=cooling_until), went_online=False)
        if self.ledger.left_today() < int(_WEB["min_tokens_per_call"]):
            return (), self._failed(OnlineError(OnlineErrorKind.BUDGET_USED_UP), went_online=False)
        if on_going_online is not None:
            on_going_online()

        def ask(timeout: float):
            client = Groq(api_key=self._api_key, timeout=timeout, max_retries=0)
            return client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": str(SETTINGS["youtube"]["find_instructions"])},
                          {"role": "user", "content": words}],
                tools=[{"type": "browser_search"}],
                tool_choice="required",
                max_tokens=int(_WEB["answer_max_tokens"]),
                reasoning_effort=str(_GROQ["reasoning_effort"]),
            )

        try:
            response = self._service.call(ask, classify_groq_error)
        except OnlineError as error:
            return (), self._failed(error, went_online=True)
        tokens = _total_tokens(response)
        self.ledger.add(tokens)
        message = response.choices[0].message
        return search_result_urls(message), AnswerResult(text="", went_online=True, tokens=tokens)

    def answer_live(
        self,
        question: str,
        on_going_online: Optional[Callable[[], None]] = None,
        should_stop: Callable[[], bool] = lambda: False,
        on_waiting: Optional[Callable[[float], None]] = None,
    ) -> AnswerResult:
        """F16 Stage 1: a question that needs live information (decided by
        jarvis/actions/web_query.py, never by the model), answered with
        Groq's browser_search tool. Same R1/R3/R14 rules as answer(), plus
        the token budget. `should_stop` is checked while waiting for the
        per-minute allowance (R8)."""
        if not self._api_key:
            return self._failed(OnlineError(OnlineErrorKind.NO_KEY), went_online=False)
        try:
            from groq import Groq
        except ImportError as exc:
            return self._failed(OnlineError(OnlineErrorKind.LIBRARY_MISSING, str(exc)), went_online=False)
        cooling_until = self._service.cooling_until_wall()
        if cooling_until is not None:
            return self._failed(OnlineError(OnlineErrorKind.COOLING, until_wall=cooling_until), went_online=False)
        needed = int(_WEB["min_tokens_per_call"])
        if self.ledger.left_today() < needed:
            return self._failed(OnlineError(OnlineErrorKind.BUDGET_USED_UP), went_online=False)
        wait = self.ledger.minute_wait(needed)
        if wait > float(_WEB["max_wait_seconds"]):
            return self._failed(OnlineError(OnlineErrorKind.MINUTE_LIMIT, retry_after_seconds=wait), went_online=False)
        if wait > 0:
            if on_waiting is not None:
                on_waiting(wait)
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                if should_stop():
                    return AnswerResult(text="", went_online=False)
                self._sleep(min(0.2, max(0.0, deadline - time.monotonic())))

        if on_going_online is not None:
            on_going_online()

        prompt = f"{self.system_prompt}\n{_WEB['live_instructions']}"

        def ask(timeout: float):
            client = Groq(api_key=self._api_key, timeout=timeout, max_retries=0)
            raw = client.chat.completions.with_raw_response.create(
                model=self.model,
                messages=[{"role": "system", "content": prompt}, {"role": "user", "content": question}],
                tools=[{"type": "browser_search"}],
                tool_choice="required",
                max_tokens=int(_WEB["answer_max_tokens"]),
                reasoning_effort=str(_GROQ["reasoning_effort"]),
            )
            try:
                self.ledger.note_headers(raw.headers)
            except Exception:
                logger.debug("No rate-limit headers on the live answer", exc_info=True)
            return raw.parse()

        try:
            response = self._service.call(ask, classify_groq_error)
        except OnlineError as error:
            return self._failed(error, went_online=True)

        tokens = _total_tokens(response)
        self.ledger.add(tokens)
        choice = response.choices[0]
        searched_for, opened = _walk_tools(choice.message)
        sources = tuple(dict.fromkeys(u for u, _ in opened))
        pages = source_hosts(sources)
        text = to_speakable(_CITATION.sub("", choice.message.content or "").strip())
        when, page = result_date(opened, text, self.today(), int(_WEB["date_search_chars"]))
        live = dict(searched_for=searched_for, pages=pages, sources=sources, tokens=tokens, result_date=when, result_page=page)

        def failed(kind: OnlineErrorKind) -> AnswerResult:
            result = self._failed(OnlineError(kind), went_online=True)
            return AnswerResult(result.text, True, result.error, **live)

        if searched_for is None:
            return failed(OnlineErrorKind.NO_RESULTS)  # the tool never ran: a memory answer would look live
        if not text:
            return failed(OnlineErrorKind.EMPTY_RESPONSE)
        trimmed = False
        if getattr(choice, "finish_reason", None) == "length":
            text, trimmed = trim_to_last_sentence(text), True
            if not text:
                return failed(OnlineErrorKind.CUT_OFF)
        return AnswerResult(text=text, went_online=True, trimmed=trimmed, **live)

    @staticmethod
    def _failed(error: OnlineError, went_online: bool) -> AnswerResult:
        logger.info("Groq Q&A unavailable (%s) -- answering with the setup message (R14)", error.kind.name)
        return AnswerResult(text=message_for(error), went_online=went_online, error=error.kind,
                            error_detail=error.detail[:300] if error.detail else None)
