"""No false claims. An online answer can only talk:
anything JARVIS does (sleep, open, close, search, remember …) is done by
her own local commands, recorded and verified (R12, R13). On a
typed "got to sleep" went online and Groq answered "Going to sleep now.
Good night." -- while she stayed awake.

Two guards: a line in every online system prompt ([claims] prompt_rule),
and this local check on every Groq answer. If the answer claims an action
in the first person ([[claims.rules]], regular expressions), the whole
answer is replaced with "I can't do that from here. Try saying '<the
command that really does it>'." -- or "I can't do that yet." when JARVIS
has no such command.

Pure text, no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Mapping, Optional, Tuple

_TRAILING = re.compile(r"[\s.!?,;:]+$")
# A claim of what she CAN do, as opposed to what she did, is doing or will do.
_CAPABILITY = re.compile(r"^(?:(?:sure|okay|ok|of course|absolutely|yes)[,!.]?\s+)?(?:i\s+can|i\s+could|i'?m\s+able\s+to|i\s+am\s+able\s+to)\b",
                         re.IGNORECASE)


@dataclass(frozen=True)
class Claim:
    """An action an online answer claimed to do ('Opening Chrome'), and what she says instead."""
    name: str  # which rule matched ("sleep", "open", …)
    said: str  # the words that made the claim
    replacement: str  # what she says instead


class ClaimCheck:
    """Checks an online answer for claims of actions it cannot do (R19), and replaces them with the honest
    local line."""
    def __init__(self, cfg: Mapping) -> None:
        self._with_command = str(cfg["reply_with_command"])
        self._not_built = str(cfg["reply_not_built"])
        self._no_command = str(cfg["reply_no_command"])
        self._settings = {str(k).lower(): str(v) for k, v in cfg.get("settings_commands", {}).items()}
        self._rules: List[Tuple[str, re.Pattern, str, str, Optional[re.Pattern]]] = [
            (str(r["name"]), re.compile(str(r["pattern"]), re.IGNORECASE | re.MULTILINE), str(r.get("command", "")),
             str(r.get("reply", "")), re.compile(str(r["when_asked"]), re.IGNORECASE) if r.get("when_asked") else None)
            for r in cfg["rules"]
        ]

    def find(self, answer: str, question: str, live: bool = False) -> Optional[Claim]:
        """The first false claim in `answer`, or None. A live answer really
        did search, so the search rule is skipped for it. A rule with
        `when_asked` applies only when the question matches it (
        "where is the cap" -> "The cap is on the left" is a claim to have
        seen it; "which side do Australians drive on" is not)."""
        for name, pattern, command, reply, when_asked in self._rules:
            if live and name == "search":
                continue
            if when_asked is not None and when_asked.search(question or "") is None:
                continue
            match = pattern.search(answer)
            if match is None:
                continue
            return Claim(name, match.group(0).strip(), reply or self._reply(command, match, question))
        return None

    def keep_information(self, answer: str, question: str, live: bool = False) -> Optional[str]:
        """("Explain how well can work and detail" lost its whole
        answer to "I can open installed apps"): a sentence that only says she
        CAN do something is dropped and the rest is kept. Returns the kept
        text, or None when the answer claims she did / is doing / will do an
        action, or nothing else is left -- then the whole answer is replaced."""
        from jarvis.speech.tts import split_sentences

        sentences = split_sentences(answer)
        kept, dropped = [], False
        for sentence in sentences:
            claim = self.find(sentence, question, live)
            if claim is None:
                kept.append(sentence.strip())
            elif _CAPABILITY.match(claim.said):
                dropped = True
            else:
                return None
        return " ".join(kept) if dropped and kept else None

    def _reply(self, command: str, match: re.Match, question: str) -> str:
        if command == "@settings":
            return self._settings_reply(match.string, question)
        if not command:
            return self._not_built
        found = match.groupdict().get("object") or ""
        if "{object}" in command:
            if not found.strip():
                return self._not_built  # an open claim with nothing named: no command to suggest
            # The model's own app name is not trusted ("I can open
            # OneNote" for "... OpenNote Pad"). Only a known app or site named
            # in HIS words is suggested; otherwise no command at all.
            from jarvis.actions.command_text import named_app_in

            named = named_app_in(question)
            if named is None:
                return self._no_command
            return self._with_command.format(command=command.format(object=named, question=""))
        filled = command.format(object="", question=_TRAILING.sub("", question).strip().lower())
        return self._with_command.format(command=" ".join(filled.split()))

    def _settings_reply(self, answer: str, question: str) -> str:
        """A volume/brightness/Wi-Fi claim names the local
        command that really does it (F22 is built); anything else ("I will
        adjust the reminder") gets "I can't do that from here."."""
        said = f"{question} {answer}".lower()
        for word, command in self._settings.items():
            if re.search(rf"\b{re.escape(word)}\b", said):
                return self._with_command.format(command=command)
        return self._no_command
