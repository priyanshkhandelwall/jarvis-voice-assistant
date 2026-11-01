"""R15 startup: the window first, voice in the background.
Measured before: dashboard usable 5.3 s after launch (voice start 3.5 s ran
first). After: 1.7 s, with voice ready about 6 s in the background."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from jarvis import startup_timing
from jarvis.dashboard.background_start import STARTING, BackgroundVoice, start_in_background
from jarvis.voice.audio_input import MicStatus

_APP = (Path(__file__).resolve().parent.parent / "jarvis" / "dashboard" / "app.py").read_text(encoding="utf-8")


class FakeVoice:
    """Voice stand-in for the dashboard (levels, microphone switch, close)."""
    def __init__(self) -> None:
        self.closed = False
        self.notes = ["a setup note"]
        self.mic_status = MicStatus(live=True, device_name="Headset (Contoso Wireless Earbuds)")

    def levels(self):
        """Fake levels for the tests."""
        return [0.5]

    def switch_microphone(self, name):
        """Fake switch microphone for the tests."""
        return MicStatus(live=True, device_name=name)

    def close(self) -> None:
        """Fake close for the tests."""
        self.closed = True


def test_before_voice_is_ready_the_dashboard_has_a_stand_in() -> None:
    """Before voice is ready the dashboard has a stand in."""
    v = BackgroundVoice()
    assert v.mic_status.live is False and v.mic_status.capture == STARTING
    assert v.levels() == [] and v.notes == []
    assert "still starting" in v.switch_microphone("x").note


def test_once_ready_it_is_the_real_voice_system() -> None:
    """Once ready it is the real voice system."""
    v, real = BackgroundVoice(), FakeVoice()
    v.set(real)
    assert v.ready.is_set() and v.mic_status.device_name == "Headset (Contoso Wireless Earbuds)" and v.levels() == [0.5]
    assert v.notes == ["a setup note"]


def test_closing_during_startup_closes_voice_when_it_arrives() -> None:
    """Closing during startup closes voice when it arrives."""
    v, real = BackgroundVoice(), FakeVoice()
    v.close()  # the window was closed while voice was still loading
    v.set(real)
    assert real.closed


def test_background_start_reports_each_step_in_order() -> None:
    """Background start reports each step in order."""
    reports, ready = [], []
    index = SimpleNamespace(started=False)
    index.start = lambda: setattr(index, "started", True)
    index.wait_ready = lambda timeout: True
    apps = SimpleNamespace(prefetch=lambda: None, apps=lambda wait_seconds=0: [1, 2, 3])
    v = BackgroundVoice()
    thread = start_in_background(v, start=FakeVoice, report=reports.append, file_index=index, store_apps=apps, on_ready=ready.append)
    thread.join(5)
    assert reports == [
        "starting voice: wake-word model and microphone…",
        "voice ready: listening on Headset (Contoso Wireless Earbuds)",
        "building the file index…",
        "app list ready: 3 Store apps",
        "file index ready",
    ]
    assert index.started and len(ready) == 1 and v.ready.is_set()


def test_voice_failing_to_start_is_reported_and_typing_still_works() -> None:
    """Voice failing to start is reported and typing still works."""
    reports = []

    def broken():
        raise RuntimeError("no audio device")

    v = BackgroundVoice()
    start_in_background(v, start=broken, report=reports.append).join(5)
    assert "voice could not start -- typed commands still work" in reports
    assert v.ready.is_set() and v.system is None


def test_voice_starts_after_the_dashboard_is_built() -> None:
    """Voice starts after the dashboard is built."""
    main = _APP[_APP.index("def main(page: ft.Page)"):]
    assert main.index("_show_splash(page)") < main.index("make_app_components()") < main.index("build(") < main.index("start_in_background(")
    assert "start_voice(" not in main[: main.index("build(")]  # never before the window is ready
    # The file index and the Store-app list wait until voice has loaded (no CPU contest).
    assert "file_index.start()" not in _APP[_APP.index("def make_app_components"):_APP.index("def _border_all")]
    assert "prefetch_apps=False" in _APP


def test_the_splash_is_jarvis_styled_and_blanks_the_title() -> None:
    """The splash is JARVIS styled and blanks the title."""
    splash = _APP[_APP.index("def _show_splash"):_APP.index("def main(page")]
    for needed in ('page.title = ""', "theme.BG_BASE", "theme.CYAN_ACCENT", '"J.A.R.V.I.S."', "theme.FONT_ORBITRON", "STARTING"):
        assert needed in splash
    assert 'ft.Text("Flet' not in splash  # no "Flet" on screen
    # Never shown or focused by JARVIS itself (no ShowWindow, no hidden start).
    assert "visible" not in splash and "FLET_APP_HIDDEN" not in _APP


def test_timings_are_written_once_each(tmp_path, monkeypatch) -> None:
    """Timings are written once each."""
    monkeypatch.setattr(startup_timing, "_MARKS", [])
    monkeypatch.setattr(startup_timing, "_WRITTEN", [0])
    log = tmp_path / "startup.log"
    startup_timing.mark("dashboard built", at=100.0)
    startup_timing.write(log)
    startup_timing.mark("voice ready (background)", at=104.5)
    startup_timing.write(log)
    startup_timing.write(log)  # nothing new: nothing written
    assert log.read_text(encoding="utf-8") == "100.000 dashboard built\n\n104.500 voice ready (background)\n\n"
