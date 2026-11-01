"""F2 voice: Kokoro (kokoro-onnx), voice af_bella, is JARVIS's voice (the
developer's choice after comparing voice samples). Piper
(piper-tts) stays optional. Both are free, local and offline. Which one
speaks is [speech] engine; pyttsx3/Zira is only the fallback.

Same contract as Pyttsx3SpeechOutput, so nothing else changes:
- speak() blocks until the text has been said (or Stop / the timeout);
- stop() (R8) cuts the current sentence within one playback block
  ([speech] neural_block_seconds) and drops everything queued;
- is_speaking() is true while anything is queued or playing, so the voice
  listener's echo guard ignores the microphone (she never hears herself);
- markdown is stripped before speaking (plain_text.to_speakable), like
  every online answer.

The text is split into sentences: the next sentence is synthesised while
the current one plays, so she starts talking after the first sentence
instead of after the whole reply, and a long first sentence is cut at its
first clause ([speech] first_chunk_max_chars). The model loads and warms
up on its own thread when JARVIS starts (R15: never blocking the window;
the package isn't even imported until then); speak() waits for it up to
[speech] startup_timeout_seconds. last_first_audio_seconds is how long the
last line took from speak() to its first sound.

make_speech_output() picks the engine. A missing package or model file
falls back to pyttsx3 with a missing-setup message that says exactly what
to run (R14) -- never a crash, never silence.
"""

from __future__ import annotations

import importlib.util
import logging
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Tuple

from jarvis.settings import SETTINGS
from jarvis.speech.output import SpeechOutput
from jarvis.speech.plain_text import to_speakable
from jarvis.speech.tts import Pyttsx3SpeechOutput, VoiceStatus, split_sentences
from jarvis.speech.voice_cache import VoiceCache, read_wav, write_wav

logger = logging.getLogger(__name__)

_SPEECH = SETTINGS["speech"]
_MESSAGES = SETTINGS["setup_messages"]
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

ENGINES = ("pyttsx3", "kokoro", "piper")

_CLAUSE_BREAK = re.compile(r"[,;:]\s+|\s+(?:-|--|–|—)\s+")


class Synth(Protocol):
    """What a neural voice must provide: load the model, then turn one sentence into (samples, sample
    rate)."""
    name: str

    def load(self) -> None:
        """Load the voice model (slow; done once, off the main thread)."""

    def synth(self, sentence: str) -> Tuple[Any, int]:
        """Synthesise `sentence`. Returns (audio samples, sample rate)."""



def speaking_chunks(
    text: str,
    max_chars: Optional[int] = None,
    min_chars: Optional[int] = None,
    tail_ratio: Optional[float] = None,
) -> list[str]:
    """Sentences, except that a first sentence longer than max_chars is cut
    after its earliest clause break that leaves a head of at least
    min_chars and a tail no longer than tail_ratio x the head: the first
    sound comes sooner, and the tail is ready before the head has finished
    playing (a longer tail would leave a silence mid-sentence, worse than
    starting a little later). No such break: the sentence stays whole."""
    max_chars = int(_SPEECH["first_chunk_max_chars"] if max_chars is None else max_chars)
    min_chars = int(_SPEECH["first_chunk_min_chars"] if min_chars is None else min_chars)
    tail_ratio = float(_SPEECH["first_chunk_tail_ratio"] if tail_ratio is None else tail_ratio)
    chunks = split_sentences(text) or [text]
    first = chunks[0]
    if len(first) > max_chars:
        for match in _CLAUSE_BREAK.finditer(first):
            head, tail = first[: match.start() + 1].rstrip(), first[match.end() :].strip()
            if len(head) >= min_chars and tail and len(tail) <= tail_ratio * len(head):
                return [head, tail, *chunks[1:]]
    return chunks


def _installed(module: str) -> bool:
    """Whether a package is installed, without importing it: kokoro_onnx
    takes 0.6 s to import, and this runs before the window opens (R15)."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


# -- the two engines --------------------------------------------------------------


class KokoroSynth:
    """Her voice: Kokoro-82M (af_bella) through kokoro-onnx, on the CPU."""

    def __init__(self, cfg=None) -> None:
        cfg = cfg or _SPEECH["kokoro"]
        folder = _PROJECT_ROOT / str(cfg["model_dir"])
        self.model_path = folder / str(cfg["model_file"])
        self.voices_path = folder / str(cfg["voices_file"])
        self.voice = str(cfg["voice"])
        self.speed = float(cfg["speed"])
        self.language = str(cfg["language"])
        self.name = f"Kokoro {self.voice}"
        # Besides the text, what changes the sound (the voice cache's key).
        self.signature = {"engine": "kokoro", "model": str(cfg["model_file"]), "voice": self.voice,
                          "speed": self.speed, "language": self.language}
        self._engine = None

    def missing(self) -> Optional[str]:
        """What is missing (package or model files) as a setup message, or None when ready."""
        if not _installed("kokoro_onnx"):
            return _MESSAGES["voice_engine_library_missing"].format(engine="Kokoro", requirements="requirements.txt")
        if not self.model_path.exists() or not self.voices_path.exists():
            return _MESSAGES["voice_engine_model_missing"].format(engine="Kokoro", command="python scripts\\download_models.py")
        return None

    def load(self) -> None:
        """Load the Kokoro model and voices file."""
        from kokoro_onnx import Kokoro

        self._engine = Kokoro(str(self.model_path), str(self.voices_path))

    def warm_up(self) -> None:
        """Synthesise a short line once, so the first real line starts quickly."""
        self.synth(str(_SPEECH["warm_up_text"]))

    def synth(self, sentence: str):
        """Synthesise one sentence with the configured voice, speed and language."""
        return self._engine.create(sentence, voice=self.voice, speed=self.speed, lang=self.language)


class PiperSynth:
    """An optional Piper voice (requirements-voices.txt)."""

    def __init__(self, cfg=None) -> None:
        cfg = cfg or _SPEECH["piper"]
        self.model_path = _PROJECT_ROOT / str(cfg["model_dir"]) / f"{cfg['voice']}.onnx"
        self.length_scale = float(cfg["length_scale"])
        self.name = f"Piper {cfg['voice']}"
        self.signature = {"engine": "piper", "voice": str(cfg["voice"]), "length_scale": self.length_scale}
        self._voice = None

    def missing(self) -> Optional[str]:
        """What is missing (package or model files) as a setup message, or None when ready."""
        if not _installed("piper"):
            return _MESSAGES["voice_engine_library_missing"].format(engine="Piper", requirements="requirements-voices.txt")
        if not self.model_path.exists() or not Path(str(self.model_path) + ".json").exists():
            return _MESSAGES["voice_engine_model_missing"].format(
                engine="Piper", command="python scripts\\download_models.py --voices piper")
        return None

    def load(self) -> None:
        """Load the Piper voice model."""
        from piper import PiperVoice

        self._voice = PiperVoice.load(self.model_path)

    def warm_up(self) -> None:
        """Synthesise a short line once, so the first real line starts quickly."""
        self.synth(str(_SPEECH["warm_up_text"]))

    def synth(self, sentence: str):
        """Synthesise one sentence with Piper."""
        import numpy as np
        from piper import SynthesisConfig

        chunks = list(self._voice.synthesize(sentence, syn_config=SynthesisConfig(length_scale=self.length_scale)))
        if not chunks:
            return np.zeros(0, dtype=np.float32), 22050
        return np.concatenate([c.audio_float_array for c in chunks]).astype("float32"), chunks[0].sample_rate


# -- playback ---------------------------------------------------------------------------


Player = Callable[[Any, int, Callable[[], bool]], bool]


def trim_silence(samples, rate: int, keep_seconds: Optional[float] = None, threshold: Optional[float] = None):
    """The sentence without the quiet the model puts before and after it,
    keeping [speech] edge_keep_seconds so no sound is clipped (Kokoro's own
    edges made most of the gap between sentences)."""
    import numpy as np

    keep = int(rate * float(_SPEECH["edge_keep_seconds"] if keep_seconds is None else keep_seconds))
    level = float(_SPEECH["silence_threshold"] if threshold is None else threshold)
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    loud = np.nonzero(np.abs(samples) > level)[0]
    if len(loud) == 0:
        return samples[:0]
    return samples[max(0, int(loud[0]) - keep): min(len(samples), int(loud[-1]) + 1 + keep)]


def sentence_gap(rate: int):
    """[speech] sentence_gap_seconds of silence, put between sentences."""
    import numpy as np

    return np.zeros(int(rate * float(_SPEECH["sentence_gap_seconds"])), dtype=np.float32)


class StreamLine:
    """One output stream for a whole line: opening the
    sound device for every sentence added its start-up time to every gap.
    Played in small blocks, so Stop still cuts within one block."""

    def __init__(self) -> None:
        self._stream = None
        self._rate = None

    def play(self, samples, rate: int, should_stop: Callable[[], bool]) -> bool:
        """Play `samples` through the speakers in small blocks, stopping early when `should_stop()` is
        true. True if it played to the end."""
        import sounddevice as sd

        if self._stream is None or self._rate != rate:
            self.close()
            self._stream = sd.OutputStream(samplerate=rate, channels=1, dtype="float32")
            self._stream.start()
            self._rate = rate
        block = max(1, int(rate * float(_SPEECH["neural_block_seconds"])))
        for start in range(0, len(samples), block):
            if should_stop():
                self._stream.abort()
                self.close()
                return False
            self._stream.write(samples[start : start + block].reshape(-1, 1))
        return True

    def close(self) -> None:
        """Close the output stream."""
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()  # plays what is still buffered, then closes
                stream.close()
            except Exception:
                logger.debug("Closing the speech stream failed", exc_info=True)


def sounddevice_player(samples, rate: int, should_stop: Callable[[], bool]) -> bool:
    """Play mono float32 samples in small blocks on the default output, so
    Stop takes effect within one block. Returns False if it was cut."""
    import sounddevice as sd

    block = max(1, int(rate * float(_SPEECH["neural_block_seconds"])))
    with sd.OutputStream(samplerate=rate, channels=1, dtype="float32") as stream:
        for start in range(0, len(samples), block):
            if should_stop():
                stream.abort()
                return False
            stream.write(samples[start : start + block].reshape(-1, 1))
    return True


# -- the speech output -------------------------------------------------------------------


# Classes & Objects: inherits SpeechOutput and overrides speak/stop with a queued worker thread.
class NeuralSpeechOutput(SpeechOutput):
    """F2: speaks through a neural voice on one worker thread: sentences synthesised and played in order,
    cached lines played at once, Stop cuts instantly (R8)."""
    def __init__(
        self,
        synth: Synth,
        player: Optional[Player] = None,
        speak_timeout_seconds: Optional[float] = None,
        load_timeout_seconds: Optional[float] = None,
        cache: Optional[VoiceCache] = None,
    ) -> None:
        self._synth = synth
        self._cache = cache
        # One synthesis at a time: a line's synthesiser, a cut line's
        # leftover sentence and the cache renderer share one model (and
        # eSpeak, which isn't known to be thread-safe).
        self._synth_lock = threading.Lock()
        self._render_thread: Optional[threading.Thread] = None
        self._closed = threading.Event()
        self._player = player or sounddevice_player
        self._speak_timeout = float(speak_timeout_seconds or _SPEECH["speak_timeout_seconds"])
        self._load_timeout = float(load_timeout_seconds or _SPEECH["startup_timeout_seconds"])
        self._jobs: "queue.Queue[Optional[tuple]]" = queue.Queue()  # (text, done, queued at, still_valid, said)
        self._cut = threading.Event()
        self._speaking = threading.Event()
        self._loaded = threading.Event()
        self.last_cut_at: Optional[float] = None
        self.last_first_audio_seconds: Optional[float] = None
        self._producer: Optional[threading.Thread] = None  # the last line's synthesiser
        self.status = VoiceStatus(ready=True, voice_name=synth.name)  # until loading says otherwise
        self._thread = threading.Thread(target=self._run, name="jarvis-neural-tts", daemon=True)
        self._thread.start()

    def speak(self, text: str) -> None:
        """Say `text` and wait until it has been said (or the timeout passes)."""
        self._queue_and_wait(text, None)

    def speak_while(self, text: str, still_valid: Callable[[], bool]) -> bool:
        """Checked again when the line reaches the front of the queue."""
        if not still_valid():
            return False
        return self._queue_and_wait(text, still_valid)

    def _queue_and_wait(self, text: str, still_valid: Optional[Callable[[], bool]]) -> bool:
        text = to_speakable(text or "").strip()
        if not text or not self.status.ready:
            return False
        if not self._loaded.wait(self._load_timeout) or not self.status.ready:
            logger.warning("%s not loaded in time; this line is text only", self._synth.name)
            return False
        done, said = threading.Event(), []
        self._jobs.put((text, done, time.monotonic(), still_valid, said))
        if not done.wait(self._speak_timeout):
            logger.warning("Speech took longer than %.0fs; carrying on", self._speak_timeout)
        return bool(said)

    def is_speaking(self) -> bool:
        """True while a line is playing or waiting in the queue."""
        return self._speaking.is_set() or not self._jobs.empty()

    def wait_until_loaded(self, timeout: Optional[float] = None) -> bool:
        """Wait for the voice model to load. True if it is ready."""
        return self._loaded.wait(self._load_timeout if timeout is None else timeout)

    def stop(self) -> None:
        """R8: drop every queued line and cut the one playing."""
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                break
            if job is None:
                self._jobs.put(None)
                break
            job[1].set()
        if self._speaking.is_set():
            self._cut.set()

    def close(self) -> None:
        """Stop speaking and end the worker thread (shutdown)."""
        self._closed.set()
        self.stop()
        self._jobs.put(None)

    def render_cache(self) -> Optional[threading.Thread]:
        """Start rendering the fixed lines that aren't cached yet, in the
        background (called once JARVIS has started). Safe to call twice."""
        if self._cache is None or (self._render_thread is not None and self._render_thread.is_alive()):
            return self._render_thread
        self._render_thread = threading.Thread(target=self._render, name="jarvis-voice-cache", daemon=True)
        self._render_thread.start()
        return self._render_thread

    # -- worker -----------------------------------------------------------------

    def _run(self) -> None:
        started = time.monotonic()
        try:
            self._synth.load()
            loaded = time.monotonic()
            warm_up = getattr(self._synth, "warm_up", None)
            if warm_up is not None:
                warm_up()
            logger.info("%s loaded in %.1fs, warmed up in %.1fs", self._synth.name,
                        loaded - started, time.monotonic() - loaded)
            if self._cache is not None:
                self._cache.index()  # lines already on disk play from the cache from now on
        except Exception:
            logger.exception("%s failed to load -- text only (R14)", self._synth.name)
            self.status = VoiceStatus(ready=False, voice_name=self._synth.name, note=_MESSAGES["tts_engine_failed"])
            self._loaded.set()
            return
        self._loaded.set()
        while True:
            job = self._jobs.get()
            if job is None:
                return
            text, done, queued_at, still_valid, said = job
            if still_valid is not None and not still_valid():
                done.set()  # its moment passed while it waited (a confirm prompt after the result)
                continue
            said.append(True)
            self._cut.clear()
            self._speaking.set()
            try:
                self._say(text, queued_at)
            except Exception:
                logger.exception("Speaking failed; carrying on (R14)")
            finally:
                if self._cut.is_set():
                    self.last_cut_at = time.monotonic()
                self._speaking.clear()
                self._cut.clear()
                done.set()

    def _say(self, text: str, queued_at: float) -> None:
        """Synthesise sentence n+1 while sentence n plays.

        Stop must not wait for synthesis: one Kokoro call can't be
        interrupted and takes up to a few seconds, and until this returns
        speak() blocks and is_speaking() keeps the microphone muted (seen
        on this laptop, Stop took 3.7 s). So a cut line
        returns at once and its synthesiser thread is left to finish its
        sentence and exit; the next line waits for it first, so two
        syntheses never compete for the CPU."""
        if self._producer is not None:
            self._producer.join()
        ready: "queue.Queue[Optional[Tuple[Any, int]]]" = queue.Queue(maxsize=2)
        cancelled = threading.Event()

        def offer(item) -> None:
            while not cancelled.is_set():
                try:
                    ready.put(item, timeout=0.05)
                    return
                except queue.Full:
                    pass

        segments = self._cache.split(text) if self._cache is not None else [("live", None, text)]

        def produce() -> None:
            try:
                for kind, path, part in segments:
                    if kind == "cached":
                        try:
                            offer(read_wav(path))
                            continue
                        except Exception:
                            logger.warning("Cached audio %s unreadable; saying it live", path.name, exc_info=True)
                    for sentence in speaking_chunks(part):
                        if cancelled.is_set():
                            return
                        with self._synth_lock:
                            samples, rate = self._synth.synth(sentence)
                        offer((trim_silence(samples, rate), rate))
            except Exception:
                logger.exception("Synthesis failed")
            finally:
                offer(None)

        producer = threading.Thread(target=produce, name="jarvis-neural-tts-synth", daemon=True)
        self._producer = producer
        producer.start()
        first = True
        # One stream for the whole line (the default player), and
        # exactly [speech] sentence_gap_seconds between sentences.
        line = StreamLine() if self._player is sounddevice_player else None
        play = line.play if line is not None else self._player
        try:
            while not self._cut.is_set():
                try:
                    item = ready.get(timeout=0.05)  # Stop is seen even between sentences
                except queue.Empty:
                    continue
                if item is None:
                    break
                samples, rate = item
                if first:
                    first = False
                    self.last_first_audio_seconds = time.monotonic() - queued_at
                    logger.info("First audio %.2fs after the line was ready", self.last_first_audio_seconds)
                else:
                    import numpy as np

                    samples = np.concatenate([sentence_gap(rate), np.asarray(samples, dtype=np.float32).reshape(-1)])
                if not play(samples, rate, self._cut.is_set):
                    break
        finally:
            cancelled.set()
            if line is not None:
                line.close()

    def _render(self) -> None:
        """Render each fixed line that has no file yet, a sentence at a
        time and only while she is quiet, so live speech never waits more
        than one sentence for the model; then delete outdated files."""
        import numpy as np

        if not self._loaded.wait(self._load_timeout * 6) or not self.status.ready:
            return
        started, made = time.monotonic(), 0
        pause = float(SETTINGS["voice_cache"]["join_pause_seconds"])
        try:
            for sentences, path in self._cache.index():
                if path.exists():
                    continue
                parts, rate = [], 24000
                for sentence in sentences:
                    while self.is_speaking() and not self._closed.is_set():
                        time.sleep(0.1)
                    if self._closed.is_set():
                        return
                    with self._synth_lock:
                        samples, rate = self._synth.synth(sentence)
                    parts += [np.asarray(samples, dtype=np.float32), np.zeros(int(rate * pause), dtype=np.float32)]
                write_wav(path, np.concatenate(parts[:-1]), rate)
                made += 1
            removed = self._cache.prune()
            logger.info("Voice cache: %d line(s) rendered in %.1fs, %d outdated file(s) removed",
                        made, time.monotonic() - started, removed)
        except Exception:
            logger.exception("Voice cache rendering failed; those lines are made live (R14)")


def make_speech_output(engine: Optional[str] = None) -> SpeechOutput:
    """The voice [speech] engine asks for. Kokoro or Piper missing (package
    or model file): pyttsx3/Zira instead, with a note on the dashboard and
    spoken at startup saying exactly what to run (R14)."""
    engine = (engine or str(_SPEECH.get("engine", "pyttsx3"))).strip().lower()
    if engine not in ENGINES:
        fallback = Pyttsx3SpeechOutput()
        fallback.status = VoiceStatus(fallback.status.ready, fallback.status.voice_name,
                                      _MESSAGES["voice_engine_unknown"].format(engine=engine))
        return fallback
    if engine == "pyttsx3":
        return Pyttsx3SpeechOutput()
    synth = KokoroSynth() if engine == "kokoro" else PiperSynth()
    missing = synth.missing()
    if missing is not None:
        fallback = Pyttsx3SpeechOutput()
        fallback.status = VoiceStatus(fallback.status.ready, fallback.status.voice_name, missing)
        return fallback
    cache = VoiceCache(synth.signature) if bool(SETTINGS["voice_cache"]["enabled"]) else None
    return NeuralSpeechOutput(synth, cache=cache)
