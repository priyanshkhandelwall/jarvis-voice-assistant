"""Real voice commands (built-in mic), and
the general intent router that fixes its root cause: commands were fixed
phrase lists, so anything said differently fell through to Groq.

Every failure is a regression test with his EXACT words; the router is
also tested on paraphrases and mishearings across every intent, and the
whole log is replayed through the real pipeline (side effects mocked:
apps and Chrome, the screen, the camera, the speech engine, Open-Meteo, the
news feeds, and Groq -- a spy). The online classifier is always mocked."""

from __future__ import annotations

import json
from datetime import date

import flet as ft
import numpy as np
import pytest

from jarvis.actions import app_finder
from jarvis.actions.app_finder import ScoredMatch
from jarvis.actions.weather import WeatherMatcher
from jarvis.controller import State
from jarvis.controller.trust import TRUST_TABLE, TrustLevel
from jarvis.dashboard import app as dashboard
from jarvis.dashboard.activity_feed import format_record
from jarvis.logbook.records import redact_answer, redact_sensitive
from jarvis.memory.commands import clean_fact, fact_subject
from jarvis.memory.store import MemoryStore
from jarvis.news.topics import NewsClient, TopicCommands, TopicMonitor, TopicStore
from jarvis.online import OnlineStatus
from jarvis.router import IntentRouter, validate_classification
from jarvis.router.commands import RouterCommands
from jarvis.router.router import load_intents
from jarvis.screen.capture import ScreenCapture, WindowInfo
from jarvis.screen.commands import ScreenCommands, explain_instructions, meaningful
from jarvis.screen.ocr import OcrLine, OcrWord
from jarvis.sensors import SensorFlags
from jarvis.settings import SETTINGS
from jarvis.speech.claims import ClaimCheck
from jarvis.speech.length import sentence_limit
from jarvis.speech.mood import MoodWatcher
from jarvis.speech.qa import AnswerResult
from jarvis.speech.rudeness import RudenessFilter
from jarvis.speech.voice_cache import fixed_lines
from jarvis.system.commands import SystemCommands
from jarvis.vision import commands as vision_mod
from jarvis.vision.camera import Camera
from jarvis.vision.commands import VisionCommands
from jarvis.vision.detector import Detection
from jarvis.vision.prices import PriceEntry
from jarvis.voice.listener import VoiceListener
from stretch_rig import SpyAnswerer, build, service, wait
from test_f16a_weather import FakeOpenMeteo, _client
from test_f22_system import Clock, FakeSystem
from test_listener import FakeSTT, _utterance

TODAY = date(2026, 10, 31)
_CAM = SETTINGS["camera"]["replies"]
_MEM = SETTINGS["memory"]["replies"]
WELCOME = SETTINGS["small_talk"]["welcome_replies"]
OKAY = SETTINGS["small_talk"]["ack_replies"]
CHECK_INS = SETTINGS["mood"]["check_in_pool"]

_APPS = [ScoredMatch("Google Chrome", 0.0, "app", r"C:\Start\Google Chrome.lnk"),
         ScoredMatch("notepad", 0.0, "app", "notepad.exe"), ScoredMatch("paint", 0.0, "app", "mspaint.exe"),
         ScoredMatch("Spotify", 0.0, "app", r"C:\Start\Spotify.lnk")]


class _Groq(SpyAnswerer):
    """Groq as a spy. `replies` maps a question's words to a canned answer;
    answer_screen records the instructions it was given."""

    def __init__(self) -> None:
        super().__init__()
        self.replies = {}
        self.length_rules = []
        self.screen_instructions = []

    def today(self):
        """Fake today for the tests."""
        return TODAY

    def answer(self, question, on_going_online=None, length_rule=None):
        """Fake answer for the tests."""
        self.questions.append(question)
        self.length_rules.append(length_rule)
        if on_going_online is not None:
            on_going_online()
        return AnswerResult(self.replies.get(question, self.reply), went_online=True)

    def answer_screen(self, question, screen_text, jpeg, model, instructions, on_going_online=None):
        """Fake answer screen for the tests."""
        self.screen_instructions.append(instructions)
        return AnswerResult("That is a chat page about the JARVIS feature list.", went_online=True)


class FakeClassifier:
    """The online classifier, mocked: words -> the JSON it would return."""

    def __init__(self, answers=None) -> None:
        self.answers = answers or {}
        self.calls = []

    def __call__(self, words, prompt, max_tokens):
        self.calls.append((words, prompt, max_tokens))
        reply = self.answers.get(words.lower(), '{"intent": "general_question", "slots": {}, "confidence": 0.9}')
        return reply, AnswerResult("", went_online=True, tokens=50)


# -- a synthetic desktop: Chrome on claude.ai with its UI noise, and Word --------------------------

CHROME_WIN = WindowInfo("123456789_11112222333344445_6666777788889990000_n.jpg - Google Chrome", "chrome.exe", (0, 0, 800, 860))
WORD_WIN = WindowInfo("Report.docx - Word", "WINWORD.EXE", (800, 0, 1600, 860))
BANK_WIN = WindowInfo("NetBank - Commonwealth Bank", "msedge.exe", (0, 0, 10, 10), minimised=True)


def _line(text, x, y):
    return OcrLine(text, (OcrWord(text.split()[0], x, y, 40, 12),))


SCREEN_LINES = [
    _line("123456789_1111222€ X +", 20, 15),  # the tab strip
    _line("claude.ai/chat/7488d1c2-91f0-4b55", 120, 55),  # the address bar
    _line("Ask Gemini", 600, 55),  # a browser button
    _line("The JARVIS feature list is approved for submission.", 40, 300),
    _line("https://claude.ai/chat/7488d1c2", 40, 330),
    _line("Build id 7488d1c291f04b55a8c1", 40, 360),
    _line("Quarterly sales report", 820, 140),
    _line("Sales rose 12 percent in September.", 820, 170),
]


class FakeScreen:
    """Screen stand-in: a fixed desktop image, windows and OCR lines."""
    def capture(self):
        """Fake capture for the tests."""
        from PIL import Image

        return ScreenCapture(image=Image.new("RGB", (1600, 900), (255, 255, 255)), windows=[CHROME_WIN, WORD_WIN, BANK_WIN],
                             origin=(0, 0))

    def ocr(self, image):
        """Fake ocr for the tests."""
        return list(SCREEN_LINES)


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
        self.found = [Detection("person", 0.91, (250, 50, 400, 470)), Detection("cell phone", 0.80, (40, 200, 120, 330))]

    def detect(self, frame):
        """Fake detect for the tests."""
        return list(self.found)

    def unload_if_idle(self):
        """Fake unload if idle for the tests."""
        return False


_PRICES = {"cell phone": PriceEntry("cell phone", 150, 2000, "AUD", date(2026, 10, 23), "rough estimate")}


@pytest.fixture
def jarvis(tmp_path, monkeypatch):
    """Fixture: the production wiring with every side effect mocked (apps, Chrome, screen, camera, volume,
    speech, Groq)."""
    groq = _Groq()
    classifier = FakeClassifier()
    status = OnlineStatus()
    sensors = SensorFlags(min_seconds=0)
    screen_fake = FakeScreen()
    monkeypatch.setitem(vision_mod._CFG, "stream_detect_seconds", 0.05)
    vision = VisionCommands(Camera(sensors, opener=lambda index: _Capture() if index == 0 else None, blocked=lambda: False),
                            _Detector(), today=lambda: TODAY, price_table=_PRICES)
    store = TopicStore(tmp_path / "topics.json")
    topics = TopicCommands(TopicMonitor(store, NewsClient(service("News"), fetch=lambda url, timeout: b"")))
    fake_pc, clock = FakeSystem(), Clock()
    rig = build(tmp_path, monkeypatch, groq=groq, weather=WeatherMatcher(_client(FakeOpenMeteo(), status), status),
                screen=ScreenCommands(groq, sensors, capture_fn=screen_fake.capture, ocr_fn=screen_fake.ocr), vision=vision,
                topics=topics, system=SystemCommands(fake_pc, clock=clock, sleep=clock.sleep),
                router=RouterCommands(classify=classifier))
    monkeypatch.setattr(app_finder, "enumerate_installed_apps", lambda store_apps=None: list(_APPS))
    rig["orch"]._rudeness = RudenessFilter(SETTINGS["rudeness"]["words"])
    rig["orch"]._mood = MoodWatcher(SETTINGS["mood"], choose=lambda pool: pool[0])
    rig.update(classifier=classifier, vision=vision, topics=store, memory=tmp_path / "memory.json", pc=fake_pc)
    yield rig
    vision.stop_stream()
    rig["c"].shutdown()


def _lines(rig):
    record = rig["logbook"].live_snapshot() or rig["logbook"].last_completed()
    return format_record(record).lines


def _shown(rig) -> str:
    return " | ".join(_lines(rig))


def _items(rig):
    path = rig["memory"]
    return [i["text"] for i in json.loads(path.read_text(encoding="utf-8"))["items"]] if path.exists() else []


# == PART 1: the router -- paraphrases and mishearings across every intent ===========================

# (what he might say, the intent it must be understood as). None of these is a
# fixed phrase any matcher knows; each goes through the router.
PARAPHRASES = [
    ("fire up spotify", "open_app"), ("could you launch claude for me", "open_app"), ("bring up notepad", "open_app"),
    ("lanch paint", "open_app"), ("get me gmail", "open_app"),
    ("search the web for cheap flights to bali", "web_search"), ("look up the best pizza in sydney", "web_search"),
    ("put on some lofi on youtube", "youtube_play"), ("youtube relaxing piano music", "youtube_play"),
    ("where did i put my passport file", "find_file"), ("locate my resume document", "find_file"),
    ("is it gonna be chilly tomorrow", "weather"), ("will it be humid in sydney today", "weather"),
    ("tell me the time please", "time"), ("what's the date today then", "date"),
    ("i need to remember to buy milk", "remember"), ("make a note that the gym closes at nine", "remember"),
    ("venn is my exam", "recall"), ("wen is my dentist appointment", "recall"), ("what time is my flight", "recall"),
    ("what did you remember", "list_memory"), ("tell me everything you remembered", "list_memory"),
    ("what have you memorised", "list_memory"),
    ("please delete my gym note", "forget"), ("erase everything about my exams", "forget"),
    ("show me what's on the display", "screen_read"), ("read what's on the monitor", "screen_read"),
    ("help me understand this page", "screen_explain"), ("walk me through this window", "screen_explain"),
    ("what can you spot", "camera_look"), ("look through the webcam", "camera_look"),
    ("switch the webcam on", "camera_on"), ("kill the webcam", "camera_off"),
    ("tell me about the bottle", "camera_tell"), ("describe the laptop", "camera_tell"),
    ("crank the sound to sixty", "volume_set"), ("make the sound twenty", "volume_set"), ("make it louder", "volume_up"),
    ("sound down a bit", "volume_down"), ("silence the speakers", "mute"), ("turn the sound back on", "unmute"),
    ("set brightness at thirty", "brightness_set"), ("screen brighter please", "brightness_up"),
    ("make the screen darker", "brightness_down"), ("disconnect the wifi", "wifi_off"), ("switch wireless on", "wifi_on"),
    ("lock it up", "lock"), ("reboot the machine", "restart"), ("power off the laptop", "shutdown"),
    ("time for bed jarvis", "sleep"), ("tell me about yourself", "intro"), ("who are you", "identity_who"),
    ("what does jarvis stand for", "identity_stands_for"), ("who made you", "identity_creator"), ("what can you do", "identity_abilities"), ("how come you did that", "why"),
    ("what are you tracking", "topics_list"), ("which topics are you following", "topics_list"),
    ("check my tracked topics for news", "topics_check"), ("keep an eye on formula one", "topic_track"),
    ("give me the daily rundown", "briefing"), ("cheers for that", "thanks"), ("much appreciated", "thanks"),
]


@pytest.mark.parametrize("said, intent", PARAPHRASES)
def test_the_router_understands_paraphrases(said, intent) -> None:
    """The router understands paraphrases."""
    verdict, routes = IntentRouter().decide(said)
    assert verdict == "run" and routes[0].intent.name == intent, (said, verdict, [(r.intent.name, r.score) for r in routes])


def test_the_paraphrase_set_covers_every_intent_and_is_big_enough() -> None:
    """The paraphrase set covers every intent and is big enough."""
    _cfg, intents = load_intents()
    assert {i.name for i in intents} <= {intent for _said, intent in PARAPHRASES}
    assert len(PARAPHRASES) >= 50


@pytest.mark.parametrize("said", ["what is gravity", "when should i start revising for an exam", "how do i turn the volume up",
                                  "the tv is on mute", "who won the cricket", "explain how volcanoes work", "what is a life",
                                  "tell me about the history of india", "where is the train station"])
def test_questions_and_statements_are_never_routed_to_actions(said) -> None:
    """Questions and statements are never routed to actions."""
    assert IntentRouter().decide(said)[0] == "none"


def test_intent_vocabularies_are_data_not_code() -> None:
    """Intent vocabularies are data not code."""
    cfg, intents = load_intents()
    assert len(intents) >= 35 and cfg["mishearings"]["venn"] == "when"


# -- the classifier (online, mocked) ----------------------------------------------------------------


def test_the_classifier_reply_is_validated_strictly() -> None:
    """The classifier reply is validated strictly."""
    _cfg, intents = load_intents()
    ok = '{"intent": "open_app", "slots": {"rest": "claude"}, "confidence": 0.9}'
    assert validate_classification(ok, intents) == ("open_app", {"rest": "claude"}, 0.9)
    assert validate_classification('Sure! ' + ok, intents) is not None  # JSON found inside text
    for bad in ['{"intent": "delete_all_files", "slots": {}, "confidence": 1}', '{"intent": "open_app", "confidence": 2}',
                '{"intent": "open_app", "slots": {"rest": {"x": 1}}, "confidence": 0.9}', "not json",
                '{"intent": "open_app", "slots": {"rest": "<script>"}, "confidence": 0.9}', '["open_app"]',
                '{"intent": "open_app", "slots": {}, "confidence": "high"}']:
        assert validate_classification(bad, intents) is None, bad


def test_a_confident_classification_runs_locally_through_policy(jarvis) -> None:
    """A confident classification runs locally through policy."""
    said = "could you be a dear and get the music app going"
    jarvis["classifier"].answers[said] = '{"intent": "open_app", "slots": {"rest": "spotify"}, "confidence": 0.92}'
    # None of open_app's own words ("open", "launch"...) was said,
    # so even a confident classification asks first ("Have a look,
    # key." was classified as "open key" and opened the On-Screen Keyboard).
    assert jarvis["say"](said) == "Did you mean 'open spotify'?"
    assert jarvis["launched"] == []
    assert jarvis["say"]("yes") == "Opened Spotify."
    assert jarvis["launched"] == [r"C:\Start\Spotify.lnk"]
    assert jarvis["groq"].questions == []  # the answerer was never asked
    shown = " | ".join(format_record(r).lines[i] for r in jarvis["logbook"].records() for i in range(len(format_record(r).lines)))
    assert "understood as: not sure (none of its words were said) -- asking" in shown and "opening Spotify" in shown
    assert "groq" not in shown.lower()


def test_a_classified_restart_still_needs_its_two_confirms(jarvis) -> None:
    """A classified restart still needs its two confirms."""
    said = "could you get the whole computer going again from scratch"
    jarvis["classifier"].answers[said] = '{"intent": "restart", "slots": {}, "confidence": 0.95}'
    assert jarvis["say"](said).startswith("Did you mean")  # No "restart" word said -> asked first
    jarvis["say"]("yes")
    assert jarvis["c"].state == State.CONFIRMING  # L4: the model never assigns trust
    assert jarvis["c"].pending_confirmation().trust_level == TrustLevel.L4
    jarvis["orch"].handle_typed_command("cancel")


def test_an_unsure_classification_asks_first(jarvis) -> None:
    """An unsure classification asks first."""
    said = "could you get the music app going"
    jarvis["classifier"].answers[said] = '{"intent": "open_app", "slots": {"rest": "spotify"}, "confidence": 0.6}'
    assert jarvis["say"](said) == "Did you mean 'open spotify'?"
    assert jarvis["launched"] == []
    assert jarvis["say"]("yes") == "Opened Spotify."


def test_a_general_question_goes_to_the_normal_answer(jarvis) -> None:
    """A general question goes to the normal answer."""
    jarvis["groq"].reply = "A volcano erupts when pressure builds."
    assert jarvis["say"]("Explain how volcanoes work").startswith("A volcano erupts")
    # A how-question skips the classifier altogether
    # (one Groq call saved); it was "understood as: a general question" before.
    assert jarvis["classifier"].calls == []


def test_plain_world_questions_cost_no_classifier_call(jarvis) -> None:
    """Plain world questions cost no classifier call."""
    jarvis["say"]("what is gravity")
    assert jarvis["classifier"].calls == []
    assert jarvis["groq"].questions == ["what is gravity"]


def test_the_classifier_gets_only_his_words_and_the_intent_list(jarvis) -> None:
    """The classifier gets only his words and the intent list."""
    jarvis["say"]("Remember that my exam is on Monday")
    jarvis["say"]("explain the meaning of life briefly")
    words, prompt, max_tokens = jarvis["classifier"].calls[-1]
    assert words == "explain the meaning of life briefly"
    assert "monday" not in prompt.lower() and "exam" not in prompt.lower()  # no memory contents
    assert "open_app" in prompt and "general_question" in prompt and max_tokens <= 300


def test_a_sensitive_sentence_is_never_classified(jarvis) -> None:
    """A sensitive sentence is never classified."""
    jarvis["say"]("explain why my pin is 4821 not working")
    assert jarvis["classifier"].calls == []


# == PART 2: the specific bugs, with his exact words ===================================================


# -- 1. open unknown names ---------------------------------------------------------------------------


def test_open_claude_and_gemini_open_the_websites(jarvis) -> None:
    """Open claude and gemini open the websites."""
    assert jarvis["say"]("open claude") == "Opening Claude in Chrome."
    assert jarvis["say"]("Open Gemini") == "Opening Gemini in Chrome."
    assert [run[1] for run in jarvis["chrome"]] == ["https://claude.ai", "https://gemini.google.com"]


def test_open_cloud_asks_did_you_mean_claude(jarvis) -> None:
    """Open cloud asks did you mean claude."""
    assert jarvis["say"]("Open Cloud") == "Did you mean Claude?"
    assert jarvis["chrome"] == []
    assert jarvis["say"]("yes") == "Opening Claude in Chrome."


def test_open_plod_searches_for_it_in_chrome(jarvis) -> None:
    """Open plod searches for it in Chrome."""
    reply = jarvis["say"]("Open plod")
    assert reply == "I couldn't find an app called plod, so I've searched for it in Chrome."
    assert jarvis["chrome"][-1][1] == "https://www.google.com/search?q=plod"
    assert "http" not in reply  # never a URL spoken


# -- 2. memory corrections --------------------------------------------------------------------------


def test_actually_my_exam_is_on_wednesday_now_updates_every_exam_item(jarvis) -> None:
    """Actually my exam is on wednesday now updates every exam item."""
    store = MemoryStore(jarvis["memory"])
    for text in ("my exam is on tuesday", "my exam is on monday"):
        store.add(text)
    assert jarvis["say"]("Actually, my exam is on Wednesday now") == "Updated."
    assert _items(jarvis) == ["my exam is on wednesday"]


@pytest.mark.parametrize("said", ["my exam changed to friday", "remember my exam is on friday, not wednesday",
                                  "remember that my exam is now on friday"])
def test_other_corrections_also_replace(jarvis, said) -> None:
    """Other corrections also replace."""
    jarvis["say"]("Remember that my exam is on Monday")
    jarvis["say"](said if said.startswith("remember") else f"actually {said}")
    assert _items(jarvis) == ["my exam is friday" if "changed" in said else "my exam is on friday"]


def test_when_is_my_exam_gives_only_the_newest(jarvis) -> None:
    """When is my exam gives only the newest."""
    store = MemoryStore(jarvis["memory"])
    store.add("my exam is on tuesday")
    store.add("my exam is on monday")
    reply = jarvis["say"]("When is my exam")
    assert reply.startswith("You told me your exam is on monday.") and "tuesday" not in reply


def test_what_did_you_remember_lists_memory(jarvis) -> None:
    """What did you remember lists memory."""
    jarvis["say"]("Remember that my exam is on Monday")
    assert jarvis["say"]("What did you remember") == "I remember 1 thing: your exam is on monday."
    assert jarvis["groq"].questions == []


@pytest.mark.parametrize("text, subject", [("my exam is on monday", "exam"), ("my exams are on monday", "exam"),
                                           ("my dentist appointment is at 3", "appointment dentist"), ("to call mum", "")])
def test_fact_subjects(text, subject) -> None:
    """Fact subjects."""
    assert fact_subject(text) == subject


@pytest.mark.parametrize("said, kept", [("my exam is on wednesday now", "my exam is on wednesday"),
                                        ("my exam is on tuesday, not wednesday", "my exam is on tuesday"),
                                        ("my favourite colour is not blue", "my favourite colour is not blue")])
def test_correction_words_are_cleaned(said, kept) -> None:
    """Correction words are cleaned."""
    assert clean_fact(said) == kept


# -- 3. forget all, and bare numbers while a pick waits ---------------------------------------------


def test_forget_all_my_exams_forgets_every_one_after_one_confirm(jarvis) -> None:
    """Forget all my exams forgets every one after one confirm."""
    store = MemoryStore(jarvis["memory"])
    for text in ("my exam is on tuesday", "the exam room is 4b", "my exam notes are in the blue folder"):
        store.add(text)
    prompt = jarvis["say"]("Forget all my exams")
    assert prompt.startswith("I'll forget all 3 things about all your exams") or prompt.startswith("I'll forget all 3 things about your exams")
    assert jarvis["c"].pending_confirmation().trust_level == TrustLevel.L2
    assert jarvis["confirming"]("confirm") == "Forgotten."
    assert _items(jarvis) == []


def test_forget_memories_is_l2() -> None:
    """Forget memories is L2."""
    assert TRUST_TABLE["forget_memories"] == TrustLevel.L2


@pytest.mark.parametrize("heard", ["1", "One.", "number one", "the first"])
def test_a_bare_number_while_a_list_waits_is_never_too_short(jarvis, heard) -> None:
    """A bare number while a list waits is never too short."""
    store = MemoryStore(jarvis["memory"])
    store.add("to call mum")
    store.add("to call the dentist")
    jarvis["say"]("forget to call")  # "which one?"
    ignored = []
    listener = VoiceListener(jarvis["c"], jarvis["orch"].handle_command, stt=FakeSTT(heard),
                             ignored=lambda t, r: ignored.append((t, r)), pick_pending=jarvis["orch"].picks.active)
    listener.attach()
    try:
        _utterance(listener)
        assert wait(lambda: jarvis["speech"].texts[-1] == "Forgotten.")
        assert ignored == []
    finally:
        listener.detach()


# -- 4. screen reading never speaks junk ---------------------------------------------------------------


def test_whats_on_my_screen_skips_browser_ui_urls_and_ids(jarvis) -> None:
    """Whats on my screen skips browser UI URLs and IDs."""
    reply = jarvis["say"]("What's on my screen")
    for junk in ("123456789", "Ask Gemini", "claude.ai", "7488", "€", " X +", "http"):
        assert junk not in reply, (junk, reply)
    assert "Chrome showing an image" in reply  # The window in front, in plain words
    assert "Quarterly sales report" in reply or "The JARVIS feature list is approved" in reply


@pytest.mark.parametrize("line", ["123456789_1111222€ X +", "Ask Gemini", "claude.ai/chat/7488", "a1b2c3d4e5f6a7b8c9d0e1f2",
                                  "© 2026 | ™"])
def test_noise_lines_are_dropped(line) -> None:
    """Noise lines are dropped."""
    assert meaningful(line) == ""


def test_real_sentences_are_kept() -> None:
    """Real sentences are kept."""
    assert meaningful("Sales rose 12 percent in September.") == "Sales rose 12 percent in September."


# -- 5. explain my screen ------------------------------------------------------------------------------


def test_explain_my_screen_says_the_prompt_before_waiting_and_logs_what_was_hidden(jarvis) -> None:
    """Explain my screen says the prompt before waiting and logs what was hidden."""
    jarvis["say"]("Explain my screen")
    lines = _lines(jarvis)
    saying = next(i for i, l in enumerate(lines) if l.startswith("saying: I've put exactly what I'd send"))
    waiting = next(i for i, l in enumerate(lines) if l.startswith("waiting for confirm"))
    assert saying < waiting
    assert any(l.startswith("blacked out: ") for l in lines)
    jarvis["confirming"]("confirm")
    assert "never ask anyone to remove, lift or uncover it" in jarvis["groq"].screen_instructions[-1]
    assert "no window was blacked out" in jarvis["groq"].screen_instructions[-1]


def test_the_model_is_told_how_many_windows_are_hidden() -> None:
    """The model is told how many Windows are hidden."""
    assert "2 private window(s) were blacked out" in explain_instructions(2)


# -- 6. the false-claim filter keeps information ------------------------------------------------------


def test_explain_how_well_can_work_keeps_the_explanation(jarvis) -> None:
    """Explain how well can work keeps the explanation."""
    jarvis["groq"].reply = "Wells draw groundwater from an aquifer. A pump lifts it to the surface. I can open installed apps."
    reply = jarvis["say"]("Explain how well can work and detail")
    assert reply == "Wells draw groundwater from an aquifer. A pump lifts it to the surface."


def test_a_real_action_claim_is_still_replaced() -> None:
    """A real action claim is still replaced."""
    assert ClaimCheck(SETTINGS["claims"]).keep_information("Sure. I've opened Chrome for you.", "x") is None


# -- 7. redaction only for real secrets ---------------------------------------------------------------


VOLCANO = ("A volcano erupts when magma reaches the surface. Magma chambers secrete gases that build pressure. "
           "The secret of an eruption is that pressure.")


def test_the_volcano_answer_is_never_redacted(jarvis) -> None:
    """The volcano answer is never redacted."""
    jarvis["groq"].reply = VOLCANO
    jarvis["say"]("Explain how wall canos work in detail")
    record = jarvis["logbook"].last_completed()
    assert record.understood["answer"] == VOLCANO
    assert "[redacted]" not in " ".join(format_record(record).lines)
    assert "[redacted]" not in jarvis["debug"]()


@pytest.mark.parametrize("secret", ["your password is hunter2", "the pin: 4821", "key gsk_abcdefghijklmnop1234",
                                    "card 4111 1111 1111 1111"])
def test_real_secrets_in_an_answer_are_still_redacted(secret) -> None:
    """Real secrets in an answer are still redacted."""
    assert redact_answer(secret) == "[redacted]"


def test_his_own_words_keep_the_strict_rule() -> None:
    """His own words keep the strict rule."""
    assert redact_sensitive("what's the PIN") == "[redacted]"


# -- 8. an insult with nothing asked -----------------------------------------------------------------


def test_an_insult_with_no_command_is_answered_locally(jarvis) -> None:
    """An insult with no command is answered locally."""
    assert jarvis["say"]("What the hell you, useless idiot AI") == "That wasn't a nice way to say it."
    assert jarvis["groq"].questions == [] and jarvis["classifier"].calls == []


def test_an_insult_with_a_command_still_does_it(jarvis) -> None:
    """An insult with a command still does it."""
    assert jarvis["say"]("open notepad you useless idiot") == "That wasn't a nice way to say it, but here you go. Opened notepad."


# -- 9. mood on every path ----------------------------------------------------------------------------


def test_everything_is_broken_today_gets_a_check_in(jarvis) -> None:
    """Everything is broken today gets a check in."""
    assert jarvis["say"]("Everything is broken today, OpenNotePad") == f"Opened notepad. {CHECK_INS[0]}"


@pytest.mark.parametrize("calm", ["this is not broken", "open notepad", "is everything ok", "it is freezing outside"])
def test_calm_sentences_get_no_check_in(calm) -> None:
    """Calm sentences get no check in."""
    assert MoodWatcher(SETTINGS["mood"]).score(calm).score < float(SETTINGS["mood"]["threshold"])


def test_mood_counts_on_a_routed_command_too(jarvis) -> None:
    """Mood counts on a routed command too."""
    assert jarvis["say"]("everything keeps failing, fire up notepad") == f"Opened notepad. {CHECK_INS[0]}"


# -- 10. length --------------------------------------------------------------------------------------


def test_machine_learning_in_one_life_is_one_sentence(jarvis) -> None:
    """Machine learning in one life is one sentence."""
    jarvis["groq"].reply = "Machine learning teaches computers from data. It finds patterns. It improves over time."
    assert jarvis["say"]("What is Machine Learning in One Life") == "Machine learning teaches computers from data."
    assert jarvis["groq"].length_rules[-1] == SETTINGS["length"]["one_sentence_instruction"]


@pytest.mark.parametrize("said, limit", [("explain dns in one lie", 1), ("what is rust, briefly", 1), ("explain tcp quickly", 1),
                                         ("explain gravity in details", 6), ("explain how well can work and detail", 6),
                                         ("what is a life", None), ("tell me about the line of succession", None)])
def test_fuzzy_length_requests(said, limit) -> None:
    """Fuzzy length requests."""
    assert sentence_limit(said) == limit


# -- 11. small talk -------------------------------------------------------------------------------------


@pytest.mark.parametrize("said", ["Thank you", "thanks jarvis", "cheers mate", "thank you so much"])
def test_thanks_is_local(jarvis, said) -> None:
    """Thanks is local."""
    assert jarvis["say"](said) in WELCOME
    assert jarvis["groq"].questions == []


def test_ok_is_local(jarvis) -> None:
    """Ok is local."""
    assert jarvis["say"]("ok") in OKAY
    assert jarvis["groq"].questions == []


def test_small_talk_replies_are_cached_lines() -> None:
    """Small talk replies are cached lines."""
    assert set(WELCOME) | set(OKAY) <= set(fixed_lines())


# -- 12. camera limits ------------------------------------------------------------------------------------


def _looked(jarvis) -> None:
    assert jarvis["say"]("What can you see") == "I can see a person and a cell phone."


@pytest.mark.parametrize("said", ["Is it iPhone", "Am I holding an iPhone", "what model is this", "is this a samsung"])
def test_brand_and_model_are_never_guessed(jarvis, said) -> None:
    """Brand and model are never guessed."""
    _looked(jarvis)
    assert jarvis["say"](said) == "I can tell it's a cell phone, but not the brand or model."
    assert jarvis["groq"].questions == []


@pytest.mark.parametrize("said", ["is the person good", "Tell me about the person you saw", "is he nice", "how old is that man"])
def test_people_are_never_judged_or_identified(jarvis, said) -> None:
    """People are never judged or identified."""
    _looked(jarvis)
    assert jarvis["say"](said) == _CAM["no_faces"]
    assert jarvis["groq"].questions == []


def test_tell_me_about_the_cell_phone(jarvis) -> None:
    """Tell me about the cell phone."""
    _looked(jarvis)
    reply = jarvis["say"]("Tell me about the cell phone")
    assert reply.startswith("In my last look I saw a cell phone. From the camera's view, the cell phone is on the left.")
    assert "$150" in reply
    assert jarvis["groq"].questions == []


def test_the_newest_iphone_is_still_an_ordinary_question(jarvis) -> None:
    """The newest iphone is still an ordinary question."""
    jarvis["say"]("what is the newest iphone")
    assert jarvis["groq"].questions == ["what is the newest iphone"]


# -- 13 and 14 -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("said", ["Venn is my exam", "Ven is my exam", "wen is my exam", "when is my exam"])
def test_venn_is_my_exam(jarvis, said) -> None:
    """Venn is my exam."""
    jarvis["say"]("Remember that my exam is on Monday")
    assert jarvis["say"](said).startswith("You told me your exam is on monday.")
    assert jarvis["groq"].questions == []


def test_what_are_you_tracking_lists_topics(jarvis) -> None:
    """What are you tracking lists topics."""
    jarvis["say"]("track indian cricket")
    assert jarvis["say"]("What are you tracking") == "You're tracking one topic: indian cricket."
    assert jarvis["groq"].questions == []


# -- 15. the dashboard at half-screen width -----------------------------------------------------------


def test_sensor_labels_are_one_line_with_ellipsis() -> None:
    """Sensor labels are one line with ellipsis."""
    row = dashboard._sensor_row(ft.Icons.MIC, "Microphone", None, None, subtitle=ft.Text("off - only when you ask"))
    texts = [c for c in _walk(row) if isinstance(c, ft.Text)]
    assert {t.value for t in texts} == {"Microphone", "off - only when you ask"}
    for t in texts:
        assert t.max_lines == 1 and t.no_wrap and t.overflow == ft.TextOverflow.ELLIPSIS, t.value


def test_copy_log_never_shrinks() -> None:
    """Copy log never shrinks."""
    button = ft.Container(content=ft.Text("COPY LOG", no_wrap=True, max_lines=1))
    header = dashboard._live_activity_header(button)
    title_box, copy = header.controls
    assert title_box.expand and title_box.content.overflow == ft.TextOverflow.ELLIPSIS  # the title gives way first
    assert copy is button and not getattr(copy, "expand", False)


def _walk(control):
    yield control
    for attr in ("content", "controls"):
        child = getattr(control, attr, None)
        for c in (child if isinstance(child, list) else [child] if child is not None else []):
            yield from _walk(c)


# == PART 3: the whole replayed ===============================================================

# (utterance, what it must be understood as, a check on what she says). Lines in
# brackets are his own earlier commands from the same log that set things up.
REPLAY = [
    ("Open Cloud", "ask: open Claude?", lambda r: r == "Did you mean Claude?"),
    ("Open plod", "open_url (Google search)", lambda r: r == "I couldn't find an app called plod, so I've searched for it in Chrome."),
    ("open claude", "open_url claude.ai", lambda r: r == "Opening Claude in Chrome."),
    ("Open Gemini", "open_url gemini.google.com", lambda r: r == "Opening Gemini in Chrome."),
    ("Explain how well can work and detail", "general question (6 sentences)",
     lambda r: r == "Wells draw groundwater from an aquifer. A pump lifts it to the surface."),
    ("Explain how wall canos work in detail", "general question (6 sentences)", lambda r: r == VOLCANO),
    ("[Remember that my exam is on Monday]", "remember", lambda r: r == "Got it."),
    ("Venn is my exam", "recall (exam)", lambda r: r.startswith("You told me your exam is on monday.")),
    ("Actually, my exam is on Wednesday now", "remember (correction)", lambda r: r == "Updated."),
    ("What did you remember", "list_memory", lambda r: r == "I remember 1 thing: your exam is on wednesday."),
    ("Forget all my exams", "forget (one item left)", lambda r: r == "Forgotten."),
    ("1", "no list pending", lambda r: "list" in r.lower()),
    ("Everything is broken today, OpenNotePad", "open_app notepad + check-in", lambda r: r == f"Opened notepad. {CHECK_INS[0]}"),
    ("What the hell you, useless idiot AI", "insult, nothing asked", lambda r: r == "That wasn't a nice way to say it."),
    ("What's on my screen", "capture_screen", lambda r: "Chrome showing an image" in r and "Ask Gemini" not in r),
    ("What I am looking right now", "capture_screen", lambda r: "Chrome showing an image" in r and "123456789" not in r),
    ("Explain my screen", "send_screen_online (L3 preview)", lambda r: r.startswith("I've put exactly what I'd send")),
    ("[confirm]", "confirm", lambda r: r.startswith("That is a chat page")),
    ("[What can you see]", "capture_camera", lambda r: r == "I can see a person and a cell phone."),
    ("Is it on left or right", "camera follow-up", lambda r: r == "From the camera's view, the cell phone is on the left."),
    ("Tell me about the cell phone", "camera_tell", lambda r: r.startswith("In my last look I saw a cell phone.")),
    ("Tell me about the person you saw", "camera: no faces", lambda r: r == _CAM["no_faces"]),
    ("is the person good", "camera: no faces", lambda r: r == _CAM["no_faces"]),
    ("Am I holding an iPhone", "camera: no brands", lambda r: r == "I can tell it's a cell phone, but not the brand or model."),
    ("Is it iPhone", "camera: no brands", lambda r: r == "I can tell it's a cell phone, but not the brand or model."),
    ("[track indian cricket]", "topic_track", lambda r: r == "Tracking indian cricket."),
    ("What are you tracking", "topics_list", lambda r: r == "You're tracking one topic: indian cricket."),
    ("What is Machine Learning in One Life", "general question (1 sentence)", lambda r: r == "Machine learning teaches computers from data."),
    ("Thank you", "small talk", lambda r: r in WELCOME),
    ("ok", "small talk", lambda r: r in OKAY),
]
ONLINE_REPLIES = {
    "explain how well can work and detail":
        "Wells draw groundwater from an aquifer. A pump lifts it to the surface. I can open installed apps.",
    "explain how wall canos work in detail": VOLCANO,
    "what is machine learning in one life": "Machine learning teaches computers from data. It finds patterns. It improves over time.",
}


def test_the_whole_log_replays_correctly(jarvis) -> None:
    """The whole log replays correctly."""
    jarvis["groq"].replies.update(ONLINE_REPLIES)
    wrong = []
    for said, meant, check in REPLAY:
        words = said.strip("[]")
        reply = jarvis["confirming"](words) if jarvis["c"].state == State.CONFIRMING else jarvis["say"](words)
        if not check(reply):
            wrong.append((said, meant, reply))
    assert wrong == []
    # Online: only the three real questions were answered; nothing local went out.
    assert jarvis["groq"].questions == list(ONLINE_REPLIES)
