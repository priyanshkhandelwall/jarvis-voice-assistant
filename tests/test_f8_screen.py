"""F8 screen awareness. Synthetic screenshots only (no
real screen capture in tests): a Pillow image with an editor window and a
"CommBank NetBank" window, fake window lists, and a spy instead of Groq.
One test runs the real Windows OCR on the synthetic image.

Covers: blackout happens before OCR AND before sending, the image is never
written to disk, the R4 preview is required (L3), cancel/Stop send nothing,
the request holds the blacked-out image, the text-only fallback, the hash
check, what is spoken vs logged, "find X on my screen", the Screen Capture
row, and no banned input/focus APIs anywhere in jarvis/."""

from __future__ import annotations

import io
import time
from pathlib import Path
from typing import List

import pytest
from PIL import Image, ImageDraw, ImageFont

from jarvis.actions.file_index import FileIndex
from jarvis.actions.find_file import FileFinder
from jarvis.controller import Controller, Event, EventType, State
from jarvis.controller.trust import TRUST_TABLE, TrustLevel
from jarvis.dashboard.activity_feed import format_record
from jarvis.dashboard.app import _screen_preview_rows, register_matchers
from jarvis.interpreter import Interpreter
from jarvis.logbook import Logbook
from jarvis.logbook.debug_log import DebugLog
from jarvis.memory.commands import MemoryCommands
from jarvis.memory.store import MemoryStore
from jarvis.orchestrator import Orchestrator
from jarvis.screen import capture as capture_mod
from jarvis.screen import commands as screen_mod
from jarvis.screen.capture import ScreenCapture, WindowInfo, capture, is_protected
from jarvis.screen.commands import ScreenCommands, ScreenPreview
from jarvis.screen.ocr import OcrLine, OcrUnavailable, OcrWord
from jarvis.security import paths
from jarvis.sensors import SCREEN, SensorFlags
from jarvis.settings import SETTINGS
from jarvis.speech import SpeechOutput
from jarvis.speech.qa import AnswerResult

REPLIES = SETTINGS["screen"]["replies"]
EDITOR = WindowInfo("main.py - Visual Studio Code", "Code.exe", (0, 0, 1000, 900))
BANK = WindowInfo("CommBank NetBank - Google Chrome", "chrome.exe", (1000, 0, 1600, 900))
SECRET = "Balance 12345"


def synthetic_screen() -> Image.Image:
    """A 1600x900 test desktop drawn with PIL: an editor window and a 'bank' window."""
    img = Image.new("RGB", (1600, 900), "white")
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype("arial.ttf", 40)
    d.text((40, 60), "ZeroDivisionError: division by zero", fill="black", font=font)
    d.text((40, 160), "line 12: result = total / count", fill="black", font=font)
    d.text((1040, 60), SECRET, fill="black", font=font)  # inside the protected window
    return img


def _bank_pixels_black(image) -> bool:
    crop = image.crop((1000, 0, 1600, 900)).convert("L")
    return crop.getextrema() == (0, 0)


def fake_ocr_from(image) -> List[OcrLine]:
    """Pretends to read whatever text is still visible: if the bank window
    weren't black, its secret would be "read" too."""
    lines = [OcrLine("ZeroDivisionError: division by zero", (OcrWord("ZeroDivisionError:", 40, 60, 380, 40),)),
             OcrLine("line 12: result = total / count", (OcrWord("line", 40, 160, 80, 40),))]
    if not _bank_pixels_black(image):
        lines.append(OcrLine(SECRET, (OcrWord("Balance", 1040, 60, 150, 40),)))
    return lines


class Rig:
    """A fake screen for F8 tests: synthetic desktop image, windows and OCR lines."""
    def __init__(self) -> None:
        self.ocr_saw_black: List[bool] = []
        self.sensor_on_during_ocr: List[bool] = []
        self.sensors = SensorFlags(min_seconds=0)

    def capture(self) -> ScreenCapture:
        """Fake capture for the tests."""
        return capture(grab=synthetic_screen, windows=lambda: [EDITOR, BANK], origin=lambda: (0, 0))

    def ocr(self, image) -> List[OcrLine]:
        """Fake ocr for the tests."""
        self.ocr_saw_black.append(_bank_pixels_black(image))
        self.sensor_on_during_ocr.append(self.sensors.really_on(SCREEN))
        return fake_ocr_from(image)


class SpyAnswerer:
    """Groq stand-in: records each question that would have gone online and returns a fixed reply."""
    model = "openai/gpt-oss-20b"

    def __init__(self) -> None:
        self.screen_calls: List[dict] = []
        self.questions: List[str] = []

    def today(self):
        """Fake today for the tests."""
        from datetime import date

        return date(2026, 10, 23)

    def answer(self, question, on_going_online=None):
        """Fake answer for the tests."""
        self.questions.append(question)
        return AnswerResult("an online answer", went_online=True)

    answer_live = answer

    def answer_screen(self, question, screen_text, jpeg, model, instructions, on_going_online=None):
        """Fake answer screen for the tests."""
        if on_going_online is not None:
            on_going_online()
        self.screen_calls.append(dict(question=question, text=screen_text, jpeg=jpeg, model=model))
        return AnswerResult("You have a crash in your code. Line 12 divides by zero.", went_online=True)


class FakeSpeech(SpeechOutput):
    """Speech output stand-in: records what she would say; nothing is played."""
    def __init__(self) -> None:
        self.texts: List[str] = []

    def speak(self, text: str) -> None:
        """Fake speak for the tests."""
        self.texts.append(text)


def _wait(pred, timeout=3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


@pytest.fixture
def app(tmp_path, monkeypatch):
    """Fixture: the app components with this feature's side effects mocked."""
    monkeypatch.setattr(paths, "APPROVED_FOLDERS", paths.APPROVED_FOLDERS + [tmp_path.resolve()])
    rig = Rig()
    c = Controller(start_state=State.AWAKE)
    logbook = Logbook(debug_log=DebugLog(path=tmp_path / "debug.log"))
    c.set_transition_listener(logbook.on_transition)
    c.start()
    interpreter, speech = Interpreter(), FakeSpeech()
    orch = Orchestrator(c, interpreter, speech, logbook=logbook)
    groq = SpyAnswerer()
    screen = ScreenCommands(groq, rig.sensors, capture_fn=rig.capture, ocr_fn=rig.ocr)
    register_matchers(interpreter, orch, logbook, FileFinder(FileIndex(roots=[tmp_path / "f"])), groq, None,
                      memory=MemoryCommands(MemoryStore(tmp_path / "m.json")), screen=screen)

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

    yield dict(say=say, typed=typed, c=c, speech=speech, groq=groq, screen=screen, rig=rig, logbook=logbook,
               debug=tmp_path / "debug.log")
    c.shutdown()


def _lines(app) -> List[str]:
    return format_record(app["logbook"].last_completed()).lines


# -- protected windows: blacked out first --------------------------------------------------------


def test_protected_list_matches_the_developers_defaults() -> None:
    """Protected list matches the developers defaults."""
    for title in ("CommBank NetBank", "PayPal: Summary", "New InPrivate tab - Microsoft Edge", "Bitwarden",
                  "New Incognito Tab - Google Chrome", "Mozilla Firefox Private Browsing", "Windows Security"):
        assert is_protected(title, ""), title
    assert is_protected("Vault", "KeePassXC.exe") and is_protected("Login", "1Password.exe")
    assert not is_protected("main.py - Visual Studio Code", "Code.exe")


def test_capture_blacks_out_before_returning() -> None:
    """Capture blacks out before returning."""
    shot = capture(grab=synthetic_screen, windows=lambda: [EDITOR, BANK], origin=lambda: (0, 0))
    assert shot.covered == 1 and _bank_pixels_black(shot.image)
    assert shot.image.getpixel((500, 500)) == (255, 255, 255)  # the editor is untouched
    assert shot.visible_windows == [EDITOR]


def test_blackout_respects_a_negative_virtual_screen_origin() -> None:
    """Blackout respects a negative virtual screen origin."""
    left_monitor_bank = WindowInfo("NetBank", "", (-1600, 0, -1000, 900))
    shot = capture(grab=synthetic_screen, windows=lambda: [left_monitor_bank], origin=lambda: (-1600, 0))
    assert shot.image.crop((0, 0, 600, 900)).convert("L").getextrema() == (0, 0)


def test_blackout_happens_before_ocr(app) -> None:
    """Blackout happens before OCR."""
    app["say"]("what's on my screen")
    assert app["rig"].ocr_saw_black == [True]


def test_summary_speaks_titles_and_first_lines_never_the_protected_title(app) -> None:
    """Summary speaks titles and first lines never the protected title."""
    reply = app["say"]("What's on my screen?")
    # Only the window in front, and its one clean line.
    assert reply == "You're looking at VS Code showing main.py. The document starts with: Line 12: result = total / count."
    assert "CommBank" not in reply and SECRET not in reply
    assert app["groq"].questions == [] and app["groq"].screen_calls == []  # local only
    record = app["logbook"].last_completed()
    assert record.rule_or_trust_level == TrustLevel.L1


def test_screen_text_is_spoken_but_never_logged(app) -> None:
    """Screen text is spoken but never logged."""
    app["say"]("read my screen")
    record = app["logbook"].last_completed()
    # What she said is shown in Live Activity ("saying:..."), as a
    # session-only line: never in the result, the steps written to disk, or debug.log.
    private = set(record.session_only)
    assert any(step.startswith("saying: ") and "total / count" in step for step in private)
    assert "total / count" not in repr([s for s in record.progress + record.closing if s not in private])
    assert "ZeroDivisionError" not in repr([s for s in record.progress + record.closing if s not in private])
    assert "ZeroDivisionError" not in str(record.result) and "ZeroDivisionError" not in repr(record.understood)
    assert "described the screen: the window in front (1 showing, 0 minimised, 1 covered), 2 line(s) of text (text not logged)" in record.result
    assert "read text from: VS Code with main.py" in _lines(app)  # which window was read
    assert "ZeroDivisionError" not in app["debug"].read_text(encoding="utf-8")


def test_find_a_word(app) -> None:
    """Find a word."""
    assert app["say"]("find ZeroDivisionError on my screen") == \
        "Yes, I can see 'zerodivisionerror' in VS Code – main.py."  # The app, then the title
    assert app["say"]("is there balance on my screen") == "I can't see 'balance' on your screen."  # blacked out
    assert app["groq"].questions == []


def test_the_screen_row_is_on_only_during_the_capture(app) -> None:
    """The screen row is on only during the capture."""
    app["say"]("what's on my screen")
    assert app["rig"].sensor_on_during_ocr == [True]
    assert not app["rig"].sensors.really_on(SCREEN)


# -- the image never touches the disk ---------------------------------------------------------------


def test_the_image_is_never_written_to_disk(app, tmp_path, monkeypatch) -> None:
    """The image is never written to disk."""
    monkeypatch.chdir(tmp_path)
    for var in ("TMP", "TEMP"):
        monkeypatch.setenv(var, str(tmp_path))
    real_save = Image.Image.save

    def save_only_to_memory(self, fp, *args, **kwargs):
        assert isinstance(fp, io.BytesIO), f"image saved to {fp!r}"
        return real_save(self, fp, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", save_only_to_memory)
    before = set(tmp_path.rglob("*"))
    app["say"]("what's on my screen")
    app["say"]("explain my screen")
    app["typed"]("confirm")
    created = {p for p in set(tmp_path.rglob("*")) - before if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp")}
    assert created == set()
    assert app["groq"].screen_calls  # it really ran


# -- explain: R4 preview, L3, then the exact bytes -----------------------------------------------


def test_explain_shows_the_preview_and_waits_for_confirm(app) -> None:
    """Explain shows the preview and waits for confirm."""
    prompt = app["say"]("explain my screen")
    assert app["c"].state == State.CONFIRMING
    assert prompt == REPLIES["confirm_prompt"]
    pending = app["c"].pending_confirmation()
    assert pending.action_name == "send_screen_online" and pending.trust_level == TrustLevel.L3
    assert app["groq"].screen_calls == []  # nothing sent before confirm
    preview = app["screen"].preview(dict(pending.params)["preview_id"])
    shown = Image.open(io.BytesIO(preview.jpeg))
    assert max(shown.size) == SETTINGS["screen"]["send_max_side"]  # downscaled, never the original 1600 px
    assert shown.crop((800, 0, 1280, 720)).convert("L").getextrema()[1] < 20  # bank window black in the preview


def test_confirm_sends_exactly_the_previewed_blacked_out_image(app) -> None:
    """Confirm sends exactly the previewed blacked out image."""
    app["say"]("help me with this")
    preview = app["screen"].preview(dict(app["c"].pending_confirmation().params)["preview_id"])
    reply = app["typed"]("confirm")
    sent = app["groq"].screen_calls[0]
    assert sent["jpeg"] == preview.jpeg and sent["text"] == preview.text and sent["question"] == "help me with this"
    assert sent["model"] == SETTINGS["screen"]["vision_model"] == "qwen/qwen3.8-27b"
    assert _bank_pixels_black(Image.open(io.BytesIO(sent["jpeg"])).resize((1600, 900)).point(lambda v: 0 if v < 40 else v))
    assert SECRET not in sent["text"]
    assert reply == "You have a crash in your code. Line 12 divides by zero."
    assert app["screen"].preview(preview.preview_id) is None  # freed after sending


def test_what_is_logged_about_the_send(app) -> None:
    """What is logged about the send."""
    app["say"]("explain my screen")  # "what am I looking at" reads the screen since
    app["typed"]("confirm")
    record = app["logbook"].last_completed()
    lines = " ".join(_lines(app)).lower()
    assert "sending online: screenshot (jpeg 1280x720" in lines and "sha256" in lines
    assert "groq" not in lines and "qwen" not in lines  # R3: never in Live Activity
    assert "qwen/qwen3.8-27b" in record.online_destination  # only in the record / debug log
    # The answer about laptop data is shown this session only, never logged.
    private = set(record.session_only)
    assert any("line 12 divides" in step.lower() for step in private)
    assert "line 12 divides" not in repr([s for s in record.progress + record.closing if s not in private]).lower()
    assert "line 12 divides" not in str(record.result).lower()
    assert "line 12 divides" not in app["debug"].read_text(encoding="utf-8").lower()
    assert app["speech"].texts[-2] == REPLIES["sending"]  # said before it went


def test_cancel_sends_nothing_and_frees_the_preview(app) -> None:
    """Cancel sends nothing and frees the preview."""
    app["say"]("explain my screen")
    preview_id = dict(app["c"].pending_confirmation().params)["preview_id"]
    app["typed"]("cancel")
    assert _wait(lambda: app["c"].state == State.AWAKE)
    assert app["groq"].screen_calls == [] and app["screen"].preview(preview_id) is None
    app["typed"]("confirm")  # too late: nothing to confirm
    assert app["groq"].screen_calls == []


def test_stop_sends_nothing(app) -> None:
    """Stop sends nothing."""
    app["say"]("explain my screen")
    preview_id = dict(app["c"].pending_confirmation().params)["preview_id"]
    app["c"].request_stop("test")
    assert _wait(lambda: app["c"].state == State.AWAKE)
    assert _wait(lambda: app["screen"].preview(preview_id) is None)
    assert app["groq"].screen_calls == []


def test_a_changed_preview_is_never_sent(app) -> None:
    """A changed preview is never sent."""
    app["say"]("explain my screen")
    preview_id = dict(app["c"].pending_confirmation().params)["preview_id"]
    old = app["screen"].preview(preview_id)
    with app["screen"]._lock:  # something altered the bytes after the preview
        app["screen"]._previews[preview_id] = ScreenPreview(old.preview_id, old.jpeg + b"x", old.size, old.text,
                                                             old.question, old.sha256, old.windows_read)
    assert app["typed"]("confirm") == REPLIES["preview_changed"]
    assert app["groq"].screen_calls == []


def test_text_only_when_no_vision_model(app, monkeypatch) -> None:
    """Text only when no vision model."""
    monkeypatch.setitem(screen_mod._CFG, "vision_model", "")
    assert app["say"]("explain my screen") == REPLIES["confirm_prompt_text_only"]
    app["typed"]("confirm")
    sent = app["groq"].screen_calls[0]
    assert sent["jpeg"] is None and "ZeroDivisionError" in sent["text"] and sent["model"] == "openai/gpt-oss-20b"


def test_the_preview_card_shows_image_text_and_warnings(monkeypatch) -> None:
    """The preview card shows image text and warnings."""
    preview = ScreenPreview("p1", b"\xff\xd8jpeg", (1280, 720), "ZeroDivisionError", "explain my screen", "h", ())
    texts = [getattr(c, "value", "") for c in _screen_preview_rows(preview)]
    assert "This will be sent online." in texts
    assert "Online service terms not yet checked by you" in texts  # until he marks them checked
    images = [c for c in _screen_preview_rows(preview) if c.__class__.__name__ == "Image"]
    assert images and images[0].src == preview.jpeg  # the exact bytes, from memory
    monkeypatch.setitem(SETTINGS["screen"], "online_terms_checked", True)
    assert "Online service terms not yet checked by you" not in [getattr(c, "value", "") for c in _screen_preview_rows(preview)]


def test_trust_levels() -> None:
    """Trust levels."""
    assert TRUST_TABLE["capture_screen"] == TrustLevel.L1
    assert TRUST_TABLE["send_screen_online"] == TrustLevel.L3


# -- missing setup (R14) ---------------------------------------------------------------------------


def test_missing_ocr_says_what_to_install(app, monkeypatch) -> None:
    """Missing OCR says what to install."""
    def no_ocr(image):
        raise OcrUnavailable(SETTINGS["setup_messages"]["ocr_library_missing"])

    monkeypatch.setattr(app["screen"], "_ocr", no_ocr)
    reply = app["say"]("what's on my screen")
    assert reply.endswith(SETTINGS["setup_messages"]["ocr_library_missing"])
    assert reply.startswith("You're looking at VS Code showing main.py.")


def test_missing_pillow_says_what_to_install(app, monkeypatch) -> None:
    """Missing pillow says what to install."""
    def no_pillow():
        raise ImportError("PIL")

    monkeypatch.setattr(app["screen"], "_capture", no_pillow)
    assert app["say"]("what's on my screen") == SETTINGS["setup_messages"]["screen_capture_missing"]


# -- the real Windows OCR on a synthetic screen ---------------------------------------------------


def _winrt_ocr() -> bool:
    from jarvis.screen.ocr import ocr_available

    return ocr_available() is None


@pytest.mark.skipif(not _winrt_ocr(), reason="Windows OCR package or language not installed")
def test_real_windows_ocr_reads_the_editor_and_never_the_bank() -> None:
    """Real Windows OCR reads the editor and never the bank."""
    from jarvis.screen.ocr import read_text

    shot = capture(grab=synthetic_screen, windows=lambda: [EDITOR, BANK], origin=lambda: (0, 0))
    text = " ".join(line.text for line in read_text(shot.image))
    assert "ZeroDivisionError" in text and "Balance" not in text


# -- no banned APIs anywhere -------------------------------------------------------------------------


BANNED = ("SendKeys", "AppActivate", "SetForegroundWindow", "ShowWindow", "BringWindowToTop", "MoveWindow",
          "keybd_event", "mouse_event", "SendInput", "SetActiveWindow", "SetFocus", "SetWindowPos", "PostMessage",
          "SendMessage", "SetCursorPos", "ImageGrab.grabclipboard", "OpenClipboard")


def test_no_banned_input_or_focus_api_in_jarvis() -> None:
    """No banned input or focus API in JARVIS."""
    root = Path(capture_mod.__file__).resolve().parent.parent
    offenders = []
    for source in root.rglob("*.py"):
        text = source.read_text(encoding="utf-8")
        for name in BANNED:
            if name in text:
                offenders.append(f"{source.relative_to(root)}: {name}")
    assert offenders == []


@pytest.mark.skipif(not _winrt_ocr(), reason="Windows OCR package or language not installed")
def test_ocr_first_then_onnxruntime_does_not_crash() -> None:
    """Found Windows OCR loads Windows' own onnxruntime.dll, and
    importing the pip onnxruntime afterwards crashed the whole process. Run
    in a separate process so a regression can't take the test run down."""
    import subprocess
    import sys

    code = ("from PIL import Image\nfrom jarvis.screen.ocr import read_text\n"
            "read_text(Image.new('RGB', (200, 100), 'white'))\n"
            "import onnxruntime\nprint('ok', onnxruntime.get_device())\n")
    root = Path(capture_mod.__file__).resolve().parents[2]
    done = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and done.stdout.startswith("ok"), done.stderr[-500:]
