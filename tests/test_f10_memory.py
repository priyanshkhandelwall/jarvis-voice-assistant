"""F10 long-term memory: only what the developer tells
her to remember; local only, never sent online; never-store for passwords,
PINs, cards (Luhn), bank and ID numbers, with the refused text written
nowhere; "forget everything" needs two confirmations (L4); atomic writes,
a cap, corrupt-file recovery, survives a restart.

Commands go through the production matcher order (app.register_matchers)
and the real orchestrator/controller. Groq is a spy. Files live in tmp_path
(added to the approved folders, R9)."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import List

import pytest

from jarvis.actions import app_finder, open_app
from jarvis.actions.app_finder import ScoredMatch
from jarvis.actions.file_index import FileIndex
from jarvis.actions.find_file import FileFinder
from jarvis.controller import Controller, Event, EventType, State
from jarvis.controller.trust import TRUST_TABLE, TrustLevel
from jarvis.dashboard.activity_feed import format_record
from jarvis.dashboard.app import register_matchers
from jarvis.interpreter import Interpreter
from jarvis.logbook import Logbook
from jarvis.logbook.debug_log import DebugLog
from jarvis.logbook.records import redact_sensitive
from jarvis.memory.commands import MemoryCommands, turn_round
from jarvis.memory.sensitive import luhn_valid, sensitive_type
from jarvis.memory.store import MemoryStore
from jarvis.orchestrator import Orchestrator
from jarvis.security import paths
from jarvis.settings import SETTINGS
from jarvis.speech import SpeechOutput
from jarvis.speech.qa import AnswerResult

R = SETTINGS["memory"]["replies"]
NOW = datetime(2026, 10, 23, 15, 0, 0)


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
    """Stands in for Groq: records everything that would have gone online."""

    model = "openai/gpt-oss-20b"

    def __init__(self) -> None:
        self.questions: List[str] = []

    def today(self):
        """Fake today for the tests."""
        return NOW.date()

    def answer(self, question, on_going_online=None):
        """Fake answer for the tests."""
        self.questions.append(question)
        if on_going_online is not None:
            on_going_online()
        return AnswerResult("an online answer", went_online=True)

    answer_live = answer


class NoStoreApps:
    """Store-app list stand-in: no Store apps and no background read."""
    def prefetch(self) -> None:
        """Fake prefetch for the tests."""
        pass

    def apps(self, wait_seconds: float = 0.0):
        """Fake apps for the tests."""
        return []


@pytest.fixture
def approved(tmp_path, monkeypatch):
    """tmp_path is an approved folder for these tests (R9 still runs)."""
    monkeypatch.setattr(paths, "APPROVED_FOLDERS", paths.APPROVED_FOLDERS + [tmp_path.resolve()])
    return tmp_path


@pytest.fixture
def rig(approved, monkeypatch):
    """Fixture: the components this file's tests drive, with every side effect mocked."""
    tmp_path = approved
    launched: List[str] = []
    monkeypatch.setattr(app_finder, "enumerate_installed_apps",
                        lambda store_apps=None: [ScoredMatch("notepad", 0.0, "app", "notepad.exe")])
    monkeypatch.setattr(open_app, "STORE_APPS", NoStoreApps())
    monkeypatch.setattr(open_app.os, "startfile", launched.append)
    monkeypatch.setattr(open_app, "launch_and_verify_with_retry", lambda launch, timeout, poll: (launch(), False))

    clock = {"now": NOW}
    store = MemoryStore(tmp_path / "memory.json", now=lambda: clock["now"])
    c = Controller(start_state=State.AWAKE)
    logbook = Logbook(debug_log=DebugLog(path=tmp_path / "debug.log"))
    c.set_transition_listener(logbook.on_transition)
    c.start()
    interpreter = Interpreter()
    speech = FakeSpeech()
    orch = Orchestrator(c, interpreter, speech, logbook=logbook)
    groq = SpyAnswerer()
    index = FileIndex(roots=[tmp_path / "files"])
    register_matchers(interpreter, orch, logbook, FileFinder(index), groq, None, memory=MemoryCommands(store))

    def say(text: str, typed: bool = False) -> str:
        if typed:
            orch.handle_typed_command(text)
        else:
            c.submit(Event(EventType.SPEECH_STARTED, {"source": "voice"}))
            assert _wait(lambda: c.state == State.LISTENING)
            orch.handle_command(text, "voice")
        assert _wait(lambda: c.state in (State.AWAKE, State.CONFIRMING))
        return speech.texts[-1] if speech.texts else ""

    def confirm() -> str:
        orch.handle_typed_command("confirm")
        assert _wait(lambda: c.state in (State.AWAKE, State.CONFIRMING))
        return speech.texts[-1]

    yield dict(c=c, say=say, confirm=confirm, speech=speech, logbook=logbook, groq=groq, store=store, clock=clock,
               path=tmp_path / "memory.json", debug=tmp_path / "debug.log", launched=launched, orch=orch)
    c.shutdown()


def _activity(rig) -> List[str]:
    return format_record(rig["logbook"].last_completed()).lines


def _file_texts(path: Path) -> List[str]:
    return [i["text"] for i in json.loads(path.read_text(encoding="utf-8"))["items"]]


# -- remember ---------------------------------------------------------------------------


def test_remember_says_got_it_logs_it_and_writes_the_file(rig) -> None:
    """Remember says got it logs it and writes the file."""
    assert rig["say"]("Remember that my exam is on Tuesday.") == "Got it."
    assert _file_texts(rig["path"]) == ["my exam is on tuesday"]
    assert "Remembered: my exam is on tuesday" in _activity(rig)
    record = rig["logbook"].last_completed()
    assert record.rule_or_trust_level == TrustLevel.L0
    assert rig["groq"].questions == []


@pytest.mark.parametrize("said", ["Don't forget my sister's birthday is on 4 May",
                                  "Jarvis, please remember my sister's birthday is on 4 May",
                                  "do not forget that my sister's birthday is on 4 May"])
def test_other_ways_to_say_remember(rig, said) -> None:
    """Other ways to say remember."""
    assert rig["say"](said) == "Got it."
    assert _file_texts(rig["path"]) == ["my sister's birthday is on 4 may"]


def test_typed_remember_works_too(rig) -> None:
    """Typed remember works too."""
    assert rig["say"]("remember the wifi router is in the hallway", typed=True) == "Got it."
    assert _file_texts(rig["path"]) == ["the wifi router is in the hallway"]


def test_remember_with_nothing_after_it_asks_what(rig) -> None:
    """Remember with nothing after it asks what."""
    assert rig["say"]("remember") == R["remember_empty"]
    assert not rig["path"].exists()


def test_a_command_inside_remember_is_still_a_memory(rig) -> None:
    """A command inside remember is still a memory."""
    rig["say"]("remember to open the windows at 5")
    assert rig["launched"] == [] and _file_texts(rig["path"]) == ["to open the windows at 5"]


# -- never store ------------------------------------------------------------------------


@pytest.mark.parametrize("said, kind", [
    ("remember my password is hunter2", "password"),
    ("remember my bank pin is 4821", "PIN"),
    ("remember my card number 4111 1111 1111 1111", "card number"),  # Luhn-valid
    ("remember 4111-1111-1111-1111 expires in May", "card number"),
    ("remember my BSB and account are 062-000 12345678", "bank account number"),
    ("remember my account number is 1234 5678", "bank account number"),
    ("remember my passport number is PA1234567", "passport number"),
    ("remember my driver's licence is 12345678", "driver's licence number"),
    ("remember my tax file number is 123 456 782", "tax file number"),
    ("remember my medicare is 2123 45670 1", "Medicare number"),
])
def test_never_store_refuses_and_writes_nothing_anywhere(rig, said, kind) -> None:
    """Never store refuses and writes nothing anywhere."""
    secret = said.split(" is ")[-1] if " is " in said else said.split("remember ")[-1]
    article = "an" if kind[0].lower() in "aeiou" else "a"
    assert rig["say"](said) == f"I won't store that. It looks like {article} {kind}."
    assert not rig["path"].exists()  # memory untouched
    # Nowhere else either: not in Live Activity, not in any record, not in debug.log.
    for line in _activity(rig):
        assert secret.lower() not in line.lower()
    for record in rig["logbook"].records():
        assert secret.lower() not in repr(record).lower()
    debug = rig["debug"].read_text(encoding="utf-8").lower() if rig["debug"].exists() else ""
    assert secret.lower() not in debug and "4111" not in debug
    assert rig["groq"].questions == []


def test_a_card_number_that_fails_luhn_is_just_a_number(rig) -> None:
    """A card number that fails luhn is just a number."""
    assert not luhn_valid("1234567812345678")
    assert sensitive_type("remember the parcel code 1234 5678 1234 5678") is None
    assert rig["say"]("remember the parcel code 1234 5678 1234 5678") == "Got it."


def test_luhn() -> None:
    """Luhn."""
    assert luhn_valid("4111111111111111") and luhn_valid("5555555555554444") and luhn_valid("378282246310005")
    assert not luhn_valid("4111111111111112")


def test_redaction_uses_the_same_check() -> None:
    """Redaction uses the same check."""
    assert redact_sensitive("remember my card 4111 1111 1111 1111") == SETTINGS["logbook"]["redacted_placeholder"]
    assert redact_sensitive("my exam is on tuesday") == "my exam is on tuesday"
    assert redact_sensitive("find my passport") == "find my passport"  # no number: an ordinary F4 search


# -- recall -----------------------------------------------------------------------------


def test_recall_turns_it_round_with_the_date(rig) -> None:
    """Recall turns it round with the date."""
    rig["say"]("remember that my exam is on tuesday")
    reply = rig["say"]("what do you know about my exam")
    assert reply == "You told me your exam is on tuesday. You said that on 23 October 2026."
    assert rig["groq"].questions == []


def test_recall_nothing(rig) -> None:
    """Recall nothing."""
    assert rig["say"]("What do you remember about my car?") == "You haven't told me anything about your car."


def test_recall_gives_the_three_newest(rig) -> None:
    """Recall gives the three newest."""
    for day, fact in enumerate(["exam one is maths", "exam two is physics", "exam three is chemistry", "exam four is english"], 1):
        rig["clock"]["now"] = NOW.replace(day=day)
        rig["say"](f"remember {fact}")
    reply = rig["say"]("what do you know about exams")
    assert reply.count("You told me") == 3
    assert "english" in reply and "chemistry" in reply and "physics" in reply and "maths" not in reply
    assert reply.index("english") < reply.index("chemistry")  # newest first
    assert "You said that on 4 October 2026." in reply


def test_what_do_you_remember_counts_speaks_five_and_lists_all(rig) -> None:
    """What do you remember counts speaks five and lists all."""
    for n in range(7):
        rig["clock"]["now"] = NOW.replace(minute=n)
        rig["say"](f"remember fact number {n} is true")
    reply = rig["say"]("what do you remember")
    assert reply.startswith("I remember 7 things. The newest 5:")
    assert "fact number 6" in reply and "fact number 2" in reply and "fact number 1" not in reply
    listed = [line for line in _activity(rig) if " is true (" in line]
    assert len(listed) == 7  # the full list in Live Activity


def test_what_do_you_remember_when_empty(rig) -> None:
    """What do you remember when empty."""
    assert rig["say"]("what do you remember") == R["list_none"]


# -- forget -----------------------------------------------------------------------------


def test_forget_one_match(rig) -> None:
    """Forget one match."""
    rig["say"]("remember my exam is on tuesday")
    rig["say"]("remember the gym opens at 6")
    assert rig["say"]("forget my exam") == "Forgotten."
    assert _file_texts(rig["path"]) == ["the gym opens at 6"]
    assert rig["logbook"].last_completed().rule_or_trust_level == TrustLevel.L1
    assert "my exam" not in rig["path"].with_name("memory.json.bak").read_text(encoding="utf-8")  # deleted stays deleted


def test_forget_several_matches_asks_which_then_picks(rig) -> None:
    """Forget several matches asks which then picks."""
    rig["say"]("remember the maths exam is on monday")
    rig["say"]("remember the physics exam is on friday")
    reply = rig["say"]("forget the exam")
    assert reply.startswith("I have 2 things about the exam: 1,") and reply.endswith("Say the number.")
    assert rig["say"]("the second one") == "Forgotten."
    assert len(_file_texts(rig["path"])) == 1


def test_forget_nothing_matching(rig) -> None:
    """Forget nothing matching."""
    assert rig["say"]("forget my car") == "I don't have anything about your car to forget."


def test_forget_it_is_not_a_memory_command(rig) -> None:
    """Forget it is not a memory command."""
    rig["say"]("remember the gym opens at 6")
    rig["say"]("forget it")
    assert _file_texts(rig["path"]) == ["the gym opens at 6"]


# -- corrections --------------------------------------------------------------------------


def test_correction_updates_the_one_clear_match(rig) -> None:
    """Correction updates the one clear match."""
    rig["say"]("remember my exam is on monday")
    rig["say"]("remember the gym opens at 6")
    rig["clock"]["now"] = NOW.replace(day=24)
    assert rig["say"]("Actually, my exam is on Tuesday.") == "Updated."
    assert _file_texts(rig["path"]) == ["my exam is on tuesday", "the gym opens at 6"]
    assert rig["say"]("what do you know about my exam").endswith("You said that on 24 October 2026.")


def test_correction_with_no_clear_match_is_stored_new(rig) -> None:
    """Correction with no clear match is stored new."""
    rig["say"]("remember the gym opens at 6")
    assert rig["say"]("actually my dentist is on friday") == "Got it."
    assert len(_file_texts(rig["path"])) == 2


def test_actually_with_a_command_or_a_question_is_not_a_memory(rig) -> None:
    """Actually with a command or a question is not a memory."""
    rig["say"]("actually, open notepad")
    assert rig["launched"] == ["notepad.exe"]
    rig["say"]("actually never mind")
    rig["say"]("actually what is the capital of france")
    assert not rig["path"].exists()


# -- forget everything: L4 ------------------------------------------------------------------


def test_forget_everything_needs_two_confirmations(rig) -> None:
    """Forget everything needs two confirmations."""
    rig["say"]("remember my exam is on tuesday")
    rig["say"]("remember the gym opens at 6")
    prompt = rig["say"]("forget everything")
    assert rig["c"].state == State.CONFIRMING
    assert prompt == R["forget_all_prompt"].format(count=2)
    assert rig["c"].pending_confirmation().trust_level == TrustLevel.L4
    assert rig["confirm"]() == SETTINGS["conversation"]["confirm_again_reply"]
    assert rig["c"].state == State.CONFIRMING and len(_file_texts(rig["path"])) == 2  # one confirm is not enough
    assert rig["confirm"]() == R["forget_all_done"]
    assert _file_texts(rig["path"]) == [] and _file_texts(rig["path"].with_name("memory.json.bak")) == []


def test_forget_everything_cancelled_keeps_everything(rig) -> None:
    """Forget everything cancelled keeps everything."""
    rig["say"]("remember my exam is on tuesday")
    rig["say"]("forget everything")
    rig["orch"].handle_typed_command("cancel")
    assert _wait(lambda: rig["c"].state == State.AWAKE)
    assert _file_texts(rig["path"]) == ["my exam is on tuesday"]


def test_forget_everything_when_empty(rig) -> None:
    """Forget everything when empty."""
    assert rig["say"]("forget everything") == R["forget_all_empty"]
    assert rig["c"].state == State.AWAKE


def test_trust_levels_are_fixed_in_code() -> None:
    """Trust levels are fixed in code."""
    assert TRUST_TABLE["remember"] == TrustLevel.L0
    assert TRUST_TABLE["forget_memory"] == TrustLevel.L1
    assert TRUST_TABLE["forget_all_memory"] == TrustLevel.L4


# -- never online ---------------------------------------------------------------------------


def test_memory_is_never_sent_online(rig) -> None:
    """Memory is never sent online."""
    rig["say"]("remember my exam is on tuesday")
    rig["say"]("what do you know about my exam")
    rig["say"]("what do you remember")
    rig["say"]("forget my exam")
    assert rig["groq"].questions == []  # no memory command reaches Groq
    rig["say"]("remember my exam is on tuesday")
    rig["say"]("when should I start revising for an exam?")  # a real online question
    assert rig["groq"].questions == ["when should i start revising for an exam"]  # voice: "?" dropped
    assert not any("tuesday" in q for q in rig["groq"].questions)  # nothing remembered rides along


def test_the_memory_package_imports_nothing_online() -> None:
    """The memory package imports nothing online."""
    root = Path(__file__).resolve().parent.parent / "jarvis" / "memory"
    for source in root.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        for banned in ("jarvis.online", "jarvis.speech.qa", "groq", "httpx", "urllib", "requests", "socket"):
            assert banned not in text, f"{source.name} mentions {banned}"


def test_conversations_are_never_stored(rig) -> None:
    """Conversations are never stored."""
    rig["say"]("what is the capital of france")
    rig["say"]("my exam is on tuesday")  # said, not "remember": not stored
    assert not rig["path"].exists()


# -- why --------------------------------------------------------------------------------------


def test_why_did_you_do_that_after_remembering(rig) -> None:
    """Why did you do that after remembering."""
    rig["say"]("remember my exam is on tuesday")
    why = rig["say"]("why did you do that")
    assert why.startswith("I remembered my exam is on tuesday because you asked me to remember my exam is on tuesday.")
    assert "level 0" in why


# -- the file ----------------------------------------------------------------------------------


def test_survives_a_restart(approved) -> None:
    """Survives a restart."""
    path = approved / "memory.json"
    MemoryStore(path, now=lambda: NOW).add("my exam is on tuesday")
    again = MemoryStore(path)
    assert [i.text for i in again.items()] == ["my exam is on tuesday"]
    assert again.items()[0].saved_at == NOW


def test_writes_are_atomic_and_backed_up(approved) -> None:
    """Writes are atomic and backed up."""
    store = MemoryStore(approved / "memory.json", now=lambda: NOW)
    store.add("one")
    store.add("two")
    assert not list(approved.glob("*.tmp"))
    assert (approved / "memory.json").read_text(encoding="utf-8") == (approved / "memory.json.bak").read_text(encoding="utf-8")


def test_cap(rig, monkeypatch) -> None:
    """Cap."""
    rig["store"].max_items = 3
    for n in range(3):
        rig["say"](f"remember item {n} is here")
    assert rig["say"]("remember item 3 is here") == R["full"].format(max=3)
    assert len(_file_texts(rig["path"])) == 3


def test_corrupt_file_is_kept_and_the_last_good_copy_restored(approved) -> None:
    """Corrupt file is kept and the last good copy restored."""
    path = approved / "memory.json"
    MemoryStore(path, now=lambda: NOW).add("my exam is on tuesday")
    path.write_text("{not json", encoding="utf-8")
    store = MemoryStore(path, now=lambda: NOW)
    assert [i.text for i in store.items()] == ["my exam is on tuesday"]
    damaged = list(approved.glob("memory.corrupt-*.json"))
    assert len(damaged) == 1 and damaged[0].read_text(encoding="utf-8") == "{not json"
    assert store.load_note.startswith("My memory file was damaged. I kept it as memory.corrupt-")
    assert "with 1 things" in store.load_note or "with 1 thing" in store.load_note


def test_corrupt_file_without_a_backup_starts_empty(approved) -> None:
    """Corrupt file without a backup starts empty."""
    path = approved / "memory.json"
    path.write_text("[1, 2", encoding="utf-8")
    store = MemoryStore(path, now=lambda: NOW)
    assert store.items() == [] and store.load_note.startswith("My memory file was damaged and there was no good copy")
    assert json.loads(path.read_text(encoding="utf-8"))["items"] == []


def test_writes_outside_an_approved_folder_are_refused(tmp_path) -> None:
    """Writes outside an approved folder are refused."""
    store = MemoryStore(tmp_path / "memory.json")  # tmp_path is NOT approved here
    with pytest.raises(PermissionError):
        store.add("anything")


def test_turn_round() -> None:
    """Turn round."""
    assert turn_round("my exam is on tuesday and I'm ready") == "your exam is on tuesday and you're ready"
    assert turn_round("give me my keys") == "give you your keys"
