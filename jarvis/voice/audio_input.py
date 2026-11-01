"""F2 voice input: microphone capture (sounddevice, MIT; the Windows wheel
bundles PortAudio).

JARVIS works on 16 kHz, mono, int16 audio in 1280-sample blocks (80 ms --
openWakeWord's frame size, also fine for Whisper).

Which device: stored by NAME ([audio] input_device_name; empty = Windows'
default input) and looked up each time the stream opens, never stored as an
index (indices change when devices are plugged in). A named device that is
missing or fails to open falls back to the default, and the status says so.

Sample rate (developer request): Windows lists the same
microphone several times, once per audio system ("host API"), each with its
own native rate -- e.g. the Contoso earphones are 16000 Hz under WASAPI but
reported as 44100 Hz under MME. JARVIS prefers a copy whose native rate is
16, 32 or 48 kHz: it opens that copy at its own rate and decimates to 16 kHz
itself with a proper anti-aliasing low-pass filter (numpy), so nothing is
resampled twice. Only if no such copy opens does it ask Windows for 16 kHz
directly (Windows then resamples). MicStatus.capture says which path is live.
WDM-KS copies are skipped: they need exclusive, device-specific formats.

This module only captures. Where each frame goes is decided by
jarvis/voice/listener.py. Nothing here writes audio anywhere.

The audio callback runs on PortAudio's own thread, so it does the minimum:
convert the block and hand it on. It never blocks and never raises.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

from jarvis.platform import setup_message
from jarvis.settings import SETTINGS

logger = logging.getLogger(__name__)

_AUDIO = SETTINGS["audio"]
_MESSAGES = SETTINGS["setup_messages"]

TARGET_RATE = int(_AUDIO["sample_rate"])
_SKIPPED_HOST_APIS = ("Windows WDM-KS",)
# Windows' generic aliases for "the default device": never listed by name.
_ALIAS_NAMES = ("microsoft sound mapper - input", "primary sound capture driver")

FrameSink = Callable[[Any], None]  # receives one 1-D int16 numpy array per block


@dataclass(frozen=True)
class MicStatus:
    """What the dashboard's Microphone row shows."""

    live: bool
    device_name: Optional[str] = None
    # A missing-setup sentence when something needs the developer's
    # attention (no mic, blocked, fallback used). None when all is well.
    note: Optional[str] = None
    # How it is being captured, e.g. "Windows WASAPI, 48000 Hz, decimated
    # to 16000 Hz in JARVIS" -- for the tooltip and the report.
    capture: Optional[str] = None


class MicUnavailable(Exception):
    """Raised when no microphone can be used; `message` says what to do."""
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _load_sounddevice() -> Any:
    try:
        import sounddevice
    except (ImportError, OSError) as exc:  # OSError: PortAudio library missing
        raise MicUnavailable(_MESSAGES["voice_library_missing"]) from exc
    return sounddevice


def _host_api_name(sd: Any, index: int) -> str:
    try:
        return str(sd.query_hostapis(index)["name"])
    except Exception:
        return ""


def _input_devices(sd: Any) -> list[tuple[int, dict]]:
    devices = []
    for i, d in enumerate(sd.query_devices()):
        if int(d.get("max_input_channels", 0)) <= 0:
            continue
        if _host_api_name(sd, d.get("hostapi", -1)) in _SKIPPED_HOST_APIS:
            continue
        devices.append((i, d))
    return devices


def _default_input_index(sd: Any) -> Optional[int]:
    try:
        index = sd.default.device[0]
    except Exception:
        return None
    return index if isinstance(index, int) and index >= 0 else None


def _same_device(a: str, b: str) -> bool:
    """MME cuts names at 31 characters, so "Microphone Array (Realtek(R) Au"
    and "Microphone Array (Realtek(R) Audio)" are the same device."""
    a, b = a.strip().lower(), b.strip().lower()
    return bool(a) and bool(b) and (a == b or a.startswith(b) or b.startswith(a))


def find_device_by_name(devices: list[tuple[int, dict]], wanted: str, preferred_hostapi: Optional[int]) -> Optional[int]:
    """Exact name first, then a prefix match either way (MME truncation),
    preferring the default host API's copy. Case-insensitive."""
    wanted = wanted.strip().lower()
    if not wanted:
        return None

    def ordered(candidates):
        return sorted(candidates, key=lambda item: 0 if item[1].get("hostapi") == preferred_hostapi else 1)

    exact = [(i, d) for i, d in devices if str(d.get("name", "")).strip().lower() == wanted]
    if exact:
        return ordered(exact)[0][0]
    prefix = [(i, d) for i, d in devices if _same_device(str(d.get("name", "")), wanted)]
    if prefix:
        return ordered(prefix)[0][0]
    return None


def list_input_device_names(sd_module: Any = None) -> List[str]:
    """Every microphone, once each, by its fullest name -- for the
    dashboard's picker. Empty if sounddevice can't load."""
    try:
        sd = sd_module or _load_sounddevice()
        devices = _input_devices(sd)
    except Exception:
        return []
    names: List[str] = []
    for _i, d in devices:
        name = str(d.get("name", "")).strip()
        if not name or name.lower() in _ALIAS_NAMES:
            continue
        same = [n for n in names if _same_device(n, name)]
        if same:
            if len(name) > len(same[0]):  # keep the untruncated spelling
                names[names.index(same[0])] = name
            continue
        names.append(name)
    return sorted(names, key=str.lower)


@dataclass(frozen=True)
class _OpenPlan:
    """How to open one input device: its index and the sample rate to open it at."""
    index: int
    rate: int  # the rate the stream is opened at
    factor: int  # decimation factor to reach TARGET_RATE (1 = none)
    description: str


def plan_capture(sd: Any, devices: list[tuple[int, dict]], name: str) -> List[_OpenPlan]:
    """The ways to open the device called `name`, best first: a copy whose
    native rate is a whole multiple of 16 kHz (opened natively, decimated in
    JARVIS), then the default host API's copy at 16 kHz (Windows
    resamples)."""
    copies = [(i, d) for i, d in devices if _same_device(str(d.get("name", "")), name)]
    native: List[_OpenPlan] = []
    resampled: List[_OpenPlan] = []
    default_index = _default_input_index(sd)
    default_hostapi = sd.query_devices(default_index).get("hostapi") if default_index is not None else None
    for i, d in copies:
        api = _host_api_name(sd, d.get("hostapi", -1))
        rate = int(round(float(d.get("default_samplerate", 0) or 0)))
        if rate and rate % TARGET_RATE == 0 and rate // TARGET_RATE in (1, 2, 3):
            native.append(_OpenPlan(i, rate, rate // TARGET_RATE, f"{api}, {rate} Hz native" + ("" if rate == TARGET_RATE else f", decimated to {TARGET_RATE} Hz in JARVIS")))
        if rate != TARGET_RATE:  # a native 16 kHz copy is already planned above
            resampled.append(_OpenPlan(i, TARGET_RATE, 1, f"{api}, opened at {TARGET_RATE} Hz (Windows resamples from {rate} Hz)"))
    resampled.sort(key=lambda p: 0 if sd.query_devices(p.index).get("hostapi") == default_hostapi else 1)
    return native + resampled


class Decimator:
    """Stateful anti-aliased decimation by a whole factor (2 or 3): a
    low-pass FIR filter below the new Nyquist frequency, carried across
    blocks so block edges add no clicks, then every n-th sample."""

    def __init__(self, factor: int, rate: int) -> None:
        import numpy as np

        self.factor = factor
        # Pass up to 7 kHz (speech), stop before 8 kHz (the 16 kHz Nyquist).
        self._taps = lowpass_taps(int(_AUDIO["decimation_filter_taps"]), 7000.0, rate)
        self._history = np.zeros(len(self._taps) - 1, dtype=np.float64)  # the filter's memory across blocks
        self._phase = 0  # where the next kept sample falls, so blocks of any length join up
        self._np = np

    def __call__(self, block: Any) -> Any:
        np = self._np
        samples = block.astype(np.float64)
        joined = np.concatenate([self._history, samples])
        filtered = np.convolve(joined, self._taps, mode="valid")  # == lfilter with carried state
        self._history = joined[len(joined) - (len(self._taps) - 1):]
        out = filtered[self._phase:: self.factor]
        self._phase = (self._phase - len(filtered)) % self.factor
        return np.clip(np.round(out), -32768, 32767).astype(np.int16)


def lowpass_taps(count: int, cutoff_hz: float, rate: float) -> Any:
    """A windowed-sinc low-pass FIR filter: Hamming window, unity gain at
    0 Hz -- the same taps as scipy.signal.firwin(count, cutoff_hz, fs=rate),
    in plain numpy (Importing scipy.signal for this was 816
    files and 77 MB when the built-in 48 kHz microphone opened)."""
    import numpy as np

    c = 2.0 * cutoff_hz / rate  # the cutoff as a share of the Nyquist frequency
    m = np.arange(count) - (count - 1) / 2.0
    taps = c * np.sinc(c * m) * np.hamming(count)
    return taps / taps.sum()


class MicInput:
    """F2: the microphone stream, opened at a rate that gives clean 16 kHz audio, delivering frames to the
    listener."""
    def __init__(
        self,
        on_frames: FrameSink,
        device_name: Optional[str] = None,
        sd_module: Any = None,
        on_status: Optional[Callable[[MicStatus], None]] = None,
    ) -> None:
        self._on_frames = on_frames
        self._device_name = str(_AUDIO["input_device_name"] if device_name is None else device_name)
        self._sd = sd_module
        self._on_status = on_status
        self._block_size = int(_AUDIO["block_size"])
        self._silent_check_frames = max(1, int(float(_AUDIO["silent_check_seconds"]) * TARGET_RATE / self._block_size))
        self._stream: Any = None
        self._abandoned = False  # an open that timed out may still finish later
        self._decimator: Optional[Decimator] = None
        self._lock = threading.Lock()
        self._frames_seen = 0
        self._heard_nonzero = False
        self._silence_reported = False
        self.status = MicStatus(live=False, note=setup_message("mic_none"))

    # -- opening -----------------------------------------------------------

    def start(self) -> MicStatus:
        """Open the stream. Never raises: on any failure the status carries
        the missing-setup sentence and the rest of JARVIS keeps working."""
        try:
            sd = self._sd or _load_sounddevice()
            self._sd = sd
            devices = _input_devices(sd)
            if not devices:
                raise MicUnavailable(setup_message("mic_none"))
            default_index = _default_input_index(sd)
            default_name = self._name_of(default_index) if default_index is not None else "the default microphone"

            note = None
            if self._device_name.strip():
                default_hostapi = sd.query_devices(default_index).get("hostapi") if default_index is not None else None
                if find_device_by_name(devices, self._device_name, default_hostapi) is not None:
                    opened = self._open_first(sd, devices, self._device_name)
                    if opened is not None:
                        self.status = MicStatus(live=True, device_name=opened[0], capture=opened[1])
                        self._publish()
                        return self.status
                note = _MESSAGES["mic_saved_missing"].format(name=self._device_name, default=default_name)
            if default_index is None:
                raise MicUnavailable(setup_message("mic_none"))
            opened = self._open_first(sd, devices, default_name)
            if opened is None:
                raise MicUnavailable(setup_message("mic_blocked"))
            self.status = MicStatus(live=True, device_name=opened[0], note=note, capture=opened[1])
        except MicUnavailable as exc:
            self.status = MicStatus(live=False, note=exc.message)
        except Exception:
            logger.exception("Microphone setup failed")
            self.status = MicStatus(live=False, note=setup_message("mic_none"))
        self._publish()
        return self.status

    def _open_first(self, sd: Any, devices, name: str) -> Optional[tuple[str, str]]:
        """The first way of opening `name` that works. At most
        [audio] open_max_attempts ways, each given open_timeout_seconds,
        so a stuck driver can't hold a switch for minutes."""
        timeout = float(_AUDIO["open_timeout_seconds"])
        for plan in plan_capture(sd, devices, name)[: int(_AUDIO["open_max_attempts"])]:
            outcome: dict = {}

            def attempt(plan=plan, outcome=outcome) -> None:
                try:
                    self._open(plan)
                    outcome["ok"] = True
                except Exception as exc:  # noqa: BLE001 -- reported, then the next way is tried
                    outcome["error"] = exc

            worker = threading.Thread(target=attempt, name="jarvis-mic-open", daemon=True)
            worker.start()
            worker.join(timeout)
            if outcome.get("ok"):
                logger.info("Microphone %r open: %s", self._name_of(plan.index), plan.description)
                return self._name_of(plan.index), plan.description
            if worker.is_alive():
                logger.warning("Microphone %r took over %.0fs to open via %s; trying the next way", name, timeout, plan.description)
                self._abandoned = True  # if it opens late, _open closes it again
            else:
                logger.info("Microphone %r could not open via %s: %s", name, plan.description, outcome.get("error"))
        return None

    def _name_of(self, index: int) -> str:
        try:
            return str(self._sd.query_devices(index).get("name", "the default microphone"))
        except Exception:
            return "the default microphone"

    def _open(self, plan: _OpenPlan) -> None:
        decimator = Decimator(plan.factor, plan.rate) if plan.factor > 1 else None
        stream = self._sd.InputStream(
            samplerate=plan.rate,
            blocksize=self._block_size * plan.factor,
            channels=1,
            dtype="int16",
            device=plan.index,
            callback=self._callback,
        )
        with self._lock:
            if getattr(self, "_abandoned", False) and self._stream is not None:
                stream.close()  # a late open after a newer one already worked
                return
            self._decimator = decimator
        stream.start()
        with self._lock:
            late = self._stream is not None  # another way opened first
            if not late:
                self._stream = stream
        if late:
            stream.stop()
            stream.close()

    def stop(self) -> None:
        """Close the microphone stream."""
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                logger.exception("Closing the microphone stream failed")

    # -- audio thread ------------------------------------------------------

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ARG002 (PortAudio's signature)
        try:
            block = indata[:, 0].copy()
            decimator = self._decimator
            if decimator is not None:
                block = decimator(block)
            self._check_not_silent(block)
            self._on_frames(block)
        except Exception:
            # Never let an exception escape into PortAudio's thread.
            logger.exception("Audio frame handling failed")

    def _check_not_silent(self, block) -> None:
        """Windows' microphone privacy block delivers exact digital silence
        rather than an error. A real microphone always has some noise, so
        if the first few seconds are all exact zeros, say what to change."""
        if self._heard_nonzero or self._silence_reported:
            return
        if block.any():
            self._heard_nonzero = True
            return
        self._frames_seen += 1
        if self._frames_seen >= self._silent_check_frames:
            self._silence_reported = True
            self.status = MicStatus(live=False, device_name=self.status.device_name, note=setup_message("mic_blocked"))
            self._publish()

    def _publish(self) -> None:
        if self._on_status is not None:
            try:
                self._on_status(self.status)
            except Exception:
                logger.exception("Microphone status listener failed")
