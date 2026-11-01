"""F8 commands (the developer's F8 decisions).

- "what's on my screen" / "read my screen" -> one passive look (fixed
  action `capture_screen`, L1), local OCR, and a short local summary: the
  window titles and the first lines of text. Nothing goes online.
- "find <word> on my screen" -> the same look and a local search: "Yes, I
  can see '<word>' in <window title>." L1, nothing goes online.
- "explain my screen" / "help me with this" / "what am I looking at" ->
  the WHOLE screen goes online, but only after the R4 preview: the exact
  downscaled image (protected windows black), the exact OCR text and his
  question are shown on the dashboard with "This will be sent online.", and
  he says "confirm" (fixed action `send_screen_online`, L3). Cancel, Stop
  or a timeout sends nothing and frees the image.

The image and text stay in memory only (F8: never saved). R4's "the same
file is sent" check is kept without a file: the SHA-256 of the exact
previewed bytes is in the confirmed action's parameters, and the bytes are
hashed again just before sending; a mismatch sends nothing.

What is logged (F8): that he asked, which windows were read and how much
was sent (type, size, hash; R4) -- never the screen's text, nor the answer
about it.
"""

from __future__ import annotations

import hashlib
from collections import Counter
import logging
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from jarvis.interpreter import ActionRequest, Answer
from jarvis.screen.capture import ScreenCapture, capture, downscale_jpeg, protected_category
from jarvis.screen.ocr import OcrLine, OcrUnavailable, read_text
from jarvis.sensors import SCREEN, SensorFlags
from jarvis.settings import SETTINGS
from jarvis.speech.claims import ClaimCheck

logger = logging.getLogger(__name__)

_CFG = SETTINGS["screen"]
_REPLIES = _CFG["replies"]
_MESSAGES = SETTINGS["setup_messages"]
VISION_SERVICE = "groq"  # OnlineStatus service while the request is out (ONLINE)


def _phrase_set(key: str) -> set:
    """Each phrase as said AND after the interpreter's clean-up ("what can
    you see on my screen" reaches the matcher as "what see on my screen")."""
    from jarvis.actions.command_text import clean_command

    said = {" ".join(str(p).lower().split()) for p in _CFG[key]}
    return said | {clean_command(p) for p in said}


_READ = _phrase_set("read_phrases")
_EXPLAIN = _phrase_set("explain_phrases")
_FIND = re.compile(str(_CFG["find_pattern"]))

# "What am I looking right now", "Can you please
# tell me what I'm looking right now" -> read the screen.
_LOOKING_AT = re.compile(r"(?:(?:can you |could you )?(?:please )?tell me )?what (?:am i|i'm|i am|im) (?:looking|looking at|seeing)"
                         r"(?: at)?(?: right now| now| here)?")
# "Splend my screen", "X-Link, My screen": a close mishearing of "explain".
# the display and the monitor are the screen too.
_SCREEN_END = re.compile(r"(?P<before>.+?)[ ,]+(?:on )?(?:my|the|this) (?:screen|display|monitor)")
_EXPLAIN_WORDS = {str(w).lower() for w in _CFG["explain_mishearings"]}


def _explain_like(sentence: str) -> bool:
    """"<word> my screen" where the word is "explain", within two letters of
    it, or a mishearing from [screen] explain_mishearings."""
    from jarvis.actions.command_text import edit_distance

    match = _SCREEN_END.fullmatch(sentence.replace(",", " "))
    if match is None:
        return False
    before = match.group("before").split()
    for i, word in enumerate(before):
        joined = word.replace("-", "")
        if word in _EXPLAIN_WORDS or joined in _EXPLAIN_WORDS or (len(joined) >= 5 and edit_distance(joined, "explain") <= 2):
            return True
        if i + 1 < len(before) and (word + before[i + 1]) in _EXPLAIN_WORDS:
            return True
    return False


@dataclass(frozen=True)
class ScreenPreview:
    """Exactly what would be sent (R4), held in memory until confirm/cancel."""

    preview_id: str
    jpeg: Optional[bytes]  # None: text only (no image model)
    size: Optional[tuple]
    text: str
    question: str
    sha256: str
    windows_read: tuple
    covered: int = 0  # How many private windows are black in the picture (told to the model)

    @staticmethod
    def digest(jpeg: Optional[bytes], text: str, question: str) -> str:
        """A SHA-256 over the image, the text and the question: what was previewed is exactly what gets
        sent."""
        h = hashlib.sha256()
        h.update(jpeg or b"")
        h.update(b"\x00")
        h.update(text.encode("utf-8"))
        h.update(b"\x00")
        h.update(question.encode("utf-8"))
        return h.hexdigest()

    def unchanged(self, expected: str) -> bool:
        """True if the preview still matches the digest he confirmed."""
        return self.digest(self.jpeg, self.text, self.question) == expected


def _join(items: List[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


_APPS = [({str(p).lower() for p in app["processes"]}, [str(c) for c in app["cut"]], str(app["phrase"]), str(app["kind"]),
          str(app.get("app", "")))
         for app in _CFG.get("apps", [])]
_NOTIFICATION_COUNT = re.compile(r"^\(\d+\)\s*")


def describe_window(window) -> tuple:
    """(how she names it, kind): "(22) YouTube - Google Chrome" ->
    ("YouTube open in Chrome", "page"); "Report.docx - Word" -> ("a Word
    document called Report", "document"). Kind is document, page or app."""
    # "marigold_launch_plan.docx  -  Compatibility Mode - Word": spacing evened
    # out, and every suffix cut (it was "a Word document called Compatibility Mode").
    title = re.sub(r"\s+-\s+", " - ", _NOTIFICATION_COUNT.sub("", window.title.strip()))
    app = _app_of(window)
    if app is None:
        return _short(plain_title(title), int(_CFG["title_max_chars"])), "app"
    _processes, cuts, phrase, kind, _name = app
    name = _cut_title(title, cuts, kind)
    return phrase.format(name=_short(plain_title(name or title), int(_CFG["title_max_chars"]))), kind


def _app_of(window):
    process = (window.process or "").lower()
    return next((app for app in _APPS if process in app[0]), None)


def _cut_title(title: str, cuts: List[str], kind: str) -> str:
    name = title
    cut_one = True
    while cut_one:
        cut_one = False
        for cut in cuts:
            if name.endswith(cut):
                name = name[: -len(cut)].strip()
                cut_one = True
                break
    return re.sub(r"\.(docx?|xlsx?|pptx?|pdf|txt)$", "", name, flags=re.IGNORECASE) if kind == "document" else name


def window_name(window) -> str:
    """How a found
    word's window is named -- "Chrome – JARVIS feature list" (the app, then
    the cleaned title), the app alone, or the title alone. "" when there is
    nothing usable (no window, a system or protected one, an empty or
    ID-only title from an unknown app): the caller then says "on your
    screen". Never an empty name in a sentence."""
    if window is None or getattr(window, "system", False) or window.protected:
        return ""
    title = re.sub(r"\s+-\s+", " - ", _NOTIFICATION_COUNT.sub("", (window.title or "").strip()))
    app = _app_of(window)
    name = _cut_title(title, app[1], app[3]) if app is not None else title
    name = plain_title(name) if name.strip() else ""
    name = "" if name == "a page" else _short(name, int(_CFG["title_max_chars"]))
    app_name = app[4] if app is not None else ""
    if app_name and name:
        return f"{app_name} – {name}"
    return app_name or name


def split_showing(shot: ScreenCapture, samples: int = 12):
    """(showing, behind): readable windows with at least [screen]
    min_visible_share of their area on top, and the ones hidden behind
    others (top of the z-order first)."""
    showing, behind = [], []
    share = float(_CFG["min_visible_share"])
    for window in shot.visible_windows:
        left, top, right, bottom = window.rect
        on_top = total = 0
        for i in range(samples):
            for j in range(samples):
                x = left + (right - left) * (i + 0.5) / samples - shot.origin[0]
                y = top + (bottom - top) * (j + 0.5) / samples - shot.origin[1]
                total += 1
                on_top += shot.window_at(x, y) is window
        (showing if total and on_top / total >= share else behind).append(window)
    return showing, behind


def lines_by_window(shot: ScreenCapture, lines: List[OcrLine]) -> Dict[int, List[str]]:
    """Each OCR line under the window it is in (by its first word's centre):
    lines over the desktop, the taskbar, JARVIS or a protected window are
    dropped."""
    out: Dict[int, List[str]] = {}
    for line in lines:
        if len(line.text.strip()) < int(_CFG["min_line_chars"]) or not line.words:
            continue
        window = shot.window_at(*line.words[0].centre)
        if window is None or not window.readable:
            continue
        if _in_browser_ui(shot, window, line):
            continue  # The tab strip, address bar and bookmarks
        text = meaningful(line.text)
        if text:
            out.setdefault(id(window), []).append(_short(text, int(_CFG["line_max_chars"])))
    return out


_BROWSERS = {str(p).lower() for p in _CFG.get("browser_processes", [])}
# "where it is", "where is it?", "which window" -- right after a find.
_WHERE_IT = re.compile(r"(?:so |and |ok |okay )?(?:where(?:'s| is| was)? (?:it|that|the word)(?: is| was)?(?: on (?:my|the) screen)?|"
                       r"which window(?: is it(?: in)?)?|where on (?:my|the) screen)")


def _area(share: float) -> str:
    """Top, middle or bottom third of the screen."""
    key = "area_top" if share < 1 / 3 else "area_bottom" if share > 2 / 3 else "area_middle"
    return str(_REPLIES[key])
_UI_PHRASES = {" ".join(str(p).lower().split()) for p in _CFG.get("ui_phrases", [])}
_URL = re.compile(r"(?:https?://|www\.)\S+|\b[\w-]+(?:\.[\w-]+)*\.(?:com|ai|org|net|io|au|co|dev|app|gov|edu|uk|us|me|tv|so|gg)\b(?:/\S*)?",
                  re.IGNORECASE)
_NOISE = re.compile(r"\S*\d{6,}\S*|\b(?=\w*\d)(?=\w*[a-z])\w{16,}\b|\S*_\S*_\S*|[€£¥©®™|<>{}\[\]~^`]+", re.IGNORECASE)


def meaningful(text: str) -> str:
    """The line without URLs, long numbers, IDs, hashes and file-name noise;
    "" when what is left isn't words a person would say ("X +", "Ask
    Gemini", a run of symbols)."""
    cleaned = _NOISE.sub(" ", _URL.sub(" ", text))
    cleaned = re.sub(r"(?:^|\s)[xX×+](?=\s|$)", " ", cleaned)  # a tab's close and new-tab marks
    cleaned = " ".join(cleaned.split()).strip(" ,;:-+")
    if not cleaned or " ".join(cleaned.lower().split()) in _UI_PHRASES:
        return ""
    letters = sum(c.isalpha() or c.isspace() for c in cleaned)
    if letters / len(cleaned) < float(_CFG.get("min_letter_share", 0.6)):
        return ""
    if len(re.findall(r"[A-Za-z]{3,}", cleaned)) < int(_CFG.get("min_real_words", 2)):
        return ""
    return cleaned


def front_sentence(window) -> str:
    """"You're looking at Chrome showing JARVIS feature list." -- the app and
    its cleaned title, or whichever of the two there is."""
    title = re.sub(r"\s+-\s+", " - ", _NOTIFICATION_COUNT.sub("", (window.title or "").strip()))
    app = _app_of(window)
    name = _cut_title(title, app[1], app[3]) if app is not None else title
    name = plain_title(name) if name.strip() and not window.protected else ""
    name = "" if name == "a page" else _short(name, int(_CFG["title_max_chars"]))
    app_name = app[4] if app is not None else ""
    if app_name and name:
        return str(_REPLIES["front"]).format(app=app_name, title=name)
    return str(_REPLIES["front_one"]).format(name=app_name or name or str(_REPLIES["front_unnamed"]))


def _real_words(text: str) -> List[str]:
    return re.findall(r"[A-Za-z][A-Za-z']{2,}", text)


def best_line(lines: List[str]) -> str:
    """The one line that best says what the page is about: enough real
    words, not a list of names (mostly capitalised, or comma-separated
    scraps); the one with the most words of the first few, as a sentence."""
    best, most = "", 0
    for line in lines[: int(_CFG.get("front_scan_lines", 12))]:
        words = _real_words(line)
        if len(words) < int(_CFG.get("front_min_words", 4)):
            continue  # a fragment
        capitals = sum(w[0].isupper() for w in words) / len(words)
        pieces = [p for p in line.split(",") if p.strip()]
        if capitals > float(_CFG.get("front_max_capital_share", 0.6)) or (len(pieces) >= 3 and len(words) / len(pieces) <= 2):
            continue  # a list of names
        if len(words) > most:
            best, most = line, len(words)
    if not best:
        return ""
    best = _short(best, int(_CFG["line_max_chars"])).rstrip(" .,;:-")
    return best[0].upper() + best[1:]


def _in_browser_ui(shot: ScreenCapture, window, line: OcrLine) -> bool:
    if (window.process or "").lower() not in _BROWSERS:
        return False
    y = line.words[0].centre[1] + shot.origin[1]
    return y - window.rect[1] < int(_CFG.get("browser_ui_band_px", 120))


def explain_instructions(covered: int) -> str:
    """The vision model said "Two black rectangles block my view
    ... Type your OK statement to lift them" when nothing was blacked out.
    It is told exactly how many private windows are hidden, to ignore them,
    and never to ask for them to be removed."""
    note = _CFG["covered_note_none"] if covered == 0 else str(_CFG["covered_note_some"]).format(count=covered)
    return f"{_CFG['instructions']} {note}"


def plain_title(name: str) -> str:
    """"123456789_11112222333344445_6666777788889990000_n.jpg" -> "an image";
    IDs and long numbers dropped from any title."""
    if re.search(r"\.(?:jpe?g|png|gif|webp|bmp|heic)$", name, re.IGNORECASE) and not meaningful(re.sub(r"\.\w+$", "", name)):
        return "an image"
    # "meeting_notes_marigold.txt" was "a page" -- an
    # underscore between letters separates words; only ID-like runs are noise.
    name = re.sub(r"(?<=[A-Za-z])_(?=[A-Za-z])", " ", name)
    cleaned = " ".join(_NOISE.sub(" ", name).split()).strip(" -_.,")
    return cleaned if re.search(r"[A-Za-z]{2,}", cleaned) else "a page"


class ScreenCommands:
    """F8: 'what's on my screen' and 'find X on my screen' locally; 'explain my screen' online only after
    an R4 preview and confirm."""
    def __init__(
        self,
        answerer=None,
        sensors: Optional[SensorFlags] = None,
        capture_fn: Callable[[], ScreenCapture] = capture,
        ocr_fn: Callable[[object], List[OcrLine]] = read_text,
        online_status=None,
    ) -> None:
        self._answerer = answerer
        self.sensors = sensors or SensorFlags()
        self._capture = capture_fn
        self._ocr = ocr_fn
        self._online_status = online_status
        self._orchestrator = None
        self._lock = threading.Lock()
        self._previews: Dict[str, ScreenPreview] = {}
        self._claims = ClaimCheck(SETTINGS["claims"])
        # The last find that hit: (when, word, window name, area). No screen
        # text or pixels -- only what she already said.
        self._last_find: Optional[tuple] = None
        self._clock = time.monotonic

    # -- registration ------------------------------------------------------------------

    def register(self, interpreter, orchestrator) -> None:
        """Register the screen matcher and its executors."""
        self._orchestrator = orchestrator
        interpreter.register("screen", self.matcher)
        orchestrator.register_executor("capture_screen", self.capture_executor)
        orchestrator.register_executor("send_screen_online", self.send_executor)
        orchestrator.controller.add_transition_listener(self.on_transition)

    def _report(self, step: str) -> None:
        if self._orchestrator is not None:
            self._orchestrator.report_step(step)

    def on_transition(self, entry) -> None:
        """Free a preview the moment it can no longer be sent: cancelled,
        stopped, expired or replaced. Kept only while it is the pending
        confirmation, or the permit about to be used for it."""
        controller = self._orchestrator.controller if self._orchestrator is not None else None
        if controller is None:
            return
        keep = set()
        pending = controller.pending_confirmation()
        if pending is not None and pending.action_name == "send_screen_online":
            keep.add(dict(pending.params).get("preview_id"))
        permit = controller.current_permit()
        if permit is not None and permit.action_name == "send_screen_online":
            keep.add(dict(permit.params).get("preview_id"))
        with self._lock:
            for preview_id in list(self._previews):
                if preview_id not in keep:
                    del self._previews[preview_id]

    def preview(self, preview_id: str) -> Optional[ScreenPreview]:
        """For the dashboard's preview card."""
        with self._lock:
            return self._previews.get(preview_id)

    # -- matching ------------------------------------------------------------------------

    def matcher(self, cleaned: str):
        """Turn a screen command into a capture request, a 'where is it' answer, or an explain preview.
        None otherwise."""
        sentence = " ".join(cleaned.lower().replace("’", "'").split()).rstrip(".!?")
        if sentence in _READ or _LOOKING_AT.fullmatch(sentence):
            return ActionRequest("capture_screen", {"purpose": "summary", "word": "", "display_name": "your screen"})
        found = _FIND.fullmatch(sentence)
        if found is not None:
            word = (found.group("word") or found.groupdict().get("word2") or "").strip(" '\"")
            if word:
                return ActionRequest("capture_screen", {"purpose": "find", "word": word, "display_name": "your screen"})
        where = self.where_it_is(sentence.replace("?", "").replace(",", ""))
        if where is not None:
            return where
        if sentence in _EXPLAIN:
            return self._prepare_explain(sentence)
        if _explain_like(sentence):
            return self._prepare_explain("explain my screen")  # what he meant, not the mishearing
        return None

    # -- one look ---------------------------------------------------------------------------

    def _look(self):
        """Capture (blacked out inside capture()), then OCR. Returns
        (shot, lines, problem). The screen row is on for exactly this."""
        with self.sensors.active(SCREEN):
            self._report(str(_REPLIES["step_capturing"]))
            try:
                shot = self._capture()
            except ImportError:
                return None, [], str(_MESSAGES["screen_capture_missing"])
            except OSError:
                logger.exception("Screen capture failed")
                return None, [], str(_REPLIES["capture_failed"])
            if shot.covered:
                self._report(str(_REPLIES["step_covered"]).format(count=shot.covered))
            # Which KINDS of window were blacked out (never titles).
            kinds = Counter(protected_category(w.title, w.process) for w in shot.windows if w.protected and not w.minimised)
            self._report("blacked out: " + (", ".join(f"{n} {kind}" for kind, n in sorted(kinds.items())) or "nothing (no private window open)"))
            try:
                lines = self._ocr(shot.image)
            except OcrUnavailable as missing:
                return shot, [], str(missing)
        titles = [describe_window(w)[0] for w in shot.visible_windows]
        if titles:
            self._report(f"read text from: {_join([_short(t, 50) for t in titles[: int(_CFG['summary_windows'])]])}")
        return shot, lines, None

    def capture_executor(self, action_name: str, params: dict):
        """capture_screen (L1). Returns (verified, spoken, logged): the
        spoken answer holds screen text, the log line never does."""
        purpose, word = str(params.get("purpose")), str(params.get("word") or "")
        if sys.platform == "darwin":  # no screen-text reading on macOS; "explain my screen" still works
            message = str(_MESSAGES["screen_text_mac"])
            return False, message, message
        shot, lines, problem = self._look()
        try:
            if shot is None:
                return False, problem, problem
            if purpose == "find":
                return self._find(shot, lines, word, problem)
            return self._summary(shot, lines, problem)
        finally:
            if shot is not None:
                shot.release()
                self._report(str(_REPLIES["step_freed"]))

    def _summary(self, shot: ScreenCapture, lines: List[OcrLine], problem: Optional[str]):
        """Every window that is showing, on every monitor,
        named by what it is ("YouTube open in Chrome", "a Word document
        called Report"), then the first lines of the main one. Each line of
        text belongs to the window it is in; the desktop, the taskbar and
        JARVIS are never read or named."""
        # ("...Also open but minimised: VS Code... Behind
        # them, so I can't read it: WhatsApp and WhatsApp. The page says: <raw
        # OCR fragments>"). Only the window in front, in plain words, and one
        # clean sentence from it. Minimised and background windows are never named.
        showing, _behind = split_showing(shot)
        texts = lines_by_window(shot, lines)
        front = showing[0] if showing else None
        parts = []
        if front is None:
            parts.append(str(_REPLIES["no_windows"]))
        else:
            parts.append(front_sentence(front))
        if problem:
            parts.append(problem)
        elif front is not None:
            line = best_line(texts.get(id(front), []))
            if not texts.get(id(front)):
                parts.append(str(_REPLIES["no_text"]))
            elif line:
                parts.append(str(_REPLIES[f"starts_{describe_window(front)[1]}"]).format(lines=line))
        logged = (f"described the screen: the window in front ({len(showing)} showing, {len(shot.minimised_windows)} minimised, "
                  f"{shot.covered} covered), {len(lines)} line(s) of text (text not logged)")
        return problem is None, " ".join(parts), logged

    def _find(self, shot: ScreenCapture, lines: List[OcrLine], word: str, problem: Optional[str]):
        if problem:
            return False, problem, problem
        wanted = word.lower()
        for line in lines:
            if wanted not in line.text.lower():
                continue
            first = wanted.split()[0]
            hit = next((w for w in line.words if first in w.text.lower()), line.words[0] if line.words else None)
            window = shot.window_at(*hit.centre) if hit is not None else None
            name = window_name(window)
            text = str(_REPLIES["found_in"]).format(word=word, title=name) if name else str(_REPLIES["found"]).format(word=word)
            height = float(getattr(shot.image, "height", 0) or 0)
            self._last_find = (self._clock(), word, name, _area(hit.centre[1] / height if hit is not None and height else 0.5))
            self._note_topic()
            return True, text, text
        self._last_find = None
        text = str(_REPLIES["not_found"]).format(word=word)
        return True, text, text

    def _note_topic(self) -> None:
        note = getattr(self._orchestrator, "note_topic", None)
        if note is not None:
            note("screen")

    def where_it_is(self, sentence: str) -> Optional[Answer]:
        """("where it is" right after a screen find went
        online): answered from that find, locally, while it is the latest
        thing looked at (a camera look since then wins) and no older than
        [screen] follow_up_seconds."""
        if self._last_find is None or _WHERE_IT.fullmatch(sentence) is None:
            return None
        at, word, name, area = self._last_find
        if self._clock() - at > float(_CFG["follow_up_seconds"]):
            return None
        latest = getattr(self._orchestrator, "latest_topic", None)
        if latest is not None and latest() not in (None, "screen"):
            return None
        if name:
            return Answer(str(_REPLIES["found_where"]).format(word=word, title=name, area=area))
        return Answer(str(_REPLIES["found_where_unnamed"]).format(word=word, area=area))

    # -- explain: the R4 preview, then L3 ---------------------------------------------------------

    def _prepare_explain(self, question: str):
        shot, lines, problem = self._look()
        if shot is None:
            return Answer(problem)
        try:
            vision_model = str(_CFG["vision_model"]).strip()
            jpeg, size = (downscale_jpeg(shot.image, int(_CFG["send_max_side"]), int(_CFG["send_jpeg_quality"]))
                          if vision_model else (None, None))
            text = "\n".join(l.text for l in lines)[: int(_CFG["send_max_text_chars"])]
            windows = tuple(w.title for w in shot.visible_windows[: int(_CFG["summary_windows"])])
            covered = shot.covered
        finally:
            shot.release()
            self._report(str(_REPLIES["step_freed_full"]))
        if jpeg is None and not text:
            return Answer(problem or str(_REPLIES["nothing_to_send"]))
        preview = ScreenPreview(uuid.uuid4().hex, jpeg, size, text, question, ScreenPreview.digest(jpeg, text, question), windows,
                                covered=covered)
        with self._lock:
            self._previews = {preview.preview_id: preview}  # only the newest preview exists
        self._report(self._sending_line(preview, ready=True))
        prompt = str(_REPLIES["confirm_prompt" if jpeg is not None else "confirm_prompt_text_only"])
        return ActionRequest("send_screen_online",
                             {"preview_id": preview.preview_id, "sha256": preview.sha256, "question": question,
                              "display_name": "your screen"},
                             prompt=prompt)

    @staticmethod
    def _sending_line(preview: ScreenPreview, ready: bool) -> str:
        """R4's log line: type, size and hash, never the content."""
        what = []
        if preview.jpeg is not None:
            what.append(f"screenshot (JPEG {preview.size[0]}x{preview.size[1]}, {len(preview.jpeg) // 1024} KB)")
        what.append(f"screen text ({len(preview.text)} characters)")
        what.append("your question")
        verb = "ready to send online after you confirm" if ready else "sending online"
        return f"{verb}: {_join(what)}; sha256 {preview.sha256[:12]}"

    def send_executor(self, action_name: str, params: dict):
        """L3: send the confirmed preview online, only if it is unchanged, and say the explanation."""
        preview = self.preview(str(params.get("preview_id")))
        if preview is None or not preview.unchanged(str(params.get("sha256"))):
            text = str(_REPLIES["preview_changed"])
            return False, text, text
        try:
            if self._answerer is None:
                text = str(_REPLIES["no_answerer"])
                return False, text, text
            orch = self._orchestrator
            model = str(_CFG["vision_model"]).strip() if preview.jpeg is not None else self._answerer.model

            def going_online() -> None:
                self._report(self._sending_line(preview, ready=False))
                if orch is not None:
                    orch.record_online_destination(f"Groq, {model}; R4 sha256 {preview.sha256}")
                    orch.announce(str(_REPLIES["sending"]))

            if self._online_status is not None:
                self._online_status.hold(VISION_SERVICE)
            try:
                result = self._answerer.answer_screen(preview.question, preview.text, preview.jpeg, model,
                                                      explain_instructions(preview.covered), on_going_online=going_online)
            finally:
                if self._online_status is not None:
                    self._online_status.release(VISION_SERVICE)
            if result.error is not None or not result.text:
                text = result.text or str(_REPLIES["send_failed"])
                return False, text, f"explaining the screen failed: {result.error}"
            claim = self._claims.find(result.text, preview.question)
            spoken = claim.replacement if claim is not None else result.text
            return True, spoken, "explained the screen (the answer describes laptop data, so it isn't logged)"
        finally:
            with self._lock:
                self._previews.pop(preview.preview_id, None)  # sent or failed: freed either way
