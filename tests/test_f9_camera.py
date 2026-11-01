"""F9 webcam vision. The real camera is never opened:
a fake capture serves synthetic numpy frames and records every read and
release. One test runs the real YOLOX-S detector on YOLOX's own sample
photo (tests/fixtures/yolox_dog.jpg, Apache-2.0).

Covers: warm-up frames, the black-frame retry, the camera released before
analysis, camera probing only inside a capture, no frames written to disk,
price formatting and staleness, the missing camera / privacy block / model
messages, the continuous L3 preview (confirm, Live Activity, turn off,
Stop), and no GUI or file-writing OpenCV calls."""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path
from typing import List

import numpy as np
import pytest

from jarvis.actions.file_index import FileIndex
from jarvis.actions.find_file import FileFinder
from jarvis.controller import Controller, Event, EventType, State
from jarvis.controller.trust import TRUST_TABLE, TrustLevel
from jarvis.dashboard.activity_feed import format_record
from jarvis.dashboard.app import register_matchers
from jarvis.interpreter import Interpreter
from jarvis.logbook import Logbook
from jarvis.logbook.debug_log import DebugLog
from jarvis.memory.commands import MemoryCommands
from jarvis.memory.store import MemoryStore
from jarvis.orchestrator import Orchestrator
from jarvis.security import paths
from jarvis.sensors import CAMERA, SensorFlags
from jarvis.settings import SETTINGS
from jarvis.speech import SpeechOutput
from jarvis.vision import camera as camera_mod
from jarvis.vision import commands as vision_mod
from jarvis.vision.camera import Camera, CameraProblem
from jarvis.vision.commands import VisionCommands, describe
from jarvis.vision.detector import Detection, ObjectDetector, model_path
from jarvis.vision.prices import PriceEntry, load_prices, money, price_sentence

MSG = SETTINGS["setup_messages"]
REPLIES = SETTINGS["camera"]["replies"]
WARMUP = SETTINGS["camera"]["warmup_frames"]
TODAY = date(2026, 10, 23)


def bright(n=1):
    """A plain mid-grey frame (a camera that is working)."""
    return np.full((48, 64, 3), 120, dtype=np.uint8)


def black():
    """An all-black frame (a camera that isn't ready yet)."""
    return np.zeros((48, 64, 3), dtype=np.uint8)


class FakeCapture:
    """Camera capture stand-in returning prepared frames."""
    def __init__(self, frames) -> None:
        self.frames = list(frames)
        self.reads = 0
        self.released = False

    def read(self):
        """Fake read for the tests."""
        assert not self.released, "read after release"
        self.reads += 1
        frame = self.frames.pop(0) if self.frames else bright()
        return True, frame

    def release(self) -> None:
        """Fake release for the tests."""
        self.released = True


class Opener:
    """Hands out FakeCaptures per camera index; None = no camera there."""

    def __init__(self, per_index) -> None:
        self.per_index = per_index
        self.opened: List[int] = []
        self.captures: List[FakeCapture] = []

    def __call__(self, index):
        self.opened.append(index)
        frames = self.per_index.get(index)
        if frames is None:
            return None
        cap = FakeCapture(frames() if callable(frames) else frames)
        self.captures.append(cap)
        return cap


class SpyDetector:
    """Object detector stand-in: returns canned detections and records each frame it was given."""
    def __init__(self, found=None, opener=None) -> None:
        self.found = found if found is not None else [Detection("laptop", 0.91, (0, 0, 50, 40)), Detection("cup", 0.66, (0, 0, 5, 5))]
        self.opener = opener
        self.released_when_called: List[bool] = []

    def detect(self, frame):
        """Fake detect for the tests."""
        if self.opener is not None and self.opener.captures:
            self.released_when_called.append(self.opener.captures[-1].released)
        return list(self.found)

    def unload_if_idle(self):
        """Fake unload if idle for the tests."""
        return False


# -- one look: warm-up, black frame, release before analysis ------------------------------------


def test_warm_up_frames_are_thrown_away_and_three_spaced_frames_are_kept() -> None:
    """Three frames (two skipped between each) instead of one."""
    frames = [black()] * WARMUP + [bright()] * 10  # exposure settling: dark first frames
    opener = Opener({0: frames})
    look = Camera(opener=opener, blocked=lambda: False).one_look()
    cap = opener.captures[0]
    assert cap.reads == WARMUP + 3 + 2 * 2 and cap.released and look.frame.mean() == 120 and not look.retried
    assert len(look.frames) == 3 and all(f.mean() == 120 for f in look.frames)


def test_a_black_frame_is_retried_once() -> None:
    """A black frame is retried once."""
    attempts = iter([[black()] * (WARMUP + 1), [bright()] * (WARMUP + 1)])
    opener = Opener({0: lambda: next(attempts)})
    look = Camera(opener=opener, blocked=lambda: False).one_look()
    assert look.retried and len(opener.captures) == 2 and all(c.released for c in opener.captures)


def test_two_black_frames_say_so() -> None:
    """Two black frames say so."""
    opener = Opener({0: lambda: [black()] * (WARMUP + 1)})
    with pytest.raises(CameraProblem) as problem:
        Camera(opener=opener, blocked=lambda: False).one_look()
    assert str(problem.value) == MSG["camera_black"] and len(opener.captures) == 2


def test_the_camera_is_released_before_analysis(rig) -> None:
    """The camera is released before analysis."""
    rig["say"]("what do you see")
    assert rig["detector"].released_when_called == [True, True, True]  # one detection per kept frame


def test_other_cameras_are_probed_only_inside_a_capture() -> None:
    """Other cameras are probed only inside a capture."""
    opener = Opener({2: [bright()] * 20})
    camera = Camera(opener=opener, blocked=lambda: False)
    VisionCommands(camera, SpyDetector())
    assert opener.opened == []  # nothing at startup
    camera.one_look()
    assert opener.opened == [0, 1, 2] and camera.last_index == 2


def test_no_camera_at_all() -> None:
    """No camera at all."""
    opener = Opener({})
    with pytest.raises(CameraProblem) as problem:
        Camera(opener=opener, blocked=lambda: False).one_look()
    assert str(problem.value) == MSG["camera_not_found"]
    assert opener.opened == list(range(SETTINGS["camera"]["max_index_probe"]))


def test_windows_privacy_block_gives_the_settings_path() -> None:
    """Windows privacy block gives the settings path."""
    opener = Opener({0: [bright()] * 20})
    with pytest.raises(CameraProblem) as problem:
        Camera(opener=opener, blocked=lambda: True).one_look()
    assert "Settings, Privacy and security, Camera" in str(problem.value) and opener.opened == []


def test_the_privacy_check_reads_the_registry_only() -> None:
    """The privacy check reads the registry only."""
    assert isinstance(camera_mod.camera_blocked_by_windows(), bool)  # read-only query, no camera opened


def test_camera_row_is_on_only_during_the_look() -> None:
    """Camera row is on only during the look."""
    sensors = SensorFlags(min_seconds=0)
    seen = []

    class Watching(FakeCapture):
        def read(self):
            seen.append(sensors.really_on(CAMERA))
            return super().read()

    camera = Camera(sensors, opener=lambda i: Watching([bright()] * 20), blocked=lambda: False)
    camera.one_look()
    assert all(seen) and not sensors.really_on(CAMERA)


# -- prices -------------------------------------------------------------------------------------------


def test_money_formatting() -> None:
    """Money formatting."""
    assert money(800) == "$800" and money(2500) == "$2,500" and money(0.4) == "$0.40" and money(15) == "$15"
    assert money(120, "EUR") == "120 EUR"


def test_price_sentence_fresh_stale_and_undated() -> None:
    """Price sentence fresh stale and undated."""
    fresh = PriceEntry("laptop", 800, 2500, "AUD", date(2026, 10, 23), "rough estimate")
    # Figures from this very month need no "from <month>" note.
    assert price_sentence(fresh, TODAY) == "Roughly $800 to $2,500."
    older = PriceEntry("laptop", 800, 2500, "AUD", date(2026, 9, 2), "rough estimate")
    assert price_sentence(older, TODAY) == "Roughly $800 to $2,500. Those are rough figures from September 2026."
    stale = PriceEntry("laptop", 800, 2500, "AUD", date(2025, 6, 1), "rough estimate")
    assert price_sentence(stale, TODAY).endswith("from June 2025, so they're out of date.")
    undated = PriceEntry("laptop", 800, 2500, "AUD", None, "")
    assert price_sentence(undated, TODAY).endswith(REPLIES["price_undated"])


def test_the_shipped_price_table_loads() -> None:
    """The shipped price table loads."""
    table = load_prices()
    assert table["laptop"].low == 800 and table["laptop"].high == 2500 and table["laptop"].currency == "AUD"
    assert "person" not in table


def test_describe() -> None:
    """Describe."""
    d = lambda label: Detection(label, 0.9, (0, 0, 1, 1))  # noqa: E731
    assert describe([d("laptop"), d("cup")]) == "a laptop and a cup"
    assert describe([d("cup"), d("cup"), d("apple"), d("person")]) == "two cups, an apple and a person"


# -- through the real matcher order -----------------------------------------------------------------------


class FakeSpeech(SpeechOutput):
    """Speech output stand-in: records what she would say; nothing is played."""
    def __init__(self) -> None:
        self.texts: List[str] = []

    def speak(self, text: str) -> None:
        """Fake speak for the tests."""
        self.texts.append(text)


class NoGroq:
    """Groq stand-in that records calls and never answers (proves nothing went online)."""
    model = "x"

    def __init__(self) -> None:
        self.questions: List[str] = []

    def today(self):
        """Fake today for the tests."""
        return TODAY

    def answer(self, question, on_going_online=None):
        """Fake answer for the tests."""
        self.questions.append(question)
        from jarvis.speech.qa import AnswerResult

        return AnswerResult("online", went_online=True)

    answer_live = answer


def _wait(pred, timeout=3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Fixture: the components this file's tests drive, with every side effect mocked."""
    monkeypatch.setattr(paths, "APPROVED_FOLDERS", paths.APPROVED_FOLDERS + [tmp_path.resolve()])
    monkeypatch.setitem(vision_mod._CFG, "stream_detect_seconds", 0.1)
    sensors = SensorFlags(min_seconds=0)
    opener = Opener({0: lambda: [bright() for _ in range(400)]})
    detector = SpyDetector(opener=opener)
    status: List[str] = []
    vision = VisionCommands(Camera(sensors, opener=opener, blocked=lambda: False), detector, status=status.append,
                            today=lambda: TODAY)
    c = Controller(start_state=State.AWAKE)
    logbook = Logbook(debug_log=DebugLog(path=tmp_path / "debug.log"))
    c.set_transition_listener(logbook.on_transition)
    c.start()
    interpreter, speech = Interpreter(), FakeSpeech()
    orch = Orchestrator(c, interpreter, speech, logbook=logbook)
    groq = NoGroq()
    register_matchers(interpreter, orch, logbook, FileFinder(FileIndex(roots=[tmp_path / "f"])), groq, None,
                      memory=MemoryCommands(MemoryStore(tmp_path / "m.json")), vision=vision)

    def say(text):
        c.submit(Event(EventType.SPEECH_STARTED, {"source": "voice"}))
        assert _wait(lambda: c.state == State.LISTENING)
        orch.handle_command(text, "voice")
        assert _wait(lambda: c.state in (State.AWAKE, State.CONFIRMING))
        return speech.texts[-1]

    def typed(text):
        orch.handle_typed_command(text)
        assert _wait(lambda: c.state in (State.AWAKE, State.CONFIRMING))
        return speech.texts[-1]

    yield dict(say=say, typed=typed, c=c, vision=vision, opener=opener, detector=detector, status=status,
               sensors=sensors, logbook=logbook, groq=groq)
    vision.stop_stream()
    c.shutdown()


def test_what_do_you_see(rig) -> None:
    """What do you see."""
    # A cup at 0.66 is steady but not sure ([camera] sure_confidence).
    assert rig["say"]("What do you see?") == "I can see a laptop, and I think I see a cup."
    record = rig["logbook"].last_completed()
    assert record.rule_or_trust_level == TrustLevel.L1 and rig["groq"].questions == []
    lines = format_record(record).lines
    assert "detected: laptop 0.91, cup 0.66" in lines  # with its confidence (spec)
    assert any("released before analysis" in line for line in lines)


def test_how_much_is_this(rig) -> None:
    """How much is this."""
    assert rig["say"]("how much is this") == \
        "That looks like a laptop. Roughly $800 to $2,500."  # This month's figures, no date note


def test_how_much_for_something_without_a_price(rig) -> None:
    """How much for something without a price."""
    rig["detector"].found = [Detection("giraffe", 0.8, (0, 0, 9, 9))]
    assert rig["say"]("how much is this") == "That looks like a giraffe, but I don't have a price for it."
    rig["detector"].found = [Detection("person", 0.9, (0, 0, 9, 9))]
    assert rig["say"]("how much is this") == REPLIES["price_only_person"]


def test_nothing_recognised(rig) -> None:
    """Nothing recognised."""
    rig["detector"].found = []
    assert rig["say"]("what's this") == REPLIES["nothing"]


def test_missing_detector_model_says_what_to_run(rig, tmp_path) -> None:
    """Missing detector model says what to run."""
    real = ObjectDetector(path=tmp_path / "no_model.onnx")
    rig["vision"].detector = real
    assert rig["say"]("what do you see") == MSG["detector_missing"]


def test_turn_on_the_camera_needs_one_confirm_then_streams(rig) -> None:
    """Turn on the camera needs one confirm then streams."""
    prompt = rig["say"]("turn on the camera")
    assert prompt == REPLIES["stream_prompt"] and rig["c"].state == State.CONFIRMING
    assert rig["c"].pending_confirmation().trust_level == TrustLevel.L3
    assert rig["opener"].opened == []  # nothing opened before confirm
    assert rig["typed"]("confirm") == REPLIES["stream_on"]
    stream = rig["vision"].stream
    assert stream.running and stream.latest_jpeg()[:2] == b"\xff\xd8"  # a JPEG, in memory
    assert rig["sensors"].really_on(CAMERA)
    assert _wait(lambda: any(s.startswith("camera sees: laptop 0.91") for s in rig["status"]))  # Live Activity
    assert rig["say"]("turn off the camera") == "Camera off."
    assert rig["vision"].stream is None and rig["opener"].captures[-1].released
    assert not rig["sensors"].really_on(CAMERA)


def test_stop_turns_the_camera_off(rig) -> None:
    """Stop turns the camera off."""
    rig["say"]("turn on the camera")
    rig["typed"]("confirm")
    assert rig["vision"].stream.running
    rig["c"].request_stop("test")
    assert _wait(lambda: rig["vision"].stream is None)
    assert rig["opener"].captures[-1].released


def test_cancel_never_opens_the_camera(rig) -> None:
    """Cancel never opens the camera."""
    rig["say"]("turn on the camera")
    rig["typed"]("cancel")
    assert rig["opener"].opened == [] and rig["vision"].stream is None


def test_turn_off_when_already_off(rig) -> None:
    """Turn off when already off."""
    assert rig["say"]("turn off the camera") == REPLIES["already_off"]


def test_trust_levels() -> None:
    """Trust levels."""
    assert TRUST_TABLE["capture_camera"] == TrustLevel.L1
    assert TRUST_TABLE["camera_stream"] == TrustLevel.L3


# -- nothing on disk ----------------------------------------------------------------------------------------


def test_no_frames_are_written_to_disk(rig, tmp_path, monkeypatch) -> None:
    """No frames are written to disk."""
    import cv2

    monkeypatch.chdir(tmp_path)
    for var in ("TMP", "TEMP"):
        monkeypatch.setenv(var, str(tmp_path))
    monkeypatch.setattr(cv2, "imwrite", lambda *a, **k: pytest.fail("a frame was written to disk"))
    before = set(tmp_path.rglob("*"))
    rig["say"]("what do you see")
    rig["say"]("turn on the camera")
    rig["typed"]("confirm")
    assert _wait(lambda: rig["vision"].stream.frame_count > 3)
    rig["say"]("turn off the camera")
    images = {p for p in set(tmp_path.rglob("*")) - before if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp")}
    assert images == set()


def test_no_gui_or_file_writing_opencv_calls() -> None:
    """No gui or file writing opencv calls."""
    root = Path(vision_mod.__file__).parent
    for source in root.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        for banned in ("imshow", "namedWindow", "imwrite", "VideoWriter", "waitKey"):
            assert banned not in text, f"{source.name} uses {banned}"


# -- the real detector, offline ---------------------------------------------------------------------------


@pytest.mark.skipif(not model_path().exists(), reason="YOLOX-S not downloaded (scripts/download_models.py)")
def test_real_detector_on_yoloxs_sample_photo() -> None:
    """Real detector on yoloxs sample photo."""
    import cv2

    frame = cv2.imread(str(Path(__file__).parent / "fixtures" / "yolox_dog.jpg"))
    found = ObjectDetector().detect(frame)
    labels = [d.label for d in found]
    assert labels[:2] == ["bicycle", "dog"] and "cat" not in labels
    assert found[1].confidence > 0.8
