"""F3: Open Any Application.

The interpreter matcher (open_app_matcher) resolves the name at match
time -- app first, website fallback only if no app matched (F3's own
spec order) -- and hands back an ActionRequest with the resolved target
already decided, plus the candidates considered for F5's decision record.

The executors (registered separately, see jarvis/dashboard/__init__.py's
future wiring -- not wired into app.py by this module itself, per the
developer's current dashboard-styling freeze) do the actual launching and
R12 verification.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Dict, List, Optional, Tuple

import re

from jarvis.actions.app_finder import (
    STORE_APP_PREFIX,
    STORE_APPS,
    ScoredMatch,
    bare_name,
    canonical_app_name,
    closest_site,
    confident,
    find_app,
    find_chrome,
    find_known_site,
    find_website_fallback,
)
from jarvis.actions.command_text import (PickSlot, is_pick_shaped, looks_like_a_pick, name_pick, names_a_file, not_on_list_text,
                                         out_of_range_text, pick_index, pick_selector, strip_determiners)
from jarvis.actions.launch_verify import launch_and_verify_with_retry
from jarvis.controller import EventType
from jarvis.interpreter import ActionRequest, Answer
from jarvis.logbook import Candidate
from jarvis.platform import current, is_mac
from jarvis.security import PathIntent, check_path
from jarvis.settings import SETTINGS

logger = logging.getLogger(__name__)

_VERIFY_TIMEOUT = float(SETTINGS["app_finder"]["verify_timeout_seconds"])
_VERIFY_POLL = float(SETTINGS["app_finder"]["verify_poll_interval_seconds"])


# -- the interpreter matcher ---------------------------------------------


def _to_logbook_candidates(matches: List[ScoredMatch]) -> List[Candidate]:
    return [Candidate(m.display_name, m.score) for m in matches]


_NOT_SURE_HEADER = "I'm not sure which app you mean:"
_PICK_HINT = "Say 'open' and the number to pick one."


def _ask_which(matches: List[ScoredMatch]) -> Answer:
    """F3 safety rule: an unsure match is never launched -- the
    top matches are listed as a numbered pick, the same way F4 lists files."""
    lines = [_NOT_SURE_HEADER, *(f"{i + 1}. {m.display_name}" for i, m in enumerate(matches)), _PICK_HINT]
    return Answer("\n".join(lines), candidates=_to_logbook_candidates(matches))


# Matched on the cleaned command (no "please"/"sir").
# "open chat gpt on chrome", "sir, chat gpd on chrome please" (no "open").
_ON_CHROME = re.compile(r"^(?:(?P<verb>open|go to|launch|start) )?(?P<site>.+?) (?:on|in|using|with) (?:google )?chrome$")
# "open chrome and then search chat gpt": Chrome opens; searching is F16.
_CHROME_SEARCH = re.compile(r"^open (?:google )?chrome,? (?:and )?(?:then )?(?:search|look up|google)(?: for)? (?P<query>.+)$")
_SEARCH_NOT_BUILT = str(SETTINGS["app_finder"]["search_not_built_text"])
_UNKNOWN_SITE = "I don't know a website called '{site}' yet. Add it to [app_finder.website_fallbacks] in settings.toml, then restart JARVIS."


def site_name(site: ScoredMatch) -> str:
    """How she says a site: "ChatGPT", never its URL."""
    names = {" ".join(str(k).lower().split()): str(v) for k, v in SETTINGS["app_finder"].get("site_names", {}).items()}
    key = " ".join(site.display_name.lower().split())
    return names.get(key, site.display_name.title())


def _url_request(site: ScoredMatch, chrome_path: str) -> ActionRequest:
    """open_url: the URL is for the launch and the decision record only;
    what is said and shown is the site's name."""
    return ActionRequest(
        "open_url",
        {"url": site.target, "browser_path": chrome_path, "display_name": site_name(site)},
        candidates=_to_logbook_candidates([site]),
    )


def _open_on_chrome(site_words: str, progress=None) -> Optional[object]:
    site_words = strip_determiners(site_words)  # "open your youtube in chrome"
    site = find_known_site(site_words)
    if site is None:
        return None
    if progress is not None:
        progress(f"searching for {site_words}")
    chrome_path = find_chrome()
    if chrome_path is None:
        return Answer(f"'{site_name(site)}' is a website, but Chrome isn't installed, so I can't open it.")
    return _url_request(site, chrome_path)


def open_app_matcher(normalized_text: str, progress=None, on_ambiguous=None) -> Optional[object]:
    """`progress(text)`, if given, is told what the search is doing (shown
    live in the dashboard's activity panel). `on_ambiguous(matches)`, if
    given, is told the list she asked the developer to pick from."""
    search = _CHROME_SEARCH.match(normalized_text)
    if search is not None:
        opened = _open_named_app("chrome", progress, on_ambiguous)
        if isinstance(opened, ActionRequest):
            # Chrome opens. (F16b's search matcher normally takes "open chrome and
            # search X" first; this note only covers a phrasing it missed.)
            return ActionRequest(opened.action_name, {**opened.params, "note": _SEARCH_NOT_BUILT}, opened.candidates)
        return opened

    on_chrome = _ON_CHROME.match(normalized_text)
    if on_chrome is not None:
        site_words = on_chrome.group("site")
        result = _open_on_chrome(site_words, progress)
        if result is not None:
            return result
        if on_chrome.group("verb"):
            return Answer(_UNKNOWN_SITE.format(site=site_words))
        return None  # "... in chrome" without "open" and no known site: not a command

    if not normalized_text.startswith("open "):
        # A bare app or site name on its own ("VS Code") opens it.
        bare = bare_name(normalized_text)
        return _open_named_app(bare, progress, on_ambiguous) if bare is not None else None
    name = strip_determiners(normalized_text[len("open ") :].strip())  # "open my notepad"
    if not name:
        return None
    return _open_named_app(name, progress, on_ambiguous)


def _open_named_app(name: str, progress=None, on_ambiguous=None) -> object:
    if progress is not None:
        progress(f"searching for {name}")
    name = canonical_app_name(name)
    app_matches = find_app(name)
    if app_matches and not set(name.lower().split()) <= set(app_matches[0].display_name.lower().split()):
        # "open Google Maps" opened Windows' Maps app (0.85, the
        # word "maps") although Google Maps is a known site, named exactly.
        # An app missing a word he said yields to a site named exactly; an
        # app with all his words still comes first (F3).
        exact_site = find_website_fallback(name)
        if exact_site is not None and exact_site.score >= 1.0:
            app_matches = []
    if app_matches:
        if not confident(app_matches):
            if on_ambiguous is not None:
                on_ambiguous(app_matches)
            return _ask_which(app_matches)
        best = app_matches[0]
        return ActionRequest(
            "open_app",
            {"target": best.target, "display_name": best.display_name},
            candidates=_to_logbook_candidates(app_matches),
        )

    # F3: "checks for an installed app first, and if there is none she
    # opens the site in Chrome."
    website = find_website_fallback(name)
    if website is not None:
        chrome_path = find_chrome()
        if chrome_path is None:
            return Answer(f"'{site_name(website)}' looks like a website, but Chrome isn't installed, so I can't open it.")
        _guess, sure = closest_site(name)
        if not sure and website.score < 1.0 and CLARIFIER is not None:
            # "Open Cloud" opened Claude straight away -- a rough match asks first.
            return CLARIFIER.ask([(site_name(website), _url_request(website, chrome_path))])
        return _url_request(website, chrome_path)

    # "Open Cloud" / "Open plod" / "open claude" -> "can't find".
    # A close known site: sure -> open it; likely -> "Did you mean Claude?";
    # otherwise his words are searched for in his Chrome. Never a URL spoken.
    guess, sure = closest_site(name)
    chrome_path = find_chrome()
    if guess is not None and chrome_path is not None:
        if sure:
            return _url_request(guess, chrome_path)
        if CLARIFIER is not None:
            return CLARIFIER.ask([(site_name(guess), _url_request(guess, chrome_path))])
    if chrome_path is None:
        return Answer(str(SETTINGS["app_finder"]["not_found_no_chrome"]).format(name=name))
    from urllib.parse import quote_plus

    return ActionRequest("open_url", {"url": f"https://www.google.com/search?q={quote_plus(name)}", "browser_path": chrome_path,
                                      "display_name": "Google", "reason": "app not found",
                                      "spoken": str(SETTINGS["app_finder"]["not_found_search"]).format(name=name)})


# The intent router's "Did you mean ...?" (set by register_matchers).
CLARIFIER = None


# -- executors --------------------------------------------------------------
# R12 verification (retry once, report plainly either way) is shared with
# F4 via jarvis/actions/launch_verify.py, not duplicated here.


def open_app_executor(action_name: str, params: Dict) -> Tuple[bool, str]:
    """F3: launch an installed app after the R9 path check, then verify a new window appeared (R12).
    Returns (verified, what to say)."""
    target = params.get("target", "")
    display_name = params.get("display_name", target)

    # R9: every module MUST call the one shared path check, even for a
    # read-only "open" -- Start Menu shortcuts are never inside the
    # protected list in practice, but the rule applies unconditionally,
    # not just where a violation seems plausible.
    # A Store app's target is its package app ID inside the shell's Apps
    # folder, not a file: the ID itself is what R9 checks (it has no folder).
    checked = target[len(STORE_APP_PREFIX):] if target.startswith(STORE_APP_PREFIX) else target
    allowed, reason = check_path(checked, PathIntent.READ)
    if not allowed:
        logger.warning("Refusing to open %r: %s", target, reason)
        return False, f"I can't open that: {reason}"

    def launch_once() -> bool:
        if is_mac():
            return current().open_app(target)
        try:
            os.startfile(target)  # noqa: S606 -- fixed action, not a shell/terminal command (R10 doesn't apply)
            return True
        except OSError as exc:
            logger.warning("os.startfile(%r) failed: %s", target, exc)
            return False

    note = params.get("note")
    verified, needed_retry = launch_and_verify_with_retry(launch_once, _VERIFY_TIMEOUT, _VERIFY_POLL)
    if verified:
        text = f"Opened {display_name}." + (" (took a retry)" if needed_retry else "")
        return True, f"{text} {note}" if note else text
    return False, f"I couldn't confirm {display_name} opened."


def open_url_executor(action_name: str, params: Dict) -> Tuple[bool, str]:
    """Open a URL in his own Chrome (F16b/F18). Returns (verified, what to say)."""
    url = params.get("url", "")
    browser_path = params.get("browser_path", "")
    # Said and shown: the site's name, never the URL.
    name = params.get("display_name") or "the website"

    def launch_once() -> bool:
        if is_mac():
            return current().open_url(url, browser_path or None)
        try:
            subprocess.Popen([browser_path, url], shell=False)
            return True
        except OSError as exc:
            logger.warning("Launching Chrome for %r failed: %s", url, exc)
            return False

    verified, needed_retry = launch_and_verify_with_retry(launch_once, _VERIFY_TIMEOUT, _VERIFY_POLL)
    note = params.get("note")
    if verified:
        if params.get("search") is not None:  # F16b: a Google search in his Chrome
            from jarvis.browser.commands import search_spoken

            text = search_spoken(params)
        elif params.get("spoken"):  # F18: "Searching YouTube for X in Chrome."
            text = str(params["spoken"])
        else:
            text = f"Opening {name} in Chrome."
        text += " (took a retry)" if needed_retry else ""
        return True, f"{text} {note}" if note else text
    return False, f"I couldn't confirm {name} opened in Chrome."


class AppPicker:
    """Remembers the list from the last "I'm not sure which app you mean"
    so "open 2" (or "open number two", "open the second one") picks from
    it. The list stays pickable for [interpreter] pick_valid_seconds
    It was the very next command only), until a pick, a
    newer list, Stop or sleep -- an old list must never decide what opens
    later. "file" in the words ("open file number 2") is never a pick from
    this list: in testing it opened OneNote."""

    def __init__(self, slot: Optional[PickSlot] = None) -> None:
        self._pending: Optional[List[ScoredMatch]] = None
        self._slot = slot  # shared with F4: only the newest list is picked from
        self._listed_at = 0.0

    def remember(self, matches: List[ScoredMatch]) -> None:
        """Keep `matches` as the 'which app?' list that a pick answers."""
        self._pending = list(matches)
        if self._slot is not None:
            self._slot.claim(self)
            self._listed_at = self._slot.now()

    def on_transition(self, entry) -> None:
        """Drop the pending list on Stop or sleep."""
        if entry.event_type is None or (entry.event_type == EventType.GO_TO_SLEEP and entry.accepted):  # Stop, sleep
            self._pending = None

    def selection_matcher(self, normalized_text: str) -> Optional[object]:
        """"open 2", "the second one", "number two", or the app's name.
        Something that isn't a pick at all ("what time is it") is handled as
        a new command; the list stays."""
        pending = self._pending
        if pending is not None and self._slot is not None and not self._slot.owned_by(self):
            if not self._slot.fresh(self._listed_at):
                self._pending = None
            return None  # a newer list (F4's) was shown since, or this one is too old
        if pending is None or names_a_file(normalized_text):
            return None
        number = pick_index(normalized_text)
        if number is not None and not 1 <= number <= len(pending):
            # A number always answers from the list -- never an app search
            # for "one" ("open one" -> OneDrive/OneNote).
            return Answer(out_of_range_text(len(pending), "apps"))
        chosen = _pick(pick_selector(normalized_text), pending, number)
        if chosen is None:
            if looks_like_a_pick(normalized_text):
                return Answer(not_on_list_text(len(pending), "apps"))
            return None
        self._pending = None
        if self._slot is not None:
            self._slot.release(self)
        return ActionRequest(
            "open_app",
            {"target": chosen.target, "display_name": chosen.display_name},
            candidates=[Candidate(chosen.display_name, chosen.score)],
        )


def no_list_matcher(slot: Optional[PickSlot]):
    """After every pick matcher: a pick by number with NO
    list pending ("open number one please", "open file number 1", "open
    one") is answered locally, never searched for as an app or sent
    online."""

    def matcher(normalized_text: str) -> Optional[Answer]:
        if slot is not None and slot.active():
            return None  # a list is pending; its own matcher answers
        if is_pick_shaped(normalized_text):
            return Answer(str(SETTINGS["interpreter"]["no_list_text"]))
        return None

    return matcher


def _pick(selector: str, pending: List[ScoredMatch], number: Optional[int]) -> Optional[ScoredMatch]:
    if number is not None:
        return pending[number - 1]
    index, _tied = name_pick(selector, [m.display_name for m in pending])
    return pending[index] if index is not None else None


def register(interpreter, orchestrator, picker: Optional[AppPicker] = None, prefetch: bool = True) -> None:
    """Pluggable registration. The pick matcher goes first so "open 2" right
    after a "which app?" list picks from it. The app registers its own
    `picker` at the very front of the matcher order (any pick, before any
    other command); without one it is registered here."""
    if picker is None:
        picker = AppPicker(getattr(orchestrator, "picks", None))
        interpreter.register("open_app_pick", picker.selection_matcher)
    if prefetch:
        STORE_APPS.prefetch()  # Store apps load in the background (the app does it after voice, R15)
    interpreter.register(
        "open_app",
        lambda text: open_app_matcher(text, progress=orchestrator.report_step, on_ambiguous=picker.remember),
    )
    orchestrator.register_executor("open_app", open_app_executor)
    orchestrator.register_executor("open_url", open_url_executor)
    orchestrator.controller.add_transition_listener(picker.on_transition)
