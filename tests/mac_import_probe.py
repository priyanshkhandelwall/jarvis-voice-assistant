"""Run as a script by tests/test_platform_mac.py: pretend to be macOS, refuse
every Windows-only module, import every JARVIS module, then print what was
refused (one JSON line). Never imported by pytest itself."""

import importlib
import importlib.abc
import json
import pkgutil
import sys
from pathlib import Path

# Standard-library modules pick their own platform code when first imported;
# on this (real) Windows Python they must load as Windows before the switch,
# or they would look for macOS-only C modules that don't exist here.
for stdlib in ("subprocess", "asyncio", "urllib.request", "ssl", "ctypes", "http.client", "tempfile", "shutil",
               "multiprocessing", "concurrent.futures", "uuid", "mimetypes", "socket", "selectors"):
    importlib.import_module(stdlib)

WINDOWS_ONLY = ("win32", "pythoncom", "pywintypes", "winreg", "winrt", "pycaw", "comtypes", "wmi",
                "screen_brightness_control", "ctypes.wintypes")
refused = []


class RefuseWindows(importlib.abc.MetaPathFinder):
    """Raise ImportError for every Windows-only module, and remember the attempt."""

    def find_spec(self, fullname, path=None, target=None):
        """Refuse Windows-only modules; let everything else load normally."""
        if any(fullname == name or fullname.startswith(name) for name in WINDOWS_ONLY):
            refused.append(fullname)
            raise ImportError(f"{fullname} is Windows-only (simulated macOS)")
        return None


for name in list(sys.modules):
    if any(name == w or name.startswith(w) for w in WINDOWS_ONLY):
        del sys.modules[name]
sys.meta_path.insert(0, RefuseWindows())
sys.platform = "darwin"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jarvis  # noqa: E402

failed = {}
for info in pkgutil.walk_packages(jarvis.__path__, "jarvis."):
    try:
        importlib.import_module(info.name)
    except Exception as exc:  # noqa: BLE001 -- reported, not hidden
        failed[info.name] = f"{type(exc).__name__}: {exc}"
loaded = sorted(n for n in sys.modules if any(n == w or n.startswith(w) for w in WINDOWS_ONLY))
print(json.dumps({"refused": sorted(set(refused)), "failed": failed, "windows_modules_loaded": loaded}))
