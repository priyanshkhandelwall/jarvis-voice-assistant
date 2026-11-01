"""Tests for F6's activity-feed formatter (jarvis/dashboard/activity_feed.py):
the Live Activity panel shows clean, human-readable steps only -- raw match
data belongs in the debug log (see test_debug_log.py)."""

from __future__ import annotations

from datetime import datetime

from jarvis.dashboard.activity_feed import format_record
from jarvis.logbook.records import Candidate, DecisionRecord


def _open_chrome_record() -> DecisionRecord:
    record = DecisionRecord.start(heard="open chrome")
    record.understood = {
        "action_name": "open_app",
        "params": {"target": r"C:\ProgramData\Start Menu\Google Chrome.lnk", "display_name": "Google Chrome"},
    }
    record.choice = "open_app"
    record.candidates = [Candidate("Google Chrome", 0.91)]
    record.result = "Opened Google Chrome."
    record.verification_result = "True"
    record.completed = True
    return record


def test_open_app_is_heard_searching_opening() -> None:
    """Open app is heard searching opening."""
    row = format_record(_open_chrome_record())
    assert row.lines == ["heard: open chrome", "searching for chrome", "opening Google Chrome"]
    assert row.verified is True


def test_no_raw_match_data_reaches_the_panel() -> None:
    """No raw match data reaches the panel."""
    text = " ".join(format_record(_open_chrome_record()).lines)
    for leaked in ("matched", "open_app(", "target=", ".lnk", "display_name", "considered", "0.91", "verified:"):
        assert leaked not in text


def test_failed_action_appends_the_reason() -> None:
    """Failed action appends the reason."""
    record = _open_chrome_record()
    record.verification_result = "False"
    record.result = "I couldn't confirm Google Chrome opened."
    row = format_record(record)
    assert row.lines[-1] == "I couldn't confirm Google Chrome opened."
    assert row.verified is False


def test_denied_action_shows_the_denial() -> None:
    """Denied action shows the denial."""
    record = DecisionRecord.start(heard="open chrome")
    record.understood = {"action_name": "open_app", "params": {"display_name": "Google Chrome"}}
    record.result = "denied: not allowed"
    record.completed = True
    assert format_record(record).lines[-1] == "denied: not allowed"


def test_open_file_steps() -> None:
    """Open file steps."""
    record = DecisionRecord.start(heard="find resume")
    record.understood = {"action_name": "open_file", "params": {"path": r"D:\x\resume.docx", "display_name": "resume.docx"}}
    record.verification_result = "True"
    assert format_record(record).lines == ["heard: find resume", "searching for resume", "opening resume.docx"]


def test_answer_shows_what_was_said() -> None:
    """Answer shows what was said."""
    record = DecisionRecord.start(heard="introduce yourself")
    record.understood = {"answer": "Hello everyone."}
    assert format_record(record).lines == ["heard: introduce yourself", "saying: Hello everyone."]


def test_a_list_answer_keeps_each_line_as_its_own_row() -> None:
    """A list answer keeps each line as its own row."""
    record = DecisionRecord.start(heard="find resume")
    record.understood = {"answer": "I found 2 matches:\n1. a.pdf (D:\\x)\n2. b.pdf (D:\\y)"}
    assert format_record(record).lines == [
        "heard: find resume",
        "saying: I found 2 matches:",
        "1. a.pdf (D:\\x)",
        "2. b.pdf (D:\\y)",
    ]


def test_not_understood_shows_the_plain_result() -> None:
    """Not understood shows the plain result."""
    record = DecisionRecord.start(heard="asdkjfh")
    record.result = "could not understand"
    record.completed = True
    assert format_record(record).lines == ["heard: asdkjfh", "could not understand"]


def test_nothing_heard_falls_back_to_placeholder() -> None:
    """Nothing heard falls back to placeholder."""
    row = format_record(DecisionRecord.start(heard=None))
    assert row.lines == ["(nothing understood)"]
    assert row.verified is False


def test_time_label_matches_the_records_wall_clock_timestamp() -> None:
    """Time label matches the records wall clock timestamp."""
    record = DecisionRecord.start(heard="hi")
    expected = datetime.fromtimestamp(record.timestamp).strftime("%H:%M:%S")
    assert format_record(record).time_label == expected


# -- NewRecordTracker: the feed must keep showing new records after the
# logbook's [logbook] max_records deque starts dropping the oldest. ------


def _push_ui_records(logbook, start: int, count: int) -> None:
    for i in range(start, start + count):
        logbook.record_ui_action(f"record {i}")


def test_feed_keeps_showing_new_records_after_the_logbook_rolls() -> None:
    """Push 600 records into a 500-record logbook, refreshing the feed the
    way app.py does (newest inserted at index 0): the newest is shown."""
    from jarvis.dashboard.activity_feed import NewRecordTracker
    from jarvis.logbook import Logbook
    from jarvis.settings import SETTINGS

    max_records = int(SETTINGS["logbook"]["max_records"])
    assert max_records == 500  # the cap this test is about
    logbook = Logbook()
    tracker = NewRecordTracker()
    feed: list[str] = []  # newest first, like the reversed ListView

    for batch_start in range(0, 600, 50):
        _push_ui_records(logbook, batch_start, 50)
        for record in tracker.take_new(logbook.records()):
            feed.insert(0, record.result)

    assert len(logbook.records()) == 500
    assert feed[0] == "record 599"
    assert feed[:3] == ["record 599", "record 598", "record 597"]
    assert len(feed) == 600  # every record appeared exactly once, none skipped


def test_tracker_returns_each_record_once_in_order() -> None:
    """Tracker returns each record once in order."""
    from jarvis.dashboard.activity_feed import NewRecordTracker
    from jarvis.logbook import Logbook

    logbook = Logbook()
    tracker = NewRecordTracker()
    _push_ui_records(logbook, 0, 3)
    assert [r.result for r in tracker.take_new(logbook.records())] == ["record 0", "record 1", "record 2"]
    assert tracker.take_new(logbook.records()) == []
    _push_ui_records(logbook, 3, 1)
    assert [r.result for r in tracker.take_new(logbook.records())] == ["record 3"]


def test_tracker_skips_records_not_yet_completed() -> None:
    """Tracker skips records not yet completed."""
    from jarvis.dashboard.activity_feed import NewRecordTracker

    record = DecisionRecord.start(heard="open chrome")
    tracker = NewRecordTracker()
    assert tracker.take_new([record]) == []
    record.completed = True
    assert tracker.take_new([record]) == [record]


def test_tracker_memory_stays_bounded_by_the_logbook() -> None:
    """Tracker memory stays bounded by the logbook."""
    from jarvis.dashboard.activity_feed import NewRecordTracker
    from jarvis.logbook import Logbook

    logbook = Logbook()
    tracker = NewRecordTracker()
    for start in range(0, 1200, 100):
        _push_ui_records(logbook, start, 100)
        tracker.take_new(logbook.records())
    assert len(tracker._seen) == len(logbook.records()) == 500


def test_groq_setup_message_shows_in_full_in_the_feed() -> None:
    """Fix 3: the no-key setup message names GROQ_API_KEY, which the R11
    redactor would blank; it must reach the Live Activity row in full."""
    from jarvis.logbook.records import redact_value
    from jarvis.settings import SETTINGS

    message = SETTINGS["setup_messages"]["groq_no_key"]
    record = DecisionRecord.start(heard="tell me a joke")
    record.understood = redact_value({"answer": message})
    record.completed = True
    assert format_record(record).lines == ["heard: tell me a joke", f"saying: {message}"]
