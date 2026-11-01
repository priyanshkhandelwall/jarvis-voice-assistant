"""Raw decision-record detail for troubleshooting, kept out of the UI.

The dashboard's Live Activity panel shows only clean, human-readable
steps (jarvis/dashboard/activity_feed.py). Everything else a record
carries -- the matched action and its parameters, every candidate with its
score, the trust level, the origin tag, the raw result and verification
strings -- goes here instead, one line per completed record.

Fields on a DecisionRecord are already passed through redact_sensitive()
(R11) before they get here, so this file never holds more than the
in-memory record does. It lives in the data folder, which is excluded from
Git and submissions (R18).
"""

from __future__ import annotations

import logging
from pathlib import Path

from jarvis.logbook.records import DecisionRecord
from jarvis.settings import SETTINGS

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

logger = logging.getLogger(__name__)


def debug_log_path() -> Path:
    """Where debug.log lives in jarvis_data/logs."""
    return _PROJECT_ROOT / SETTINGS["logbook"]["debug_log_path"]


def format_debug_line(record: DecisionRecord) -> str:
    """One self-contained line with every field of the record."""
    candidates = "; ".join(f"{c.label} ({c.score:.2f})" for c in record.candidates) or "-"
    level = getattr(record.rule_or_trust_level, "name", record.rule_or_trust_level)
    origin = getattr(record.origin, "name", record.origin)
    # Session-only steps (F14's mood guess) never reach this file.
    private = set(record.session_only)
    progress = [step for step in record.progress if step not in private]
    closing = [step for step in record.closing if step not in private]
    return (
        f"id={record.id} t={record.timestamp:.3f} heard={record.heard!r} "
        f"understood={record.understood!r} candidates=[{candidates}] "
        f"choice={record.choice!r} trust={level} origin={origin} "
        f"progress={progress!r} closing={closing!r} online={record.online_destination!r} "
        f"result={record.result!r} verified={record.verification_result!r}"
        + (f" started_by_jarvis={record.started_by_jarvis!r}" if record.started_by_jarvis else "")
    )


class DebugLog:
    """Appends one line per completed record. R14: a file problem (locked,
    read-only disk) is logged once per failure and never raised -- it must
    not take the controller thread down.

    §11 #8 (resolved): this file is the one exception to
    "records are session only" -- a persistent, redacted troubleshooting
    log, capped at `debug_log_max_bytes`; when it grows past the cap the
    oldest lines are dropped."""

    def __init__(self, path: Path | None = None, max_bytes: int | None = None) -> None:
        self._path = path or debug_log_path()
        self._max_bytes = int(max_bytes if max_bytes is not None else SETTINGS["logbook"]["debug_log_max_bytes"])

    @property
    def path(self) -> Path:
        """The log file's path."""
        return self._path

    def write(self, record: DecisionRecord) -> None:
        """Append one decision record as a single line (secrets already redacted). A write failure is
        logged, never raised."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(format_debug_line(record) + "\n")
            if self._path.stat().st_size > self._max_bytes:
                self._drop_oldest_lines()
        except OSError:
            logger.warning("Could not write the debug log at %s", self._path, exc_info=True)

    def _drop_oldest_lines(self) -> None:
        """Keep the newest whole lines that fit under the cap."""
        lines = self._path.read_bytes().splitlines(keepends=True)
        kept: list[bytes] = []
        size = 0
        for line in reversed(lines):
            if size + len(line) > self._max_bytes:
                break
            kept.append(line)
            size += len(line)
        self._path.write_bytes(b"".join(reversed(kept)))
