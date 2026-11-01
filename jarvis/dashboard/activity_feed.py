"""F6's "LIVE ACTIVITY" feed: turns a real F5 DecisionRecord into a short
sequence of plain, human-readable steps ("heard: open chrome" ->
"searching for chrome" -> "opening Google Chrome"). Nothing else reaches
the panel: matched action names, parameters, candidate scores, trust
levels and raw result strings stay in the debug log instead
(jarvis/logbook/debug_log.py).

Pure formatting, no Flet, so it's testable without a GUI -- app.py only
calls format_record() and lays out the result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from typing import Iterable

from jarvis.logbook import DecisionRecord

_OPEN_PREFIX = "open "
_FIND_PREFIX = "find "
_STOPPED = "stopped before finishing"  # Logbook's result for a command Stop cut short


@dataclass(frozen=True)
class ActivityRow:
    """One Live Activity entry as shown: its time, its lines, and whether the result was verified."""
    time_label: str
    lines: list[str]
    verified: bool


def _query_after(heard: str | None, prefix: str) -> str:
    """The thing the developer named ("chrome" from "open chrome"), taken
    from what was heard so the step reads the way they said it."""
    text = (heard or "").strip()
    if text.lower().startswith(prefix):
        return text[len(prefix):].strip()
    # After "Did you mean...?" -> "Yes" it read "searching for
    # Yes". Not an "open/find ..." sentence: the caller shows the target.
    return ""


def _action_steps(record: DecisionRecord, action_name: str, params: dict) -> list[str]:
    display = params.get("display_name")
    # A matcher that already reported its own search step (record.progress)
    # said it as it happened; only say it here when nothing did.
    searched = any(step.startswith("searching") for step in record.progress)
    if action_name == "open_app":
        search = [] if searched else [f"searching for {_query_after(record.heard, _OPEN_PREFIX) or display}"]
        return [*search, f"opening {display}"]
    if action_name == "open_url":
        if params.get("search") is not None:  # F16b: a Google search in his Chrome
            return [f'searching Google for "{params["search"]}" in Chrome']
        if params.get("youtube_search") is not None:  # F18: YouTube's results in his Chrome
            return [f'searching YouTube for "{params["youtube_search"]}" in Chrome']
        search = [] if searched else [f"searching for {_query_after(record.heard, _OPEN_PREFIX) or display or 'the website'}"]
        # The site's name, never its URL; the record keeps the URL.
        return [*search, f"opening {display or 'the website'} in Chrome"]
    if action_name == "open_file":
        search = [] if searched else [f"searching for {_query_after(record.heard, _FIND_PREFIX) or display}"]
        return [*search, f"opening {display}"]
    label = _ACTION_LABELS.get(action_name)
    if label is not None:
        return [label] if label else []
    return [f"running {action_name.replace('_', ' ')}"]


# What the panel says for actions whose own steps tell the story (
# "running capture camera" read like a code name). "" = nothing extra.
_ACTION_LABELS = {
    "capture_screen": "",
    "capture_camera": "",
    "send_screen_online": "",
    "camera_stream": "",
    "remember": "",
    "forget_memory": "",
    "forget_all_memory": "",
    "edit_topic_list": "",  # F20 reports "topic added: ..."
    # F22 reports "set volume 40%: immediate (L1)" -> "volume now 40%" itself.
    "send_text_online": "",  # F19 reports its own steps (never the content)
    "set_volume": "", "set_mute": "", "set_brightness": "", "set_wifi": "", "lock_screen": "", "restart": "", "shutdown": "",
}


def format_record(record: DecisionRecord) -> ActivityRow:
    """F5: turn a decision record into the lines Live Activity shows (heard, understood as, steps, result),
    in plain words."""
    time_label = datetime.fromtimestamp(record.timestamp).strftime("%H:%M:%S")
    verified = record.verification_result == "True"

    understood_any = record.understood if isinstance(record.understood, dict) else {}
    ui_action = understood_any.get("ui_action")
    if ui_action:  # a dashboard action ("copied activity log"), not a command
        return ActivityRow(time_label=time_label, lines=[ui_action], verified=False)
    if "event" in understood_any:  # conversation mode: woke up / went to sleep
        said = understood_any.get("said")
        lines = [understood_any["event"], *([f"saying: {said}"] if said else [])]
        return ActivityRow(time_label=time_label, lines=lines, verified=False)
    if "asleep_heard" in understood_any:  # F15: shown, never acted on or kept
        return ActivityRow(time_label=time_label, lines=[f"heard while asleep: {understood_any['asleep_heard']}"], verified=False)
    if "ignored" in understood_any:
        reason = understood_any.get("reason", "too short/unclear")
        return ActivityRow(time_label=time_label, lines=[f"ignored ({reason}): {understood_any['ignored']}"], verified=False)

    if not record.heard and not record.started_by_jarvis:
        return ActivityRow(time_label=time_label, lines=["(nothing understood)"], verified=False)

    first = f"started by JARVIS: {record.started_by_jarvis}" if record.started_by_jarvis else f"heard: {record.heard}"
    lines: list[str] = [first, *record.progress]
    understood = record.understood or {}
    action_name = understood.get("action_name")

    if action_name:
        lines.extend(_action_steps(record, action_name, understood.get("params") or {}))
        # Success is already shown by the check mark on the row; only a
        # failure, denial or stop needs its reason spelled out.
        # (Unless what she said is already there as a "saying:" line.)
        said = any(step.startswith("saying: ") for step in record.closing)
        if record.completed and not verified and record.result and not said:
            lines.append(record.result)
    elif "answer" in understood:
        # A private answer (Answer.logged, F20) already has its own
        # session-only "saying:" line; the record holds the logged summary.
        if not any(step.startswith("saying: ") for step in record.closing):
            # An answer can be a list (one item per line): keep each line its own row.
            first, *rest = str(understood["answer"]).split("\n")
            lines.append(f"saying: {first}")
            lines.extend(rest)
    elif record.result and record.completed:
        lines.append(record.result)
    lines.extend(record.closing)  # after what she said: credit line, check-in
    # Stop during an answer left no trace on screen --
    # the row showed "saying: ..." as if she had finished.
    if record.result == _STOPPED and _STOPPED not in lines:
        lines.append(_STOPPED)

    return ActivityRow(time_label=time_label, lines=lines, verified=verified)


class NewRecordTracker:
    """Which completed records the feed hasn't shown yet, tracked by record
    ID. Counting doesn't work: the logbook keeps at most
    [logbook] max_records and drops the oldest, so once it is full the
    count stays the same while new records keep arriving -- the feed froze
    there.

    Only IDs still in the logbook are remembered, so this never holds more
    than max_records IDs (R15)."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def take_new(self, records: Iterable[DecisionRecord]) -> list[DecisionRecord]:
        """The completed records not returned before, oldest first."""
        completed = [r for r in records if r.completed]
        new = [r for r in completed if r.id not in self._seen]
        self._seen = {r.id for r in completed}
        return new


_NOTHING_TO_COPY = "(no activity yet)"


def format_session_log(records: Iterable[DecisionRecord]) -> str:
    """COPY LOG's plain text: one line per entry, oldest first, each starting
    with its time, steps joined by " | ". Built from the same records and
    the same format_record() as the panel, so anything redacted there
    (R11) is redacted here too."""
    lines = []
    for record in records:
        row = format_record(record)
        steps = " | ".join(" ".join(step.split()) for step in row.lines)
        lines.append(f"{row.time_label}  {steps}")
    return "\n".join(lines) if lines else _NOTHING_TO_COPY
