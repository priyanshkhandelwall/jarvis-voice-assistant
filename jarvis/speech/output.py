"""F2 speech output: the interface every voice implements.

NeuralSpeechOutput (Kokoro, neural_tts.py) and Pyttsx3SpeechOutput (Zira,
tts.py) implement it in the app; tests use small fakes. Anything that
reaches SPEAKING calls `speak()` without knowing which voice it is, so the
whole pipeline is testable with speech mocked (Section 8).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable


# Classes & Objects: an abstract base class; Kokoro, Zira and test fakes are interchangeable subclasses.
class SpeechOutput(ABC):
    """The speech interface every voice implements (Kokoro, Zira, silent), so the rest of JARVIS never
    cares which one is used."""
    @abstractmethod
    def speak(self, text: str) -> None:
        """Say `text`, blocking until it has been said."""

    def speak_while(self, text: str, still_valid: Callable[[], bool]) -> bool:
        """("Say confirm or cancel." said AFTER the screen
        was sent; "Say confirm once more" after "Forgotten."): a line that is
        only true while something holds -- a confirm prompt while that same
        confirmation is still pending. `still_valid` is checked at the last
        moment before it would be heard; a queued line whose moment has
        passed is dropped. True if it was said. Outputs that queue override
        this to check when the line reaches the front."""
        if not still_valid():
            return False
        self.speak(text)
        return True

    def is_speaking(self) -> bool:
        """True while she is saying something (the voice listener ignores
        the microphone then -- she must never respond to her own voice)."""
        return False

    def stop(self) -> None:
        """R8: cut the current utterance and drop queued ones. Outputs that
        can't be interrupted (or say nothing) need not override this."""
        return None


class NullSpeechOutput(SpeechOutput):
    """A voice that says nothing (R14: no voice degrades to silence and
    text on the dashboard, never a crash)."""

    def speak(self, text: str) -> None:
        """Say nothing."""
        return None
