"""Measure how long JARVIS takes to start (R15), from launch to a usable
window, with a breakdown of each step.

Launches `python main.py` exactly as a user would, then watches -- with
read-only window queries only (EnumWindows, IsWindowVisible, IsIconic,
GetClassName, GetWindowText) -- for the dashboard window to appear, and
reads the app's own step marks from jarvis_data/logs/startup_timings.log.
It never focuses, moves, shows or sends input to any window. When the
window is ready (or the timeout passes) it stops the JARVIS it started.

Usage: venv\\Scripts\\python.exe tools\\measure_startup.py [--runs 3] [--timeout 90]
"""

from __future__ import annotations

import argparse
import statistics
import subprocess
import sys
import time
from pathlib import Path

import psutil
import win32gui

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from jarvis.startup_timing import log_path  # noqa: E402

FLUTTER_WINDOW_CLASS = "FLUTTER_RUNNER_WIN32_WINDOW"


def _windows():
    found = []

    def visit(hwnd, _):
        if win32gui.GetClassName(hwnd) == FLUTTER_WINDOW_CLASS:
            found.append((win32gui.IsWindowVisible(hwnd), win32gui.IsIconic(hwnd), win32gui.GetWindowText(hwnd)))
        return True

    win32gui.EnumWindows(visit, None)
    return found


def _jarvis_processes():
    """This project's JARVIS only: python/pythonw running main.py from the
    project folder (the windowless re-launch runs as plain "main.py"), and
    the flet.exe serving this project's dashboard assets."""
    procs = []
    for p in psutil.process_iter(["name", "cmdline"]):
        cmd = " ".join(p.info.get("cmdline") or []).lower()
        name = (p.info.get("name") or "").lower()
        try:
            if name in ("python.exe", "pythonw.exe") and "main.py" in cmd and Path(p.cwd()).resolve() == ROOT:
                procs.append(p)
            elif name == "flet.exe" and "jarvis\\dashboard\\assets" in cmd:
                procs.append(p)
        except psutil.Error:
            continue
    return procs


def _stop_jarvis() -> None:
    for p in _jarvis_processes():
        try:
            p.kill()
        except psutil.Error:
            pass


def _read_marks(since: float):
    path = log_path()
    if not path.exists():
        return []
    marks = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        stamp, _, step = line.partition(" ")
        try:
            t = float(stamp)
        except ValueError:
            continue
        if t >= since:
            marks.append((t, step))
    return marks


def one_run(timeout: float):
    """Start JARVIS once and wait for its window. Returns (start time, window events, startup marks)."""
    _stop_jarvis()
    time.sleep(1.0)
    t0 = time.time()
    subprocess.Popen([sys.executable, str(ROOT / "main.py")], cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    events = {}
    deadline = t0 + timeout
    while time.time() < deadline:
        now = time.time()
        wins = _windows()
        if wins and "window appears" not in events:
            events["window appears"] = now
            events["window first title"] = wins[0][2] or "(blank)"
        if any(v and not i for v, i, _ in wins) and "window shown, not minimised" not in events:
            events["window shown, not minimised"] = now
        marks = _read_marks(t0)
        steps = {step for _, step in marks}
        # Usable at "dashboard built"; wait on for the background steps
        # (voice, app list, file index) unless this build has none.
        if "background start finished" in steps or ("dashboard built" in steps and not any("background" in s for s in steps) and now - t0 > 15):
            break
        if "dashboard built" in steps and "window title after the dashboard" not in events:
            wins = _windows()
            events["window title after the dashboard"] = (wins[0][2] or "(blank)") if wins else "-"
        time.sleep(0.05)
    time.sleep(0.5)
    marks = _read_marks(t0)
    _stop_jarvis()
    return t0, events, marks


def main() -> None:
    """Measure startup over --runs runs and print the median time to each mark."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()
    totals = {}
    for run in range(1, args.runs + 1):
        t0, events, marks = one_run(args.timeout)
        print(f"\n--- run {run} (seconds from launch) ---")
        rows = [(t - t0, step) for t, step in marks] + [(t - t0, name) for name, t in events.items() if isinstance(t, float)]
        for seconds, step in sorted(rows):
            print(f"{seconds:7.2f}  {step}")
            totals.setdefault(step, []).append(seconds)
        print(f"         window title at first: {events.get('window first title', '-')!r}; after the dashboard: {events.get('window title after the dashboard', '-')!r}")
    print("\n--- median over runs (seconds from launch) ---")
    for step, values in sorted(totals.items(), key=lambda kv: statistics.median(kv[1])):
        print(f"{statistics.median(values):7.2f}  {step}  (runs: {', '.join(f'{v:.2f}' for v in values)})")


if __name__ == "__main__":
    main()
