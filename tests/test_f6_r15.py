"""F6 / R15: animations pause while the window is
minimised or hidden; the low-memory warning waits for 3 low slow ticks in a
row and is said at most once per 300 s; module statuses match what is
built; no unbuilt feature is left on the dashboard."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.dashboard.gauges import LowMemoryWarning, SystemGauges, low_memory_warning_text
from jarvis.dashboard.module_status import BUILT, MODULE_STATUS
from jarvis.dashboard.visibility import WindowVisibility
from jarvis.settings import SETTINGS

_APP_SOURCE = (Path(__file__).resolve().parent.parent / "jarvis" / "dashboard" / "app.py").read_text(encoding="utf-8")


# -- R15: pause while minimised or hidden -------------------------------------------------


def test_minimise_and_restore_events() -> None:
    """Minimise and restore events."""
    v = WindowVisibility()
    assert not v.hidden
    v.on_window_event(SimpleNamespace(name="MINIMIZE"))
    assert v.hidden
    v.on_window_event(SimpleNamespace(name="RESTORE"))
    assert not v.hidden


@pytest.mark.parametrize("hide, show", [("HIDE", "SHOW"), ("MINIMIZE", "MAXIMIZE"), ("MINIMIZE", "FOCUS")])
def test_other_hide_and_show_events(hide, show) -> None:
    """Other hide and show events."""
    v = WindowVisibility()
    v.on_window_event(hide)
    assert v.hidden
    v.on_window_event(show)
    assert not v.hidden


def test_unrelated_events_change_nothing() -> None:
    """Unrelated events change nothing."""
    v = WindowVisibility()
    v.on_window_event("MINIMIZE")
    for name in ("MOVE", "RESIZE", "BLUR", "MOVED"):
        v.on_window_event(name)
    assert v.hidden


def test_slow_tick_fallback_when_an_event_is_missed() -> None:
    """Slow tick fallback when an event is missed."""
    v = WindowVisibility()
    v.poll(minimized=True, app_visible=None)
    assert v.hidden
    v.poll(minimized=False, app_visible=True)
    assert not v.hidden
    v.poll(minimized=False, app_visible=False)  # Flet's lifecycle says hidden
    assert v.hidden


def test_either_source_hiding_counts() -> None:
    """Either source hiding counts."""
    v = WindowVisibility()
    v.on_window_event("MINIMIZE")
    v.poll(minimized=False, app_visible=True)  # a property that lags behind the event
    assert v.hidden


def test_the_refresh_loop_skips_animation_work_while_hidden() -> None:
    """The refresh loop skips animation work while hidden."""
    # The loop lives inside build() (Flet), so its two guards are checked in the source.
    assert "paused = visibility.hidden" in _APP_SOURCE
    assert "if not paused:\n                            levels = voice.levels()" in _APP_SOURCE
    assert "if not paused and now >= next_glow:" in _APP_SOURCE
    assert "visibility.on_window_event(e.type)" in _APP_SOURCE
    # Gauges are not behind the pause: the slow tick still samples them.
    slow_tick = _APP_SOURCE[_APP_SOURCE.index("if now >= next_slow:"):]
    assert "gauges = read_gauges()" in slow_tick.split("# -- activity feed")[0]


def test_nothing_here_moves_focuses_or_minimises_the_window() -> None:
    """Nothing here moves focuses or minimises the window."""
    source = (Path(__file__).resolve().parent.parent / "jarvis" / "dashboard" / "visibility.py").read_text(encoding="utf-8")
    for banned in ("ShowWindow", "SetForegroundWindow", "minimized =", "minimized=True", "win32gui", "ctypes"):
        assert banned not in source


# -- R15: low-memory warning ------------------------------------------------------------------


def test_warning_waits_for_three_low_ticks_in_a_row() -> None:
    """Warning waits for three low ticks in a row."""
    w = LowMemoryWarning(consecutive=3, cooldown_seconds=300, clock=lambda: 0.0)
    assert [w.sample(True), w.sample(True)] == [False, False]
    assert w.sample(True) is True


def test_one_good_tick_resets_the_count() -> None:
    """One good tick resets the count."""
    w = LowMemoryWarning(consecutive=3, cooldown_seconds=300, clock=lambda: 0.0)
    assert [w.sample(x) for x in (True, True, False, True, True)] == [False] * 5
    assert w.sample(True) is True


def test_said_at_most_once_per_cooldown() -> None:
    """Said at most once per cooldown."""
    now = [0.0]
    w = LowMemoryWarning(consecutive=3, cooldown_seconds=300, clock=lambda: now[0])
    results = []
    for t in range(0, 700, 10):  # low the whole time, one tick every 10 s
        now[0] = float(t)
        results.append((t, w.sample(True)))
    warned_at = [t for t, warned in results if warned]
    assert warned_at == [20, 320, 620]


def test_settings_defaults_and_text() -> None:
    """Settings defaults and text."""
    cfg = SETTINGS["dashboard"]
    assert cfg["low_memory_consecutive_samples"] == 3 and cfg["low_memory_warning_cooldown_seconds"] == 300
    text = low_memory_warning_text(SystemGauges(cpu_percent=0, memory_percent=91.6, memory_available_mb=1300, available=True))
    assert "8 percent free" in text and "Close apps" in text


def test_gauge_turns_red_at_once_and_warning_is_spoken_outside_the_ui_lock() -> None:
    """Gauge turns red at once and warning is spoken outside the UI lock."""
    tick = _APP_SOURCE[_APP_SOURCE.index("low = is_memory_low(gauges)"):]
    assert tick.index('ram_status.value = "LOW MEMORY"') < tick.index("memory_warning.sample(low)")
    assert "warn(warning)  # outside ui_lock" in _APP_SOURCE
    assert "warn=notify" in _APP_SOURCE  # notify shows the notice and speaks it


# -- statuses and unbuilt features ------------------------------------------------------------


def test_module_statuses_match_what_is_built() -> None:
    """Module statuses match what is built."""
    status = {m.code: m for m in MODULE_STATUS}
    for code in ("F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F13", "F14", "F15", "F16", "F16b", "F17"):
        assert status[code].status == BUILT, code
    assert "F12" not in status and "F23" not in status
    assert "F11" not in status
    assert "part of F2" in status["F15"].name
    assert "weather" in status["F16"].name.lower()


def test_no_unbuilt_feature_is_left_on_the_dashboard() -> None:
    """No unbuilt feature is left on the dashboard."""
    # Camera (F9) and Screen Capture (F8) are live rows; there is no
    # Keyboard / Mouse row and no "isn't built yet" toast left.
    assert "Keyboard / Mouse\", \"F11\"" not in _APP_SOURCE and "ft.Icons.KEYBOARD" not in _APP_SOURCE
    assert '_capture_row(\n        ft.Icons.CAMERA_ALT, "Camera", CAMERA' in _APP_SOURCE
    assert '_capture_row(\n        ft.Icons.DESKTOP_WINDOWS, "Screen Capture", SCREEN' in _APP_SOURCE
    assert "not_built_yet" not in _APP_SOURCE and "_unbuilt_row" not in _APP_SOURCE
    assert "not built yet" not in _APP_SOURCE and "isn't built yet" not in _APP_SOURCE
    assert "not available" not in _APP_SOURCE.replace('never just "not available"', "")


def test_env_example_quotes_the_current_setup_message() -> None:
    """Env example quotes the current setup message."""
    text = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    quoted = " ".join(line.lstrip("# ").strip() for line in text.splitlines() if line.startswith("#"))
    assert SETTINGS["setup_messages"]["groq_no_key"] in quoted


# -- Modules panel scrolls at the normal window size ----------------------


def test_modules_list_scrolls_inside_its_panel() -> None:
    """Modules list scrolls inside its panel."""
    modules = _APP_SOURCE[_APP_SOURCE.index('_panel_title("Modules")'):]
    listview = modules[: modules.index("spacing=6,")]
    assert "ft.ListView(" in listview and "expand=True" in listview and "scroll=ft.ScrollMode.ALWAYS" in listview


def test_module_rows_stay_on_one_line_with_the_full_name_on_hover() -> None:
    """Module rows stay on one line with the full name on hover."""
    rows = _APP_SOURCE[_APP_SOURCE.index("module_rows = []"):_APP_SOURCE.index("column1 = ft.Column(")]
    assert "max_lines=1" in rows and "overflow=ft.TextOverflow.ELLIPSIS" in rows
    assert 'tooltip=f"{m.code} {m.name}' in rows


def test_layout_split_and_title_alignment_are_unchanged() -> None:
    """Layout split and title alignment are unchanged."""
    assert "_COL1_FLEX, _COL2_FLEX, _COL3_FLEX = 3, 10, 4" in _APP_SOURCE
    # Column 1 keeps the 14px top padding of every panel, so titles line up.
    assert "_COL1_PANEL_PADDING = ft.Padding(14, 14, 14, 8)" in _APP_SOURCE
    assert "padding: ft.Padding | int = 14" in _APP_SOURCE
