"""F8 screen capture: PASSIVE only.

- The pixels come from Pillow's ImageGrab.grab(all_screens=True), which
  copies what the desktop already shows (a BitBlt from the screen). It
  never touches a window.
- The windows come from read-only queries (EnumWindows, IsWindowVisible,
  IsIconic, GetWindowText, GetClassName, GetWindowRect, the owning
  process's name and DWM's "cloaked" flag). Nothing here focuses, moves,
  shows or sends input to any window.
- Every window is listed, so each line of text read off
  the screen belongs to the window it is in. The desktop ("Program
  Manager"), the taskbar and JARVIS's own window are marked as system
  windows and never read or named; a minimised window has no pixels, so
  only its title is mentioned.
- PROTECTED windows (password managers, banking titles, private/incognito
  browser windows, Windows Security; [screen] in settings) are BLACKED OUT
  inside capture(), before the image is returned: nothing else ever sees
  their pixels, not the OCR and not anything sent online. Their titles are
  never spoken or sent either.
- The image stays in memory only. It is never saved, and the caller drops
  it as soon as it is done.

Coordinates: both the window rectangles and the grab are taken with the
thread made per-monitor DPI aware, so they are in the same physical
pixels; the image's (0, 0) is the virtual screen's top-left corner, which
can be negative on a multi-monitor desktop.

She cannot see a sensitive page inside an ordinary browser tab whose title
doesn't match the list (spec F8): that is a documented limit.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator, List, Optional, Tuple

from jarvis.settings import SETTINGS

logger = logging.getLogger(__name__)

_CFG = SETTINGS["screen"]

Rect = Tuple[int, int, int, int]  # left, top, right, bottom


@dataclass(frozen=True)
class WindowInfo:
    """One top-level window: title, program, rectangle, and whether it is a system or minimised window."""
    title: str
    process: str  # e.g. "chrome.exe" ("" when it can't be read)
    rect: Rect  # screen coordinates, physical pixels
    # The desktop (Program Manager), the taskbar and JARVIS's own
    # window are listed only so their pixels are never read as another
    # window's; they are never spoken. Minimised windows have no pixels to
    # read; only their (unprotected) titles are mentioned.
    system: bool = False
    minimised: bool = False

    @property
    def protected(self) -> bool:
        """True for a private window (bank, password manager, ...): blacked out, its title never said."""
        return is_protected(self.title, self.process)

    @property
    def readable(self) -> bool:
        """True if its text may be read: not protected, not a system window, not minimised."""
        return not (self.protected or self.system or self.minimised)


@dataclass
class ScreenCapture:
    """One passive look, already blacked out. `image` is a PIL image in memory."""

    image: object
    windows: List[WindowInfo]  # top of the z-order first, protected ones included
    origin: Tuple[int, int]  # screen coordinates of the image's (0, 0)
    covered: int = 0  # how many protected windows were blacked out
    notes: List[str] = field(default_factory=list)

    @property
    def visible_windows(self) -> List[WindowInfo]:
        """The windows she may read and name: not protected, not the
        desktop, taskbar or JARVIS, not minimised. Top of the z-order first."""
        return [w for w in self.windows if w.readable]

    @property
    def minimised_windows(self) -> List[WindowInfo]:
        """The minimised windows that may be named (never protected or system ones)."""
        return [w for w in self.windows if w.minimised and not w.protected and not w.system]

    def window_at(self, x: float, y: float) -> Optional[WindowInfo]:
        """The topmost window under an image point (OCR word position)."""
        sx, sy = x + self.origin[0], y + self.origin[1]
        for window in self.windows:
            if window.minimised:
                continue
            left, top, right, bottom = window.rect
            if left <= sx < right and top <= sy < bottom:
                return window
        return None

    def release(self) -> None:
        """Free the pixels now (nothing is kept)."""
        close = getattr(self.image, "close", None)
        if close is not None:
            close()
        self.image = None


def is_protected(title: str, process: str) -> bool:
    """True if the title or program is on the protected list in settings."""
    lowered = (title or "").lower()
    if any(str(word).lower() in lowered for word in _CFG["protected_title_words"]):
        return True
    return (process or "").lower() in {str(p).lower() for p in _CFG["protected_processes"]}


def protected_category(title: str, process: str) -> str:
    """What KIND of window was blacked out ("banking",
    "password manager"), for the log -- never its title. From [screen]
    protected_categories; "private window" when no category names it."""
    lowered = f"{title or ''} {process or ''}".lower()
    for category, words in _CFG.get("protected_categories", {}).items():
        if any(str(w).lower() in lowered for w in words):
            return str(category)
    return "private window"


# -- reading the desktop (read-only Win32 calls) --------------------------------------------------


_DPI_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
_SM_XVIRTUALSCREEN, _SM_YVIRTUALSCREEN = 76, 77
_DWMWA_CLOAKED = 14


@contextmanager
def _dpi_aware() -> Iterator[None]:
    """Physical pixels for this thread only, then back as it was (Windows only)."""
    if sys.platform != "win32":
        yield
        return
    user32 = ctypes.windll.user32
    previous = None
    try:
        user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        previous = user32.SetThreadDpiAwarenessContext(_DPI_PER_MONITOR_AWARE_V2)
    except (AttributeError, OSError):
        pass
    try:
        yield
    finally:
        if previous:
            user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(previous))


def _is_cloaked(hwnd: int) -> bool:
    """Hidden UWP/virtual-desktop windows report as visible but are cloaked."""
    try:
        value = ctypes.c_int(0)
        ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value))
        return value.value != 0
    except (AttributeError, OSError):
        return False


def _own_process_ids() -> set:
    """JARVIS itself and her own window ([screen] own_processes: the Flet
    window). It was every process she started, so
    Notepad, Word or Paint opened by her ("open notepad", "open number one")
    were invisible to "what's on my screen"."""
    try:
        import os

        import psutil

        me = psutil.Process(os.getpid())
        own_names = {str(p).lower() for p in _CFG["own_processes"]}
        return {me.pid, *(child.pid for child in me.children(recursive=True) if child.name().lower() in own_names)}
    except Exception:
        return set()


def _process(hwnd: int) -> Tuple[str, int]:
    try:
        import psutil
        import win32process

        _thread, pid = win32process.GetWindowThreadProcessId(hwnd)
        return psutil.Process(pid).name(), pid
    except Exception:
        return "", -1


def is_system_window(title: str, class_name: str, process: str, own: bool = False) -> bool:
    """The desktop, the taskbar, JARVIS's own window ([screen]
    system_window_classes / system_window_titles / own_processes)."""
    if own or not title:
        return True
    if class_name in {str(c) for c in _CFG["system_window_classes"]}:
        return True
    if title.strip().lower() in {str(t).lower() for t in _CFG["system_window_titles"]}:
        return True
    return (process or "").lower() in {str(p).lower() for p in _CFG["own_processes"]}


def list_windows() -> List[WindowInfo]:
    """Every visible, not cloaked top-level window, top of the z-order
    first: readable ones, minimised ones (title only) and system ones (the
    desktop, the taskbar, JARVIS -- only so their pixels are never taken
    for another window's). Read-only: EnumWindows, IsWindowVisible,
    IsIconic, GetWindowText, GetClassName, GetWindowRect. Empty on macOS,
    which has no window list without extra permissions."""
    if sys.platform == "darwin":
        return []
    import win32gui

    found: List[WindowInfo] = []
    own = _own_process_ids()

    def visit(hwnd, _extra) -> bool:
        if not win32gui.IsWindowVisible(hwnd) or _is_cloaked(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd).strip()
        minimised = bool(win32gui.IsIconic(hwnd))
        if minimised and not title:
            return True
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if not minimised and (right - left <= 1 or bottom - top <= 1):
            return True
        process, pid = _process(hwnd)
        system = is_system_window(title, win32gui.GetClassName(hwnd), process, own=pid in own)
        found.append(WindowInfo(title, process, (left, top, right, bottom), system=system, minimised=minimised))
        return True

    win32gui.EnumWindows(visit, None)
    return found


def _virtual_origin() -> Tuple[int, int]:
    if sys.platform != "win32":
        return 0, 0
    user32 = ctypes.windll.user32
    return user32.GetSystemMetrics(_SM_XVIRTUALSCREEN), user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)


def grab_desktop():
    """Grab every monitor as one image (read-only, PIL ImageGrab)."""
    from PIL import ImageGrab

    return ImageGrab.grab(all_screens=True)


# -- the one capture function ---------------------------------------------------------------------


def black_out(image, windows: List[WindowInfo], origin: Tuple[int, int]) -> int:
    """Paint every protected window's whole rectangle black, in place.
    The full rectangle, even where another window covers part of it:
    covering too much is safe, covering too little is not."""
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    covered = 0
    for window in windows:
        if not window.protected or window.minimised:
            continue
        left, top, right, bottom = window.rect
        draw.rectangle((left - origin[0], top - origin[1], right - origin[0] - 1, bottom - origin[1] - 1), fill=(0, 0, 0))
        covered += 1
    return covered


def capture(
    grab: Callable[[], object] = grab_desktop,
    windows: Callable[[], List[WindowInfo]] = list_windows,
    origin: Callable[[], Tuple[int, int]] = _virtual_origin,
) -> ScreenCapture:
    """One passive look at the whole desktop, protected windows already
    black. Raises ImportError (Pillow/pywin32 missing) for the caller's
    missing-setup message (R14)."""
    with _dpi_aware():
        found = windows()
        image = grab()
        at = origin()
    image = image.convert("RGB")
    covered = black_out(image, found, at)  # before anything else can use it
    return ScreenCapture(image=image, windows=found, origin=at, covered=covered)


def downscale_jpeg(image, max_side: int, quality: int) -> Tuple[bytes, Tuple[int, int]]:
    """The image as it will be sent: at most `max_side` pixels on its long
    side, JPEG, in memory only (never the original resolution unless it is
    already that small)."""
    import io

    copy = image.copy()
    copy.thumbnail((max_side, max_side))
    out = io.BytesIO()
    copy.save(out, "JPEG", quality=quality, optimize=True)
    size = copy.size
    copy.close()
    return out.getvalue(), size
