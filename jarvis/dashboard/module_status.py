"""F6 Modules panel: a hand-maintained status dict, not a dynamic
feature-discovery framework (per the developer's explicit instruction --
that would be over-engineering for what this is worth here). Update this
dict by hand each time a feature actually ships or starts.

Names/numbers match the feature list in the project specification exactly.
"""

from __future__ import annotations

from typing import NamedTuple

# "built" / "in_progress" / "pending" -- the only three states F6 shows.
ModuleStatusValue = str

BUILT = "built"
IN_PROGRESS = "in_progress"
PENDING = "pending"


class ModuleStatus(NamedTuple):
    """One row of the Modules panel: feature code, name and whether it is built."""
    code: str
    name: str
    status: ModuleStatusValue


# Every feature in this build, as shown in the Modules panel. F13 is the
# shared online layer and the Internet panel, F15 is part of F2's
# conversation mode, and F16b (the browser search) has its own row.
MODULE_STATUS: tuple[ModuleStatus, ...] = (
    ModuleStatus("F1", "Self Introduction", BUILT),
    ModuleStatus("F2", "Voice Interaction", BUILT),  # conversation mode voice fixes
    ModuleStatus("F3", "Open Any Application", BUILT),
    ModuleStatus("F4", "File Finder", BUILT),
    ModuleStatus("F5", "Live Activity Log", BUILT),
    ModuleStatus("F6", "Futuristic Dashboard", BUILT),  # R15 pause + low-memory warning
    ModuleStatus("F7", "Trust Levels", BUILT),
    ModuleStatus("F8", "Screen Awareness", BUILT),  # Local OCR, protected blackout, R4 preview for "explain"
    ModuleStatus("F9", "Webcam Vision", BUILT),  # One look, prices, live preview (L3)
    ModuleStatus("F10", "Long-Term Memory", BUILT),  # Told-only, voice/typed, no panel
    ModuleStatus("F13", "Local-First Mode", BUILT),
    ModuleStatus("F14", "Personality/Emotion", BUILT),  # Words only, no voice loudness
    ModuleStatus("F15", "Sleep Mode (part of F2)", BUILT),  # merged into conversation mode
    ModuleStatus("F16", "Web: live questions, weather", BUILT),  # Stage 1 F16a
    ModuleStatus("F16b", "Web Search in Chrome", BUILT),  # A Google search opened in his own Chrome
    ModuleStatus("F17", "Core State Machine", BUILT),
    # Stretch features get a row once built, so the panel
    # and her list of abilities match what she can really do (R19).
    ModuleStatus("F18", "YouTube Tools", BUILT),  # Search in Chrome, transcript summaries
    ModuleStatus("F19", "File Processor", BUILT),  # Local reading, R4 preview before a summary
    ModuleStatus("F20", "Topic Monitor", BUILT),  # Track topics, on demand only (first wake removed)
    ModuleStatus("F21", "Morning Briefing", BUILT),  # Only on request, never automatic
    ModuleStatus("F22", "System Settings", BUILT),  # Volume, brightness, Wi-Fi, lock, restart, shutdown
)
