"""Test-wide guard: no test may really open a browser, an
app, a URL or a file on the developer's screen.

The cause it guards against: a test that ran a command through the real
F3/F16b executors (jarvis/actions/open_app.py `open_url_executor` ->
subprocess.Popen([chrome, url]) and `open_app_executor` -> os.startfile)
opened Chrome on his screen during an unattended run. Every test must mock
those executors or the launch call instead.

Every real opener is replaced, for every test, by a stub that records the
call and raises. The test is then failed at teardown, naming the call --
also when the call happened on a worker thread, where the raise alone
would not fail the test. A test that mocks an opener itself (monkeypatch
inside the test) replaces the stub, which is the intended way.

Child processes are allowed only when they run this same Python
(`sys.executable`), which a few tests use to check imports in isolation.

F22: the same guard refuses every real system-setting
change -- WindowsSystem's setters, LockWorkStation, InitiateSystemShutdown,
AbortSystemShutdown, ExitWindowsEx and screen_brightness_control's
set_brightness -- so no test can change the volume, brightness or Wi-Fi,
lock the laptop or shut it down.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import pytest

_PYTHON = os.path.normcase(os.path.abspath(sys.executable))

# Imported once here, before any guard is in place: screen_brightness_control
# asks Windows its version (platform -> `ver`) on first import, which the
# guard below would otherwise refuse inside whichever test imported it first.
try:
    import screen_brightness_control as _sbc
except ImportError:
    _sbc = None
try:
    from jarvis.system import backend as _system_backend
except ImportError:
    _system_backend = None


class RealOpenAttempted(RuntimeError):
    """Raised instead of opening anything for real."""


def _program(args) -> str:
    if isinstance(args, (list, tuple)) and args:
        return str(args[0])
    return str(args)


@pytest.fixture(autouse=True)
# Testing: an autouse fixture; every test automatically gets real openers swapped for failing stubs.
def no_real_opening(monkeypatch):
    """Autouse fixture: every real opener (apps, URLs, files) and system setter is replaced by a stub that
    fails the test."""
    attempts: list = []
    lock = threading.Lock()

    def refuse(what: str):
        def stub(*args, **kwargs):
            with lock:
                attempts.append(f"{what}{args!r}")
            raise RealOpenAttempted(f"test tried to really call {what}{args!r}; mock it")

        return stub

    real_popen = subprocess.Popen

    class GuardedPopen(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, args, *rest, **kwargs):
            if kwargs.get("shell") or os.path.normcase(os.path.abspath(_program(args))) != _PYTHON:
                with lock:
                    attempts.append(f"subprocess.Popen({args!r})")
                raise RealOpenAttempted(f"test tried to really start {args!r}; mock it")
            super().__init__(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)
    if hasattr(os, "startfile"):
        monkeypatch.setattr(os, "startfile", refuse("os.startfile"))
    monkeypatch.setattr(os, "system", refuse("os.system"))
    for name in ("open", "open_new", "open_new_tab"):
        monkeypatch.setattr(webbrowser, name, refuse(f"webbrowser.{name}"))
    try:
        import win32api

        monkeypatch.setattr(win32api, "ShellExecute", refuse("win32api.ShellExecute"))
    except ImportError:
        pass
    if sys.platform == "win32":
        import ctypes

        monkeypatch.setattr(ctypes.windll.shell32, "ShellExecuteW", refuse("ShellExecuteW"), raising=False)
        # F22: no test may lock the laptop.
        monkeypatch.setattr(ctypes.windll.user32, "LockWorkStation", refuse("LockWorkStation"), raising=False)
    # F22: no test may change a real system setting or shut anything down.
    try:
        import win32api

        for name in ("InitiateSystemShutdown", "AbortSystemShutdown", "ExitWindowsEx"):
            monkeypatch.setattr(win32api, name, refuse(f"win32api.{name}"), raising=False)
    except ImportError:
        pass
    if _system_backend is not None:
        for name in ("set_volume", "set_mute", "set_brightness", "set_wifi", "lock", "schedule_shutdown", "abort_shutdown"):
            monkeypatch.setattr(_system_backend.WindowsSystem, name, refuse(f"WindowsSystem.{name}"))
    if _sbc is not None:
        monkeypatch.setattr(_sbc, "set_brightness", refuse("screen_brightness_control.set_brightness"))

    yield attempts

    if attempts:
        pytest.fail("A test tried to really open something (browser, app, URL or file): " + "; ".join(attempts))


# -- the developer's real data folder is never written by a test --
#
# Found at the end of the run: tests/test_voice_system.py built the
# real app (default DebugLog) and pressed Stop, adding "stop while idle" to the
# real jarvis_data/logs/debug.log on every run; tests/test_startup.py's
# background start appended to the real startup_timings.log. Every default
# data path is now redirected into the test's own temporary folder, and the
# whole session fails if anything under the real jarvis_data changed.

_DATA_DEFAULTS = [
    ("jarvis.logbook.debug_log", "debug_log_path", "logs/debug.log"),
    ("jarvis.startup_timing", "log_path", "logs/startup_timings.log"),
    ("jarvis.news.topics", "default_topics_path", "topics.json"),
    ("jarvis.files.commands", "default_temp_dir", "tmp"),
    ("jarvis.memory.store", "default_memory_path", "memory.json"),
    ("jarvis.actions.wake_greeting", "default_state_path", "greeting_state.json"),
    ("jarvis.actions.weather", "default_cache_path", "weather_cache.json"),
    ("jarvis.online.budget", "default_usage_path", "groq_usage.json"),
    # Names app.py imported directly:
    ("jarvis.dashboard.app", "default_state_path", "greeting_state.json"),
    ("jarvis.dashboard.app", "default_cache_path", "weather_cache.json"),
    ("jarvis.dashboard.app", "default_usage_path", "groq_usage.json"),
]
_REAL_DATA = Path(__file__).resolve().parent.parent / "jarvis_data"


@pytest.fixture(autouse=True)
def no_real_data_files(tmp_path, monkeypatch):
    """Autouse fixture: every default data path points into the test's temporary folder, never the real
    jarvis_data."""
    import importlib

    fake = tmp_path / "jarvis_data_for_test"
    for module_name, attribute, relative in _DATA_DEFAULTS:
        # Every module is imported HERE, before the test runs.
        # Skipping the ones not imported yet left a gap: the e2e rig imported
        # the dashboard lazily, inside the test, and its first run wrote the
        # real greeting_state.json, debug.log and startup_timings.log.
        module = importlib.import_module(module_name)
        if hasattr(module, attribute):
            target = fake / relative
            monkeypatch.setattr(module, attribute, lambda target=target: target)
    # (The dashboard's diag log is written only by its refresh loop, which no
    # test runs; a test checks that path itself, so it is left as it is.)
    yield


def _snapshot() -> dict:
    if not _REAL_DATA.exists():
        return {}
    found = {}
    for path in _REAL_DATA.rglob("*"):
        if path.is_file() and "work" not in path.relative_to(_REAL_DATA).parts:
            stat = path.stat()
            found[str(path.relative_to(_REAL_DATA))] = (stat.st_size, stat.st_mtime_ns)
    return found


def pytest_configure(config) -> None:
    """Register this suite's custom markers."""
    config.addinivalue_line(
        "markers", "e2e: end-to-end verification (tests/e2e): real models, real voice pipeline; "
                   "excluded from the normal run, run with: python -m pytest -m e2e")


def pytest_collection_modifyitems(config, items) -> None:
    """The e2e suite (minutes long, a few real online calls) is
    left out of the normal fast run unless -m names it."""
    if "e2e" in (config.getoption("-m") or ""):
        return
    selected = [item for item in items if item.get_closest_marker("e2e") is None]
    deselected = [item for item in items if item.get_closest_marker("e2e") is not None]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected


def pytest_sessionstart(session) -> None:
    """Snapshot the real jarvis_data before any test runs."""
    session.config._jarvis_data_before = _snapshot()


def pytest_sessionfinish(session, exitstatus) -> None:
    """Fail the whole session if anything under the real jarvis_data changed."""
    before = getattr(session.config, "_jarvis_data_before", None)
    if before is None:
        return
    after = _snapshot()
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    if changed:
        session.config.get_terminal_writer().line(
            f"\nFAILED: the test run changed the developer's real jarvis_data: {', '.join(changed)}", red=True)
        session.exitstatus = 1
