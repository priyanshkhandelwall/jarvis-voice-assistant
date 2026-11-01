"""The test-wide data guard (tests/conftest.py) redirects every default data
path into the test's temporary folder -- also for modules a test imports
only later, inside the test (The e2e rig's first run
imported the dashboard lazily and wrote the real greeting_state.json,
debug.log and startup_timings.log, because the guard skipped modules not
yet imported when the test started)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture
def guard(request):
    """tests/conftest.py itself (tests/e2e has its own conftest.py, so the
    bare module name "conftest" may be either)."""
    target = Path(__file__).resolve().with_name("conftest.py")
    return next(p for p in request.config.pluginmanager.get_plugins()
                if getattr(p, "__file__", None) and Path(p.__file__).resolve() == target)


def test_every_default_data_path_is_redirected_even_if_imported_late(tmp_path, guard) -> None:
    """Every default data path is redirected even if imported late."""
    _DATA_DEFAULTS, _REAL_DATA = guard._DATA_DEFAULTS, guard._REAL_DATA
    for module_name, attribute, _relative in _DATA_DEFAULTS:
        assert module_name in sys.modules, f"{module_name} was not imported (and redirected) before the test"
        module = importlib.import_module(module_name)
        target = getattr(module, attribute)()
        assert tmp_path in target.parents, f"{module_name}.{attribute}() -> {target}, not under the test's folder"
        assert _REAL_DATA not in target.parents


def test_a_lazily_imported_dashboard_writes_nothing_real(tmp_path, guard) -> None:
    """A lazily imported dashboard writes nothing real."""
    _REAL_DATA = guard._REAL_DATA
    from jarvis import startup_timing
    from jarvis.actions import wake_greeting
    from jarvis.dashboard import app as app_mod
    from jarvis.logbook import debug_log

    for path in (wake_greeting.default_state_path(), app_mod.default_state_path(), startup_timing.log_path(),
                 debug_log.debug_log_path()):
        assert _REAL_DATA not in path.parents, path
