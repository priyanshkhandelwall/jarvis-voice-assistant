"""Real-audio checks of her Kokoro voice (23 October 2026). Plays through
the default speakers and, for the echo check, listens on the default
microphone. It never focuses, moves or sends input to any window, and
presses no keys: Stop is triggered through the same calls the Stop
button and Ctrl+Shift+X make.

    venv\\Scripts\\python.exe tools\\voice_check.py [latency] [fixed] [words] [markdown] [stop] [echo] [ram] [--cache]

--cache uses (and first brings up to date) the real voice cache in
jarvis_data/voice_cache/; without it every line is synthesised live.

- latency: time from "answer ready" (speak() called) to the first audio
  handed to the sound device, for the wake greeting, the F1 intro and a
  three-sentence Groq answer (a real Groq call if GROQ_API_KEY is set).
- words: the full F1 intro is spoken; every chunk plays to the end, and
  the synthesised audio is transcribed with Whisper base.en and compared
  word for word with jarvis/settings/intro.txt.
- markdown: a markdown answer reaches the voice with no symbols in it.
- stop: Stop mid-sentence, through the hotkey's own calls; how long until
  the sound stops.
- echo: she says "Hey Jarvis" lines out loud while the real wake-word
  listener runs on the microphone (asleep); with the echo guard nothing
  may wake. Then once with the guard switched off, as the control.
  [wake_word] debug_scores is turned off in memory first, so this never
  writes to the developer's wake_scores.log.
"""

from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.settings import SETTINGS  # noqa: E402

SETTINGS["wake_word"]["debug_scores"] = False  # before any detector is built

from jarvis.actions.intro import load_intro_text  # noqa: E402
from jarvis.actions.wake_greeting import WakeGreeter  # noqa: E402
from jarvis.speech import neural_tts  # noqa: E402
from jarvis.speech.neural_tts import KokoroSynth, NeuralSpeechOutput, speaking_chunks  # noqa: E402

CANNED_ANSWER = ("Ottawa is the capital of Canada, located in the province of Ontario on the Ottawa River. "
                 "It was chosen as the capital by Queen Victoria in 1857. "
                 "Today it is home to Parliament Hill and about one million people.")


class Recorder:
    """Wraps the real synth and the real player to see what was said and played."""

    def __init__(self) -> None:
        self.synth = KokoroSynth()
        self.chunks: list[str] = []
        self.audio: list = []
        self.played: list[tuple[float, float, bool]] = []  # (started, seconds of audio, finished)
        self.player_returned_at = 0.0  # when the sound device stopped
        self.parts_started: list[tuple[float, float]] = []  # (started, seconds of audio), as each part starts
        self.name = self.synth.name

    def load(self) -> None:
        """Load the wrapped voice."""
        self.synth.load()

    def warm_up(self) -> None:
        """Warm the wrapped voice up."""
        self.synth.warm_up()

    def reset(self) -> None:
        """Forget everything recorded so far."""
        self.chunks.clear(), self.audio.clear(), self.played.clear(), self.parts_started.clear()

    def synth_one(self, sentence: str):
        """Synthesise one sentence and record it."""
        samples, rate = self.synth.synth(sentence)
        self.chunks.append(sentence)
        self.audio.append((samples, rate))
        return samples, rate

    def player(self, samples, rate, should_stop) -> bool:
        """Play through the real speakers and record when each part started."""
        started = time.monotonic()
        self.parts_started.append((started, len(samples) / rate))
        finished = neural_tts.sounddevice_player(samples, rate, should_stop)
        self.player_returned_at = time.monotonic()
        self.played.append((started, len(samples) / rate, finished))
        return finished


def make_output(cache: bool = False) -> tuple[NeuralSpeechOutput, Recorder]:
    """cache=True: the real voice cache in jarvis_data/voice_cache/,
    rendered (or brought up to date) before anything is measured."""
    from jarvis.speech.voice_cache import VoiceCache

    rec = Recorder()
    proxy = type("Proxy", (), {"name": rec.name, "load": lambda s: rec.load(), "warm_up": lambda s: rec.warm_up(),
                               "synth": lambda s, text: rec.synth_one(text)})()
    t = time.monotonic()
    out = NeuralSpeechOutput(proxy, player=rec.player, cache=VoiceCache(rec.synth.signature) if cache else None)
    made = time.monotonic() - t
    out.wait_until_loaded(60)
    print(f"Speech output created in {made * 1000:.0f} ms (the window is never kept waiting); "
          f"model loaded + warmed up {time.monotonic() - t:.1f} s after start, in the background.")
    if cache:
        t = time.monotonic()
        out.render_cache().join()
        files = list(out._cache.folder.glob("*.wav"))
        print(f"Voice cache: up to date after {time.monotonic() - t:.1f} s; {len(files)} files, "
              f"{sum(f.stat().st_size for f in files) / 2**20:.1f} MB in {out._cache.folder}")
        rec.reset()
    return out, rec


def words(text: str) -> list[str]:
    """The lower-case words of `text`."""
    return re.findall(r"[a-z0-9']+", text.lower())


def transcribe(rec: Recorder) -> str:
    """Transcribe what was recorded with Whisper (to check every word was spoken)."""
    import numpy as np
    from faster_whisper import WhisperModel
    from scipy.signal import resample_poly

    audio = np.concatenate([resample_poly(s, 2, 3).astype("float32") for s, r in rec.audio])  # 24 kHz -> 16 kHz
    model = WhisperModel(str(ROOT / "models" / "faster-whisper-base.en"), device="cpu", compute_type="int8")
    segments, _ = model.transcribe(audio, language="en", beam_size=5)
    return " ".join(s.text.strip() for s in segments)


def check_latency(out, rec) -> None:
    """Measure the time from an answer being ready to her first audio."""
    print("\n== Latency: answer ready -> first audio ==")
    import sounddevice as sd

    print(f"(Plus the output device's own latency, {sd.query_devices(kind='output')['default_low_output_latency'] * 1000:.0f} ms.)")
    lines = [("Wake greeting", WakeGreeter(state_path=None).choose()),
             ("F1 intro", load_intro_text())]
    for topic in ("Ottawa", "how rainbows form", "the history of chess", "why the sky is blue", "black holes"):
        try:
            from jarvis.speech.qa import GroqAnswerer, default_groq_service

            result = GroqAnswerer(service=default_groq_service()).answer(
                f"Tell me about {topic} in exactly three sentences.")
            if result.error is None and result.text:
                lines.append((f"Groq 3-sentence answer, {topic}", result.text))
                continue
        except Exception as exc:  # noqa: BLE001
            print(f"Groq failed ({exc}); using canned text")
        lines.append(("Canned 3-sentence answer (no Groq)", CANNED_ANSWER))
        break
    for label, text in lines:
        rec.reset()
        out.speak(text)
        first = speaking_chunks(neural_tts.to_speakable(text))[0]
        print(f"{label}: {out.last_first_audio_seconds:.2f} s | first chunk ({len(first)} chars): {first!r}")
        print(f"    silences between chunks (s): {gaps(rec)}")
        time.sleep(0.5)


def check_fixed(out, rec) -> None:
    """The fixed lines only (no Groq calls): three wake greetings and the
    F1 intro, with the silences between their parts."""
    print(f"\n== Fixed lines, {'CACHED' if out._cache is not None else 'live synthesis'} ==")
    greeter = WakeGreeter(state_path=None)
    for label, text in [("Wake greeting", greeter.choose()), ("Wake greeting", greeter.choose()),
                        ("Wake greeting", greeter.choose()), ("F1 intro", load_intro_text())]:
        rec.reset()
        out.speak(text)
        print(f"{label}: {out.last_first_audio_seconds:.2f} s | {text[:40]!r} | parts played {len(rec.played)}, "
              f"silences {gaps(rec)}, synthesised live {len(rec.chunks)}")
        time.sleep(0.5)


def gaps(rec: Recorder) -> list[float]:
    """Silence between one chunk ending and the next starting (0 = seamless)."""
    return [round(max(0.0, b[0] - (a[0] + a[1])), 2) for a, b in zip(rec.played, rec.played[1:])]


def check_words(out, rec) -> None:
    """Check the F1 intro is spoken word for word."""
    print("\n== F1 intro, word for word ==")
    rec.reset()
    text = load_intro_text()
    out.speak(text)
    expected = speaking_chunks(text)
    all_played = len(rec.played) == len(expected) and all(f for _, _, f in rec.played)
    print(f"Chunks synthesised: {len(rec.chunks)}/{len(expected)}, all played to the end: {all_played}")
    print(f"Chunk text == intro text, word for word: {words(' '.join(rec.chunks)) == words(text)}")
    heard = transcribe(rec)
    want, got = words(text), words(heard)
    import difflib

    diff = [d for d in difflib.ndiff(want, got) if d[0] in "+-"]
    print(f"Whisper base.en heard: {heard}")
    print(f"Word differences vs intro.txt ({len(want)} words): {diff or 'none'}")


def check_markdown(out, rec) -> None:
    """Check markdown symbols are never read out."""
    print("\n== Markdown stripping ==")
    rec.reset()
    out.speak("## Ottawa\n**Ottawa** is the capital of *Canada*.\n- It has `Parliament Hill`.\n- [Read more](https://example.com).")
    said = " ".join(rec.chunks)
    print(f"Sent to the voice: {said!r}")
    leftover = bool(re.search(r"[*_`#\[\]]|https?://", said))
    print(f"Any markdown symbol left: {leftover}")
    print(f"Whisper heard: {transcribe(rec)}")


def check_stop(out, rec) -> None:
    """Check Stop cuts her mid-sentence."""
    print("\n== Stop mid-sentence (the calls Ctrl+Shift+X makes) ==")
    from jarvis.controller import Controller

    controller = Controller()
    controller.add_transition_listener(lambda entry: out.stop() if entry.event_type is None else None)
    controller.start()

    def on_stop_hotkey() -> None:  # jarvis/dashboard/app.py, word for word
        out.stop()
        controller.request_stop(source="hotkey")

    for delay in (1.2, 3.0):
        rec.reset()
        speaker = threading.Thread(target=out.speak, args=(load_intro_text(),))
        speaker.start()
        while not rec.parts_started:
            time.sleep(0.005)
        time.sleep(delay)  # counted from her first sound
        pressed = time.monotonic()
        on_stop_hotkey()
        speaker.join(5)
        returned = time.monotonic() - pressed
        idx = len(rec.parts_started) - 1
        started, length = rec.parts_started[idx]
        into = pressed - started
        cached = out._cache is not None and not rec.chunks
        print(f"Stop {delay:.1f} s after her first sound: part {idx + 1} ({'cached file' if cached else 'live'}) was "
              f"{into:.2f} s into {length:.2f} s of audio (cut mid-part: {0 < into < length}); sound device stopped "
              f"{(rec.player_returned_at - pressed) * 1000:.0f} ms after Stop; speak() returned after "
              f"{returned * 1000:.0f} ms; parts started after Stop: {len(rec.parts_started) - 1 - idx}")
        time.sleep(1)
    controller.shutdown()


def check_echo(out, rec) -> None:
    """Check the echo guard: her own 'Hey Jarvis' never wakes her."""
    print("\n== Echo guard: she says 'Hey Jarvis' out loud while the wake word listens ==")
    from jarvis.controller import Controller, State
    from jarvis.voice.system import start_voice

    lines = "Hey Jarvis. Hey Jarvis, wake up. Hey Jarvis, introduce yourself. Hey Jarvis!"
    for guard in (True, False):
        events: list[str] = []
        controller = Controller(start_state=State.SLEEPING)
        controller.start()
        system = start_voice(
            controller,
            handle_command=lambda text, source: events.append(f"command: {text}"),
            on_stop_hotkey=lambda: None,
            notify=lambda note: events.append(f"note: {note}"),
            enable_hotkeys=False,
            wake=lambda source: events.append(f"WAKE ({source})"),
            heard_while_asleep=lambda text: events.append(f"heard while asleep: {text}"),
            ignored=lambda text, why: events.append(f"ignored: {text} ({why})"),
            is_speaking=out.is_speaking if guard else (lambda: False),
        )
        time.sleep(2)  # microphone settles
        level = MicLevel()
        quiet = level.measure(1.5)
        rec.reset()
        samples: list[bool] = []
        speaker = threading.Thread(target=out.speak, args=(lines,))
        level.start()
        speaker.start()
        while speaker.is_alive():
            samples.append(out.is_speaking())
            time.sleep(0.01)
        loud = level.stop()
        time.sleep(4)  # the echo tail and any late recording
        system.close()
        controller.shutdown()
        audio_start, audio_end = rec.played[0][0], rec.played[-1][0] + rec.played[-1][1]
        wakes = [e for e in events if e.startswith("WAKE")]
        print(f"Echo guard {'ON ' if guard else 'OFF'}: wakes {len(wakes)}; events: {events or 'none'}")
        print(f"    microphone level: room {quiet:.4f} RMS, while she spoke {loud:.4f} RMS "
              f"({loud / max(quiet, 1e-6):.1f}x); is_speaking() true in {sum(samples)}/{len(samples)} checks "
              f"over {audio_end - audio_start:.1f} s of audio")


def check_ram(out, rec) -> None:
    """Memory with all three models loaded at once: Kokoro (already loaded
    by make_output), the wake word (with the microphone open) and Whisper
    base.en, which the app otherwise loads on the first spoken command."""
    print("\n== RAM: Kokoro + wake word + Whisper base.en ==")
    import numpy as np
    import psutil

    from jarvis.controller import Controller, State
    from jarvis.voice.system import start_voice

    me = psutil.Process()

    def mb() -> str:
        info = me.memory_info()
        return f"working set {info.rss / 2**20:.0f} MB, private {info.private / 2**20:.0f} MB"

    print(f"Kokoro loaded and warmed up: {mb()}")
    controller = Controller(start_state=State.SLEEPING)
    controller.start()
    system = start_voice(controller, handle_command=lambda t, s: None, on_stop_hotkey=lambda: None,
                         enable_hotkeys=False, is_speaking=out.is_speaking)
    time.sleep(2)
    print(f"+ wake word running on the microphone: {mb()}")
    system.stt.transcribe_detailed(np.zeros(16000, dtype=np.int16))
    print(f"+ Whisper base.en loaded (one transcription): {mb()}")
    out.speak("Good evening, Priyansh. All three models are loaded.")
    print(f"+ after Kokoro has spoken a line: {mb()}")
    system.close()
    controller.shutdown()


class MicLevel:
    """RMS level on the default microphone (a second, independent stream)."""

    def __init__(self) -> None:
        import sounddevice as sd

        self._sd, self._sum, self._n, self._stream = sd, 0.0, 0, None

    def _callback(self, data, frames, t, status) -> None:
        self._sum += float((data ** 2).sum())
        self._n += data.size

    def start(self) -> None:
        """Open the microphone and start measuring its level."""
        self._sum, self._n = 0.0, 0
        self._stream = self._sd.InputStream(channels=1, samplerate=16000, dtype="float32", callback=self._callback)
        self._stream.start()

    def stop(self) -> float:
        """Close the microphone. Returns the RMS level measured."""
        self._stream.stop(), self._stream.close()
        return (self._sum / max(self._n, 1)) ** 0.5

    def measure(self, seconds: float) -> float:
        """The microphone's RMS level over `seconds`."""
        self.start()
        time.sleep(seconds)
        return self.stop()


def main() -> int:
    """Run the chosen voice checks (latency, words, markdown, stop, echo) and print the results."""
    wanted = [a for a in sys.argv[1:] if not a.startswith("--")] or ["latency", "words", "markdown", "stop", "echo"]
    out, rec = make_output(cache="--cache" in sys.argv)
    for name in wanted:
        globals()[f"check_{name}"](out, rec)
    out.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
