"""Live progress: steps a module really reports while a command runs reach
the record (and so the dashboard) in order, instead of one dump at the end."""

from __future__ import annotations

from pathlib import Path

from jarvis.actions.file_index import FileIndex
from jarvis.actions.find_file import FileFinder
from jarvis.controller import Controller, Event, EventType, State
from jarvis.dashboard.activity_feed import format_record
from jarvis.logbook import Logbook
from jarvis.logbook.records import DecisionRecord


def _capture(logbook: Logbook, text: str) -> Controller:
    c = Controller(start_state=State.AWAKE)
    c.set_transition_listener(logbook.on_transition)
    c.start()
    c.submit(Event(EventType.SPEECH_STARTED))
    c.submit(Event(EventType.COMMAND_CAPTURED, {"text": text}))
    import time

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and c.state != State.UNDERSTANDING:
        time.sleep(0.005)
    return c


def test_steps_are_recorded_in_order_on_the_in_progress_record() -> None:
    """Steps are recorded in order on the in progress record."""
    logbook = Logbook()
    c = _capture(logbook, "find resume")
    try:
        logbook.add_step("searching files...")
        logbook.add_step("found 1 match so far", replaces="found ")
        logbook.add_step("found 2 matches so far", replaces="found ")
        logbook.add_step("found 2 matches", replaces="found ")
        snap = logbook.live_snapshot()
        assert snap is not None and snap.heard == "find resume"
        assert snap.progress == ["searching files...", "found 2 matches"]
        assert format_record(snap).lines == ["heard: find resume", "searching files...", "found 2 matches"]
    finally:
        c.shutdown()


def test_add_step_with_nothing_in_progress_is_ignored() -> None:
    """Add step with nothing in progress is ignored."""
    logbook = Logbook()
    logbook.add_step("searching files...")
    assert logbook.live_snapshot() is None


def test_live_snapshot_is_a_copy() -> None:
    """Live snapshot is a copy."""
    logbook = Logbook()
    c = _capture(logbook, "find x")
    try:
        snap = logbook.live_snapshot()
        logbook.add_step("searching files...")
        assert snap is not None and snap.progress == []  # the copy was taken before the step
    finally:
        c.shutdown()


def test_sensitive_words_in_a_step_are_redacted() -> None:
    """Sensitive words in a step are redacted."""
    logbook = Logbook()
    c = _capture(logbook, "find notes")
    try:
        logbook.add_step("found my password file")
        assert logbook.live_snapshot().progress == ["[redacted]"]
    finally:
        c.shutdown()


def test_in_progress_action_shows_steps_without_a_final_result_yet() -> None:
    """In progress action shows steps without a final result yet."""
    record = DecisionRecord.start(heard="open chrome")
    record.progress = ["searching for chrome"]
    record.understood = {"action_name": "open_app", "params": {"display_name": "Google Chrome"}}
    record.result = "Opened Google Chrome."  # set by ACTION_DONE, before verification
    assert format_record(record).lines == ["heard: open chrome", "searching for chrome", "opening Google Chrome"]


def test_reported_search_step_is_not_repeated_by_the_derived_one() -> None:
    """Reported search step is not repeated by the derived one."""
    record = DecisionRecord.start(heard="find resume")
    record.progress = ["searching files...", "found 1 match"]
    record.understood = {"action_name": "open_file", "params": {"display_name": "resume.md"}}
    record.verification_result = "True"
    record.completed = True
    assert format_record(record).lines == ["heard: find resume", "searching files...", "found 1 match", "opening resume.md"]


class _Orchestrator:
    """Test helper: Orchestrator."""
    def __init__(self) -> None:
        self.steps: list[tuple[str, str | None]] = []

    def report_step(self, text: str, replaces: str | None = None) -> None:
        """Fake report step for the tests."""
        self.steps.append((text, replaces))


def test_file_finder_reports_searching_then_found(tmp_path: Path) -> None:
    """File finder reports searching then found."""
    for name in ("resume_a.docx", "resume_b.docx", "resume_c.pdf"):
        (tmp_path / name).write_text("x")
    index = FileIndex(roots=[tmp_path])
    index.rebuild_now()
    finder = FileFinder(index)
    orch = _Orchestrator()
    finder._report_step = orch.report_step
    finder.find_matcher("find resume")
    texts = [t for t, _ in orch.steps]
    assert texts[0] == "searching files..."
    # Then the numbered names, shown on screen (only the count is said).
    assert texts[-4] == "found 3 matches"
    assert sorted(t.split(". ", 1)[1] for t in texts[-3:]) == ["resume_a.docx", "resume_b.docx", "resume_c.pdf"]


def test_index_search_reports_a_growing_count(tmp_path: Path, monkeypatch) -> None:
    """Index search reports a growing count."""
    import jarvis.actions.file_index as fi

    monkeypatch.setattr(fi, "_PROGRESS_INTERVAL", 0.0)  # report every new match
    for i in range(4):
        (tmp_path / f"resume_{i}.txt").write_text("x")
    index = FileIndex(roots=[tmp_path])
    index.rebuild_now()
    seen: list[int] = []
    index.search("resume", on_progress=seen.append)
    assert seen and seen == sorted(seen) and seen[-1] <= 4
