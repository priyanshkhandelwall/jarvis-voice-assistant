"""Conversation mode (developer-approved; merges F2 and F15):
the state table, waking and sleeping, typed text while asleep, "cancel",
and F15 non-retention. Speech and Groq are faked."""

from __future__ import annotations

import time
from typing import List

import pytest

from jarvis.actions.conversation import Conversation
from jarvis.actions.sleep_wake import SleepMatcher, SleepRequest, is_wake_phrase
from jarvis.controller import Controller, EventType, State
from jarvis.controller.permits import OriginTag, PendingConfirmation
from jarvis.controller.transitions import ALLOWED_TRANSITIONS
from jarvis.controller.trust import TrustLevel
from jarvis.dashboard.activity_feed import format_record
from jarvis.interpreter import Interpreter
from jarvis.logbook import Logbook
from jarvis.logbook.debug_log import DebugLog
from jarvis.orchestrator import Orchestrator
from jarvis.settings import SETTINGS
from jarvis.speech import SpeechOutput
from jarvis.speech.qa import AnswerResult

_CONV = SETTINGS["conversation"]


def _wait(pred, timeout=3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class FakeSpeech(SpeechOutput):
    """Speech output stand-in: records what she would say; nothing is played."""
    def __init__(self) -> None:
        self.texts: List[str] = []

    def speak(self, text: str) -> None:
        """Fake speak for the tests."""
        self.texts.append(text)


class SpyAnswerer:
    """Stands in for Groq; records anything that would have gone online."""

    model = "test-model"

    def __init__(self) -> None:
        self.questions: List[str] = []

    def answer(self, question, on_going_online=None):
        """Fake answer for the tests."""
        self.questions.append(question)
        return AnswerResult("a Groq answer", went_online=True)


@pytest.fixture
def rig(tmp_path):
    """Fixture: the components this file's tests drive, with every side effect mocked."""
    made = []

    def _make(start_state=State.SLEEPING, hour=9):
        c = Controller(start_state=start_state)
        debug = DebugLog(path=tmp_path / "debug.log")
        logbook = Logbook(debug_log=debug)
        c.set_transition_listener(logbook.on_transition)
        c.start()
        made.append(c)
        interpreter = Interpreter()
        interpreter.register("sleep", SleepMatcher())
        speech = FakeSpeech()
        notices: List[str] = []
        orch = Orchestrator(c, interpreter, speech, logbook=logbook, notice=notices.append, clock_hour=lambda: hour)
        answerer = SpyAnswerer()
        Conversation(answerer).register(interpreter, orch)
        return c, orch, speech, logbook, answerer, notices, tmp_path / "debug.log"

    yield _make
    for c in made:
        c.shutdown()


# -- the state table (F17) -------------------------------------------------------------


def test_startup_state_is_sleeping() -> None:
    """Startup state is sleeping."""
    assert Controller().state == State.SLEEPING


@pytest.mark.parametrize(
    "start, event, target",
    [
        (State.SLEEPING, EventType.WAKE, State.SPEAKING),
        (State.SPEAKING, EventType.GO_TO_SLEEP, State.SLEEPING),
        (State.AWAKE, EventType.GO_TO_SLEEP, State.SLEEPING),
        (State.AWAKE, EventType.SPEECH_STARTED, State.LISTENING),
        (State.LISTENING, EventType.LISTEN_TIMEOUT, State.AWAKE),
        (State.SPEAKING, EventType.SPEECH_FINISHED, State.AWAKE),
        (State.CONFIRMING, EventType.CANCEL_RECEIVED, State.SPEAKING),
    ],
)
def test_conversation_mode_transitions(start, event, target) -> None:
    """Conversation mode transitions."""
    assert ALLOWED_TRANSITIONS[(start, event)] == target


@pytest.mark.parametrize(
    "event",
    [e for e in EventType if e != EventType.WAKE],
)
def test_nothing_but_wake_leaves_sleeping(event) -> None:
    """Nothing but wake leaves sleeping."""
    assert (State.SLEEPING, event) not in ALLOWED_TRANSITIONS


@pytest.mark.parametrize("state", [s for s in State if s != State.SLEEPING])
def test_wake_is_invalid_unless_sleeping(state) -> None:
    """Wake is invalid unless sleeping."""
    assert (state, EventType.WAKE) not in ALLOWED_TRANSITIONS


@pytest.mark.parametrize("state", [s for s in State if s != State.CONFIRMING])
def test_cancel_is_invalid_unless_confirming(state) -> None:
    """Cancel is invalid unless confirming."""
    assert (state, EventType.CANCEL_RECEIVED) not in ALLOWED_TRANSITIONS


def test_stop_while_sleeping_leaves_her_sleeping() -> None:
    """Stop while sleeping leaves her sleeping."""
    c = Controller(start_state=State.SLEEPING)
    c.start()
    try:
        c.request_stop("hotkey")
        assert _wait(lambda: c.transition_count() == 1)
        entry = c.last_transition()
        assert entry.event_type is None and entry.to_state == State.SLEEPING
        assert c.state == State.SLEEPING
    finally:
        c.shutdown()


def test_stop_from_awake_states_returns_to_awake() -> None:
    """Stop from awake states returns to awake."""
    c = Controller(start_state=State.LISTENING)
    c.start()
    try:
        c.request_stop("hotkey")
        assert _wait(lambda: c.state == State.AWAKE)
    finally:
        c.shutdown()


# -- waking --------------------------------------------------------------------------------


@pytest.mark.parametrize("hour, word", [(9, "morning"), (14, "afternoon"), (20, "evening")])
def test_wake_greets_by_time_of_day_then_she_is_awake(rig, hour, word) -> None:
    """Wake greets by time of day then she is awake."""
    c, orch, speech, logbook, answerer, notices, debug_path = rig(hour=hour)
    assert orch.wake("wake word") is True
    # Redesigned One varied line from [greeting.pools] (the
    # first wake of the day here); shown in Live Activity too.
    pools = SETTINGS["greeting"]["pools"]
    allowed = [line.replace("{name}", SETTINGS["user"]["name"]) for line in pools["first_wake"]]
    assert len(speech.texts) == 1 and speech.texts[0] in allowed
    assert _wait(lambda: c.state == State.AWAKE)
    assert format_record(logbook.records()[0]).lines == ["woke up (wake word)", f"saying: {speech.texts[0]}"]


def test_typed_wake_phrase_wakes_her(rig) -> None:
    """Typed wake phrase wakes her."""
    c, orch, speech, logbook, answerer, notices, debug_path = rig()
    orch.handle_typed_command("Wake up, JARVIS")
    assert _wait(lambda: c.state == State.AWAKE)
    greetings = {line.replace("{name}", SETTINGS["user"]["name"]) for pool in SETTINGS["greeting"]["pools"].values() for line in pool}
    assert len(speech.texts) == 1 and speech.texts[0] in greetings


def test_typed_text_while_asleep_is_discarded_unlogged(rig) -> None:
    """Typed text while asleep is discarded unlogged."""
    c, orch, speech, logbook, answerer, notices, debug_path = rig()
    orch.handle_typed_command("my bank pin is 1234 open chrome")
    assert c.state == State.SLEEPING
    assert notices == [_CONV["asleep_typed_notice"]]
    assert logbook.records() == [] and speech.texts == [] and answerer.questions == []
    assert not debug_path.exists() or "chrome" not in debug_path.read_text(encoding="utf-8")


def test_wake_phrase_matching() -> None:
    """Wake phrase matching."""
    for said in ("wake up jarvis", "Wake up, JARVIS.", "Hey JARVIS", "hey jarvis, open chrome", "JARVIS, wake up"):
        assert is_wake_phrase(said), said
    for said in ("jarvis", "wake up", "hey", "my friend jarvis said hi"):
        assert not is_wake_phrase(said), said


# -- going to sleep ------------------------------------------------------------------------------


@pytest.mark.parametrize("said", ["go to sleep", "Sleep, JARVIS.", "goodnight jarvis", "Good night", "jarvis go to sleep"])
def test_sleep_matcher(said) -> None:
    """Sleep matcher."""
    assert SleepMatcher()(said.lower()) == SleepRequest(_CONV["sleep_reply"])


def test_sleep_matcher_ignores_other_sentences() -> None:
    """Sleep matcher ignores other sentences."""
    assert SleepMatcher()("i could not sleep last night") is None


@pytest.mark.parametrize("command", ["go to sleep", "goodnight jarvis", "sleep jarvis"])
def test_go_to_sleep_says_goodnight_and_never_goes_online(rig, command) -> None:
    """Go to sleep says goodnight and never goes online."""
    c, orch, speech, logbook, answerer, notices, debug_path = rig(start_state=State.AWAKE)
    orch.handle_typed_command(command)
    assert _wait(lambda: c.state == State.SLEEPING)
    assert speech.texts == ["Goodnight."]
    assert answerer.questions == []  # handled locally, never sent to Groq
    assert format_record(logbook.records()[-1]).lines == ["went to sleep"]


def test_spoken_go_to_sleep_strips_whisper_punctuation(rig) -> None:
    """Spoken go to sleep strips Whisper punctuation."""
    c, orch, speech, logbook, answerer, notices, debug_path = rig(start_state=State.LISTENING)
    orch.handle_command("Go to sleep.", "voice")
    assert _wait(lambda: c.state == State.SLEEPING)
    assert answerer.questions == []


def test_auto_sleep_is_off_by_default() -> None:
    """Auto sleep is off by default."""
    assert _CONV["auto_sleep_minutes"] == 0


def test_auto_sleep_when_enabled(monkeypatch) -> None:
    """Auto sleep when enabled."""
    from jarvis.voice.system import VoiceSystem

    c = Controller(start_state=State.AWAKE)
    c.start()
    try:
        system = VoiceSystem(listener=None, controller=c, auto_sleep_minutes=0.0001)
        system._last_activity = time.monotonic() - 1
        system._maybe_auto_sleep()
        assert _wait(lambda: c.state == State.SLEEPING)
    finally:
        c.shutdown()


# -- cancel ---------------------------------------------------------------------------------------


def _pending(c):
    with c._state_lock:
        c._pending_confirmation = PendingConfirmation.issue(
            action_name="lock_screen", params={"text": "hi"}, origin=OriginTag.DEVELOPER_REQUESTED,
            trust_level=TrustLevel.L2, timeout_seconds=60, confirmations_required=1,
        )


def test_typed_cancel_drops_the_pending_action(rig) -> None:
    """Typed cancel drops the pending action."""
    c, orch, speech, logbook, answerer, notices, debug_path = rig(start_state=State.CONFIRMING)
    _pending(c)
    orch.handle_typed_command("cancel")
    assert _wait(lambda: c.state == State.AWAKE)
    assert speech.texts == [_CONV["cancelled_reply"]]
    assert c.pending_confirmation() is None
    orch.handle_typed_command("confirm")
    assert speech.texts[-1].startswith("Nothing to confirm")  # Section 5: nothing left


def test_cancel_with_nothing_pending_says_so(rig) -> None:
    """Cancel with nothing pending says so."""
    c, orch, speech, logbook, answerer, notices, debug_path = rig(start_state=State.AWAKE)
    orch.handle_typed_command("cancel")
    assert speech.texts == [_CONV["nothing_to_cancel_reply"]]
    assert answerer.questions == []


# -- F15 non-retention -------------------------------------------------------------------------------


def test_heard_while_asleep_is_shown_but_never_written_to_disk(rig) -> None:
    """Heard while asleep is shown but never written to disk."""
    c, orch, speech, logbook, answerer, notices, debug_path = rig()
    secret_words = "the quick purple elephant recipe"
    logbook.record_heard_while_asleep(secret_words)
    logbook.record_ignored("purple elephant", "too short")
    orch.wake("wake word")  # forces a debug-log write of the "woke up" entry
    lines = [format_record(r).lines[0] for r in logbook.records()]
    assert f"heard while asleep: {secret_words}" in lines
    assert "ignored (too short): purple elephant" in lines
    assert debug_path.exists()
    written = debug_path.read_text(encoding="utf-8")
    assert "elephant" not in written and "purple" not in written


def test_asleep_transcripts_are_redacted_like_any_record(rig) -> None:
    """Asleep transcripts are redacted like any record."""
    c, orch, speech, logbook, answerer, notices, debug_path = rig()
    logbook.record_heard_while_asleep("my password is hunter2")
    assert format_record(logbook.records()[-1]).lines == ["heard while asleep: [redacted]"]


def test_why_skips_asleep_and_event_entries(rig) -> None:
    """Why skips asleep and event entries."""
    from jarvis.actions.explain import ExplainMatcher
    from jarvis.logbook.logbook import NOTHING_YET

    c, orch, speech, logbook, answerer, notices, debug_path = rig()
    logbook.record_heard_while_asleep("something on the radio")
    orch.wake("wake word")
    assert ExplainMatcher(logbook)("why did you do that").text == NOTHING_YET


# -- spoken wording (Change 4) ---------------------------------------------------------------------


def test_she_says_searching_online_and_never_the_provider() -> None:
    """She says searching online and never the provider."""
    assert SETTINGS["speech"]["checking_online_text"] == "Searching online."
    for key, message in SETTINGS["setup_messages"].items():
        if key.startswith("groq_"):
            assert "Groq" not in message.replace("GROQ_API_KEY", ""), key


def test_the_log_still_records_the_destination(rig) -> None:
    """The log still records the destination."""
    c, orch, speech, logbook, answerer, notices, debug_path = rig(start_state=State.AWAKE)
    answerer.answer = lambda q, on_going_online=None: (on_going_online(), AnswerResult("ok", went_online=True))[1]
    orch.handle_typed_command("what is photosynthesis")
    assert _wait(lambda: c.state == State.AWAKE)
    record = logbook.last_completed()
    # R3 amended The destination is in the record, not Live Activity.
    assert 'searching online, sending "what is photosynthesis"' in record.progress
    assert record.online_destination.startswith("Groq, ")
    assert speech.texts[0] == "Searching online."
