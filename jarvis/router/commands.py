"""Wires the intent router into the interpreter.

- `clarify_pick` goes first, with the other picks: the answer to "Did you
  mean ...?" ("yes", "the first", "no").
- `intent_router` goes after every fixed matcher and just before the online
  conversation. What it decides is rewritten into a fixed command and run
  through the interpreter again (without the router and the conversation),
  so every action still goes through the controller, policy and trust
  levels exactly as if he had said the fixed phrase.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional, Tuple

from jarvis.router.router import GENERAL, Clarifier, IntentRouter, Route, classifier_prompt, validate_classification, words_of

logger = logging.getLogger(__name__)

SKIP_WHEN_ROUTED = frozenset({"intent_router", "conversation"})

# classify(words, system prompt, max tokens) -> (the reply text or None, the AnswerResult)
Classify = Callable[[str, str, int], Tuple[Optional[str], object]]


class RouterCommands:
    """Connects the intent router to the interpreter: a clear local route is run as its template command, a
    close call asks 'Did you mean ...?', and only then the online classifier may name an intent."""
    def __init__(self, classify: Optional[Classify] = None, router: Optional[IntentRouter] = None) -> None:
        self.router = router or IntentRouter()
        self._classify = classify
        self._interpreter = None
        self._orchestrator = None
        self._local = threading.local()
        self.clarifier = Clarifier(run=self._run_command)

    # -- registration ------------------------------------------------------------------------------

    def register_pick(self, interpreter, orchestrator) -> None:
        """Register the answer to 'Did you mean ...?' first of all matchers."""
        self.clarifier.slot = getattr(orchestrator, "picks", None)
        interpreter.register("clarify_pick", self.clarifier.matcher)

    def register(self, interpreter, orchestrator) -> None:
        """Register the router after every fixed matcher and before the online conversation."""
        self._interpreter, self._orchestrator = interpreter, orchestrator
        interpreter.register("intent_router", self.matcher, raw=True)

    # -- matching ------------------------------------------------------------------------------------

    def matcher(self, raw: str):
        """Route `raw` (his words as said). Returns the routed command's result, a 'Did you mean' question,
        or None for the online answer."""
        if getattr(self._local, "busy", False):
            return None
        from jarvis.actions.command_text import clean_command, extract_command, split_glued

        unglued = split_glued(raw)
        command = extract_command(unglued)
        cleaned = clean_command(command if command is not None else unglued)
        verdict, routes = self.router.decide(cleaned)
        if verdict == "none":
            verdict, routes = self.router.decide(unglued, about_me_only=True)
        if verdict == "run":
            return self._run(routes[0])
        if verdict == "ask":
            self._report("understood as: not sure -- asking")
            return self.clarifier.ask([(self._name(r), r.command) for r in routes])
        return self._classified(raw, cleaned)

    def _classified(self, raw: str, cleaned: str):
        """Step 3: the online classifier, only for words that look like a
        local command at all, never for anything sensitive."""
        from jarvis.memory.sensitive import sensitive_type

        if self._classify is None or not self.router.looks_local(cleaned) or sensitive_type(raw) is not None:
            return None
        if self.router.is_question(cleaned) or self.router.is_plain_question(cleaned):
            return None  # "how do I turn the volume up", "what is photosynthesis": answered, never classified
        cfg = self.router.cfg
        # R3: these words go online too -- shown before they are
        # sent, and the destination kept in the record (it was missing:
        # debug.log said online=None for a classified command).
        self._report(f'checking what that means online, sending "{raw}"')
        try:
            reply, result = self._classify(raw, classifier_prompt(self.router.intents), int(cfg["classifier_max_tokens"]))
        except Exception:
            logger.exception("Intent classifier failed; treating it as a question")
            return None
        if getattr(result, "went_online", False) and self._orchestrator is not None:
            self._orchestrator.record_online_destination(f"Groq, intent classifier; usage {getattr(result, 'tokens', 0)}")
        parsed = validate_classification(reply or "", self.router.intents)
        if parsed is None or parsed[0] == GENERAL:
            self._report("understood as: a general question")
            return None
        name, slots, confidence = parsed
        intent = next(i for i in self.router.intents if i.name == name)
        if self.router.ruled_out(intent, cleaned):
            self._report("understood as: a general question")
            return None
        slot = slots.get(intent.slot) or next(iter(slots.values()), "") if intent.slot else ""
        if intent.slot_required and not slot:
            return None
        route = Route(intent, confidence, slot, self.router._command(intent, slot, words_of(cleaned)))
        if confidence >= float(cfg["run_confidence"]) and self.router.has_cue(intent, cleaned):
            return self._run(route)
        if confidence >= float(cfg["run_confidence"]):
            self._report("understood as: not sure (none of its words were said) -- asking")
            return self.clarifier.ask([(self._name(route), route.command)])
        if confidence >= float(cfg["ask_confidence"]):
            self._report("understood as: not sure -- asking")
            return self.clarifier.ask([(self._name(route), route.command)])
        self._report("understood as: a general question")
        return None

    # -- running -------------------------------------------------------------------------------------

    def _run(self, route: Route):
        label = route.intent.name.replace("_", " ")
        self._report(f"understood as: {label}" + (f" ({route.slot})" if route.slot else ""))
        result = self._run_command(route.command)
        if result is None:
            self._report(f"no local command for: {route.command}")
        return result

    def _run_command(self, command: str):
        if self._interpreter is None:
            return None
        self._local.busy = True
        try:
            return self._interpreter.interpret(command, skip=SKIP_WHEN_ROUTED)
        finally:
            self._local.busy = False

    @staticmethod
    def _name(route: Route) -> str:
        """How she names an option: the command itself when it carries his
        words ("open claude"), else the intent's plain name."""
        return f"'{route.command}'" if route.slot else route.intent.say

    def _report(self, step: str) -> None:
        if self._orchestrator is not None:
            self._orchestrator.report_step(step)
