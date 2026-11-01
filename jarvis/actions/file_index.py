"""F4: a background-refreshed file name index (DEFAULT per Section 6 --
not a full scan per search, since a slow secondary drive would make
per-search scanning too slow).

Read-only (R9): every folder the walk enters is checked against the
protected list before descending into it, and the whole tree under a
protected folder is skipped, not just filtered out of results afterward.
"""

from __future__ import annotations

import difflib
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, List, Optional, Tuple

from jarvis.security import PathIntent, check_path
from jarvis.settings import SETTINGS

_CFG = SETTINGS["file_finder"]
MATCH_THRESHOLD = float(_CFG["match_threshold"])
REFRESH_INTERVAL_SECONDS = float(_CFG["refresh_interval_seconds"])
MAX_RESULTS_SHOWN = int(_CFG["max_results_shown"])
_EXCLUDED_DIR_NAMES = {d.lower() for d in _CFG["excluded_directory_names"]}
_SKIP_HIDDEN_DIRS = bool(_CFG["skip_hidden_directories"])
# Word's lock file "~$rigold_launch_plan.docx" was
# listed next to the document ("I found 2 files called that").
_TEMP_FILE_PREFIXES = tuple(str(p).lower() for p in _CFG["skip_file_prefixes"])
_SKIP_SUFFIXES = tuple(str(s).lower() for s in _CFG.get("skip_file_suffixes", []))
# JARVIS's own folders ([file_finder] excluded_project_folders,
# __pycache__ by name above) are never walked.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_EXCLUDED_PATHS = {os.path.normcase(str(_PROJECT_ROOT / p)) for p in _CFG.get("excluded_project_folders", [])}
_FILLER_WORDS = {w.lower() for w in _CFG["query_filler_words"]}
_FUZZY_MIN_RATIO = float(_CFG["fuzzy_min_ratio"])
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")
_PROGRESS_INTERVAL = float(_CFG["search_progress_interval_seconds"])
# ("find my report" listed make_report.py and
# final_report.html): his documents first, code only when asked or alone.
_DOCUMENT_SUFFIXES = {str(s).lower() for s in _CFG.get("document_suffixes", [])}
_CODE_SUFFIXES = {str(s).lower() for s in _CFG.get("code_suffixes", [])}
TYPE_WORDS = {str(k).lower(): tuple(str(s).lower() for s in v) for k, v in _CFG.get("type_words", {}).items()}


@dataclass(frozen=True)
class FileMatch:
    """One file or folder that matches a search: its name, full path, score (0-1) and whether it is a
    folder."""
    filename: str
    path: str
    score: float
    is_folder: bool = False


def _is_excluded_dir(name: str) -> bool:
    if _SKIP_HIDDEN_DIRS and name.startswith("."):
        return True  # .vscode, .codex, .cache ...: tool internals, not the developer's own files
    return name.lower() in _EXCLUDED_DIR_NAMES


def _tokens(text: str) -> List[str]:
    return [t for t in _TOKEN_SPLIT.split(text.lower()) if t]


def clean_query(query: str) -> str:
    """Drops spoken filler ("my", "the", "file" in "find my resume file") so
    it cannot dilute the match -- "my resume" scored 0.59 against
    "resource.pyi" purely because of the extra characters. If the query is
    nothing but filler, it is used as typed."""
    tokens = _tokens(query)
    kept = [t for t in tokens if t not in _FILLER_WORDS]
    return " ".join(kept or tokens)


def _score(query: str, filename: str) -> float:
    """1.0 exact name; ~0.9 every query word is a whole word of the name
    (tighter names rank higher); 0.8 query starts a word of the name; otherwise
    fuzzy similarity of the query to the name WITHOUT its extension, which
    only counts at or above fuzzy_min_ratio -- enough for a typo
    ("resum" / "resumee"), not enough for a different word that merely
    shares letters ("resource", "resample")."""
    q = clean_query(query)
    if not q:
        return 0.0
    # Compared in the same normalised form as the query ("resume_2024.docx"
    # -> "resume 2024 docx"), so an exact filename still scores 1.0.
    stem_tokens = _tokens(Path(filename).stem)
    stem = " ".join(stem_tokens)
    if q == stem or q == " ".join(_tokens(filename)):
        return 1.0
    q_tokens = q.split()
    if stem_tokens and all(t in stem_tokens for t in q_tokens):
        return 0.85 + 0.1 * len(q_tokens) / len(stem_tokens)
    if (" " + q) in (" " + stem):
        return 0.8  # starts a word of the name ("resumes", "resume2024"), not buried inside one ("presume")
    ratio = difflib.SequenceMatcher(None, q, stem).ratio()
    return ratio if ratio >= _FUZZY_MIN_RATIO else 0.0


def split_type_words(query: str) -> Tuple[str, Tuple[str, ...]]:
    """("python report", ...) -> ("report", (".py", ...)): a kind he names
    ("the python file") is a filter, not part of the name."""
    words = query.split()
    suffixes = tuple(s for w in words for s in TYPE_WORDS.get(w, ()))
    rest = [w for w in words if w not in TYPE_WORDS]
    return (" ".join(rest) if rest else query), suffixes


def _modified(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def rank(matches: List[FileMatch], suffixes: Tuple[str, ...] = ()) -> List[FileMatch]:
    """His documents, then anything else, then code and build files -- the
    code only if he asked for that kind or no document matched. Within each:
    exact name, whole-word name, partial name; then the newest modified."""
    if suffixes:
        wanted = [m for m in matches if not m.is_folder and Path(m.filename).suffix.lower() in suffixes]
        matches = wanted or matches
    def kind(m: FileMatch) -> int:
        suffix = "" if m.is_folder else Path(m.filename).suffix.lower()
        return 0 if suffix in _DOCUMENT_SUFFIXES else 2 if suffix in _CODE_SUFFIXES else 1
    if not suffixes and any(kind(m) == 0 for m in matches):
        matches = [m for m in matches if kind(m) != 2]
    def tier(m: FileMatch) -> int:
        return 0 if m.score >= 1.0 else 1 if m.score >= 0.85 else 2
    # Newest to the minute; then the tighter name (higher score), then the shallower path.
    return sorted(matches, key=lambda m: (kind(m), tier(m), -int(_modified(m.path) // 60), -m.score, len(m.path)))


def default_roots() -> List[Path]:
    """The user's home directory, plus every other local drive found
    (D: on the developer's laptop; none on a machine without one, R14)."""
    roots = [Path.home()]
    for letter in "DEFGHIJ":
        drive = Path(f"{letter}:\\")
        if drive.exists():
            roots.append(drive)
    return roots


# Generators: yields one (name, path) at a time while os.walk descends; no big intermediate list.
def walk_root(root: Path, folders: bool = False) -> Iterator[Tuple[str, str]]:
    """Yields (filename, full_path) under `root`, skipping excluded
    directory names and anything on the protected list (R9) -- the whole
    subtree is skipped, never just filtered out afterward. With `folders`,
    each folder walked into is yielded too, its path ending in os.sep (FIX
    7, "find Jarvis folder" found 0, only files were indexed)."""
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=lambda _e: None):
        pruned = []
        for d in dirnames:
            if _is_excluded_dir(d) or os.path.normcase(str(Path(dirpath) / d)) in _EXCLUDED_PATHS:
                continue
            allowed, _reason = check_path(str(Path(dirpath) / d), PathIntent.READ)
            if not allowed:
                continue
            pruned.append(d)
        dirnames[:] = pruned
        if folders:
            for d in pruned:
                yield d, str(Path(dirpath) / d) + os.sep
        for fname in filenames:
            if not fname.lower().startswith(_TEMP_FILE_PREFIXES) and not fname.lower().endswith(_SKIP_SUFFIXES):
                yield fname, str(Path(dirpath) / fname)


class FileIndex:
    """F4: an in-memory list of the laptop's file and folder names, rebuilt in the background so a search
    never scans the disk."""
    def __init__(self, roots: Optional[List[Path]] = None) -> None:
        self._roots = roots if roots is not None else default_roots()
        self._entries: List[Tuple[str, str]] = []
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._running = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start building the index on a background thread, and refresh it every refresh_interval_seconds."""
        if self._thread is not None:
            return
        self._running.set()
        self._thread = threading.Thread(target=self._loop, name="jarvis-file-index", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the background refresh (clean shutdown)."""
        self._running.clear()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def is_ready(self) -> bool:
        """True once the first full index has been built."""
        return self._ready.is_set()

    def wait_ready(self, timeout: float) -> bool:
        """Block until the first index build has finished (or `timeout`)."""
        return self._ready.wait(timeout)

    def rebuild_now(self) -> None:
        """Synchronous rebuild -- used by tests and by start() for the
        first build, so callers can wait for a known-good index instead
        of racing the background schedule."""
        entries: List[Tuple[str, str]] = []
        seen_paths = set()  # overlapping roots (e.g. home inside a scanned drive) must not list a file twice
        for root in self._roots:
            for name, path in walk_root(root, folders=True):
                key = os.path.normcase(path)
                if key not in seen_paths:
                    seen_paths.add(key)
                    entries.append((name, path))
        with self._lock:
            self._entries = entries
        self._ready.set()

    def _loop(self) -> None:
        self.rebuild_now()
        while self._running.is_set():
            deadline = time.monotonic() + REFRESH_INTERVAL_SECONDS
            while self._running.is_set() and time.monotonic() < deadline:
                time.sleep(0.5)
            if self._running.is_set():
                self.rebuild_now()

    def search(
        self,
        query: str,
        limit: int = MAX_RESULTS_SHOWN,
        on_progress: Optional[Callable[[int], None]] = None,
        kind: str = "any",
        suffixes: Tuple[str, ...] = (),
    ) -> List[FileMatch]:
        """`on_progress(count)` is called while the scan runs, each time the
        number of matches found so far has grown -- at most once per
        search_progress_interval_seconds, so a burst of hits is one update,
        not dozens. It reports what the scan really has at that moment; the
        caller still gets the final, ranked list as the return value.

        `kind`: "any", "file" or "folder" ("find jarvis folder").
        `suffixes`: the kind of file he named ("python"), see rank()."""
        with self._lock:
            pool = list(self._entries)
        scored: List[FileMatch] = []
        last_reported, last_time = 0, time.monotonic()
        for name, path in pool:
            is_folder = path.endswith(os.sep)
            if (kind == "file" and is_folder) or (kind == "folder" and not is_folder):
                continue
            # A folder's name has no extension: "Jarvis.v2" is not "Jarvis".
            score = _score(query, name + "." if is_folder else name)
            if score >= MATCH_THRESHOLD:
                scored.append(FileMatch(name, path.rstrip(os.sep) if is_folder else path, score, is_folder))
                if on_progress is not None and len(scored) > last_reported and time.monotonic() - last_time >= _PROGRESS_INTERVAL:
                    on_progress(len(scored))
                    last_reported, last_time = len(scored), time.monotonic()
        ranked = rank(scored, suffixes)
        if on_progress is not None and (len(ranked) > last_reported or len(ranked) < len(scored)):
            on_progress(len(ranked))  # the scan is done: report the true total (code files left out)
        return ranked[:limit]
