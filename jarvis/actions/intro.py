"""F1: Self Introduction.

The developer's fixed intro text is locked, word for word, and lives in
jarvis/settings/intro.txt so it can change without touching code. Do not
edit the text here or in that file without the developer's explicit
instruction -- it names features that are built later (screen, camera,
memory, permissions); if a feature is cut, the developer changes the
text himself.

This is a direct L0 answer (Section 5): it never touches the fixed
action set, never needs a permit, same as any other Q&A.

"what does JARVIS stand for", "who are you", "who
made you" and "what can you do" are answered here too, never online. The
router (intents.toml) rewrites his wording into the IDENTITY_COMMANDS below;
the replies are [identity] data, the expansion comes from intro.txt.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from jarvis.interpreter import Answer
from jarvis.settings import SETTINGS

_INTRO_PATH = Path(__file__).resolve().parent.parent / "settings" / "intro.txt"
_CFG = SETTINGS.get("identity", {})

# Exactly what F2's wake-word layer will hand the interpreter once built:
# the command captured *after* "Hey JARVIS", not including the wake
# phrase itself (F2's own design -- the wake word is consumed before the
# command is ever captured). Typed commands work the same way.
INTRO_COMMAND_PHRASE = "introduce yourself"

# The router's templates (intents.toml) -> which identity answer.
IDENTITY_COMMANDS = {
    "what does your name stand for": "stands_for",
    "say who you are": "who",
    "say who made you": "creator",
    "say what you can do": "abilities",
}


def load_intro_text() -> str:
    """Read the locked text fresh from disk every time -- never cached
    at import time -- so a test can prove the file, not a frozen copy of
    it, is the single source of truth."""
    return _INTRO_PATH.read_text(encoding="utf-8").strip()


def name_expansion() -> str:
    """"Just A Rather Very Intelligent System", as the intro says it."""
    found = re.search(r"stands for ([^.]+)\.", load_intro_text())
    return found.group(1).strip() if found else ""


def creator() -> str:
    """Who made her ([identity] creator)."""
    return str(_CFG.get("creator", SETTINGS["user"]["name"]))


def identity_prompt_rule() -> str:
    """For the online system prompt: the exact expansion and creator."""
    return str(_CFG.get("prompt_rule", "")).format(expansion=name_expansion(), creator=creator())


def _sentences(text: str) -> list:
    return [s for s in re.split(r"(?<=[.?!])\s+", text) if s]


def identity_answer(kind: str) -> str:
    """The local reply for one identity question: 'stands_for', 'creator', 'abilities' or 'who'."""
    if kind == "stands_for":
        return str(_CFG["stands_for_reply"]).format(expansion=name_expansion())
    if kind == "creator":
        return str(_CFG["creator_reply"]).format(creator=creator())
    if kind == "abilities":
        return str(_CFG["abilities_reply"])
    start = int(_CFG.get("who_from_sentence", 1)) - 1
    return " ".join(_sentences(load_intro_text())[start:start + int(_CFG.get("who_sentence_count", 2))])


def intro_matcher(normalized_text: str) -> Optional[Answer]:
    """F1: the locked intro for 'introduce yourself', or an identity answer for the router's identity
    commands."""
    if normalized_text == INTRO_COMMAND_PHRASE:
        return Answer(load_intro_text())
    kind = IDENTITY_COMMANDS.get(normalized_text)
    return Answer(identity_answer(kind)) if kind else None
