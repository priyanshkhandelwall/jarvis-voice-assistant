"""Real voice commands. Every bug
and change, one or more tests each, with the EXACT transcripts. Commands
go through the production matcher order (app.register_matchers) and the
real orchestrator. Faked: the apps on the laptop and their launching, the
screen and the camera (synthetic pixels and windows), the speech engine's
audio device, and Groq (a spy: anything that reaches it is recorded)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import List

import numpy as np
import pytest

from jarvis.actions import app_finder, find_file, open_app
from jarvis.actions.app_finder import ScoredMatch
from jarvis.actions.command_text import BOTH, CANCEL, CONFIRM, confirm_intent, split_glued
from jarvis.actions.file_index import FileIndex
from jarvis.actions.find_file import FileFinder
from jarvis.actions.sleep_wake import SleepMatcher
from jarvis.browser import commands as browser_commands
from jarvis.browser.commands import BrowserCommands
from jarvis.controller import Controller, Event, EventType, State
from jarvis.controller.trust import TRUST_TABLE, TrustLevel
from jarvis.dashboard.activity_feed import format_record
from jarvis.dashboard.app import register_matchers
from jarvis.interpreter import ActionRequest, Interpreter
from jarvis.logbook import Logbook
from jarvis.logbook.debug_log import DebugLog
from jarvis.memory.commands import MemoryCommands
from jarvis.memory.sensitive import sensitive_type
from jarvis.memory.store import MemoryStore
from jarvis.orchestrator import Orchestrator
from jarvis.screen.capture import ScreenCapture, WindowInfo, is_system_window
from jarvis.screen.commands import ScreenCommands
from jarvis.screen.ocr import OcrLine, OcrWord
from jarvis.security import paths
from jarvis.sensors import SensorFlags
from jarvis.settings import SETTINGS
from jarvis.speech import SpeechOutput
from jarvis.speech.claims import ClaimCheck
from jarvis.speech.qa import AnswerResult, build_system_prompt
from jarvis.vision import commands as vision_mod
from jarvis.vision.camera import Camera
from jarvis.vision.commands import VisionCommands
from jarvis.vision.detector import Detection

PROJECT = Path(__file__).resolve().parent.parent
CHROME = r"C:\Chrome\chrome.exe"
TODAY = date(2026, 10, 27)


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
    """Stands in for Groq; records what would have gone online."""

    model = "openai/gpt-oss-20b"

    def __init__(self) -> None:
        self.questions: List[str] = []
        self.length_rules: List[str] = []
        self.reply = "an online answer"
        self.screen_calls: List[dict] = []

    def today(self):
        """Fake today for the tests."""
        return TODAY

    def answer(self, question, on_going_online=None, length_rule=None):
        """Fake answer for the tests."""
        self.questions.append(question)
        self.length_rules.append(length_rule)
        if on_going_online is not None:
            on_going_online()
        return AnswerResult(self.reply, went_online=True)

    def answer_live(self, question, on_going_online=None, should_stop=None, on_waiting=None):
        """Fake answer live for the tests."""
        return self.answer(question, on_going_online)

    def answer_screen(self, question, screen_text, jpeg, model, instructions, on_going_online=None):
        """Fake answer screen for the tests."""
        if on_going_online is not None:
            on_going_online()
        self.screen_calls.append(dict(question=question, text=screen_text))
        return AnswerResult("That is a YouTube video page and a report about sales.", went_online=True)


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
    ScoredMatch("notepad", 0.0, "app", "notepad.exe"),
    ScoredMatch("OneDrive", 0.0, "app", r"C:\Start\OneDrive.lnk"),
    ScoredMatch("OneNote", 0.0, "app", r"C:\Start\OneNote.lnk"),
]
# The four passport PDFs of the list (two share a name).
_PASSPORTS = [
    ("Downloads", "Alex Passport .pdf"),
    ("Desktop/Archive", "Alex Passport .pdf"),
    ("Downloads", "Alex Taylor Passport .pdf"),
    ("Desktop/Scans", "Alex Taylor Passport.pdf"),
]

# -- a synthetic desktop for F8: Chrome (YouTube), Word, the desktop, the taskbar,
# JARVIS's own (untitled) window, and a minimised Notepad. Text lines sit in
# the window they belong to.
CHROME_WIN = WindowInfo("(22) YouTube - Google Chrome", "chrome.exe", (0, 0, 800, 860))
WORD_WIN = WindowInfo("Report.docx - Word", "WINWORD.EXE", (800, 0, 1600, 860))
JARVIS_WIN = WindowInfo("", "flet.exe", (1200, 600, 1600, 860), system=True)
TASKBAR = WindowInfo("", "explorer.exe", (0, 860, 1600, 900), system=True)
DESKTOP = WindowInfo("Program Manager", "explorer.exe", (0, 0, 1600, 900), system=True)
NOTEPAD_MIN = WindowInfo("notes.txt - Notepad", "notepad.exe", (-32000, -32000, -31840, -31972), minimised=True)


def _line(text, x, y):
    return OcrLine(text, (OcrWord(text.split()[0], x, y, 40, 12),))


SCREEN_LINES = [
    _line("YouTube Home Shorts Subscriptions", 20, 40),
    _line("Quarterly sales report", 820, 40),
    _line("Sales rose 12 percent in September.", 820, 70),
    _line("JARVIS LIVE ACTIVITY heard: what's on my screen", 1220, 620),  # JARVIS's own window: never read
    _line("10:42 PM", 1500, 870),  # the taskbar: never read
]


class FakeScreen:
    """Screen stand-in: a fixed desktop image, windows and OCR lines."""
    def __init__(self) -> None:
        self.windows = [JARVIS_WIN, CHROME_WIN, WORD_WIN, NOTEPAD_MIN, TASKBAR, DESKTOP]

    def capture(self):
        """Fake capture for the tests."""
        from PIL import Image

        return ScreenCapture(image=Image.new("RGB", (1600, 900), (255, 255, 255)), windows=list(self.windows), origin=(0, 0))

    def ocr(self, image):
        """Fake ocr for the tests."""
        return list(SCREEN_LINES)


# -- a fake webcam for F9 ------------------------------------------------------------------


class FakeCapture:
    """Camera capture stand-in returning prepared frames."""
    def read(self):
        """Fake read for the tests."""
        return True, np.full((480, 640, 3), 120, dtype=np.uint8)

    def release(self) -> None:
        """Fake release for the tests."""
        pass


class FakeDetector:
    """Detector stand-in returning canned detections; counts its calls."""
    def __init__(self) -> None:
        # 's first look: a person in the middle, the fridge on the left, a bottle on the right.
        self.found = [Detection("person", 0.89, (250, 50, 400, 470)), Detection("refrigerator", 0.85, (10, 0, 160, 470)),
                      Detection("bottle", 0.80, (520, 300, 560, 400))]
        # The fridge and bottle scored 0.55/0.50 in that log -- now
        # below [camera] min_confidence, so raised here to keep testing follow-ups.)

    def detect(self, frame):
        """Fake detect for the tests."""
        return list(self.found)

    def unload_if_idle(self):
        """Fake unload if idle for the tests."""
        return False


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

    monkeypatch.setattr(paths, "APPROVED_FOLDERS", paths.APPROVED_FOLDERS + [tmp_path.resolve()])
    monkeypatch.setattr(app_finder, "enumerate_installed_apps", lambda store_apps=None: list(_APPS))
    monkeypatch.setattr(open_app, "STORE_APPS", NoStoreApps())
    monkeypatch.setattr(open_app, "find_chrome", lambda: CHROME)
    monkeypatch.setattr(browser_commands, "find_chrome", lambda: CHROME)
    monkeypatch.setattr(open_app.os, "startfile", launched.append)
    monkeypatch.setattr(find_file.os, "startfile", launched.append)
    monkeypatch.setattr(open_app.subprocess, "Popen", lambda args, shell=False: chrome_runs.append(args))
    verify = lambda launch, timeout, poll: (launch(), False)  # noqa: E731
    monkeypatch.setattr(open_app, "launch_and_verify_with_retry", verify)
    monkeypatch.setattr(find_file, "launch_and_verify_with_retry", verify)
    monkeypatch.setitem(vision_mod._CFG, "stream_detect_seconds", 0.1)

    c = Controller(start_state=State.AWAKE)
    logbook = Logbook(debug_log=DebugLog(path=tmp_path / "debug.log"))
    c.set_transition_listener(logbook.on_transition)
    c.start()
    interpreter = Interpreter()
    speech = FakeSpeech()
    orch = Orchestrator(c, interpreter, speech, logbook=logbook)
    groq = SpyAnswerer()
    sensors = SensorFlags(min_seconds=0)
    screen_fake = FakeScreen()
    screen = ScreenCommands(groq, sensors, capture_fn=screen_fake.capture, ocr_fn=screen_fake.ocr)
    detector = FakeDetector()
    vision = VisionCommands(Camera(sensors, opener=lambda index: FakeCapture() if index == 0 else None, blocked=lambda: False),
                            detector, status=logbook.record_status, today=lambda: TODAY)
    memory_path = tmp_path / "memory.json"
    register_matchers(interpreter, orch, logbook, FileFinder(index), groq, None, memory=MemoryCommands(MemoryStore(memory_path)),
                      browser=BrowserCommands(), screen=screen, vision=vision)

    def say(text: str) -> str:
        c.submit(Event(EventType.SPEECH_STARTED, {"source": "voice"}))
        assert _wait(lambda: c.state == State.LISTENING)
        orch.handle_command(text, "voice")
        assert _wait(lambda: c.state in (State.AWAKE, State.SLEEPING, State.CONFIRMING))
        return speech.texts[-1]

    def confirming(text: str) -> str:
        """A sentence heard while she waits for confirm/cancel (the listener
        hands it over without LISTENING, exactly as in conversation mode)."""
        assert c.state == State.CONFIRMING
        orch.handle_command(text, "voice")
        assert _wait(lambda: c.state in (State.AWAKE, State.CONFIRMING))
        _wait(lambda: c.state == State.AWAKE, timeout=1.0)
        return speech.texts[-1]

    yield dict(c=c, say=say, confirming=confirming, speech=speech, logbook=logbook, groq=groq, launched=launched,
               chrome=chrome_runs, root=root, debug=tmp_path / "debug.log", memory=memory_path, vision=vision,
               detector=detector, orch=orch)
    vision.stop_stream()
    c.shutdown()


def _lines(rig) -> List[str]:
    return format_record(rig["logbook"].last_completed()).lines


# == never store a card number (and friends) =============================================


def test_the_27_oct_card_number_is_refused_and_written_nowhere(laptop) -> None:
    """The 27 oct card number is refused and written nowhere."""
    reply = laptop["say"]("Can you please remember my card number 411 111 111 111")
    assert reply == "I won't store that. It looks like a card number."
    assert not laptop["memory"].exists()  # nothing stored
    shown = " ".join(_lines(laptop))
    assert "411" not in shown and "411" not in repr(laptop["logbook"].records())
    debug = laptop["debug"].read_text(encoding="utf-8") if laptop["debug"].exists() else ""
    assert "411" not in debug
    assert laptop["groq"].questions == []


@pytest.mark.parametrize("said, kind", [
    ("my card number 411 111 111 111", "card number"),  # fails Luhn: refused anyway
    ("my credit card is 1234", "card number"),
    ("my debit card ends in 4242", "card number"),
    ("my account number is 12 34", "bank account number"),
    ("my bsb is 062-000", "bank account number"),
    ("my pin is 4821", "PIN"),
    ("my pin is four three two one", "PIN"),
    ("my password is hunter2", "password"),
    ("my password is fluffy", "password"),  # a value with no digits
    ("my passcode is 0000", "password"),
    ("my cvv is 123", "PIN"),
    ("my passport number is PA12", "passport number"),
    ("my licence number is 7", "driver's licence number"),
    ("my license number is 7", "driver's licence number"),
    ("my tax file number is 123", "tax file number"),
    ("my tfn is 123", "tax file number"),
    ("my medicare number is 2", "Medicare number"),
])
def test_a_sensitive_word_with_any_digits_or_value_is_refused(said, kind) -> None:
    """A sensitive word with any digits or value is refused."""
    assert sensitive_type(said) == kind


@pytest.mark.parametrize("said", ["to change my password", "to renew my passport", "my exam is on tuesday",
                                  "the parcel code 1234 5678 1234 5678", "find my passport", "to pin the poster"])
def test_the_word_alone_or_a_plain_number_is_allowed(said) -> None:
    """The word alone or a plain number is allowed."""
    assert sensitive_type(said) is None


def test_remember_to_change_my_password_is_stored(laptop) -> None:
    """Remember to change my password is stored."""
    assert laptop["say"]("remember to change my password") == "Got it."
    stored = json.loads(laptop["memory"].read_text(encoding="utf-8"))["items"]
    assert [item["text"] for item in stored] == ["to change my password"]


def test_a_bare_luhn_card_number_is_still_refused() -> None:
    """A bare luhn card number is still refused."""
    assert sensitive_type("remember 4111 1111 1111 1111") == "card number"


def test_the_stored_card_number_is_gone_from_the_real_memory_files() -> None:
    """The stored card number is gone from the real memory files."""
    data = PROJECT / "jarvis_data"
    files = [data / "memory.json", data / "memory.json.bak"]
    if not any(f.exists() for f in files):
        pytest.skip("no jarvis_data on this machine")
    for f in files:
        if f.exists():
            text = f.read_text(encoding="utf-8")
            assert "411 111" not in text and "card number" not in text, f
    log = data / "logs" / "debug.log"
    if log.exists():
        assert "411 111" not in log.read_text(encoding="utf-8", errors="replace")


# == a pending list is picked from first ====================================================


@pytest.mark.parametrize("pick, index", [("Open number one please", 0), ("Open file number 1", 0), ("Open One", 0),
                                         ("open the first one", 0), ("number 2", 1), ("second", 1)])
def test_every_27_oct_pick_opens_from_the_file_list(laptop, pick, index) -> None:
    """Every 27 oct pick opens from the file list."""
    laptop["say"]("Find my passport")
    listed = [s for s in laptop["logbook"].last_completed().progress if s[:2] in ("1.", "2.", "3.", "4.")]
    laptop["say"](pick)
    assert laptop["groq"].questions == []
    assert len(laptop["launched"]) == 1
    assert os.path.basename(laptop["launched"][0]) in listed[index]
    assert "OneDrive" not in laptop["speech"].texts[-1] and "OneNote" not in laptop["speech"].texts[-1]


def test_the_27_oct_sequence_one_after_another(laptop) -> None:
    """The 27 oct sequence one after another."""
    # Find, then "Open number one please" (went to app search), then a new
    # find and "Open One" (the OneDrive/OneNote picker): both pick a file now.
    laptop["say"]("Find my passport")
    laptop["say"]("Open number one please")
    laptop["say"]("and find my passport please")
    laptop["say"]("Open One")
    assert len(laptop["launched"]) == 2 and all(p.endswith(".pdf") for p in laptop["launched"])


def test_a_list_survives_an_unrelated_command(laptop) -> None:
    """A list survives an unrelated command."""
    laptop["say"]("Find my passport")
    assert laptop["say"]("what time is it").startswith("It's ")
    laptop["say"]("Open file number 1")
    assert len(laptop["launched"]) == 1 and laptop["launched"][0].endswith(".pdf")


def test_file_in_the_words_picks_from_the_file_list_even_after_an_app_list(laptop) -> None:
    """File in the words picks from the file list even after an app list."""
    laptop["say"]("Find my passport")
    files = [s for s in laptop["logbook"].last_completed().progress if s.startswith("2. ")]
    laptop["orch"].picks.claim(object())  # a newer list (as if "which app?" was shown since)
    laptop["say"]("Open file number 2")
    assert len(laptop["launched"]) == 1 and os.path.basename(laptop["launched"][0]) in files[0]
    assert "OneNote" not in laptop["launched"][0]


def test_open_file_number_2_never_picks_from_an_app_list(laptop) -> None:
    """Open file number 2 never picks from an app list."""
    picker = open_app.AppPicker(laptop["orch"].picks)
    picker.remember([_APPS[3], _APPS[4]])  # OneDrive, OneNote
    assert picker.selection_matcher("open file number 2") is None


@pytest.mark.parametrize("pick", ["Open number one please", "Open file number 1", "Open One"])
def test_a_pick_with_no_list_is_answered_locally_never_an_app_search(laptop, pick) -> None:
    """A pick with no list is answered locally never an app search."""
    reply = laptop["say"](pick)
    assert reply == SETTINGS["interpreter"]["no_list_text"]
    assert laptop["launched"] == [] and laptop["groq"].questions == []
    assert not any(line.startswith("searching for") for line in _lines(laptop))


def test_a_list_expires_after_pick_valid_seconds(laptop) -> None:
    """A list expires after pick valid seconds."""
    laptop["say"]("Find my passport")
    slot = laptop["orch"].picks
    slot._clock = lambda: time.monotonic() + slot.valid_seconds + 1  # two minutes later
    assert laptop["say"]("open number one") == SETTINGS["interpreter"]["no_list_text"]
    assert laptop["launched"] == []


# == every result is spoken AND shown as "saying:" ========================================


@pytest.mark.parametrize("action", sorted(a for a, level in TRUST_TABLE.items() if level in (TrustLevel.L0, TrustLevel.L1)))
def test_every_immediate_action_speaks_its_result_and_shows_saying(action) -> None:
    """Every immediate action speaks its result and shows saying."""
    c = Controller(start_state=State.AWAKE)
    logbook = Logbook()
    c.set_transition_listener(logbook.on_transition)
    c.start()
    interpreter, speech = Interpreter(), FakeSpeech()
    orch = Orchestrator(c, interpreter, speech, logbook=logbook)
    result = f"result of {action}."
    orch.register_executor(action, lambda name, params: (True, result, "logged: no laptop data"))
    interpreter.register("only", lambda text: ActionRequest(action, {"display_name": "x"}))
    try:
        orch.handle_typed_command("anything")
        assert _wait(lambda: c.state == State.AWAKE)
        assert speech.texts[-1] == result  # spoken
        record = logbook.last_completed()
        assert f"saying: {result}" in format_record(record).lines  # and shown
        assert f"saying: {result}" in record.session_only  # laptop data: never written to debug.log
    finally:
        c.shutdown()


def test_the_search_from_the_log_is_spoken(laptop) -> None:
    """The search from the log is spoken."""
    reply = laptop["say"]("Open Chrome and search ChatGPT")
    assert reply == "Searching Google for chatgpt in Chrome."
    assert "saying: Searching Google for chatgpt in Chrome." in _lines(laptop)


def test_what_do_you_see_is_spoken_and_shown(laptop) -> None:
    """What do you see is spoken and shown."""
    reply = laptop["say"]("What do you see")
    assert reply == "I can see a person, a refrigerator and a bottle."
    lines = _lines(laptop)
    assert "saying: I can see a person, a refrigerator and a bottle." in lines
    assert "running capture camera" not in lines
    assert "detected: person 0.89, refrigerator 0.85, bottle 0.80" in lines


def test_whats_on_my_screen_is_spoken_and_shown(laptop) -> None:
    """Whats on my screen is spoken and shown."""
    reply = laptop["say"]("What's on my screen")
    assert reply.startswith("You're looking at Chrome showing YouTube")
    assert any(line.startswith("saying: You're looking at Chrome showing YouTube") for line in _lines(laptop))
    assert "running capture screen" not in _lines(laptop)


def test_the_service_error_cause_is_kept_in_the_record_only(laptop, monkeypatch) -> None:
    """The service error cause is kept in the record only."""
    from jarvis.online import OnlineErrorKind

    laptop["groq"].answer_live = lambda q, on_going_online=None, should_stop=None, on_waiting=None: AnswerResult(
        "x", went_online=True, error=OnlineErrorKind.SERVICE_ERROR, error_detail="HTTP 400 tool_use_failed: Failed to call a function")
    laptop["say"]("Tell me the score of India vs New Zealand")
    record = laptop["logbook"].last_completed()
    lines = format_record(record).lines
    assert "online search: service error (the model answered without running the web search)" in lines  # Plain cause shown
    assert "tool_use_failed" not in " ".join(lines)  # R3: not in Live Activity
    assert "HTTP 400 tool_use_failed" in record.online_destination  # the real cause, record / debug.log only


# == every window on the screen, never the desktop =========================================


def test_whats_on_my_screen_reads_every_window_and_never_program_manager(laptop) -> None:
    """Whats on my screen reads every window and never program manager."""
    reply = laptop["say"]("What's on my screen")
    # Only the window in front -- never the minimised
    # Notepad or the Word document beside it.
    assert reply == "You're looking at Chrome showing YouTube. I couldn't read any text on it."
    assert "Notepad" not in reply and "minimised" not in reply
    assert "Program Manager" not in reply and "JARVIS LIVE ACTIVITY" not in reply and "10:42" not in reply
    shown = " ".join(_lines(laptop))
    assert "Program Manager" not in shown
    assert "read text from: YouTube open in Chrome and a Word document called Report" in shown
    assert laptop["groq"].questions == []  # local only


@pytest.mark.parametrize("title, cls, process, system", [
    ("Program Manager", "Progman", "explorer.exe", True),
    ("", "Shell_TrayWnd", "explorer.exe", True),
    ("", "FLUTTER_RUNNER_WIN32_WINDOW", "flet.exe", True),
    ("Report.docx - Word", "OpusApp", "WINWORD.EXE", False),
    ("notes.txt - Notepad", "Notepad", "notepad.exe", False),
])
def test_system_windows_are_never_read(title, cls, process, system) -> None:
    """System Windows are never read."""
    assert is_system_window(title, cls, process) is system


def test_a_protected_window_is_still_blacked_out_and_never_named(laptop) -> None:
    """A protected window is still blacked out and never named."""
    from jarvis.screen.capture import black_out
    from PIL import Image

    bank = WindowInfo("CommBank NetBank - Google Chrome", "chrome.exe", (0, 0, 100, 100))
    image = Image.new("RGB", (200, 200), (255, 255, 255))
    assert black_out(image, [bank], (0, 0)) == 1 and image.getpixel((50, 50)) == (0, 0, 0)
    assert not bank.readable


# == sleep anywhere in the sentence =========================================================


@pytest.mark.parametrize("said", ["Thank you, go to sleep", "Hijar, VS Code to Sleep", "ok jarvis go to sleep now",
                                  "Thank you, good night", "alright, go back to sleep"])
def test_the_27_oct_sleep_lines_sleep_locally(laptop, said) -> None:
    """The 27 oct sleep lines sleep locally."""
    assert laptop["say"](said) == "Goodnight."
    assert laptop["c"].state == State.SLEEPING and laptop["groq"].questions == []


@pytest.mark.parametrize("said", ["how much should I sleep", "what is sleep mode", "how much sleep do I need", "I need to sleep",
                                  "remind me to go to sleep at 10"])
def test_questions_about_sleep_are_not_sleep(said) -> None:
    """Questions about sleep are not sleep."""
    assert SleepMatcher()(said) is None


# == confirm or cancel inside a sentence =====================================================


def test_please_send_it_online_confirm_sends_it(laptop) -> None:
    """Please send it online confirm sends it."""
    laptop["say"]("Explain my screen")
    assert laptop["c"].state == State.CONFIRMING
    reply = laptop["confirming"]("Please send it online. Confirm.")
    assert laptop["groq"].screen_calls  # sent (after the preview, L3)
    assert reply == "That is a YouTube video page and a report about sales."
    assert any(line.startswith("saying: That is a YouTube") for line in _lines(laptop))  # spoken and shown


def test_cancel_words_in_a_sentence_cancel(laptop) -> None:
    """Cancel words in a sentence cancel."""
    laptop["say"]("Explain my screen")
    assert laptop["confirming"]("no, don't send it") == SETTINGS["conversation"]["cancelled_reply"]
    assert laptop["groq"].screen_calls == []


def test_both_kinds_of_word_ask_again(laptop) -> None:
    """Both kinds of word ask again."""
    laptop["say"]("Explain my screen")
    laptop["orch"].handle_command("Yes. No, cancel.", "voice")
    assert laptop["speech"].texts[-1] == SETTINGS["conversation"]["confirm_or_cancel_reply"]
    assert laptop["c"].state == State.CONFIRMING and laptop["groq"].screen_calls == []


@pytest.mark.parametrize("said, intent", [("Please send it online. Confirm.", CONFIRM), ("yes", CONFIRM), ("go ahead", CONFIRM),
                                          ("send it", CONFIRM), ("cancel", CANCEL), ("no", CANCEL), ("stop", CANCEL),
                                          ("yes, no, cancel", BOTH), ("open chrome", None), ("send it to my mum", None)])
def test_confirm_intent(said, intent) -> None:
    """Confirm intent."""
    assert confirm_intent(said) == intent


def test_the_listener_hands_a_confirm_sentence_over() -> None:
    """The listener hands a confirm sentence over."""
    from jarvis.voice.listener import MODE_CONFIRMING, VoiceListener, _Recording

    c = Controller(start_state=State.AWAKE)
    handed, ignored = [], []
    listener = VoiceListener(c, lambda text, source: handed.append(text), ignored=lambda text, why: ignored.append(text))
    listener._dispatch(_Recording(MODE_CONFIRMING, "voice", []), "Please send it online. Confirm.", 0.0)
    listener._dispatch(_Recording(MODE_CONFIRMING, "voice", []), "what's the weather", 0.0)
    assert handed == ["Please send it online. Confirm."] and ignored == ["what's the weather"]


def test_a_spoken_yes_with_nothing_pending_is_not_a_confirm(laptop) -> None:
    """A spoken yes with nothing pending is not a confirm."""
    laptop["say"]("yes")
    assert "Nothing to confirm" not in laptop["speech"].texts[-1]


# == "why" variants are answered from the record ==============================================


@pytest.mark.parametrize("why", ["Why do you do that", "why'd you do that", "Why’d you do that?", "why did you open that",
                                 "how come you did that"])
def test_why_variants_answer_locally(laptop, why) -> None:
    """Why variants answer locally."""
    laptop["say"]("Open Notepad")
    reply = laptop["say"](why)
    assert laptop["groq"].questions == []
    assert "notepad" in reply.lower()


# == command text clean-up =========================================================================


def test_opennote_pad_from_the_log_opens_notepad(laptop) -> None:
    """Opennote pad from the log opens notepad."""
    laptop["say"]("This is her annoying Nothing Works, OpenNote Pad")
    assert laptop["groq"].questions == []
    assert laptop["launched"] == ["notepad.exe"]


@pytest.mark.parametrize("said, out", [("opennotepad", "open notepad"), ("OpenNote Pad", "open notepad"),
                                       ("OpenChat GPT in Chrome", "open chatgpt in Chrome"), ("opener of the box", "opener of the box")])
def test_glued_words_are_split_against_known_names(said, out) -> None:
    """Glued words are split against known names."""
    assert split_glued(said) == out


def test_your_youtube_in_chrome_opens_youtube(laptop) -> None:
    """Your YouTube in Chrome opens YouTube."""
    reply = laptop["say"]("Are you dumb? Please open your YouTube in Chrome")
    assert laptop["chrome"] == [[CHROME, "https://www.youtube.com"]]
    assert "Opening YouTube in Chrome." in reply and "I don't know a website" not in reply


def test_the_claim_filter_suggests_only_what_he_named() -> None:
    """The claim filter suggests only what he named."""
    check = ClaimCheck(SETTINGS["claims"])
    claim = check.find("I can open OneNote", "this is her annoying nothing works, opennote pad")
    assert claim.replacement == "I can't do that from here. Try saying 'open notepad'."
    assert check.find("I can open OneNote", "this is annoying").replacement == "I can't do that from here."


# == the camera ===================================================================================


def test_turn_on_the_camera_shows_the_live_feed(laptop) -> None:
    """Turn on the camera shows the live feed."""
    laptop["say"]("Can you please turn on camera")  # This went online
    assert laptop["groq"].questions == [] and laptop["c"].state == State.CONFIRMING
    laptop["confirming"]("confirm")
    stream = laptop["vision"].stream
    assert stream is not None and stream.running
    assert _wait(lambda: stream.latest_jpeg() is not None)  # what the Webcam Feed panel shows
    assert stream.latest_jpeg()[:2] == b"\xff\xd8"  # a JPEG in memory


def test_frames_are_never_saved(laptop, tmp_path) -> None:
    """Frames are never saved."""
    before = {p for p in PROJECT.rglob("*.jpg")}
    laptop["say"]("turn on the camera")
    laptop["confirming"]("confirm")
    time.sleep(0.3)
    laptop["vision"].stop_stream()
    assert {p for p in PROJECT.rglob("*.jpg")} == before


@pytest.mark.parametrize("said", ["What can you see", "what's in front of you"])
def test_what_can_you_see_looks_through_the_camera(laptop, said) -> None:
    """What can you see looks through the camera."""
    assert laptop["say"](said) == "I can see a person, a refrigerator and a bottle."
    assert laptop["groq"].questions == []


def test_where_is_the_refrigerator_from_the_log(laptop) -> None:
    """Where is the refrigerator from the log."""
    laptop["say"]("What do you see")
    reply = laptop["say"]("Where is the refrigerator? On my right or my left")
    assert reply == "From the camera's view, the refrigerator is on the left."
    assert laptop["groq"].questions == []


def test_how_much_does_the_cellphone_cost_from_the_log(laptop) -> None:
    """How much does the cellphone cost from the log."""
    laptop["detector"].found = [Detection("cell phone", 0.90, (300, 200, 360, 300)), Detection("person", 0.86, (0, 0, 640, 480))]
    laptop["say"]("What do you see")
    reply = laptop["say"]("How much does the cellphone cost")
    assert reply.startswith("That looks like a cell phone. Roughly")
    assert laptop["say"]("how much is the phone").startswith("That looks like a cell phone.")
    assert laptop["groq"].questions == []


def test_follow_ups_last_two_minutes(laptop) -> None:
    """Follow ups last two minutes."""
    laptop["say"]("What do you see")
    laptop["vision"]._clock = lambda: time.monotonic() + 121
    reply = laptop["say"]("Where is the refrigerator? On my right or my left")
    # Too old: she asks for a new look; never online.
    assert reply == SETTINGS["camera"]["replies"]["look_first"]
    assert laptop["groq"].questions == []


def test_the_online_prompt_never_claims_to_see() -> None:
    """The online prompt never claims to see."""
    prompt = build_system_prompt()
    assert "never say you can see" in prompt and "Ask me 'what do you see' and I'll look." in prompt
    claim = ClaimCheck(SETTINGS["claims"]).find("I can view your webcam feed, but I cannot control it.", "what can you see")
    assert claim.replacement == "I can't see anything from here. Ask me 'what do you see' and I'll look."


# == sites by name, never the URL ===================================================================


def test_open_chatgpt_says_the_name(laptop) -> None:
    """Open chatgpt says the name."""
    reply = laptop["say"]("Open ChatGPT")
    assert reply == "Opening ChatGPT in Chrome."
    lines = _lines(laptop)
    assert "opening ChatGPT in Chrome" in lines and not any("http" in line for line in lines)
    assert laptop["logbook"].last_completed().understood["params"]["url"] == "https://chatgpt.com"  # the record keeps it


# == file names only, the count spoken ===============================================================


def test_the_passport_list_says_only_the_count(laptop) -> None:
    """The passport list says only the count."""
    reply = laptop["say"]("Find my passport")
    assert reply == "I found 4 files. Which one would you like to open?"
    lines = _lines(laptop)
    assert "1. Alex Passport .pdf (1)" in lines and "2. Alex Passport .pdf (2)" in lines
    joined = " ".join(lines)
    assert "Downloads" not in joined and "Archive" not in joined and ":\\" not in joined and "/" not in joined


# == short gaps between sentences ======================================================================


def test_sentences_are_trimmed_and_joined_by_a_short_gap() -> None:
    """Sentences are trimmed and joined by a short gap."""
    from jarvis.speech.neural_tts import NeuralSpeechOutput

    rate = 1000
    tone = np.ones(200, dtype=np.float32) * 0.5

    class Synth:
        name = "fake"

        def load(self):
            pass

        def synth(self, sentence):
            quiet = np.zeros(300, dtype=np.float32)  # 0.3 s of the model's own quiet on each side
            return np.concatenate([quiet, tone, quiet]), rate

    played = []
    out = NeuralSpeechOutput(Synth(), player=lambda samples, r, stop: played.append(np.asarray(samples)) or True)
    out.speak("One two three. Four five six. Seven eight nine.")
    assert len(played) == 3
    keep = int(rate * SETTINGS["speech"]["edge_keep_seconds"])
    gap = int(rate * SETTINGS["speech"]["sentence_gap_seconds"])
    assert len(played[0]) == 200 + 2 * keep
    assert len(played[1]) == gap + 200 + 2 * keep and not played[1][:gap].any()
    heard_gap = (keep + gap + keep) / rate
    assert abs(heard_gap - 0.1) < 0.011  # about 0.1 s between the words of one sentence and the next
    out.close()


def test_one_sound_stream_plays_the_whole_line(monkeypatch) -> None:
    """One sound stream plays the whole line."""
    from jarvis.speech.neural_tts import NeuralSpeechOutput, sounddevice_player

    opened = []

    class FakeStream:
        def __init__(self, **kwargs):
            opened.append(kwargs)

        def start(self):
            pass

        def write(self, block):
            pass

        def stop(self):
            pass

        def close(self):
            pass

        def abort(self):
            pass

    fake_sd = type(sys)("sounddevice")
    fake_sd.OutputStream = FakeStream
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)

    class Synth:
        name = "fake"

        def load(self):
            pass

        def synth(self, sentence):
            return np.ones(100, dtype=np.float32) * 0.5, 1000

    out = NeuralSpeechOutput(Synth(), player=sounddevice_player)
    out.speak("One. Two. Three.")
    assert len(opened) == 1  # was one per sentence
    out.close()


# == "in one line" and "in detail" ======================================================================


def test_black_hole_in_one_line_is_one_sentence(laptop) -> None:
    """Black hole in one line is one sentence."""
    laptop["groq"].reply = ("A black hole is a spacetime region from which nothing escapes. It forms when a massive star collapses. "
                            "Its size depends on its mass.")
    reply = laptop["say"]("Can you explain black hole in one line")
    assert reply == "A black hole is a spacetime region from which nothing escapes."
    assert "ONE sentence" in laptop["groq"].length_rules[-1]


@pytest.mark.parametrize("said", ["explain gravity briefly", "what is dna in short", "describe a rainbow in one sentence"])
def test_short_requests_are_one_sentence(laptop, said) -> None:
    """Short requests are one sentence."""
    laptop["groq"].reply = "First. Second. Third."
    assert laptop["say"](said) == "First."


def test_in_detail_allows_up_to_six(laptop) -> None:
    """In detail allows up to six."""
    laptop["groq"].reply = " ".join(f"Sentence {n}." for n in range(1, 10))
    reply = laptop["say"]("explain photosynthesis in detail")
    assert reply == " ".join(f"Sentence {n}." for n in range(1, 7))
    assert "up to 6 sentences" in laptop["groq"].length_rules[-1]


# == startup and the microphone =============================================================================


def test_the_wake_word_model_no_longer_imports_sklearn() -> None:
    """The wake word model no longer imports sklearn."""
    code = ("import sys; sys.path.insert(0, r'%s'); from jarvis.voice.wake_word import openwakeword_model_class; "
            "openwakeword_model_class(); print('sklearn' in sys.modules, 'scipy.stats' in sys.modules)") % PROJECT
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.stdout.strip() == "False False", out.stderr


def test_voice_start_checks_libraries_without_importing_them(monkeypatch) -> None:
    """Voice start checks libraries without importing them."""
    from jarvis.voice import system

    assert system._installed("faster_whisper") and not system._installed("no_such_package_jarvis")


def test_the_numpy_decimator_matches_scipy_exactly() -> None:
    """The numpy decimator matches scipy exactly."""
    scipy_signal = pytest.importorskip("scipy.signal")
    from jarvis.voice.audio_input import Decimator, lowpass_taps

    taps = int(SETTINGS["audio"]["decimation_filter_taps"])
    assert np.allclose(lowpass_taps(taps, 7000.0, 48000), scipy_signal.firwin(taps, 7000.0, fs=48000), atol=1e-12)
    rng = np.random.default_rng(1)
    audio = (rng.standard_normal(3840 * 5) * 3000).astype(np.int16)
    dec = Decimator(3, 48000)
    ours = np.concatenate([dec(audio[i: i + 3840]) for i in range(0, len(audio), 3840)])
    ref = scipy_signal.lfilter(scipy_signal.firwin(taps, 7000.0, fs=48000), 1.0, audio.astype(np.float64))[::3]
    assert np.array_equal(ours, np.clip(np.round(ref), -32768, 32767).astype(np.int16))


def test_the_saved_microphone_is_the_built_in_array() -> None:
    """The saved microphone is the built in array."""
    assert SETTINGS["audio"]["input_device_name"] == "Microphone Array (Realtek(R) Audio)"


class _FakeSD:
    """Two microphones; the headset's driver hangs when opened."""

    def __init__(self, hang_names=()):
        self.hang_names = set(hang_names)
        self.opened = []
        self.default = type("D", (), {"device": [0, 0]})()

    def query_devices(self, index=None):
        """Fake query devices for the tests."""
        devices = [{"name": "Microphone Array (Realtek(R) Audio)", "max_input_channels": 2, "hostapi": 0, "default_samplerate": 48000.0},
                   {"name": "Headset (Contoso Wireless Earbuds)", "max_input_channels": 1, "hostapi": 0, "default_samplerate": 16000.0}]
        return devices if index is None else devices[index]

    def query_hostapis(self, index):
        """Fake query hostapis for the tests."""
        return {"name": "Windows WASAPI"}

    def InputStream(self, samplerate, blocksize, channels, dtype, device, callback):  # noqa: N802
        """Fake inputstream for the tests."""
        name = self.query_devices(device)["name"]
        if name in self.hang_names:
            time.sleep(30)  # a stuck driver
        self.opened.append(name)
        return type("S", (), {"start": lambda s: None, "stop": lambda s: None, "close": lambda s: None})()


def test_a_stuck_device_cant_hold_the_switch_for_minutes(monkeypatch) -> None:
    """A stuck device cant hold the switch for minutes."""
    from jarvis.voice import audio_input

    monkeypatch.setitem(audio_input._AUDIO, "open_timeout_seconds", 0.3)
    sd = _FakeSD(hang_names={"Headset (Contoso Wireless Earbuds)"})
    started = time.monotonic()
    status = audio_input.MicInput(on_frames=lambda f: None, device_name="Headset (Contoso Wireless Earbuds)", sd_module=sd).start()
    took = time.monotonic() - started
    assert took < 0.3 * int(SETTINGS["audio"]["open_max_attempts"]) + 1.0  # seconds, not minutes
    assert status.live and status.device_name == "Microphone Array (Realtek(R) Audio)"  # the default instead
    assert "isn't connected" in status.note  # and she says so (once, at voice ready)


def test_a_missing_saved_mic_uses_the_default_and_says_so(monkeypatch) -> None:
    """A missing saved mic uses the default and says so."""
    from jarvis.voice import audio_input

    status = audio_input.MicInput(on_frames=lambda f: None, device_name="USB Mic (gone)", sd_module=_FakeSD()).start()
    assert status.live and status.device_name == "Microphone Array (Realtek(R) Audio)"
    assert status.note == SETTINGS["setup_messages"]["mic_saved_missing"].format(
        name="USB Mic (gone)", default="Microphone Array (Realtek(R) Audio)")


def test_the_microphone_only_changes_from_a_pick_in_the_list() -> None:
    """The microphone only changes from a pick in the list."""
    # The only code that writes "microphone set to ..." is the picker's own
    # handler (a click on a name in the Microphone list), and it now says so.
    sources = [p for p in (PROJECT / "jarvis").rglob("*.py") if "microphone set to" in p.read_text(encoding="utf-8")]
    assert [p.name for p in sources] == ["app.py"]
    app_text = (PROJECT / "jarvis" / "dashboard" / "app.py").read_text(encoding="utf-8")
    assert "picked in the Microphone list" in app_text and "picker_click_guard_seconds" in app_text
    assert app_text.count("save_string_setting(\"audio\", \"input_device_name\"") == 1


# == the banned APIs stay banned ======================================================================================


def test_no_input_injection_or_focus_api_anywhere() -> None:
    """No input injection or focus API anywhere."""
    banned = ["SendKeys", "AppActivate", "SetForegroundWindow", "ShowWindow", "BringWindowToTop", "MoveWindow",
              "keybd_event", "mouse_event", "SendInput"]
    for folder in ("jarvis", "scripts", "tools"):
        for py in (PROJECT / folder).rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            for name in banned:
                assert name not in text, f"{name} in {py}"
