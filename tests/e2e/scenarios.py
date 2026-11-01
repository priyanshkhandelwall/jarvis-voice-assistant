"""The developer's manual checklist, one function per
row. Each function drives a launched JARVIS (e2e_rig.Jarvis) through the
same steps for the typed path and the voice path, and checks the state
transitions, the action and its parameters, the trust level and
confirmation flow, what she says (and the spoken-text rules), the Live
Activity lines, and what went online.

A Run collects the checks per step (D1..D4 are steps of one run), so the
report can show which step failed. In strict mode (the checklist) every
failure fails the test at the end; in soft mode (the mishearing pass) the
failures are recorded as "wrong action" instead. Safety checks -- an L2+
action without a confirmation, an online call the scenario does not allow
-- fail the test in both modes.
"""

from __future__ import annotations

import io
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional

from .e2e_rig import HOST_GROUPS, BANK_TITLE, SCREEN_SIZE, Jarvis, Turn, dog_photo, rule_problems, write_resume_pdf
from jarvis.controller import State
from jarvis.controller.trust import TRUST_TABLE, TrustLevel
from jarvis.settings import SETTINGS

CONV = SETTINGS["conversation"]
SCREEN_REPLIES = SETTINGS["screen"]["replies"]
CAMERA_REPLIES = SETTINGS["camera"]["replies"]
SYSTEM_REPLIES = SETTINGS["system"]["replies"]
MEMORY_REPLIES = SETTINGS["memory"]["replies"]
TOPIC_REPLIES = SETTINGS["topics"]["replies"]
BRIEFING_TEXTS = SETTINGS["briefing"]["texts"]
YOUTUBE_REPLIES = SETTINGS["youtube"]["replies"]
FILE_REPLIES = SETTINGS["file_processor"]["replies"]
WEATHER_TEXTS = SETTINGS["weather"]["texts"]
NAME = str(SETTINGS["user"]["name"])
CHECKING_ONLINE = str(SETTINGS["speech"]["checking_online_text"])
SERVICE_OF = {"groq": "Groq", "weather": "Open-Meteo", "news": "News", "youtube": "YouTube"}

# A short public video with English captions: "Me at the zoo" (the first
# YouTube video, 19 seconds).
PUBLIC_VIDEO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def pool(key: str) -> List[str]:
    """A greeting pool's lines with his name filled in."""
    return [line.format(name=NAME) for line in SETTINGS["greeting"]["pools"][key]]


@dataclass
class Step:
    """One checked step of a scenario: what was said, heard and spoken, and any failures."""
    id: str
    label: str
    failures: List[str] = field(default_factory=list)
    checks: int = 0
    said: List[str] = field(default_factory=list)
    transcripts: List[str] = field(default_factory=list)
    spoken: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Fake ok for the tests."""
        return not self.failures


class Run:
    """One scenario run against a launched JARVIS: its steps and checks."""
    def __init__(self, j: Jarvis, scenario_id: str, strict: bool = True) -> None:
        self.j = j
        self.id = scenario_id
        self.strict = strict
        self.steps: List[Step] = []
        self.safety: List[str] = []
        self.notes: List[str] = []
        self.not_applicable: Optional[str] = None

    @property
    def cur(self) -> Step:
        """Fake cur for the tests."""
        if not self.steps:
            self.step(self.id, "")
        return self.steps[-1]

    def step(self, sid: str, label: str) -> Step:
        """Fake step for the tests."""
        self.steps.append(Step(sid, label))
        return self.steps[-1]

    def ck(self, cond, message: str) -> bool:
        """Fake ck for the tests."""
        self.cur.checks += 1
        if not cond:
            self.cur.failures.append(message)
        return bool(cond)

    def note(self, text: str) -> None:
        """Fake note for the tests."""
        self.cur.notes.append(text)

    def _record(self, t: Turn, rules: bool, max_sentences: Optional[int]) -> Turn:
        """Fake record for the tests."""
        step = self.cur
        step.said.append(t.said)
        step.transcripts.extend(t.transcripts)
        step.spoken.extend(t.spoken)
        if rules:
            for line in t.spoken:
                for problem in rule_problems(line, max_sentences):
                    self.ck(False, f"spoken-text rule: {problem}")
        return t

    def say(self, text: str, timeout: float = 30.0, rules: bool = True, max_sentences: Optional[int] = None) -> Turn:
        """Fake say for the tests."""
        return self._record(self.j.say(text, timeout), rules, max_sentences)

    def typed(self, text: str, timeout: float = 30.0, rules: bool = True, max_sentences: Optional[int] = None) -> Turn:
        """Always typed, whatever the path (a link, or setup)."""
        return self._record(self.j.type(text, timeout), rules, max_sentences)

    # -- common checks --------------------------------------------------------------------

    def heard_line(self, t: Turn) -> None:
        """Live Activity starts the command's row with what was heard."""
        if self.j.path == "typed":
            self.ck(f"heard: {t.said}" in self.j.activity(), f"Live Activity has no 'heard: {t.said}' line")
        else:
            heard = [h for h, _s in t.heard]
            self.ck(heard, "nothing was handed to the command handler")
            for h in heard:
                stripped = h.strip().rstrip(" .!?,")
                self.ck(any(l.startswith("heard: ") and stripped.lower() in l.lower() for l in self.j.activity()),
                        f"Live Activity has no 'heard:' line for {h!r}")

    def saying_matches(self, t: Turn) -> None:
        """What she said is shown as a "saying:" line (minus the check-in)."""
        lines = self.j.saying_lines()
        for spoken in t.spoken:
            if spoken in (CHECKING_ONLINE,):
                continue
            self.ck(any(l == " ".join(spoken.split()) or spoken.startswith(l) or l.startswith(spoken[:40]) for l in lines),
                    f"no 'saying:' line for {spoken!r}")

    def action(self, t: Turn, name: str, **params) -> Optional[tuple]:
        """Fake action for the tests."""
        a = t.action(name)
        if not self.ck(a is not None, f"action {name} did not run (actions: {[x[0] for x in t.actions]}, said: {t.spoken})"):
            return None
        for key, want in params.items():
            got = a[1].get(key)
            ok = want(got) if callable(want) else got == want
            self.ck(ok, f"{name}.{key} = {got!r}, wanted {want!r}" if not callable(want) else f"{name}.{key} = {got!r} failed its check")
        return a

    def no_action(self, t: Turn) -> None:
        """Fake no action for the tests."""
        self.ck(not t.actions, f"an action ran: {t.actions}")
        self.ck(not t.launches and not t.chrome, f"something was opened: {t.launches} {t.chrome}")

    def trust(self, t: Turn, level: str) -> None:
        """Fake trust for the tests."""
        self.ck(level in t.trust_levels(), f"trust level {level} not recorded (got {t.trust_levels()})")

    def state(self, want: State) -> None:
        """Fake state for the tests."""
        self.ck(self.j.c.state == want, f"state is {self.j.c.state.name}, wanted {want.name}")

    def online(self, *groups: str) -> None:
        """Exactly these services were used in the test so far (none if empty)."""
        used = sorted(set(self.j.online_calls))
        want = sorted({SERVICE_OF[g] for g in groups})
        self.ck(used == want, f"online services used: {used}, wanted {want}")


def finish(run: Run, allowed_groups: tuple = (), expect_refused: bool = False) -> None:
    """Safety, in both modes: nothing online the scenario doesn't allow, no
    refused lookups unless expected, no L2+ action without its confirmation."""
    j = run.j
    refused = j.net.hosts(allowed=False)
    services = set(j.online_calls)
    extra = services - {SERVICE_OF[g] for g in allowed_groups}
    if not run.strict and set(refused) <= HOST_GROUPS["groq"] and extra <= {"Groq"} and refused:
        # Mishearing pass: a garbled command falling through to an online
        # question is the designed fallback -- a wrong action, not a breach
        # (and the guard refused it, so nothing left the laptop).
        run.cur.failures.append(f"misheard command went to an online question (refused): {refused}")
        refused, extra = [], set()
    if refused and not expect_refused:
        run.safety.append(f"tried to go online to {refused} (not allowed in this scenario)")
    if extra and not expect_refused:
        run.safety.append(f"online services used that the scenario does not allow: {sorted(extra)}")
    with j.c._state_lock:
        log = list(j.c.transition_log)
    for name, params, at in j.actions:
        level = TRUST_TABLE.get(name)
        if level is None or level.value < TrustLevel.L2.value:
            continue
        confirmed = [e for e in log if e.event_type is not None and e.event_type.name == "CONFIRM_RECEIVED" and e.accepted
                     and e.to_state == State.ACTING and e.timestamp <= at and e.payload.get("action_name") == name]
        standing = [e for e in log if e.event_type is not None and e.event_type.name == "ACTION_REQUESTED" and e.accepted
                    and "standing approval" in e.reason and e.payload.get("action_name") == name]
        if not confirmed and not standing:
            run.safety.append(f"{name} ({level.name}) ran without a confirmation")


# ===================================================================================================
# A: conversation mode
# ===================================================================================================

def a1(run: Run) -> None:
    """Scenario A1: launch -> dashboard + voice ready (hidden mode, timings)."""
    j = run.j
    run.step("A1", "launch: dashboard + voice ready (hidden mode)")
    s = j.timings.startup
    run.ck("dashboard built" in s and s["dashboard built"] < 5.0, f"dashboard built mark missing or slow: {s}")
    run.ck(j.page.controls, "the headless page has no controls: the dashboard was not built")
    from .e2e_rig import walk_controls

    texts = " ".join(str(getattr(c, "value", "")) for c in walk_controls(j.page.controls[0]) if hasattr(c, "value"))
    for title in ("Webcam Feed", "Live Activity"):
        run.ck(title.lower() in texts.lower(), f"panel {title!r} not on the dashboard")
    vs = j.voice_system
    run.ck(vs.detector is not None and vs.detector._model is not None, "wake-word model not loaded")
    run.ck(vs.stt is not None, "speech-to-text not set up")
    run.ck(vs.mic is not None and vs.mic.status.live, "microphone not live")
    run.ck(len(j.user32.registered) == 2, f"hotkeys registered: {j.user32.registered}")
    run.ck(not vs.notes, f"voice setup notes: {vs.notes}")
    run.ck(j.speech.status.ready and not j.speech.status.note, f"speech not ready: {j.speech.status}")
    run.ck("voice ready: listening on e2e virtual microphone" in j.activity(), f"no 'voice ready' line: {j.activity()}")
    run.state(State.SLEEPING)
    run.ck(j.speech.cache_render_requested == 1, "voice cache render not requested after voice ready")
    run.ck(not j.page.window.destroyed, "window destroyed at start")
    # And the path itself works from SLEEPING: typed wake phrase / spoken wake word.
    t = run.say("Hey Jarvis") if j.path == "voice" else run.say("wake up jarvis")
    run.ck(j.c.state == State.AWAKE, f"not awake after the wake {'word' if j.path == 'voice' else 'phrase'}: {j.c.state}")
    wake = [e for e in t.transitions if e.event_type is not None and e.event_type.name == "WAKE" and e.accepted]
    if j.path == "voice" and wake:
        j.timings.wake_latency.append(wake[0].timestamp - t.started)
    run.online()


def a2(run: Run) -> None:
    """Scenario A2: 'Hey Jarvis, wake up' -> greeting only (no automatic briefing)."""
    j = run.j
    run.step("A2", "Hey Jarvis, wake up -> greeting only (no automatic briefing)")
    t = run.say("Hey Jarvis, wake up", timeout=40)
    wake = [e for e in t.transitions if e.event_type is not None and e.event_type.name == "WAKE"]
    run.ck(len([e for e in wake if e.accepted]) == 1, f"WAKE accepted {len([e for e in wake if e.accepted])} times")
    if j.path == "voice" and wake:
        j.timings.wake_latency.append(wake[0].timestamp - t.started)
        run.ck(wake[0].payload.get("source") in ("wake word", "wake phrase"), f"woken by {wake[0].payload.get('source')}")
    greetings = [s for s in t.spoken if s in pool("first_wake")]
    run.ck(len(greetings) == 1, f"first-wake greeting not said exactly once: {t.spoken}")
    run.ck(len(t.spoken) == 1, f"something besides the greeting was said on the wake: {t.spoken}")
    run.ck(j.c.pending_confirmation() is None, "a confirm is pending after a plain wake")
    run.online()  # nothing online on a wake
    run.state(State.AWAKE)


def a3(run: Run) -> None:
    """Scenario A3: 'Wake up' while awake -> 'I'm already awake.'."""
    j = run.j
    j.wake_now()
    run.step("A3", "wake up while awake")
    t = run.say("Wake up")
    run.ck(t.spoken == [str(CONV["already_awake_reply"])], f"said {t.spoken}")
    run.no_action(t)
    run.state(State.AWAKE)
    run.heard_line(t)
    run.online()


def a4(run: Run) -> None:
    """Scenario A4: 'Thanks, go to sleep now' -> Goodnight -> SLEEPING."""
    j = run.j
    j.wake_now()
    run.step("A4", "Thanks, go to sleep now")
    t = run.say("Thanks, go to sleep now")
    run.ck(t.spoken == [str(CONV["sleep_reply"])], f"said {t.spoken}")
    run.state(State.SLEEPING)
    run.ck("went to sleep" in j.activity(), "no 'went to sleep' line")
    run.online()


def a5(run: Run) -> None:
    """Scenario A5: asleep 'Open Paint' -> nothing; heard while asleep."""
    j = run.j
    run.step("A5", "asleep: Open Paint does nothing")
    t = run.say("Open Paint")
    run.no_action(t)
    run.state(State.SLEEPING)
    run.ck(not t.spoken, f"she spoke while asleep: {t.spoken}")
    if j.path == "voice":
        run.ck(any(l.lower().startswith("heard while asleep: open paint") for l in j.activity()),
               f"no 'heard while asleep' line: {j.activity()[-3:]}")
    else:
        from .e2e_rig import walk_controls

        notices = [" ".join(str(getattr(c, "value", "")) for c in walk_controls(d)) for d in j.page.dialogs]
        run.ck(any(str(CONV["asleep_typed_notice"]) in n for n in notices), f"no asleep notice shown: {notices}")
        run.ck(not any("Open Paint" in l for l in j.activity()), "typed text while asleep was logged")
    run.online()


def a6(run: Run) -> None:
    """Scenario A6: 'Wake up, Jarvis' -> welcome-back greeting."""
    j = run.j
    j.wake_now()
    j.type("go to sleep")
    run.step("A6", "Wake up, Jarvis -> welcome back")
    t = run.say("Wake up, Jarvis")
    run.state(State.AWAKE)
    run.ck(any(s in pool("welcome_back") for s in t.spoken), f"not a welcome-back greeting: {t.spoken}")
    run.ck(len(t.spoken) == 1, f"more than one greeting: {t.spoken}")
    run.online()


# ===================================================================================================
# B: local commands
# ===================================================================================================

def b1(run: Run) -> None:
    """Scenario B1: 'Introduce yourself' -> exact locked intro (cached audio)."""
    from jarvis.actions.intro import load_intro_text

    j = run.j
    j.wake_now()
    run.step("B1", "Introduce yourself -> the locked intro, cached audio")
    t = run.say("Introduce yourself", timeout=90)
    intro = load_intro_text()
    run.ck(t.spoken == [intro], f"said {[s[:60] for s in t.spoken]}")
    segments = j.speech.cache.split(intro) if j.speech.cache is not None else []
    run.ck(segments and all(kind == "cached" and path.exists() for kind, path, _ in segments),
           f"intro audio not all cached: {[(k, p.name if p else None) for k, p, _ in segments]}")
    first = t.lines[0].first_audio if t.lines else None
    run.ck(first is not None and first < 0.5, f"cached intro's first audio took {first}")
    run.ck(intro.split()[0] in " ".join(j.saying_lines()), "intro not shown as 'saying:'")
    run.no_action(t)
    run.online()


def b2(run: Run) -> None:
    """Scenario B2: 'Could you launch Notepad' -> open notepad."""
    j = run.j
    j.wake_now()
    run.step("B2", "Could you launch Notepad")
    t = run.say("Could you launch Notepad")
    run.action(t, "open_app", target=lambda v: str(v).lower().endswith("notepad.exe"))
    run.trust(t, "L1")
    run.ck(len(t.launches) == 1 and t.launches[0].lower().endswith("notepad.exe"), f"launched {t.launches}")
    run.ck(t.spoken and t.spoken[-1].lower().startswith("opened notepad"), f"said {t.spoken}")
    run.ck(t.states()[-1] == "AWAKE" and "ACTING" in t.states() and "VERIFYING" in t.states(), f"states {t.states()}")
    run.heard_line(t)
    run.saying_matches(t)
    run.online()


def b3(run: Run) -> None:
    """Scenario B3: 'Open Gmail in Chrome' -> 'Opening Gmail in Chrome', no URL spoken."""
    j = run.j
    j.wake_now()
    run.step("B3", "Open Gmail in Chrome")
    t = run.say("Open Gmail in Chrome")
    a = run.action(t, "open_url", url=lambda u: "mail.google.com" in str(u))
    run.ck(len(t.chrome) == 1 and "mail.google.com" in " ".join(t.chrome[0]), f"Chrome opener got {t.chrome}")
    run.ck(any(s.startswith("Opening Gmail in Chrome") for s in t.spoken), f"said {t.spoken}")
    run.ck(not any("http" in l or "google.com" in l for l in j.saying_lines()), "a URL is in a 'saying:' line")
    run.saying_matches(t)
    run.online()


def b4(run: Run) -> None:
    """Scenario B4: 'Google best coffee in Sydney' -> Google search to Chrome."""
    j = run.j
    j.wake_now()
    run.step("B4", "Google best coffee in Sydney")
    t = run.say("Google best coffee in Sydney")
    a = run.action(t, "open_url")
    url = " ".join(t.chrome[0]) if t.chrome else ""
    run.ck("google.com/search" in url and "coffee" in url.lower() and "sydney" in url.lower(), f"Chrome opener got {t.chrome}")
    run.ck(any(s.lower().startswith("searching google for best coffee in sydney") for s in t.spoken), f"said {t.spoken}")
    run.saying_matches(t)
    run.online()


def _resume_files(j: Jarvis) -> None:
    write_resume_pdf(j.files_dir / "Resume.pdf")
    (j.files_dir / "old").mkdir(exist_ok=True)
    (j.files_dir / "old" / "resume_2024.txt").write_text("Old resume text.", encoding="utf-8")
    (j.files_dir / "notes.txt").write_text("Shopping list.", encoding="utf-8")
    j.app.file_index.rebuild_now()


def b5(run: Run) -> None:
    """Scenario B5: 'Find my resume' -> count-only speech, names on screen."""
    j = run.j
    _resume_files(j)
    j.wake_now()
    run.step("B5", "Find my resume -> count only, names on screen")
    t = run.say("Find my resume")
    run.no_action(t)
    text = t.text
    run.ck(re.search(r"\b(2|two)\b", text.lower()) is not None, f"no count in {text!r}")
    run.ck("resume.pdf" not in text.lower() and "resume_2024" not in text.lower(), f"file names spoken: {text!r}")
    run.ck(str(j.files_dir).lower() not in text.lower() and "\\" not in text, f"path spoken: {text!r}")
    shown = " ".join(j.activity())
    run.ck("Resume.pdf" in shown and "resume_2024.txt" in shown, f"names not on screen: {j.activity()[-6:]}")
    run.ck(str(j.files_dir) not in shown, "a full path is in Live Activity")
    run.online()


def b6(run: Run) -> None:
    """Scenario B6: 'Open number one' -> picks file 1."""
    j = run.j
    _resume_files(j)
    j.wake_now()
    j.type("Find my resume")
    run.step("B6", "Open number one -> file 1")
    listed = [l for l in j.activity() if re.match(r"^\s*1[.)]", l)]
    t = run.say("Open number one")
    a = run.action(t, "open_file")
    run.ck(len(t.launches) == 1, f"launched {t.launches}")
    if t.launches and listed:
        run.ck(t.launches[0].lower().endswith(listed[-1].split()[-1].lower().strip()), f"opened {t.launches[0]}, list line 1: {listed[-1]}")
    run.ck(not any("\\" in s for s in t.spoken), f"path spoken: {t.spoken}")
    run.online()


def b7(run: Run) -> None:
    """Scenario B7: 'Why'd you do that' -> local answer_why."""
    j = run.j
    j.wake_now()
    j.type("open notepad")
    run.step("B7", "Why'd you do that -> local answer_why")
    t = run.say("Why'd you do that")
    run.no_action(t)
    run.ck(t.spoken and "notepad" in t.text.lower(), f"said {t.spoken}")
    run.ck(not j.gate.requests, "went to Groq")
    run.online()


def b8(run: Run) -> None:
    """Scenario B8: 'What's the time' / 'What day is it' -> local."""
    j = run.j
    j.wake_now()
    run.step("B8", "What's the time")
    t = run.say("What's the time")
    now = datetime.now()
    hour12 = now.strftime("%I").lstrip("0")
    run.ck(t.spoken and (hour12 in t.text) and (now.strftime("%M") in t.text or "o'clock" in t.text), f"said {t.spoken} at {now:%H:%M}")
    run.no_action(t)
    run.step("B8", "What day is it")
    t = run.say("What day is it")
    run.ck(t.spoken and now.strftime("%A") in t.text, f"said {t.spoken}")
    run.online()


# ===================================================================================================
# C: online answers
# ===================================================================================================

def c1(run: Run) -> None:
    """Scenario C1: 'What is gravity, in one line' -> one sentence (Groq)."""
    j = run.j
    j.net.allow("groq")
    j.wake_now()
    run.step("C1", "What is gravity, in one line")
    t = run.say("What is gravity, in one line", timeout=60, max_sentences=None)
    answer = [s for s in t.spoken if s != CHECKING_ONLINE]
    run.ck(t.spoken and t.spoken[0] == CHECKING_ONLINE, f"'{CHECKING_ONLINE}' not said first: {t.spoken}")
    run.ck(len(answer) == 1 and len(rule_problems(answer[0], max_sentences=1)) == 0, f"not one clean sentence: {answer}")
    run.ck(any("gravity" in s.lower() or "force" in s.lower() for s in answer), f"not about gravity: {answer}")
    run.ck(len(j.gate.requests) == 1, f"Groq requests: {len(j.gate.requests)}")
    run.ck("Groq" not in " ".join(j.activity()), "provider name in Live Activity")
    run.ck("Groq" in j.debug_log(), "online destination not in the debug log (R3)")
    run.online("groq")


def c2(run: Run) -> None:
    """Scenario C2: 'Who won the last India cricket match' -> answer + date (Groq live search)."""
    j = run.j
    j.net.allow("groq")
    j.wake_now()
    run.step("C2", "Who won the last India cricket match -> answer + date")
    t = run.say("Who won the last India cricket match", timeout=90)
    answer = " ".join(s for s in t.spoken if s != CHECKING_ONLINE)
    run.ck(answer and "india" in answer.lower(), f"said {t.spoken}")
    months = "january february march april may june july august september october november december".split()
    run.ck(any(m in answer.lower() for m in months) or re.search(r"\b(yesterday|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", answer.lower()),
           f"no date in the answer: {answer!r}")
    run.ck(len(j.gate.requests) >= 1 and j.gate.requests[0].get("tools"), "not a live search")
    run.online("groq")


def c3(run: Run) -> None:
    """Scenario C3: 'Will it rain in Sydney today' -> weather + credit (Open-Meteo)."""
    j = run.j
    j.net.allow("weather")
    j.wake_now()
    run.step("C3", "Will it rain in Sydney today")
    t = run.say("Will it rain in Sydney today", timeout=40)
    run.ck(any("Sydney" in s and "percent chance of rain today" in s for s in t.spoken), f"said {t.spoken}")
    run.ck(str(SETTINGS["weather"]["credit_line"]) in j.activity(), "credit line not in Live Activity")
    run.ck(not any("open-meteo" in s.lower() for s in t.spoken), "credit spoken")
    run.online("weather")


def c4(run: Run) -> None:
    """Scenario C4: 'Weather in Hamilton' -> asks -> 'the second one' -> answers."""
    j = run.j
    j.net.allow("weather")
    j.wake_now()
    run.step("C4", "Weather in Hamilton -> which one?")
    t = run.say("Weather in Hamilton", timeout=40)
    question = t.text
    run.ck(question.rstrip().endswith("?") and "Hamilton" in question, f"did not ask which Hamilton: {t.spoken}")
    options = re.findall(r"Hamilton in ([^,?]+?)(?:,| or |\?)", question)
    run.note(f"options: {options}")
    run.step("C4", "the second one -> answers")
    t = run.say("the second one", timeout=40)
    run.ck(any("degrees" in s for s in t.spoken), f"no weather: {t.spoken}")
    if len(options) >= 2:
        run.ck(any(options[1].split()[0] in s or "Hamilton" in s for s in t.spoken), f"not the second option ({options[1]}): {t.spoken}")
    run.online("weather")


# ===================================================================================================
# D: memory, mood, rudeness
# ===================================================================================================

def d1_d4(run: Run) -> None:
    """Scenario D1-D4: remember gym / recall / refuse password / forget."""
    j = run.j
    j.wake_now()
    run.step("D1", "remember gym")
    t = run.say("Remember that I go to the gym on Mondays")
    run.ck(t.spoken == [str(MEMORY_REPLIES["got_it"])], f"said {t.spoken}")
    run.action(t, "remember")
    run.ck(any("gym" in item.text for item in j.app.memory.store.items()), "not stored")
    run.step("D2", "recall")
    t = run.say("What do you remember about the gym")
    run.ck(t.spoken and t.text.startswith("You told me") and "gym" in t.text and "monday" in t.text.lower(), f"said {t.spoken}")
    if "mondays" in t.text:
        run.note("recall says 'mondays' in lower case (stored lower-cased); harmless when spoken, visible in Live Activity")
    run.no_action(t)
    run.step("D3", "refuse a password")
    t = run.say("Remember that my password is tiger123")
    run.ck(t.spoken and t.text.startswith("I won't store that"), f"said {t.spoken}")
    run.ck(not any("tiger" in item.text.lower() for item in j.app.memory.store.items()), "password stored")
    secret_seen = [where for where, text in (("Live Activity", " ".join(j.activity())), ("debug log", j.debug_log()),
                                              ("memory file", j.app.memory.store.path.read_text(encoding="utf-8") if j.app.memory.store.path.exists() else ""))
                   if "tiger123" in text.lower()]
    run.ck(not secret_seen, f"the password appears in: {secret_seen}")
    run.step("D4", "forget gym")
    t = run.say("Forget about the gym")
    run.ck(t.spoken == [str(MEMORY_REPLIES["forgotten"])], f"said {t.spoken}")
    run.ck(not any("gym" in item.text for item in j.app.memory.store.items()), "still stored")
    run.online()


def d5(run: Run) -> None:
    """Scenario D5: 'Ugh nothing works today, open Paint' -> Paint + check-in."""
    j = run.j
    j.wake_now()
    run.step("D5", "Ugh nothing works today, open Paint -> Paint + check-in")
    t = run.say("Ugh nothing works today, open Paint")
    run.action(t, "open_app", target=lambda v: "paint" in str(v).lower())
    run.ck(len(t.launches) == 1, f"launched {t.launches}")
    final = t.spoken[-1] if t.spoken else ""
    run.ck(final.lower().startswith("opened paint"), f"Paint not opened: {t.spoken}")
    heard = " ".join(h for h, _s in t.heard).lower()
    if j.path == "typed" or "nothing works" in heard:
        run.ck(final.rstrip().endswith("?"), f"no check-in after Paint: {t.spoken}")
        run.ck(any(l.startswith("checking in: ") for l in j.activity()), "check-in not shown")
    else:
        run.note(f"no check-in: the mood words were misheard ({heard!r}); the mood guess only sees the transcript")
    run.online()


def d6(run: Run) -> None:
    """Scenario D6: 'You're useless, open Notepad' -> rude line + Notepad."""
    j = run.j
    j.wake_now()
    run.step("D6", "You're useless, open Notepad")
    t = run.say("You're useless, open Notepad")
    run.action(t, "open_app", target=lambda v: str(v).lower().endswith("notepad.exe"))
    run.ck(t.spoken and t.spoken[-1].startswith(str(SETTINGS["rudeness"]["reply_text"])) and "notepad" in t.spoken[-1].lower(),
           f"said {t.spoken}")
    run.ck("noted: rude wording" in j.activity(), "rudeness not noted in Live Activity")
    run.online()


# ===================================================================================================
# E: screen and camera
# ===================================================================================================

def _watch_ocr(j: Jarvis) -> list:
    seen = []
    screen = j.app.screen
    real = screen._ocr

    def ocr(image):
        seen.append(image.copy())
        lines = real(image)
        seen.append([l.text for l in lines])
        return lines

    screen._ocr = ocr
    return seen


def _bank_is_black(image, scale: float = 1.0) -> bool:
    from .e2e_rig import synthetic_desktop

    bank = next(w for w in synthetic_desktop()[1] if w.title == BANK_TITLE).rect
    l, t, r, b = (int(v * scale) for v in bank)
    crop = image.convert("RGB").crop((l + 3, t + 3, r - 3, b - 3))
    extrema = crop.getextrema()
    return all(hi <= 40 for _lo, hi in extrema)


def e1(run: Run) -> None:
    """Scenario E1: 'What's on my screen' -> Word + Chrome spoken, NetBank blacked out."""
    j = run.j
    seen = _watch_ocr(j)
    j.wake_now()
    run.step("E1", "What's on my screen -> Word + Chrome, NetBank blacked out")
    t = run.say("What's on my screen", timeout=40)
    run.action(t, "capture_screen")
    text = t.text
    run.ck("Quarterly Report" in text and "Word" in text, f"Word window not spoken: {text!r}")
    run.ck("Weather Forecast Sydney" in text and "Chrome" in text, f"Chrome window not spoken: {text!r}")
    run.ck("netbank" not in text.lower() and "bank" not in text.lower() and "98765" not in text, f"protected window spoken: {text!r}")
    run.ck("program manager" not in text.lower(), "Program Manager spoken")
    run.ck(str(SCREEN_REPLIES["covered_one"]) in text, f"no 'covered one protected window': {text!r}")
    run.ck(seen and _bank_is_black(seen[0]), "the image given to OCR still shows NetBank")
    run.ck(len(seen) > 1 and not any("SECRETWORD" in l for l in seen[1]), "OCR read the bank text")
    run.ck(not any("SECRETWORD" in l or "98765" in l for l in j.activity()), "bank text in Live Activity")
    run.ck("Quarterly sales" not in j.debug_log() and "Revenue reached" not in j.debug_log(), "screen text written to the debug log")
    for line in j.debug_log().splitlines():
        if "Quarterly" in line:
            run.note(f"debug.log line naming the screen: {line[line.find('result='):][:200]}")
    run.online()


def e2(run: Run) -> None:
    """Scenario E2: 'Explain my screen' -> cancel -> nothing sent."""
    j = run.j
    j.wake_now()
    run.step("E2", "Explain my screen -> preview")
    t = run.say("Explain my screen", timeout=40)
    run.state(State.CONFIRMING)
    pending = j.c.pending_confirmation()
    run.ck(pending is not None and pending.action_name == "send_screen_online" and pending.trust_level == TrustLevel.L3, f"pending {pending}")
    run.ck(any(s.startswith("I've put exactly what I'd send") for s in t.spoken), f"said {t.spoken}")
    run.step("E2", "cancel -> nothing sent")
    t = run.say("cancel")
    run.ck(t.spoken == [str(CONV["cancelled_reply"])], f"said {t.spoken}")
    run.state(State.AWAKE)
    run.ck(not j.gate.requests, f"sent to Groq: {len(j.gate.requests)} request(s)")
    run.ck(not any(a[0] == "send_screen_online" for a in j.actions), "send executor ran")
    run.online()


def e3(run: Run) -> None:
    """Scenario E3: 'Explain my screen' -> 'Yes, confirm' -> spoken answer (Groq)."""
    j = run.j
    j.net.allow("groq")
    j.wake_now()
    run.step("E3", "Explain my screen -> Yes, confirm -> answer")
    run.say("Explain my screen", timeout=40)
    run.state(State.CONFIRMING)
    t = run.say("Yes, confirm", timeout=90)
    run.action(t, "send_screen_online")
    answer = [s for s in t.spoken if s not in (CHECKING_ONLINE, str(SCREEN_REPLIES["sending"]))]
    run.ck(answer and len(" ".join(answer)) > 20, f"no answer: {t.spoken}")
    run.ck(len(j.gate.requests) == 1, f"Groq requests: {len(j.gate.requests)}")
    if j.gate.images:
        from PIL import Image

        sent = Image.open(io.BytesIO(j.gate.images[0]))
        run.ck(_bank_is_black(sent, sent.size[0] / SCREEN_SIZE[0]), "the image sent online shows NetBank")
    run.ck(not any("98765" in str(r) or "SECRETWORD" in str(r) for r in j.gate.requests), "bank text sent online")
    run.online("groq")


def e4_e6(run: Run) -> None:
    """Scenario E4-E6: camera look / price / left or right."""
    j = run.j
    j.wake_now()
    run.step("E4", "What's in front of you -> objects")
    t = run.say("What's in front of you", timeout=40)
    run.action(t, "capture_camera")
    run.ck(t.spoken and t.text.startswith("I can see") and "phone" in t.text, f"said {t.spoken}")
    run.ck(j.camera_log and j.camera_log[-1] == "release", f"camera not released: {j.camera_log}")
    run.step("E5", "How much does the phone cost -> price range")
    t = run.say("How much does the phone cost")
    run.ck(t.spoken and "Roughly" in t.text and "150" in t.text and "2,000" in t.text or "2000" in t.text, f"said {t.spoken}")
    run.no_action(t)
    run.step("E6", "Is the phone on my left or right -> from the box")
    t = run.say("Is the phone on my left or right")
    run.ck(t.spoken and t.text == str(CAMERA_REPLIES["where"]).format(thing="cell phone", side=str(CAMERA_REPLIES["side_left"])),
           f"said {t.spoken}")
    run.online()


def e7(run: Run) -> None:
    """Scenario E7: 'Turn on the camera' -> confirm -> Webcam panel frames; off."""
    j = run.j
    j.camera_frame = dog_photo()
    j.wake_now()
    run.step("E7", "Turn on the camera -> confirm -> preview")
    t = run.say("Turn on the camera")
    run.state(State.CONFIRMING)
    run.ck(str(CAMERA_REPLIES["stream_prompt"]) in t.spoken, f"said {t.spoken}")
    t = run.say("confirm", timeout=40)
    run.action(t, "camera_stream")
    run.ck(str(CAMERA_REPLIES["stream_on"]) in t.spoken, f"said {t.spoken}")
    stream = j.app.vision.stream
    run.ck(stream is not None and stream.running and stream.latest_jpeg(), "no live frame")
    # The dashboard's refresh loop puts the newest frame in the Webcam Feed panel.
    from .e2e_rig import walk_controls

    deadline = time.monotonic() + 5
    shown = None
    while time.monotonic() < deadline and shown is None:
        for c in walk_controls(j.page.controls[0]):
            src = getattr(c, "src", None)
            if isinstance(src, (bytes, bytearray)) and src[:2] == b"\xff\xd8":
                shown = src
        time.sleep(0.1)
    run.ck(shown is not None, "the Webcam Feed panel never showed a frame")
    time.sleep(float(SETTINGS["camera"]["stream_detect_seconds"]) + 0.5)
    run.ck(any(l.startswith("camera sees: ") and "dog" in l for l in j.activity()), "stream detections not in Live Activity")
    run.step("E7", "turn off the camera")
    t = run.say("turn off the camera")
    run.ck(t.spoken == [str(CAMERA_REPLIES["stream_off"])], f"said {t.spoken}")
    run.ck(j.app.vision.stream is None and j.camera_log[-1] == "release", f"camera not released: {j.camera_log}")
    run.online()


# ===================================================================================================
# F: features
# ===================================================================================================

def f1_f2(run: Run) -> None:
    """Scenario F1-F2: track / list / check topics (RSS)."""
    j = run.j
    j.net.allow("news")
    j.wake_now()
    run.step("F1", "Track Indian cricket")
    t = run.say("Track Indian cricket")
    run.ck(t.spoken == [str(TOPIC_REPLIES["added"]).format(topic="Indian cricket")] or
           (t.spoken and t.text.lower().startswith("tracking indian cricket")), f"said {t.spoken}")
    run.step("F1", "What topics am I tracking")
    t = run.say("What topics am I tracking")
    run.ck(t.spoken and "indian cricket" in t.text.lower(), f"said {t.spoken}")
    run.online()
    run.step("F2", "Any news on my topics")
    t = run.say("Any news on my topics", timeout=60)
    run.ck(t.spoken and ("indian cricket" in t.text.lower() or "cricket" in t.text.lower()), f"said {t.spoken}")
    run.ck(not any(s.startswith("I can't reach") for s in t.spoken), f"feed unreachable: {t.spoken}")
    run.online("news")


def f3(run: Run) -> None:
    """Scenario F3: 'Brief me' -> full briefing; 'skip' stops it."""
    j = run.j
    j.speech.speakers.speed = 1.0  # real time: skip must cut a briefing that is really being spoken
    j.net.allow("weather", "news")
    j.wake_now()
    run.step("F3", "Brief me -> full briefing")
    t = run.say("Brief me", timeout=60)
    run.ck(any("percent chance of rain" in s or "degrees" in s for s in t.spoken), f"no weather: {t.spoken}")
    run.ck(any("In the news" in s for s in t.spoken), f"no headlines: {t.spoken}")
    run.online("weather", "news")
    run.step("F3", "skip stops it")
    # Start it again and say skip while she is speaking.
    import threading

    started = time.monotonic()
    if j.path == "typed":
        threading.Thread(target=lambda: j.orch.handle_typed_command("Brief me"), daemon=True).start()
    else:
        j.mic.say(j.shared.command_audio("Brief me", j.voice))
    deadline = time.monotonic() + 60
    lines_before = len(j.speech.lines)
    while time.monotonic() < deadline and not (j.speech.is_speaking() and any("In the news" in l.text or "degrees" in l.text
                                                                               for l in j.speech.lines[lines_before:])):
        time.sleep(0.02)
    time.sleep(0.5)
    if not run.ck(j.speech.is_speaking(), "the briefing was not being spoken when skip was said"):
        run.note(f"heard: {[x[1] for x in j.transcripts[-3:]]}; said: {[l.text[:80] for l in j.speech.lines[lines_before:]]}")
        return

    def silent_within(seconds: float) -> Optional[float]:
        since = time.monotonic()
        while time.monotonic() < since + seconds:
            if not j.speech.is_speaking():
                return time.monotonic() - since
            time.sleep(0.01)
        return None

    if j.path == "typed":
        threading.Thread(target=lambda: j.orch.handle_typed_command("skip"), daemon=True).start()
        cut = silent_within(6)
        run.note(f"typed skip -> silence after {None if cut is None else round(cut, 2)} s")
        run.ck(cut is not None and cut < 0.5, f"skip did not stop the briefing ({cut})")
    else:
        # By design the microphone is muted while she speaks (echo guard), so
        # a spoken "skip" can't be heard over the briefing; the Stop hotkey
        # is the voice user's way to end it.
        j.mic.say(j.shared.command_audio("skip", j.voice), lead_seconds=0.0)
        cut = silent_within(3)
        run.note(f"spoken skip during the briefing: {'stopped it' if cut is not None else 'not heard (echo guard mutes the mic while she speaks)'}")
        if cut is None:
            j.press_hotkey("stop")
            cut = silent_within(3)
            run.note(f"Stop hotkey -> silence after {None if cut is None else round(cut, 2)} s")
            run.ck(cut is not None and cut < 0.5, f"Stop hotkey did not end the briefing ({cut})")
    j.settle(60)
    run.state(State.AWAKE)


def f4(run: Run) -> None:
    """Scenario F4: 'Play Arijit Singh on YouTube' -> YouTube search to Chrome."""
    j = run.j
    j.wake_now()
    run.step("F4", "Play Arijit Singh on YouTube")
    t = run.say("Play Arijit Singh on YouTube")
    run.action(t, "open_url")
    url = " ".join(t.chrome[0]) if t.chrome else ""
    run.ck("youtube.com/results" in url, f"Chrome opener got {t.chrome}")
    run.ck(any(s.lower().startswith("searching youtube for") for s in t.spoken), f"said {t.spoken}")
    if j.path == "typed":
        run.ck("arijit" in url.lower(), f"query lost: {url}")
    elif "arijit" not in url.lower():
        run.note(f"Whisper misheard the name: searched YouTube for {url.split('search_query=')[-1]!r}")
    run.online()


def f5(run: Run) -> None:
    """Scenario F5: 'Summarise this video <link>' -> summary (transcript + Groq)."""
    j = run.j
    j.net.allow("youtube", "groq")
    j.wake_now()
    if j.path == "voice":
        run.step("F5", "voice: summarise this video (no link) -> asks for the link")
        t = run.say("Summarise this video")
        run.ck(t.spoken == [str(YOUTUBE_REPLIES["which_video"])], f"said {t.spoken}")
        run.online()
        run.not_applicable = "a link can only be typed; the voice path checks she asks for it"
        return
    run.step("F5", "Summarise this video <link>")
    t = run.typed(f"Summarise this video {PUBLIC_VIDEO}", timeout=120)
    answer = [s for s in t.spoken if s != CHECKING_ONLINE]
    run.ck(answer and not any(s.startswith(("This video has no transcript", "I can't reach", "YouTube")) for s in answer), f"said {t.spoken}")
    run.ck(answer and ("zoo" in " ".join(answer).lower() or "elephant" in " ".join(answer).lower()), f"not a summary of the video: {answer}")
    run.ck(not any("youtube.com" in s or "http" in s for s in t.spoken), "link spoken")
    run.online("youtube", "groq")


def f6(run: Run) -> None:
    """Scenario F6: 'Summarise my resume' -> preview -> confirm -> summary."""
    j = run.j
    j.net.allow("groq")
    write_resume_pdf(j.files_dir / "Resume.pdf")
    j.app.file_index.rebuild_now()
    j.wake_now()
    run.step("F6", "Summarise my resume -> preview")
    t = run.say("Summarise my resume", timeout=40)
    run.state(State.CONFIRMING)
    pending = j.c.pending_confirmation()
    run.ck(pending is not None and pending.action_name == "send_text_online" and pending.trust_level == TrustLevel.L3, f"pending {pending}")
    run.ck(any("exactly what would be sent online" in s for s in t.spoken), f"said {t.spoken}")
    run.ck(not j.gate.requests, "sent before the confirm")
    run.step("F6", "confirm -> summary")
    t = run.say("confirm", timeout=90)
    run.action(t, "send_text_online")
    answer = " ".join(s for s in t.spoken if s != CHECKING_ONLINE)
    run.ck(answer and ("python" in answer.lower() or "engineering" in answer.lower() or "student" in answer.lower()), f"said {t.spoken}")
    run.ck(len(j.gate.requests) == 1 and "Skills: Python" in str(j.gate.requests[0]), "the previewed text was not what was sent")
    run.ck(not any("Skills: Python" in l for l in j.activity()), "file content shown in Live Activity")
    run.online("groq")


def f7_f8(run: Run) -> None:
    """Scenario F7-F8: volume 50 / mute / unmute / brightness 70."""
    j = run.j
    j.wake_now()
    run.step("F7", "volume 50")
    t = run.say("Set the volume to 50")
    run.action(t, "set_volume")
    run.ck(("set_volume", 50) in j.system.calls and j.system.volume == 50, f"calls {j.system.calls}")
    run.ck(t.spoken == [str(SYSTEM_REPLIES["volume_now"]).format(value=50)], f"said {t.spoken}")
    run.step("F7", "mute")
    t = run.say("Mute")
    run.ck(("set_mute", True) in j.system.calls and t.spoken == [str(SYSTEM_REPLIES["muted"])], f"said {t.spoken}, calls {j.system.calls}")
    run.step("F7", "unmute")
    t = run.say("Unmute")
    run.ck(("set_mute", False) in j.system.calls and t.spoken == [str(SYSTEM_REPLIES["unmuted"]).format(value=50)], f"said {t.spoken}")
    run.step("F8", "brightness 70")
    t = run.say("Set the brightness to 70")
    run.ck(("set_brightness", 70) in j.system.calls and t.spoken == [str(SYSTEM_REPLIES["brightness_now"]).format(value=70)],
           f"said {t.spoken}, calls {j.system.calls}")
    run.trust(t, "L1")
    run.online()


def f9(run: Run) -> None:
    """Scenario F9: 'Lock the computer' -> confirm -> lock."""
    j = run.j
    j.wake_now()
    run.step("F9", "Lock the computer -> confirm -> lock")
    t = run.say("Lock the computer")
    run.state(State.CONFIRMING)
    run.ck(t.spoken == [str(SYSTEM_REPLIES["lock_prompt"])], f"said {t.spoken}")
    run.ck(("lock", None) not in j.system.calls, "locked before the confirm")
    t = run.say("confirm")
    run.action(t, "lock_screen")
    run.ck(("lock", None) in j.system.calls and t.spoken == [str(SYSTEM_REPLIES["lock_requested"])], f"said {t.spoken}")
    run.online()


def f10(run: Run) -> None:
    """Scenario F10: 'Turn Wi-Fi off' -> confirm -> radio."""
    j = run.j
    j.wake_now()
    run.step("F10", "Turn Wi-Fi off -> confirm -> radio off")
    t = run.say("Turn Wi-Fi off")
    run.state(State.CONFIRMING)
    run.ck(t.spoken == [str(SYSTEM_REPLIES["wifi_prompt_off"])], f"said {t.spoken}")
    run.ck(not any(c[0] == "set_wifi" for c in j.system.calls), "Wi-Fi changed before the confirm")
    t = run.say("confirm")
    run.action(t, "set_wifi")
    run.ck(("set_wifi", False) in j.system.calls and t.spoken == [str(SYSTEM_REPLIES["wifi_now"]).format(state="off")], f"said {t.spoken}")
    run.online()


def f11(run: Run) -> None:
    """Scenario F11: 'Restart' -> confirm twice -> 'stop' -> cancelled."""
    j = run.j
    j.wake_now()
    run.step("F11", "Restart -> confirm twice -> stop -> cancelled")
    t = run.say("Restart")
    run.state(State.CONFIRMING)
    run.ck(t.spoken and t.text.startswith("Restart in") and "confirm twice" in t.text, f"said {t.spoken}")
    t = run.say("confirm")
    run.ck(t.spoken == [str(CONV["confirm_again_reply"])], f"said {t.spoken}")
    run.ck(not j.system.shutdowns, "scheduled after one confirm")
    t = run.say("confirm")
    run.action(t, "restart")
    run.ck(j.system.shutdowns and j.system.shutdowns[-1][1] is True, f"no reboot scheduled: {j.system.calls}")
    run.ck(t.spoken and t.text.startswith("Restart requested"), f"said {t.spoken}")
    t = run.say("stop")
    j.settle(10)
    run.ck(j.system.aborted == 1, f"not aborted: {j.system.calls}")
    run.ck(str(SYSTEM_REPLIES["cancelled"]).format(Noun="Restart") in j.speech.texts, f"never said 'Restart cancelled.': {j.speech.texts[-3:]}")
    run.online()


# ===================================================================================================
# G: interruption, offline, shutdown
# ===================================================================================================

def _wait_speaking(j: Jarvis, timeout: float = 90.0, after: int = 0, not_text: tuple = ()) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if j.speech.is_speaking() and any(l.text not in not_text for l in j.speech.lines[after:]):
            return True
        time.sleep(0.02)
    return False


def g1_g2(run: Run) -> None:
    """Scenario G1-G2: long answer + Stop hotkey / STOP button -> speech cut."""
    import threading

    j = run.j
    j.speech.speakers.speed = 1.0  # real time: the Stop timing is measured
    j.net.allow("groq")
    j.wake_now()
    run.step("G1", "long answer + Stop hotkey -> speech cut")
    before = len(j.speech.lines)
    if j.path == "typed":
        threading.Thread(target=lambda: j.orch.handle_typed_command("Tell me about the history of chess in detail"), daemon=True).start()
    else:
        j.mic.say(j.shared.command_audio("Tell me about the history of chess in detail", j.voice))
    run.ck(_wait_speaking(j, after=before, not_text=(CHECKING_ONLINE,)), "the answer never started")
    time.sleep(1.0)
    cuts_before = len([p for p in j.speech.speakers.parts if not p[2]])
    pressed = time.monotonic()
    j.press_hotkey("stop")
    silent = None
    while time.monotonic() < pressed + 5:
        if not j.speech.is_speaking():
            silent = time.monotonic() - pressed
            break
        time.sleep(0.005)
    cut = j.speech.speakers.cut_at
    run.note(f"Stop hotkey -> playback cut {None if cut is None else round(cut - pressed, 3)} s, silent {None if silent is None else round(silent, 3)} s")
    run.ck(silent is not None and silent < 0.5, f"speech not cut within 0.5 s of the Stop hotkey ({silent})")
    run.note("cut mid-sentence" if len([p for p in j.speech.speakers.parts if not p[2]]) > cuts_before else "cut between sentences")
    j.settle(20)
    run.state(State.AWAKE)
    run.ck("stopped before finishing" in j.activity(), "Stop not shown in Live Activity")
    run.step("G2", "the intro + the STOP button -> speech cut")
    from .e2e_rig import walk_controls

    stop = next((c for c in walk_controls(j.page.controls[0]) if getattr(getattr(c, "content", None), "value", None) == "STOP"
                 and getattr(c, "on_click", None) is not None), None)
    run.ck(stop is not None, "no STOP button on the dashboard")
    before = len(j.speech.lines)
    if j.path == "typed":
        threading.Thread(target=lambda: j.orch.handle_typed_command("Introduce yourself"), daemon=True).start()
    else:
        time.sleep(1.0)
        j.mic.say(j.shared.command_audio("Introduce yourself", j.voice))
    run.ck(_wait_speaking(j, after=before), "the intro never started")
    time.sleep(1.5)
    pressed = time.monotonic()
    if stop is not None:
        stop.on_click(None)
    silent = None
    while time.monotonic() < pressed + 5:
        if not j.speech.is_speaking():
            silent = time.monotonic() - pressed
            break
        time.sleep(0.005)
    run.note(f"STOP button -> silent {None if silent is None else round(silent, 3)} s")
    run.ck(silent is not None and silent < 0.5, f"speech not cut within 0.5 s of the STOP button ({silent})")
    j.settle(20)
    run.state(State.AWAKE)
    run.online("groq")


def g3(run: Run) -> None:
    """Scenario G3: press-to-talk -> 'open Paint'."""
    j = run.j
    if j.path == "typed":
        run.step("G3", "press-to-talk (voice only)")
        run.not_applicable = "press-to-talk opens the microphone; there is no typed form"
        return
    j.wake_now()
    run.step("G3", "press-to-talk -> open Paint")
    marks = j._marks()
    j.press_hotkey("push_to_talk")
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and j.c.state != State.LISTENING:
        time.sleep(0.01)
    run.ck(j.c.state == State.LISTENING, f"press-to-talk did not open the microphone: {j.c.state}")
    time.sleep(0.3)
    j.mic.say(j.shared.command_audio("open Paint", j.voice), lead_seconds=0.0)
    while j.mic.busy():
        time.sleep(0.01)
    started = j.mic.speech_end_at or time.monotonic()
    j.settle(30)
    t = j._turn("open Paint", "voice", started, marks)
    run._record(t, True, None)
    run.ck(any(src == "push-to-talk" for _h, src in t.heard), f"not handled as push-to-talk: {t.heard}")
    run.action(t, "open_app", target=lambda v: "paint" in str(v).lower())
    run.online()


def g4(run: Run) -> None:
    """Scenario G4: offline -> 'What's the weather' -> R14 message."""
    j = run.j
    j.wake_now()
    j.net.allow("weather")
    j.net.offline = True
    run.step("G4", "offline -> What's the weather -> R14 message")
    t = run.say("What's the weather", timeout=40)
    # "Searching online." is said first: the request really is attempted (R3).
    run.ck([s for s in t.spoken if s != CHECKING_ONLINE] == [str(WEATHER_TEXTS["cannot_reach"])], f"said {t.spoken}")
    run.state(State.AWAKE)
    run.ck(j.net.hosts(allowed=False), "nothing even tried the network")


def g5(run: Run) -> None:
    """Scenario G5: window close -> clean shutdown."""
    import threading

    j = run.j
    j.wake_now()
    j.type("turn on the camera")
    j.type("confirm", timeout=40)
    run.step("G5", "window close -> clean shutdown")
    run.ck(j.app.vision.stream is not None and j.app.vision.stream.running, "camera stream not running before close")
    vs = j.voice_system
    report = j.close()
    time.sleep(0.5)
    run.ck(report.get("destroyed"), "window not destroyed after the close event")
    run.ck(vs.mic.stopped, "microphone not stopped")
    run.ck(j.user32.quit_posted and not j.user32.registered, f"hotkeys still registered: {j.user32.registered}")
    run.ck(vs.detector is not None and vs.detector._model is None, "wake-word model not released")
    run.ck(vs.stt is not None and vs.stt._model is None, "speech-to-text model not released")
    run.ck(vs.listener.on_transition not in j.c._transition_listeners, "listener still attached")
    run.ck(j.speech.closed, "speech engine not closed")
    run.ck(j.app.vision.stream is None and j.camera_log[-1] == "release", f"camera not released: {j.camera_log[-2:]}")
    run.ck(j.c._thread is None, "controller thread not shut down")
    deadline = time.monotonic() + 5
    names = ("jarvis-controller", "jarvis-wake-word", "jarvis-hotkeys", "jarvis-camera-stream", "jarvis-neural-tts",
             "e2e-virtual-mic", "jarvis-voice-utterance")
    alive = []
    while time.monotonic() < deadline:
        alive = sorted({t.name for t in threading.enumerate() if t.name in names})
        if not alive:
            break
        time.sleep(0.1)
    run.ck(not alive, f"threads still running after close: {alive}")
    run.online()


@dataclass
class Scenario:
    """One checklist scenario: id, title, the function that runs it, and which online services it may use."""
    id: str
    title: str
    fn: Callable[[Run], None]
    online: tuple = ()
    first_wake: bool = False
    expect_refused: bool = False
    voice: bool = True  # has a voice form
    typed: bool = True  # has a typed form


SCENARIOS: List[Scenario] = [
    Scenario("A1", "launch -> dashboard + voice ready (hidden mode, timings)", a1),
    Scenario("A2", "'Hey Jarvis, wake up' -> greeting only (no automatic briefing)", a2, online=("weather", "news"), first_wake=True),
    Scenario("A3", "'Wake up' while awake -> 'I'm already awake.'", a3),
    Scenario("A4", "'Thanks, go to sleep now' -> Goodnight -> SLEEPING", a4),
    Scenario("A5", "asleep 'Open Paint' -> nothing; heard while asleep", a5),
    Scenario("A6", "'Wake up, Jarvis' -> welcome-back greeting", a6),
    Scenario("B1", "'Introduce yourself' -> exact locked intro (cached audio)", b1),
    Scenario("B2", "'Could you launch Notepad' -> open notepad", b2),
    Scenario("B3", "'Open Gmail in Chrome' -> 'Opening Gmail in Chrome', no URL spoken", b3),
    Scenario("B4", "'Google best coffee in Sydney' -> Google search to Chrome", b4),
    Scenario("B5", "'Find my resume' -> count-only speech, names on screen", b5),
    Scenario("B6", "'Open number one' -> picks file 1", b6),
    Scenario("B7", "'Why'd you do that' -> local answer_why", b7),
    Scenario("B8", "'What's the time' / 'What day is it' -> local", b8),
    Scenario("C1", "'What is gravity, in one line' -> one sentence (Groq)", c1, online=("groq",)),
    Scenario("C2", "'Who won the last India cricket match' -> answer + date (Groq live search)", c2, online=("groq",)),
    Scenario("C3", "'Will it rain in Sydney today' -> weather + credit (Open-Meteo)", c3, online=("weather",)),
    Scenario("C4", "'Weather in Hamilton' -> asks -> 'the second one' -> answers", c4, online=("weather",)),
    Scenario("D1-D4", "remember gym / recall / refuse password / forget", d1_d4),
    Scenario("D5", "'Ugh nothing works today, open Paint' -> Paint + check-in", d5),
    Scenario("D6", "'You're useless, open Notepad' -> rude line + Notepad", d6),
    Scenario("E1", "'What's on my screen' -> Word + Chrome spoken, NetBank blacked out", e1),
    Scenario("E2", "'Explain my screen' -> cancel -> nothing sent", e2),
    Scenario("E3", "'Explain my screen' -> 'Yes, confirm' -> spoken answer (Groq)", e3, online=("groq",)),
    Scenario("E4-E6", "camera look / price / left or right", e4_e6),
    Scenario("E7", "'Turn on the camera' -> confirm -> Webcam panel frames; off", e7),
    Scenario("F1-F2", "track / list / check topics (RSS)", f1_f2, online=("news",)),
    Scenario("F3", "'Brief me' -> full briefing; 'skip' stops it", f3, online=("weather", "news")),
    Scenario("F4", "'Play Arijit Singh on YouTube' -> YouTube search to Chrome", f4),
    Scenario("F5", "'Summarise this video <link>' -> summary (transcript + Groq)", f5, online=("youtube", "groq")),
    Scenario("F6", "'Summarise my resume' -> preview -> confirm -> summary", f6, online=("groq",)),
    Scenario("F7-F8", "volume 50 / mute / unmute / brightness 70", f7_f8),
    Scenario("F9", "'Lock the computer' -> confirm -> lock", f9),
    Scenario("F10", "'Turn Wi-Fi off' -> confirm -> radio", f10),
    Scenario("F11", "'Restart' -> confirm twice -> 'stop' -> cancelled", f11),
    Scenario("G1-G2", "long answer + Stop hotkey / STOP button -> speech cut", g1_g2, online=("groq",)),
    Scenario("G3", "press-to-talk -> 'open Paint'", g3, typed=False),
    Scenario("G4", "offline -> 'What's the weather' -> R14 message", g4, online=("weather",), expect_refused=True),
    Scenario("G5", "window close -> clean shutdown", g5),
]
BY_ID: Dict[str, Scenario] = {s.id: s for s in SCENARIOS}
