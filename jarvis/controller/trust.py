"""Trust levels (Section 5). Fixed in code, never chosen by JARVIS or a model.

All five levels are implemented as of F7 step 3: L0/L1 run immediately;
L2/L3/L4 route through the confirmation engine in controller.py before a
permit is ever issued.

Answering a question (F2) and searching for a file (F4) never touch the
fixed action set below -- per 4.2 the fixed action set is only actions
that do something to the OS (open, capture, change a setting, ...). Search
and Q&A are read-only/informational and go straight to a spoken answer;
only *opening* the file the search turned up is a real action ("open_file").

L4's real targets per Section 5 ("deleting files. Changing system
settings") have no match in 4.2's fixed action set at all -- there is no
delete action. move_file was mapped to L4 in an earlier step as an
unreviewed guess and has been removed from TRUST_TABLE below (it stays in
FIXED_ACTION_SET, matching 4.2's literal list, but now has no trust level
and is therefore denied if ever requested). A real L4 action gets added
here only when the developer designates one, per 4.2's "add to it only
with the developer's approval." F7's L4 confirmation mechanics (double
confirmation) are built and tested against a transient, test-only action
name instead (see tests/test_confirmation.py) -- nothing added here.

F10's "forget everything" (`forget_all_memory`) is the
first real L4 action, designated by the developer with F10's decisions.

L3 (one confirm, after a preview of exactly what goes online or what
runs continuously): send_screen_online (F8 "explain my screen"),
send_text_online (F19 summaries), camera_stream (F9 live preview) and
set_wifi (F22). capture_screen/capture_camera stay L1: a one-time look he
asked for, read locally. restart and shutdown (F22) are L4 like
forget_all_memory.
"""

from __future__ import annotations

from enum import Enum, auto

from jarvis.controller.events import EventType


class TrustLevel(Enum):
    """F7 trust levels: L0 answer, L1 safe action, L2 one confirm, L3 one confirm with a preview of what
    goes online, L4 two confirms."""
    L0 = auto()  # answering questions
    L1 = auto()  # opening apps/URLs, one-time look at screen/camera
    L2 = auto()  # one confirm: locking the laptop, forgetting several memories
    L3 = auto()  # one confirm after a preview: sending online, continuous camera, Wi-Fi
    L4 = auto()  # two confirms: restart, shutdown, forgetting everything


# The fixed action set, exactly as listed in 4.2. Only these action names
# may ever be requested through the permit pipeline; anything else is
# denied by default.
FIXED_ACTION_SET: frozenset[str] = frozenset(
    {
        "open_app",
        "open_file",
        "open_url",
        "move_file",
        "capture_screen",
        "capture_camera",
        # F10 long-term memory (the developer's F10 decisions:
        # remember L0, forget one item L1, forget everything L4).
        "remember",
        "forget_memory",
        "forget_all_memory",
        # "forget all my exams" -- every item on one subject,
        # listed, after one confirm (L2).
        "forget_memories",
        # F16b: a search is a Google results page opened in
        # his own Chrome through open_url (L1). JARVIS's own browser and its
        # browser_open_read action are gone.
        # F8 (the developer's F8 decisions): the R4-previewed
        # screenshot + screen text + question, sent online after "confirm".
        "send_screen_online",
        # F9 (the developer's F9 decisions): "turn on the camera" keeps it on
        # with a live preview -- continuous, so L3 (Section 5).
        "camera_stream",
        # F20: "track X" / "stop tracking X". L0 by the
        # developer's decision: his own words, local, reversible.
        "edit_topic_list",
        # F22, the levels set by the developer: volume, mute
        # and brightness L1; lock L2; Wi-Fi L3; restart and shutdown L4.
        "set_volume",
        "set_mute",
        "set_brightness",
        "set_wifi",
        "lock_screen",
        "restart",
        "shutdown",
        # F19: a file's text, sent online for a summary only
        # after the R4 preview of the exact text (approved as
        # send_text_online, L3).
        "send_text_online",
    }
)

# Fixed trust table (deterministic, no model involved). move_file has no
# entry -- see the module docstring for why.
# Collections: a dict maps each action to its fixed trust level (O(1) lookup, never a model's guess).
TRUST_TABLE: dict[str, TrustLevel] = {
    "open_app": TrustLevel.L1,
    "open_url": TrustLevel.L1,
    "open_file": TrustLevel.L1,
    "capture_screen": TrustLevel.L1,
    "capture_camera": TrustLevel.L1,
    "remember": TrustLevel.L0,
    "forget_memory": TrustLevel.L1,
    "forget_all_memory": TrustLevel.L4,
    "forget_memories": TrustLevel.L2,
    "send_screen_online": TrustLevel.L3,
    "camera_stream": TrustLevel.L3,
    "edit_topic_list": TrustLevel.L0,
    "set_volume": TrustLevel.L1,
    "set_mute": TrustLevel.L1,
    "set_brightness": TrustLevel.L1,
    "lock_screen": TrustLevel.L2,
    "set_wifi": TrustLevel.L3,
    "restart": TrustLevel.L4,
    "shutdown": TrustLevel.L4,
    "send_text_online": TrustLevel.L3,
}

# Section 5: "This is the only standing approval." For these actions only,
# a JARVIS-started request whose exact outbound list matches the approved
# SHA-256 gets its permit without a new confirm. The check is the
# controller's (a hash comparison, deterministic); the feature supplies it.
# Empty. Its only user was F21's first-wake briefing, which was
# removed -- the briefing now runs only when he asks (L0).
STANDING_APPROVAL_ACTIONS: frozenset[str] = frozenset()

# All five levels are now implemented (F7 step 3): L0/L1 authorize
# immediately, L2/L3/L4 route through the confirmation engine. Kept as an
# explicit set (rather than "any TrustLevel") so a future sixth level
# would have to be added here deliberately, not fall through silently.
IMPLEMENTED_LEVELS: frozenset[TrustLevel] = frozenset(
    {TrustLevel.L0, TrustLevel.L1, TrustLevel.L2, TrustLevel.L3, TrustLevel.L4}
)

# F7 Section 5: trust levels that require a preview + confirmation before
# a permit is issued, and how many CONFIRM_RECEIVED events each needs.
CONFIRMATION_REQUIRED_LEVELS: frozenset[TrustLevel] = frozenset(
    {TrustLevel.L2, TrustLevel.L3, TrustLevel.L4}
)
CONFIRMATIONS_NEEDED: dict[TrustLevel, int] = {
    TrustLevel.L2: 1,
    TrustLevel.L3: 1,
    TrustLevel.L4: 2,
}

# Q&A (F2) and file search (F4) never enter the permit pipeline (see the
# module docstring above), so they have no TRUST_TABLE/FIXED_ACTION_SET
# entry -- but F5's decision record still needs a trust level to show for
# them (R13). This is that level, fixed in code exactly like TRUST_TABLE,
# never inferred from a model. Opening a file the search found is the
# L1 action open_file, in TRUST_TABLE above.
EVENT_TRUST_LEVELS: dict[EventType, TrustLevel] = {
    EventType.ANSWER_READY: TrustLevel.L0,
}
