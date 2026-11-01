"""The shared "chunked summary" (spec, Groq budget; F18 and F19).

1. Split on paragraph, then sentence boundaries into chunks of at most
   [groq] max_chars_per_request.
2. Summarise each chunk, waiting [groq] chunk_spacing_seconds between
   requests; Stop is checked before every request and during each wait.
3. More than one chunk: one last request combines the chunk summaries.
4. The caller decides what goes in: never more than [groq]
   max_chunks_per_task chunks, and it says plainly how much was covered.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from jarvis.online import OnlineErrorKind
from jarvis.settings import SETTINGS

_GROQ = SETTINGS["groq"]
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def split_text(text: str, max_chars: Optional[int] = None) -> List[str]:
    """Chunks of at most `max_chars`, cut after a paragraph break, else a
    sentence end, else a space, else hard. The chunks are exact slices:
    "".join(chunks) == text, so what is sent is byte for byte what F19's R4
    preview showed."""
    limit = int(_GROQ["max_chars_per_request"]) if max_chars is None else max_chars
    chunks: List[str] = []
    start = 0
    while len(text) - start > limit:
        window = text[start:start + limit]
        half = limit // 2  # a break this early would waste one of the few requests
        cut = window.rfind("\n\n") + 2
        if cut < half:
            ends = [m.end() for m in _SENTENCE.finditer(window)]
            cut = ends[-1] if ends else 0
        if cut < half:
            cut = window.rfind(" ") + 1
        if cut < half:
            cut = limit
        chunks.append(text[start:start + cut])
        start += cut
    if start < len(text):
        chunks.append(text[start:])
    return chunks


@dataclass
class ChunkedResult:
    """The result of a summary sent in chunks: the text, an error if one stopped it, how many requests ran,
    and whether Stop ended it."""
    text: str  # the summary, or the failure sentence
    error: Optional[OnlineErrorKind] = None
    requests: int = 0
    stopped: bool = False


def summarise_chunks(
    chunks: List[str],
    summarise: Callable[[str, str], object],
    part_instructions: str,
    combine_instructions: str,
    should_stop: Callable[[], bool],
    report: Callable[[str], None] = lambda _s: None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> ChunkedResult:
    """`summarise(text, instructions)` makes one online request and returns
    an AnswerResult (text, error). Never sends more than it is given."""
    spacing = float(_GROQ["chunk_spacing_seconds"])
    summaries: List[str] = []
    requests = 0

    def wait() -> bool:
        """False if Stop came during the wait."""
        deadline = clock() + spacing
        while clock() < deadline:
            if should_stop():
                return False
            sleep(min(0.2, max(0.0, deadline - clock())))
        return not should_stop()

    for n, chunk in enumerate(chunks, 1):
        if should_stop() or (n > 1 and not wait()):
            return ChunkedResult("", stopped=True, requests=requests)
        report(f"summarising online: part {n} of {len(chunks)}" if len(chunks) > 1 else "summarising online")
        result = summarise(chunk, part_instructions)
        requests += 1
        if result.error is not None:
            return ChunkedResult(result.text, error=result.error, requests=requests)
        summaries.append(result.text)
    if len(summaries) == 1:
        return ChunkedResult(summaries[0], requests=requests)
    if not wait():
        return ChunkedResult("", stopped=True, requests=requests)
    report("combining")
    result = summarise("\n\n".join(f"Part {i}: {s}" for i, s in enumerate(summaries, 1)), combine_instructions)
    requests += 1
    if result.error is not None:
        return ChunkedResult(result.text, error=result.error, requests=requests)
    return ChunkedResult(result.text, requests=requests)
