"""Real voice commands (an earphone mic and the
laptop mic), one test per line, using the EXACT transcripts Whisper wrote.

Commands go through the production matcher order (app.register_matchers)
and the real controller, orchestrator, listener, F3/F4 finders and logbook.
Faked: the microphone, Whisper (it returns the logged transcript), the wake
model, Groq (a spy that fails the test's "never online" checks), and the
launching itself (os.startfile / Chrome).
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import List

import numpy as np
import pytest

from jarvis.actions import app_finder, find_file, open_app
from jarvis.actions.app_finder import STORE_APP_PREFIX, ScoredMatch, StoreAppCache, enumerate_installed_apps
from jarvis.actions.command_text import clean_command, is_name, pick_number
from jarvis.actions.conversation import _FAILURE_STEPS
from jarvis.actions.file_index import FileIndex
from jarvis.actions.find_file import FileFinder
from jarvis.actions.sleep_wake import is_wake_phrase
from jarvis.controller import Controller, Event, EventType, State
from jarvis.dashboard.activity_feed import format_record, format_session_log
from jarvis.dashboard.app import register_matchers
from jarvis.dashboard.internet_status import internet_display
from jarvis.interpreter import Interpreter
from jarvis.logbook import Logbook
from jarvis.logbook.debug_log import DebugLog
from jarvis.online import ConnectionMode, OnlineSnapshot
from jarvis.orchestrator import Orchestrator
from jarvis.settings import SETTINGS
from jarvis.speech import SpeechOutput
from jarvis.speech.qa import AnswerResult, GroqAnswerer
from jarvis.voice.listener import SOURCE_HOTKEY, SOURCE_WAKE_WORD, VoiceListener
from jarvis.voice.stt import Transcript, transcribe_options

EVENING = 20
# Any wake greeting from [greeting.pools] (redesigned: varied).
_ALL_GREETINGS = {
    line.replace("{name}", SETTINGS["user"]["name"])
    for pool in SETTINGS["greeting"]["pools"].values()
    for line in pool
}


def _one_greeting(texts) -> bool:
    return len(texts) == 1 and texts[0] in _ALL_GREETINGS
_LOUD = np.full(1280, 3000, dtype=np.int16)
_QUIET = np.zeros(1280, dtype=np.int16)
_FPS = 16000 / 1280  # 12.5 frames of 80 ms per second


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

    def is_speaking(self) -> bool:
        """Fake is speaking for the tests."""
        return False


class SpyAnswerer:
    """Stands in for Groq: records every question that would have gone online."""

    model = "openai/gpt-oss-20b"

    def __init__(self) -> None:
        self.questions: List[str] = []

    def answer(self, question, on_going_online=None):
        """Fake answer for the tests."""
        self.questions.append(question)
        if on_going_online is not None:
            on_going_online()
        return AnswerResult("an online answer", went_online=True)


class FakeSTT:
    """Whisper, returning exactly the words it really heard."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.samples: List[int] = []

    def transcribe_detailed(self, audio):
        """Fake transcribe detailed for the tests."""
        self.samples.append(len(audio))
        return Transcript(self.text, 0.05)


class FakeDetector:
    """Detector stand-in returning canned detections; counts its calls."""
    def __init__(self) -> None:
        self.frames = 0
        self.lines: List[str] = []

    def feed(self, frame) -> None:
        """Fake feed for the tests."""
        self.frames += 1

    def reset(self) -> None:
        """Fake reset for the tests."""
        pass

    def log_line(self, text: str) -> None:
        """Fake log line for the tests."""
        self.lines.append(text)


class NoStoreApps:
    """Store-app list stand-in: no Store apps and no background read."""
    def prefetch(self) -> None:
        """Fake prefetch for the tests."""
        pass

    def apps(self, wait_seconds: float = 0.0):
        """Fake apps for the tests."""
        return []


_CHROME = ScoredMatch("Google Chrome", 0.0, "app", r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Google Chrome.lnk")
_APPS = [
    _CHROME,
    ScoredMatch("OneDrive", 0.0, "app", r"C:\Start\OneDrive.lnk"),
    ScoredMatch("OneNote", 0.0, "app", r"C:\Start\OneNote.lnk"),
    ScoredMatch("Camera", 0.0, "app", STORE_APP_PREFIX + "Microsoft.WindowsCamera_8wekyb3d8bbwe!App"),
    ScoredMatch("WSL Settings", 0.0, "app", r"C:\Start\WSL Settings.lnk"),
]
_PASSPORTS = [
    ("Downloads", "Alex Passport .pdf"),
    ("Desktop/Archive", "Alex Passport .pdf"),
    ("Downloads", "Alex Taylor Passport .pdf"),
    ("Desktop/Scans", "Alex Taylor Passport.pdf"),
]


@pytest.fixture
def laptop(tmp_path, monkeypatch):
    """The whole command path with this laptop's apps and files faked."""
    made = []
    launched: List[str] = []
    chrome_runs: List[list] = []

    # Files: the four passport PDFs from the log, and a folder named Jarvis.
    root = tmp_path / "drive"
    for folder, name in _PASSPORTS:
        (root / folder).mkdir(parents=True, exist_ok=True)
        (root / folder / name).write_text("pdf", encoding="utf-8")
    (root / "Jarvis").mkdir()
    (root / "Jarvis" / "main.py").write_text("x", encoding="utf-8")
    index = FileIndex(roots=[root])
    index.rebuild_now()

    # Apps: the Start Menu + Store apps the session touched; launching recorded, not done.
    monkeypatch.setattr(app_finder, "enumerate_installed_apps", lambda store_apps=None: list(_APPS))
    monkeypatch.setattr(open_app, "STORE_APPS", NoStoreApps())
    monkeypatch.setattr(open_app, "find_chrome", lambda: r"C:\Chrome\chrome.exe")
    monkeypatch.setattr(open_app.os, "startfile", launched.append)
    monkeypatch.setattr(find_file.os, "startfile", launched.append)
    monkeypatch.setattr(open_app.subprocess, "Popen", lambda args, shell=False: chrome_runs.append(args))
    verify = lambda launch, timeout, poll: (launch(), False)  # noqa: E731
    monkeypatch.setattr(open_app, "launch_and_verify_with_retry", verify)
    monkeypatch.setattr(find_file, "launch_and_verify_with_retry", verify)

    def _make(start_state=State.AWAKE):
        c = Controller(start_state=start_state)
        logbook = Logbook(debug_log=DebugLog(path=tmp_path / "debug.log"))
        c.set_transition_listener(logbook.on_transition)
        c.start()
        made.append(c)
        interpreter = Interpreter()
        speech = FakeSpeech()
        orch = Orchestrator(c, interpreter, speech, logbook=logbook, clock_hour=lambda: EVENING)
        answerer = SpyAnswerer()
        register_matchers(interpreter, orch, logbook, FileFinder(index), answerer, None)

        def say(text: str) -> None:
            """A spoken command while AWAKE, exactly as the listener hands it over."""
            c.submit(Event(EventType.SPEECH_STARTED, {"source": "voice"}))
            assert _wait(lambda: c.state == State.LISTENING)
            orch.handle_command(text, "voice")
            assert _wait(lambda: c.state in (State.AWAKE, State.SLEEPING, State.CONFIRMING))

        return {
            "c": c, "orch": orch, "speech": speech, "logbook": logbook, "groq": answerer, "say": say,
            "launched": launched, "chrome": chrome_runs, "root": root, "debug": tmp_path / "debug.log",
        }

    yield _make
    for c in made:
        c.shutdown()


def _lines(logbook) -> List[str]:
    return [line for r in logbook.records() for line in format_record(r).lines]


# -- wake / sleep recognition ---------------------------------------------------------


def _asleep_listener(rig, transcript):
    stt = FakeSTT(transcript)
    detector = FakeDetector()
    heard: List[str] = []
    listener = VoiceListener(
        rig["c"], rig["orch"].handle_command, detector=detector, stt=stt,
        wake=rig["orch"].wake, heard_while_asleep=heard.append,
    )
    listener.attach()
    return listener, stt, detector, heard


def _speak_into(listener, speech_frames=6, quiet_frames=None):
    quiet_frames = quiet_frames if quiet_frames is not None else int(SETTINGS["listen"]["end_silence_seconds"] * _FPS) + 2
    for _ in range(speech_frames):
        listener.on_frames(_LOUD)
    for _ in range(quiet_frames):
        listener.on_frames(_QUIET)


@pytest.mark.parametrize(
    "transcript",
    ["Wake up, Johnless.", "Hey, Javis, wake up.", "Make up Jarvis.", "Wake up, Charvis.", "Welcome, Charvis!"],
)
def test_asleep_misheard_wake_phrases_wake_her_and_she_greets(laptop, transcript) -> None:
    """Asleep misheard wake phrases wake her and she greets."""
    rig = laptop(start_state=State.SLEEPING)
    listener, stt, detector, heard = _asleep_listener(rig, transcript)
    _speak_into(listener)
    assert _wait(lambda: rig["c"].state == State.AWAKE)
    assert heard == []  # it woke her, so it is not "heard while asleep"
    assert _one_greeting(rig["speech"].texts)
    assert rig["groq"].questions == []


def test_asleep_here_job_is_is_ignored(laptop) -> None:
    """Asleep here job is is ignored."""
    rig = laptop(start_state=State.SLEEPING)
    listener, stt, detector, heard = _asleep_listener(rig, "Here, job is")
    _speak_into(listener)
    assert _wait(lambda: heard == ["Here, job is"])
    assert rig["c"].state == State.SLEEPING and rig["speech"].texts == []


def test_name_variants_and_edit_distance() -> None:
    """Name variants and edit distance."""
    for word in ("jarvis", "javis", "charvis", "jervis", "johnless", "travis", "jarvas", "service", "jarvys"):
        assert is_name(word), word
    for word in ("job", "is", "here", "chrome", "open", "jar"):
        assert not is_name(word), word


def test_plain_wake_up_or_hey_without_a_name_never_wakes() -> None:
    """Plain wake up or hey without a name never wakes."""
    for said in ("wake up", "hey", "make up your mind", "welcome home", "Here, job is", "my friend jarvis said hi"):
        assert not is_wake_phrase(said), said


def test_wake_word_model_hears_the_phrase_while_it_is_being_recorded(laptop) -> None:
    """Root cause of the late wake: once speech started, every frame went to
    the recording and none to the wake-word model, so "Hey JARVIS" itself
    was never scored. Now the model gets every asleep frame."""
    rig = laptop(start_state=State.SLEEPING)
    listener, stt, detector, heard = _asleep_listener(rig, "something else")
    before = detector.frames
    _speak_into(listener, speech_frames=10)
    assert detector.frames - before >= 10 + int(SETTINGS["listen"]["end_silence_seconds"] * _FPS)


def test_wake_word_hit_wakes_and_greets_at_once(laptop) -> None:
    """Wake word hit wakes and greets at once."""
    rig = laptop(start_state=State.SLEEPING)
    listener, stt, detector, heard = _asleep_listener(rig, "Hey, Javis, wake up.")
    for _ in range(4):
        listener.on_frames(_LOUD)  # recording open, phrase still being said
    started = time.monotonic()
    listener.on_wake_word(0.9)  # the model fires mid-phrase
    assert _wait(lambda: rig["c"].state == State.AWAKE)
    assert time.monotonic() - started < 1.0
    assert _one_greeting(rig["speech"].texts)
    assert stt.samples == []  # the recording was dropped: no second wake from the transcript


def test_noisy_mic_ends_the_utterance_on_quiet_not_at_max_length(laptop) -> None:
    """The 10 s: with the Contoso mic's background above speech_rms_threshold
    (500), quiet never arrived and every recording ran the full 8 s
    max_command_seconds before Whisper even started. The threshold now
    rises above the background."""
    rig = laptop(start_state=State.SLEEPING)
    listener, stt, detector, heard = _asleep_listener(rig, "Wake up, Charvis.")
    noise = np.full(1280, 700, dtype=np.int16)  # above the fixed 500 threshold
    for _ in range(int(3 * _FPS)):
        listener.on_frames(noise)  # background before speaking
    assert listener.speech_threshold() >= 1400
    for _ in range(10):
        listener.on_frames(_LOUD)  # "Wake up, Charvis."
    for _ in range(int(1.5 * _FPS)):
        listener.on_frames(noise)  # noise, not silence, after it
    assert _wait(lambda: rig["c"].state == State.AWAKE)
    recorded, ended_by, _transcribe = listener.last_timing
    assert ended_by == "quiet" and recorded < 2.5, listener.last_timing
    assert any("ended by quiet" in line for line in detector.lines)  # timing line, numbers only


def test_asleep_recording_is_capped_shorter_than_a_command(laptop) -> None:
    """Asleep recording is capped shorter than a command."""
    rig = laptop(start_state=State.SLEEPING)
    listener, stt, detector, heard = _asleep_listener(rig, "the tv is talking")
    for _ in range(int(9 * _FPS)):
        listener.on_frames(_LOUD)  # speech that never stops
    assert _wait(lambda: listener.last_timing is not None)
    recorded, ended_by, _ = listener.last_timing
    assert ended_by == "max length" and recorded <= SETTINGS["listen"]["max_asleep_utterance_seconds"] + 0.1


def test_good_night_javis_while_awake_sleeps_locally(laptop) -> None:
    """Good night javis while awake sleeps locally."""
    rig = laptop()
    rig["say"]("Good night, Javis")
    assert _wait(lambda: rig["c"].state == State.SLEEPING)
    assert rig["speech"].texts == ["Goodnight."]
    assert rig["groq"].questions == []


def test_wake_up_while_awake_is_answered_locally(laptop) -> None:
    """Wake up while awake is answered locally."""
    rig = laptop()
    rig["say"]("Wake up")
    assert rig["speech"].texts == ["I'm already awake."]
    assert rig["groq"].questions == [] and rig["c"].state == State.AWAKE


def test_whisper_is_biased_toward_her_name() -> None:
    """Whisper is biased toward her name."""
    options = transcribe_options()
    # Her name first, then the app and site names he says most.
    assert options["hotwords"].startswith("JARVIS, ") and "ChatGPT" in options["hotwords"]
    assert "initial_prompt" not in options  # a sentence prompt can be hallucinated on noise


def test_wake_score_debug_log_is_off_again() -> None:
    """Wake score debug log is off again."""
    # Debug scoring is off in the shipped settings.
    assert SETTINGS["wake_word"]["debug_scores"] is False


# -- greeting on every wake ----------------------------------------------------------------


@pytest.mark.parametrize("route", ["wake word", "wake phrase", "wake hotkey", "typed"])
def test_every_wake_route_greets_and_shows_it(laptop, route) -> None:
    """Every wake route greets and shows it."""
    rig = laptop(start_state=State.SLEEPING)
    listener, stt, detector, heard = _asleep_listener(rig, "Wake up, Johnless.")
    if route == "wake word":
        listener.on_wake_word(0.8)
    elif route == "wake phrase":
        _speak_into(listener)
    elif route == "wake hotkey":
        listener.push_to_talk()
    else:
        rig["orch"].handle_typed_command("wake up jarvis")
    assert _wait(lambda: rig["c"].state == State.AWAKE)
    assert _one_greeting(rig["speech"].texts)
    assert f"saying: {rig['speech'].texts[0]}" in _lines(rig["logbook"])


def test_two_routes_for_one_phrase_greet_once(laptop) -> None:
    """The wake word and the transcript both firing used to speak two
    greetings, the refused one also sending its own SPEECH_FINISHED."""
    rig = laptop(start_state=State.SLEEPING)
    import threading

    threads = [threading.Thread(target=rig["orch"].wake, args=(src,)) for src in (SOURCE_WAKE_WORD, "wake phrase", SOURCE_HOTKEY)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert rig["c"].state == State.AWAKE
    assert _one_greeting(rig["speech"].texts)


# -- no provider name on screen ------------------------------------------------------------


def test_live_activity_never_names_the_provider_but_the_record_does(laptop) -> None:
    """Live activity never names the provider but the record does."""
    rig = laptop()
    rig["say"]("What is photosynthesis")
    lines = _lines(rig["logbook"])
    assert 'searching online, sending "what is photosynthesis"' in lines
    shown = "\n".join(lines) + format_session_log(rig["logbook"].records())
    assert "Groq" not in shown and "gpt-oss" not in shown
    record = rig["logbook"].last_completed()
    assert record.online_destination == "Groq, openai/gpt-oss-20b"
    assert "online='Groq, openai/gpt-oss-20b'" in rig["debug"].read_text(encoding="utf-8")
    # "why" still says it went online -- without naming the provider.
    rig["say"]("Tell me why did you do that")
    why = rig["speech"].texts[-1]
    assert "searched online" in why and "Groq" not in why


def test_no_user_visible_string_names_the_provider() -> None:
    """No user visible string names the provider."""
    allowed = {"groq_no_key", "groq_key_rejected"}  # must say which key to add or check
    for key, message in SETTINGS["setup_messages"].items():
        if key not in allowed:
            assert "groq" not in message.lower() and "gpt-oss" not in message.lower(), key
    for step in _FAILURE_STEPS.values():
        assert "groq" not in step.lower()
    assert "Groq" not in SETTINGS["speech"]["checking_online_text"]
    for mode in (ConnectionMode.ONLINE, ConnectionMode.LIMITED, ConnectionMode.LOCAL):
        assert "Groq" not in internet_display(OnlineSnapshot(mode, service="Groq")).detail


# -- time and date from the laptop clock -----------------------------------------------------


@pytest.mark.parametrize(
    "said, expected",
    [
        ("Tell me the current time please", "It's "),
        ("What is the Sydney time right now", "in Sydney"),
        ("what time is it", "It's "),
        ("tell me the time", "It's "),
        ("what's the date", "Today is "),
        ("what day is it", "It's "),
    ],
)
def test_time_and_date_are_answered_locally(laptop, said, expected) -> None:
    """Time and date are answered locally."""
    rig = laptop()
    rig["say"](said)
    assert expected in rig["speech"].texts[-1]
    assert rig["groq"].questions == []


def test_sydney_time_comes_from_windows_time_zones() -> None:
    """Sydney time comes from Windows time zones."""
    pytest.importorskip("win32timezone")
    from jarvis.actions.clock import ClockMatcher

    fixed = datetime(2026, 10, 16, 9, 39, tzinfo=timezone.utc)  # 20:39 in Sydney (daylight time)
    assert ClockMatcher(now=lambda: fixed)("what is the sydney time").text == "It's 8:39 PM in Sydney, on Friday."
    assert ClockMatcher(now=lambda: fixed)("what time is it in london").text == "It's 10:39 AM in London, on Friday."


def test_groq_prompt_carries_the_current_local_date_and_time() -> None:
    """Groq prompt carries the current local date and time."""
    fixed = datetime(2026, 10, 16, 21, 5, tzinfo=timezone(timedelta(hours=11)))
    prompt = GroqAnswerer(api_key="x", now=lambda: fixed).system_prompt
    assert "Friday 16 October 2026, 21:05" in prompt
    assert "never say you cannot tell the time" in prompt


# -- command clean-up, Chrome sites, "why" ---------------------------------------------------


def test_tell_me_why_did_you_do_that_is_answered_from_the_record(laptop) -> None:
    """Tell me why did you do that is answered from the record."""
    rig = laptop()
    rig["say"]("Open Chrome please")
    rig["say"]("Tell me why did you do that")
    assert rig["speech"].texts[-1].startswith("I opened Google Chrome because you asked me to")
    assert rig["groq"].questions == []


def test_open_chrome_please_opens_chrome(laptop) -> None:
    """Open Chrome please opens Chrome."""
    rig = laptop()
    rig["say"]("Open Chrome please")
    assert rig["launched"] == [_CHROME.target]
    assert rig["speech"].texts == ["Opened Google Chrome."]


@pytest.mark.parametrize("said", ["Open Chat GPT on Chrome please", "Sir, Chat GPD on Chrome please"])
def test_chatgpt_on_chrome_opens_chatgpt_com_in_chrome(laptop, said) -> None:
    """Chatgpt on Chrome opens chatgpt com in Chrome."""
    rig = laptop()
    rig["say"](said)
    assert rig["chrome"] == [[r"C:\Chrome\chrome.exe", "https://chatgpt.com"]]
    assert rig["groq"].questions == []


def test_open_chrome_and_then_search_opens_chrome_and_says_search_is_not_built(laptop) -> None:
    """Open Chrome and then search opens Chrome and says search is not built."""
    rig = laptop()
    rig["say"]("open Chrome and then search chat GPT")
    assert rig["launched"] == [_CHROME.target]
    assert rig["speech"].texts == ["Opened Google Chrome. To search the web, say search and what you want, for example: search best pizza in Sydney."]
    assert rig["groq"].questions == []


def test_cleanup_keeps_the_words_that_matter() -> None:
    """Cleanup keeps the words that matter."""
    assert clean_command("Open Chrome please") == "open chrome"
    assert clean_command("Sir, Chat GPD on Chrome please") == "chat gpd on chrome"
    assert clean_command("Find Jarvis folder") == "find jarvis folder"  # her name as the thing searched for stays
    assert clean_command("Open Chrome, Jarvis.") == "open chrome"
    assert clean_command("Can you just open notepad for me now") == "open notepad"


# -- number words pick from the pending list ------------------------------------------------


def test_passport_sequence_open_one_opens_file_one(laptop) -> None:
    """Passport sequence open one opens file one."""
    rig = laptop()
    rig["say"]("Find Alex Passport")
    assert rig["speech"].texts[-1] == "I found 4 files. Which one would you like to open?"
    first_line = next(step for step in rig["logbook"].last_completed().progress if step.startswith("1. "))
    rig["say"]("Open one")
    assert len(rig["launched"]) == 1
    opened = rig["launched"][0]
    # The list shows names (plus the folder's name when two share a name), never a path.
    assert os.path.basename(opened) in first_line and os.path.dirname(opened) not in first_line
    assert "OneDrive" not in rig["speech"].texts[-1] and "OneNote" not in rig["speech"].texts[-1]


def test_out_of_range_number_never_falls_through_to_app_search(laptop) -> None:
    """Out of range number never falls through to app search."""
    rig = laptop()
    rig["say"]("Find Alex Passport")
    rig["say"]("Open number nine")
    assert rig["launched"] == []
    assert rig["speech"].texts[-1] == "There are only 4 matches on the list. Say 'open' and a number from 1 to 4."


def test_an_app_list_shown_later_is_the_one_picked_from(laptop) -> None:
    """An app list shown later is the one picked from."""
    rig = laptop()
    rig["say"]("Find Alex Passport")
    rig["say"]("Open one drive or note")  # no confident app: the "which app?" list
    rig["say"]("open the second one")
    assert rig["launched"] and not rig["launched"][0].endswith(".pdf")


def test_number_words() -> None:
    """Number words."""
    assert [pick_number(s) for s in ("one", "2", "number two", "the second one", "first", "three", "number one")] == [1, 2, 2, 2, 1, 3, 1]
    for selector in ("chrome", "google for me", "one drive"):
        assert pick_number(selector) is None, selector


# -- Store apps and folders ------------------------------------------------------------------


def test_open_camera_opens_the_windows_camera_app(laptop) -> None:
    """Open camera opens the Windows camera app."""
    rig = laptop()
    rig["say"]("Open camera")
    assert rig["launched"] == [STORE_APP_PREFIX + "Microsoft.WindowsCamera_8wekyb3d8bbwe!App"]
    assert rig["speech"].texts == ["Opened Camera."]


def test_store_apps_are_part_of_the_app_list() -> None:
    """Store apps are part of the app list."""
    camera = ScoredMatch("Camera", 0.0, "app", STORE_APP_PREFIX + "Microsoft.WindowsCamera_8wekyb3d8bbwe!App")
    cache = StoreAppCache(reader=lambda: [camera])
    assert camera in enumerate_installed_apps(store_apps=cache)
    # Still subject to the 0.75 rule: an exact name is 1.0, a loose one asks.
    assert app_finder.find_app("camera", candidates=[camera])[0].score == 1.0
    assert app_finder.confident(app_finder.find_app("camera", candidates=[camera]))


def test_find_jarvis_folder_finds_the_folder(laptop) -> None:
    """Find JARVIS folder finds the folder."""
    rig = laptop()
    rig["say"]("Find Jarvis folder")
    assert rig["launched"] == [str(rig["root"] / "Jarvis")]
    assert rig["speech"].texts == ["Opened Jarvis."]
