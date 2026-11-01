"""Real voice commands. Every failure, one test
per line, with the EXACT transcripts. Commands go through the production
matcher order (app.register_matchers), the real orchestrator with the real
rudeness filter and mood watcher, and the real F3/F4 finders. Faked: the
apps on the laptop, the launching itself, and Groq (a spy: any command
that reaches it fails the "never online" checks)."""

from __future__ import annotations

import os
import time
from typing import List

import pytest

from jarvis.actions import app_finder, find_file, open_app
from jarvis.actions.app_finder import ScoredMatch
from jarvis.actions.command_text import extract_command
from jarvis.actions.file_index import FileIndex
from jarvis.actions.find_file import FileFinder, listing_lines
from jarvis.actions.file_index import FileMatch
from jarvis.actions.sleep_wake import SleepMatcher, SleepRequest
from jarvis.controller import Controller, Event, EventType, State
from jarvis.dashboard.activity_feed import format_record, format_session_log
from jarvis.dashboard.app import register_matchers
from jarvis.interpreter import Interpreter
from jarvis.logbook import Logbook
from jarvis.logbook.debug_log import DebugLog
from jarvis.orchestrator import Orchestrator
from jarvis.settings import SETTINGS
from jarvis.speech import SpeechOutput
from jarvis.speech.claims import ClaimCheck
from jarvis.speech.mood import MoodWatcher
from jarvis.speech.qa import AnswerResult
from jarvis.speech.rudeness import RudenessFilter


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
    """Stands in for Groq. Records every question that would have gone online
    and replies with `reply` (to test the false-claim check)."""

    model = "openai/gpt-oss-20b"

    def __init__(self) -> None:
        self.questions: List[str] = []
        self.reply = "an online answer"

    def today(self):
        """Fake today for the tests."""
        from datetime import date

        return date(2026, 10, 23)

    def answer(self, question, on_going_online=None):
        """Fake answer for the tests."""
        self.questions.append(question)
        if on_going_online is not None:
            on_going_online()
        return AnswerResult(self.reply, went_online=True)

    answer_live = answer


class NoStoreApps:
    """Store-app list stand-in: no Store apps and no background read."""
    def prefetch(self) -> None:
        """Fake prefetch for the tests."""
        pass

    def apps(self, wait_seconds: float = 0.0):
        """Fake apps for the tests."""
        return []


_APPS = [
    ScoredMatch("Google Chrome", 0.0, "app", r"C:\Start\Google Chrome.lnk"),
    ScoredMatch("Visual Studio Code", 0.0, "app", r"C:\Start\Visual Studio Code.lnk"),
    ScoredMatch("Android Studio", 0.0, "app", r"C:\Start\Android Studio.lnk"),
    ScoredMatch("notepad", 0.0, "app", "notepad.exe"),
    ScoredMatch("OneDrive", 0.0, "app", r"C:\Start\OneDrive.lnk"),
    ScoredMatch("OneNote", 0.0, "app", r"C:\Start\OneNote.lnk"),
]
# The four passport PDFs from the log (two share a name).
_PASSPORTS = [
    ("Downloads", "Alex Passport .pdf"),
    ("Desktop/Archive", "Alex Passport .pdf"),
    ("Downloads", "Alex Taylor Passport .pdf"),
    ("Desktop/Scans", "Alex Taylor Passport.pdf"),
]


@pytest.fixture
def laptop(tmp_path, monkeypatch):
    """Fixture: the production wiring with a fake laptop (apps, files, screen, camera), nothing real
    opened."""
    launched: List[str] = []
    chrome_runs: List[list] = []
    root = tmp_path / "drive"
    for folder, name in _PASSPORTS:
        (root / folder).mkdir(parents=True, exist_ok=True)
        (root / folder / name).write_text("pdf", encoding="utf-8")
    index = FileIndex(roots=[root])
    index.rebuild_now()

    monkeypatch.setattr(app_finder, "enumerate_installed_apps", lambda store_apps=None: list(_APPS))
    monkeypatch.setattr(open_app, "STORE_APPS", NoStoreApps())
    monkeypatch.setattr(open_app, "find_chrome", lambda: r"C:\Chrome\chrome.exe")
    monkeypatch.setattr(open_app.os, "startfile", launched.append)
    monkeypatch.setattr(find_file.os, "startfile", launched.append)
    monkeypatch.setattr(open_app.subprocess, "Popen", lambda args, shell=False: chrome_runs.append(args))
    verify = lambda launch, timeout, poll: (launch(), False)  # (verified, needed a retry)  # noqa: E731
    monkeypatch.setattr(open_app, "launch_and_verify_with_retry", verify)
    monkeypatch.setattr(find_file, "launch_and_verify_with_retry", verify)

    c = Controller(start_state=State.AWAKE)
    logbook = Logbook(debug_log=DebugLog(path=tmp_path / "debug.log"))
    c.set_transition_listener(logbook.on_transition)
    c.start()
    interpreter = Interpreter()
    speech = FakeSpeech()
    orch = Orchestrator(
        c, interpreter, speech, logbook=logbook,
        rudeness=RudenessFilter(SETTINGS["rudeness"]["words"]),
        mood=MoodWatcher(SETTINGS["mood"], choose=lambda pool: pool[0]),
    )
    groq = SpyAnswerer()
    register_matchers(interpreter, orch, logbook, FileFinder(index), groq, None)

    def say(text: str) -> None:
        c.submit(Event(EventType.SPEECH_STARTED, {"source": "voice"}))
        assert _wait(lambda: c.state == State.LISTENING)
        orch.handle_command(text, "voice")
        assert _wait(lambda: c.state in (State.AWAKE, State.SLEEPING, State.CONFIRMING))

    yield dict(c=c, say=say, speech=speech, logbook=logbook, groq=groq, launched=launched, chrome=chrome_runs, root=root,
               debug=tmp_path / "debug.log")
    c.shutdown()


def _last_lines(rig) -> List[str]:
    return format_record(rig["logbook"].last_completed()).lines


# -- the command is found anywhere in the sentence ---------------------------------


def test_annoyed_then_open_notepad_runs_locally(laptop) -> None:
    """Annoyed then open notepad runs locally."""
    laptop["say"]("This is so annoying, nothing is working, open notepad")
    assert laptop["groq"].questions == []  # never online
    assert laptop["launched"] == ["notepad.exe"]
    spoken = laptop["speech"].texts[-1]
    assert spoken.startswith("Opened notepad.")
    # F14 still notices -- and asks after the request is done.
    assert spoken.endswith(SETTINGS["mood"]["check_in_pool"][0])
    assert any(line.startswith("noticed: you sound frustrated") for line in _last_lines(laptop))


def test_insults_then_open_chat_gpt_in_chrome(laptop) -> None:
    """Insults then open chat gpt in Chrome."""
    laptop["say"]("You idiot AI. DUM. USELESS. Can you please open chat GPT in Chrome")
    assert laptop["groq"].questions == []
    assert laptop["chrome"] == [[r"C:\Chrome\chrome.exe", "https://chatgpt.com"]]
    spoken = laptop["speech"].texts[-1]
    assert spoken.startswith(SETTINGS["rudeness"]["reply_text"])  # the rude reply, then the command runs
    assert "Opening ChatGPT in Chrome." in spoken  # The name, never the URL
    assert "noted: rude wording" in _last_lines(laptop)


def test_are_you_dumb_open_chad_gpt_in_chrome(laptop) -> None:
    """Are you dumb open chad gpt in Chrome."""
    laptop["say"]("Are you dumb? Open Chad GPT in Chrome")
    assert laptop["groq"].questions == []
    assert laptop["chrome"] == [[r"C:\Chrome\chrome.exe", "https://chatgpt.com"]]


def test_bare_vs_code_opens_it(laptop) -> None:
    """Bare VS code opens it."""
    laptop["say"]("VS Code")
    assert laptop["groq"].questions == []
    assert laptop["launched"] == [r"C:\Start\Visual Studio Code.lnk"]


def test_launch_virtual_studio_opens_visual_studio_code(laptop) -> None:
    """Launch virtual studio opens visual studio code."""
    laptop["say"]("Launch Virtual Studio")
    assert laptop["groq"].questions == []
    assert laptop["launched"] == [r"C:\Start\Visual Studio Code.lnk"]


def test_gota_slide_goes_to_sleep(laptop) -> None:
    """Gota slide goes to sleep."""
    laptop["say"]("GOTA SLIDE")
    assert laptop["groq"].questions == []
    assert laptop["c"].state == State.SLEEPING and laptop["speech"].texts[-1] == "Goodnight."


@pytest.mark.parametrize("said", ["gota slide", "go to slide", "gotta sleep", "GOTA SLIDE."])
def test_go_to_sleep_mishearings(said) -> None:
    """Go to sleep mishearings."""
    assert isinstance(SleepMatcher()(said), SleepRequest)


def test_open_chat_chip_it_in_chrome(laptop) -> None:
    """Open chat chip it in Chrome."""
    laptop["say"]("open chat chip it in Chrome")
    assert laptop["chrome"] == [[r"C:\Chrome\chrome.exe", "https://chatgpt.com"]]
    assert "I don't know a website" not in laptop["speech"].texts[-1]


@pytest.mark.parametrize("said, command", [
    ("This is so annoying, nothing is working, open notepad", "open notepad"),
    ("You idiot AI. DUM. USELESS. Can you please open chat GPT in Chrome", "open chat gpt in chrome"),
    ("Are you dumb? Open Chad GPT in Chrome", "open chad gpt in chrome"),
    ("Launch Virtual Studio", "open virtual studio"),
    ("I want you to pull up spotify for me", "open spotify for me"),
    ("Jarvis, bring up chrome", "open chrome"),
    ("how do I open chrome", None),
    ("I can't open it", None),
    ("find out who won the match", None),
    ("the store opens at 9", None),
])
def test_command_extraction(said, command) -> None:
    """Command extraction."""
    assert extract_command(said) == command


def test_questions_about_opening_still_go_online(laptop) -> None:
    """Questions about opening still go online."""
    laptop["say"]("how do I open chrome")
    assert laptop["groq"].questions == ["how do i open chrome"] and laptop["launched"] == []


# -- false claims ------------------------------------------------------------------


CHECK = ClaimCheck(SETTINGS["claims"])


# An "open" suggestion comes only from a known app or site
# named in HIS words, never from the model's own claim.
@pytest.mark.parametrize("answer, question, reply", [
    ("I'm ready to open Notepad", "notepad keeps freezing", "I can't do that from here. Try saying 'open notepad'."),
    ("Sure, I can open Visual Studio Code", "my vs code is slow", "I can't do that from here. Try saying 'open vs code'."),
    ("I'll launch Spotify for you.", "play something on spotify", "I can't do that from here. Try saying 'open spotify'."),
    ("I can open YouTube in Chrome.", "q", "I can't do that from here."),
    ("I'll launch Spotify for you.", "q", "I can't do that from here."),
    ("Sure, I can close that.", "q", "I can't do that yet."),
    ("I'm ready to go to sleep.", "q", "I can't do that from here. Try saying 'go to sleep'."),
])
def test_willing_claims_are_caught(answer, question, reply) -> None:
    """Willing claims are caught."""
    claim = CHECK.find(answer, question)
    assert claim is not None and claim.replacement == reply


@pytest.mark.parametrize("answer", ["Sure, I can help with that!", "I can explain how sleep works.", "You can open Notepad from Start.",
                                    "I can't open apps from here."])
def test_ordinary_answers_pass(answer) -> None:
    """Ordinary answers pass."""
    assert CHECK.find(answer, "q") is None


def test_an_online_claim_without_a_local_command_is_replaced(laptop) -> None:
    """An online claim without a local command is replaced."""
    laptop["groq"].reply = "I'm ready to open Notepad"  # the reply
    laptop["say"]("notepad keeps freezing, any ideas")  # no command in it: it goes online
    assert len(laptop["groq"].questions) == 1
    assert laptop["speech"].texts[-1] == "I can't do that from here. Try saying 'open notepad'."
    assert laptop["launched"] == []


# -- picks from a pending file list -------------------------------------------------


@pytest.mark.parametrize("pick", ["opens second", "and the second file please", "the second file", "second one please",
                                  "number two", "2", "open second"])
def test_every_way_of_saying_the_second_file(laptop, pick) -> None:
    """Every way of saying the second file."""
    laptop["say"]("Find Alex Passport")
    assert laptop["speech"].texts[-1] == "I found 4 files. Which one would you like to open?"  # The count only
    second_line = next(s for s in laptop["logbook"].last_completed().progress if s.startswith("2. "))
    laptop["say"](pick)
    assert laptop["groq"].questions == []
    assert len(laptop["launched"]) == 1
    assert os.path.basename(laptop["launched"][0]) in second_line


def test_open_by_file_name_picks_from_the_list(laptop) -> None:
    """Open by file name picks from the list."""
    laptop["say"]("Find passport")
    laptop["say"]("Open Alex Taylor Passport")
    assert laptop["groq"].questions == []
    # Two files have that name (with and without a space before .pdf): she asks for the number.
    assert laptop["launched"] == [] and "Say its number" in laptop["speech"].texts[-1]


def test_open_alex_passport_after_the_list_is_answered_from_the_list(laptop) -> None:
    """Open alex passport after the list is answered from the list."""
    laptop["say"]("Find Alex Passport")
    laptop["say"]("Open Alex Passport")  # the exact line, which went online
    assert laptop["groq"].questions == []
    # Two files on the list are called exactly that (Downloads and Archive), so she asks which.
    assert laptop["launched"] == [] and "Say its number, from 1 to 4" in laptop["speech"].texts[-1]
    laptop["say"]("the first one")
    assert len(laptop["launched"]) == 1


def test_a_unique_part_of_a_name_picks_it() -> None:
    """A unique part of a name picks it."""
    from jarvis.actions.command_text import name_pick

    names = ["Alex Passport .pdf", "Visa scan.pdf", "Alex Taylor Passport.pdf"]
    assert name_pick("visa", names) == (1, False)
    assert name_pick("taylor passport", names) == (2, False)
    assert name_pick("alex passport", names) == (0, False)  # fewest extra words wins
    assert name_pick("driving licence", names) == (None, False)


def test_a_pick_that_matches_nothing_is_answered_locally(laptop) -> None:
    """A pick that matches nothing is answered locally."""
    laptop["say"]("Find Alex Passport")
    laptop["say"]("the ninth one")
    assert laptop["groq"].questions == [] and laptop["launched"] == []
    assert "only 4 matches" in laptop["speech"].texts[-1]
    laptop["say"]("the second one")  # the list is still there
    assert len(laptop["launched"]) == 1


def test_an_unrelated_command_lets_the_list_expire(laptop) -> None:
    """An unrelated command lets the list expire."""
    laptop["say"]("Find Alex Passport")
    laptop["say"]("what time is it")
    assert laptop["speech"].texts[-1].startswith("It's ")
    laptop["say"]("open chrome")
    assert laptop["launched"] == [r"C:\Start\Google Chrome.lnk"]


# -- no file paths, ever -------------------------------------------------------------


def test_the_list_shows_names_only_and_numbers_twins(laptop) -> None:
    """The list shows names only and numbers twins."""
    # No folder names either; twins get (1), (2).
    laptop["say"]("Find Alex Passport")
    root = str(laptop["root"])
    lines = _last_lines(laptop)
    assert any(line.endswith("Alex Passport .pdf (1)") for line in lines)
    assert any(line.endswith("Alex Passport .pdf (2)") for line in lines)
    shown = "\n".join(lines) + format_session_log(laptop["logbook"].records())
    assert root not in shown and ":\\" not in shown and "Downloads" not in shown and "Archive" not in shown


def test_opening_says_only_the_name_and_the_record_keeps_the_path(laptop) -> None:
    """Opening says only the name and the record keeps the path."""
    laptop["say"]("Find Alex Passport")
    laptop["say"]("open number two")
    assert laptop["speech"].texts[-1] == "Opened Alex Passport .pdf."
    record = laptop["logbook"].last_completed()
    assert record.understood["params"]["path"].startswith(str(laptop["root"]))  # needed to open it; never shown
    assert str(laptop["root"]) not in "\n".join(format_record(record).lines)
    laptop["say"]("why did you do that")
    assert str(laptop["root"]) not in laptop["speech"].texts[-1]


def test_listing_lines_number_twins_and_never_name_a_folder_or_drive() -> None:
    """Listing lines number twins and never name a folder or drive."""
    m = [FileMatch("a.pdf", r"C:\Users\user\Documents\a.pdf", 1.0), FileMatch("a.pdf", r"E:\Files\a.pdf", 1.0),
         FileMatch("b.pdf", r"C:\x\b.pdf", 1.0)]
    assert listing_lines(m) == ["1. a.pdf (1)", "2. a.pdf (2)", "3. b.pdf"]


def test_glued_openchat_gpt_from_the_whisper_benchmark(laptop) -> None:
    """Glued openchat gpt from the Whisper benchmark."""
    # base.en heard "Open ChatGPT in Chrome" as "OpenChat GPT in Chrome" once (benchmark).
    laptop["say"]("OpenChat GPT in Chrome")
    assert laptop["chrome"] == [[r"C:\Chrome\chrome.exe", "https://chatgpt.com"]] and laptop["groq"].questions == []


def test_speech_to_text_uses_base_en() -> None:
    """Speech to text uses base en."""
    from jarvis.voice.stt import transcribe_options

    assert SETTINGS["models"]["whisper_size"] == "base.en" and transcribe_options()["without_timestamps"] is True
