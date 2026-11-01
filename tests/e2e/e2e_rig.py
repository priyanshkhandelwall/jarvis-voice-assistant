"""End-to-end rig: the REAL JARVIS -- the dashboard's own
main(page), make_app_components(), the controller, orchestrator,
interpreter, every action, the speech text and the voice pipeline (the
real openWakeWord model, real Whisper base.en, the real VoiceListener and
the real hotkey thread) -- with only the side effects replaced:

- the window: a headless stand-in page (no Flet window is ever created);
- the microphone: a virtual one fed with Kokoro speech plus noise, in real
  time (80 ms frames), exactly as the sound device would call it;
- the speakers: Kokoro really synthesises every line (so first-audio time
  is real), but a simulated player only waits out the audio's length;
- the hotkeys: the real GlobalHotkeys thread over a fake user32 (nothing
  is registered with Windows);
- app / URL / file openers, system setters, the camera and the screen grab:
  recorders and synthetic inputs (the real detector and the real Windows
  OCR still run on them);
- the network: a guard that refuses every host lookup the scenario did not
  allow; Groq calls are counted against a persistent budget and recorded,
  so a later run replays the real response instead of spending another.

Nothing here opens an app, a URL or a file, plays a sound, uses the real
microphone or camera, changes a system setting or shows a window
(tests/conftest.py fails any test that tries), and nothing writes under
jarvis_data (the session guard).
"""

from __future__ import annotations

import json
import queue
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

import jarvis.dashboard.app  # noqa: F401 -- imported before any test, so conftest's data guard redirects its paths
from jarvis.settings import SETTINGS

ROOT = Path(__file__).resolve().parents[2]
RESULTS = Path(__file__).resolve().parent / "results"
FIXTURES = ROOT / "tests" / "fixtures"

SAMPLE_RATE = int(SETTINGS["audio"]["sample_rate"])
BLOCK = int(SETTINGS["audio"]["block_size"])
FRAME_SECONDS = BLOCK / SAMPLE_RATE

# Voices: her own voice and a clearly different one (male, US).
VOICES = ("af_bella", "am_michael")
# Speech is scaled to this RMS (int16), a normal voice close to a laptop mic.
SPEECH_RMS = 3000.0
# Background noise RMS (int16) per level, and the resulting SNR.
NOISE_LEVELS = {"light": 60.0, "medium": 350.0, "heavy": 900.0}


def snr_db(level: str) -> float:
    """The speech-to-noise ratio in dB for a background noise level."""
    return 20 * np.log10(SPEECH_RMS / NOISE_LEVELS[level])


def root_guard(config):
    """tests/conftest.py's module, from pytest's plugin manager (with two
    conftest.py files, sys.modules["conftest"] may be the other one)."""
    target = (ROOT / "tests" / "conftest.py").resolve()
    for plugin in config.pluginmanager.get_plugins():
        f = getattr(plugin, "__file__", None)
        if f and Path(f).resolve() == target:
            return plugin
    raise RuntimeError("tests/conftest.py (the data guard) is not loaded")


def refuse_real_data_paths(guard) -> None:
    """Before anything launches: every default data path must already point
    into the test's own folder (tests/conftest.py). Fails before a write."""
    import importlib

    for module_name, attribute, _relative in guard._DATA_DEFAULTS:
        target = getattr(importlib.import_module(module_name), attribute)()
        if guard._REAL_DATA in Path(target).resolve().parents:
            raise RuntimeError(f"{module_name}.{attribute}() still points at the real jarvis_data ({target}); refusing to launch")


# -- the network guard ------------------------------------------------------------------------------

NEWS_HOSTS = {re.sub(r"^https?://([^/]+).*$", r"\1", url) for url in SETTINGS["news"]["general_feed_urls"]} | {"www.bing.com"}
HOST_GROUPS = {
    "groq": {"api.groq.com"},
    "weather": {"geocoding-api.open-meteo.com", "api.open-meteo.com"},
    "news": NEWS_HOSTS,
    "youtube": {"www.youtube.com", "youtube.com", "m.youtube.com"},
}


class NetGuard:
    """Every DNS lookup goes through here (urllib, httpx and requests all
    resolve with socket.getaddrinfo). A host the scenario did not allow is
    refused as if the network were down, and recorded."""

    def __init__(self) -> None:
        self.allowed: set = set()
        self.offline = False
        self.lookups: List[Tuple[str, bool]] = []
        self._lock = threading.Lock()

    def allow(self, *groups: str) -> None:
        """Fake allow for the tests."""
        for group in groups:
            self.allowed |= HOST_GROUPS[group]

    def install(self, monkeypatch) -> None:
        """Fake install for the tests."""
        real = socket.getaddrinfo

        def guarded(host, *args, **kwargs):
            name = host.decode() if isinstance(host, bytes) else str(host)
            if name in ("localhost", "127.0.0.1", "::1", "None"):
                return real(host, *args, **kwargs)
            ok = not self.offline and any(name == h or name.endswith("." + h) for h in self.allowed)
            with self._lock:
                self.lookups.append((name, ok))
            if not ok:
                raise socket.gaierror(11001, f"e2e network guard: {name} is not allowed in this scenario")
            return real(host, *args, **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", guarded)

    def hosts(self, allowed: Optional[bool] = None) -> List[str]:
        """Fake hosts for the tests."""
        with self._lock:
            return sorted({h for h, ok in self.lookups if allowed is None or ok == allowed})


# -- Groq: counted, recorded, replayed ----------------------------------------------------------------

class GroqBudgetUsedUp(RuntimeError):
    """Raised when an e2e run would go over its Groq call budget."""
    pass


class GroqGate:
    """Stands in for groq.Groq. The first time a scenario makes its n-th
    Groq call, the call is REAL (counted against the persistent budget) and
    its response is recorded; every later run of that scenario replays the
    recorded response through the same parsing code. Requests are kept so a
    test can check exactly what was sent."""

    def __init__(self, folder: Path, max_real: int) -> None:
        self.folder = folder
        self.max_real = max_real
        self.budget_path = folder / "groq_budget.json"
        self.scope = "unscoped"
        self._counter: Dict[str, int] = {}
        self.requests: List[dict] = []  # this test only
        self.replayed = 0
        self.real = 0
        self.exhausted = False
        self._lock = threading.Lock()

    def begin(self, scope: str) -> None:
        """Fake begin for the tests."""
        self.scope = scope
        self._counter = {}
        self.requests = []
        self.images: List[bytes] = []
        self.errors: List[str] = []
        self.replayed = self.real = 0

    def _budget(self) -> dict:
        """Fake budget for the tests."""
        try:
            return json.loads(self.budget_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"real_calls": 0, "calls": []}

    def real_calls_so_far(self) -> int:
        """Fake real calls so far for the tests."""
        return int(self._budget().get("real_calls", 0))

    def install(self, monkeypatch) -> None:
        """Fake install for the tests."""
        import groq

        real_cls = groq.Groq
        gate = self

        class _Raw:
            def __init__(self, parsed, headers) -> None:
                self._parsed, self.headers = parsed, headers

            def parse(self):
                return self._parsed

        class _Completions:
            def __init__(self, client_kwargs) -> None:
                self._kwargs = client_kwargs
                self.with_raw_response = type("WithRaw", (), {"create": lambda _s, **req: gate._call(real_cls, client_kwargs, req, raw=True, wrap=_Raw)})()

            def create(self, **request):
                return gate._call(real_cls, self._kwargs, request, raw=False, wrap=_Raw)

        class FakeGroq:
            def __init__(self, **kwargs) -> None:
                self.chat = type("Chat", (), {})()
                self.chat.completions = _Completions(kwargs)

        monkeypatch.setattr(groq, "Groq", FakeGroq)

    @staticmethod
    def _visible_request(request: dict) -> dict:
        """Fake visible request for the tests."""
        messages = []
        for m in request.get("messages", []):
            content = m.get("content")
            if isinstance(content, list):
                content = [{"type": c.get("type"), "text": c.get("text")} if c.get("type") == "text" else {"type": c.get("type"), "bytes": len(str(c))}
                           for c in content]
            messages.append({"role": m.get("role"), "content": content})
        return {"model": request.get("model"), "messages": messages, "tools": request.get("tools")}

    def _call(self, real_cls, client_kwargs: dict, request: dict, raw: bool, wrap):
        """Fake call for the tests."""
        try:
            return self._call_inner(real_cls, client_kwargs, request, raw, wrap)
        except Exception as exc:  # JARVIS classifies it; keep the real cause for the report
            self.errors.append(f"{type(exc).__name__}: {exc}"[:400])
            raise

    def _call_inner(self, real_cls, client_kwargs: dict, request: dict, raw: bool, wrap):
        """Fake call inner for the tests."""
        from groq.types.chat import ChatCompletion

        with self._lock:
            n = self._counter.get(self.scope, 0) + 1
            self._counter[self.scope] = n
            key = f"{self.scope}#{n}"
            self.requests.append(self._visible_request(request))
            for m in request.get("messages", []):  # images exactly as sent (E3: NetBank must be black)
                for c in m.get("content") if isinstance(m.get("content"), list) else []:
                    if c.get("type") == "image_url":
                        import base64

                        self.images.append(base64.b64decode(c["image_url"]["url"].split(",", 1)[1]))
        path = self.folder / "groq_recordings" / f"{re.sub(r'[^A-Za-z0-9#_-]', '_', key)}.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            parsed = ChatCompletion.model_validate(data["response"])
            self.replayed += 1
            return wrap(parsed, data.get("headers") or {}) if raw else parsed
        import groq

        with self._lock:
            if int(self._budget().get("real_calls", 0)) >= self.max_real:
                self.exhausted = True
                raise GroqBudgetUsedUp(f"Groq budget of {self.max_real} real calls used up")
        client = real_cls(**client_kwargs)
        reached = True
        try:
            if raw:
                response = client.chat.completions.with_raw_response.create(**request)
                headers = {k: v for k, v in response.headers.items() if k.lower().startswith("x-ratelimit")}
                parsed = response.parse()
            else:
                parsed, headers = client.chat.completions.create(**request), {}
        except groq.APIConnectionError:
            reached = False  # refused by the network guard (or offline): never reached Groq, not counted
            raise
        finally:
            if reached:  # it reached Groq (an answer or an HTTP error): counted
                with self._lock:
                    budget = self._budget()
                    budget["real_calls"] = int(budget.get("real_calls", 0)) + 1
                    budget.setdefault("calls", []).append({"key": key, "at": datetime.now().isoformat(timespec="seconds"),
                                                           "model": request.get("model")})
                    self.folder.mkdir(parents=True, exist_ok=True)
                    self.budget_path.write_text(json.dumps(budget, indent=2), encoding="utf-8")
        self.real += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"key": key, "request": self._visible_request(request), "headers": headers,
                                    "response": parsed.model_dump(mode="json")}, indent=2), encoding="utf-8")
        return wrap(parsed, headers) if raw else parsed


# -- speech out: real Kokoro, simulated speakers ------------------------------------------------------

class SharedKokoro:
    """One kokoro-onnx engine for the whole session: her voice (af_bella,
    through NeuralSpeechOutput) and the developer's spoken commands (any
    voice). One synthesis at a time."""

    def __init__(self) -> None:
        from jarvis.speech.neural_tts import KokoroSynth

        self.config = KokoroSynth()
        self.config.load()
        self.lock = threading.Lock()
        self._said: Dict[Tuple[str, str], np.ndarray] = {}

    def synth(self, text: str, voice: str):
        """Fake synth for the tests."""
        with self.lock:
            return self.config._engine.create(text, voice=voice, speed=self.config.speed, lang=self.config.language)

    def command_audio(self, text: str, voice: str) -> np.ndarray:
        """The developer saying `text`, as 16 kHz int16 at SPEECH_RMS."""
        key = (text, voice)
        if key not in self._said:
            from scipy.signal import resample_poly

            samples, rate = self.synth(text, voice)
            audio = resample_poly(np.asarray(samples, dtype=np.float64), SAMPLE_RATE, rate)
            loud = np.abs(audio) > 0.01
            rms = float(np.sqrt(np.mean(audio[loud] ** 2))) if loud.any() else 1.0
            self._said[key] = np.clip(audio * (SPEECH_RMS / 32768.0 / rms) * 32768.0, -32768, 32767).astype(np.int16)
        return self._said[key]


class HerVoice:
    """The Synth NeuralSpeechOutput loads: the shared engine, voice af_bella."""

    def __init__(self, shared: SharedKokoro) -> None:
        self._shared = shared
        self.name = shared.config.name
        self.signature = shared.config.signature

    def load(self) -> None:
        """Fake load for the tests."""
        pass  # loaded once for the session

    def warm_up(self) -> None:
        """Fake warm up for the tests."""
        pass

    def synth(self, sentence: str):
        """Fake synth for the tests."""
        return self._shared.synth(sentence, self._shared.config.voice)


class SimulatedSpeakers:
    """The player: no sound. Waits out each part's length in [speech]
    neural_block_seconds blocks, so Stop cuts it exactly as it cuts the
    sound device, and the echo guard sees her speaking for as long as she
    would."""

    def __init__(self) -> None:
        self.block = float(SETTINGS["speech"]["neural_block_seconds"])
        self.parts: List[Tuple[float, float, bool]] = []  # (started, seconds, finished)
        self.cut_at: Optional[float] = None
        # How much faster than real time the audio "plays" (4: a 2 s line takes
        # 0.5 s). The Stop and skip timing scenarios set 1.0 (real time).
        self.speed = 4.0

    def __call__(self, samples, rate: int, should_stop) -> bool:
        seconds = len(samples) / float(rate) / self.speed
        started = time.monotonic()
        end = started + seconds
        while time.monotonic() < end:
            if should_stop():
                self.cut_at = time.monotonic()
                self.parts.append((started, seconds, False))
                return False
            time.sleep(min(self.block, max(0.0, end - time.monotonic())))
        self.parts.append((started, seconds, True))
        return True


@dataclass
class Line:
    """One line she was asked to say, and when it was asked and finished."""
    text: str
    asked_at: float
    done_at: float = 0.0
    first_audio: Optional[float] = None


def make_recording_speech(shared: SharedKokoro):
    """NeuralSpeechOutput as the app makes it (real Kokoro, the real voice
    cache read-only), with simulated speakers. Records every line."""
    from jarvis.speech.neural_tts import NeuralSpeechOutput
    from jarvis.speech.voice_cache import VoiceCache

    class RecordingSpeech(NeuralSpeechOutput):
        def __init__(self) -> None:
            self.speakers = SimulatedSpeakers()
            self.cache = VoiceCache(shared.config.signature) if bool(SETTINGS["voice_cache"]["enabled"]) else None
            super().__init__(HerVoice(shared), player=self.speakers, cache=self.cache)
            self.lines: List[Line] = []
            self.cache_render_requested = 0
            self.closed = False

        def speak(self, text: str) -> None:
            line = Line(text, time.monotonic())
            self.lines.append(line)
            self.last_first_audio_seconds = None
            super().speak(text)
            line.done_at = time.monotonic()
            line.first_audio = self.last_first_audio_seconds

        def render_cache(self):
            # The real renderer writes jarvis_data/voice_cache: never in a test.
            self.cache_render_requested += 1
            return None

        def close(self) -> None:
            self.closed = True
            super().close()

        @property
        def texts(self) -> List[str]:
            return [l.text for l in self.lines]

    return RecordingSpeech()


# -- microphone and hotkeys -----------------------------------------------------------------------------

class VirtualMic:
    """Replaces MicInput. While running, a pump thread calls on_frames with
    one 80 ms int16 block every 80 ms -- background noise, with spoken
    commands mixed in when queued -- just as the sound device's callback
    does."""

    instances: List["VirtualMic"] = []

    def __init__(self, on_frames, device_name: str = "") -> None:
        from jarvis.voice.audio_input import MicStatus

        self.on_frames = on_frames
        self.status = MicStatus(live=True, device_name="e2e virtual microphone", capture="virtual, 16000 Hz")
        self.noise_rms = NOISE_LEVELS["light"]
        self._speech: "queue.Queue[np.ndarray]" = queue.Queue()
        self._pending = np.zeros(0, dtype=np.int16)
        self._running = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._rng = np.random.default_rng(29)
        self._lp = 0.0
        self.stopped = False
        self.frames_sent = 0
        self.speech_end_at: Optional[float] = None  # monotonic: last speech sample handed over
        self._speech_left = 0
        VirtualMic.instances.append(self)

    def start(self):
        """Fake start for the tests."""
        self._running.set()
        self._thread = threading.Thread(target=self._pump, name="e2e-virtual-mic", daemon=True)
        self._thread.start()
        return self.status

    def stop(self) -> None:
        """Fake stop for the tests."""
        self.stopped = True
        self._running.clear()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def say(self, audio: np.ndarray, lead_seconds: float = 0.3) -> None:
        """Fake say for the tests."""
        lead = np.zeros(int(lead_seconds * SAMPLE_RATE), dtype=np.int16)
        self.speech_end_at = None
        self._speech.put(np.concatenate([lead, audio]))

    def busy(self) -> bool:
        """Fake busy for the tests."""
        return not self._speech.empty() or len(self._pending) > 0

    def _noise(self, n: int) -> np.ndarray:
        """Fake noise for the tests."""
        # Pink-ish: white noise through a one-pole low-pass, scaled to the level.
        white = self._rng.standard_normal(n)
        out = np.empty(n)
        lp = self._lp
        for i in range(n):
            lp = 0.9 * lp + 0.1 * white[i]
            out[i] = lp
        self._lp = lp
        rms = float(np.sqrt(np.mean(out ** 2))) or 1.0
        return out * (self.noise_rms / rms)

    def _pump(self) -> None:
        """Fake pump for the tests."""
        next_at = time.monotonic()
        while self._running.is_set():
            if len(self._pending) < BLOCK:
                try:
                    chunk = self._speech.get_nowait()
                    self._pending = np.concatenate([self._pending, chunk])
                    self._speech_left = len(self._pending)
                except queue.Empty:
                    pass
            if len(self._pending):
                take = self._pending[:BLOCK]
                self._pending = self._pending[BLOCK:]
                voice = np.zeros(BLOCK)
                voice[: len(take)] = take
                self._speech_left = len(self._pending)
                if self._speech_left == 0:
                    self.speech_end_at = time.monotonic()
            else:
                voice = np.zeros(BLOCK)
            frame = np.clip(voice + self._noise(BLOCK), -32768, 32767).astype(np.int16)
            try:
                self.on_frames(frame)
            except Exception:  # the real callback never raises; record and carry on
                import logging

                logging.getLogger(__name__).exception("on_frames raised")
            self.frames_sent += 1
            next_at += FRAME_SECONDS
            delay = next_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_at = time.monotonic()  # fell behind: carry on in real time


_WM_HOTKEY, _WM_QUIT = 0x0312, 0x0012


class FakeUser32:
    """The four user32 calls GlobalHotkeys makes, with nothing registered in
    Windows. press(setting_key) delivers WM_HOTKEY exactly as Windows would
    to the real hotkey thread."""

    def __init__(self) -> None:
        self.registered: Dict[int, Tuple[int, int]] = {}
        self.unregistered: List[int] = []
        self.quit_posted = False
        self._messages: "queue.Queue[Tuple[int, int]]" = queue.Queue()
        self._ids: Dict[str, int] = {}

    def RegisterHotKey(self, hwnd, hotkey_id, flags, vk) -> bool:  # noqa: N802 -- user32's names
        """Fake registerhotkey for the tests."""
        self.registered[hotkey_id] = (flags, vk)
        return True

    def UnregisterHotKey(self, hwnd, hotkey_id) -> bool:  # noqa: N802
        """Fake unregisterhotkey for the tests."""
        self.unregistered.append(hotkey_id)
        self.registered.pop(hotkey_id, None)
        return True

    def GetMessageW(self, msg_ref, hwnd, low, high) -> int:  # noqa: N802
        """Fake getmessagew for the tests."""
        message, wparam = self._messages.get()
        msg = msg_ref._obj
        msg.message, msg.wParam = message, wparam
        return 0 if message == _WM_QUIT else 1

    def PostThreadMessageW(self, thread_id, message, wparam, lparam) -> bool:  # noqa: N802
        """Fake postthreadmessagew for the tests."""
        if message == _WM_QUIT:
            self.quit_posted = True
        self._messages.put((message, wparam))
        return True

    def press(self, hotkey_id: int) -> None:
        """Fake press for the tests."""
        self._messages.put((_WM_HOTKEY, hotkey_id))


# -- the window: a headless page ------------------------------------------------------------------------

class HeadlessPage:
    """What build()/main() use of ft.Page, with no window: controls are
    kept, page.update() is counted, run_task runs coroutines on a private
    event loop thread, show_dialog records the dialog."""

    def __init__(self) -> None:
        import asyncio

        self.controls: List[Any] = []
        self.updates = 0
        self.dialogs: List[Any] = []
        self.title = ""
        self.bgcolor = None
        self.padding = None
        self.fonts = None
        self.on_keyboard_event = None
        self.closed = threading.Event()
        page = self

        class _Window:
            width = height = min_width = min_height = 0
            maximized = False
            icon = None
            prevent_close = False
            on_event = None
            destroyed = False

            def center(self) -> None:
                pass

            async def destroy(self) -> None:
                self.destroyed = True
                page.closed.set()

        self.window = _Window()
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._loop.run_forever, name="e2e-headless-page-loop", daemon=True)
        self._loop_thread.start()

    def add(self, *controls) -> None:
        """Fake add for the tests."""
        self.controls.extend(controls)

    def update(self, *controls) -> None:
        """Fake update for the tests."""
        if self.closed.is_set():
            raise RuntimeError("page closed")
        self.updates += 1

    def run_task(self, handler, *args, **kwargs):
        """Fake run task for the tests."""
        import asyncio

        if self.closed.is_set():
            raise RuntimeError("page closed")
        return asyncio.run_coroutine_threadsafe(handler(*args, **kwargs), self._loop)

    def run_thread(self, handler, *args, **kwargs) -> None:
        """Fake run thread for the tests."""
        threading.Thread(target=handler, args=args, kwargs=kwargs, daemon=True, name="e2e-page-thread").start()

    def show_dialog(self, dialog) -> None:
        """Fake show dialog for the tests."""
        self.dialogs.append(dialog)

    def pop_dialog(self) -> None:
        """Fake pop dialog for the tests."""
        pass

    def close_window(self, timeout: float = 15.0) -> bool:
        """The window's close button: the CLOSE window event, as Flet sends it."""
        import asyncio

        import flet as ft

        handler = self.window.on_event
        if handler is None:
            self.closed.set()
            return False
        event = type("WindowEvent", (), {"type": ft.WindowEventType.CLOSE})()
        future = asyncio.run_coroutine_threadsafe(handler(event), self._loop)
        try:
            future.result(timeout)
        finally:
            self.closed.set()
        return self.window.destroyed

    def stop_loop(self) -> None:
        """Fake stop loop for the tests."""
        self.closed.set()
        self._loop.call_soon_threadsafe(self._loop.stop)


def walk_controls(control) -> List[Any]:
    """Every control under `control` (Flet 1.0: content / controls)."""
    found, todo = [], [control]
    while todo:
        c = todo.pop()
        if c is None:
            continue
        found.append(c)
        for attr in ("content", "controls", "title", "leading", "trailing"):
            try:
                v = getattr(c, attr, None)
            except Exception:
                continue
            if isinstance(v, list):
                todo.extend(v)
            elif v is not None and hasattr(v, "__dict__") and not isinstance(v, (str, int, float)):
                todo.append(v)
    return found


# -- fake system backend, camera, screen ------------------------------------------------------------------

class FakeSystem:
    """F22's backend: remembers the settings, records every change. Nothing
    real is changed."""

    def __init__(self) -> None:
        self.volume, self.muted, self.brightness, self.wifi = 30, False, 40, True
        self.calls: List[Tuple[str, Any]] = []
        self.shutdowns: List[Tuple[int, bool]] = []
        self.aborted = 0

    def get_volume(self) -> int:
        """Fake get volume for the tests."""
        return self.volume

    def set_volume(self, percent: int) -> None:
        """Fake set volume for the tests."""
        self.calls.append(("set_volume", percent))
        self.volume = percent

    def get_mute(self) -> bool:
        """Fake get mute for the tests."""
        return self.muted

    def set_mute(self, muted: bool) -> None:
        """Fake set mute for the tests."""
        self.calls.append(("set_mute", muted))
        self.muted = muted

    def get_brightness(self) -> int:
        """Fake get brightness for the tests."""
        return self.brightness

    def set_brightness(self, percent: int) -> None:
        """Fake set brightness for the tests."""
        self.calls.append(("set_brightness", percent))
        self.brightness = percent

    def get_wifi(self) -> bool:
        """Fake get wi-fi for the tests."""
        return self.wifi

    def set_wifi(self, on: bool) -> None:
        """Fake set wi-fi for the tests."""
        self.calls.append(("set_wifi", on))
        self.wifi = on

    def lock(self) -> bool:
        """Fake lock for the tests."""
        self.calls.append(("lock", None))
        return True

    def schedule_shutdown(self, seconds: int, reboot: bool, message: str) -> None:
        """Fake schedule shutdown for the tests."""
        self.calls.append(("schedule_shutdown", (seconds, reboot)))
        self.shutdowns.append((seconds, reboot))

    def abort_shutdown(self) -> None:
        """Fake abort shutdown for the tests."""
        self.calls.append(("abort_shutdown", None))
        self.aborted += 1

    def window_titles(self) -> List[str]:
        """Fake window titles for the tests."""
        return ["Report - Word", "Inbox - Gmail - Google Chrome"]


class FakeCapture:
    """cv2.VideoCapture's read()/release(), serving one synthetic frame."""

    def __init__(self, frame, log: List[str]) -> None:
        self.frame = frame
        self.log = log
        self.released = False

    def read(self):
        """Fake read for the tests."""
        if self.released:
            return False, None
        return True, self.frame.copy()

    def release(self) -> None:
        """Fake release for the tests."""
        self.released = True
        self.log.append("release")


def phone_scene(width: int = 640, height: int = 480):
    """A rendered desk with a dark-screen phone on its LEFT third (BGR).
    No licensed COCO photo with a phone is in the project or the venv (the
    YOLOX sample photo, tests/fixtures/yolox_dog.jpg, has a dog, a bicycle
    and a truck), and the run may not download one; this rendering is
    detected by the real YOLOX-S as "cell phone" at about 0.93."""
    import cv2
    from PIL import Image, ImageDraw, ImageFilter

    rng = np.random.default_rng(7)
    x = np.arange(width)[None, :]
    y = np.arange(height)[:, None]
    grain = np.sin(x / 9.0 + np.sin(y / 40.0) * 3) * 0.5 + 0.5
    base = np.zeros((height, width, 3), np.float32)
    base[..., 0], base[..., 1], base[..., 2] = 120 + 40 * grain, 85 + 30 * grain, 55 + 20 * grain
    base += rng.normal(0, 6, base.shape)
    desk = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))
    w, h = 120, 240
    phone = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(phone)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=17, fill=(18, 18, 22, 255), outline=(160, 160, 170, 255), width=2)
    d.rounded_rectangle((6, 12, w - 7, h - 13), radius=8, fill=(8, 8, 12, 255))
    d.polygon([(6, 12), (w * 0.55, 12), (6, h * 0.45)], fill=(60, 60, 70, 255))
    d.ellipse((w // 2 - 3, 3, w // 2 + 3, 9), fill=(5, 5, 5, 255))
    phone = phone.rotate(8, expand=True, resample=Image.BICUBIC)
    shadow = phone.split()[3].point(lambda v: int(v * 0.5)).filter(ImageFilter.GaussianBlur(6))
    desk.paste(Image.new("RGB", phone.size, (20, 15, 10)), (68, 128), shadow)
    desk.paste(phone, (60, 120), phone)
    return cv2.cvtColor(np.asarray(desk), cv2.COLOR_RGB2BGR)


def dog_photo():
    """YOLOX's own sample photo (Apache-2.0): dog, bicycle, truck (BGR)."""
    import cv2

    return cv2.imread(str(FIXTURES / "yolox_dog.jpg"))


SCREEN_SIZE = (1920, 1080)
WORD_TITLE = "Quarterly Report - Word"
CHROME_TITLE = "Weather Forecast Sydney - Google Chrome"
BANK_TITLE = "CommBank NetBank - Google Chrome"
BANK_SECRET = "Account balance 98765 SECRETWORD"


def synthetic_desktop():
    """A fake desktop: the Program Manager behind everything, a Word window
    on the left, a Chrome window top right and a NetBank window bottom
    right, each with readable text drawn in it. Returns (image, windows)."""
    from PIL import Image, ImageDraw, ImageFont

    from jarvis.screen.capture import WindowInfo

    img = Image.new("RGB", SCREEN_SIZE, (0, 90, 160))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 30)
        title_font = ImageFont.truetype("arial.ttf", 22)
    except OSError:
        font = title_font = ImageFont.load_default()
    word = (40, 40, 1000, 1000)
    chrome = (1040, 40, 1880, 520)
    bank = (1040, 560, 1880, 1040)

    def window(rect, title, lines, bar=(43, 87, 154)):
        l, t, r, b = rect
        d.rectangle(rect, fill=(255, 255, 255), outline=(80, 80, 80), width=2)
        d.rectangle((l, t, r, t + 40), fill=bar)
        d.text((l + 12, t + 8), title, fill=(255, 255, 255), font=title_font)
        for i, text in enumerate(lines):
            d.text((l + 30, t + 80 + i * 60), text, fill=(0, 0, 0), font=font)

    d.text((60, 1040), "Recycle Bin", fill=(255, 255, 255), font=title_font)
    window(word, WORD_TITLE, ["Quarterly sales grew by twelve percent", "Revenue reached four million dollars", "Next steps for the marketing team"])
    window(chrome, CHROME_TITLE, ["Sydney weather today", "Sunny with a high of 24 degrees"], bar=(60, 64, 67))
    window(bank, BANK_TITLE, [BANK_SECRET, "Transfer money to savings"], bar=(255, 204, 0))
    windows = [
        WindowInfo(WORD_TITLE, "WINWORD.EXE", word),
        WindowInfo(CHROME_TITLE, "chrome.exe", chrome),
        WindowInfo(BANK_TITLE, "chrome.exe", bank),
        WindowInfo("Program Manager", "explorer.exe", (0, 0) + SCREEN_SIZE, system=True),
    ]
    return img, windows


def write_resume_pdf(path: Path) -> None:
    """A small synthetic resume, as a real PDF (Pillow's PDF writer has no
    text layer, so it is written by hand: one page, Helvetica)."""
    lines = ["Priya Example - Resume", "Software engineering student, Sydney.", "Skills: Python, machine learning, embedded systems.",
             "Experience: intern at a robotics lab, built a voice assistant.", "Education: Bachelor of Engineering, final year."]
    text_ops = ["BT", "/F1 14 Tf", "72 720 Td", "18 TL"]
    for line in lines:
        text_ops.append("(" + line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ") Tj T*")
    text_ops.append("ET")
    stream = "\n".join(text_ops).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))


# -- the spoken-text rules ----------------------------------------------------------------------------------

_PATH = re.compile(r"[A-Za-z]:\\|\\\\|/Users/|\\Users\\|[\\/][\w.-]+\.(?:pdf|docx?|txt|exe|py)\b", re.I)
_URL = re.compile(r"https?://|www\.|\b[\w-]+\.(?:com|org|net|io|au|uk)(?:/|\b)", re.I)
PROVIDERS = ["groq", "open-meteo", "openmeteo", "openai", "gpt-oss", "qwen", "exa", "bing", "kokoro", "whisper",
             "openwakeword", "yolox", "onnx", "llama"]


def rule_problems(text: str, max_sentences: Optional[int] = None) -> List[str]:
    """What breaks the spoken-text rules: a path, a URL, a provider name,
    too many sentences."""
    problems = []
    if _PATH.search(text):
        problems.append(f"path in {text!r}")
    if _URL.search(text):
        problems.append(f"URL in {text!r}")
    lowered = text.lower()
    for name in PROVIDERS:
        if re.search(rf"\b{re.escape(name)}\b", lowered):
            problems.append(f"provider name {name!r} in {text!r}")
    if max_sentences is not None:
        count = len([s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s])
        if count > max_sentences:
            problems.append(f"{count} sentences (max {max_sentences}) in {text!r}")
    return problems


@dataclass
class Timings:
    """Latencies measured during a run: wake, command to action, and time to first audio."""
    wake_latency: List[float] = field(default_factory=list)  # end of "jarvis" audio -> WAKE accepted
    command_to_action: List[float] = field(default_factory=list)  # end of speech / typed submit -> executor runs
    first_audio: List[float] = field(default_factory=list)  # speak() -> first synthesised audio ready
    transcribe: List[float] = field(default_factory=list)
    startup: Dict[str, float] = field(default_factory=dict)


# -- the app ------------------------------------------------------------------------------------------------

class NoStoreApps:
    """The Store-app list comes from PowerShell (a child process): empty here."""

    def prefetch(self) -> None:
        """Fake prefetch for the tests."""
        pass

    def apps(self, wait_seconds: float = 0.0):
        """Fake apps for the tests."""
        return []


@dataclass
class Turn:
    """What one spoken or typed command did."""

    said: str
    path: str
    started: float  # monotonic: typed submit, or the last speech sample handed to the listener
    heard: List[Tuple[str, str]]  # (text, source) handed to handle_command
    transcripts: List[str]  # everything Whisper produced in this turn, asleep ones included
    spoken: List[str]
    lines: List[Line]
    actions: List[Tuple[str, dict, float]]
    transitions: list
    launches: List[str]
    chrome: List[list]

    @property
    def text(self) -> str:
        """Fake text for the tests."""
        return " ".join(self.spoken)

    def action(self, name: Optional[str] = None):
        """Fake action for the tests."""
        for a in self.actions:
            if name is None or a[0] == name:
                return a
        return None

    def states(self) -> List[str]:
        """Fake states for the tests."""
        return [e.to_state.name for e in self.transitions if e.accepted]

    def events(self) -> List[str]:
        """Fake events for the tests."""
        return [e.event_type.name if e.event_type is not None else "STOP" for e in self.transitions]

    def trust_levels(self) -> List[str]:
        """Fake trust levels for the tests."""
        return [e.trust_level.name for e in self.transitions if e.trust_level is not None]


class Jarvis:
    """One launched JARVIS. `path` is "typed" or "voice"; `voice` and
    `noise` say how the virtual microphone speaks."""

    def __init__(self, monkeypatch, tmp_path: Path, shared: SharedKokoro, gate: GroqGate, models: dict, *,
                 path: str = "typed", voice: str = VOICES[0], noise: str = "light", first_wake: bool = False,
                 scope: str = "", guard=None) -> None:
        self.monkeypatch, self.tmp, self.shared, self.gate = monkeypatch, tmp_path, shared, gate
        self.path, self.voice, self.noise = path, voice, noise
        self.net = NetGuard()
        self.net.install(monkeypatch)
        gate.install(monkeypatch)
        gate.begin(scope)
        self.system = FakeSystem()
        self.camera_frame = phone_scene()
        self.camera_log: List[str] = []
        self.launches: List[str] = []
        self.chrome: List[list] = []
        self.actions: List[Tuple[str, dict, float]] = []
        self.heard: List[Tuple[float, str, str]] = []
        self.transcripts: List[Tuple[float, str, float]] = []  # (finished at, text, seconds)
        self.online_calls: List[str] = []
        self.inflight = 0
        self._inflight_lock = threading.Lock()
        self.user32 = FakeUser32()
        self.page = HeadlessPage()
        self.app = None
        self.voice_system = None
        self.speech = None
        self.timings = Timings()
        self.launched_at = 0.0
        self.closed = False
        self.files_dir = tmp_path / "files"
        self.files_dir.mkdir(exist_ok=True)
        refuse_real_data_paths(guard)
        self._patch(models, first_wake)

    # -- wiring -------------------------------------------------------------------

    def _patch(self, models: dict, first_wake: bool) -> None:
        """Fake patch for the tests."""
        from jarvis.actions import find_file, open_app
        from jarvis.actions.file_index import FileIndex
        from jarvis.actions.wake_greeting import WakeGreeter
        from jarvis.dashboard import app as app_mod
        from jarvis.online.service import OnlineService
        from jarvis.screen.capture import capture
        from jarvis.screen.commands import ScreenCommands
        from jarvis.security import paths
        from jarvis.system.commands import SystemCommands
        from jarvis.vision.camera import Camera
        from jarvis.voice import hotkeys as hotkeys_mod
        from jarvis.voice import stt as stt_mod
        from jarvis.voice import system as system_mod
        from jarvis.voice import wake_word as wake_mod

        mp = self.monkeypatch
        rig = self
        mp.setattr(paths, "APPROVED_FOLDERS", paths.APPROVED_FOLDERS + [self.tmp.resolve()])
        mp.setattr(app_mod, "_DIAG_LOG_PATH", self.tmp / "dashboard_diag.log")
        mp.setattr(app_mod, "apply_caption_color_async", lambda *a, **k: None)
        mp.setattr(app_mod, "make_speech_output", lambda: make_recording_speech(self.shared))
        mp.setattr(app_mod, "FileIndex", lambda: FileIndex(roots=[self.files_dir]))
        mp.setattr(app_mod, "SystemCommands", lambda: SystemCommands(backend=self.system))
        mp.setattr(app_mod, "Camera", lambda sensors: Camera(sensors, opener=self._open_camera, blocked=lambda: False))
        self.desktop = synthetic_desktop()
        self.screen_grabs = 0

        def grab():
            self.screen_grabs += 1
            return self.desktop[0].copy()

        mp.setattr(app_mod, "ScreenCommands", lambda answerer, sensors, online_status=None: ScreenCommands(
            answerer, sensors, capture_fn=lambda: capture(grab=grab, windows=lambda: list(self.desktop[1]), origin=lambda: (0, 0)),
            online_status=online_status))
        # Openers: recorded, never run. Verification sees the launch succeed.
        mp.setattr(open_app, "STORE_APPS", NoStoreApps())
        mp.setattr(open_app.os, "startfile", self.launches.append)
        mp.setattr(find_file.os, "startfile", self.launches.append)
        mp.setattr(open_app.subprocess, "Popen", lambda args, shell=False: self.chrome.append(list(args)))
        verify = lambda launch, timeout, poll: (launch(), False)  # noqa: E731
        mp.setattr(open_app, "launch_and_verify_with_retry", verify)
        mp.setattr(find_file, "launch_and_verify_with_retry", verify)
        # Voice: a virtual microphone, the real hotkey thread over a fake user32, shared models.
        mp.setattr(system_mod, "MicInput", VirtualMic)
        mp.setattr(system_mod, "GlobalHotkeys", lambda bindings: hotkeys_mod.GlobalHotkeys(bindings, api=self.user32))
        mp.setattr(wake_mod, "_default_model_factory", models["wake"])
        mp.setattr(stt_mod, "_default_model_factory", models["whisper"])
        real_transcribe = stt_mod.SpeechToText.transcribe_detailed

        def transcribe_detailed(stt_self, samples):
            with rig._inflight_lock:
                rig.inflight += 1
            started = time.monotonic()
            try:
                result = real_transcribe(stt_self, samples)
                rig.transcripts.append((time.monotonic(), result.text, time.monotonic() - started))
                return result
            finally:
                with rig._inflight_lock:
                    rig.inflight -= 1

        mp.setattr(stt_mod.SpeechToText, "transcribe_detailed", transcribe_detailed)
        real_call = OnlineService.call

        def call(service_self, fn, classify):
            rig.online_calls.append(service_self.name)
            return real_call(service_self, fn, classify)

        mp.setattr(OnlineService, "call", call)
        # Not the first wake of the day unless the scenario is about it.
        if not first_wake:
            from jarvis.actions import wake_greeting

            state = wake_greeting.default_state_path()
            state.parent.mkdir(parents=True, exist_ok=True)
            day = WakeGreeter._day_of(datetime.now())
            state.write_text(json.dumps({"day": day, "last_sleep": None, "recent": []}), encoding="utf-8")
        # Capture what main() builds.
        real_make = app_mod.make_app_components

        def make_app_components():
            app = real_make()
            rig.app = app
            rig.speech = app.speech
            for name, executor in list(app.orchestrator._executors.items()):
                app.orchestrator._executors[name] = rig._recording_executor(executor)
            return app

        mp.setattr(app_mod, "make_app_components", make_app_components)
        real_start_voice = app_mod.start_voice

        def start_voice(controller, handle_command, on_stop_hotkey, **kwargs):
            if kwargs.get("wake") is not None:
                kwargs["wake"] = rig._tracked_wake(kwargs["wake"])
            system = real_start_voice(controller, rig._tracked_command(handle_command), on_stop_hotkey, **kwargs)
            rig.voice_system = system
            return system

        mp.setattr(app_mod, "start_voice", start_voice)

    def _recording_executor(self, executor):
        """Fake recording executor for the tests."""
        def run(action_name, params):
            self.actions.append((action_name, dict(params), time.monotonic()))
            return executor(action_name, params)

        return run

    def _tracked_command(self, handle_command):
        """Fake tracked command for the tests."""
        def handle(text, source):
            self.heard.append((time.monotonic(), text, source))
            with self._inflight_lock:
                self.inflight += 1
            try:
                handle_command(text, source)
            finally:
                with self._inflight_lock:
                    self.inflight -= 1

        return handle

    def _tracked_wake(self, wake):
        """main()'s wake() starts orchestrator.wake on its own thread and
        returns; the wake counts as in flight until that thread ends."""

        def run(source):
            with self._inflight_lock:
                self.inflight += 1
            before = set(threading.enumerate())
            try:
                wake(source)
                for t in set(threading.enumerate()) - before:
                    if t.name == "jarvis-wake":
                        t.join(timeout=60)
            finally:
                with self._inflight_lock:
                    self.inflight -= 1

        return run

    def _open_camera(self, index: int):
        """Fake open camera for the tests."""
        if index != 0:
            return None
        self.camera_log.append(f"open {index}")
        return FakeCapture(self.camera_frame, self.camera_log)

    # -- launch / close -------------------------------------------------------------------

    def launch(self, wait_voice: float = 60.0) -> "Jarvis":
        """Fake launch for the tests."""
        from jarvis import startup_timing
        from jarvis.dashboard import app as app_mod

        before = len(startup_timing.marks())
        self.launched_at = time.time()
        started = time.monotonic()
        app_mod.main(self.page)
        self.timings.startup["main() returned: dashboard built"] = time.monotonic() - started
        deadline = time.monotonic() + wait_voice
        while self.voice_system is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert self.voice_system is not None, "voice never started"
        self.timings.startup["voice system started"] = time.monotonic() - started
        for at, step in startup_timing.marks()[before:]:
            self.timings.startup[step] = at - self.launched_at
        self.mic.noise_rms = NOISE_LEVELS[self.noise]
        self.app.speech.wait_until_loaded(30)
        # The noise floor needs a couple of seconds of background first, as a
        # real microphone has by the time he speaks.
        time.sleep(1.5)
        return self

    @property
    def mic(self) -> VirtualMic:
        """Fake mic for the tests."""
        return self.voice_system.mic

    @property
    def c(self):
        """Fake c for the tests."""
        return self.app.controller

    @property
    def orch(self):
        """Fake orch for the tests."""
        return self.app.orchestrator

    def close(self) -> dict:
        """The window's close button."""
        if self.closed:
            return {}
        self.closed = True
        report = {"destroyed": False}
        try:
            report["destroyed"] = self.page.close_window()
        finally:
            self.page.stop_loop()
        return report

    # -- driving ---------------------------------------------------------------------------

    def busy(self) -> bool:
        """Fake busy for the tests."""
        from jarvis.controller import State

        rest = (State.AWAKE, State.SLEEPING, State.CONFIRMING)
        voice = self.voice_system
        recording = voice is not None and voice.listener._recording is not None
        return (self.inflight > 0 or self.speech.is_speaking() or self.c.state not in rest
                or (voice is not None and self.mic.busy()) or recording)

    def settle(self, timeout: float = 30.0, quiet: float = 0.5) -> bool:
        """Fake settle for the tests."""
        deadline = time.monotonic() + timeout
        quiet_since = None
        while time.monotonic() < deadline:
            if self.busy():
                quiet_since = None
            elif quiet_since is None:
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since >= quiet:
                return True
            time.sleep(0.02)
        return False

    def wake_now(self) -> None:
        """Setup only (not a scenario step): wake her as the wake hotkey does."""
        self.orch.wake("e2e setup")
        self.settle()

    def _marks(self):
        """Fake marks for the tests."""
        return (len(self.heard), len(self.transcripts), len(self.speech.lines), len(self.actions), self.c.transition_count(),
                len(self.launches), len(self.chrome))

    def _turn(self, said: str, path: str, started: float, marks) -> Turn:
        """Fake turn for the tests."""
        h, t, s, a, tr, la, ch = marks
        with self.c._state_lock:
            transitions = list(self.c.transition_log[tr:])
        lines = list(self.speech.lines[s:])
        turn = Turn(said=said, path=path, started=started, heard=[(x[1], x[2]) for x in self.heard[h:]],
                    transcripts=[x[1] for x in self.transcripts[t:]], spoken=[l.text for l in lines], lines=lines,
                    actions=list(self.actions[a:]), transitions=transitions, launches=list(self.launches[la:]),
                    chrome=list(self.chrome[ch:]))
        for x in self.transcripts[t:]:
            self.timings.transcribe.append(x[2])
        if turn.actions:
            self.timings.command_to_action.append(turn.actions[0][2] - started)
        for line in lines:
            if line.first_audio is not None:
                self.timings.first_audio.append(line.first_audio)
                break
        return turn

    def type(self, text: str, timeout: float = 30.0) -> Turn:
        """The typed-command box: submit_command runs handle_typed_command on its own thread."""
        self.settle(timeout)
        marks = self._marks()
        started = time.monotonic()
        handle = self._tracked_command(lambda t, _s: self.orch.handle_typed_command(t))
        with self._inflight_lock:
            self.inflight += 1  # counted from the click, before the thread starts

        def run():
            try:
                handle(text, "typed")
            finally:
                with self._inflight_lock:
                    self.inflight -= 1

        threading.Thread(target=run, daemon=True).start()
        self.settle(timeout)
        return self._turn(text, "typed", started, marks)

    def speak(self, text: str, timeout: float = 30.0, voice: Optional[str] = None) -> Turn:
        """He says `text` into the (virtual) microphone."""
        self.settle(timeout)
        # Her echo tail: she ignores the microphone for a moment after speaking.
        time.sleep(float(SETTINGS["speech"]["echo_tail_seconds"]) + 0.2)
        marks = self._marks()
        audio = self.shared.command_audio(text, voice or self.voice)
        self.mic.say(audio)
        deadline = time.monotonic() + 20
        while self.mic.busy() and time.monotonic() < deadline:
            time.sleep(0.01)
        started = self.mic.speech_end_at or time.monotonic()
        time.sleep(0.1)
        self.settle(timeout)
        return self._turn(text, "voice", started, marks)

    def say(self, text: str, timeout: float = 30.0) -> Turn:
        """The scenario's path: typed or spoken."""
        return self.type(text, timeout) if self.path == "typed" else self.speak(text, timeout)

    def press_hotkey(self, setting_key: str) -> None:
        """Windows delivering WM_HOTKEY for that combination to the real hotkey thread."""
        for hotkey_id, binding in self.voice_system.hotkeys._registered.items():
            if binding.setting_key == setting_key:
                self.user32.press(hotkey_id)
                return
        raise AssertionError(f"hotkey {setting_key} not registered")

    # -- reading what happened -----------------------------------------------------------------

    def activity(self) -> List[str]:
        """Every Live Activity line, as the dashboard shows them."""
        from jarvis.dashboard.activity_feed import format_record

        lines = []
        for record in self.app.logbook.records():
            lines.extend(format_record(record).lines)
        live = self.app.logbook.live_snapshot()
        if live is not None:
            lines.extend(format_record(live).lines)
        return lines

    def saying_lines(self) -> List[str]:
        """Fake saying lines for the tests."""
        return [l[len("saying: "):] for l in self.activity() if l.startswith("saying: ")]

    def debug_log(self) -> str:
        """Fake debug log for the tests."""
        from jarvis.logbook import debug_log

        path = debug_log.debug_log_path()
        return path.read_text(encoding="utf-8") if path.exists() else ""
