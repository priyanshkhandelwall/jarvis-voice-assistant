"""Real voice commands. Every
failure becomes a regression test with the EXACT words said. Commands go
through the production matcher order (register_matchers) and the real
orchestrator. Faked: the apps and their launching, the system settings
(FakeSystem), Open-Meteo, the screen, the camera, the speech engine's audio
device, and Groq (a spy: anything that reaches it is recorded)."""

from __future__ import annotations

import time
from datetime import date

import numpy as np
import pytest

from jarvis.actions import app_finder
from jarvis.actions.command_text import CONFIRM, confirm_intent
from jarvis.actions.conversation import failure_cause
from jarvis.actions.weather import WeatherMatcher
from jarvis.controller import State
from jarvis.dashboard.activity_feed import format_record
from jarvis.online import OnlineErrorKind, OnlineStatus
from jarvis.screen.commands import ScreenCommands
from jarvis.sensors import SensorFlags
from jarvis.settings import SETTINGS
from jarvis.speech.claims import ClaimCheck
from jarvis.speech.qa import AnswerResult, build_system_prompt
from jarvis.system.commands import SystemCommands
from jarvis.vision import commands as vision_mod
from jarvis.vision.camera import Camera
from jarvis.vision.commands import VisionCommands
from jarvis.vision.detector import Detection
from jarvis.vision.prices import PriceEntry
from jarvis.voice.listener import VoiceListener, fragment_reason
from jarvis.youtube.commands import YouTubeCommands
from stretch_rig import CHROME, SpyAnswerer, build, service, wait
from test_f16a_weather import FakeOpenMeteo, _client
from test_f22_system import Clock, FakeSystem
from test_regressions_files_and_sites import _APPS, FakeScreen
from test_listener import FakeSTT, _utterance

_SYS = SETTINGS["system"]["replies"]
_CAM = SETTINGS["camera"]["replies"]
_CONV = SETTINGS["conversation"]
REMINDER = _CONV["confirm_reminder_reply"]
TODAY = date(2026, 10, 31)


class _Groq(SpyAnswerer):
    """Test helper: Groq."""
    def __init__(self) -> None:
        super().__init__()
        self.result = None  # an AnswerResult to return instead of self.reply

    def answer(self, question, on_going_online=None, length_rule=None):
        """Fake answer for the tests."""
        if self.result is None:
            return super().answer(question, on_going_online, length_rule)
        self.questions.append(question)
        if on_going_online is not None:
            on_going_online()
        return self.result

    def answer_screen(self, question, screen_text, jpeg, model, instructions, on_going_online=None):
        """Fake answer screen for the tests."""
        self.questions.append(question)
        return AnswerResult("That is a YouTube page and a sales report.", went_online=True)


class _Capture:
    """Test helper: Capture."""
    def read(self):
        """Fake read for the tests."""
        return True, np.full((480, 640, 3), 120, dtype=np.uint8)

    def release(self) -> None:
        """Fake release for the tests."""
        pass


class _Detector:
    """Test helper: Detector."""
    def __init__(self) -> None:
        # 's look: him in the middle, a cell phone held up on the left.
        self.found = [Detection("person", 0.91, (250, 50, 400, 470)), Detection("cell phone", 0.80, (40, 200, 120, 330))]

    def detect(self, frame):
        """Fake detect for the tests."""
        return list(self.found)

    def unload_if_idle(self):
        """Fake unload if idle for the tests."""
        return False


_PRICES = {"cell phone": PriceEntry("cell phone", 200, 1500, "AUD", date(2026, 10, 1), "test table")}


@pytest.fixture
def laptop(tmp_path, monkeypatch):
    """Fixture: the production wiring with a fake laptop (apps, files, screen, camera), nothing real
    opened."""
    groq = _Groq()
    fake_pc, clock = FakeSystem(), Clock()
    system = SystemCommands(fake_pc, clock=clock, sleep=clock.sleep)
    status = OnlineStatus()
    meteo = FakeOpenMeteo()
    weather = WeatherMatcher(_client(meteo, status), status)
    sensors = SensorFlags(min_seconds=0)
    screen_fake = FakeScreen()
    screen = ScreenCommands(groq, sensors, capture_fn=screen_fake.capture, ocr_fn=screen_fake.ocr)
    detector = _Detector()
    monkeypatch.setitem(vision_mod._CFG, "stream_detect_seconds", 0.05)
    vision = VisionCommands(Camera(sensors, opener=lambda index: _Capture() if index == 0 else None, blocked=lambda: False),
                            detector, today=lambda: TODAY, price_table=_PRICES)
    youtube = YouTubeCommands(groq, service("YouTube"), chrome=lambda: CHROME)
    rig = build(tmp_path, monkeypatch, groq=groq, system=system, weather=weather, screen=screen, vision=vision, youtube=youtube)
    monkeypatch.setattr(app_finder, "enumerate_installed_apps", lambda store_apps=None: list(_APPS))
    rig.update(pc=fake_pc, meteo=meteo, vision=vision, detector=detector, system=system)
    yield rig
    vision.stop_stream()
    rig["c"].shutdown()


def _lines(rig):
    return format_record(rig["logbook"].live_snapshot() or rig["logbook"].last_completed()).lines


def _shown(rig) -> str:
    return " | ".join(_lines(rig))


# == "Turn off Wi-Fi" and "Restart the laptop" start the confirm flow =====================


def test_turn_off_wifi_asks_says_and_logs_the_prompt(laptop) -> None:
    """Turn off Wi-Fi asks says and logs the prompt."""
    reply = laptop["say"]("Turn off Wi-Fi")
    assert reply == _SYS["wifi_prompt_off"]
    assert laptop["c"].state == State.CONFIRMING
    shown = _shown(laptop)
    assert f"saying: {_SYS['wifi_prompt_off']}" in shown
    assert "waiting for confirm (L3, 1 needed)" in shown
    assert laptop["pc"].calls == []  # nothing changed before the confirm
    assert laptop["confirming"]("Confirm.") == "Wi-Fi is now off."
    assert laptop["pc"].wifi is False
    assert laptop["groq"].questions == []


def test_restart_the_laptop_needs_two_confirms_and_logs_each(laptop) -> None:
    """Restart the laptop needs two confirms and logs each."""
    prompt = _SYS["restart_prompt"].format(seconds=int(SETTINGS["system"]["shutdown_grace_seconds"]))
    assert laptop["say"]("Restart the laptop") == prompt
    assert f"saying: {prompt}" in _shown(laptop)
    assert "waiting for confirm (L4, 2 needed)" in _shown(laptop)
    assert laptop["confirming"]("Confirm") == _CONV["confirm_again_reply"]
    assert f"saying: {_CONV['confirm_again_reply']}" in _shown(laptop)
    assert not any(call[0] == "schedule_shutdown" for call in laptop["pc"].calls)
    reply = laptop["confirming"]("confirm")
    assert reply.startswith("Restart requested.")
    assert any(call[0] == "schedule_shutdown" and call[2] is True for call in laptop["pc"].calls)
    laptop["system"].cancel_countdown()


def test_a_cancel_while_waiting_is_logged_with_what_was_heard(laptop) -> None:
    """The cause found in the debug.log: both records ended with
    result=None, which the logbook only writes for CANCEL -> "Cancelled." ->
    SPEECH_FINISHED, and nothing heard while confirming was ever logged."""
    laptop["say"]("Turn off Wi-Fi")
    assert laptop["confirming"]("No.") == "Cancelled."
    shown = _shown(laptop)
    assert 'heard while waiting: "No" -> cancel' in shown
    assert "saying: Cancelled." in shown
    assert laptop["pc"].calls == []


def test_whispers_no_on_noise_never_cancels_a_pending_wifi_change(laptop) -> None:
    """Whispers no on noise never cancels a pending Wi-Fi change."""
    laptop["say"]("Turn off Wi-Fi")
    ignored = []
    listener = VoiceListener(laptop["c"], laptop["orch"].handle_command, stt=FakeSTT("No.", no_speech_prob=0.9),
                             ignored=lambda t, r: ignored.append((t, r)),
                             not_confirm_or_cancel=laptop["orch"].remind_confirm_or_cancel)
    listener.attach()
    try:
        _utterance(listener)
        assert wait(lambda: ignored == [("No.", "unclear (waiting for confirm or cancel)")])
        assert laptop["c"].state == State.CONFIRMING and laptop["c"].pending_confirmation() is not None
    finally:
        listener.detach()


# == no invented facts ================================================================


def test_is_it_going_to_be_hot_tomorrow_is_the_local_weather_never_groq(laptop) -> None:
    """Is it going to be hot tomorrow is the local weather never Groq."""
    reply = laptop["say"]("Is it going to be hot tomorrow")
    assert laptop["groq"].questions == []
    assert "forecast" in laptop["meteo"].kinds()
    assert reply.startswith("Tomorrow in Sydney: high ")  # the fake forecast's numbers, not Groq's


@pytest.mark.parametrize("said", ["When is my presentation due", "Ven is my presentation", "When is my presentation now"])
def test_a_question_about_my_thing_with_nothing_stored_never_goes_online(laptop, said) -> None:
    """A question about my thing with nothing stored never goes online."""
    assert laptop["say"](said) == "You haven't told me about your presentation."
    assert laptop["groq"].questions == []


def test_my_presentation_is_answered_from_memory(laptop) -> None:
    """My presentation is answered from memory."""
    assert laptop["say"]("Remember that my presentation is on Tuesday") == "Got it."
    for said in ["Ven is my presentation", "When is my presentation due", "When is my presentation now",
                 "But I told you my presentation is on Tuesday, not Wednesday"]:
        reply = laptop["say"](said)
        assert reply.startswith("You told me your presentation is on tuesday."), (said, reply)
    assert laptop["groq"].questions == []


def test_i_told_you_with_nothing_stored_says_how_to_store_it(laptop) -> None:
    """I told you with nothing stored says how to store it."""
    reply = laptop["say"]("But I told you my presentation is on Tuesday, not Wednesday")
    assert reply.startswith("You haven't told me about your presentation.")
    assert laptop["groq"].questions == []


def test_the_groq_prompt_forbids_his_facts_weather_time_and_promises() -> None:
    """The Groq prompt forbids his facts weather time and promises."""
    prompt = build_system_prompt()
    for words in ("never state his personal facts", "the weather or forecast", "or the time", "Never promise a reminder",
                  "never ask for permission"):
        assert words in prompt


@pytest.mark.parametrize("groq_said, replaced", [
    ("I need your permission to adjust the volume. Could you confirm?", "I can't do that from here. Try saying 'set the volume to 50'."),
    ("Got it, I will adjust the reminder for Tuesday.", "I can't do that from here."),
    ("I can adjust that for you.", "I can't do that from here."),
    ("I will update your schedule.", "I can't do that from here."),
    ("I can change the brightness.", "I can't do that from here. Try saying 'set the brightness to 50'."),
])
def test_false_claims_are_caught(groq_said, replaced) -> None:
    """False claims are caught."""
    claim = ClaimCheck(SETTINGS["claims"]).find(groq_said, "x")
    assert claim is not None and claim.replacement == replaced


def test_a_claim_from_groq_is_replaced_before_she_says_it(laptop) -> None:
    """A claim from Groq is replaced before she says it."""
    laptop["groq"].reply = "Got it, I will adjust the reminder."
    assert laptop["say"]("tell me a joke about cats") == "I can't do that from here."


@pytest.mark.parametrize("plain", ["Photosynthesis turns sunlight into food.", "You can set the volume in Windows settings."])
def test_plain_answers_are_not_claims(plain) -> None:
    """Plain answers are not claims."""
    assert ClaimCheck(SETTINGS["claims"]).find(plain, "x") is None


# == spoken confirm ===================================================================


@pytest.mark.parametrize("heard", ["Confirm", "Confirm.", "Icon Firm", "Icon firm.", "Con firm", "confirmed", "Yes confirm",
                                   "Please confirm the Wi-Fi"])
def test_close_variants_of_confirm_count(heard) -> None:
    """Close variants of confirm count."""
    assert confirm_intent(heard) == CONFIRM


@pytest.mark.parametrize("heard", ["Console.", "and fun.", "and some.", "Say confirm.", "Say confirm twice."])
def test_other_words_are_not_a_confirm(heard) -> None:
    """Other words are not a confirm."""
    assert confirm_intent(heard) is None


def test_icon_firm_confirms_the_wifi_change(laptop) -> None:
    """Icon firm confirms the Wi-Fi change."""
    laptop["say"]("Turn off Wi-Fi")
    assert laptop["confirming"]("Icon Firm") == "Wi-Fi is now off."


@pytest.mark.parametrize("heard", ["Console.", "and fun.", "and some."])
def test_console_gets_the_reminder_spoken_and_logged(laptop, heard) -> None:
    """Console gets the reminder spoken and logged."""
    laptop["say"]("Turn off Wi-Fi")
    assert laptop["confirming"](heard) == REMINDER
    shown = _shown(laptop)
    assert f"saying: {REMINDER}" in shown
    assert f'heard while waiting: "{heard.rstrip(".")}" -> not confirm or cancel' in shown
    assert laptop["c"].state == State.CONFIRMING


def test_icon_firm_heard_by_the_listener_is_handed_over(laptop) -> None:
    """Icon firm heard by the listener is handed over."""
    laptop["say"]("Turn off Wi-Fi")
    listener = VoiceListener(laptop["c"], laptop["orch"].handle_command, stt=FakeSTT("Icon Firm."),
                             not_confirm_or_cancel=laptop["orch"].remind_confirm_or_cancel)
    listener.attach()
    try:
        _utterance(listener)
        assert wait(lambda: laptop["pc"].wifi is False)
    finally:
        listener.detach()


# == camera follow-ups ==================================================================


def _looked(laptop) -> None:
    assert laptop["say"]("What can you see") == "I can see a person and a cell phone."


def test_how_much_is_this_cost_the_cell_phone(laptop) -> None:
    """How much is this cost the cell phone."""
    _looked(laptop)
    reply = laptop["say"]("How much is this cost, the cell phone")
    assert reply.startswith("That looks like a cell phone.") and "200" in reply
    assert laptop["groq"].questions == []


def test_the_cell_phone_you_just_see(laptop) -> None:
    """The cell phone you just see."""
    _looked(laptop)
    assert laptop["say"]("The cell phone you just see") == \
        "Yes, in my last look I saw a cell phone. From the camera's view, the cell phone is on the left."
    assert laptop["groq"].questions == []


def test_where_it_is_left_or_right_the_cell_phone(laptop) -> None:
    """Where it is left or right the cell phone."""
    _looked(laptop)
    assert laptop["say"]("where it is left or right, the cell phone") == "From the camera's view, the cell phone is on the left."
    assert laptop["groq"].questions == []


def test_tell_me_about_that_person_never_identifies_anyone(laptop) -> None:
    """Tell me about that person never identifies anyone."""
    _looked(laptop)
    assert laptop["say"]("Tell me about that person") == _CAM["no_faces"]
    assert laptop["groq"].questions == []


def test_where_is_a_place_is_not_a_camera_question_without_a_recent_look(laptop) -> None:
    """Where is a place is not a camera question without a recent look."""
    laptop["say"]("where is the train station")
    assert laptop["groq"].questions  # an ordinary question, as before


def _camera_on(laptop) -> None:
    assert laptop["say"]("Turn on the camera") == _CAM["stream_prompt"]
    assert laptop["confirming"]("confirm") == _CAM["stream_on"]
    assert wait(lambda: laptop["vision"].recent() is not None)


def test_what_can_you_see_with_the_camera_on_answers_from_the_live_stream(laptop) -> None:
    """What can you see with the camera on answers from the live stream."""
    _camera_on(laptop)
    assert laptop["say"]("What can you see") == "I can see a person and a cell phone."


def test_10_of_the_camera_turns_it_off(laptop) -> None:
    """10 of the camera turns it off."""
    _camera_on(laptop)
    assert laptop["say"]("10 of the camera") == _CAM["stream_off"]
    assert laptop["vision"].stream is None


# == screen variants ====================================================================


@pytest.mark.parametrize("said", ["What am I looking right now", "Can you please tell me what I'm looking right now",
                                  "what am I looking at", "what I'm looking at"])
def test_what_am_i_looking_at_reads_the_screen(laptop, said) -> None:
    """What am i looking at reads the screen."""
    reply = laptop["say"](said)
    assert "Word" in reply or "YouTube" in reply or "Chrome" in reply, reply
    assert laptop["groq"].questions == []


@pytest.mark.parametrize("said", ["Splend my screen", "X-Link, My screen", "Explane my screen"])
def test_a_misheard_explain_my_screen_starts_the_explain_preview(laptop, said) -> None:
    """A misheard explain my screen starts the explain preview."""
    laptop["say"](said)
    pending = laptop["c"].pending_confirmation()
    assert pending is not None and pending.action_name == "send_screen_online"
    assert dict(pending.params)["question"] == "explain my screen"
    assert laptop["groq"].questions == []  # nothing sent before the confirm


# == volume words =========================================================================


@pytest.mark.parametrize("said, percent", [("Can you be set the volume to 4p", 40), ("Adjust the volume to 4p", 40),
                                           ("Adjust the Volume to for P", 40), ("Set the volume to max", 100),
                                           ("set the volume to forty", 40), ("set the volume to half", 50),
                                           ("set the volume to full", 100), ("change the volume to 30", 30)])
def test_volume_words(laptop, said, percent) -> None:
    """Volume words."""
    assert laptop["say"](said) == f"Volume is now {percent}%."
    assert laptop["pc"].volume == percent
    assert laptop["groq"].questions == []


def test_volume_to_min_is_zero(laptop) -> None:
    """Volume to min is zero."""
    laptop["say"]("set the volume to min")
    assert laptop["pc"].volume == 0


# == small misses ===========================================================================


def test_are_you_awake(laptop) -> None:
    """Are you awake."""
    assert laptop["say"]("Are you awake") == "I'm already awake."
    assert laptop["groq"].questions == []


def test_watch_todays_date_is_the_local_date(laptop) -> None:
    """Watch todays date is the local date."""
    assert laptop["say"]("Watch today's date").startswith("Today is ")
    assert laptop["groq"].questions == []


@pytest.mark.parametrize("said", ["Shader, then please open Chrome", "Shutter, then please open Chrome"])
def test_shader_then_please_open_chrome(laptop, said) -> None:
    """Shader then please open Chrome."""
    assert laptop["say"](said) == "Opened Google Chrome."
    assert laptop["groq"].questions == []


@pytest.mark.parametrize("said, query", [("Hello, free music on YouTube", "music"), ("pray music on YouTube", "music"),
                                         ("sad music on YouTube", "sad music")])
def test_music_on_youtube_is_a_youtube_search(laptop, said, query) -> None:
    """Music on YouTube is a YouTube search."""
    assert laptop["say"](said) == f"Searching YouTube for {query} in Chrome."
    assert laptop["groq"].questions == []


def test_a_single_known_app_name_is_not_too_short() -> None:
    """A single known app name is not too short."""
    assert fragment_reason("Notepad", 0.05) is None
    assert fragment_reason("Notepad.", 0.05) is None
    assert fragment_reason("camera", 0.05) == "too short"  # F9's webcam, never the Camera app
    assert fragment_reason("banana", 0.05) == "too short"


def test_notepad_alone_opens_notepad(laptop) -> None:
    """Notepad alone opens notepad."""
    assert laptop["say"]("Notepad") == "Opened notepad."


# == every stop names its cause ==============================================================


def test_a_stop_while_she_speaks_names_its_source(laptop) -> None:
    """A stop while she speaks names its source."""
    c, speech = laptop["c"], laptop["speech"]
    real_speak = speech.speak

    def speak_then_esc(text):
        real_speak(text)
        if c.state == State.SPEAKING:
            c.request_stop(source="Esc key")

    speech.speak = speak_then_esc
    laptop["groq"].reply = "Artificial intelligence mimics human thinking."
    laptop["say"]("Can you please tell me what is Artificial Intelligence")
    assert wait(lambda: laptop["logbook"].last_completed().result == "stopped before finishing")
    record = laptop["logbook"].last_completed()
    assert "stopped by: Esc key (while SPEAKING)" in record.progress
    assert format_record(record).lines[-1] == "stopped before finishing"
    assert "stopped by: Esc key (while SPEAKING)" in laptop["debug"]()


def test_a_spoken_stop_that_whisper_made_up_is_not_a_stop(laptop) -> None:
    """A spoken stop that Whisper made up is not a stop."""
    listener = VoiceListener(laptop["c"], laptop["orch"].handle_command, stt=FakeSTT("Stop.", no_speech_prob=0.9))
    listener.attach()
    try:
        _utterance(listener)
        time.sleep(0.3)
        assert not laptop["c"].is_stop_requested()
    finally:
        listener.detach()


def test_a_real_spoken_stop_is_recorded_as_spoken(laptop) -> None:
    """A real spoken stop is recorded as spoken."""
    listener = VoiceListener(laptop["c"], laptop["orch"].handle_command, stt=FakeSTT("Stop.", no_speech_prob=0.05))
    listener.attach()
    try:
        _utterance(listener)
        assert wait(lambda: laptop["c"].is_stop_requested())
        assert wait(lambda: "spoken stop" in laptop["debug"]())
    finally:
        listener.detach()


# == (voice): Paint opened, but no mood check-in ===============================================


_D5_SAID = ["Ugh nothing works today, open Paint", "Ugg Nothing Works Today, Open Paint", "Ugh, nothing works today. Open Paint.",
            "Uh, nothing works today, open Paint", "Ugh. Nothing works today. Open paint."]


def test_every_check_in_line_is_a_question() -> None:
    """The cause of 's voice failure: random.choice picked "You okay? I
    can keep things simple for a bit." -- it ends as a statement, so the reply
    didn't end with "?" (typed passed only because a different line was drawn)."""
    assert all(str(line).rstrip().endswith("?") for line in SETTINGS["mood"]["check_in_pool"])


def test_a_statement_in_the_pool_is_never_picked() -> None:
    """A statement in the pool is never picked."""
    from jarvis.speech.mood import MoodWatcher

    cfg = dict(SETTINGS["mood"], check_in_pool=["I can keep things simple for a bit.", "Are you okay?"])
    seen = []
    watcher = MoodWatcher(cfg, choose=lambda pool: seen.append(list(pool)) or pool[0])
    assert watcher.hear("ugh nothing works today, open paint").check_in == "Are you okay?"
    assert seen == [["Are you okay?"]]


@pytest.mark.parametrize("line", range(len(SETTINGS["mood"]["check_in_pool"])))
@pytest.mark.parametrize("said", _D5_SAID)
def test_d5_paint_opens_with_a_check_in(laptop, said, line, monkeypatch) -> None:
    """D5 paint opens with a check in."""
    from jarvis.actions.app_finder import ScoredMatch
    from jarvis.speech.mood import MoodWatcher

    apps = list(_APPS) + [ScoredMatch("paint", 0.0, "app", "mspaint.exe"), ScoredMatch("Paint 3D", 0.0, "app", r"C:\Start\Paint 3D.lnk")]
    monkeypatch.setattr(app_finder, "enumerate_installed_apps", lambda store_apps=None: list(apps))
    laptop["orch"]._mood = MoodWatcher(SETTINGS["mood"], choose=lambda pool: pool[line % len(pool)])  # every line, not a random one
    reply = laptop["say"](said)
    assert reply.lower().startswith("opened paint") and reply.rstrip().endswith("?"), reply


# == the real cause of "online search: service error" ==========================================


_TOOL_FAILED = "HTTP 400 tool_use_failed Tool choice is required, but model did not call a tool"


def test_the_cause_is_plain_and_names_no_provider() -> None:
    """The cause is plain and names no provider."""
    assert failure_cause(_TOOL_FAILED) == "the model answered without running the web search"
    assert failure_cause("HTTP 503 upstream unavailable") == "the service replied HTTP 503"
    assert failure_cause(None) == ""


def test_service_error_shows_its_cause_in_live_activity(laptop) -> None:
    """Service error shows its cause in live activity."""
    laptop["groq"].result = AnswerResult("", went_online=True, error=OnlineErrorKind.SERVICE_ERROR, error_detail=_TOOL_FAILED)
    laptop["say"]("who won the cricket world cup final yesterday")
    shown = " | ".join(format_record(laptop["logbook"].last_completed()).lines)
    assert "online search: service error (the model answered without running the web search)" in shown
    assert "groq" not in shown.lower()
