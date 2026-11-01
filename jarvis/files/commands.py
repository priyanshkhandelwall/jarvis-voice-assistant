"""F19 File processor (built).

| Say | She | Trust |
|---|---|---|
| "what's in <file>", "how many pages/words/rows in <file>" | a local answer: pages and words, or CSV rows, columns and headers | local (read_document) |
| "read <file>" | reads the first words aloud | local |
| "summarise <file>" | the R4 preview of the EXACT text, then one "confirm" sends it online | L3 (send_text_online) |

- The file is found through F4's index (its matching, and a numbered pick
  list when several match: "the second one", "number 2", part of the name),
  or a typed full path. PDF, DOCX, TXT/MD and CSV only. It must pass R9's
  read check: protected folders are never read.
- Everything is read locally (jarvis/files/extract.py). JARVIS never writes
  to his files and converts nothing.
- Online: at most [groq] max_chars_per_request per request and
  max_chunks_per_task requests (48,000 characters). Longer: "This file is
  long. Which part: the start, the middle, or the end?"
- R4: the exact text is written to jarvis_data/tmp/f19-<id>.txt and hashed;
  the dashboard shows that file's text with "This will be sent online." and
  "Online service terms not yet checked by you" (until [online]
  terms_checked = true). After "confirm" the file is hashed again and sent
  only if unchanged. It is deleted after sending, on cancel/Stop/expiry, and
  at startup (leftovers). Cancel sends nothing.
- A file's content never reaches Live Activity or debug.log: they get its
  name, type, size, the destination, the time and a hash. What she says
  from it is shown only as "(about <name>: the file's content isn't shown
  here)".
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from jarvis.actions.command_text import PickSlot, clean_command, extract_command, name_pick, pick_index
from jarvis.actions.file_index import FileIndex, FileMatch, clean_query
from jarvis.actions.find_file import listing_lines
from jarvis.files.extract import PAGE_MARK, Extracted, ExtractError, extract
from jarvis.interpreter import ActionRequest, Answer
from jarvis.online.chunked import split_text, summarise_chunks
from jarvis.security import PathIntent, check_path
from jarvis.settings import SETTINGS
from jarvis.speech.length import limit_sentences

logger = logging.getLogger(__name__)

_CFG = SETTINGS["file_processor"]
_REPLIES = _CFG["replies"]
_GROQ = SETTINGS["groq"]
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_EXTENSIONS = tuple(str(e).lower() for e in _CFG["extensions"])
_FILE_WORDS = [str(w).lower() for w in _CFG["file_words"]]
_PICK_LEAD = {str(w).lower() for w in _CFG.get("pick_lead_words", [])}


def _pick_words(normalized: str) -> str:
    """"will summarize file one" -> "file one": his verb is not part of the pick."""
    words = re.findall(r"[a-z0-9']+", normalized.lower())
    while len(words) > 1 and words[0] in _PICK_LEAD:
        words = words[1:]
    return " ".join(words)

SEND_ACTION = "send_text_online"
SUMMARISE, OVERVIEW, READ = "summarise", "overview", "read"

_INTENTS = [
    (SUMMARISE, re.compile(r"(?:summari[sz]e|sum up|give me a summary of)\s+(?P<t>.+)")),
    (OVERVIEW, re.compile(r"(?:what's|whats|what is)\s+in\s+(?P<t>.+)")),
    (OVERVIEW, re.compile(r"how many (?:pages|words|rows|columns|lines)\s+(?:are\s+|is\s+)?(?:there\s+)?in\s+(?P<t>.+)")),
    (OVERVIEW, re.compile(r"(?:word|page|row)\s+count\s+(?:of|for|in)\s+(?P<t>.+)")),
    # "What are the columns in my budget summary staff CSV?" was a memory question.
    (OVERVIEW, re.compile(r"(?:what|which)\s+(?:are\s+|is\s+)?(?:the\s+)?(?:columns|headers|column names|headings|fields)\s+(?:are\s+)?(?:in|of)\s+(?P<t>.+)")),
    (READ, re.compile(r"read\s+(?:out\s+|me\s+)*(?P<t>.+)")),
]
_TYPED_PATH = re.compile(r"[A-Za-z]:[\\/][^\"<>|?*\r\n]+?\.(?:pdf|docx|txt|md|csv)\b", re.IGNORECASE)
_NOT_FILES = {"my screen", "the screen", "screen", "this video", "the video", "that video", "my topics", "the news", "news",
              "front of you", "my mind", "the room", "my emails", "my email", "the weather"}
_PARTS = {"start": "start", "beginning": "start", "first part": "start", "middle": "middle", "end": "end", "last part": "end"}


def default_temp_dir() -> Path:
    """Where F19 keeps a file's extracted text while its preview waits (deleted afterwards)."""
    return _PROJECT_ROOT / str(_CFG["temp_dir"])


def sha256_bytes(data: bytes) -> str:
    """The SHA-256 of `data`, as hex (the preview's fingerprint)."""
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Preview:
    """R4: the exact text that would be sent, saved as a temporary file."""

    preview_id: str
    path: Path
    name: str
    chars: int
    sha256: str
    part: str  # "" (the whole file) or start / middle / end

    def text(self, limit: Optional[int] = None) -> str:
        """The exact text that would be sent online (at most `limit` characters)."""
        try:
            data = self.path.read_text(encoding="utf-8")
        except OSError:
            return ""
        return data if limit is None else data[:limit]


class FileCommands:
    """F19: reads PDF, Word, text and CSV files locally ('what's in', 'read'); 'summarise' goes online only
    after an R4 preview and an L3 confirm."""
    def __init__(self, index: FileIndex, answerer=None, temp_dir: Optional[Path] = None, slot: Optional[PickSlot] = None,
                 sleep=None, clock=None) -> None:
        self._index = index
        self._answerer = answerer
        self.temp_dir = temp_dir or default_temp_dir()
        self._slot = slot
        self._sleep = sleep
        self._clock = clock
        self._orchestrator = None
        self._lock = threading.Lock()
        self._previews: Dict[str, Preview] = {}
        self._pending_pick: Optional[Tuple[str, List[FileMatch], float]] = None  # (intent, matches, listed at)
        self._pending_part: Optional[Tuple[str, Extracted]] = None  # (name, extracted)
        self.clear_leftovers()

    # -- registration ------------------------------------------------------------------------------

    def register_pick(self, interpreter, orchestrator) -> None:
        """With the other list picks, first of all matchers."""
        if self._slot is None:
            self._slot = getattr(orchestrator, "picks", None)
        interpreter.register("file_processor_pick", self.pick_matcher)
        interpreter.register("file_processor_part", self.part_matcher)

    def register(self, interpreter, orchestrator) -> None:
        """After F8 ("read my screen") and F9, before F4 and Groq."""
        self._orchestrator = orchestrator
        if self._slot is None:
            self._slot = getattr(orchestrator, "picks", None)
        interpreter.register("file_processor", self.matcher, keep_case=True)
        orchestrator.register_executor(SEND_ACTION, self.send_executor)
        orchestrator.controller.add_transition_listener(self.on_transition)

    # -- temporary files (R4) -----------------------------------------------------------------------

    def clear_leftovers(self) -> None:
        """Startup: any f19-*.txt left by a crash is deleted."""
        try:
            for leftover in self.temp_dir.glob("f19-*.txt"):
                self._delete(leftover)
        except OSError:
            logger.warning("Could not clear %s", self.temp_dir, exc_info=True)

    def preview(self, preview_id: str) -> Optional[Preview]:
        """The pending preview with this id, or None."""
        with self._lock:
            return self._previews.get(preview_id)

    def on_transition(self, entry) -> None:
        """A preview lives only while it is the pending confirmation or the
        permit about to send it: cancelled, stopped, expired or replaced ->
        its temporary file is deleted."""
        controller = self._orchestrator.controller if self._orchestrator is not None else None
        if controller is None:
            return
        keep = set()
        pending = controller.pending_confirmation()
        if pending is not None and pending.action_name == SEND_ACTION:
            keep.add(dict(pending.params).get("preview_id"))
        permit = controller.current_permit()
        if permit is not None and permit.action_name == SEND_ACTION:
            keep.add(dict(permit.params).get("preview_id"))
        if entry.event_type is None:
            self._pending_pick = None
            self._pending_part = None
        with self._lock:
            gone = [p for pid, p in self._previews.items() if pid not in keep]
            for p in gone:
                del self._previews[p.preview_id]
        for p in gone:
            self._delete(p.path)
            self._report("temporary copy deleted")

    def _delete(self, path: Path) -> None:
        allowed, reason = check_path(str(path), PathIntent.WRITE)  # R9: deleting is a change
        if not allowed:
            logger.warning("Not deleting %s: %s", path, reason)
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not delete %s", path, exc_info=True)

    # -- understanding ------------------------------------------------------------------------------

    def matcher(self, text: str):
        """Handle 'summarise / what's in / read <file>': find the file, read it locally, and answer or
        prepare the summary."""
        typed = _TYPED_PATH.search(text)
        command = clean_command(extract_command(text) or text)
        intent, target = None, ""
        for kind, pattern in _INTENTS:
            match = pattern.fullmatch(command)
            if match is not None:
                intent, target = kind, match.group("t").strip(" .?!'\"")
                break
        if intent is None:
            return None
        if typed is not None:
            return self._open(intent, Path(typed.group(0)), Path(typed.group(0)).name)
        target = re.sub(r"^(?:the|my|this|that)\s+", "", target)
        if not target or target in _NOT_FILES:
            return None
        filey = any(re.search(rf"\b{re.escape(w)}\b", target) for w in _FILE_WORDS) or bool(re.search(r"\.\w{2,4}\b", target))
        if not self._index.is_ready():
            return Answer(str(_REPLIES["index_not_ready"])) if filey else None
        ext = next((e for e in _EXTENSIONS if target.endswith(e) or re.search(rf"\b{e[1:]}\b", target)), None)
        query = clean_query(re.sub(r"\b(?:file|document|word document|text file|spreadsheet)\b", " ", target))
        matches = [m for m in self._index.search(query, kind="file") if m.path.lower().endswith(_EXTENSIONS)]
        if ext is not None:
            matches = [m for m in matches if m.path.lower().endswith(ext)] or matches
        if not matches:
            return Answer(str(_REPLIES["not_found"]).format(name=target)) if filey else None
        if len(matches) == 1:
            return self._open(intent, Path(matches[0].path), matches[0].filename)
        self._pending_pick = (intent, matches, self._slot.now() if self._slot is not None else 0.0)
        if self._slot is not None:
            self._slot.claim(self)
        for line in listing_lines(matches):
            self._report(line)
        return Answer(str(_REPLIES["which_file"]).format(count=len(matches)))

    def pick_matcher(self, normalized: str):
        """"2", "the second one", or part of a name, after the list."""
        if self._pending_pick is None:
            return None
        intent, matches, listed_at = self._pending_pick
        if self._slot is not None and (not self._slot.fresh(listed_at) or not self._slot.owned_by(self)):
            if not self._slot.fresh(listed_at):
                self._pending_pick = None
            return None
        number = pick_index(_pick_words(normalized))
        if number is not None:
            if not 1 <= number <= len(matches):
                return Answer(f"There are only {len(matches)} files on the list. Say a number from 1 to {len(matches)}.")
            chosen = matches[number - 1]
        else:
            index, _tied = name_pick(normalized, [m.filename for m in matches])
            if index is None:
                return None  # not a pick: the list stays for [interpreter] pick_valid_seconds
            chosen = matches[index]
        self._pending_pick = None
        if self._slot is not None:
            self._slot.release(self)
        return self._open(intent, Path(chosen.path), chosen.filename)

    def part_matcher(self, normalized: str):
        """The answer to "the start, the middle, or the end?" -- the next
        command only."""
        if self._pending_part is None:
            return None
        name, extracted = self._pending_part
        self._pending_part = None
        words = normalized.replace("the ", " ").split()
        sentence = " ".join(words)
        part = next((v for k, v in _PARTS.items() if re.search(rf"\b{k}\b", sentence)), None)
        if part is None:
            return None
        limit = int(_GROQ["max_chars_per_request"]) * int(_GROQ["max_chunks_per_task"])
        text = extracted.send_text
        if part == "start":
            piece = text[:limit]
        elif part == "end":
            piece = text[-limit:]
        else:
            middle = len(text) // 2
            piece = text[max(0, middle - limit // 2): max(0, middle - limit // 2) + limit]
        return self._prepare_send(name, piece, part)

    # -- local work ----------------------------------------------------------------------------------

    def _open(self, intent: str, path: Path, name: str):
        allowed, reason = check_path(str(path), PathIntent.READ)  # R9: protected folders are never read
        if not allowed:
            logger.info("F19 refused %s: %s", path, reason)
            return Answer(str(_REPLIES["protected"]).format(name=name))
        try:
            extracted = extract(path)
        except ExtractError as problem:
            return Answer(self._problem(problem, name, path))
        size = path.stat().st_size if path.exists() else 0
        if extracted.kind == "pdf":
            self._report(f"reading {name} locally ({extracted.pages} pages)")
        else:
            self._report(f"reading {name} locally ({extracted.kind}, {size:,} bytes)")
        self._report(f"text extracted ({len(extracted.text):,} chars)")
        shown = str(_REPLIES["shown_instead"]).format(name=name)
        if intent == OVERVIEW:
            return Answer(self._overview(name, extracted), shown=shown)
        if intent == READ:
            words = PAGE_MARK.sub(" ", extracted.text).split()  # "[Page 1]" is ours, not his text
            start = " ".join(words[: int(_CFG["read_aloud_words"])])
            text = str(_REPLIES["read_start"]).format(name=name, text=start)
            if len(words) > int(_CFG["read_aloud_words"]):
                text += " " + str(_REPLIES["read_more"]) + " " + str(_REPLIES["offer_summary"]).format(name=name)
            return Answer(text, shown=shown)
        return self._summarise_request(name, extracted)

    def _overview(self, name: str, extracted: Extracted) -> str:
        if extracted.kind == "csv":
            headers = ", ".join(extracted.headers[:-1]) + f" and {extracted.headers[-1]}" if len(extracted.headers) > 1 else "".join(extracted.headers)
            text = str(_REPLIES["overview_csv"]).format(name=name, rows=extracted.rows, columns=extracted.columns, headers=headers)
        elif extracted.kind == "pdf":
            pages = f"{extracted.pages} page{'' if extracted.pages == 1 else 's'}"
            text = str(_REPLIES["overview_pdf"]).format(name=name, pages=pages, words=f"{extracted.words:,}")
            if extracted.pages_read is not None and extracted.pages_read < (extracted.pages or 0):
                text += " " + str(_REPLIES["pages_truncated"]).format(max=extracted.pages_read)
        else:
            text = str(_REPLIES["overview_text"]).format(name=name, words=f"{extracted.words:,}")
        return f"{text} {str(_REPLIES['offer_summary']).format(name=name)}"

    def _problem(self, problem: ExtractError, name: str, path: Path) -> str:
        if problem.key == "missing":
            return str(SETTINGS["setup_messages"]["system_library_missing"]).format(library=problem.reason)
        if problem.key == "too_big":
            return str(_REPLIES["too_big"]).format(name=name, size=problem.reason)
        if problem.key == "unsupported":
            return str(_REPLIES["unsupported"]).format(ext=path.suffix.lstrip(".") or "those")
        if problem.key == "unreadable":
            return str(_REPLIES["unreadable"]).format(name=name, reason=problem.reason)
        return str(_REPLIES[problem.key]).format(name=name)

    # -- online (R4 preview, L3) ---------------------------------------------------------------------

    def _summarise_request(self, name: str, extracted: Extracted):
        until = self._answerer.cooling_until() if self._answerer is not None and hasattr(self._answerer, "cooling_until") else None
        if until is not None:
            return Answer(str(_REPLIES["cooling"]).format(until=datetime.fromtimestamp(until).strftime("%H:%M")))
        limit = int(_GROQ["max_chars_per_request"]) * int(_GROQ["max_chunks_per_task"])
        if len(extracted.send_text) > limit:
            self._pending_part = (name, extracted)
            self._report(f"{name} is {len(extracted.send_text):,} characters, over the {limit:,} sent at most: asking which part")
            return Answer(str(_REPLIES["which_part"]))
        return self._prepare_send(name, extracted.send_text, "")

    def _prepare_send(self, name: str, text: str, part: str):
        preview_id = uuid.uuid4().hex
        path = self.temp_dir / f"f19-{preview_id}.txt"
        allowed, reason = check_path(str(path), PathIntent.WRITE)  # R9
        if not allowed:
            return Answer(str(_REPLIES["unreadable"]).format(name=name, reason="the temporary folder isn't allowed"))
        try:
            self.temp_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="")
            data = path.read_bytes()
        except OSError:
            logger.exception("R4 temporary copy not written")
            return Answer(str(_REPLIES["unreadable"]).format(name=name, reason="the temporary copy couldn't be saved"))
        preview = Preview(preview_id, path, name, len(text), sha256_bytes(data), part)
        with self._lock:
            old = list(self._previews.values())
            self._previews = {preview_id: preview}  # only the newest preview exists
        for p in old:
            self._delete(p.path)
        self._report(f"preview ready: {preview.chars:,} chars from {name}, sha256 {preview.sha256[:12]}; waiting for confirm")
        return ActionRequest(SEND_ACTION, {"preview_id": preview_id, "sha256": preview.sha256, "name": name, "chars": preview.chars,
                                           "part": part, "display_name": name},
                             prompt=str(_REPLIES["confirm_prompt"]).format(name=name, chars=f"{preview.chars:,}"))

    def send_executor(self, action_name: str, params: dict):
        """send_text_online, after his confirm: the same file, re-hashed."""
        preview = self.preview(str(params.get("preview_id")))
        name = str(params.get("name", "the file"))
        shown = str(_REPLIES["shown_instead"]).format(name=name)
        try:
            data = preview.path.read_bytes() if preview is not None else b""
            if preview is None or sha256_bytes(data) != params.get("sha256") or sha256_bytes(data) != preview.sha256:
                text = str(_REPLIES["preview_changed"])
                return False, text, "R4: the text changed after the preview; nothing sent", text
            self._report("confirmed")
            text = data.decode("utf-8")
            chunks = split_text(text)[: int(_GROQ["max_chunks_per_task"])]
            orch = self._orchestrator
            if orch is not None:
                # R4: type, size, destination, time and hash -- never the content.
                orch.record_online_destination(
                    f"sent text, {len(text):,} chars, to Groq ({getattr(self._answerer, 'model', '?')}), "
                    f"{datetime.now():%H:%M}, sha256 {preview.sha256[:16]}")
                orch.announce(str(SETTINGS["speech"]["checking_online_text"]))
            kwargs = {}
            if self._sleep is not None:
                kwargs["sleep"] = self._sleep
            if self._clock is not None:
                kwargs["clock"] = self._clock
            result = summarise_chunks(chunks, lambda t, i: self._answerer.summarise(t, i), str(_CFG["summary_instructions"]),
                                      str(_CFG["combine_instructions"]), self._stopped,
                                      lambda step: self._report(step.replace("summarising online", "checking online: summary")), **kwargs)
            if result.stopped:
                return False, "", "stopped before the summary was finished", shown
            if result.error is not None:
                text = str(_REPLIES["summary_failed"]).format(name=name, reason=result.text)
                return False, text, f"summary of {name} failed: {result.error.name}", text
            self._report("summary ready")
            spoken = limit_sentences(result.text, int(_CFG["spoken_summary_sentences"]))
            return True, spoken, f"summarised {name} ({len(text):,} chars sent; summary not logged)", shown
        finally:
            if preview is not None:
                with self._lock:
                    self._previews.pop(preview.preview_id, None)
                self._delete(preview.path)
                self._report("temporary copy deleted")

    # -- helpers -----------------------------------------------------------------------------------------

    def _report(self, step: str) -> None:
        if self._orchestrator is not None:
            self._orchestrator.report_step(step)

    def _stopped(self) -> bool:
        return self._orchestrator is not None and self._orchestrator.controller.is_stop_requested()
