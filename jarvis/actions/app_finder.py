"""F3: resolving a spoken/typed app name to something that can be opened.

Pure resolution logic -- no launching, no verification, no I/O beyond
reading the Start Menu folders and settings.toml. Kept separate from
open_app.py (which does the actual launching) so it's testable without
touching the real filesystem beyond two well-known, fast, C:-only
folders (no indexing needed here, unlike F4's slower/larger file search).
"""

from __future__ import annotations

import difflib
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from jarvis.actions.command_text import edit_distance
from jarvis.platform import current, is_mac
from jarvis.settings import SETTINGS

logger = logging.getLogger(__name__)

_CFG = SETTINGS["app_finder"]
MATCH_THRESHOLD = float(_CFG["match_threshold"])
# Launch only when the best match is at least this good AND clearly ahead
# of the runner-up; otherwise she lists the top matches and asks.
LAUNCH_THRESHOLD = float(_CFG["launch_threshold"])
AMBIGUITY_MARGIN = float(_CFG["ambiguity_margin"])
# Spoken nicknames -> the exact Start Menu name ("vs code" -> "Visual Studio Code").
_APP_ALIASES: dict = {k.lower().strip(): v for k, v in _CFG.get("aliases", {}).items()}
_SYSTEM_APP_ALIASES: dict = _CFG.get("system_app_aliases", {})
# A kind of app ("music app") -> installed apps that are one, in order.
_CATEGORIES: dict = {" ".join(k.lower().split()): [str(v) for v in vs] for k, vs in _CFG.get("categories", {}).items()}
_WORD = re.compile(r"[a-z0-9+#]+")
_WEBSITE_FALLBACKS: dict = _CFG.get("website_fallbacks", {})
_SITE_MISHEARINGS: dict = _CFG.get("site_mishearings", {})
_BARE_NAMES: dict = {" ".join(str(k).lower().split()): str(v) for k, v in _CFG.get("bare_names", {}).items()}
# Two words of at least this many
# letters count as the same word when this similar (1 - edits / length).
_TOKEN_MIN_LENGTH = int(_CFG["token_min_length"])
_TOKEN_SIMILARITY = float(_CFG["token_similarity"])
_STORE_CACHE_SECONDS = float(_CFG["store_apps_cache_seconds"])
# Opening a Store app goes through the shell's Apps folder by its app ID.
STORE_APP_PREFIX = "shell:AppsFolder\\"


@dataclass(frozen=True)
class ScoredMatch:
    """An installed app (or site) that may be the one he named: display name, match score, kind and what to
    launch."""
    display_name: str
    score: float
    kind: str  # "app" or "website"
    target: str  # a path/exe name to os.startfile (kind=app) or a URL (kind=website)


def word_similarity(a: str, b: str) -> float:
    """1 for the same word, else 1 - edits / longer length; words shorter
    than [app_finder] token_min_length must match exactly."""
    if a == b:
        return 1.0
    if min(len(a), len(b)) < _TOKEN_MIN_LENGTH:
        return 0.0
    return 1.0 - edit_distance(a, b) / max(len(a), len(b))


def _token_score(query: str, candidate_name: str) -> float:
    """Word by word: each request word against its closest app-name word,
    averaged, and scaled by 0.95 so it never beats an exact name. "virtual
    studio" vs "Visual Studio Code" = (0.71 + 1) / 2 * 0.95 = 0.81; vs
    "Android Studio" = (0.14 + 1) / 2 * 0.95 = 0.54."""
    q_words, c_words = _words(query), _words(candidate_name)
    if not q_words or not c_words:
        return 0.0
    best = [max(word_similarity(q, c) for c in c_words) for q in q_words]
    return 0.95 * sum(best) / len(best)


def _score(query: str, candidate_name: str) -> float:
    q = query.lower().strip()
    c = candidate_name.lower().strip()
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    ratio = difflib.SequenceMatcher(None, q, c).ratio()
    # (Whisper's "Have a look, key." -> "open key" ->
    # On-Screen Keyboard opened): a fragment inside a name counts only as a
    # whole word or with 4+ letters ("calc" -> Calculator still does).
    if (q in c or c in q) and (len(min(q, c, key=len)) >= 4 or min(q, c, key=len) in _words(max(q, c, key=len))):
        ratio = max(ratio, 0.85)
    return max(ratio, _token_score(q, c))


def _start_menu_dirs() -> List[Path]:
    dirs = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        dirs.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    programdata = os.environ.get("PROGRAMDATA")
    if programdata:
        dirs.append(Path(programdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return [d for d in dirs if d.exists()]


def read_store_apps() -> List[ScoredMatch]:
    """Windows Store / UWP apps (Camera, Photos, Clock ...), which have no
    Start Menu .lnk: read from the shell's Apps folder (shell:AppsFolder)
    through pywin32's COM. Only entries with a package app ID ("...!App")
    are kept -- desktop apps there already have a Start Menu shortcut.
    Empty, never an error, without pywin32 or on any COM failure (R14)."""
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        return []
    apps: List[ScoredMatch] = []
    pythoncom.CoInitialize()
    try:
        folder = win32com.client.Dispatch("Shell.Application").NameSpace("shell:AppsFolder")
        for item in folder.Items():
            name, app_id = str(item.Name), str(item.Path)
            if "!" in app_id and name:
                apps.append(ScoredMatch(display_name=name, score=0.0, kind="app", target=STORE_APP_PREFIX + app_id))
    except Exception:
        logger.warning("Could not read the Store apps list", exc_info=True)
    finally:
        pythoncom.CoUninitialize()
    return apps


class StoreAppCache:
    """read_store_apps() takes about 2 s the first time, so it runs on its
    own thread and is reused for [app_finder] store_apps_cache_seconds."""

    def __init__(self, reader=read_store_apps, max_age: float = _STORE_CACHE_SECONDS) -> None:
        self._reader = reader
        self._max_age = max_age
        self._apps: Optional[List[ScoredMatch]] = None
        self._read_at = 0.0
        self._lock = threading.Lock()
        self._loading: Optional[threading.Thread] = None

    def prefetch(self) -> None:
        """Start reading the Microsoft Store app list on a background thread (once)."""
        with self._lock:
            if self._loading is None or not self._loading.is_alive():
                self._loading = threading.Thread(target=self._load, name="jarvis-store-apps", daemon=True)
                self._loading.start()

    def _load(self) -> None:
        apps = self._reader()
        with self._lock:
            self._apps, self._read_at = apps, time.monotonic()

    def apps(self, wait_seconds: float = 4.0) -> List[ScoredMatch]:
        """The Store apps, waiting up to `wait_seconds` for the background read; an empty list if it isn't
        ready."""
        with self._lock:
            fresh = self._apps is not None and time.monotonic() - self._read_at < self._max_age
            apps = self._apps
        if fresh:
            return list(apps or [])
        self.prefetch()
        if apps is not None:
            return list(apps)  # stale is fine while the new read runs
        loading = self._loading
        if loading is not None:
            loading.join(wait_seconds)
        with self._lock:
            return list(self._apps or [])


STORE_APPS = StoreAppCache()


def enumerate_installed_apps(store_apps: Optional[StoreAppCache] = None) -> List[ScoredMatch]:
    """Every app candidate this build knows how to find: Start Menu
    shortcuts (score computed against the caller's query separately), the
    fixed system-app aliases, and Windows Store apps (without them
    "open camera" found nothing). Score is filled in by find_app().
    On macOS: every .app bundle in the Applications folders instead."""
    if is_mac():
        return [ScoredMatch(display_name=name, score=0.0, kind="app", target=target)
                for name, target in current().installed_apps()]
    candidates: List[ScoredMatch] = []
    for base in _start_menu_dirs():
        try:
            for lnk in base.rglob("*.lnk"):
                candidates.append(ScoredMatch(display_name=lnk.stem, score=0.0, kind="app", target=str(lnk)))
        except OSError:
            continue  # a locked/unreadable folder shouldn't break the whole search
    for alias, exe in _SYSTEM_APP_ALIASES.items():
        candidates.append(ScoredMatch(display_name=alias, score=0.0, kind="app", target=exe))
    # After the shortcuts and aliases: when a Store app has the same name as
    # one of those ("Calculator"), find_app keeps the first, so nothing that
    # already opened changes.
    candidates.extend((store_apps or STORE_APPS).apps())
    return candidates


def _words(text: str) -> List[str]:
    return _WORD.findall(text.lower())


def shares_a_word(query: str, candidate_name: str) -> bool:
    """The request and the app name must have a word in common -- a whole
    word, or a request word of 3+ letters that starts an app-name word
    ("calc" -> "Calculator"). Letter-level similarity alone is never
    enough: "vs code" scored 0.57 against "Discord" (shared letters, no
    shared word) and opened it."""
    name_words = _words(candidate_name)
    for q in _words(query):
        for w in name_words:
            if w == q or (len(q) >= 3 and w.startswith(q)) or word_similarity(q, w) >= _TOKEN_SIMILARITY:
                return True
    return False


def find_app(name: str, candidates: Optional[List[ScoredMatch]] = None, limit: int = 3) -> List[ScoredMatch]:
    """Installed-app candidates only, best first, capped at `limit`, one per
    app name. Empty if nothing qualifies.

    An exact alias ([app_finder.aliases]) scores 1.0. Anything else must
    share a word with the request (shares_a_word) and score at least
    MATCH_THRESHOLD. Whether the best one is safe to launch is decided by
    confident(), not here.

    `candidates` defaults to a live scan (enumerate_installed_apps()) but
    can be injected -- e.g. by tests, since real Start Menu contents
    differ machine to machine, including the tutor's."""
    pool = enumerate_installed_apps() if candidates is None else candidates
    query = " ".join(name.lower().split())
    kind = " ".join(w for w in query.split() if w not in ("the", "my", "a", "an", "some"))
    for wanted in _CATEGORIES.get(kind, []):
        # "music app": the first installed app of that kind (none: matched as words below).
        hit = next((m for m in pool if m.display_name.lower() == wanted.lower()), None)
        if hit is not None:
            return [ScoredMatch(hit.display_name, 1.0, hit.kind, hit.target)]
    alias_target = _APP_ALIASES.get(query)
    best_by_name: dict = {}
    for m in pool:
        if alias_target is not None and m.display_name.lower() == alias_target.lower():
            score = 1.0
        elif shares_a_word(query, m.display_name):
            score = _score(query, m.display_name)
        else:
            continue
        if score < MATCH_THRESHOLD:
            continue
        key = m.display_name.lower()
        # The same app often has two shortcuts (per-user and all-users).
        if key not in best_by_name or score > best_by_name[key].score:
            best_by_name[key] = ScoredMatch(m.display_name, score, m.kind, m.target)
    scored = sorted(best_by_name.values(), key=lambda m: m.score, reverse=True)
    return scored[:limit]


def confident(matches: List[ScoredMatch]) -> bool:
    """True if the best match is good enough to launch without asking: at
    least LAUNCH_THRESHOLD, and more than AMBIGUITY_MARGIN ahead of the
    runner-up (an exact alias, 1.0, is always confident unless another
    app also scores 1.0)."""
    if not matches:
        return False
    best = matches[0]
    if best.score < LAUNCH_THRESHOLD:
        return False
    if best.score >= 1.0 and (len(matches) == 1 or matches[1].score < 1.0):
        # The exception above was documented but not
        # coded, so "open paint" asked "paint, or Paint 3D?" (0.95 is within
        # the margin) instead of opening Paint.
        return True
    if len(matches) > 1 and best.score - matches[1].score < AMBIGUITY_MARGIN:
        return False
    return True


def _misheard_site(name: str) -> Optional[ScoredMatch]:
    """[app_finder.site_mishearings]: Whisper's spellings of a known site
    ("chat chip it", "Chad GPT" -> chatgpt), compared with spaces removed."""
    wanted = _squash(name)
    for heard, site in _SITE_MISHEARINGS.items():
        if _squash(heard) == wanted and str(site) in _WEBSITE_FALLBACKS:
            return ScoredMatch(display_name=str(site), score=1.0, kind="website", target=_WEBSITE_FALLBACKS[str(site)])
    return None


def bare_name(text: str) -> Optional[str]:
    """What to open when the whole command is just an app or site name
    ("VS Code", "Chrome", "chat GPT"): only exact names from the aliases,
    system apps, known sites, their mishearings and [app_finder.bare_names],
    so a sentence that merely mentions an app never opens anything."""
    said = " ".join(_words(text))
    if not said:
        return None
    if said in _BARE_NAMES:
        return _BARE_NAMES[said]
    if said in _APP_ALIASES or said in {k.lower() for k in _SYSTEM_APP_ALIASES}:
        return said
    squashed = said.replace(" ", "")
    if any(squashed == _squash(k) for k in _WEBSITE_FALLBACKS) or _misheard_site(said) is not None:
        return said
    return None


def canonical_app_name(name: str) -> str:
    """[app_finder.bare_names] also apply after "open": "open
    Microsoft Word" asked to pick from Word, Microsoft To Do and Microsoft
    Store although "microsoft word" = "word" is listed."""
    said = " ".join(_words(name))
    return _BARE_NAMES.get(said, name)


def find_website_fallback(name: str) -> Optional[ScoredMatch]:
    """Only meaningful once find_app() has already come back empty --
    F3: 'checks for an installed app first, and if there is none...'."""
    misheard = _misheard_site(name)
    if misheard is not None:
        return misheard
    best: Optional[ScoredMatch] = None
    for alias, url in _WEBSITE_FALLBACKS.items():
        score = _score(name, alias)
        if score >= MATCH_THRESHOLD and (best is None or score > best.score):
            best = ScoredMatch(display_name=alias, score=score, kind="website", target=url)
    return best


def _squash(name: str) -> str:
    return "".join(_WORD.findall(name.lower()))


def find_known_site(name: str) -> Optional[ScoredMatch]:
    """A site from [app_finder.website_fallbacks] for "open X on Chrome",
    matched with spaces removed ("chat gpt" -> chatgpt) and allowing a
    small mishearing: 1 letter for names under 6 letters, 2 otherwise
    ("chat gpd", "chat gbt" -> chatgpt)."""
    wanted = _squash(name)
    if not wanted:
        return None
    misheard = _misheard_site(name)
    if misheard is not None:
        return misheard
    best: Optional[ScoredMatch] = None
    best_distance = None
    for alias, url in _WEBSITE_FALLBACKS.items():
        key = _squash(alias)
        distance = edit_distance(wanted, key)
        allowed = 1 if len(key) < 6 else 2
        if distance <= allowed and (best_distance is None or distance < best_distance):
            best = ScoredMatch(display_name=alias, score=1.0 - distance / max(len(key), 1), kind="website", target=url)
            best_distance = distance
    return best


_VOWELS = re.compile(r"[aeiouy]")


def closest_site(name: str) -> Tuple[Optional[ScoredMatch], bool]:
    """The known site a name most
    likely means, and whether it is sure enough to open. Sure: at most one
    letter off (two for 8+ letters). Worth asking about: 60% the same, or
    the same consonants ("cloud" / "claude": c-l-d)."""
    wanted = _squash(name)
    if len(wanted) < 3:
        return None, False
    best, best_ratio, best_sure = None, 0.0, False
    for alias, url in _WEBSITE_FALLBACKS.items():
        key = _squash(alias)
        distance = edit_distance(wanted, key)
        ratio = 1.0 - distance / max(len(key), len(wanted))
        sure = distance <= (2 if len(key) >= 8 else 1)
        bones = _VOWELS.sub("", wanted), _VOWELS.sub("", key)
        likely = sure or ratio >= float(_CFG["site_guess_ratio"]) or (len(bones[0]) >= 2 and bones[0] == bones[1])
        if likely and (ratio > best_ratio or sure and not best_sure):
            best = ScoredMatch(display_name=alias, score=round(ratio, 2), kind="website", target=url)
            best_ratio, best_sure = ratio, sure
    return best, best_sure


def find_chrome() -> Optional[str]:
    """Chrome's install path, or None if it isn't installed (R14/2.2 --
    never assume the tutor's laptop has Chrome). On macOS: Google Chrome.app."""
    if is_mac():
        return current().find_chrome()
    candidates = [
        os.environ.get("PROGRAMFILES", r"C:\Program Files") + r"\Google\Chrome\Application\chrome.exe",
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)") + r"\Google\Chrome\Application\chrome.exe",
        os.environ.get("LOCALAPPDATA", "") + r"\Google\Chrome\Application\chrome.exe",
    ]
    for path in candidates:
        if path and Path(path).is_file():
            return path
    return None
