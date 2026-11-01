"""Assembles voice input for the app (conversation mode):
microphone, wake word, speech-to-text, the listener that routes audio by
state, and the global hotkeys (press-to-talk / wake, Stop).

Every piece is optional (R14). A missing library, model, microphone or
hotkey turns off only that piece, with a missing-setup sentence in
`notes` (what is missing and exactly what to do and where); typed
commands, the dashboard and everything else keep working.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from jarvis.controller import Controller, Event, EventType, State
from jarvis.settings import SETTINGS
from jarvis.voice.audio_input import MicInput, MicStatus
from jarvis.voice.hotkeys import GlobalHotkeys, HotkeyBinding, HotkeyStatus
from jarvis.voice.listener import VoiceListener
from jarvis.voice.stt import SpeechToText, stt_ready
from jarvis.voice.wake_word import WakeWordDetector, wake_word_ready

logger = logging.getLogger(__name__)

_MESSAGES = SETTINGS["setup_messages"]
_HOTKEYS = SETTINGS["hotkeys"]
_HOUSEKEEPING_SECONDS = 5.0


def _importable(module: str) -> bool:
    try:
        __import__(module)
        return True
    except Exception:  # ImportError, or OSError from a missing native library
        return False


def _installed(module: str) -> bool:
    """Installed, without importing it: importing
    faster_whisper here pulled in CTranslate2, PyAV and tokenizers before
    the microphone could open; speech-to-text loads on first use anyway."""
    import importlib.util

    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


@dataclass
class VoiceSystem:
    """The running voice input: listener, microphone, wake word, speech-to-text, hotkeys, and their setup
    notes."""
    listener: VoiceListener
    controller: Optional[Controller] = None
    mic: Optional[MicInput] = None
    detector: Optional[WakeWordDetector] = None
    stt: Optional[SpeechToText] = None
    hotkeys: Optional[GlobalHotkeys] = None
    hotkey_statuses: List[HotkeyStatus] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    auto_sleep_minutes: float = 0.0
    _closed: threading.Event = field(default_factory=threading.Event)
    _mic_lock: threading.Lock = field(default_factory=threading.Lock)
    _last_activity: float = field(default_factory=time.monotonic)

    @property
    def mic_status(self) -> MicStatus:
        """The microphone's status for the dashboard."""
        return self.mic.status if self.mic is not None else MicStatus(live=False, note=_MESSAGES["voice_library_missing"])

    def levels(self) -> List[float]:
        """The newest microphone levels for the waveform."""
        return self.listener.levels()

    def switch_microphone(self, device_name: str) -> MicStatus:
        """The dashboard picker: close the current stream and open the named
        device (empty = Windows' default). Returns the new status."""
        with self._mic_lock:
            if self.mic is not None:
                self.mic.stop()
            self.mic = MicInput(on_frames=self.listener.on_frames, device_name=device_name)
            return self.mic.start()

    def close(self) -> None:
        """Clean shutdown (window closed): stop the microphone, unregister the
        hotkeys, stop the threads and release the models."""
        if self._closed.is_set():
            return
        self._closed.set()
        if self.hotkeys is not None:
            self.hotkeys.stop()
        with self._mic_lock:
            if self.mic is not None:
                self.mic.stop()
        self.listener.detach()
        if self.detector is not None:
            self.detector.stop()
            self.detector.release()
        if self.stt is not None:
            self.stt.release()
        logger.info("Voice input shut down")

    def on_transition(self, entry: Any) -> None:
        """Note activity, so idle models can be unloaded later."""
        self._last_activity = time.monotonic()

    def _housekeeping(self) -> None:
        while not self._closed.wait(_HOUSEKEEPING_SECONDS):
            try:
                # R15: unload the speech-to-text model after it has been idle.
                self.listener.unload_idle_models()
                self._maybe_auto_sleep()
            except Exception:
                logger.exception("Voice housekeeping failed")

    def _maybe_auto_sleep(self) -> None:
        """[conversation] auto_sleep_minutes (0 = off, the developer's
        choice): after that long AWAKE with nothing happening, sleep."""
        if self.auto_sleep_minutes <= 0 or self.controller is None:
            return
        if self.controller.state != State.AWAKE:
            return
        if time.monotonic() - self._last_activity >= self.auto_sleep_minutes * 60:
            self.controller.submit(Event(EventType.GO_TO_SLEEP, {"source": "auto-sleep"}))


def start_voice(
    controller: Controller,
    handle_command: Callable[[str, str], None],
    on_stop_hotkey: Callable[[], None],
    notify: Optional[Callable[[str], None]] = None,
    enable_hotkeys: bool = True,
    wake: Optional[Callable[[str], None]] = None,
    heard_while_asleep: Optional[Callable[[str], None]] = None,
    ignored: Optional[Callable[[str, str], None]] = None,
    is_speaking: Optional[Callable[[], bool]] = None,
    not_confirm_or_cancel: Optional[Callable[[], None]] = None,
    pick_pending: Optional[Callable[[], bool]] = None,
) -> VoiceSystem:
    """Start whatever voice pieces this laptop can run. Never raises."""
    from jarvis import startup_timing

    notes: List[str] = []
    libraries_ok = _importable("numpy") and _importable("sounddevice")
    startup_timing.mark("voice: libraries checked")

    stt = None
    if not _installed("faster_whisper"):
        notes.append(_MESSAGES["voice_library_missing"])
    elif not stt_ready():
        notes.append(_MESSAGES["stt_model_missing"])
    else:
        stt = SpeechToText()

    listener = VoiceListener(
        controller,
        handle_command,
        stt=stt,
        notify=notify,
        stt_missing_message=_MESSAGES["stt_model_missing"] if stt is None else None,
        wake=wake,
        heard_while_asleep=heard_while_asleep,
        ignored=ignored,
        is_speaking=is_speaking,
        not_confirm_or_cancel=not_confirm_or_cancel,
        pick_pending=pick_pending,
    )

    detector = None
    if not _installed("openwakeword"):
        if _MESSAGES["voice_library_missing"] not in notes:
            notes.append(_MESSAGES["voice_library_missing"])
    elif not wake_word_ready():
        notes.append(_MESSAGES["wake_model_missing"])
    else:
        detector = WakeWordDetector(on_detected=listener.on_wake_word)
        if not detector.start():
            notes.append(_MESSAGES["wake_model_missing"])
            detector = None
    listener.set_detector(detector)  # after construction: the detector calls back into the listener
    listener.attach()
    startup_timing.mark("voice: wake-word model loaded")

    mic = None
    if libraries_ok:
        mic = MicInput(on_frames=listener.on_frames)
        status = mic.start()
        startup_timing.mark("voice: microphone open")
        if status.note:
            notes.append(status.note)
    elif _MESSAGES["voice_library_missing"] not in notes:
        notes.append(_MESSAGES["voice_library_missing"])

    system = VoiceSystem(
        listener=listener,
        controller=controller,
        mic=mic,
        detector=detector,
        stt=stt,
        notes=notes,
        auto_sleep_minutes=float(SETTINGS["conversation"]["auto_sleep_minutes"]),
    )
    controller.add_transition_listener(system.on_transition)

    if enable_hotkeys:
        bindings = [
            # Press-to-talk is also the wake hotkey while she sleeps.
            HotkeyBinding("push_to_talk", str(_HOTKEYS["push_to_talk"]), listener.push_to_talk),
            HotkeyBinding("stop", str(_HOTKEYS["stop"]), on_stop_hotkey),
        ]
        system.hotkeys = GlobalHotkeys(bindings)
        system.hotkey_statuses = system.hotkeys.start()
        notes.extend(s.message for s in system.hotkey_statuses if s.message)

    threading.Thread(target=system._housekeeping, name="jarvis-voice-housekeeping", daemon=True).start()
    return system
