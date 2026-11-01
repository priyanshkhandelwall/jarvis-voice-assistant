"""Real voice commands: thirteen failures,
each fixed at its root cause and tested here with the EXACT words. Side
effects are mocked (apps, Chrome, the screen, the camera, the speakers'
volume, the speech engine, Groq -- a spy -- and the online classifier)."""

from __future__ import annotations

import os
import threading
import time
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from jarvis.actions import app_finder, file_index
from jarvis.actions.app_finder import ScoredMatch
from jarvis.actions.file_index import FileIndex, walk_root
from jarvis.controller import State
from jarvis.controller.trust import TrustLevel
from jarvis.dashboard.activity_feed import format_record
from jarvis.logbook.logbook import NOTHING_YET
from jarvis.news.topics import NewsClient, TopicCommands, TopicMonitor, TopicStore
from jarvis.router import validate_classification
from jarvis.router.commands import RouterCommands
from jarvis.router.router import CLASSIFIER_INTENTS, IntentRouter, load_intents
from jarvis.screen.capture import ScreenCapture, WindowInfo
from jarvis.screen.commands import ScreenCommands, window_name
from jarvis.screen.ocr import OcrLine, OcrWord
from jarvis.sensors import SensorFlags
from jarvis.settings import SETTINGS
from jarvis.speech.claims import ClaimCheck
from jarvis.speech.neural_tts import NeuralSpeechOutput
from jarvis.speech.qa import AnswerResult, source_hosts
from jarvis.speech.voice_cache import fixed_lines
from jarvis.system.commands import SystemCommands
from jarvis.vision import commands as vision_mod
from jarvis.vision.camera import Camera
from jarvis.vision.commands import VisionCommands, fuzzy_label
from jarvis.vision.detector import Detection, vote
from stretch_rig import SpyAnswerer, build, service
from test_f22_system import Clock, FakeSystem
from test_neural_tts import FakePlayer, FakeSynth

TODAY = date(2026, 11, 1)
_CAM = SETTINGS["camera"]["replies"]
THANK_YOU = [str(x).format(name=SETTINGS["user"]["name"]) for x in SETTINGS["small_talk"]["compliment_replies"]]
PROVIDERS = [str(n).lower() for n in SETTINGS["web"]["never_spoken_names"]] + [str(h) for h in SETTINGS["web"]["provider_hosts"]]

_APPS = [ScoredMatch("notepad", 0.0, "app", "notepad.exe")]


class _Groq(SpyAnswerer):
    """Test helper: Groq."""
    def __init__(self) -> None:
        super().__init__()
        self.live = []

    def today(self):
        """Fake today for the tests."""
        return TODAY

    def answer(self, question, on_going_online=None, length_rule=None):
        """Fake answer for the tests."""
        self.questions.append(question)
        if on_going_online is not None:
            on_going_online()
        return AnswerResult(self.reply, went_online=True)

    def answer_screen(self, question, screen_text, jpeg, model, instructions, on_going_online=None):
        """Fake answer screen for the tests."""
        self.questions.append(question)
        return AnswerResult("That is the JARVIS feature list.", went_online=True)

    def answer_live(self, question, on_going_online=None, should_stop=None, on_waiting=None):
        """The search provider's own link is the only page it reports."""
        self.live.append(question)
        if on_going_online is not None:
            on_going_online()
        return AnswerResult("Bitcoin is trading near 70,000 US dollars, according to coindesk.", went_online=True,
                            searched_for="bitcoin price", pages=("exa.ai",),
                            sources=("https://exa.ai/search?q=bitcoin+price",), tokens=900)


class FakeClassifier:
    """The online classifier, mocked: it would happily say camera_look."""

    def __init__(self) -> None:
        self.calls = []
        self.answers = {}

    def __call__(self, words, prompt, max_tokens):
        self.calls.append((words, prompt))
        reply = self.answers.get(words.lower(), '{"intent": "camera_look", "slots": {}, "confidence": 0.95}')
        return reply, AnswerResult("", went_online=True, tokens=40)


# -- the camera: three frames per look; the fridge shows up in one of them only ------------------

class _Capture:
    """Test helper: Capture."""
    def read(self):
        """Fake read for the tests."""
        return True, np.full((480, 640, 3), 120, dtype=np.uint8)

    def release(self) -> None:
        """Fake release for the tests."""
        pass


PERSON = (250, 50, 400, 470)
CUP = (40, 300, 110, 380)  # on the left
FRAMES = [
    [Detection("person", 0.90, PERSON), Detection("refrigerator", 0.63, (10, 0, 200, 470)), Detection("bottle", 0.46, (520, 300, 560, 400)),
     Detection("cup", 0.82, CUP)],
    [Detection("person", 0.91, PERSON), Detection("cup", 0.80, CUP)],
    [Detection("person", 0.89, PERSON), Detection("cup", 0.79, CUP), Detection("bottle", 0.47, (520, 300, 560, 400))],
]


class _FlickeringDetector:
    """Test helper: FlickeringDetector."""
    def __init__(self) -> None:
        self.calls = 0
        self.frames = FRAMES

    def detect(self, frame):
        """Fake detect for the tests."""
        found = self.frames[self.calls % len(self.frames)]
        self.calls += 1
        return list(found)

    def unload_if_idle(self):
        """Fake unload if idle for the tests."""
        return False


# -- the screen: Chrome on the feature list, and an untitled overlay window ---------------------

CHROME_WIN = WindowInfo("JARVIS feature list - Google Chrome", "chrome.exe", (0, 0, 800, 860))
UNTITLED_WIN = WindowInfo("", "", (800, 0, 1600, 860))


def _line(text, x, y):
    return OcrLine(text, (OcrWord(text.split()[0], x, y, 40, 12),))


class FakeScreen:
    """Screen stand-in: a fixed desktop image, windows and OCR lines."""
    def capture(self):
        """Fake capture for the tests."""
        from PIL import Image

        return ScreenCapture(image=Image.new("RGB", (1600, 900), (255, 255, 255)), windows=[CHROME_WIN, UNTITLED_WIN], origin=(0, 0))

    def ocr(self, image):
        """Fake ocr for the tests."""
        return [_line("quick start: the JARVIS feature list", 40, 420), _line("zebra notes", 900, 100)]


class DeferredSpeech:
    """Lines said only while something holds are kept for later, as a busy
    speech queue would: the test then plays them after the flow moved on."""

    def __init__(self, speech) -> None:
        self.speech = speech
        self.deferred = []

    def install(self) -> None:
        """Fake install for the tests."""
        def speak_while(text, still_valid):
            self.deferred.append((text, still_valid))
            return True

        self.speech.speak_while = speak_while


@pytest.fixture
def jarvis(tmp_path, monkeypatch):
    """Fixture: the production wiring with every side effect mocked (apps, Chrome, screen, camera, volume,
    speech, Groq)."""
    groq, classifier = _Groq(), FakeClassifier()
    sensors = SensorFlags(min_seconds=0)
    monkeypatch.setitem(vision_mod._CFG, "stream_detect_seconds", 0.05)
    detector = _FlickeringDetector()
    opened = []
    vision = VisionCommands(Camera(sensors, opener=lambda index: opened.append(index) or (_Capture() if index == 0 else None),
                                   blocked=lambda: False), detector, today=lambda: TODAY, price_table={})
    screen_fake = FakeScreen()
    fake_pc, clock = FakeSystem(), Clock()
    topics = TopicCommands(TopicMonitor(TopicStore(tmp_path / "topics.json"), NewsClient(service("News"), fetch=lambda u, t: b"")))
    rig = build(tmp_path, monkeypatch, groq=groq,
                screen=ScreenCommands(groq, sensors, capture_fn=screen_fake.capture, ocr_fn=screen_fake.ocr), vision=vision,
                topics=topics, system=SystemCommands(fake_pc, clock=clock, sleep=clock.sleep), router=RouterCommands(classify=classifier))
    monkeypatch.setattr(app_finder, "enumerate_installed_apps", lambda store_apps=None: list(_APPS))
    rig.update(classifier=classifier, vision=vision, detector=detector, pc=fake_pc, opened=opened, memory=tmp_path / "memory.json")
    yield rig
    vision.stop_stream()
    rig["c"].shutdown()


def _lines(rig):
    record = rig["logbook"].live_snapshot() or rig["logbook"].last_completed()
    return format_record(record).lines


def _shown(rig) -> str:
    return " | ".join(_lines(rig))


def _all_activity(rig) -> str:
    return " | ".join(line for record in rig["logbook"].records() for line in format_record(record).lines)


def _looked(rig) -> None:
    assert rig["say"]("What can you see") == "I can see a person and a cup."


# -- 1. briefing: never automatic ---------------------------------------------------------------


def test_woke_up_says_the_greeting_and_starts_nothing(tmp_path, monkeypatch) -> None:
    """Woke up says the greeting and starts nothing."""
    rig = build(tmp_path, monkeypatch, first_wake=True, start_state=State.SLEEPING)
    rig["wake"]()
    time.sleep(0.2)
    assert rig["c"].state == State.AWAKE and len(rig["speech"].texts) == 1
    assert "started by JARVIS" not in _all_activity(rig)
    rig["c"].shutdown()


# -- 2. camera false positives -------------------------------------------------------------------


def test_what_can_you_see_names_only_steady_objects(jarvis) -> None:
    """"detected: person 0.90, refrigerator 0.63, bottle 0.46" -- there is no
    fridge in his room. The fridge was in 1 frame of 3; the bottle never
    reached 0.6."""
    _looked(jarvis)
    shown = _shown(jarvis)
    assert "detected: person 0.90, cup 0.80" in shown
    assert "refrigerator" not in jarvis["speech"].texts[-1] and "bottle" not in jarvis["speech"].texts[-1]
    assert "not steady across 3 frames, dropped: bottle 0.46, bottle 0.47, refrigerator 0.63" in shown
    assert jarvis["detector"].calls == 3  # three frames, each detected


def test_a_steady_but_unsure_object_is_said_as_i_think(jarvis) -> None:
    """A steady but unsure object is said as i think."""
    jarvis["detector"].frames = [[Detection("person", 0.9, PERSON), Detection("bottle", 0.66, CUP)]] * 3
    assert jarvis["say"]("What can you see") == "I can see a person, and I think I see a bottle."
    jarvis["detector"].frames = [[Detection("bottle", 0.7, CUP)]] * 3
    assert jarvis["say"]("What can you see") == "I think I see a bottle."


def test_vote_keeps_two_of_three_and_counts_conservatively() -> None:
    """Vote keeps two of three and counts conservatively."""
    cup = lambda c: Detection("cup", c, CUP)  # noqa: E731
    frames = [[cup(0.9), cup(0.8)], [cup(0.85)], [], ]
    kept = vote(frames, 2, 0.6)
    assert [d.label for d in kept] == ["cup"]  # two cups in one frame only: one cup
    assert vote([[cup(0.9)], [], []], 2, 0.6) == []
    assert vote([[cup(0.59)], [cup(0.59)], [cup(0.59)]], 2, 0.6) == []


def test_camera_settings() -> None:
    """Camera settings."""
    cam = SETTINGS["camera"]
    assert cam["look_frames"] == 3 and cam["look_min_votes"] == 2
    assert cam["min_confidence"] == 0.6 and cam["sure_confidence"] == 0.75


# -- 3. "make it ..." is the volume, never the camera ------------------------------------------------


def test_make_it_max_after_a_look_sets_the_volume(jarvis) -> None:
    """Make it max after a look sets the volume."""
    _looked(jarvis)
    assert jarvis["say"]("Make it Max") == "Volume is now 100%."
    assert jarvis["pc"].volume == 100


def test_make_it_to_90_after_a_look_sets_the_volume(jarvis) -> None:
    """Make it to 90 after a look sets the volume."""
    _looked(jarvis)
    assert jarvis["say"]("Make it to 90") == "Volume is now 90%."
    assert jarvis["pc"].volume == 90


def test_make_it_louder_after_a_look_turns_the_volume_up(jarvis) -> None:
    """Make it louder after a look turns the volume up."""
    _looked(jarvis)
    assert jarvis["say"]("Make it louder") == "Volume is now 40%."


@pytest.mark.parametrize("said, volume", [("make it quieter", 20), ("turn it up", 40), ("turn it down", 20), ("make it 55", 55)])
def test_other_it_orders_are_volume_too(jarvis, said, volume) -> None:
    """Other it orders are volume too."""
    _looked(jarvis)
    jarvis["say"](said)
    assert jarvis["pc"].volume == volume


@pytest.mark.parametrize("said", ["how much is it", "is it on the left or right", "what brand is it"])
def test_real_camera_questions_about_it_still_work(jarvis, said) -> None:
    """Real camera questions about it still work."""
    _looked(jarvis)
    reply = jarvis["say"](said)
    assert "cup" in reply and jarvis["groq"].questions == []


# -- 4. the camera never comes from the classifier ---------------------------------------------------


def test_photosynthesis_in_detail_is_a_question_not_the_camera(jarvis) -> None:
    """Photosynthesis in detail is a question not the camera."""
    said = "So now can you please tell me what photos is in detail"
    jarvis["groq"].reply = "Photosynthesis is how plants make food from light."
    assert jarvis["say"](said) == "Photosynthesis is how plants make food from light."
    assert jarvis["opened"] == [] and jarvis["detector"].calls == 0  # the camera never opened
    assert jarvis["classifier"].calls == []  # "... in detail" is a question: not even classified
    assert "camera" not in _shown(jarvis)


def test_a_classifier_camera_answer_is_refused(jarvis) -> None:
    """A classifier camera answer is refused."""
    said = "could you check out the stuff around me please"
    reply = jarvis["say"](said)  # the mocked classifier says camera_look, 0.95
    assert jarvis["classifier"].calls and jarvis["opened"] == []
    assert jarvis["groq"].questions == [said] and reply == jarvis["groq"].reply


def test_camera_intents_are_not_offered_or_accepted() -> None:
    """Camera intents are not offered or accepted."""
    _cfg, intents = load_intents()
    assert not {"camera_look", "camera_on", "camera_tell"} & {name for name, _slot, _d in CLASSIFIER_INTENTS}
    assert validate_classification('{"intent": "camera_look", "slots": {}, "confidence": 0.99}', intents) is None


@pytest.mark.parametrize("said", ["what is photosynthesis", "tell me about volcanoes in detail", "what are black holes"])
def test_plain_questions_are_never_classified(said) -> None:
    """Plain questions are never classified."""
    assert IntentRouter.is_plain_question(said)


# -- 5. visual questions stay local --------------------------------------------------------------------


def test_where_is_the_cap_left_or_right_is_answered_from_the_look(jarvis) -> None:
    """Where is the cap left or right is answered from the look."""
    _looked(jarvis)
    assert jarvis["say"]("Where is the cap? Left or right") == "From the camera's view, the cup is on the left."
    assert jarvis["groq"].questions == [] and jarvis["classifier"].calls == []


@pytest.mark.parametrize("said, thing", [("where is the bottel", "bottle"), ("is there a laptop", "laptop"),
                                         ("where is my wallet left or right", "wallet")])
def test_things_not_seen_say_so_locally(jarvis, said, thing) -> None:
    """Things not seen say so locally."""
    _looked(jarvis)
    assert jarvis["say"](said).startswith(f"I didn't see a {thing} in my last look.")
    assert jarvis["groq"].questions == []


def test_is_there_a_cup_says_yes_and_where(jarvis) -> None:
    """Is there a cup says yes and where."""
    _looked(jarvis)
    assert jarvis["say"]("is there a cup") == "Yes, in my last look I saw a cup. From the camera's view, the cup is on the left."


@pytest.mark.parametrize("said, seen, label", [("cap", ["cup"], "cup"), ("bottel", [], "bottle"), ("fridge", [], "refrigerator"),
                                               ("cups", [], "cup"), ("galaxy", [], None)])
def test_fuzzy_object_names(said, seen, label) -> None:
    """Fuzzy object names."""
    assert fuzzy_label(said, seen) == label


def test_without_a_look_where_questions_are_ordinary(jarvis) -> None:
    """Without a look where questions are ordinary."""
    jarvis["say"]("where is the train station")
    assert jarvis["groq"].questions == ["where is the train station"]


def test_groq_may_never_place_or_see_things() -> None:
    """Groq may never place or see things."""
    claims = ClaimCheck(SETTINGS["claims"])
    assert claims.find("The cap is on the left.", "Where is the cap? Left or right") is not None
    assert claims.find("Australians drive on the left.", "which side do australians drive on") is None
    assert "Never say where an object near him is" in SETTINGS["claims"]["prompt_rule"]


# -- 6. the file index leaves JARVIS's own folders out -------------------------------------------------


def test_find_budget_lists_his_files_only(tmp_path, monkeypatch) -> None:
    """Find budget lists his files only."""
    project = tmp_path / "Jarvis"
    for rel in ("docs/backups/2026-10-23-pre-f10/jarvis/online/budget.py", "docs/spec-backups/budget.md", "test_runs/x/budget.txt",
                "venv/Lib/budget.py", "models/budget.onnx", "jarvis_data/budget.json", "submission/budget.py", "budget.py.bak",
                "Desktop/budget_example.xlsx"):
        (project / rel).parent.mkdir(parents=True, exist_ok=True)
        (project / rel).write_text("x", encoding="utf-8")
    monkeypatch.setattr(file_index, "_EXCLUDED_PATHS",
                        {os.path.normcase(str(project / p)) for p in SETTINGS["file_finder"]["excluded_project_folders"]})
    rig = build(tmp_path, monkeypatch, index=_index(project))
    reply = rig["say"]("Find Budget")
    assert "budget_example" in reply and "backups" not in reply and "found 13" not in reply
    rig["c"].shutdown()


def _index(root: Path) -> FileIndex:
    index = FileIndex(roots=[root])
    index.rebuild_now()
    return index


def test_the_real_project_folders_are_excluded() -> None:
    """The real project folders are excluded."""
    root = Path(file_index.__file__).resolve().parents[2]
    expected = {os.path.normcase(str(root / p)) for p in ("docs/backups", "docs/spec-backups", "test_runs", "venv", "models",
                                                          "jarvis_data", "submission")}
    assert expected <= file_index._EXCLUDED_PATHS
    walked = [path for _name, path in walk_root(root / "docs", folders=True)]
    assert not any(os.sep + "backups" + os.sep in p or os.sep + "spec-backups" in p for p in walked)
    assert ".bak" in SETTINGS["file_finder"]["skip_file_suffixes"]


# -- 7. "why" is about the last ACTION ------------------------------------------------------------------


def test_what_did_you_do_after_thanks_explains_the_last_action(jarvis) -> None:
    """What did you do after thanks explains the last action."""
    jarvis["say"]("open notepad")
    jarvis["say"]("Thanks")
    reply = jarvis["say"]("Can you tell me what did you do that")
    assert reply.startswith("I opened notepad because you asked me to open notepad.")


def test_why_with_nothing_done_yet(jarvis) -> None:
    """Why with nothing done yet."""
    jarvis["say"]("Thanks")
    jarvis["say"]("what time is it")
    assert jarvis["say"]("Can you tell me what did you do that") == NOTHING_YET == "I haven’t done anything yet."


# -- 8 and 9. screen find names the window; "where it is" follows locally ------------------------------


def test_find_quick_on_my_screen_names_the_window(jarvis) -> None:
    """Find quick on my screen names the window."""
    reply = jarvis["say"]("Can you please find quick on my screen")
    assert reply == "Yes, I can see 'quick' in Chrome – JARVIS feature list."
    assert " in ." not in reply


def test_a_window_with_no_name_is_on_your_screen(jarvis) -> None:
    """A window with no name is on your screen."""
    assert jarvis["say"]("find zebra on my screen") == "Yes, I can see 'zebra' on your screen."


@pytest.mark.parametrize("window, name", [(CHROME_WIN, "Chrome – JARVIS feature list"), (UNTITLED_WIN, ""),
                                          (WindowInfo("", "chrome.exe", (0, 0, 1, 1)), "Chrome"),
                                          (WindowInfo("Budget.xlsx - Excel", "EXCEL.EXE", (0, 0, 1, 1)), "Excel – Budget"),
                                          (WindowInfo("1234567890abcdef", "x.exe", (0, 0, 1, 1)), ""), (None, "")])
def test_window_names(window, name) -> None:
    """Window names."""
    assert window_name(window) == name


def test_where_it_is_after_a_find_is_answered_from_it(jarvis) -> None:
    """Where it is after a find is answered from it."""
    jarvis["say"]("Can you please find quick on my screen")
    assert jarvis["say"]("where it is") == "'quick' is in Chrome – JARVIS feature list, in the middle of the screen."
    assert jarvis["groq"].questions == []


def test_where_is_it_after_a_newer_camera_look_is_the_cameras(jarvis) -> None:
    """Where is it after a newer camera look is the cameras."""
    jarvis["say"]("Can you please find quick on my screen")
    _looked(jarvis)
    assert jarvis["say"]("where is it") == "From the camera's view, the cup is on the left."


# -- 10. the search provider is never shown -------------------------------------------------------------


def test_bitcoin_price_never_shows_the_provider(jarvis) -> None:
    """Bitcoin price never shows the provider."""
    jarvis["say"]("What is the price of Bitcoin")
    assert jarvis["groq"].live, "expected a live search"
    shown = _shown(jarvis).lower()
    assert "read 1 page" in shown
    for name in PROVIDERS:
        assert name not in shown, name
        assert name not in jarvis["speech"].texts[-1].lower(), name


@pytest.mark.parametrize("urls, hosts", [(["https://exa.ai/search?q=x"], ()), (["https://www.coindesk.com/price/bitcoin"], ("coindesk.com",)),
                                         (["https://exa.ai/r?url=https%3A%2F%2Fwww.bbc.co.uk%2Fnews"], ("bbc.co.uk",)),
                                         (["https://api.exa.ai/contents"], ())])
def test_source_hosts_drop_the_provider(urls, hosts) -> None:
    """Source hosts drop the provider."""
    assert source_hosts(urls) == hosts


# -- 11. no confirm prompt after the result -----------------------------------------------------------


def test_forget_everything_says_no_prompt_after_forgotten(jarvis) -> None:
    """Forget everything says no prompt after forgotten."""
    jarvis["say"]("Remember that my exam is on Monday")
    jarvis["say"]("Forget everything")
    assert jarvis["c"].pending_confirmation().trust_level == TrustLevel.L4
    deferred = DeferredSpeech(jarvis["speech"])
    deferred.install()
    jarvis["confirming"]("confirm")  # "Say confirm once more" -- held in the queue
    jarvis["orch"].remind_confirm_or_cancel()  # something unclear was heard meanwhile
    held = list(deferred.deferred)
    assert [t for t, _ok in held] == [SETTINGS["conversation"]["confirm_again_reply"], SETTINGS["conversation"]["confirm_reminder_reply"]]
    assert all(ok() for _t, ok in held)  # still due: the second confirm is awaited
    reply = jarvis["confirming"]("confirm")
    assert reply.startswith("Forgotten") or "forgot" in reply.lower()
    assert not any(ok() for _t, ok in deferred.deferred)  # the flow is over: none of them may be said now
    assert jarvis["orch"].remind_confirm_or_cancel() is False


def test_help_me_with_this_says_no_reminder_after_sending(jarvis) -> None:
    """Help me with this says no reminder after sending."""
    jarvis["say"]("Help me with this")
    assert jarvis["c"].state == State.CONFIRMING
    deferred = DeferredSpeech(jarvis["speech"])
    deferred.install()
    jarvis["orch"].remind_confirm_or_cancel()  # queued behind the prompt
    jarvis["confirming"]("confirm")
    assert deferred.deferred and not any(ok() for _t, ok in deferred.deferred)


def test_a_queued_line_whose_moment_passed_is_never_played() -> None:
    """The real speech engine: a reminder queued behind a long line is
    dropped when the confirmation ends before its turn."""
    out = NeuralSpeechOutput(FakeSynth(), player=FakePlayer(seconds_per_sentence=0.3))
    assert out.wait_until_loaded(5)
    pending = {"on": True}
    first = threading.Thread(target=out.speak, args=("Sending it online.",))
    first.start()
    time.sleep(0.05)
    said = []
    second = threading.Thread(target=lambda: said.append(out.speak_while("Say confirm or cancel.", lambda: pending["on"])))
    second.start()
    time.sleep(0.05)
    pending["on"] = False  # the confirm finished while the reminder waited
    first.join(5)
    second.join(5)
    assert said == [False]
    assert out._synth.sentences == ["Sending it online."]
    out.close()


# -- 12. out of range, locally ------------------------------------------------------------------------


@pytest.mark.parametrize("said, reply", [("Make the volume 200%", "Volume goes from 0 to 100."),
                                         ("brightness 150", "Brightness goes from 0 to 100."),
                                         ("make the brightness 150", "Brightness goes from 0 to 100."),
                                         ("crank the sound to two hundred", "Volume goes from 0 to 100.")])
def test_out_of_range_is_said_locally(jarvis, said, reply) -> None:
    """Out of range is said locally."""
    assert jarvis["say"](said) == reply
    assert jarvis["groq"].questions == [] and jarvis["classifier"].calls == []
    assert jarvis["pc"].volume == 30


# -- 13. compliments -------------------------------------------------------------------------------------


@pytest.mark.parametrize("said", ["You are so awesome", "good job", "you're the best", "Good job, Jarvis", "you are really helpful",
                                  "well done"])
def test_compliments_are_thanked_locally(jarvis, said) -> None:
    """Compliments are thanked locally."""
    assert jarvis["say"](said) in THANK_YOU
    assert jarvis["groq"].questions == [] and jarvis["classifier"].calls == []


@pytest.mark.parametrize("said", ["you are wrong", "you are so awesome, open notepad", "that was a good movie"])
def test_not_every_you_are_is_a_compliment(jarvis, said) -> None:
    """Not every you are is a compliment."""
    assert jarvis["say"](said) not in THANK_YOU


def test_compliment_replies_vary_and_are_cached() -> None:
    """Compliment replies vary and are cached."""
    assert len(set(THANK_YOU)) >= 3 and "Thank you, Priyansh." in THANK_YOU
    assert set(THANK_YOU) <= set(fixed_lines())
