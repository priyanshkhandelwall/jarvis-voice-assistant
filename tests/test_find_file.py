"""Tests for F4's matcher, disambiguation and executor. File launching is
mocked (Section 8); the file index uses small temp directory trees."""

from __future__ import annotations

from pathlib import Path

import pytest

import jarvis.actions.find_file as find_file
import jarvis.actions.launch_verify as launch_verify
from jarvis.actions.file_index import FileIndex
from jarvis.interpreter import ActionRequest, Answer


@pytest.fixture
def sample_tree(tmp_path: Path) -> Path:
    """Fixture: a small folder tree of sample documents to index."""
    (tmp_path / "Documents").mkdir()
    (tmp_path / "Documents" / "resume_2024.docx").write_text("x")
    (tmp_path / "Documents" / "resume_old.pdf").write_text("x")
    (tmp_path / "Documents" / "cover_letter.docx").write_text("x")
    return tmp_path


@pytest.fixture
def ready_finder(sample_tree: Path) -> find_file.FileFinder:
    """Fixture: a FileFinder over the sample tree's built index."""
    index = FileIndex(roots=[sample_tree])
    index.rebuild_now()
    return find_file.FileFinder(index)


# -- find_matcher -------------------------------------------------------


def test_find_matcher_ignores_non_find_commands(ready_finder) -> None:
    """Find matcher ignores non find commands."""
    assert ready_finder.find_matcher("open chrome") is None


def test_find_matcher_ignores_bare_find_with_no_query(ready_finder) -> None:
    """Find matcher ignores bare find with no query."""
    assert ready_finder.find_matcher("find ") is None


def test_find_matcher_index_not_ready_says_so(sample_tree: Path) -> None:
    """Find matcher index not ready says so."""
    index = FileIndex(roots=[sample_tree])  # never rebuilt
    finder = find_file.FileFinder(index)
    result = finder.find_matcher("find resume")
    assert isinstance(result, Answer)
    assert "still building" in result.text.lower()


def test_find_matcher_no_matches_says_so_plainly(ready_finder) -> None:
    """Find matcher no matches says so plainly."""
    result = ready_finder.find_matcher("find zzz nonexistent xyz")
    assert isinstance(result, Answer)
    assert "can't find" in result.text.lower()


def test_find_matcher_single_match_opens_directly(ready_finder) -> None:
    """Find matcher single match opens directly."""
    result = ready_finder.find_matcher("find cover_letter")
    assert isinstance(result, ActionRequest)
    assert result.action_name == "open_file"
    assert result.params["display_name"] == "cover_letter.docx"
    assert len(result.candidates) == 1


def test_find_matcher_multiple_matches_lists_them(ready_finder) -> None:
    """Find matcher multiple matches lists them."""
    result = ready_finder.find_matcher("find resume")
    assert isinstance(result, Answer)
    # She says the count; the names are on screen (Live Activity steps).
    assert result.text == "I found 2 files. Which one would you like to open?"
    assert sorted(c.label for c in result.candidates) == ["resume_2024.docx", "resume_old.pdf"]


# -- open_selection_matcher (disambiguation) -----------------------------


def test_open_selection_returns_none_with_nothing_pending(ready_finder) -> None:
    """Open selection returns none with nothing pending."""
    assert ready_finder.open_selection_matcher("open 1") is None
    assert ready_finder.open_selection_matcher("open resume_2024.docx") is None


def test_open_selection_by_number(ready_finder) -> None:
    """Open selection by number."""
    listing = ready_finder.find_matcher("find resume")
    assert isinstance(listing, Answer)

    result = ready_finder.open_selection_matcher("open 1")
    assert isinstance(result, ActionRequest)
    assert result.action_name == "open_file"
    assert result.params["display_name"] in ("resume_2024.docx", "resume_old.pdf")


def test_open_selection_by_exact_name(ready_finder) -> None:
    """Open selection by exact name."""
    ready_finder.find_matcher("find resume")
    result = ready_finder.open_selection_matcher("open resume_2024.docx")
    assert isinstance(result, ActionRequest)
    assert result.params["display_name"] == "resume_2024.docx"


def test_open_selection_invalid_number_answers_from_the_list(ready_finder) -> None:
    """Open selection invalid number answers from the list."""
    # A number never falls through to an app search.
    ready_finder.find_matcher("find resume")
    result = ready_finder.open_selection_matcher("open 99")
    assert isinstance(result, Answer) and "Say 'open' and a number from 1 to" in result.text


def test_open_selection_unrelated_name_falls_through_to_f3(ready_finder) -> None:
    """So a genuinely unrelated 'open chrome' still reaches F3's matcher
    even while a file list is pending."""
    ready_finder.find_matcher("find resume")
    assert ready_finder.open_selection_matcher("open chrome") is None


def test_open_selection_clears_pending_after_a_valid_pick(ready_finder) -> None:
    """Open selection clears pending after a valid pick."""
    ready_finder.find_matcher("find resume")
    ready_finder.open_selection_matcher("open 1")
    assert ready_finder.open_selection_matcher("open 1") is None  # nothing pending anymore


def test_new_search_replaces_the_previous_pending_list(ready_finder) -> None:
    """New search replaces the previous pending list."""
    ready_finder.find_matcher("find resume")
    ready_finder.find_matcher("find cover_letter")  # single match -- clears pending
    assert ready_finder.open_selection_matcher("open 1") is None


def test_on_transition_clears_pending_on_stop(ready_finder) -> None:
    """A stale file selection surviving an unrelated Stop is the same
    shape of bug already found and fixed three times this build."""
    from jarvis.controller import Event, EventType, State

    ready_finder.find_matcher("find resume")
    assert ready_finder.open_selection_matcher("open 1") is not None  # sanity: it was pending

    ready_finder.find_matcher("find resume")  # pending again
    from jarvis.controller.controller import TransitionLogEntry

    stop_entry = TransitionLogEntry(
        timestamp=0.0,
        from_state=State.CONFIRMING,
        to_state=State.AWAKE,
        event_type=None,  # Stop-forced transition
        accepted=True,
        reason="Stop (R8): 0 pending event(s) discarded",
    )
    ready_finder.on_transition(stop_entry)
    assert ready_finder.open_selection_matcher("open 1") is None  # cleared, nothing pending


def test_register_wires_stop_awareness_via_add_transition_listener() -> None:
    """register() must use add_transition_listener, not
    set_transition_listener -- coexisting with whatever app.py's own
    bridge+logbook combo already set, never overwriting it."""
    import jarvis.actions.open_app as open_app
    from jarvis.controller import Controller, Event, EventType, State
    from jarvis.interpreter import Interpreter
    from jarvis.orchestrator import Orchestrator
    from jarvis.speech import NullSpeechOutput

    c = Controller(start_state=State.AWAKE)
    existing_listener_calls = []
    c.set_transition_listener(lambda entry: existing_listener_calls.append(entry))

    interpreter = Interpreter()
    orchestrator = Orchestrator(c, interpreter, NullSpeechOutput())
    finder = find_file.FileFinder(FileIndex(roots=[]))
    finder.register(interpreter, orchestrator)
    open_app.register(interpreter, orchestrator)
    c.start()
    try:
        finder._last_matches = [object()]  # simulate a pending selection

        c.submit(Event(EventType.SPEECH_STARTED))
        deadline_state = State.LISTENING
        import time as _time

        t0 = _time.monotonic()
        while _time.monotonic() - t0 < 2.0 and c.state != deadline_state:
            _time.sleep(0.01)

        c.request_stop(source="test")
        t0 = _time.monotonic()
        while _time.monotonic() - t0 < 2.0 and c.state != State.AWAKE:
            _time.sleep(0.01)

        # The pre-existing listener (app.py's stand-in) still fired too --
        # add_transition_listener did not replace it.
        assert len(existing_listener_calls) >= 1
        assert finder._last_matches is None
    finally:
        c.shutdown()


# -- open_file_executor ---------------------------------------------------


def test_open_file_executor_success(monkeypatch) -> None:
    """Open file executor success."""
    calls = []
    monkeypatch.setattr(find_file.os, "startfile", lambda path: calls.append(path))
    monkeypatch.setattr(launch_verify, "snapshot", lambda: (None, 10))
    monkeypatch.setattr(launch_verify, "wait_for_new_activity", lambda bt, bc, timeout, poll: True)

    verified, text = find_file.open_file_executor(
        "open_file", {"path": r"C:\Users\x\Documents\resume.docx", "display_name": "resume.docx"}
    )
    assert verified is True
    assert text == "Opened resume.docx."
    assert calls == [r"C:\Users\x\Documents\resume.docx"]


def test_open_file_executor_fails_after_retry(monkeypatch) -> None:
    """Open file executor fails after retry."""
    monkeypatch.setattr(find_file.os, "startfile", lambda path: None)
    monkeypatch.setattr(launch_verify, "snapshot", lambda: (None, 10))
    monkeypatch.setattr(launch_verify, "wait_for_new_activity", lambda bt, bc, timeout, poll: False)

    verified, text = find_file.open_file_executor(
        "open_file", {"path": r"C:\Users\x\Documents\resume.docx", "display_name": "resume.docx"}
    )
    assert verified is False
    assert "couldn't confirm" in text.lower()


def test_open_file_executor_refuses_a_protected_path() -> None:
    """Open file executor refuses a protected path."""
    from jarvis.security import PROTECTED_FOLDERS

    protected_path = str(PROTECTED_FOLDERS[0] / "photo.jpg")
    verified, text = find_file.open_file_executor(
        "open_file", {"path": protected_path, "display_name": "photo.jpg"}
    )
    assert verified is False
    assert text == "I can't open photo.jpg: it's in a protected folder."
    assert str(PROTECTED_FOLDERS[0]) not in text  # Never a path in what she says


# -- full pipeline integration: F3 + F4 registered together --------------


def test_single_match_end_to_end(monkeypatch, sample_tree: Path) -> None:
    """Real controller/logbook/orchestrator, F3 and F4 both registered
    together (matcher-ordering matters) -- only OS launching is mocked."""
    import jarvis.actions.open_app as open_app
    from jarvis.controller import Controller, OriginTag, State, TrustLevel
    from jarvis.interpreter import Interpreter
    from jarvis.logbook import Logbook
    from jarvis.orchestrator import Orchestrator
    from jarvis.speech import SpeechOutput

    class RecordingSpeech(SpeechOutput):
        def __init__(self):
            self.spoken = []

        def speak(self, text):
            self.spoken.append(text)

    monkeypatch.setattr(find_file.os, "startfile", lambda path: None)
    monkeypatch.setattr(launch_verify, "snapshot", lambda: (None, 10))
    monkeypatch.setattr(launch_verify, "wait_for_new_activity", lambda bt, bc, timeout, poll: True)

    index = FileIndex(roots=[sample_tree])
    index.rebuild_now()
    finder = find_file.FileFinder(index)

    c = Controller(start_state=State.AWAKE)
    logbook = Logbook()
    c.set_transition_listener(logbook.on_transition)
    interpreter = Interpreter()
    speech = RecordingSpeech()
    orchestrator = Orchestrator(c, interpreter, speech, logbook=logbook)
    finder.register(interpreter, orchestrator)  # F4 registered first
    open_app.register(interpreter, orchestrator)  # F3 registered second
    c.start()
    try:
        orchestrator.handle_typed_command("find cover_letter")
        assert c.state == State.AWAKE
        record = logbook.last_completed()
        assert record.choice == "open_file"
        assert record.rule_or_trust_level == TrustLevel.L1
        assert record.origin == OriginTag.DEVELOPER_REQUESTED
        assert record.result == "Opened cover_letter.docx."
        assert speech.spoken == ["Opened cover_letter.docx."]
    finally:
        c.shutdown()


def test_multiple_matches_then_selection_end_to_end(monkeypatch, sample_tree: Path) -> None:
    """Multiple matches then selection end to end."""
    import jarvis.actions.open_app as open_app
    from jarvis.controller import Controller, State
    from jarvis.interpreter import Interpreter
    from jarvis.logbook import Logbook
    from jarvis.orchestrator import Orchestrator
    from jarvis.speech import SpeechOutput

    class RecordingSpeech(SpeechOutput):
        def __init__(self):
            self.spoken = []

        def speak(self, text):
            self.spoken.append(text)

    monkeypatch.setattr(find_file.os, "startfile", lambda path: None)
    monkeypatch.setattr(launch_verify, "snapshot", lambda: (None, 10))
    monkeypatch.setattr(launch_verify, "wait_for_new_activity", lambda bt, bc, timeout, poll: True)

    index = FileIndex(roots=[sample_tree])
    index.rebuild_now()
    finder = find_file.FileFinder(index)

    c = Controller(start_state=State.AWAKE)
    logbook = Logbook()
    c.set_transition_listener(logbook.on_transition)
    interpreter = Interpreter()
    speech = RecordingSpeech()
    orchestrator = Orchestrator(c, interpreter, speech, logbook=logbook)
    finder.register(interpreter, orchestrator)
    open_app.register(interpreter, orchestrator)
    c.start()
    try:
        orchestrator.handle_typed_command("find resume")
        assert c.state == State.AWAKE
        assert len(speech.spoken) == 1
        assert speech.spoken[0] == "I found 2 files. Which one would you like to open?"
        listing_record = logbook.last_completed()
        shown = " ".join(listing_record.progress)  # the names are shown, not said
        assert "resume_2024.docx" in shown and "resume_old.pdf" in shown
        assert len(listing_record.candidates) == 2

        orchestrator.handle_typed_command("open resume_2024.docx")
        assert c.state == State.AWAKE
        assert speech.spoken[-1] == "Opened resume_2024.docx."
        open_record = logbook.last_completed()
        assert open_record.choice == "open_file"
    finally:
        c.shutdown()


def test_unrelated_open_command_still_reaches_f3_with_file_finder_registered(monkeypatch, sample_tree: Path) -> None:
    """Proves the matcher order doesn't break ordinary F3 behavior even
    when F4 is registered first and nothing is pending."""
    import jarvis.actions.open_app as open_app
    from jarvis.controller import Controller, State
    from jarvis.interpreter import Interpreter
    from jarvis.logbook import Logbook
    from jarvis.orchestrator import Orchestrator
    from jarvis.speech import NullSpeechOutput

    monkeypatch.setattr(open_app, "find_app", lambda name: [])
    monkeypatch.setattr(open_app, "find_website_fallback", lambda name: None)
    monkeypatch.setattr(open_app, "find_chrome", lambda: None)  # With Chrome she searches instead

    index = FileIndex(roots=[sample_tree])
    index.rebuild_now()
    finder = find_file.FileFinder(index)

    c = Controller(start_state=State.AWAKE)
    logbook = Logbook()
    c.set_transition_listener(logbook.on_transition)
    interpreter = Interpreter()
    orchestrator = Orchestrator(c, interpreter, NullSpeechOutput(), logbook=logbook)
    finder.register(interpreter, orchestrator)
    open_app.register(interpreter, orchestrator)
    c.start()
    try:
        orchestrator.handle_typed_command("open some_app_name")
        assert c.state == State.AWAKE
        record = logbook.last_completed()
        assert "couldn't find an app called" in record.understood["answer"]
    finally:
        c.shutdown()
