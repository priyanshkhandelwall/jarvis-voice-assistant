"""Real voice commands with the EXACT
words: identity (F1), file ranking (F4), the weather's city (F16a), memory
corrections, never-store and the confirm order (F10), the screen summary
(F8), picks from a summarise list (F19), and the small leftovers (F9, F20,
F18, F16b). Side effects mocked as in test_regressions_core_rig."""

from __future__ import annotations

import os
import queue
import threading
import time

import pytest

from jarvis.actions.command_text import clean_command
from jarvis.actions.file_index import FileIndex, rank, split_type_words
from jarvis.actions.intro import load_intro_text
from jarvis.browser.commands import parse_search
from jarvis.controller import State
from jarvis.controller.trust import TrustLevel
from jarvis.memory.commands import MemoryCommands
from jarvis.memory.sensitive import refusal_text, sensitive_type
from jarvis.memory.store import MemoryStore
from jarvis.screen.capture import ScreenCapture, WindowInfo
from jarvis.screen.commands import ScreenCommands, best_line
from jarvis.screen.ocr import OcrLine, OcrWord
from jarvis.sensors import SensorFlags
from jarvis.settings import SETTINGS
from jarvis.speech.output import SpeechOutput
from jarvis.speech.qa import build_system_prompt
from stretch_rig import SpyAnswerer, build, service
from test_f16a_weather import FakeOpenMeteo, _geo, _matcher
from test_f18_youtube import Clock as TubeClock, FakeApi, Fetched, Snippet, SummaryAnswerer
from test_f19_files import Clock as DeskClock, Summariser
from test_regressions_core_rig import PERSON, jarvis  # noqa: F401 -- the fixture

_ID = SETTINGS["identity"]
_MEM = SETTINGS["memory"]["replies"]
_CONV = SETTINGS["conversation"]
WHO = ("I am JARVIS, which stands for Just A Rather Very Intelligent System. Think of me as a personal assistant with a "
       "stage name, and a creator, Priyansh, who wanted a computer that listens.")


def _local(rig) -> None:
    assert rig["groq"].questions == [] and rig["classifier"].calls == []


# == F1 identity: local, never online ==========================================================


def test_what_does_jarvis_stand_for(jarvis) -> None:  # noqa: F811
    """What does JARVIS stand for."""
    assert jarvis["say"]("What does Jarvis stand for") == "JARVIS stands for Just A Rather Very Intelligent System."
    _local(jarvis)


@pytest.mark.parametrize("said", ["Who you at", "who are you", "Who are you?", "what are you"])
def test_who_are_you_and_its_mishearing_is_the_short_self_description(jarvis, said) -> None:  # noqa: F811
    """Who are you and its mishearing is the short self description."""
    assert jarvis["say"](said) == WHO
    _local(jarvis)


def test_who_made_you_and_what_can_you_do(jarvis) -> None:  # noqa: F811
    """Who made you and what can you do."""
    assert jarvis["say"]("who made you") == "Priyansh created me."
    assert jarvis["say"]("who created you") == "Priyansh created me."
    assert jarvis["say"]("what can you do") == _ID["abilities_reply"]
    _local(jarvis)


def test_another_acronym_is_still_a_question(jarvis) -> None:  # noqa: F811
    """Another acronym is still a question."""
    jarvis["say"]("what does NASA stand for")
    assert jarvis["groq"].questions == ["what does nasa stand for"]


def test_introduce_yourself_is_still_the_whole_intro(jarvis) -> None:  # noqa: F811
    """Introduce yourself is still the whole intro."""
    assert jarvis["say"]("introduce yourself") == load_intro_text()


def test_the_online_prompt_carries_the_exact_expansion_and_creator() -> None:
    """The online prompt carries the exact expansion and creator."""
    for prompt in (build_system_prompt(), build_system_prompt([("open apps", True)])):
        assert '"Just A Rather Very Intelligent System"' in prompt and "created by Priyansh" in prompt


# == F4 ranking: documents first, code only when asked ==========================================


@pytest.fixture
def reports(tmp_path, monkeypatch):
    """Fixture: Report.pdf, report.docx and three code files with set modified times, indexed."""
    files = tmp_path / "files"
    files.mkdir()
    names = ["Report.pdf", "report.docx", "make_report.py", "final_report.rmd", "final_report.html"]
    for age, name in enumerate(names):
        (files / name).write_bytes(b"x")
        stamp = time.time() - 86400 * (age + 1)
        os.utime(files / name, (stamp, stamp))
    os.utime(files / "report.docx", (time.time(), time.time()))  # the newest
    index = FileIndex(roots=[files])
    index.rebuild_now()
    rig = build(tmp_path, monkeypatch, index=index)
    rig.update(index=index, files=files)
    yield rig
    rig["c"].shutdown()


def _listed(rig):
    from jarvis.dashboard.activity_feed import format_record

    return [line for line in format_record(rig["logbook"].last_completed()).lines if line[:2] in ("1.", "2.", "3.", "4.", "5.")]


def test_can_you_please_find_my_report_lists_documents_only(reports) -> None:
    """Can you please find my report lists documents only."""
    assert reports["say"]("can you please find my report") == "I found 2 files. Which one would you like to open?"
    assert _listed(reports) == ["1. report.docx", "2. Report.pdf"]  # exact names, the newest first; no code


def test_code_files_when_asked_for_their_kind(reports) -> None:
    """Code files when asked for their kind."""
    reply = reports["say"]("find the python file report")
    assert reports["launched"] or "make_report.py" in reply or _listed(reports) == ["1. make_report.py"]
    assert split_type_words("python report") == ("report", (".py", ".pyw", ".ipynb"))


def test_code_files_when_no_document_matches(reports) -> None:
    """Code files when no document matches."""
    reports["say"]("find final report")
    assert {line.split(". ", 1)[1] for line in _listed(reports)} == {"final_report.rmd", "final_report.html"}


def test_exact_and_whole_word_names_rank_above_partial_ones(tmp_path) -> None:
    """Exact and whole word names rank above partial ones."""
    from jarvis.actions.file_index import FileMatch

    partial, whole = FileMatch("reports2024.pdf", str(tmp_path / "a"), 0.8), FileMatch("final report.pdf", str(tmp_path / "b"), 0.9)
    exact = FileMatch("report.pdf", str(tmp_path / "c"), 1.0)
    assert [m.filename for m in rank([partial, whole, exact])] == ["report.pdf", "final report.pdf", "reports2024.pdf"]


def test_ranking_is_data() -> None:
    """Ranking is data."""
    cfg = SETTINGS["file_finder"]
    assert ".pdf" in cfg["document_suffixes"] and ".py" in cfg["code_suffixes"] and ".rmd" in cfg["code_suffixes"]
    assert ".html" in cfg["type_words"]["html"]


# == F16a the city from anywhere in the sentence ==================================================

_MELBOURNE = _geo(("Melbourne", "Victoria", "Australia", "AU", 4917750, -37.81, 144.96, "Australia/Melbourne"))
_PERTH = _geo(("Perth", "Western Australia", "Australia", "AU", 2059484, -31.95, 115.86, "Australia/Perth"))
_BRISBANE = _geo(("Brisbane", "Queensland", "Australia", "AU", 2514184, -27.47, 153.03, "Australia/Brisbane"))


def _weather():
    return _matcher(FakeOpenMeteo(places={"melbourne": _MELBOURNE, "perth": _PERTH, "brisbane": _BRISBANE}))[0]


@pytest.mark.parametrize("said, city", [("How hot will Melbourne be tomorrow", "Melbourne"), ("Melbourne weather tomorrow", "Melbourne"),
                                        ("is it raining in Perth", "Perth"), ("will Brisbane be cold", "Brisbane")])
def test_the_place_named_anywhere_is_used(said, city) -> None:
    """The place named anywhere is used."""
    reply = _weather().matcher(clean_command(said)).text
    assert city in reply and "Sydney" not in reply


def test_how_hot_will_melbourne_be_tomorrow_twice() -> None:
    """How hot will melbourne be tomorrow twice."""
    m = _weather()
    for _ in range(2):
        assert m.matcher(clean_command("How hot will Melbourne be tomorrow")).text.startswith("Tomorrow in Melbourne")


def test_sydney_only_when_no_place_is_named() -> None:
    """Sydney only when no place is named."""
    assert "Sydney" in _weather().matcher(clean_command("how hot will it be tomorrow")).text


def test_a_word_that_is_not_a_place_is_not_weather() -> None:
    """A word that is not a place is not weather."""
    assert _weather().matcher(clean_command("will the soup be hot")) is None


# == F10 memory ==================================================================================


def test_actually_it_is_on_wednesday_now_updates_the_meeting(jarvis) -> None:  # noqa: F811
    """Actually it is on wednesday now updates the meeting."""
    assert jarvis["say"]("Remember, my meeting is on Monday") == _MEM["got_it"]
    assert jarvis["say"]("Actually, it is on Wednesday now") == "Updated."
    listing = jarvis["say"]("what do you remember")
    assert "meeting is on wednesday" in listing and "monday" not in listing and "it is on" not in listing
    assert listing.startswith(_MEM["list_one"])


@pytest.mark.parametrize("said, day", [("it changed to Friday", "friday"), ("no, it's on Thursday", "thursday"),
                                       ("Actually, it's on Tuesday now", "tuesday")])
def test_other_pronoun_corrections(jarvis, said, day) -> None:  # noqa: F811
    """Other pronoun corrections."""
    jarvis["say"]("Remember, my meeting is on Monday")
    assert jarvis["say"](said) == "Updated."
    assert day in jarvis["say"]("what do you remember")


def test_it_after_a_recall_means_that_memory(jarvis) -> None:  # noqa: F811
    """It after a recall means that memory."""
    jarvis["say"]("Remember, my meeting is on Monday")
    jarvis["say"]("what do you know about my meeting")
    assert jarvis["say"]("actually it moved to Friday") == "Updated."


def test_it_is_only_resolved_within_two_minutes(tmp_path) -> None:
    """It is only resolved within two minutes."""
    now = [0.0]
    memory = MemoryCommands(MemoryStore(tmp_path / "m.json"), clock=lambda: now[0])
    memory._note_subject("my meeting is on monday")
    assert memory._resolve_pronoun("actually it is on wednesday now".split()) == "my meeting is on wednesday now"
    now[0] = SETTINGS["memory"]["pronoun_window_seconds"] + 1
    assert memory._resolve_pronoun("actually it is on wednesday now".split()) is None


def test_it_without_a_change_word_is_not_a_correction(tmp_path) -> None:
    """It without a change word is not a correction."""
    memory = MemoryCommands(MemoryStore(tmp_path / "m.json"))
    memory._note_subject("my meeting is on monday")
    assert memory._resolve_pronoun("it is cold today".split()) is None


def test_remember_my_bin_44555_is_refused(jarvis) -> None:  # noqa: F811
    """Remember my bin 44555 is refused."""
    assert jarvis["say"]("Remember my bin 44555") == "I won't store that. It looks like a PIN."
    assert jarvis["say"]("what do you remember") == _MEM["list_none"]
    assert "44555" not in jarvis["debug"]()


@pytest.mark.parametrize("said, kind", [("my bin 44555", "PIN"), ("my pen is 4321", "PIN"), ("my pinn 1234", "PIN"),
                                        ("my passward is 99887", "password"), ("my locker 4402", "code"),
                                        ("my gym code 1234", "code")])
def test_never_store_slips_and_codes(said, kind) -> None:
    """Never store slips and codes."""
    assert sensitive_type(said) == kind


def test_a_code_is_refused_in_plain_words() -> None:
    """A code is refused in plain words."""
    assert refusal_text(sensitive_type("my locker 4402")) == "That looks like a code, so I won't store it."


@pytest.mark.parametrize("said", ["my room is 4402", "my meeting is on monday", "in room 4402 on monday", "buy a pen tomorrow",
                                  "my flight number 1234"])
def test_ordinary_numbers_are_stored(said) -> None:
    """Ordinary numbers are stored."""
    assert sensitive_type(said) is None


class QueuedSpeech(SpeechOutput):
    """A real speech queue: one line at a time, each takes `seconds`; a
    speak_while line is checked when it reaches the front."""

    def __init__(self, seconds: float = 0.3) -> None:
        self.played, self.seconds, self._jobs = [], seconds, queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        """Fake run for the tests."""
        while True:
            text, valid, done, said = self._jobs.get()
            if valid is None or valid():
                self.played.append(text)
                said.append(True)
                time.sleep(self.seconds)
            done.set()

    def _put(self, text, valid) -> bool:
        """Fake put for the tests."""
        done, said = threading.Event(), []
        self._jobs.put((text, valid, done, said))
        done.wait(5)
        return bool(said)

    def speak(self, text: str) -> None:
        """Fake speak for the tests."""
        self._put(text, None)

    def speak_while(self, text, still_valid) -> bool:
        """Fake speak while for the tests."""
        return still_valid() and self._put(text, still_valid)


def _forget_everything(rig) -> QueuedSpeech:
    for fact in ("Remember, my meeting is on Monday", "Remember my exam is on Friday", "Remember to call mum"):
        rig["say"](fact)
    rig["say"]("Forgot everything")
    assert rig["c"].pending_confirmation().trust_level == TrustLevel.L4
    speech = QueuedSpeech()
    rig["orch"]._speech = speech
    return speech


def _confirm(rig) -> threading.Thread:
    thread = threading.Thread(target=rig["orch"].handle_command, args=("confirm", "voice"))
    thread.start()
    return thread


def _until(check, seconds: float = 2.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not check():
        time.sleep(0.01)
    return check()


def test_confirm_confirm_says_the_prompt_before_the_result(jarvis) -> None:  # noqa: F811
    """A real session: "Forgotten: all 3", then "Say confirm once more to go ahead.",
    then "Forgotten." -- the second confirm ran while the prompt played."""
    speech = _forget_everything(jarvis)
    first = _confirm(jarvis)
    assert _until(lambda: speech.played)  # the prompt is being said
    second = _confirm(jarvis)  # "confirm" again, straight away
    first.join(5)
    second.join(5)
    assert speech.played == [_CONV["confirm_again_reply"], _MEM["forget_all_done"]]
    activity = [line for record in jarvis["logbook"].records() for line in __import__(
        "jarvis.dashboard.activity_feed", fromlist=["format_record"]).format_record(record).lines]
    assert activity.index(f"saying: {_CONV['confirm_again_reply']}") < next(i for i, l in enumerate(activity) if l.startswith("Forgotten: all"))
    assert _until(lambda: jarvis["c"].state == State.AWAKE)


def test_a_second_confirm_heard_first_drops_the_prompt(jarvis) -> None:  # noqa: F811
    """A second confirm heard first drops the prompt."""
    speech = _forget_everything(jarvis)
    busy = threading.Thread(target=speech.speak, args=("Something she was already saying.",))
    busy.start()
    assert _until(lambda: speech.played)
    first = _confirm(jarvis)  # its prompt waits behind the line above
    time.sleep(0.05)
    second = _confirm(jarvis)  # heard before the prompt's turn
    for thread in (busy, first, second):
        thread.join(5)
    assert speech.played == ["Something she was already saying.", _MEM["forget_all_done"]]  # no prompt, never after the result


# == F8 what's on my screen: the window in front only =============================================


def _ocr_line(text, x, y):
    return OcrLine(text, (OcrWord(text.split()[0], x, y, 40, 12),))


class LogScreen:
    """His screen at 18:17: Chrome in front, WhatsApp (twice) behind it, VS Code minimised."""

    chrome = WindowInfo("Melbourne forecast - Google Chrome", "chrome.exe", (0, 0, 1600, 900))
    whatsapp = WindowInfo("WhatsApp", "whatsapp.exe", (100, 100, 700, 700))
    whatsapp2 = WindowInfo("WhatsApp", "whatsapp.exe", (200, 100, 800, 700))
    vscode = WindowInfo("main.py - Jarvis - Visual Studio Code", "code.exe", (0, 0, 0, 0), minimised=True)

    def capture(self):
        """Fake capture for the tests."""
        from PIL import Image

        return ScreenCapture(image=Image.new("RGB", (1600, 900), (255, 255, 255)),
                             windows=[self.chrome, self.whatsapp, self.whatsapp2, self.vscode], origin=(0, 0))

    def ocr(self, image):
        """Fake ocr for the tests."""
        return [_ocr_line("X + Ask Gemini", 40, 30), _ocr_line("bom.gov.au/places/vic/melbourne", 300, 60),
                _ocr_line("Sam, Priya, Alex, Jordan", 40, 200), _ocr_line("Rain 4mm", 40, 240),
                _ocr_line("Melbourne will be mostly sunny tomorrow with a top of 24 degrees", 40, 300),
                _ocr_line("UV 9 High", 40, 340)]


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    """Fixture: production wiring with a real session's screen (Chrome in front, WhatsApp behind, VS Code
    minimised)."""
    groq, fake = SpyAnswerer(), LogScreen()
    rig = build(tmp_path, monkeypatch, groq=groq,
                screen=ScreenCommands(groq, SensorFlags(min_seconds=0), capture_fn=fake.capture, ocr_fn=fake.ocr))
    yield rig
    rig["c"].shutdown()


@pytest.mark.parametrize("said", ["What's on my screen", "what am I looking at"])
def test_whats_on_my_screen_describes_only_the_window_in_front(desktop, said) -> None:
    """Whats on my screen describes only the window in front."""
    reply = desktop["say"](said)
    assert reply == ("You're looking at Chrome showing Melbourne forecast. "
                     "The page says: Melbourne will be mostly sunny tomorrow with a top of 24 degrees.")
    for never in ("minimised", "VS Code", "WhatsApp", "Behind", "Gemini", "bom.gov.au", "Priya"):
        assert never not in reply
    assert desktop["groq"].questions == []  # no online call


def test_best_line_drops_fragments_symbols_names_and_urls() -> None:
    """Best line drops fragments symbols names and URLs."""
    assert best_line(["Sam, Priya, Alex, Jordan", "Rain 4mm", "Quick Start Guide For Everyone",
                      "the forecast for melbourne is sunny and warm"]) == "The forecast for melbourne is sunny and warm"
    assert best_line(["X +", "Rain 4mm"]) == ""


# == F19 picks from a summarise list ================================================================


@pytest.fixture
def resumes(tmp_path, monkeypatch):
    """Fixture: four resume.txt files in separate folders, indexed, with Groq mocked."""
    from jarvis.files.commands import FileCommands

    files = tmp_path / "files"
    for n in range(1, 5):
        (files / f"v{n}").mkdir(parents=True)
        (files / f"v{n}" / "resume.txt").write_bytes(b"Sample resume. Python, SQL and R. " * 5)
    index = FileIndex(roots=[files])
    index.rebuild_now()
    clock = DeskClock()
    fc = FileCommands(index, Summariser(), temp_dir=tmp_path / "jarvis_tmp", sleep=clock.sleep, clock=clock)
    rig = build(tmp_path, monkeypatch, index=index, groq=fc._answerer if hasattr(fc, "_answerer") else Summariser(), files=fc)
    yield rig
    rig["c"].shutdown()


@pytest.mark.parametrize("pick", ["Find number one", "will summarize file one", "summarize file one", "file one", "number one",
                                  "the first", "1"])
def test_a_pick_after_summarize_my_resume_is_from_the_list(resumes, pick) -> None:
    """A pick after summarize my resume is from the list."""
    assert resumes["say"]("summarize my resume") == SETTINGS["file_processor"]["replies"]["which_file"].format(count=4)
    reply = resumes["say"](pick)
    assert "can't find" not in reply and "number one" not in reply and "file one" not in reply
    assert resumes["c"].state == State.CONFIRMING  # the summary's preview of resume.txt waits for confirm
    resumes["confirming"]("cancel")


# == small leftovers ==================================================================================


@pytest.mark.parametrize("said", ["is the person boy or girl", "how old does the person look"])
def test_f9_questions_about_who_someone_is_stay_local(jarvis, said) -> None:  # noqa: F811
    """F9 questions about who someone is stay local."""
    jarvis["detector"].frames = [[__import__("jarvis.vision.detector", fromlist=["Detection"]).Detection("person", 0.9, PERSON)]] * 3
    jarvis["say"]("What can you see")
    assert jarvis["say"](said) == "I can only say what objects I see, not who someone is."
    _local(jarvis)


def test_f20_stop_tracking_alone_asks_which(jarvis) -> None:  # noqa: F811
    """F20 stop tracking alone asks which."""
    jarvis["say"]("track Indian cricket")
    jarvis["say"]("track the stock market")
    reply = jarvis["say"]("stop tracking")
    assert reply.endswith("Which one should I stop tracking?") and "indian cricket" in reply.lower()
    assert "forget" not in reply.lower()
    assert jarvis["say"]("Indian cricket").lower() == "stopped tracking indian cricket."
    assert "stock market" in jarvis["say"]("what am I tracking").lower()


@pytest.fixture
def tube(tmp_path, monkeypatch):
    """Fixture: the production wiring with YouTube's transcript API and Groq mocked."""
    from jarvis.youtube.commands import YouTubeCommands

    groq, clock = SummaryAnswerer(), TubeClock()
    api = FakeApi(Fetched([Snippet("[Music]", 0, 4), Snippet("hey guys", 4, 4)]))
    youtube = YouTubeCommands(groq, service("YouTube"), api_factory=api.factory, sleep=clock.sleep, clock=clock, chrome=lambda: "chrome.exe")
    rig = build(tmp_path, monkeypatch, groq=groq, youtube=youtube)
    rig.update(groq=groq)
    yield rig
    rig["c"].shutdown()


def test_f18_a_video_with_almost_no_captions_is_not_summarised(tube) -> None:
    """F18 a video with almost no captions is not summarised."""
    reply = tube["say"]("summarise https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert reply.endswith("That video doesn't have enough captions to summarise.")
    assert tube["groq"].summaries == []  # nothing made up


def test_f16b_search_googles_best_pizza_in_sydney() -> None:
    """F16B search googles best pizza in sydney."""
    assert parse_search(clean_command("Search Google's best pizza in Sydney")) == "best pizza in sydney"
    assert parse_search(clean_command("search google best pizza in sydney")) == "best pizza in sydney"
