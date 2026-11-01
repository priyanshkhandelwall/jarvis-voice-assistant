"""Shared, session-wide pieces of the e2e suite: the Kokoro engine, the
wake-word and Whisper models (loaded once, reset between tests), the Groq
gate (budget + recordings) and the results file the report is built from.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime

import pytest

from .e2e_rig import RESULTS, GroqGate, Jarvis, SharedKokoro, root_guard

# Up to 20 real Groq calls in total, across every run of this suite
# (counted in results/groq_budget.json).
GROQ_MAX_REAL_CALLS = int(os.environ.get("JARVIS_E2E_GROQ_MAX", "20"))

_RESULTS: list = []
_LOCK = threading.Lock()


@pytest.fixture(scope="session")
def shared_kokoro():
    """Session fixture: one Kokoro voice shared by every e2e test (loading it is slow)."""
    return SharedKokoro()


@pytest.fixture(scope="session")
def models():
    """Session fixture: the real wake-word and Whisper models, loaded once and reused."""
    from jarvis.voice import stt, wake_word

    real_wake, real_whisper = wake_word._default_model_factory, stt._default_model_factory
    cache: dict = {}

    def wake():
        if "wake" not in cache:
            cache["wake"] = real_wake()
        else:
            cache["wake"].reset()
        return cache["wake"]

    def whisper():
        if "whisper" not in cache:
            cache["whisper"] = real_whisper()
        return cache["whisper"]

    return {"wake": wake, "whisper": whisper}


@pytest.fixture(scope="session")
def gate():
    """Session fixture: the Groq gate shared by every e2e test -- it counts real Groq calls and stops
    the run before it goes over GROQ_MAX_REAL_CALLS."""
    return GroqGate(RESULTS, GROQ_MAX_REAL_CALLS)


@pytest.fixture
def launch(monkeypatch, tmp_path, shared_kokoro, gate, models, request):
    """launch(path=..., voice=..., noise=..., first_wake=...) -> a running JARVIS.
    Closed through the window's close button at the end of the test."""
    started: list = []

    def make(path: str = "typed", voice: str = "af_bella", noise: str = "light", first_wake: bool = False, scope: str = ""):
        jarvis = Jarvis(monkeypatch, tmp_path, shared_kokoro, gate, models, path=path, voice=voice, noise=noise,
                        first_wake=first_wake, scope=scope or request.node.name, guard=root_guard(request.config))
        started.append(jarvis)
        return jarvis

    yield make
    for jarvis in started:
        try:
            jarvis.close()
        except Exception:  # a failed close must not hide the test's own result
            pass


def record(**row) -> None:
    """One result row for the report (written at the end of the session)."""
    row.setdefault("at", datetime.now().isoformat(timespec="seconds"))
    with _LOCK:
        _RESULTS.append(row)


PROGRESS = RESULTS.parents[2] / "docs" / "reports" / "e2e-progress.txt"
_PROGRESS = {"done": 0, "failed": 0, "total": 0, "started": None}


def pytest_collection_finish(session) -> None:
    """Count the e2e tests collected, for the progress file (never in the normal suite)."""
    _PROGRESS.update(total=len(session.items), started=datetime.now(), done=0, failed=0)
    _PROGRESS["total"] = len([i for i in session.items if i.get_closest_marker("e2e") is not None])
    if _PROGRESS["total"]:  # only when e2e tests run, never in the normal suite
        PROGRESS.parent.mkdir(parents=True, exist_ok=True)
        PROGRESS.write_text(f"e2e run started {datetime.now():%H:%M:%S}, {_PROGRESS['total']} tests\n", encoding="utf-8")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Record each e2e test's outcome and error for the results file."""
    outcome = yield
    report = outcome.get_result()
    if item.get_closest_marker("e2e") is None:
        return
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        item._e2e_outcome = report.outcome
        item._e2e_error = str(report.longrepr).splitlines()[-1][:400] if report.failed and report.longrepr else ""
        # One line per finished test, so progress can be read without asking.
        _PROGRESS["done"] += 1
        _PROGRESS["failed"] += report.outcome == "failed"
        elapsed = (datetime.now() - _PROGRESS["started"]).total_seconds() if _PROGRESS["started"] else 0
        left = elapsed / _PROGRESS["done"] * (_PROGRESS["total"] - _PROGRESS["done"])
        first_error = item._e2e_error.replace("\n", " ")[:160]
        with PROGRESS.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%H:%M:%S}  {_PROGRESS['done']}/{_PROGRESS['total']}  {report.outcome.upper():7} "
                    f"{item.name}  ({report.duration:.0f} s; {_PROGRESS['failed']} failed so far; ~{left / 60:.0f} min left)"
                    + (f"  {first_error}" if report.failed else "") + "\n")


def pytest_sessionfinish(session, exitstatus) -> None:
    """Write the e2e results file at the end of a run."""
    if not _RESULTS and not any(getattr(i, "_e2e_outcome", None) for i in session.items):
        return
    outcomes = {i.nodeid: {"outcome": getattr(i, "_e2e_outcome", "not run"), "error": getattr(i, "_e2e_error", "")}
                for i in session.items if i.get_closest_marker("e2e") is not None}
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / "e2e_results.json"
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {"rows": [], "outcomes": {}}
    # Keep earlier runs' rows for tests that did not run this time.
    ran = {r.get("nodeid") for r in _RESULTS}
    rows = [r for r in previous.get("rows", []) if r.get("nodeid") not in ran] + _RESULTS
    merged = dict(previous.get("outcomes", {}))
    merged.update(outcomes)
    path.write_text(json.dumps({"rows": rows, "outcomes": merged}, indent=1, default=str), encoding="utf-8")
