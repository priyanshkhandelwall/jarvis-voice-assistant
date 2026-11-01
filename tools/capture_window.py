"""Passive screenshot of the JARVIS dashboard window, for before/after evidence.

Uses PrintWindow(PW_RENDERFULLCONTENT): the window draws itself into an
off-screen bitmap, so it works while the window is behind other windows.
It never focuses, raises, moves or sends input to any window (developer
rule: no input-injection or focus APIs). A minimised window captures black;
if that happens, ask the developer to restore it.

Usage: venv\\Scripts\\python.exe tools\\capture_window.py <out.png> [--after-line <text> --log <file>]
With --after-line, waits until <text> appears in <file>, then captures.
With --wait-window, waits (up to --timeout) for the dashboard window to
appear -- found by enumerating windows, nothing else -- then captures
after --delay seconds.
"""

from __future__ import annotations

import argparse
import ctypes
import struct
import time
import zlib
from ctypes import wintypes
from pathlib import Path

import win32gui
import win32ui

PW_RENDERFULLCONTENT = 2
FLUTTER_WINDOW_CLASS = "FLUTTER_RUNNER_WIN32_WINDOW"


def find_dashboard_window() -> int:
    """The handle of the largest visible JARVIS dashboard window (by title). Exits with a message if there
    is none."""
    found: list[tuple[int, int]] = []

    def visit(hwnd: int, _) -> bool:
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetClassName(hwnd) == FLUTTER_WINDOW_CLASS:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            found.append(((right - left) * (bottom - top), hwnd))
        return True

    win32gui.EnumWindows(visit, None)
    if not found:
        raise SystemExit("JARVIS dashboard window not found. Start JARVIS first.")
    return max(found)[1]


def _png(width: int, height: int, bgra: bytes) -> bytes:
    rows = bytearray()
    stride = width * 4
    for y in range(height):
        row = bgra[y * stride:(y + 1) * stride]
        rows.append(0)
        for x in range(0, stride, 4):
            rows += bytes((row[x + 2], row[x + 1], row[x]))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b"")


def capture(hwnd: int, out: Path) -> None:
    """Save a passive screenshot of window `hwnd` to `out` (PrintWindow, never focusing it)."""
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    window_dc = win32gui.GetWindowDC(hwnd)
    src = win32ui.CreateDCFromHandle(window_dc)
    mem = src.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(src, width, height)
    mem.SelectObject(bitmap)
    try:
        if not ctypes.windll.user32.PrintWindow(wintypes.HWND(hwnd), wintypes.HDC(mem.GetSafeHdc()), PW_RENDERFULLCONTENT):
            raise SystemExit("PrintWindow failed.")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(_png(width, height, bitmap.GetBitmapBits(True)))
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        mem.DeleteDC()
        src.DeleteDC()
        win32gui.ReleaseDC(hwnd, window_dc)


def main() -> None:
    # Real pixel coordinates on a scaled (e.g. 125%) display; without this
    # Windows reports a scaled-down window size and the capture is cropped.
    """Command line: capture the dashboard window to the given PNG path."""
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    parser.add_argument("--after-line")
    parser.add_argument("--log", type=Path)
    parser.add_argument("--delay", type=float, default=0.0, help="extra seconds to wait after the line appears")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--wait-window", action="store_true", help="wait for the dashboard window to appear first")
    args = parser.parse_args()
    if args.wait_window:
        deadline = time.monotonic() + args.timeout
        while True:
            try:
                find_dashboard_window()
                break
            except SystemExit:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        if not args.after_line:
            time.sleep(args.delay)
    if args.after_line:
        deadline = time.monotonic() + args.timeout
        while not (args.log.exists() and args.after_line in args.log.read_text(encoding="utf-8")):
            if time.monotonic() > deadline:
                raise SystemExit(f"timed out waiting for {args.after_line!r}")
            time.sleep(0.02)
        time.sleep(args.delay)
    capture(find_dashboard_window(), args.out)
    print(f"{time.strftime('%H:%M:%S')} captured {args.out}")


if __name__ == "__main__":
    main()
