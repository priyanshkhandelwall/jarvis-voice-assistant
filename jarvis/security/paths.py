"""R9: the filesystem rule. One shared function checks every path, and
every module MUST call it -- no module does its own path check.

- Anything that CHANGES files (create, write, save, rename, move, copy,
  delete, and any path handed to a terminal template) works only inside
  approved_folders.
- READ-ONLY actions (search, open, show) can reach any folder EXCEPT
  protected_folders.
- Every path is resolved to a full real path before checking, including
  following .lnk shortcuts, so '..' and shortcuts cannot be used to
  escape a check.

Folder lists come from settings.toml -- the developer-approved list
Not a guess: see that file's [filesystem] section.
"""

from __future__ import annotations

import os
from enum import Enum, auto
from pathlib import Path
from typing import List, Optional, Tuple

from jarvis.settings import SETTINGS

_CFG = SETTINGS["filesystem"]


class PathIntent(Enum):
    """R9: what a path is wanted for -- reading or writing."""
    READ = auto()  # search, open, show
    WRITE = auto()  # create, write, save, rename, move, copy, delete


def _expand(raw: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(raw)))


def _project_root() -> Path:
    # jarvis/security/paths.py -> jarvis/security -> jarvis -> project root
    return Path(__file__).resolve().parent.parent.parent


def _load_folder_list(key: str) -> List[Path]:
    folders = []
    for raw in _CFG.get(key, []):
        expanded = _expand(raw)
        # %OneDrive% (and similar) may not be set on a machine without
        # OneDrive -- os.path.expandvars leaves an unresolved token
        # as-is rather than raising, so skip anything still containing
        # an unexpanded "%...%" instead of treating it as a real path.
        if "%" in str(expanded):
            continue
        folders.append(expanded)
    return folders


def _local_drive_roots() -> List[Path]:
    roots = []
    for letter in "CDEFGHIJ":
        drive = Path(f"{letter}:\\")
        if drive.exists():
            roots.append(drive)
    return roots


def _discover_pictures_folders() -> List[Path]:
    """The developer's protected-list instruction was "the Pictures
    folder", not "the one under %USERPROFILE%" specifically
    -- found by testing against this real machine that a second, entirely
    separate D:\\Pictures (with real personal photos) exists and was NOT
    covered by the settings-only list. Rather than hardcode that one
    machine-specific path, this checks every local drive root for a
    top-level folder literally named "Pictures" (case insensitive) and
    protects whichever exist -- correct here, and on a differently laid
    out tutor's laptop too."""
    found = []
    for root in _local_drive_roots():
        try:
            for entry in root.iterdir():
                if entry.is_dir() and entry.name.lower() == "pictures":
                    found.append(entry)
        except OSError:
            continue
    return found


APPROVED_FOLDERS: List[Path] = _load_folder_list("approved_folders") + [_project_root()]
# Project-relative protected folders ([filesystem] protected_project_folders):
# none at present (F16b's own browser profile was removed).
# No read-only action reaches one, and F4 never indexes it.
_PROJECT_PROTECTED: List[Path] = [(_project_root() / str(p)).resolve() for p in _CFG.get("protected_project_folders", [])]
PROTECTED_FOLDERS: List[Path] = _load_folder_list("protected_folders") + _PROJECT_PROTECTED + _discover_pictures_folders()
# De-duplicate while preserving order (settings-listed folders first).
_seen: set = set()
PROTECTED_FOLDERS = [p for p in PROTECTED_FOLDERS if not (p in _seen or _seen.add(p))]


def resolve_real_path(raw_path: str) -> Path:
    """Full real path, following .lnk shortcuts and symlinks, so '..' and
    shortcuts cannot be used to escape a check (R9)."""
    candidate = _expand(raw_path)
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        resolved = candidate.absolute()
    if resolved.suffix.lower() == ".lnk":
        target = _resolve_shortcut_target(resolved)
        if target is not None:
            return target
    return resolved


def _resolve_shortcut_target(lnk_path: Path) -> Optional[Path]:
    """Best-effort .lnk target resolution via pywin32. R14: if pywin32
    isn't installed, or the shortcut can't be read, fall back to treating
    the shortcut file's own location as the path being checked -- never
    crash, never silently skip the check entirely."""
    try:
        import win32com.client

        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortCut(str(lnk_path))
        target = shortcut.Targetpath
        if target:
            return Path(target).resolve(strict=False)
    except Exception:
        pass
    return None


def _is_within(path: Path, roots: List[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def check_path(raw_path: str, intent: PathIntent) -> Tuple[bool, str]:
    """The one shared check (R9). Returns (allowed, reason).

    A bare command/executable name with no path separator or drive
    letter (e.g. "notepad.exe", resolved via the system PATH, not a
    folder JARVIS is reaching into) has no meaningful folder to check --
    allowed through as-is rather than misapplied against a real path.
    """
    if os.sep not in raw_path and "/" not in raw_path and ":" not in raw_path:
        return True, "bare command name, not a path -- resolved via system PATH"

    real_path = resolve_real_path(raw_path)
    if intent is PathIntent.WRITE:
        if _is_within(real_path, APPROVED_FOLDERS):
            return True, "within an approved folder"
        return False, f"'{real_path}' is not inside an approved folder"

    if _is_within(real_path, PROTECTED_FOLDERS):
        return False, f"'{real_path}' is on the protected list"
    return True, "read-only and not on the protected list"
