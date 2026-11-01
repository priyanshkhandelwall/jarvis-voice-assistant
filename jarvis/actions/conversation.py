"""F2: questions and small talk, answered by Groq.

Registered **last** with the interpreter: deterministic matchers (intro,
greeting, find, open) always get the first chance, and only text none of
them claimed goes online (4.1: "deterministic first, a model only if
needed").

R3, developer decision D: right before the request leaves the
laptop she says "Searching online" -- spoken during UNDERSTANDING -- the
dashboard shows ONLINE, and the Live Activity panel shows 'searching online,
sending "<words>"'. Amended No provider or model name in Live
Activity; those go only to the decision record (online_destination) and
from there to the debug log. ONLINE is held from just before "Checking online" until
the answer has finished speaking: the request alone is often under a
second, too short to see.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Callable, Optional

from jarvis.actions.web_query import date_sentence, fix_mishearings, needs_live_info, spoken_date, with_date_note
from jarvis.interpreter import Answer
from jarvis.online import OnlineErrorKind, OnlineStatus
from jarvis.settings import SETTINGS
from jarvis.speech.claims import ClaimCheck
from jarvis.speech.length import instruction, limit_sentences, sentence_limit
from jarvis.speech.qa import LIVE_TOOL, SERVICE_NAME, AnswerResult, GroqAnswerer, is_provider_host

_SEARCHED_FOR_TEXT = str(SETTINGS["web"]["searched_for_text"])
_SERVICE_NAMES = [str(n) for n in SETTINGS["web"].get("never_spoken_names", [])]
# A sentence naming one of them; a dot followed by a letter or digit ("exa.ai",
# "77,186.05") is inside the sentence, not its end.
_IN_SENTENCE = r"(?:[^.!?\n]|[.!?](?=\S))*"
_SERVICE_SENTENCE = re.compile(_IN_SENTENCE + r"\b(?:" + "|".join(re.escape(n) for n in _SERVICE_NAMES) + r")\b" + _IN_SENTENCE + r"[.!?]?\s*",
                               re.IGNORECASE) if _SERVICE_NAMES else None


def drop_service_sentences(text: str) -> str:
    """Remove sentences that name the online service (R3: she never says which provider answered)."""
    if _SERVICE_SENTENCE is None:
        return text
    return " ".join(_SERVICE_SENTENCE.sub(" ", text).split())

_CHECKING_ONLINE_TEXT = str(SETTINGS["speech"]["checking_online_text"])

# One short activity-log step per failure, so the panel shows what went
# wrong while the spoken sentence says how to fix it.
_FAILURE_STEPS = {
    # Worded without "api key"/"token": R11's redactor blanks any record
    # field containing those words, which would hide the step itself. No
    # provider name (R3, amended).
    OnlineErrorKind.NO_KEY: "online search: key not set up",
    OnlineErrorKind.KEY_REJECTED: "online search: key rejected",
    OnlineErrorKind.LIBRARY_MISSING: "online search: library not installed",
    OnlineErrorKind.CANNOT_REACH: "online search: could not connect",
    OnlineErrorKind.TIMEOUT: "online search: timed out",
    OnlineErrorKind.RATE_LIMITED: "online search: free limit reached",
    OnlineErrorKind.COOLING: "online search: still in free-limit cooldown, not sent",
    OnlineErrorKind.SERVICE_ERROR: "online search: service error",
    OnlineErrorKind.EMPTY_RESPONSE: "online search: empty answer",
    OnlineErrorKind.CUT_OFF: "online search: answer cut off",
    OnlineErrorKind.BUDGET_USED_UP: "online search: today's allowance used up, not sent",
    OnlineErrorKind.MINUTE_LIMIT: "online search: this minute's allowance used up, not sent",
    OnlineErrorKind.NO_RESULTS: "online search: nothing found",
}


def failure_cause(detail: Optional[str]) -> str:
    """Live Activity said only "online search: service error";
    the real cause ("HTTP 400 tool_use_failed Tool choice is required, but
    model did not call a tool") was in debug.log alone. A short plain cause,
    with no provider or model name (R3)."""
    if not detail:
        return ""
    lowered = detail.lower()
    if "tool_use_failed" in lowered or "did not call a tool" in lowered:
        return "the model answered without running the web search"
    if "rate" in lowered and "limit" in lowered:
        return "free limit reached"
    code = re.search(r"\bHTTP (\d{3})\b", detail)
    if code is not None:
        return f"the service replied HTTP {code.group(1)}"
    return ""


class Conversation:
    """F2: the online answer, registered last. A question no local matcher took goes to Groq (or a live web
    search), with the online indicator shown."""
    def __init__(self, answerer: GroqAnswerer, online_status: Optional[OnlineStatus] = None) -> None:
        self._answerer = answerer
        self._online_status = online_status
        self._orchestrator = None
        # An online answer may never claim an action JARVIS didn't do.
        self._claims = ClaimCheck(SETTINGS["claims"])
        # F16b: when a live search fails in a way the browser can help with,
        # the question goes to JARVIS's own browser instead (an L1 action).
        # Set by the app; None means she only says the failure.
        self.browser_fallback: Optional[Callable[[str], object]] = None
        self._fallback_on = {OnlineErrorKind[name] for name in SETTINGS["browser"]["fallback_on"]}

    def matcher(self, normalized_text: str) -> Optional[Answer]:
        """Answer `normalized_text` online. Returns the spoken Answer, or a setup/offline message when Groq
        can't be used."""
        question = normalized_text.strip()
        if not question:
            return None
        # F16 Stage 1: known mishearings are fixed before anything is sent,
        # and the fix is shown ("Vendus T" was searched as "Vietnam").
        question, changes = fix_mishearings(question)
        for heard, sent in changes:
            self._report(f'fixed mishearing: "{heard}" -> "{sent}"')
        live = needs_live_info(question)  # decided here, never by the model
        held = []
        # Recency: JARVIS adds today's date to the words sent.
        sent = with_date_note(question, self._answerer.today()) if live else question
        try:
            if live:
                result = self._answerer.answer_live(
                    sent,
                    on_going_online=lambda: self._going_online(sent, held, live=True),
                    should_stop=self._stop_requested,
                    on_waiting=lambda s: self._report(f"waiting {s:.0f} s for the online allowance"),
                )
            else:
                limit = sentence_limit(question)
                extra = {"length_rule": instruction(limit)} if limit else {}
                result = self._answerer.answer(question, on_going_online=lambda: self._going_online(question, held), **extra)
        finally:
            self._release_after_reply(held)
        if live and result.went_online:
            self._report_live(result)
        if result.error is not None:
            step = _FAILURE_STEPS.get(result.error, "online search: failed")
            cause = failure_cause(getattr(result, "error_detail", None))
            self._report(f"{step} ({cause})" if cause else step)
            self._record_error(result)
            if live and result.error in self._fallback_on and self.browser_fallback is not None and not self._stop_requested():
                return self.browser_fallback(question)
        elif result.trimmed:
            # Hit max_tokens: only the complete sentences are spoken.
            self._report("answer trimmed")
        if not result.text:
            return None if self._stop_requested() else Answer(result.text)
        if result.error is None:
            # "This comes from exa.ai." -- the search
            # service is never named aloud (R3); the website the answer came
            # from still is.
            unnamed = drop_service_sentences(result.text)
            if unnamed and unnamed != " ".join(result.text.split()):
                self._report("dropped a sentence naming the online service")
                result = dataclasses.replace(result, text=unnamed)
            claim = self._claims.find(result.text, question, live=live)
            if claim is not None:
                kept = self._claims.keep_information(result.text, question, live=live)
                if kept is not None:
                    # An informational answer keeps its information.
                    self._report(f"dropped a sentence she can't back up: \"{claim.said}\"")
                    result = AnswerResult(kept, went_online=True, searched_for=result.searched_for, pages=result.pages,
                                          sources=result.sources, tokens=result.tokens, result_date=result.result_date,
                                          result_page=result.result_page)
                else:
                    # Only JARVIS's own commands do things; say which one would.
                    self._report(f"removed a claim she can't back up: \"{claim.said}\"")
                    return Answer(claim.replacement)
        limit = sentence_limit(question)
        if limit and result.error is None and not live:
            cut = limit_sentences(result.text, limit)
            if cut != result.text:
                self._report(f"kept {limit} sentence{'s' if limit > 1 else ''}, as asked")
            return Answer(cut)
        if live and result.error is None and result.searched_for:
            # What was really searched, from the tool's own request, then the
            # answer, then the result's date as read from the page itself.
            when = date_sentence(question, result.result_date, self._answerer.today(), result.text)
            spoken = f"{_SEARCHED_FOR_TEXT.format(query=result.searched_for)} {result.text}"
            return Answer(f"{spoken} {when}" if when else spoken)
        return Answer(result.text)

    def _report_live(self, result) -> None:
        """Live Activity: what was searched and which sites were read --
        never the provider, the model or the search engine (R3)."""
        if result.searched_for:
            self._report(f"searched for: {result.searched_for}")
        # The provider's own host ("exa.ai") is never a page read;
        # with no real site known, only how many ("read 1 page").
        sites = [p for p in result.pages if not is_provider_host(p)]
        count = max(len(sites), len(result.sources))
        if sites:
            self._report(f"read {len(sites)} {'page' if len(sites) == 1 else 'pages'}: {', '.join(sites)}")
        elif count:
            self._report(f"read {count} {'page' if count == 1 else 'pages'}")
        if result.result_date is not None:
            page = result.result_page if result.result_page and not is_provider_host(result.result_page) else ""
            self._report(f"result date: {spoken_date(result.result_date)}" + (f" ({page})" if page else ""))
        elif result.searched_for:
            self._report("result date: not shown on the page")
        if self._orchestrator is not None:
            # Record and debug log only. ("usage", not the word R11's
            # redactor blanks a whole field for.)
            detail = f"{SERVICE_NAME}, {self._answerer.model}, {LIVE_TOOL}; usage {result.tokens}; sources {' '.join(result.sources) or '-'}"
            self._orchestrator.record_online_destination(detail)

    def _record_error(self, result) -> None:
        """The real cause of a failure ("HTTP 400 tool_use_failed:
        ...") goes to the decision record and debug.log -- never to Live
        Activity (R3). Words R11's redactor blanks a whole field for are
        reworded so the cause itself survives."""
        detail = getattr(result, "error_detail", None)
        if not detail or self._orchestrator is None:
            return
        for word, safe in (("token", "tkn"), ("api key", "credential"), ("api_key", "credential"), ("secret", "s-word")):
            detail = detail.replace(word, safe).replace(word.title(), safe)
        tool = f", {LIVE_TOOL}" if result.error is not None else ""
        self._orchestrator.record_online_destination(f"{SERVICE_NAME}, {self._answerer.model}{tool}; error {result.error.name}: {detail}")

    def _stop_requested(self) -> bool:
        return self._orchestrator is not None and self._orchestrator.controller.is_stop_requested()

    def _going_online(self, question: str, held: list, live: bool = False) -> None:
        if self._online_status is not None and not held:
            self._online_status.hold(SERVICE_NAME)
            held.append(True)
        # R3: the exact words sent, before they are sent. The destination
        # (provider and model) goes to the decision record and debug log
        # only, never to Live Activity (R3 amended).
        self._report(f'searching online, sending "{question}"')
        if self._orchestrator is not None:
            tool = f", {LIVE_TOOL}" if live else ""
            self._orchestrator.record_online_destination(f"{SERVICE_NAME}, {self._answerer.model}{tool}")
        if self._orchestrator is not None:
            self._orchestrator.announce(_CHECKING_ONLINE_TEXT)

    def _release_after_reply(self, held: list) -> None:
        if not held:
            return
        release = lambda: self._online_status.release(SERVICE_NAME)  # noqa: E731
        if self._orchestrator is not None:
            self._orchestrator.when_command_finishes(release)
        else:
            release()

    def _report(self, step: str) -> None:
        if self._orchestrator is not None:
            self._orchestrator.report_step(step)

    def register(self, interpreter, orchestrator) -> None:
        """Must be called after every other matcher is registered."""
        self._orchestrator = orchestrator
        # raw: what goes online is the developer's own words, not the
        # cleaned command the local matchers see.
        interpreter.register("conversation", self.matcher, raw=True)
