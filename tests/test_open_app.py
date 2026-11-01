"""Tests for F3's matcher and executors. OS app launching is mocked
(Section 8) -- these tests never actually open a real application."""

from __future__ import annotations


import pytest

import jarvis.actions.launch_verify as launch_verify
import jarvis.actions.open_app as open_app
from jarvis.actions.app_finder import ScoredMatch
from jarvis.interpreter import ActionRequest, Answer


# -- matcher ----------------------------------------------------------------


def test_matcher_ignores_non_open_commands() -> None:
    """Matcher ignores non open commands."""
    assert open_app.open_app_matcher("find my resume") is None


def test_matcher_ignores_bare_open_with_no_name() -> None:
    """Matcher ignores bare open with no name."""
    assert open_app.open_app_matcher("open") is None
    assert open_app.open_app_matcher("open ") is None


def test_matcher_resolves_installed_app(monkeypatch) -> None:
    """Matcher resolves installed app."""
    fake_matches = [ScoredMatch("Google Chrome", 0.9, "app", r"C:\Start Menu\Google Chrome.lnk")]
    monkeypatch.setattr(open_app, "find_app", lambda name: fake_matches)

    result = open_app.open_app_matcher("open chrome")
    assert isinstance(result, ActionRequest)
    assert result.action_name == "open_app"
    assert result.params == {"target": r"C:\Start Menu\Google Chrome.lnk", "display_name": "Google Chrome"}
    assert result.candidates == [type(result.candidates[0])("Google Chrome", 0.9)]


def test_matcher_falls_back_to_website_when_no_app_matches(monkeypatch) -> None:
    """Matcher falls back to website when no app matches."""
    monkeypatch.setattr(open_app, "find_app", lambda name: [])
    monkeypatch.setattr(
        open_app, "find_website_fallback", lambda name: ScoredMatch("youtube", 0.9, "website", "https://youtube.com")
    )
    monkeypatch.setattr(open_app, "find_chrome", lambda: r"C:\chrome.exe")

    result = open_app.open_app_matcher("open youtube")
    assert isinstance(result, ActionRequest)
    assert result.action_name == "open_url"
    # The site's name travels with it; she says it, never the URL.
    assert result.params == {"url": "https://youtube.com", "browser_path": r"C:\chrome.exe", "display_name": "YouTube"}


def test_matcher_website_match_but_no_chrome_says_so(monkeypatch) -> None:
    """Matcher website match but no Chrome says so."""
    monkeypatch.setattr(open_app, "find_app", lambda name: [])
    monkeypatch.setattr(
        open_app, "find_website_fallback", lambda name: ScoredMatch("youtube", 0.9, "website", "https://youtube.com")
    )
    monkeypatch.setattr(open_app, "find_chrome", lambda: None)

    result = open_app.open_app_matcher("open youtube")
    assert isinstance(result, Answer)
    assert "chrome isn't installed" in result.text.lower()


def test_matcher_nothing_found_says_so_plainly(monkeypatch) -> None:
    """Matcher nothing found says so plainly."""
    monkeypatch.setattr(open_app, "find_app", lambda name: [])
    monkeypatch.setattr(open_app, "find_website_fallback", lambda name: None)
    monkeypatch.setattr(open_app, "find_chrome", lambda: None)  # With Chrome she searches instead

    result = open_app.open_app_matcher("open some nonexistent thing xyz")
    assert isinstance(result, Answer)
    assert result.text == "I couldn't find an app called some nonexistent thing xyz."


def test_matcher_prioritizes_installed_app_over_website(monkeypatch) -> None:
    """F3: 'checks for an installed app first' -- website fallback must
    never even be consulted when an app already matched."""
    fake_matches = [ScoredMatch("YouTube Music", 0.7, "app", r"C:\Start Menu\YouTube Music.lnk")]
    monkeypatch.setattr(open_app, "find_app", lambda name: fake_matches)
    called = []
    monkeypatch.setattr(open_app, "find_website_fallback", lambda name: called.append(name) or None)

    result = open_app.open_app_matcher("open youtube")
    # 0.7 is below the 0.75 launch line, so she asks rather
    # than launching -- but still offers the installed app, not the site.
    assert isinstance(result, Answer) and "1. YouTube Music" in result.text
    assert called == []  # website fallback never consulted


# -- executors: open_app -----------------------------------------------------


def test_open_app_executor_success_on_first_try(monkeypatch) -> None:
    """Open app executor success on first try."""
    calls = []
    monkeypatch.setattr(open_app.os, "startfile", lambda target: calls.append(target))
    monkeypatch.setattr(launch_verify, "snapshot", lambda: (None, 10))
    monkeypatch.setattr(launch_verify, "wait_for_new_activity", lambda bt, bc, timeout, poll: True)

    verified, text = open_app.open_app_executor("open_app", {"target": "chrome.lnk", "display_name": "Chrome"})
    assert verified is True
    assert text == "Opened Chrome."
    assert calls == ["chrome.lnk"]  # exactly one attempt, no retry needed


def test_open_app_executor_retries_once_then_succeeds(monkeypatch) -> None:
    """Open app executor retries once then succeeds."""
    calls = []
    monkeypatch.setattr(open_app.os, "startfile", lambda target: calls.append(target))
    monkeypatch.setattr(launch_verify, "snapshot", lambda: (None, 10))
    results = iter([False, True])
    monkeypatch.setattr(launch_verify, "wait_for_new_activity", lambda bt, bc, timeout, poll: next(results))

    verified, text = open_app.open_app_executor("open_app", {"target": "chrome.lnk", "display_name": "Chrome"})
    assert verified is True
    assert "retry" in text.lower()
    assert len(calls) == 2  # tried twice, per R12


def test_open_app_executor_fails_after_one_retry(monkeypatch) -> None:
    """Open app executor fails after one retry."""
    calls = []
    monkeypatch.setattr(open_app.os, "startfile", lambda target: calls.append(target))
    monkeypatch.setattr(launch_verify, "snapshot", lambda: (None, 10))
    monkeypatch.setattr(launch_verify, "wait_for_new_activity", lambda bt, bc, timeout, poll: False)

    verified, text = open_app.open_app_executor("open_app", {"target": "chrome.lnk", "display_name": "Chrome"})
    assert verified is False
    assert "couldn't confirm" in text.lower()
    assert len(calls) == 2  # exactly one retry, not infinite


def test_open_app_executor_refuses_a_protected_path(monkeypatch) -> None:
    """R9: every module must call the shared path check, even here."""
    from jarvis.security import PROTECTED_FOLDERS

    calls = []
    monkeypatch.setattr(open_app.os, "startfile", lambda target: calls.append(target))
    protected_target = str(PROTECTED_FOLDERS[0] / "some_shortcut.lnk")

    verified, text = open_app.open_app_executor(
        "open_app", {"target": protected_target, "display_name": "Sneaky"}
    )
    assert verified is False
    assert "can't open that" in text.lower()
    assert calls == []  # never even attempted to launch


def test_open_app_executor_handles_startfile_raising(monkeypatch) -> None:
    """Open app executor handles startfile raising."""
    def raise_oserror(target):
        raise OSError("file not found")

    monkeypatch.setattr(open_app.os, "startfile", raise_oserror)
    monkeypatch.setattr(launch_verify, "snapshot", lambda: (None, 10))

    verified, text = open_app.open_app_executor("open_app", {"target": "missing.lnk", "display_name": "Missing"})
    assert verified is False
    assert "couldn't confirm" in text.lower()


# -- executors: open_url -----------------------------------------------------


def test_open_url_executor_success(monkeypatch) -> None:
    """Open URL executor success."""
    calls = []

    class FakePopen:
        def __init__(self, args, shell):
            calls.append((args, shell))

    monkeypatch.setattr(open_app.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(launch_verify, "snapshot", lambda: (None, 10))
    monkeypatch.setattr(launch_verify, "wait_for_new_activity", lambda bt, bc, timeout, poll: True)

    verified, text = open_app.open_url_executor(
        "open_url", {"url": "https://youtube.com", "browser_path": r"C:\chrome.exe", "display_name": "YouTube"}
    )
    assert verified is True
    assert text == "Opening YouTube in Chrome." and "http" not in text
    assert calls == [([r"C:\chrome.exe", "https://youtube.com"], False)]  # never through a shell (R10 spirit)


def test_open_url_executor_fails_after_retry(monkeypatch) -> None:
    """Open URL executor fails after retry."""
    class FakePopen:
        def __init__(self, args, shell):
            pass

    monkeypatch.setattr(open_app.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(launch_verify, "snapshot", lambda: (None, 10))
    monkeypatch.setattr(launch_verify, "wait_for_new_activity", lambda bt, bc, timeout, poll: False)

    verified, text = open_app.open_url_executor(
        "open_url", {"url": "https://youtube.com", "browser_path": r"C:\chrome.exe"}
    )
    assert verified is False


# -- pluggable registration ---------------------------------------------------


def test_register_wires_matcher_and_executors() -> None:
    """Register wires matcher and executors."""
    from jarvis.controller import Controller, State
    from jarvis.interpreter import Interpreter
    from jarvis.orchestrator import Orchestrator
    from jarvis.speech import NullSpeechOutput

    c = Controller(start_state=State.AWAKE)
    interpreter = Interpreter()
    orchestrator = Orchestrator(c, interpreter, NullSpeechOutput())

    open_app.register(interpreter, orchestrator)

    assert any(name == "open_app" for name, *_ in interpreter._matchers)
    assert "open_app" in orchestrator._executors
    assert "open_url" in orchestrator._executors


# -- full pipeline integration: Controller + Logbook + Orchestrator ---------


def test_open_chrome_end_to_end_with_candidates_and_verification(monkeypatch) -> None:
    """Real controller/logbook/orchestrator wiring; only the OS-touching
    bits (finding/launching apps) are mocked, per Section 8."""
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

    fake_matches = [
        ScoredMatch("Google Chrome", 0.95, "app", r"C:\Start Menu\Google Chrome.lnk"),
        ScoredMatch("Chrome Remote Desktop", 0.6, "app", r"C:\Start Menu\Chrome Remote Desktop.lnk"),
    ]
    monkeypatch.setattr(open_app, "find_app", lambda name: fake_matches)
    monkeypatch.setattr(open_app.os, "startfile", lambda target: None)
    monkeypatch.setattr(launch_verify, "snapshot", lambda: (None, 10))
    monkeypatch.setattr(launch_verify, "wait_for_new_activity", lambda bt, bc, timeout, poll: True)

    c = Controller(start_state=State.AWAKE)
    logbook = Logbook()
    c.set_transition_listener(logbook.on_transition)
    interpreter = Interpreter()
    speech = RecordingSpeech()
    orchestrator = Orchestrator(c, interpreter, speech, logbook=logbook)
    open_app.register(interpreter, orchestrator)
    c.start()
    try:
        orchestrator.handle_typed_command("open chrome")
        assert c.state == State.AWAKE

        record = logbook.last_completed()
        assert record.heard == "open chrome"
        assert record.choice == "open_app"
        assert record.rule_or_trust_level == TrustLevel.L1
        assert record.origin == OriginTag.DEVELOPER_REQUESTED
        assert record.result == "Opened Google Chrome."
        assert record.verification_result == "True"
        assert [cand.label for cand in record.candidates] == ["Google Chrome", "Chrome Remote Desktop"]
        assert speech.spoken == ["Opened Google Chrome."]
    finally:
        c.shutdown()


def test_open_nonexistent_app_says_so_and_logs_it(monkeypatch) -> None:
    """Open nonexistent app says so and logs it."""
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

    monkeypatch.setattr(open_app, "find_app", lambda name: [])
    monkeypatch.setattr(open_app, "find_website_fallback", lambda name: None)
    monkeypatch.setattr(open_app, "find_chrome", lambda: None)  # With Chrome she searches instead

    c = Controller(start_state=State.AWAKE)
    logbook = Logbook()
    c.set_transition_listener(logbook.on_transition)
    interpreter = Interpreter()
    speech = RecordingSpeech()
    orchestrator = Orchestrator(c, interpreter, speech, logbook=logbook)
    open_app.register(interpreter, orchestrator)
    c.start()
    try:
        orchestrator.handle_typed_command("open some totally fictional application")
        assert c.state == State.AWAKE
        assert speech.spoken == ["I couldn't find an app called some totally fictional application."]
        record = logbook.last_completed()
        assert record.result == "answered"
    finally:
        c.shutdown()


# -- Unsure matches: list, never launch ---------------------------------

from jarvis.actions.open_app import AppPicker  # noqa: E402

_CLOSE = [
    ScoredMatch("Visual Studio Code", 0.85, "app", r"C:\a\Visual Studio Code.lnk"),
    ScoredMatch("Visual Studio Installer", 0.85, "app", r"C:\a\Visual Studio Installer.lnk"),
    ScoredMatch("Android Studio", 0.59, "app", r"C:\a\Android Studio.lnk"),
]


def test_close_matches_are_listed_not_launched(monkeypatch) -> None:
    """Close matches are listed not launched."""
    monkeypatch.setattr(open_app, "find_app", lambda name: _CLOSE)
    remembered = []
    result = open_app.open_app_matcher("open visual studio", on_ambiguous=remembered.append)
    assert isinstance(result, Answer)
    assert result.text.splitlines() == [
        "I'm not sure which app you mean:",
        "1. Visual Studio Code",
        "2. Visual Studio Installer",
        "3. Android Studio",
        "Say 'open' and the number to pick one.",
    ]
    assert remembered == [_CLOSE]


@pytest.mark.parametrize("pick, expected", [("open 2", "Visual Studio Installer"), ("open number two", "Visual Studio Installer"), ("open the first one", "Visual Studio Code"), ("open android studio", "Android Studio")])
def test_picking_from_the_list(pick, expected) -> None:
    """Picking from the list."""
    picker = AppPicker()
    picker.remember(_CLOSE)
    result = picker.selection_matcher(pick)
    assert isinstance(result, ActionRequest) and result.params["display_name"] == expected


def test_a_pick_survives_an_unrelated_command_until_it_expires() -> None:
    """A pick survives an unrelated command until it expires."""
    # A list stays pickable for [interpreter] pick_valid_seconds.
    from jarvis.actions.command_text import PickSlot

    now = [0.0]
    slot = PickSlot(clock=lambda: now[0], valid_seconds=120)
    picker = AppPicker(slot)
    picker.remember(_CLOSE)
    assert picker.selection_matcher("hello") is None
    assert picker.selection_matcher("open 2").params["display_name"] == "Visual Studio Installer"
    picker.remember(_CLOSE)
    now[0] = 121.0
    assert picker.selection_matcher("open 2") is None  # too old now


def test_an_out_of_range_pick_launches_nothing() -> None:
    """An out of range pick launches nothing."""
    picker = AppPicker()
    picker.remember(_CLOSE)
    result = picker.selection_matcher("open 7")
    assert isinstance(result, Answer) and "from 1 to 3" in result.text  # answered, never launched


def test_stop_clears_the_list() -> None:
    """Stop clears the list."""
    picker = AppPicker()
    picker.remember(_CLOSE)
    picker.on_transition(type("Stop", (), {"event_type": None})())
    assert picker.selection_matcher("open 1") is None
