"""F6/R8: the dashboard's bridge queue must never let a slow or stalled
UI consumer delay the controller -- see the developer's item A for step 4.
"""

from __future__ import annotations

import threading
import time


from jarvis.controller import Controller, Event, EventType, State
from jarvis.dashboard.bridge import DashboardBridge
from jarvis.settings import SETTINGS


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_push_and_drain_basic() -> None:
    """Push and drain basic."""
    bridge = DashboardBridge()
    c = Controller(start_state=State.AWAKE)
    from jarvis.controller.controller import TransitionLogEntry

    e1 = TransitionLogEntry(0.0, State.AWAKE, State.LISTENING, EventType.SPEECH_STARTED, True, "r1")
    e2 = TransitionLogEntry(0.0, State.LISTENING, State.AWAKE, EventType.LISTEN_TIMEOUT, True, "r2")
    bridge.push(e1)
    bridge.push(e2)
    drained = bridge.drain()
    assert drained == [e1, e2]
    assert bridge.drain() == []  # queue now empty


def test_queue_full_drops_oldest_and_warns(monkeypatch, caplog) -> None:
    """Queue full drops oldest and warns."""
    monkeypatch.setitem(SETTINGS["dashboard"], "ui_queue_max_size", 3)
    bridge = DashboardBridge()
    from jarvis.controller.controller import TransitionLogEntry

    entries = [
        TransitionLogEntry(float(i), State.AWAKE, State.AWAKE, EventType.LISTEN_TIMEOUT, True, f"r{i}")
        for i in range(5)
    ]
    with caplog.at_level("WARNING"):
        for e in entries:
            bridge.push(e)

    drained = bridge.drain()
    assert len(drained) == 3
    assert [e.reason for e in drained] == ["r2", "r3", "r4"]  # oldest two (r0, r1) dropped
    assert bridge.dropped_count == 2
    assert any("dropped" in r.message.lower() for r in caplog.records)


def test_push_never_blocks_even_when_never_drained(monkeypatch, caplog) -> None:
    """push() only ever uses put_nowait/get_nowait -- by construction it
    cannot block. Proven directly and deterministically: many pushes into
    a tiny, never-drained queue must all return near-instantly."""
    monkeypatch.setitem(SETTINGS["dashboard"], "ui_queue_max_size", 5)  # deliberately tiny
    # Each overflowing push logs a warning. Formatting 20k warnings under
    # pytest's log capture took 0.5-1.0 s on its own (measured),
    # which made the 1 s bound below flaky. Logging speed isn't what this
    # test proves, so the bridge logger is quietened for this test only.
    caplog.set_level("ERROR", logger="jarvis.dashboard.bridge")
    bridge = DashboardBridge()
    from jarvis.controller.controller import TransitionLogEntry

    entry = TransitionLogEntry(0.0, State.AWAKE, State.AWAKE, EventType.LISTEN_TIMEOUT, True, "r")

    start = time.monotonic()
    for _ in range(20_000):
        bridge.push(entry)  # never drained
    elapsed = time.monotonic() - start

    assert elapsed < 1.0  # 20k non-blocking pushes; a blocking implementation would hang here
    assert bridge.dropped_count == 20_000 - 5
    assert len(bridge.drain()) == 5  # capacity never exceeded


def test_stalled_dashboard_consumer_does_not_delay_stop(monkeypatch) -> None:
    """The core proof for item A: a dashboard that never drains at all
    must not slow down or block Stop. A short, bounded amount of
    real-thread pressure (not an unbounded spin) plus the direct proof
    above that push() cannot block together demonstrate this."""
    monkeypatch.setitem(SETTINGS["dashboard"], "ui_queue_max_size", 5)  # deliberately tiny
    bridge = DashboardBridge()
    c = Controller(start_state=State.AWAKE)
    c.set_transition_listener(bridge.push)
    c.start()
    try:
        stop_after = time.monotonic() + 0.2

        def hammer() -> None:
            while time.monotonic() < stop_after:
                c.submit(Event(EventType.SPEECH_STARTED))
                c.submit(Event(EventType.LISTEN_TIMEOUT))
                time.sleep(0.001)  # bounded rate -- proving the mechanism, not a stress test

        threads = [threading.Thread(target=hammer) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2.0)

        # Never drained the bridge during the hammering above -- simulates
        # a fully stalled dashboard consumer.
        start = time.monotonic()
        c.request_stop(source="test")
        assert _wait_until(lambda: c.state == State.AWAKE, timeout=2.0)
        elapsed = time.monotonic() - start

        assert elapsed < 1.0
        # The queue never grew past its cap -- old messages were dropped.
        assert bridge.dropped_count > 0
    finally:
        c.shutdown()


def test_dashboard_diag_log_lives_inside_the_project() -> None:
    """No JARVIS files outside the project (developer rule):
    the refresh-loop error log is in jarvis_data/logs/, not the system
    temp folder."""
    import tempfile
    from pathlib import Path

    from jarvis.dashboard import app

    project_root = Path(__file__).resolve().parents[1]
    path = app._DIAG_LOG_PATH.resolve()
    assert path.is_relative_to(project_root / "jarvis_data" / "logs")
    assert not path.is_relative_to(Path(tempfile.gettempdir()).resolve())
