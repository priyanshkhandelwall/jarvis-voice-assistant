"""F14 Personality and Emotion Awareness: the mood
guess, the threshold test on 30 labelled sentences, and the check-in rules
through the real orchestrator."""

from __future__ import annotations

import inspect
import time
from types import SimpleNamespace
from typing import List

import pytest

from jarvis.controller import Controller, Event, EventType, State
from jarvis.dashboard.activity_feed import format_record, format_session_log
from jarvis.interpreter import ActionRequest, Answer, Interpreter
from jarvis.logbook import Logbook
from jarvis.logbook.debug_log import DebugLog
from jarvis.orchestrator import Orchestrator
from jarvis.settings import SETTINGS
from jarvis.speech import SpeechOutput
from jarvis.speech.mood import MoodScorer, MoodWatcher, scorer_from_settings
from jarvis.speech.qa import GroqAnswerer
from jarvis.speech.rudeness import RudenessFilter

# The spec's threshold test: 30 labelled sentences, 15 upset and 15 not,
# covering calm, annoyed, angry, joking and sarcastic, in the developer's
# own style. (text, upset?, type)
SAMPLES = [
    ("damn it, open chrome", True, "annoyed"),
    ("this is so annoying", True, "annoyed"),
    ("what the hell, why is this not working", True, "angry"),
    ("ugh, this is not working again", True, "annoyed"),
    ("I'm so frustrated with this laptop", True, "frustrated"),
    ("for god's sake just open the file", True, "annoyed"),
    ("nothing ever works on this computer", True, "annoyed"),
    ("I'm sick of this stupid error", True, "angry"),
    ("what the hell is wrong with you", True, "angry"),
    ("seriously, it crashed again", True, "annoyed"),
    ("I'm really stressed about this assignment", True, "stressed"),
    ("oh great, it crashed again, just perfect", True, "sarcastic"),
    ("I hate this, it keeps crashing", True, "angry"),
    ("how many times do I have to say open chrome", True, "annoyed"),
    ("I'm fed up, nothing is working", True, "frustrated"),
    ("thanks jarvis", False, "calm"),
    ("open chrome please", False, "calm"),
    ("what's the weather in Sydney", False, "calm"),
    ("haha that's hilarious, you're killing me", False, "joking"),
    ("damn, that's impressive", False, "joking"),
    ("I'm not frustrated, just curious how this works", False, "calm"),
    ("this isn't annoying at all, nice work", False, "calm"),
    ("find my resume", False, "calm"),
    ("holy crap that actually worked, thanks", False, "joking"),
    ("tell me a joke", False, "calm"),
    ("no worries, take your time", False, "calm"),
    ("the game was crazy last night, what a match", False, "calm"),
    ("I hate waiting for the bus, ha", False, "joking"),
    ("can you open spotify and play something chill", False, "calm"),
    ("good morning jarvis, how are you", False, "calm"),
]

_MOOD = SETTINGS["mood"]
SCORER = scorer_from_settings(_MOOD)


# -- the threshold test (spec F14) -----------------------------------------------------


def test_thirty_labelled_sentences() -> None:
    """Thirty labelled sentences."""
    assert len(SAMPLES) == 30 and sum(1 for _, upset, _ in SAMPLES if upset) == 15
    kinds = {kind for _, _, kind in SAMPLES}
    assert {"calm", "annoyed", "angry", "joking", "sarcastic"} <= kinds


def test_no_false_check_in_on_any_calm_or_joking_sentence() -> None:
    """No false check in on any calm or joking sentence."""
    threshold = float(_MOOD["threshold"])
    false_alarms = [t for t, upset, _ in SAMPLES if not upset and SCORER.score(t).score >= threshold]
    assert false_alarms == []


def test_recall_at_the_chosen_threshold() -> None:
    """Recall at the chosen threshold."""
    threshold = float(_MOOD["threshold"])
    caught = [t for t, upset, _ in SAMPLES if upset and SCORER.score(t).score >= threshold]
    assert len(caught) >= 14  # 14 of 15; the sarcastic one is missed (documented)


# -- the scorer's rules --------------------------------------------------------------------


def _scorer(**kw):
    defaults = dict(weights={"annoying": 0.6, "not working": 0.5, "ugh": 0.55, "damn": 0.35, "what the heck": 0.8, "heck": 0.55},
                    intensifiers=["so"], negations=["not", "isn't"], positives=["thanks"], intensity_boost=1.3,
                    negation_factor=0.1, negation_window=2, positive_damping=0.5, exclaim_boost=0.05, exclaim_max=3, max_cue_weight=0.95)
    defaults.update(kw)
    return MoodScorer(**defaults)


def test_scores_are_between_zero_and_one() -> None:
    """Scores are between zero and one."""
    for text, _, _ in SAMPLES:
        assert 0.0 <= SCORER.score(text).score <= 1.0
    assert _scorer().score("").score == 0.0


def test_negation_intensity_positive_and_exclamation() -> None:
    """Negation intensity positive and exclamation."""
    s = _scorer()
    assert s.score("this is annoying").score == 0.6
    assert s.score("this is so annoying").score == 0.78
    assert s.score("this isn't annoying").score == 0.06
    assert s.score("damn thanks").score == pytest.approx(0.18, abs=0.01)
    assert s.score("this is annoying!!").score == 0.7
    assert s.score("ugh, not working").score == pytest.approx(0.78, abs=0.01)  # two cues: 1 - 0.45 * 0.5


def test_longest_phrase_counts_once() -> None:
    """Longest phrase counts once."""
    reading = _scorer().score("what the heck")
    assert reading.matched == ("what the heck",) and reading.score == 0.8


# -- the watcher: when she checks in ---------------------------------------------------


def _watcher(clock=None, **overrides):
    cfg = {**_MOOD, **overrides}
    return MoodWatcher(cfg, clock=clock or (lambda: 0.0), choose=lambda pool: pool[0])


def test_calm_command_notices_nothing() -> None:
    """Calm command notices nothing."""
    note = _watcher().hear("open chrome please")
    assert note.noticed is None and note.check_in is None


def test_upset_command_is_noticed_with_the_score() -> None:
    """Upset command is noticed with the score."""
    note = _watcher().hear("I'm fed up, nothing is working")
    assert note.noticed == "noticed: you sound frustrated (0.95)"
    assert note.check_in == _MOOD["check_in_pool"][0]


def test_ten_minute_cooldown_starts_only_when_she_really_asks() -> None:
    """Ten minute cooldown starts only when she really asks."""
    now = [0.0]
    w = _watcher(clock=lambda: now[0])
    first = w.hear("this is so annoying")
    assert first.check_in  # not said yet (e.g. Stop) ...
    assert w.hear("this is so annoying").check_in  # ... so it can still be asked
    w.checked_in(first.check_in)
    now[0] = 599.0
    later = w.hear("I'm so frustrated with this laptop")
    assert later.noticed and later.check_in is None
    now[0] = 600.0
    assert w.hear("I'm so frustrated with this laptop").check_in is not None
    assert _MOOD["cooldown_seconds"] == 600


def test_never_the_same_line_twice_running() -> None:
    """Never the same line twice running."""
    seen: List[str] = []
    w = MoodWatcher(_MOOD, clock=lambda: len(seen) * 1000.0, choose=lambda pool: pool[0])
    for _ in range(3):
        line = w.hear("I'm fed up").check_in
        w.checked_in(line)
        seen.append(line)
    assert seen[0] != seen[1] and seen[1] != seen[2]


def test_saying_fine_stops_check_ins_for_the_session() -> None:
    """Saying fine stops check ins for the session."""
    w = _watcher()
    w.checked_in(w.hear("I'm fed up").check_in)
    assert w.reply_to_check_in("I'm fine, thanks") == _MOOD["fine_reply"]
    assert w.hear("I'm so frustrated with this").check_in is None


def test_only_the_next_command_can_answer_the_check_in() -> None:
    """Only the next command can answer the check in."""
    w = _watcher()
    w.checked_in("You okay?")
    assert w.reply_to_check_in("open chrome") is None
    assert w.reply_to_check_in("I'm fine") is None  # too late: an ordinary command now


def test_tired_or_stressed_makes_online_answers_gentler() -> None:
    """Tired or stressed makes online answers gentler."""
    w = _watcher()
    assert w.prompt_hint() is None
    w.hear("I'm tired, find my notes")
    a = GroqAnswerer(api_key="k", style_hint=w.prompt_hint)
    assert _MOOD["gentle_prompt"] in a.system_prompt


def test_turned_off_does_nothing() -> None:
    """Turned off does nothing."""
    w = _watcher(enabled=False)
    note = w.hear("I'm so frustrated, this is damn useless")
    assert note.noticed is None and note.check_in is None and w.prompt_hint() is None


def test_the_mood_module_writes_nothing_and_sends_nothing() -> None:
    """The mood module writes nothing and sends nothing."""
    import jarvis.speech.mood as mood_module

    source = inspect.getsource(mood_module)
    for forbidden in ("open(", "write_text", "urllib", "groq", "requests", "socket"):
        assert forbidden not in source


# -- through the orchestrator ---------------------------------------------------------------


class FakeSpeech(SpeechOutput):
    """Speech output stand-in: records what she would say; nothing is played."""
    def __init__(self) -> None:
        self.texts: List[str] = []

    def speak(self, text: str) -> None:
        """Fake speak for the tests."""
        self.texts.append(text)


QUESTION = "Hamilton in Ontario, or Hamilton in Ohio?"


@pytest.fixture
def rig(tmp_path):
    """Fixture: the components this file's tests drive, with every side effect mocked."""
    c = Controller(start_state=State.AWAKE)
    logbook = Logbook(debug_log=DebugLog(path=tmp_path / "debug.log"))
    c.set_transition_listener(logbook.on_transition)
    c.start()
    interpreter = Interpreter()
    interpreter.register("open", lambda t: ActionRequest("open_app", {"display_name": "Google Chrome", "path": "x"}) if "chrome" in t else None)
    interpreter.register("ask", lambda t: Answer(QUESTION) if "hamilton" in t else None)
    interpreter.register("answer", lambda t: Answer("Here you go."))
    executed: List[dict] = []
    speech = FakeSpeech()
    mood = MoodWatcher(_MOOD, choose=lambda pool: pool[0])
    orch = Orchestrator(c, interpreter, speech, logbook=logbook, rudeness=RudenessFilter(SETTINGS["rudeness"]["words"]), mood=mood)
    orch.register_executor("open_app", lambda name, params: (executed.append(dict(params)) or True, "Opened Google Chrome."))

    def say(text):
        c.submit(Event(EventType.SPEECH_STARTED, {"source": "voice"}))
        deadline = time.monotonic() + 3
        while c.state != State.LISTENING and time.monotonic() < deadline:
            time.sleep(0.01)
        orch.handle_command(text, "voice")

    yield SimpleNamespace(c=c, logbook=logbook, speech=speech, say=say, executed=executed, debug=tmp_path / "debug.log", mood=mood)
    c.shutdown()


CHECK_IN = _MOOD["check_in_pool"][0]


def test_check_in_comes_after_the_request_is_done(rig) -> None:
    """Check in comes after the request is done."""
    rig.say("this is so annoying, open chrome")
    assert rig.executed == [{"display_name": "Google Chrome", "path": "x"}]  # the request still runs
    assert rig.speech.texts == [f"Opened Google Chrome. {CHECK_IN}"]  # the answer first, then she asks
    lines = format_record(rig.logbook.last_completed()).lines
    assert lines[1] == "noticed: you sound frustrated (0.78)"
    assert lines[-1] == f"checking in: {CHECK_IN}"


def test_the_guess_never_changes_the_action(rig) -> None:
    """The guess never changes the action."""
    rig.say("open chrome")
    rig.say("ugh this is so annoying open chrome")
    assert len(rig.executed) == 2 and rig.executed[0] == rig.executed[1]


def test_rudeness_wins_no_check_in(rig) -> None:
    """Rudeness wins no check in."""
    rig.say("what the hell, why is chrome not working, you idiot, open chrome")
    spoken = rig.speech.texts[-1]
    assert spoken.startswith(SETTINGS["rudeness"]["reply_text"])
    assert not any(line in spoken for line in _MOOD["check_in_pool"])
    lines = format_record(rig.logbook.last_completed()).lines
    assert "noted: rude wording" in lines and not any(line.startswith("noticed:") for line in lines)


def test_mood_lines_are_session_only(rig) -> None:
    """Mood lines are session only."""
    rig.say("I'm fed up, nothing is working")
    shown = format_session_log(rig.logbook.records())
    assert "noticed: you sound frustrated" in shown and "checking in:" in shown  # on screen and in COPY LOG
    written = rig.debug.read_text(encoding="utf-8")
    assert "noticed" not in written and "checking in" not in written and CHECK_IN not in written
    assert "Here you go." in written  # the rest of the record is still logged


def test_fine_after_a_check_in_is_answered_locally(rig) -> None:
    """Fine after a check in is answered locally."""
    rig.say("I'm fed up, nothing is working")
    rig.say("I'm fine")
    assert rig.speech.texts[-1] == _MOOD["fine_reply"]
    rig.say("I'm so frustrated with this laptop")
    assert rig.speech.texts[-1] == "Here you go."  # no more check-ins this session


def test_no_check_in_after_a_reply_that_is_itself_a_question(rig) -> None:
    """No check in after a reply that is itself a question."""
    rig.say("ugh, this is so annoying, weather in hamilton")
    assert rig.speech.texts[-1] == QUESTION  # one question at a time
    rig.say("ugh this is so annoying")  # the check-in wasn't used up
    assert rig.speech.texts[-1] == f"Here you go. {CHECK_IN}"
