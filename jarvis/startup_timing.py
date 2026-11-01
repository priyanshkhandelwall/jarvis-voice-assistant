"""Startup timings (R15: the project should never lag). Each step of
starting JARVIS is marked with the wall-clock time it finished; once the
window is ready the marks are written to jarvis_data/logs/startup_timings.log
(one line per step, numbers and fixed step names only -- nothing personal).
A developer timing script can read them back.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

from jarvis.settings import SETTINGS

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOCK = threading.Lock()
_MARKS: List[Tuple[float, str]] = []
_WRITTEN = [0]  # how many marks are already in the file


def log_path() -> Path:
    """Where startup_timings.log lives in jarvis_data/logs."""
    return _PROJECT_ROOT / str(SETTINGS["logbook"]["startup_timings_path"])


def mark(step: str, at: Optional[float] = None) -> None:
    """`step` just finished (now, or at the wall-clock time `at`)."""
    with _LOCK:
        _MARKS.append((time.time() if at is None else at, step))


def marks() -> List[Tuple[float, str]]:
    """Every startup mark so far: (seconds, label)."""
    with _LOCK:
        return list(_MARKS)


def write(path: Optional[Path] = None) -> None:
    """Append the marks not written yet: `<epoch seconds> <step>`, then a
    blank line. Called when the dashboard is built and again when the
    background start finishes. Never raises (R14)."""
    target = path or log_path()
    with _LOCK:
        new, _WRITTEN[0] = _MARKS[_WRITTEN[0]:], len(_MARKS)
    lines = [f"{t:.3f} {step}" for t, step in new]
    if not lines:
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n\n")
    except OSError:
        logger.warning("Could not write startup timings to %s", target, exc_info=True)
