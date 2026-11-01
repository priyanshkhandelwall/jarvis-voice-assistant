"""F8 local text recognition: Windows' own OCR engine (Windows.Media.Ocr)
through pywinrt (winrt-Windows.Media.Ocr 3.2.1, MIT). Free, offline, no
model download; it uses the OCR languages Windows already has (en-GB and
en-US on this laptop). Measured Two lines of synthetic
text read exactly in 24 ms.

The image never leaves memory: its pixels are copied into a WinRT buffer
and handed to the engine directly.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from typing import List, Optional, Tuple

from jarvis.settings import SETTINGS

_MESSAGES = SETTINGS["setup_messages"]


@dataclass(frozen=True)
class OcrWord:
    """One word Windows' OCR found, with its box in screen pixels."""
    text: str
    x: float  # image pixels
    y: float
    width: float
    height: float

    @property
    def centre(self) -> Tuple[float, float]:
        """The middle of the word's box."""
        return self.x + self.width / 2, self.y + self.height / 2


@dataclass(frozen=True)
class OcrLine:
    """One line of OCR text and its words."""
    text: str
    words: Tuple[OcrWord, ...]

    @property
    def top(self) -> float:
        """The line's top edge."""
        return min((w.y for w in self.words), default=0.0)

    @property
    def left(self) -> float:
        """The line's left edge."""
        return min((w.x for w in self.words), default=0.0)


class OcrUnavailable(Exception):
    """The OCR package or a Windows OCR language is missing; the message says what to do (R14)."""


def _onnxruntime_first() -> None:
    """Load order matters (found): Windows' OCR engine loads
    Windows' own onnxruntime.dll, and importing the pip onnxruntime (the
    wake word, Kokoro, F9's detector) AFTER that crashed the process with an
    access violation. Loaded first, both work. In the app it is already
    loaded at startup; this makes the order certain in every process. Must
    run before anything touches Windows.Media.Ocr."""
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        pass


def ocr_available() -> Optional[str]:
    """None when OCR can run, otherwise the missing-setup message. Windows'
    OCR engine doesn't exist on macOS."""
    if sys.platform == "darwin":
        return str(_MESSAGES["screen_text_mac"])
    _onnxruntime_first()
    try:
        from winrt.windows.media.ocr import OcrEngine
    except ImportError:
        return str(_MESSAGES["ocr_library_missing"])
    try:
        if not list(OcrEngine.available_recognizer_languages):
            return str(_MESSAGES["ocr_language_missing"])
    except OSError:
        return str(_MESSAGES["ocr_language_missing"])
    return None


def read_text(image) -> List[OcrLine]:
    """Every line of text in a PIL image, top to bottom. Raises OcrUnavailable."""
    problem = ocr_available()
    if problem is not None:
        raise OcrUnavailable(problem)
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import Buffer

    rgba = image.convert("RGBA")
    limit = int(OcrEngine.max_image_dimension)
    if max(rgba.size) > limit:  # Windows OCR's own limit (10000 px)
        rgba.thumbnail((limit, limit))
    data = rgba.tobytes("raw", "BGRA")
    width, height = rgba.size
    rgba.close()

    async def recognise():
        buffer = Buffer(len(data))
        buffer.length = len(data)
        memoryview(buffer)[:] = data
        bitmap = SoftwareBitmap.create_copy_from_buffer(buffer, BitmapPixelFormat.BGRA8, width, height)
        engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            raise OcrUnavailable(str(_MESSAGES["ocr_language_missing"]))
        return await engine.recognize_async(bitmap)

    result = asyncio.run(recognise())
    lines = []
    for line in result.lines:
        words = tuple(OcrWord(w.text, w.bounding_rect.x, w.bounding_rect.y, w.bounding_rect.width, w.bounding_rect.height)
                      for w in line.words)
        lines.append(OcrLine(line.text, words))
    return sorted(lines, key=lambda l: (round(l.top / 10), l.left))
