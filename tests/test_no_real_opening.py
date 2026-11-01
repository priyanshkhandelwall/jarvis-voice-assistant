"""The conftest.py guard: no test may really open a browser, app, URL or
file (after Chrome opened on the developer's screen during
an unattended test run). Each opener is called here through the guard's
stub -- nothing reaches Windows -- and the record is cleared afterwards so
these tests themselves pass."""

from __future__ import annotations

import os
import subprocess
import sys
import webbrowser

import pytest

from conftest import RealOpenAttempted


@pytest.mark.parametrize(
    "call",
    [
        lambda: os.startfile("https://www.google.com"),
        lambda: subprocess.Popen(["chrome.exe", "https://www.google.com"]),
        lambda: subprocess.run(["notepad.exe"]),
        lambda: subprocess.Popen("start chrome", shell=True),
        lambda: webbrowser.open("https://www.google.com"),
        lambda: os.system("start chrome"),
    ],
    ids=["startfile", "popen-chrome", "run-notepad", "shell", "webbrowser", "os-system"],
)
def test_every_real_opener_is_refused_and_recorded(no_real_opening, call) -> None:
    """Every real opener is refused and recorded."""
    with pytest.raises(RealOpenAttempted):
        call()
    assert len(no_real_opening) == 1
    no_real_opening.clear()


def test_this_python_may_still_run_as_a_child_process() -> None:
    """This python may still run as a child process."""
    done = subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True, timeout=60)
    assert done.stdout.strip() == "ok"


def test_the_f3_and_f16b_executors_cannot_open_chrome_or_apps(no_real_opening, monkeypatch) -> None:
    """The real executors, unmocked: the launch is refused, nothing opens."""
    from jarvis.actions import open_app

    monkeypatch.setattr(open_app, "launch_and_verify_with_retry", lambda launch, *_a: (launch(), False))
    with pytest.raises(RealOpenAttempted):
        open_app.open_url_executor("open_url", {"url": "https://www.youtube.com", "browser_path": r"C:\chrome.exe"})
    with pytest.raises(RealOpenAttempted):
        open_app.open_app_executor("open_app", {"target": "notepad.exe"})
    assert len(no_real_opening) == 2
    no_real_opening.clear()
