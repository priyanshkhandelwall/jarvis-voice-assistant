"""F10 long-term memory: what the developer told her to remember, kept in
jarvis_data/memory.json ([memory] path) so it survives a restart.

- Only what he says to remember is stored ("remember that ..."). Nothing
  is noticed or stored automatically, and conversations are never stored.
- Never sent online (R2, R4): nothing in this package imports the online
  code; recall is a local keyword match.
- Writes are atomic (a temporary file, then os.replace), so a crash
  mid-write never leaves half a file. After every save the same content is
  also written to memory.json.bak, the last good copy; deleting therefore
  deletes from the backup too ("deleted stays deleted").
- A damaged file is kept as memory.corrupt-<time>.json and the last good
  copy is loaded (or an empty memory if there is none), and she says so.
- At most [memory] max_items items. When full she says so instead of
  silently dropping old ones.
- Every write path goes through the R9 check (the data folder is inside
  the project, an approved folder).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, List, Optional

from jarvis.security import PathIntent, check_path
from jarvis.settings import SETTINGS

logger = logging.getLogger(__name__)

_CFG = SETTINGS["memory"]
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
FORMAT_VERSION = 1

_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


def default_memory_path() -> Path:
    """Where memory.json lives in jarvis_data."""
    return _PROJECT_ROOT / str(_CFG["path"])


@dataclass(frozen=True)
class MemoryItem:
    """One thing he told her to remember: id, his words, when it was saved, and its kind."""
    id: str
    text: str  # as he said it ("my exam is on tuesday")
    saved: str  # ISO date and time, local
    kind: str = "told"  # F10: told (he said it); nothing is "noticed" in this build

    @property
    def saved_at(self) -> datetime:
        """When it was saved, as a datetime."""
        return datetime.fromisoformat(self.saved)


def keywords(text: str) -> set:
    """Words that say what an item is about: lower case, stop words
    removed, a possessive 's and a plural s dropped ("exams" ~ "exam")."""
    stop = {str(w).lower() for w in _CFG["stop_words"]}
    out = set()
    for word in _WORD.findall(text.lower().replace("’", "'")):
        word = word.removesuffix("'s")
        if word in stop:
            continue
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        out.add(word)
    return out


class MemoryFull(Exception):
    """Raised when the store already holds [memory] max_items items."""
    pass


class MemoryStore:
    """F10: memory.json, thread-safe, with atomic writes and a backup copy. Nothing in it is ever sent
    online."""
    def __init__(self, path: Optional[Path] = None, max_items: Optional[int] = None,
                 now: Callable[[], datetime] = datetime.now) -> None:
        self.path = path or default_memory_path()
        self.max_items = int(max_items if max_items is not None else _CFG["max_items"])
        self._now = now
        self._lock = threading.Lock()
        self._items: Optional[List[MemoryItem]] = None  # loaded on first use
        self.load_note: Optional[str] = None  # what happened if the file was damaged

    # -- reading ---------------------------------------------------------------

    def items(self) -> List[MemoryItem]:
        """All items, newest first."""
        with self._lock:
            return sorted(self._loaded(), key=lambda i: i.saved, reverse=True)

    def count(self) -> int:
        """How many items are stored."""
        with self._lock:
            return len(self._loaded())

    def search(self, topic: str) -> List[MemoryItem]:
        """Items sharing a keyword with `topic`, newest first."""
        wanted = keywords(topic)
        if not wanted:
            return []
        return [item for item in self.items() if wanted & keywords(item.text)]

    def get(self, item_id: str) -> Optional[MemoryItem]:
        """The item with this id, or None."""
        with self._lock:
            return next((i for i in self._loaded() if i.id == item_id), None)

    # -- writing ---------------------------------------------------------------

    def add(self, text: str) -> MemoryItem:
        """Store `text` as a new item and save the file. Raises MemoryFull at the limit."""
        with self._lock:
            items = self._loaded()
            if len(items) >= self.max_items:
                raise MemoryFull()
            item = MemoryItem(id=uuid.uuid4().hex, text=text, saved=self._now().isoformat(timespec="seconds"))
            self._save(items + [item])
            return item

    def update(self, item_id: str, text: str) -> Optional[MemoryItem]:
        """A correction: new text and today's date, same item."""
        with self._lock:
            items = self._loaded()
            for n, old in enumerate(items):
                if old.id == item_id:
                    new = MemoryItem(id=old.id, text=text, saved=self._now().isoformat(timespec="seconds"), kind=old.kind)
                    self._save(items[:n] + [new] + items[n + 1 :])
                    return new
            return None

    def delete(self, item_id: str) -> bool:
        """Delete the item with this id and save. False if there was no such item."""
        with self._lock:
            items = self._loaded()
            kept = [i for i in items if i.id != item_id]
            if len(kept) == len(items):
                return False
            self._save(kept)
            return True

    def clear(self) -> int:
        """Delete every item and save. Returns how many there were."""
        with self._lock:
            removed = len(self._loaded())
            self._save([])
            return removed

    def reload(self) -> None:
        """Read the file again (a restart, or R12's check after a write)."""
        with self._lock:
            self._items = None
            self._loaded()

    # -- the file -------------------------------------------------------------

    def _loaded(self) -> List[MemoryItem]:
        if self._items is None:
            self._items = self._read()
        return self._items

    def _backup_path(self) -> Path:
        return self.path.with_name(self.path.name + ".bak")

    def _read(self) -> List[MemoryItem]:
        if not self.path.exists():
            return []
        try:
            return _parse(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("Memory file %s is damaged; keeping it and recovering", self.path)
        damaged = self.path.with_name(f"{self.path.stem}.corrupt-{self._now():%Y%m%d-%H%M%S}.json")
        try:
            os.replace(self.path, damaged)
        except OSError:
            damaged = self.path
        recovered: List[MemoryItem] = []
        backup = self._backup_path()
        try:
            if not backup.exists():
                raise FileNotFoundError(backup)
            recovered = _parse(backup.read_text(encoding="utf-8"))
            self.load_note = str(_CFG["replies"]["corrupt_recovered"]).format(count=len(recovered), name=damaged.name)
        except (OSError, ValueError, KeyError, TypeError):
            self.load_note = str(_CFG["replies"]["corrupt_lost"]).format(name=damaged.name)
        self._write_file(self.path, recovered)
        return recovered

    def _save(self, items: List[MemoryItem]) -> None:
        self._write_file(self.path, items)
        self._write_file(self._backup_path(), items)
        self._items = list(items)

    # File I/O: JSON to a .tmp file, then os.replace() over the real one (atomic: never half-written).
    def _write_file(self, target: Path, items: Iterable[MemoryItem]) -> None:
        allowed, reason = check_path(str(target), PathIntent.WRITE)  # R9
        if not allowed:
            raise PermissionError(reason)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": FORMAT_VERSION,
                "items": [{"id": i.id, "text": i.text, "saved": i.saved, "kind": i.kind} for i in items]}
        partial = target.with_name(target.name + ".tmp")
        partial.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(partial, target)


def _parse(raw: str) -> List[MemoryItem]:
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("not a memory file")
    items = []
    for entry in data["items"]:
        item = MemoryItem(id=str(entry["id"]), text=str(entry["text"]), saved=str(entry["saved"]),
                          kind=str(entry.get("kind", "told")))
        item.saved_at  # a bad date is a damaged file
        items.append(item)
    return items
