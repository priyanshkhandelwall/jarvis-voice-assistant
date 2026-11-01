"""F2 her voice and F2/F14 language: speech output (Kokoro, falling back to
Windows' Zira), Groq answers (qa.py), mood, rudeness, claim checks and the
spoken-length rules."""

from jarvis.speech.output import NullSpeechOutput, SpeechOutput
from jarvis.speech.qa import AnswerResult, GroqAnswerer

__all__ = ["SpeechOutput", "NullSpeechOutput", "GroqAnswerer", "AnswerResult"]
