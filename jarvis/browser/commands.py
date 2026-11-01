"""F16b commands: "search X", "google X", "open chrome
and (then) search X" -> a Google search for X opened in the developer's
own Chrome, exactly like opening a website (F3's `open_url`, L1): no
automation, no JARVIS browser, nothing read back. She says "Searching
Google for X in Chrome."

Also F16's last resort: when a live online question fails (timed out, a
service error, an empty answer, nothing found) the Groq conversation hands
the question here (Conversation.browser_fallback), and she says "I
couldn't get a quick answer, so I've opened the results in Chrome."

Before this opened DuckDuckGo in JARVIS's own Playwright
browser and read the result titles aloud; that browser, its Chromium
download and DuckDuckGo are gone.

R3: the search page is a website he asked to open (or, for the fallback,
the words he asked), so Live Activity names Google, never a URL.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import quote_plus

from jarvis.actions.app_finder import find_chrome
from jarvis.interpreter import ActionRequest, Answer
from jarvis.settings import SETTINGS

_CFG = SETTINGS["browser"]
_REPLIES = _CFG["replies"]
_PATTERNS = [re.compile(str(p["pattern"])) for p in _CFG["commands"]]
_GOOGLE_NOT_QUERIES = {str(q).lower() for q in _CFG["google_not_queries"]}
_QUERY_LEAD = sorted((str(w).lower().split() for w in _CFG.get("query_lead_words", [])), key=len, reverse=True)


def _without_lead(query: str) -> str:
    """"Search Google's best pizza in Sydney" searched
    for "google's best pizza in sydney". The engine's name is not the query."""
    words = query.split()
    stripped = True
    while stripped and len(words) > 1:
        stripped = False
        for lead in _QUERY_LEAD:
            if words[: len(lead)] == lead and len(words) > len(lead):
                words, stripped = words[len(lead):], True
                break
    return " ".join(words)

ASKED = "asked"
FALLBACK = "fallback"


def parse_search(text: str) -> Optional[str]:
    """The words to search for in a search command, else None.
    "open chrome and then search chat gpt" -> "chat gpt"."""
    sentence = " ".join(text.lower().replace(",", " ").split()).rstrip(".!?")
    for pattern in _PATTERNS:
        match = pattern.fullmatch(sentence)
        if match is None:
            continue
        query = _without_lead(match.group("query").strip())
        if not query or (sentence.startswith("google ") and query in _GOOGLE_NOT_QUERIES):
            return None
        return query
    return None


def search_url(query: str) -> str:
    """The Google search URL for `query`."""
    return str(_CFG["search_url"]).format(query=quote_plus(query))


def search_request(query: str, reason: str = ASKED, chrome_path: Optional[str] = None) -> object:
    """open_url for a Google search of `query` in his Chrome. No Chrome:
    she says so (R14) instead."""
    chrome_path = chrome_path or find_chrome()
    if chrome_path is None:
        key = "no_chrome_fallback" if reason == FALLBACK else "no_chrome"
        return Answer(str(_REPLIES[key]).format(query=query))
    return ActionRequest("open_url", {"url": search_url(query), "browser_path": chrome_path, "display_name": "Google",
                                      "search": query, "reason": reason})


def search_spoken(params: dict) -> str:
    """What she says once the search page has opened."""
    if params.get("reason") == FALLBACK:
        return str(_REPLIES["fallback_opened"])
    return str(_REPLIES["searching"]).format(query=params.get("search", ""))


class BrowserCommands:
    """F16b: 'search for X' / 'google X' opens a Google results page in his own Chrome (L1); also the
    fallback when a live web search fails."""
    def register(self, interpreter, orchestrator) -> None:
        """Before F16a/F4/F3: "open chrome and search X" must not open
        Chrome as a bare app, and "google the weather" is a search."""
        interpreter.register("browser_search", self.matcher)

    def matcher(self, cleaned_text: str):
        """A search command: the request to open its Google results in Chrome. None otherwise."""
        query = parse_search(cleaned_text)
        if query is None:
            return None
        return search_request(query, ASKED)

    @staticmethod
    def request(question: str, reason: str = FALLBACK) -> object:
        """The open-in-Chrome request for `question`, marked as asked or as a fallback."""
        return search_request(question, reason)
