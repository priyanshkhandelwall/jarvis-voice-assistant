"""COPY LOG (developer-approved): the session's activity log as
plain text, one line per entry with its time, redacted as on screen, and
the copy itself logged as an L0 "copied activity log" entry."""

from __future__ import annotations

from datetime import datetime

from jarvis.controller.permits import OriginTag
from jarvis.controller.trust import TrustLevel
from jarvis.dashboard.activity_feed import format_record, format_session_log
from jarvis.logbook import DecisionRecord, Logbook
from jarvis.logbook.debug_log import DebugLog
from jarvis.settings import SETTINGS

_REDACTED = SETTINGS["logbook"]["redacted_placeholder"]


def _answered(heard: str, answer: str, progress=()) -> DecisionRecord:
    record = DecisionRecord.start(heard=heard)
    record.understood = {"answer": answer}
    record.progress = list(progress)
    record.completed = True
    return record


def _time(record: DecisionRecord) -> str:
    return datetime.fromtimestamp(record.timestamp).strftime("%H:%M:%S")


def test_one_line_per_entry_with_its_time_oldest_first() -> None:
    """One line per entry with its time oldest first."""
    first = _answered("hello", "Good morning. How can I help?")
    second = _answered("what is photosynthesis", "Plants make food from light.", progress=['checking online: Groq, sending "what is photosynthesis"'])
    text = format_session_log([first, second])
    assert text.split("\n") == [
        f"{_time(first)}  heard: hello | saying: Good morning. How can I help?",
        f'{_time(second)}  heard: what is photosynthesis | checking online: Groq, sending "what is photosynthesis" | saying: Plants make food from light.',
    ]


def test_a_multi_line_answer_stays_on_its_entry_line() -> None:
    """A multi line answer stays on its entry line."""
    record = _answered("find resume", "Found 2 matches:\n1. resume.pdf\n2. resume_old.pdf")
    text = format_session_log([record])
    assert "\n" not in text
    assert text.endswith("saying: Found 2 matches: | 1. resume.pdf | 2. resume_old.pdf")


def test_redacted_entries_stay_redacted_in_the_copy() -> None:
    """Redacted entries stay redacted in the copy."""
    record = _answered("my password is hunter2", "I won't repeat that.")
    text = format_session_log([record])
    assert "hunter2" not in text
    assert f"heard: {_REDACTED}" in text


def test_empty_session_copies_a_plain_note() -> None:
    """Empty session copies a plain note."""
    assert format_session_log([]) == "(no activity yet)"


def test_copy_is_logged_as_an_l0_developer_action(tmp_path) -> None:
    """Copy is logged as an L0 developer action."""
    log_file = tmp_path / "debug.log"
    logbook = Logbook(debug_log=DebugLog(path=log_file))
    logbook.record_ui_action("copied activity log")
    (record,) = logbook.records()
    assert record.completed
    assert record.rule_or_trust_level == TrustLevel.L0
    assert record.origin == OriginTag.DEVELOPER_REQUESTED
    assert format_record(record).lines == ["copied activity log"]
    assert "copied activity log" in log_file.read_text(encoding="utf-8")


def test_the_copy_log_entry_appears_in_the_next_copy() -> None:
    """The copy log entry appears in the next copy."""
    logbook = Logbook()
    logbook.record_ui_action("copied activity log")
    assert format_session_log(logbook.records()).endswith("  copied activity log")
