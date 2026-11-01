"""F2 text-to-speech: pyttsx3 with the Microsoft Zira Desktop voice.

One dedicated worker thread creates and owns the pyttsx3 engine. pyttsx3's
Windows driver talks to SAPI through COM, and a COM object must be used
from the thread that created it -- so every utterance is handed to that
thread through a queue, and speak() waits for it to finish. That keeps
the orchestrator's existing contract: when speak() returns, she has
finished saying it, and SPEECH_FINISHED can be submitted.

Voice choice (developer decision E): the voice named in
settings (Zira). If it isn't installed, the first installed English voice,
plus a dashboard note saying how to add Zira. If speech can't start at
all, she degrades to text-only with a note saying what to set up (R14,
missing-setup rule) -- never a crash.

Stop cuts speech (R8, F2 Stage 3): stop() discards every queued utterance
and ends the current one at once. Two ways, chosen by [speech] stop_method
from a real test on the development laptop:
- "engine_stop": the speech thread itself calls pyttsx3's engine.stop()
  between event pumps (every [speech] pump_interval_seconds), so the COM
  call stays on the thread that owns the engine.
- "sentence": each utterance is spoken one sentence at a time and the stop
  flag is checked between sentences (worst case: one sentence late).
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from jarvis.settings import SETTINGS
from jarvis.speech.output import SpeechOutput

logger = logging.getLogger(__name__)

_SPEECH = SETTINGS["speech"]
_MESSAGES = SETTINGS["setup_messages"]

EngineFactory = Callable[[], Any]

STOP_ENGINE = "engine_stop"
STOP_SENTENCE = "sentence"

# A closing quote or bracket after the full stop ('…Up.” It')
# also ends a sentence (F18's live check spoke 4 sentences for a cap of 3).
_SENTENCE_SPLIT = re.compile(r"(?:(?<=[.!?])|(?<=[.!?][\"'”’)\]]))\s+")


def split_sentences(text: str) -> list[str]:
    """Split `text` into sentences, so each can be spoken (and stopped) separately."""
    return [s for s in (part.strip() for part in _SENTENCE_SPLIT.split(text)) if s]


@dataclass(frozen=True)
class VoiceStatus:
    """Whether her voice is ready, which voice it is, and a setup note if something is missing."""
    ready: bool
    voice_name: Optional[str] = None
    # A sentence for the dashboard when something needs the developer's
    # attention (voice missing, speech not set up). None when all is well.
    note: Optional[str] = None


def _default_engine_factory() -> Any:
    try:
        import comtypes

        comtypes.CoInitialize()  # COM must be initialised on the thread that uses it
    except Exception:
        pass  # pyttsx3.init() below reports any real problem
    import pyttsx3

    return pyttsx3.init()


def choose_voice(voices: list, wanted_name: str, language_prefix: str) -> tuple[Optional[Any], bool]:
    """Returns (voice, matched_wanted). The wanted voice first -- matched as
    a name prefix, case-insensitive, because Windows reports it as
    "Microsoft Zira Desktop - English (United States)" (seen on this laptop
    Not the bare name. Otherwise the first voice whose
    language starts with `language_prefix` (e.g. "en")."""
    wanted = wanted_name.strip().lower()
    for voice in voices:
        if str(getattr(voice, "name", "")).strip().lower().startswith(wanted):
            return voice, True
    prefix = language_prefix.lower()
    for voice in voices:
        languages = getattr(voice, "languages", None) or []
        if any(str(lang).lower().startswith(prefix) for lang in languages):
            return voice, False
    return None, False


class Pyttsx3SpeechOutput(SpeechOutput):
    """The fallback voice: Windows' SAPI (Microsoft Zira) through pyttsx3, on its own thread."""
    def __init__(
        self,
        engine_factory: Optional[EngineFactory] = None,
        voice_name: Optional[str] = None,
        language_prefix: Optional[str] = None,
        speak_timeout_seconds: Optional[float] = None,
        startup_timeout_seconds: Optional[float] = None,
        stop_method: Optional[str] = None,
    ) -> None:
        self._engine_factory = engine_factory or _default_engine_factory
        self._voice_name = voice_name or str(_SPEECH["voice_name"])
        self._language_prefix = language_prefix or str(_SPEECH["fallback_voice_language_prefix"])
        self._speak_timeout = float(speak_timeout_seconds or _SPEECH["speak_timeout_seconds"])
        self._pump_interval = float(_SPEECH["pump_interval_seconds"])
        startup_timeout = float(startup_timeout_seconds or _SPEECH["startup_timeout_seconds"])
        self.stop_method = str(stop_method or _SPEECH["stop_method"])
        self._stop_settle = float(_SPEECH["stop_settle_seconds"])
        # Set by stop() while an utterance is playing; the speech thread
        # sees it within one pump interval and ends the utterance.
        self._cut = threading.Event()
        self._speaking = threading.Event()
        self.last_cut_at: Optional[float] = None  # monotonic time the last cut took effect

        self._jobs: "queue.Queue[tuple | None]" = queue.Queue()  # (text, done, still_valid, said)
        self._ready_event = threading.Event()
        self.status = VoiceStatus(ready=False, note=_MESSAGES["tts_engine_failed"])
        self._thread = threading.Thread(target=self._run, name="jarvis-tts", daemon=True)
        self._thread.start()
        if not self._ready_event.wait(startup_timeout):
            logger.warning("TTS engine did not start within %.1fs -- text only (R14)", startup_timeout)

    def speak(self, text: str) -> None:
        """Blocks until the text has been spoken, or the timeout passes (a
        stuck engine must never freeze a command forever). Does nothing
        when speech isn't available -- the text is still on the dashboard."""
        self._queue_and_wait(text, None)

    def speak_while(self, text: str, still_valid: Callable[[], bool]) -> bool:
        """Checked again when the line reaches the front of the queue."""
        if not still_valid():
            return False
        return self._queue_and_wait(text, still_valid)

    def _queue_and_wait(self, text: str, still_valid: Optional[Callable[[], bool]]) -> bool:
        if not self.status.ready or not text.strip():
            return False
        done, said = threading.Event(), []
        self._jobs.put((text, done, still_valid, said))
        if not done.wait(self._speak_timeout):
            logger.warning("Speech took longer than %.0fs; carrying on", self._speak_timeout)
        return bool(said)

    def is_speaking(self) -> bool:
        """True while a line is playing or waiting in the queue."""
        return self._speaking.is_set() or not self._jobs.empty()

    def stop(self) -> None:
        """R8: end the current utterance now and discard queued ones. Safe
        from any thread, and a no-op when nothing is being said."""
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                break
            if job is None:
                self._jobs.put(None)  # keep a pending close() request
                break
            job[1].set()  # its speak() caller returns at once, unsaid
        if self._speaking.is_set():
            self._cut.set()

    def close(self) -> None:
        """End the speech thread (shutdown)."""
        self._jobs.put(None)

    # -- worker thread -------------------------------------------------
    #
    # Why not engine.runAndWait() per utterance: in pyttsx3 2.99 (read in
    # its own source) the driver is left "not busy" after the
    # first runAndWait, so on the next call the queued end-of-loop marker
    # is consumed before the loop starts -- runAndWait then either never
    # returns (seen on this laptop) or returns while she is still talking,
    # which would fire SPEECH_FINISHED early. pyttsx3's own external-loop
    # API (startLoop(False) + iterate(), finished-utterance callback) has
    # neither problem, and keeps every COM call on this one thread -- which
    # is also where Stage 3's Stop will cut speech.

    def _run(self) -> None:
        engine = self._start_engine()
        self._ready_event.set()
        if engine is None:
            return
        finished = threading.Event()
        try:
            engine.connect("finished-utterance", lambda name=None, completed=True: finished.set())
            engine.startLoop(False)
        except Exception:
            logger.exception("Could not start the speech loop -- text only (R14)")
            self.status = VoiceStatus(ready=False, note=_MESSAGES["tts_engine_failed"])
            return
        while True:
            job = self._jobs.get()
            if job is None:
                return
            text, done, still_valid, said = job
            if still_valid is not None and not still_valid():
                done.set()  # its moment passed while it waited (a confirm prompt after the result)
                continue
            said.append(True)
            self._cut.clear()
            self._speaking.set()
            try:
                parts = split_sentences(text) if self.stop_method == STOP_SENTENCE else [text]
                deadline = time.monotonic() + self._speak_timeout
                for part in parts:
                    if self._cut.is_set():
                        break  # sentence fallback: stop between sentences
                    finished.clear()
                    engine.say(part)
                    while not finished.is_set() and time.monotonic() < deadline:
                        if self._cut.is_set() and self.stop_method == STOP_ENGINE:
                            engine.stop()  # on the thread that owns the engine
                            self._drain_finished(engine, finished)
                            break
                        engine.iterate()  # pumps SAPI's events on this thread
                        time.sleep(self._pump_interval)
                    if self._cut.is_set() and self.stop_method == STOP_ENGINE:
                        break
                if self._cut.is_set():
                    self.last_cut_at = time.monotonic()
                    logger.info("Speech cut short by Stop (%s)", self.stop_method)
            except Exception:
                logger.exception("Speaking failed; carrying on (R14)")
            finally:
                self._speaking.clear()
                self._cut.clear()
                done.set()

    def _drain_finished(self, engine: Any, finished: threading.Event) -> None:
        """After engine.stop(), SAPI still delivers the cut utterance's own
        "finished" event a moment later. Pump until it arrives, so it can't
        be mistaken for the NEXT utterance finishing (found in the real
        stop test, The next line was skipped without this).
        Then keep pumping for [speech] stop_settle_seconds: an utterance
        started straight after the purge was silently dropped."""
        deadline = time.monotonic() + max(0.5, self._pump_interval * 25)
        while not finished.is_set() and time.monotonic() < deadline:
            engine.iterate()
            time.sleep(self._pump_interval)
        settle_until = time.monotonic() + self._stop_settle
        while time.monotonic() < settle_until:
            engine.iterate()
            time.sleep(self._pump_interval)

    def _start_engine(self) -> Optional[Any]:
        try:
            engine = self._engine_factory()
        except ImportError:
            self.status = VoiceStatus(ready=False, note=_MESSAGES["tts_library_missing"])
            return None
        except Exception:
            logger.exception("Could not start the speech engine")
            self.status = VoiceStatus(ready=False, note=_MESSAGES["tts_engine_failed"])
            return None
        try:
            voices = list(engine.getProperty("voices") or [])
        except Exception:
            voices = []
        voice, exact = choose_voice(voices, self._voice_name, self._language_prefix)
        if voice is None:
            self.status = VoiceStatus(ready=False, note=_MESSAGES["tts_no_english_voice"])
            return None
        try:
            engine.setProperty("voice", voice.id)
        except Exception:
            logger.exception("Could not select voice %s", getattr(voice, "name", "?"))
            self.status = VoiceStatus(ready=False, note=_MESSAGES["tts_engine_failed"])
            return None
        note = None if exact else _MESSAGES["tts_zira_missing"].format(voice=voice.name)
        self.status = VoiceStatus(ready=True, voice_name=voice.name, note=note)
        return engine
