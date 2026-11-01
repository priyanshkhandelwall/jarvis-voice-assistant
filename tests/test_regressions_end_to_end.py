"""Regressions for the bugs the end-to-end verification found. F3's skip has its own
test in test_f21_briefing.py; the data-guard gap in test_data_guard.py."""

from __future__ import annotations

from jarvis.actions.app_finder import ScoredMatch, confident
from jarvis.controller import State
from jarvis.dashboard.activity_feed import format_record
from jarvis.logbook.records import DecisionRecord
from jarvis.orchestrator import Orchestrator
from stretch_rig import SpyAnswerer, build, wait


def _match(name: str, score: float) -> ScoredMatch:
    return ScoredMatch(display_name=name, score=score, kind="app", target=f"{name}.exe")


def test_an_exact_app_name_opens_even_with_a_close_runner_up() -> None:
    """D5: "open paint" asked "paint, or Paint 3D?" (1.0 vs 0.95)."""
    assert confident([_match("paint", 1.0), _match("Paint 3D", 0.95)])


def test_two_exact_names_still_ask() -> None:
    """Two exact names still ask."""
    assert not confident([_match("paint", 1.0), _match("Paint", 1.0)])


def test_close_fuzzy_matches_still_ask() -> None:
    """Close fuzzy matches still ask."""
    assert not confident([_match("Visual Studio", 0.9), _match("Visual Studio Code", 0.85)])


class _Mood:
    """Test helper: Mood."""
    def __init__(self) -> None:
        self.asked = []

    def checked_in(self, line: str) -> None:
        """Fake checked in for the tests."""
        self.asked.append(line)


def test_no_check_in_after_a_numbered_list_to_pick_from() -> None:
    """D5: the mood check-in was tacked onto "1. paint 2. Paint 3D Say
    'open' and the number" -- two questions at once."""
    orch = Orchestrator.__new__(Orchestrator)
    import threading

    orch._command = threading.local()
    orch._mood = _Mood()
    orch._logbook = None
    listing = "I'm not sure which app you mean:\n1. paint\n2. Paint 3D\nSay 'open' and the number to pick one."
    orch._command.check_in = "Are you alright?"
    assert orch._say("", listing) == listing
    orch._command.check_in = "Are you alright?"
    assert orch._say("", "Opened paint.") == "Opened paint. Are you alright?"


def test_a_typed_stop_is_a_stop_not_a_question(tmp_path, monkeypatch) -> None:
    """F11: "Say stop to cancel" -- a typed "stop" went online instead."""
    groq = SpyAnswerer()
    rig = build(tmp_path, monkeypatch, groq=groq)
    rig["orch"].handle_typed_command("stop")
    assert wait(lambda: rig["c"].is_stop_requested())
    assert groq.questions == []
    assert rig["c"].state == State.AWAKE


def test_a_typed_stop_while_confirming_is_still_a_cancel(tmp_path, monkeypatch) -> None:
    """A typed stop while confirming is still a cancel."""
    rig = build(tmp_path, monkeypatch)
    rig["say"]("forget everything")  # nothing stored: no confirmation needed
    rig["say"]("remember that I like tea")
    rig["say"]("forget everything")
    assert rig["c"].state == State.CONFIRMING
    rig["orch"].handle_typed_command("stop")
    assert wait(lambda: rig["c"].state == State.AWAKE)
    assert rig["speech"].texts[-1] == "Cancelled."


def test_stop_while_saying_an_answer_is_recorded_as_stopped(tmp_path, monkeypatch) -> None:
    """G1: the record said "answered" though Stop cut her off."""
    rig = build(tmp_path, monkeypatch)
    c, speech = rig["c"], rig["speech"]
    real_speak = speech.speak

    def speak(text):
        real_speak(text)
        c.request_stop(source="hotkey")  # Stop while she is saying it

    speech.speak = speak
    rig["say"]("tell me about chess")
    assert wait(lambda: rig["logbook"].records() and rig["logbook"].records()[-1].completed)
    record = rig["logbook"].records()[-1]
    assert record.result == "stopped before finishing"
    assert format_record(record).lines[-1] == "stopped before finishing"


def test_stop_after_an_action_ran_keeps_its_result(tmp_path, monkeypatch) -> None:
    """Stop after an action ran keeps its result."""
    rig = build(tmp_path, monkeypatch)
    c, speech = rig["c"], rig["speech"]
    real_speak = speech.speak

    def speak(text):
        real_speak(text)
        c.request_stop(source="hotkey")

    speech.speak = speak
    rig["say"]("open gmail in chrome")
    assert wait(lambda: rig["logbook"].records() and rig["logbook"].records()[-1].completed)
    record = rig["logbook"].records()[-1]
    assert record.understood["action_name"] == "open_url" and rig["chrome"]
    assert record.result != "stopped before finishing", (record.understood, rig["speech"].texts)


def test_spoken_one_word_system_commands_are_not_fragments() -> None:
    """F7/F11: spoken "Mute", "Unmute", "Restart" were ignored as too short."""
    from jarvis.voice.listener import fragment_reason

    for word in ("Mute", "Unmute.", "Restart"):
        assert fragment_reason(word, 0.1) is None, word
    assert fragment_reason("Banana", 0.1) == "too short"


def test_a_run_together_brief_me_is_still_the_briefing() -> None:
    """F3: Whisper heard "BriefMe" (one word; dropped as too short) and
    "Brief man" (went online)."""
    from jarvis.news.briefing import _ON_DEMAND, _phrase
    from jarvis.voice.listener import fragment_reason

    assert fragment_reason("BriefMe.", 0.1) is None
    assert _phrase("BriefMe.") in _ON_DEMAND
    assert _phrase("Brief man.") in _ON_DEMAND


def test_confirm_with_nothing_pending_says_so_plainly(tmp_path, monkeypatch) -> None:
    """F11: she said "Nothing to confirm: deny by default: no such transition in the fixed table"."""
    rig = build(tmp_path, monkeypatch)
    rig["orch"].handle_typed_command("confirm")
    assert rig["speech"].texts[-1] == "Nothing to confirm right now."


def test_a_misheard_rain_question_is_still_weather() -> None:
    """C3: Whisper heard "Will it reign in Sydney today?" and it went online."""
    from jarvis.actions.weather import parse_request

    assert parse_request("will it reign in sydney today").kind == "rain"
    assert parse_request("how long was victorias reign in england") is None


def test_rude_your_is_removed_with_the_insult() -> None:
    """D6: "Your useless open notepad" left "Your open notepad" and went online."""
    from jarvis.settings import SETTINGS
    from jarvis.speech.rudeness import RudenessFilter

    rude = RudenessFilter(SETTINGS["rudeness"]["words"])
    assert rude.check("Your useless open notepad").cleaned == "open notepad"
    assert rude.check("open your notes").rude is False


def test_play_on_youtube_split_by_a_comma(tmp_path, monkeypatch) -> None:
    """F4: Whisper heard "Play Arrogit, sing on YouTube"; it went online."""
    from jarvis.youtube.commands import YouTubeCommands

    groq = SpyAnswerer()
    rig = build(tmp_path, monkeypatch, groq=groq, youtube=YouTubeCommands(groq, chrome=lambda: r"C:\Chrome\chrome.exe"))
    rig["say"]("Play Arrogit, sing on YouTube.")
    assert groq.questions == []
    assert rig["chrome"] and "youtube.com/results" in rig["chrome"][-1][1]


def test_a_stopped_answer_says_so_in_live_activity() -> None:
    """G1: Stop mid-answer left the row looking finished."""
    record = DecisionRecord.start("tell me about chess")
    record.understood = {"answer": "Chess began in India."}
    record.result = "stopped before finishing"
    record.completed = True
    assert format_record(record).lines[-1] == "stopped before finishing"
