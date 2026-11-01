"""F4: File Finder.

"Find my resume" searches by name (jarvis/actions/file_index.py, a
background-refreshed index, not a full scan per search) and either opens
the single match directly, or lists the likely matches and waits for the
developer to pick one.

Stateful on purpose: FileFinder remembers the last set of listed matches
so a follow-up "open <selection>" can resolve against them. This mirrors
F7's PendingConfirmation in spirit (remembers exactly one pending thing,
voided by a new search) but is F4's own, simpler mechanism -- F4 has no
expiry/exact-match-of-parameters semantics like L2-L4 do, since Section 6
doesn't ask for that here.
"""

from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional, Tuple

from jarvis.actions.command_text import (PickSlot, looks_like_a_pick, name_pick, names_a_file, not_on_list_text, out_of_range_text, pick_index,
                                         pick_selector)
from jarvis.actions.file_index import FileIndex, FileMatch, clean_query, split_type_words
from jarvis.actions.launch_verify import launch_and_verify_with_retry
from jarvis.controller import EventType
from jarvis.interpreter import ActionRequest, Answer
from jarvis.logbook import Candidate
from jarvis.platform import current, is_mac
from jarvis.security import PathIntent, check_path
from jarvis.settings import SETTINGS

logger = logging.getLogger(__name__)

_CFG = SETTINGS["file_finder"]
_FIND_PREFIX = _CFG["find_command_prefix"]
_VERIFY_TIMEOUT = float(_CFG["verify_timeout_seconds"])
_VERIFY_POLL = float(_CFG["verify_poll_interval_seconds"])
_FOLDER_WORDS = {str(w).lower() for w in _CFG["folder_words"]}
_RUNS_WHEN_OPENED = {str(e).lower() for e in _CFG["runs_when_opened"]}
_RUNS_WHEN_OPENED_MAC = {str(e).lower() for e in _CFG.get("runs_when_opened_mac", [])}
_FILE_WORDS = {"file", "files", "document", "documents"}


def listing_lines(matches: List[FileMatch]) -> List[str]:
    """One line per result, file NAMES only -- never a folder or a path, on
    screen or spoken (the full path stays in the decision
    record, which is needed to open the file). Two results with the same
    name get (1), (2) after it."""
    names = [m.filename.lower() for m in matches]
    seen: Dict[str, int] = {}
    lines = []
    for i, m in enumerate(matches, 1):
        text = f"{i}. {m.filename}"
        if names.count(m.filename.lower()) > 1:
            seen[m.filename.lower()] = seen.get(m.filename.lower(), 0) + 1
            text += f" ({seen[m.filename.lower()]})"
        lines.append(text + (" (folder)" if m.is_folder else ""))
    return lines


def found_list_text(matches: List[FileMatch], total: int) -> str:
    """What she SAYS about a list: only how many. The
    names are shown on screen, never read out."""
    if all(m.is_folder for m in matches):
        noun = "folders"
    elif any(m.is_folder for m in matches):
        noun = "matches"
    else:
        noun = "files"
    return f"I found {total} {noun}. Which one would you like to open?"


def _found_text(count: int, running: bool = False, shown: int | None = None) -> str:
    noun = "match" if count == 1 else "matches"
    text = f"found {count} {noun}"
    if running:
        return text + " so far"
    if shown is not None and shown < count:
        text += f", showing the best {shown}"
    return text


def _to_logbook_candidates(matches: List[FileMatch]) -> List[Candidate]:
    return [Candidate(m.filename, m.score) for m in matches]


class FileFinder:
    """F4: 'find <name>' lists the best matches from the index; one match opens straight away (L1), several
    are listed to pick from."""
    def __init__(self, index: FileIndex, slot: Optional[PickSlot] = None) -> None:
        self._index = index
        self._last_matches: Optional[List[FileMatch]] = None
        self._listed_at = 0.0
        # Shared with F3's "which app?" list: only the newest list is
        # picked from. register() sets the orchestrator's.
        self._slot = slot
        # Set by register(): where to say what the search is doing right now.
        self._report_step = lambda text, replaces=None: None
        self._pick_registered = False

    def register_pick(self, interpreter, orchestrator) -> None:
        """The app puts the list pick first of ALL matchers, so a pick
        ("the second one", "open alex passport") is never taken by
        another command or sent online while a list is pending."""
        if self._slot is None:
            self._slot = getattr(orchestrator, "picks", None)
        interpreter.register("file_finder_open_selection", self.open_selection_matcher)
        self._pick_registered = True

    def on_transition(self, entry) -> None:
        """Registered via controller.add_transition_listener (see
        register() below) so Stop clears a pending file-selection list
        the same way it already clears F7's permit/pending confirmation
        -- a stale "open 1" surviving an unrelated Stop is the same
        shape of bug as three already found and fixed this build (the
        developer's review after step 7). entry.event_type is None only
        for a Stop-forced transition."""
        if entry.event_type is None or (entry.event_type == EventType.GO_TO_SLEEP and entry.accepted):
            self._last_matches = None  # Stop or sleep

    # -- matchers (registered with the interpreter) ------------------

    def find_matcher(self, normalized_text: str):
        """Handle 'find <name>': search the index and return an open request, a numbered list, or 'I can't
        find ...'."""
        if not normalized_text.startswith(_FIND_PREFIX):
            return None
        query = normalized_text[len(_FIND_PREFIX) :].strip()
        if not query:
            return None

        if not self._index.is_ready():
            self._last_matches = None
            return Answer("I'm still building the file index -- try again in a moment.")

        self._report_step("searching files...")
        total_found = [0]

        def on_progress(count: int) -> None:
            total_found[0] = count
            self._report_step(_found_text(count, running=True), replaces="found ")

        # "find jarvis folder": folder names only; "find my resume file":
        # files only. The word itself is not part of the name searched for.
        words = query.split()
        kind = "folder" if _FOLDER_WORDS & set(words) else "file" if _FILE_WORDS & set(words) else "any"
        # The reply named the raw words ("anything called 'my
        # called flamingo notes'"); it names what was searched for.
        query, suffixes = split_type_words(clean_query(query))
        matches = self._index.search(query, on_progress=on_progress, kind=kind, suffixes=suffixes)
        # The list is capped (max_results_shown), so say the real total and
        # how many of them are being shown.
        total = max(total_found[0], len(matches))
        self._report_step(_found_text(total, shown=len(matches)), replaces="found ")
        if not matches:
            self._last_matches = None
            if kind == "any":
                return Answer(f"I can't find anything called '{query}'.")
            return Answer(f"I can't find any {kind} called '{query}'.")

        if len(matches) == 1:
            self._last_matches = None
            m = matches[0]
            return ActionRequest(
                "open_file",
                {"path": m.path, "display_name": m.filename},
                candidates=_to_logbook_candidates(matches),
            )

        # F4: "lists the likely matches, and asks which one to open." The
        # numbered names go to the screen (Live Activity); she says only
        # how many there are.
        self._last_matches = matches
        if self._slot is not None:
            self._slot.claim(self)
            self._listed_at = self._slot.now()
        total = max(total_found[0], len(matches))
        for line in listing_lines(matches):
            self._report_step(line)
        return Answer(found_list_text(matches, total), candidates=_to_logbook_candidates(matches))

    def open_selection_matcher(self, normalized_text: str):
        """While a list is pending: "open 2", "opens second", "the second
        file please", "number two", "2", or the file's name or part of it
        ("open alex passport") all pick from THAT list (each of these
        once went online). A pick that matches
        nothing is answered here, never sent online; something that is
        clearly not a pick ("what time is it", "open chrome") is handled as
        a new command and the list stays for [interpreter]
        pick_valid_seconds. With "file" in it ("open file number 1") the
        file list answers even if an app list was shown since. Registered
        first of all matchers (see register_pick)."""
        if self._last_matches is None:
            return None
        if self._slot is not None and not self._slot.fresh(self._listed_at):
            self._expire()  # older than [interpreter] pick_valid_seconds
            return None
        if self._slot is not None and not self._slot.owned_by(self) and not names_a_file(normalized_text):
            return None  # a newer list (F3's "which app?") was shown since -- unless he said "file"
        matches = self._last_matches
        number = pick_index(normalized_text)
        if number is not None and not 1 <= number <= len(matches):
            # A number always answers from this list -- never an app search
            # ("open one" -> OneDrive/OneNote).
            return Answer(out_of_range_text(len(matches), "matches"))
        if number is not None:
            chosen = matches[number - 1]
        else:
            selector = pick_selector(normalized_text)
            index, tied = name_pick(selector, [m.filename for m in matches])
            if tied:
                return Answer(f"More than one file on the list matches '{selector}'. Say its number, from 1 to {len(matches)}.")
            chosen = matches[index] if index is not None else None
        if chosen is None:
            if looks_like_a_pick(normalized_text):
                return Answer(not_on_list_text(len(matches), "files"))
            # Not a pick ("what time is it", "open chrome"): handled as a new
            # command, and the list stays pickable.
            return None
        self._expire()
        return ActionRequest(
            "open_file",
            {"path": chosen.path, "display_name": chosen.filename},
            candidates=[Candidate(chosen.filename, chosen.score)],
        )

    def _expire(self) -> None:
        self._last_matches = None
        if self._slot is not None:
            self._slot.release(self)

    # -- registration ---------------------------------------------------

    def register(self, interpreter, orchestrator) -> None:
        """Pluggable registration, same pattern as F3's open_app.register
        -- not wired into jarvis/dashboard/app.py by this module, per the
        current dashboard-styling freeze. open_selection_matcher must be
        registered before any "open <name>" matcher (F3's) so a pick from
        this finder's own list is tried first. Also subscribes to the
        controller's transition stream (add_transition_listener, not
        set_transition_listener -- coexists with whatever app.py already
        set, e.g. the bridge+logbook combo) purely to clear a pending
        selection on Stop."""
        self._report_step = lambda text, replaces=None: orchestrator.report_step(text, replaces=replaces)
        if self._slot is None:
            self._slot = getattr(orchestrator, "picks", None)
        if not self._pick_registered:
            interpreter.register("file_finder_open_selection", self.open_selection_matcher)
        interpreter.register("file_finder_find", self.find_matcher)
        orchestrator.register_executor("open_file", open_file_executor)
        orchestrator.controller.add_transition_listener(self.on_transition)


# -- executor -----------------------------------------------------------


def open_file_executor(action_name: str, params: Dict) -> Tuple[bool, str]:
    """Open a file with its default app after the R9 path check (code files are shown in File Explorer,
    never run). Returns (verified, what to say)."""
    path = params.get("path", "")
    display_name = params.get("display_name", path)

    # R9: read-only, so checked against the protected list, not the
    # approved-folders list (that's for writes).
    allowed, reason = check_path(path, PathIntent.READ)
    if not allowed:
        logger.warning("Refusing to open %r: %s", path, reason)
        # The reason names the full path: kept in the log, never spoken.
        return False, f"I can't open {display_name}: it's in a protected folder."

    if is_mac():
        return _open_on_mac(path, display_name)
    if os.path.splitext(path)[1].lower() in _RUNS_WHEN_OPENED:
        return _show_in_explorer(path, display_name, str(_CFG["shown_not_run"]))
    if not _has_opener(path):
        return _show_in_explorer(path, display_name, str(_CFG["shown_no_app"]))

    def launch_once() -> bool:
        try:
            os.startfile(path)  # noqa: S606 -- fixed action, not a shell/terminal command (R10 doesn't apply)
            return True
        except OSError as exc:
            logger.warning("os.startfile(%r) failed: %s", path, exc)
            return False

    verified, needed_retry = launch_and_verify_with_retry(launch_once, _VERIFY_TIMEOUT, _VERIFY_POLL)
    if verified:
        return True, f"Opened {display_name}." + (" (took a retry)" if needed_retry else "")
    return False, f"I couldn't confirm {display_name} opened."


def _open_on_mac(path: str, display_name: str) -> Tuple[bool, str]:
    """macOS: `open` the file, but a script or app is only shown in Finder
    (`open -R`), never run. `open` fails when no app handles the file, and
    then the file is shown in Finder instead."""
    platform = current()
    extension = os.path.splitext(path)[1].lower()
    if extension in _RUNS_WHEN_OPENED or extension in _RUNS_WHEN_OPENED_MAC:
        shown = platform.show_in_file_manager(path)
        return (True, str(_CFG["shown_not_run_mac"]).format(name=display_name)) if shown else             (False, f"I couldn't show {display_name} in Finder.")
    verified, needed_retry = launch_and_verify_with_retry(lambda: platform.open_path(path), _VERIFY_TIMEOUT, _VERIFY_POLL)
    if verified:
        return True, f"Opened {display_name}." + (" (took a retry)" if needed_retry else "")
    if platform.show_in_file_manager(path):
        return True, str(_CFG["shown_no_app_mac"]).format(name=display_name)
    return False, f"I couldn't confirm {display_name} opened."


def _has_opener(path: str) -> bool:
    """Whether Windows has an app for this file. os.startfile on
    an extensionless header ("UmfPackSupport") showed Windows' "Pick an app"
    dialog and blocked -- she sat in ACTING until it was closed. Folders
    always open (File Explorer)."""
    if os.path.isdir(path) or not os.path.exists(path):
        return True  # a missing file fails in os.startfile and is reported as before
    try:
        import win32api
    except ImportError:
        return True  # R14: without pywin32 the old behaviour stays
    try:
        _instance, program = win32api.FindExecutable(path)
    except Exception:  # pywintypes.error: no association (SE_ERR_NOASSOC) or unreadable
        return False
    return bool(program)


def _show_in_explorer(path: str, display_name: str, reply: str) -> Tuple[bool, str]:
    """A script or program is never opened (Windows would run it), and a
    file no app opens never gets the "Pick an app" dialog: File Explorer
    opens its folder with the file selected. Explorer is an app, not a
    terminal (R10), and nothing in the file runs."""
    import subprocess

    def launch_once() -> bool:
        try:
            subprocess.Popen(["explorer.exe", f"/select,{os.path.normpath(path)}"], shell=False)
            return True
        except OSError as exc:
            logger.warning("Showing %r in File Explorer failed: %s", path, exc)
            return False

    verified, _retry = launch_and_verify_with_retry(launch_once, _VERIFY_TIMEOUT, _VERIFY_POLL)
    if verified:
        return True, reply.format(name=display_name)
    return False, f"I couldn't confirm File Explorer showed {display_name}."
