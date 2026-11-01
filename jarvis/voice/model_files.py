"""Where F2 Stage 2's model files live, and whether they are on disk.

All paths are inside the project folder ([models] in settings.toml), so
nothing is written to a user-wide cache. "Ready" means the files exist on
disk -- checked without loading any model (cheap, safe at startup).
"""

from __future__ import annotations

from pathlib import Path

from jarvis.settings import SETTINGS

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MODELS = SETTINGS["models"]

# Files faster-whisper needs to load a CTranslate2 model from a folder.
_WHISPER_REQUIRED = ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt")


def wake_word_dir() -> Path:
    """The folder holding openWakeWord's models."""
    return PROJECT_ROOT / str(_MODELS["wake_word_dir"])


def wake_word_model_path() -> Path:
    """The 'hey jarvis' model file."""
    return wake_word_dir() / str(_MODELS["wake_word_model"])


def melspectrogram_model_path() -> Path:
    """openWakeWord's melspectrogram feature model."""
    return wake_word_dir() / str(_MODELS["melspectrogram_model"])


def embedding_model_path() -> Path:
    """openWakeWord's embedding feature model."""
    return wake_word_dir() / str(_MODELS["embedding_model"])


def whisper_dir() -> Path:
    """The folder holding the Whisper base.en model."""
    return PROJECT_ROOT / str(_MODELS["whisper_dir"])


def wake_word_ready() -> bool:
    """The "hey jarvis" model and the two feature models it needs."""
    return all(p.is_file() for p in (wake_word_model_path(), melspectrogram_model_path(), embedding_model_path()))


def stt_ready() -> bool:
    """True if every Whisper model file is on disk."""
    return all((whisper_dir() / name).is_file() for name in _WHISPER_REQUIRED)
