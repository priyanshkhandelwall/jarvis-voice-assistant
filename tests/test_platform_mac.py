"""macOS support, tested on Windows by simulating `sys.platform == "darwin"`.

Every macOS command (`open`, `osascript`, `pmset`, `networksetup`) goes
through jarvis.platform.mac._run, which these tests replace with a
recorder: nothing is ever run for real. A real Mac was not available, so
these tests check the commands JARVIS builds and the messages it gives,
not how macOS reacts to them.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

import jarvis.platform as platform_layer
from jarvis.platform import mac
from jarvis.settings import SETTINGS
from jarvis.system.backend import NoDevice

PROJECT = Path(__file__).resolve().parents[1]


class Recorder:
    """Stands in for mac._run: records each command and answers with a fixed result."""

    def __init__(self, returncode: int = 0, stdout: str = "", answers=None) -> None:
        self.calls = []
        self.returncode = returncode
        self.stdout = stdout
        self.answers = answers or {}
        self.done = threading.Event()

    def __call__(self, args):
        """Record the command; reply with the answer for its first words, or the default."""
        self.calls.append(list(args))
        self.done.set()
        for prefix, (code, out) in self.answers.items():
            if tuple(args[: len(prefix)]) == prefix:
                return subprocess.CompletedProcess(args, code, out, "")
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, "")


@pytest.fixture
def on_mac(monkeypatch):
    """Simulated macOS: sys.platform is "darwin" and every command is recorded, never run."""
    monkeypatch.setattr(sys, "platform", "darwin")
    recorder = Recorder()
    monkeypatch.setattr(mac, "_run", recorder)
    return recorder


# -- importing JARVIS on macOS -----------------------------------------------------------------------


def test_importing_every_jarvis_module_on_macos_loads_no_windows_module() -> None:
    """Every jarvis module imports with sys.platform "darwin" while all Windows-only modules are refused."""
    done = subprocess.run([sys.executable, str(PROJECT / "tests" / "mac_import_probe.py")], capture_output=True,
                          text=True, timeout=300, cwd=str(PROJECT))
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert result == {"refused": [], "failed": {}, "windows_modules_loaded": []}


def test_the_platform_is_chosen_from_sys_platform(on_mac, monkeypatch) -> None:
    """MacPlatform on darwin, WindowsPlatform on win32."""
    assert platform_layer.is_mac() and platform_layer.current().name == "mac"
    monkeypatch.setattr(sys, "platform", "win32")
    assert platform_layer.is_windows() and platform_layer.current().name == "windows"


def test_mac_capabilities_are_declared_honestly() -> None:
    """No global hotkeys, screen text, window list or brightness on macOS."""
    p = mac.MacPlatform()
    assert (p.global_hotkeys, p.screen_text, p.window_list, p.brightness) == (False, False, False, False)


# -- apps, files, URLs ----------------------------------------------------------------------------------


def test_apps_are_found_in_the_applications_folders(tmp_path) -> None:
    """Every .app bundle directly in the folders is listed once, by name."""
    first, second = tmp_path / "Applications", tmp_path / "System"
    for folder, names in ((first, ["Safari.app", "Notes.app", "README.txt"]), (second, ["Notes.app", "Calculator.app"])):
        folder.mkdir()
        for name in names:
            (folder / name).mkdir() if name.endswith(".app") else (folder / name).write_text("x")
    found = mac.list_apps([first, second, tmp_path / "missing"])
    assert [n for n, _ in found] == ["Notes", "Safari", "Calculator"]
    assert found[1][1] == str(first / "Safari.app")


def test_app_folders_include_the_users_own(tmp_path) -> None:
    """/Applications, /System/Applications and ~/Applications."""
    folders = [str(p).replace("\\", "/") for p in mac.app_folders(tmp_path)]
    assert "/Applications" in folders and "/System/Applications" in folders
    assert folders[-1] == str(tmp_path / "Applications").replace("\\", "/")


def test_the_app_finder_uses_the_mac_apps(on_mac, monkeypatch) -> None:
    """On macOS the F3 candidates are the .app bundles, not Start Menu shortcuts."""
    from jarvis.actions import app_finder

    monkeypatch.setattr(mac.MacPlatform, "installed_apps", lambda self: [("Safari", "/Applications/Safari.app")])
    apps = app_finder.enumerate_installed_apps()
    assert [(a.display_name, a.target, a.kind) for a in apps] == [("Safari", "/Applications/Safari.app", "app")]
    assert app_finder.find_app("safari", candidates=apps)[0].target == "/Applications/Safari.app"


def test_chrome_is_found_as_an_app_bundle(on_mac, monkeypatch, tmp_path) -> None:
    """~/Applications/Google Chrome.app counts; no Chrome gives None."""
    monkeypatch.setattr(mac.MacPlatform, "home", staticmethod(lambda: tmp_path))
    assert mac.MacPlatform().find_chrome() in (None, "/Applications/Google Chrome.app")
    (tmp_path / "Applications" / "Google Chrome.app").mkdir(parents=True)
    found = mac.MacPlatform().find_chrome()
    assert found is not None and found.endswith("Google Chrome.app")


def test_open_app_uses_open_dash_a(on_mac) -> None:
    """`open -a <app>`, as a fixed argument list."""
    assert mac.MacPlatform().open_app("/Applications/Safari.app") is True
    assert on_mac.calls == [["open", "-a", "/Applications/Safari.app"]]


def test_open_path_and_show_in_finder(on_mac) -> None:
    """`open <file>` opens it; `open -R <file>` only shows it in Finder."""
    p = mac.MacPlatform()
    p.open_path("/Users/me/report.pdf")
    p.show_in_file_manager("/Users/me/run.sh")
    assert on_mac.calls == [["open", "/Users/me/report.pdf"], ["open", "-R", "/Users/me/run.sh"]]


def test_open_url_in_chrome_then_the_default_browser(on_mac) -> None:
    """Chrome first; when that fails, the default browser."""
    on_mac.answers = {("open", "-a"): (1, "")}
    assert mac.MacPlatform().open_url("https://www.youtube.com", "/Applications/Google Chrome.app") is True
    assert on_mac.calls == [["open", "-a", "/Applications/Google Chrome.app", "https://www.youtube.com"],
                            ["open", "https://www.youtube.com"]]


def test_a_failed_open_is_reported_not_raised(on_mac) -> None:
    """A non-zero exit or a missing `open` returns False."""
    on_mac.returncode = 1
    assert mac.MacPlatform().open_app("Nothing") is False

    def missing(args):
        raise FileNotFoundError("open")

    mac._run = missing  # restored by monkeypatch in the fixture
    assert mac.MacPlatform().open_path("/x") is False


def test_the_open_app_executor_on_mac(on_mac) -> None:
    """F3's executor launches with `open -a` and trusts its exit status (R12)."""
    from jarvis.actions.open_app import open_app_executor

    ok, said = open_app_executor("open_app", {"target": "/Applications/Safari.app", "display_name": "Safari"})
    assert (ok, said) == (True, "Opened Safari.")
    assert on_mac.calls == [["open", "-a", "/Applications/Safari.app"]]


def test_the_open_app_executor_retries_once_then_says_so(on_mac) -> None:
    """Two failed `open` calls: one retry, then the plain failure sentence."""
    from jarvis.actions.open_app import open_app_executor

    on_mac.returncode = 1
    ok, said = open_app_executor("open_app", {"target": "/Applications/Gone.app", "display_name": "Gone"})
    assert (ok, said) == (False, "I couldn't confirm Gone opened.") and len(on_mac.calls) == 2


def test_the_open_url_executor_on_mac(on_mac) -> None:
    """F16b/F18's URL opener uses Chrome through `open -a`."""
    from jarvis.actions.open_app import open_url_executor

    ok, said = open_url_executor("open_url", {"url": "https://www.youtube.com", "browser_path": "/Applications/Google Chrome.app",
                                              "display_name": "YouTube"})
    assert ok and on_mac.calls == [["open", "-a", "/Applications/Google Chrome.app", "https://www.youtube.com"]]


@pytest.mark.parametrize("name", ["setup.sh", "tool.command", "Thing.app", "build.py"])
def test_scripts_and_apps_are_only_shown_in_finder(on_mac, tmp_path, name) -> None:
    """A script or app is never run: Finder shows it selected."""
    from jarvis.actions.find_file import open_file_executor

    path = tmp_path / name
    path.write_text("x")
    ok, said = open_file_executor("open_file", {"path": str(path), "display_name": name})
    assert ok and on_mac.calls == [["open", "-R", str(path)]]
    assert said == SETTINGS["file_finder"]["shown_not_run_mac"].format(name=name)


def test_a_document_opens_with_its_app(on_mac, tmp_path) -> None:
    """`open <file>` for a document."""
    from jarvis.actions.find_file import open_file_executor

    path = tmp_path / "report.pdf"
    path.write_text("x")
    assert open_file_executor("open_file", {"path": str(path), "display_name": "report.pdf"}) == (True, "Opened report.pdf.")
    assert on_mac.calls == [["open", str(path)]]


def test_a_file_no_app_opens_is_shown_in_finder(on_mac, tmp_path) -> None:
    """`open` fails twice (no app for it): the file is shown in Finder instead."""
    from jarvis.actions.find_file import open_file_executor

    path = tmp_path / "data.xyz"
    path.write_text("x")
    on_mac.answers = {("open", "-R"): (0, "")}
    on_mac.returncode = 1
    ok, said = open_file_executor("open_file", {"path": str(path), "display_name": "data.xyz"})
    assert ok and said == SETTINGS["file_finder"]["shown_no_app_mac"].format(name="data.xyz")
    assert on_mac.calls[-1] == ["open", "-R", str(path)]


# -- system settings (F22) ----------------------------------------------------------------------------


def test_volume_and_mute_use_osascript(on_mac) -> None:
    """Read and set volume and mute with osascript."""
    on_mac.answers = {("osascript", "-e", "output volume of (get volume settings)"): (0, "40\n"),
                      ("osascript", "-e", "output muted of (get volume settings)"): (0, "false\n")}
    system = mac.MacSystem()
    assert system.get_volume() == 40 and system.get_mute() is False
    system.set_volume(130)
    system.set_mute(True)
    system.set_mute(False)
    assert on_mac.calls[2:] == [["osascript", "-e", "set volume output volume 100"],
                                ["osascript", "-e", "set volume with output muted"],
                                ["osascript", "-e", "set volume without output muted"]]


def test_an_osascript_failure_is_no_device(on_mac) -> None:
    """A failed osascript call becomes NoDevice (a plain reply), never a crash."""
    on_mac.returncode = 1
    with pytest.raises(NoDevice):
        mac.MacSystem().get_volume()


def test_volume_is_read_back_after_setting_it(on_mac) -> None:
    """R12: F22 reads the volume back and reports the real value."""
    from jarvis.system.commands import SystemCommands

    state = {"volume": 30}

    def fake(args):
        on_mac.calls.append(list(args))
        script = args[-1]
        if script.startswith("set volume output volume"):
            state["volume"] = int(script.split()[-1])
        out = str(state["volume"]) if script == "output volume of (get volume settings)" else "false"
        return subprocess.CompletedProcess(args, 0, out, "")

    mac._run = fake
    system = SystemCommands(mac.MacSystem(), clock=lambda: 0.0, sleep=lambda s: None)
    assert system._set_volume("set_volume", {"percent": 70}) == (True, "Volume is now 70%.")


def test_brightness_says_it_is_not_supported_on_macos(on_mac) -> None:
    """No free built-in way: the plain macOS sentence, never a crash."""
    from jarvis.system.commands import SystemCommands

    system = SystemCommands(mac.MacSystem(), clock=lambda: 0.0, sleep=lambda s: None)
    assert system._set_brightness("set_brightness", {"percent": 50}) == (False, "Changing brightness isn't supported on macOS.")


def test_the_default_backend_on_macos_is_mac_system(on_mac) -> None:
    """jarvis.system.backend.default_backend() picks MacSystem on darwin."""
    from jarvis.system.backend import default_backend

    assert isinstance(default_backend(), mac.MacSystem)


def test_wifi_device_is_read_from_networksetup() -> None:
    """The Wi-Fi port's device, from -listallhardwareports output."""
    ports = "Hardware Port: Ethernet\nDevice: en1\n\nHardware Port: Wi-Fi\nDevice: en0\nEthernet Address: aa\n"
    assert mac.wifi_device(ports) == "en0" and mac.wifi_device("Hardware Port: Ethernet\nDevice: en1\n") is None


def test_wifi_on_off_uses_networksetup(on_mac) -> None:
    """Find the device, then -getairportpower / -setairportpower."""
    on_mac.answers = {("networksetup", "-listallhardwareports"): (0, "Hardware Port: Wi-Fi\nDevice: en0\n"),
                      ("networksetup", "-getairportpower"): (0, "Wi-Fi Power (en0): On\n")}
    system = mac.MacSystem()
    assert system.get_wifi() is True
    system.set_wifi(False)
    assert on_mac.calls[-1] == ["networksetup", "-setairportpower", "en0", "off"]


def test_no_wifi_hardware_is_no_device(on_mac) -> None:
    """No Wi-Fi port: NoDevice, so F22 says there is no Wi-Fi radio."""
    on_mac.stdout = "Hardware Port: Ethernet\nDevice: en1\n"
    with pytest.raises(NoDevice):
        mac.MacSystem().get_wifi()


def test_lock_sleeps_the_display(on_mac) -> None:
    """Lock = `pmset displaysleepnow`."""
    assert mac.MacSystem().lock() is True and on_mac.calls == [["pmset", "displaysleepnow"]]


@pytest.mark.parametrize("reboot, verb", [(True, "restart"), (False, "shut down")])
def test_restart_and_shutdown_ask_system_events_never_forced(on_mac, reboot, verb) -> None:
    """After the delay, System Events restarts / shuts down (apps quit normally)."""
    mac.MacSystem().schedule_shutdown(0, reboot, "bye")
    assert on_mac.done.wait(5)
    assert on_mac.calls == [["osascript", "-e", f'tell application "System Events" to {verb}']]


def test_a_scheduled_shutdown_can_be_cancelled(on_mac) -> None:
    """Abort before the delay ends: nothing runs."""
    system = mac.MacSystem()
    system.schedule_shutdown(60, False, "bye")
    system.abort_shutdown()
    system.abort_shutdown()  # nothing pending: no error
    assert on_mac.calls == []


def test_window_titles_are_not_read_on_macos() -> None:
    """No window list on macOS."""
    assert mac.MacSystem().window_titles() == []


# -- screen, hotkeys, messages ----------------------------------------------------------------------------


def test_screen_text_reading_says_it_needs_windows(on_mac) -> None:
    """"what's on my screen" / "find X on my screen": the plain macOS sentence."""
    from jarvis.screen.commands import ScreenCommands
    from jarvis.screen.ocr import ocr_available

    message = "Reading screen text needs Windows; try 'explain my screen'."
    assert ocr_available() == message
    screen = ScreenCommands(capture_fn=lambda: pytest.fail("no capture for screen text on macOS"))
    assert screen.capture_executor("capture_screen", {"purpose": "summary"}) == (False, message, message)
    assert screen.capture_executor("capture_screen", {"purpose": "find", "word": "x"})[1] == message


def test_capture_on_macos_has_no_window_list_and_no_offset(on_mac) -> None:
    """The whole screen, no Windows calls: no windows, origin (0, 0)."""
    from PIL import Image

    from jarvis.screen import capture as capture_mod

    assert capture_mod.list_windows() == [] and capture_mod._virtual_origin() == (0, 0)
    shot = capture_mod.capture(grab=lambda: Image.new("RGB", (40, 30)))
    assert shot.windows == [] and shot.origin == (0, 0) and shot.covered == 0 and shot.image.size == (40, 30)


def test_explain_my_screen_still_prepares_a_preview_on_macos(on_mac) -> None:
    """The image goes into the R4 preview (no screen text); nothing is sent before "confirm"."""
    from PIL import Image

    from jarvis.screen.capture import capture as real_capture
    from jarvis.screen.commands import ScreenCommands
    from jarvis.screen.ocr import read_text

    sent = []
    groq = types.SimpleNamespace(explain_screen=lambda *a, **k: sent.append(a) or "x")
    screen = ScreenCommands(groq, capture_fn=lambda: real_capture(grab=lambda: Image.new("RGB", (64, 48), "white")),
                            ocr_fn=read_text)
    request = screen.matcher("explain my screen")
    assert request.action_name == "send_screen_online" and sent == []
    assert screen.preview(request.params["preview_id"]).jpeg is not None


def test_global_hotkeys_are_not_registered_on_macos(on_mac) -> None:
    """No global hotkeys on macOS (they would need extra permissions): every binding reports not registered."""
    from jarvis.voice.hotkeys import GlobalHotkeys, HotkeyBinding

    statuses = GlobalHotkeys([HotkeyBinding("stop", "Ctrl+Shift+X", lambda: None)]).start()
    assert [(s.registered, s.message) for s in statuses] == [(False, None)]


@pytest.mark.parametrize("key", ["mic_none", "mic_blocked", "camera_not_found", "camera_privacy_blocked"])
def test_mic_and_camera_messages_name_the_macos_privacy_settings(on_mac, monkeypatch, key) -> None:
    """macOS sentences point at System Settings, Privacy and Security; Windows keeps its own."""
    assert "System Settings, Privacy and Security" in platform_layer.setup_message(key)
    monkeypatch.setattr(sys, "platform", "win32")
    assert platform_layer.setup_message(key) == SETTINGS["setup_messages"][key]


def test_a_camera_that_wont_open_on_macos_names_the_privacy_setting(on_mac) -> None:
    """Permission denied looks like no camera to OpenCV: the macOS sentence says where to allow it."""
    from jarvis.vision.camera import Camera, CameraProblem

    camera = Camera(opener=lambda index: None, blocked=lambda: False)
    with pytest.raises(CameraProblem) as problem:
        camera._open_any()
    assert str(problem.value) == SETTINGS["setup_messages"]["camera_not_found_mac"]


def test_main_refuses_an_unsupported_system_cleanly(monkeypatch, capsys) -> None:
    """Linux: one clear sentence and exit code 1, no traceback."""
    import main

    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(SystemExit) as stop:
        main.main()
    assert stop.value.code == 1 and "Windows 10/11 and macOS" in capsys.readouterr().out


def test_the_macos_scripts_are_unix_text_with_the_setup_steps() -> None:
    """setup.sh / run.sh: LF line endings, Python 3.11 check, venv, pip, models, .env, then main.py."""
    setup = (PROJECT / "setup.sh").read_bytes()
    run = (PROJECT / "run.sh").read_bytes()
    assert b"\r" not in setup and b"\r" not in run
    assert setup.startswith(b"#!/usr/bin/env bash") and run.startswith(b"#!/usr/bin/env bash")
    text = setup.decode()
    for step in ("sys.version_info[:2] == (3, 11)", "-m venv venv", "pip install --disable-pip-version-check -r requirements.txt",
                 "scripts/download_models.py", 'cp ".env.example" ".env"'):
        assert step in text, step
    assert "venv/bin/python main.py" in run.decode()


def test_windows_only_packages_carry_the_windows_marker() -> None:
    """pip skips them on macOS; every other package installs everywhere."""
    lines = [l.strip() for l in (PROJECT / "requirements.txt").read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.startswith("#")]
    windows_only = ("pywin32", "pypiwin32", "pycaw", "comtypes", "WMI", "winrt-", "screen-brightness-control")
    for line in lines:
        marked = line.endswith('; sys_platform == "win32"')
        assert marked == line.startswith(windows_only), line


def test_city_times_say_plainly_they_are_not_on_macos(on_mac, monkeypatch) -> None:
    """Without Windows' time-zone data, a city's time gets the plain macOS sentence."""
    import builtins
    from datetime import datetime, timezone

    from jarvis.actions import clock

    city = next(iter(SETTINGS["clock"]["cities"]))
    real_import = builtins.__import__

    def no_win32timezone(name, *args, **kwargs):
        if name == "win32timezone":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_win32timezone)
    assert clock._city_time(city, datetime.now(timezone.utc)) == SETTINGS["setup_messages"]["clock_timezones_missing_mac"]


def test_setup_sh_falls_back_to_the_unpinned_mac_requirements() -> None:
    """If the pinned install fails, setup.sh retries with requirements-mac.txt and names what is unpinned."""
    text = (PROJECT / "setup.sh").read_text(encoding="utf-8")
    assert "if ! venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt; then" in text
    assert "-r requirements-mac.txt" in text
    assert "opencv-python-headless, scipy, av, onnxruntime, scikit-learn" in text


def test_requirements_mac_differs_only_by_the_five_unpinned_packages() -> None:
    """requirements-mac.txt = requirements.txt with exactly five packages unpinned (plus its header)."""
    pinned = [l for l in (PROJECT / "requirements.txt").read_text(encoding="utf-8").splitlines() if l and not l.startswith("#")]
    mac_lines = [l for l in (PROJECT / "requirements-mac.txt").read_text(encoding="utf-8").splitlines() if l and not l.startswith("#")]
    unpinned = {"opencv-python-headless", "scipy", "av", "onnxruntime", "scikit-learn"}
    assert len(pinned) == len(mac_lines)
    for a, b in zip(pinned, mac_lines):
        assert b == (a.split("==")[0] if a.split("==")[0] in unpinned else a)
    assert unpinned <= set(mac_lines)
