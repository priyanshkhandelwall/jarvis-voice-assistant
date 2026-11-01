"""Pre-rendered audio for her fixed lines, so they start
at once instead of after Kokoro has made the first sentence.

Which lines: fixed_lines() -- the F1 intro, every wake-greeting pool line
and the greeting replies (name filled in), "Goodnight.", "I'm already
awake.", "Say confirm or cancel.", the camera's "Ask me 'what do you see'
first.", the rude replies, the mood check-in lines and every setup message
that has no blanks to fill in.

Where: [voice_cache] dir (jarvis_data/voice_cache/, never submitted; it
rebuilds itself). One 16-bit mono WAV per line, named by a hash of the
text, voice, model, speed and language: change any of them and the line
simply gets a new file, and files no current line uses are deleted after
each render.

How they are used (jarvis/speech/neural_tts.py): a line is split into
sentences, and any run of sentences that matches a cached line plays from
its file; the rest is made live as before. So the rude reply in front of an
answer, or a check-in after it, also plays from the cache, and the live
part is made while the cached part plays. A missing or unreadable file
just means live synthesis (R14). Stop and the echo guard work exactly as
for live speech: cached audio goes through the same player.

Rendering runs in the background after startup, one sentence at a time,
only while she isn't speaking.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import wave
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from jarvis.settings import SETTINGS
from jarvis.speech.plain_text import to_speakable
from jarvis.speech.tts import split_sentences

logger = logging.getLogger(__name__)

_CFG = SETTINGS["voice_cache"]
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
FORMAT_VERSION = 1


def sentences_of(text: str) -> Tuple[str, ...]:
    """How a line is matched: its speakable sentences."""
    return tuple(split_sentences(to_speakable(text or "").strip()))


def fixed_lines() -> List[str]:
    """Every fixed line she says, most often heard first (render order)."""
    from jarvis.actions.intro import load_intro_text

    name = str(SETTINGS["user"]["name"])
    greeting = SETTINGS["greeting"]
    lines: List[str] = []
    for pool in greeting["pools"].values():
        lines += [str(line).format(name=name) for line in pool]
    lines.append(str(greeting["fallback"]).format(name=name))
    lines += [str(greeting["reply"]).format(period=p) for p in ("morning", "afternoon", "evening")]
    conversation = SETTINGS["conversation"]
    lines += [str(conversation["sleep_reply"]), str(conversation["already_awake_reply"])]
    lines.append(str(conversation["confirm_reminder_reply"]))
    small = SETTINGS["small_talk"]
    lines += [str(x) for x in small["welcome_replies"]] + [str(x) for x in small["ack_replies"]]
    lines += [str(x).format(name=name) for x in small["compliment_replies"]]
    lines.append(str(SETTINGS["camera"]["replies"]["look_first"]))
    rudeness = SETTINGS["rudeness"]
    lines += [str(rudeness["reply_text"]), str(rudeness["reply_text_nothing_asked"])]
    mood = SETTINGS["mood"]
    lines += [str(line) for line in mood["check_in_pool"]] + [str(mood["fine_reply"])]
    lines.append(load_intro_text())
    lines += [str(m) for m in SETTINGS["setup_messages"].values() if isinstance(m, str) and "{" not in m]
    seen: Dict[Tuple[str, ...], str] = {}
    for line in lines:
        key = sentences_of(line)
        if key and key not in seen:
            seen[key] = line
    return list(seen.values())


class VoiceCache:
    """Fixed lines (intro, greetings, replies) rendered once to WAV files, so they play instantly."""
    def __init__(
        self,
        signature: Dict[str, Any],
        folder: Optional[Path] = None,
        lines: Optional[Callable[[], Iterable[str]]] = None,
    ) -> None:
        """`signature`: what else, besides the text, changes the sound
        (voice, model, speed, language)."""
        self.folder = folder or _PROJECT_ROOT / str(_CFG["dir"])
        self._signature = dict(signature)
        self._lines = lines or fixed_lines
        self._index: Dict[Tuple[str, ...], Path] = {}
        self._lock = threading.Lock()
        self.longest = 0  # sentences in the longest cached line

    def path_for(self, sentences: Sequence[str]) -> Path:
        """The cache file for these sentences with this voice (a hash, so a voice change makes new files)."""
        blob = json.dumps([FORMAT_VERSION, list(sentences), self._signature], sort_keys=True)
        return self.folder / f"{hashlib.sha256(blob.encode('utf-8')).hexdigest()[:24]}.wav"

    def index(self) -> List[Tuple[Tuple[str, ...], Path]]:
        """Every current fixed line and its file (which may not exist yet).
        Lines already on disk can be played from now on."""
        entries = [(s, self.path_for(s)) for s in (sentences_of(line) for line in self._lines()) if s]
        with self._lock:
            self._index = dict(entries)
            self.longest = max((len(s) for s, _ in entries), default=0)
        return entries

    def lookup(self, sentences: Sequence[str]) -> Optional[Path]:
        """The cached WAV for these sentences, or None."""
        with self._lock:
            path = self._index.get(tuple(sentences))
        return path if path is not None and path.exists() else None

    def split(self, text: str) -> List[Tuple[str, Optional[Path], str]]:
        """The line as segments, in order: ("cached", path, its text) for
        each run of sentences that is a cached line (the longest run wins),
        and ("live", None, text) for the rest. The text of a cached run is
        kept so an unreadable file can still be said live."""
        sentences = sentences_of(text)
        segments: List[Tuple[str, Optional[Path], str]] = []
        live: List[str] = []
        i = 0
        while i < len(sentences):
            for k in range(min(self.longest, len(sentences) - i), 0, -1):
                path = self.lookup(sentences[i : i + k])
                if path is not None:
                    if live:
                        segments.append(("live", None, " ".join(live)))
                        live = []
                    segments.append(("cached", path, " ".join(sentences[i : i + k])))
                    i += k
                    break
            else:
                live.append(sentences[i])
                i += 1
        if live:
            segments.append(("live", None, " ".join(live)))
        return segments

    def prune(self) -> int:
        """Delete files no current line uses (old text, voice or model)."""
        with self._lock:
            keep = {p.name for p in self._index.values()}
        removed = 0
        for path in list(self.folder.glob("*.wav")) + list(self.folder.glob("*.part")):
            if path.name not in keep:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed


def write_wav(path: Path, samples: Any, rate: int) -> None:
    """16-bit mono WAV, written to a .part file first so a half-written
    file is never played."""
    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * 32767).astype("<i2")
    partial = path.with_suffix(".part")
    with wave.open(str(partial), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(int(rate))
        out.writeframes(pcm.tobytes())
    partial.replace(path)


def read_wav(path: Path) -> Tuple[Any, int]:
    """Read a 16-bit WAV file. Returns (float samples in -1..1, sample rate)."""
    import numpy as np

    with wave.open(str(path), "rb") as src:
        rate = src.getframerate()
        pcm = np.frombuffer(src.readframes(src.getnframes()), dtype="<i2")
    return pcm.astype(np.float32) / 32767.0, rate
