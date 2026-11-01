"""R15 startup: the window first, the heavy parts after.

Measured on the developer's laptop: the window appeared 1.1 s after launch
but stayed empty -- titled "Flet" -- until 5.3 s, because starting voice
(the wake-word model 2.4 s, the microphone 0.6 s, the voice libraries
0.5 s) ran before the dashboard was built. Now the dashboard is built as
soon as the light parts are ready, and voice starts on its own thread.
Live Activity shows each step as it finishes.

BackgroundVoice stands in for the VoiceSystem until it is ready, so the
dashboard can be built without it: the Microphone row says "starting…",
there is no waveform yet, and closing the window during startup still
closes the voice system once it exists.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, List, Optional

from jarvis.voice.audio_input import MicStatus

logger = logging.getLogger(__name__)

STARTING = "starting…"


class BackgroundVoice:
    """Stands in for the VoiceSystem until it has started, so the dashboard can be built first (R15)."""
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._system = None
        self._closed = False
        self.ready = threading.Event()

    def set(self, system) -> None:
        """The real VoiceSystem, once started. Closed at once if the window
        was closed while it was starting."""
        with self._lock:
            self._system = system
            closed = self._closed
        self.ready.set()
        if closed and system is not None:
            system.close()

    @property
    def system(self):
        """The real VoiceSystem, or None while it is still starting."""
        with self._lock:
            return self._system

    @property
    def notes(self) -> List[str]:
        """The voice setup notes (missing model, no microphone, ...), empty while starting."""
        system = self.system
        return list(system.notes) if system is not None else []

    @property
    def mic_status(self) -> MicStatus:
        """The microphone row's status ('starting...' until voice is ready)."""
        system = self.system
        if system is None:
            return MicStatus(live=False, device_name=None, note=None, capture=STARTING)
        return system.mic_status

    def levels(self) -> List[float]:
        """The newest microphone levels for the waveform (empty while starting)."""
        system = self.system
        return system.levels() if system is not None else []

    def switch_microphone(self, device_name: str) -> MicStatus:
        """Switch to the microphone called `device_name`; while starting, says to try again."""
        system = self.system
        if system is None:
            return MicStatus(live=False, device_name=None, note="The microphone is still starting. Try again in a few seconds.")
        return system.switch_microphone(device_name)

    def close(self) -> None:
        """Close the voice system, now or as soon as it has started."""
        with self._lock:
            self._closed = True
            system = self._system
        if system is not None:
            system.close()


def start_in_background(
    voice: BackgroundVoice,
    start: Callable[[], object],
    report: Callable[[str], None],
    file_index=None,
    store_apps=None,
    on_ready: Optional[Callable[[object], None]] = None,
) -> threading.Thread:
    """Start voice (and watch the file index and the Store-app list) on one
    background thread, reporting each step to Live Activity via `report`.
    Never raises: a failure is reported and logged, and typing still works."""

    def run() -> None:
        from jarvis import startup_timing

        report("starting voice: wake-word model and microphone…")
        system = None
        try:
            system = start()
        except Exception:
            logger.exception("Voice failed to start")
            report("voice could not start -- typed commands still work")
        voice.set(system)
        startup_timing.mark("voice ready (background)")
        if system is not None:
            status = system.mic_status
            heard_on = status.device_name if status.live else "no microphone"
            report(f"voice ready: listening on {heard_on}")
        if on_ready is not None:
            try:
                on_ready(system)
            except Exception:
                logger.exception("Voice ready callback failed")
        # Then, one after the other so voice never competes with them: the
        # file index scan and the Store-app list.
        if file_index is not None:
            report("building the file index…")
            file_index.start()
        if store_apps is not None:
            store_apps.prefetch()
            apps = store_apps.apps(wait_seconds=30.0)
            report(f"app list ready: {len(apps)} Store apps")
            startup_timing.mark("app list ready (background)")
        if file_index is not None and file_index.wait_ready(timeout=120.0):
            report("file index ready")
            startup_timing.mark("file index ready (background)")
        startup_timing.mark("background start finished")
        startup_timing.write()

    thread = threading.Thread(target=run, name="jarvis-background-start", daemon=True)
    thread.start()
    return thread
