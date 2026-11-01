"""The raw match detail removed from the Live Activity panel must still be
recoverable -- from the debug log, one line per completed record."""

from __future__ import annotations

import time
from pathlib import Path

from jarvis.controller import Controller, Event, EventType, State
from jarvis.logbook import Logbook
from jarvis.logbook.debug_log import DebugLog, format_debug_line
from jarvis.logbook.records import Candidate, DecisionRecord


def _wait(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not predicate():
        time.sleep(0.005)
    assert predicate()


def test_debug_line_carries_the_raw_detail() -> None:
    """Debug line carries the raw detail."""
    record = DecisionRecord.start(heard="open chrome")
    record.understood = {"action_name": "open_app", "params": {"target": r"C:\x\Chrome.lnk", "display_name": "Google Chrome"}}
    record.candidates = [Candidate("Google Chrome", 0.91)]
    record.result = "Opened Google Chrome."
    record.verification_result = "True"
    line = format_debug_line(record)
    for expected in ("open_app", "Chrome.lnk", "Google Chrome (0.91)", "Opened Google Chrome.", "verified='True'"):
        assert expected in line


def test_logbook_writes_a_line_when_a_record_completes(tmp_path: Path) -> None:
    """Logbook writes a line when a record completes."""
    log_path = tmp_path / "logs" / "debug.log"
    logbook = Logbook(debug_log=DebugLog(log_path))
    c = Controller(start_state=State.AWAKE)
    c.set_transition_listener(logbook.on_transition)
    c.start()
    try:
        c.submit(Event(EventType.SPEECH_STARTED))
        _wait(lambda: c.state == State.LISTENING)
        c.submit(Event(EventType.COMMAND_CAPTURED, {"text": "hello"}))
        _wait(lambda: c.state == State.UNDERSTANDING)
        c.submit(Event(EventType.ANSWER_READY, {"text": "hi there"}))
        _wait(lambda: c.state == State.SPEAKING)
        c.submit(Event(EventType.SPEECH_FINISHED))
        _wait(lambda: c.state == State.AWAKE)
    finally:
        c.shutdown()
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and "hello" in lines[0] and "hi there" in lines[0]


def test_unwritable_path_never_raises(tmp_path: Path) -> None:
    """Unwritable path never raises."""
    blocker = tmp_path / "file"
    blocker.write_text("x")
    # The parent is a regular file, so mkdir/open raise OSError -- swallowed (R14).
    DebugLog(blocker / "sub" / "debug.log").write(DecisionRecord.start(heard="x"))


def test_debug_log_drops_oldest_lines_past_the_cap(tmp_path: Path) -> None:
    """§11 #8: the persistent debug log is capped; when it grows
    past the cap the oldest lines go and the newest whole lines stay."""
    log_path = tmp_path / "debug.log"
    debug_log = DebugLog(log_path, max_bytes=1200)
    for i in range(40):
        debug_log.write(DecisionRecord.start(heard=f"command number {i:02d}"))
    data = log_path.read_bytes()
    assert len(data) <= 1200
    lines = data.decode("utf-8").splitlines()
    assert "command number 39" in lines[-1]  # newest kept
    assert all("command number 00" not in line for line in lines)  # oldest dropped
    assert all(line.startswith("id=") for line in lines)  # only whole lines survive


def test_debug_log_default_cap_is_one_megabyte() -> None:
    """Debug log default cap is one megabyte."""
    from jarvis.settings import SETTINGS

    assert SETTINGS["logbook"]["debug_log_max_bytes"] == 1048576
