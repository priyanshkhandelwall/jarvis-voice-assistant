"""Tests for R9's one shared path-check function.

Required by Section 8: '..', links, outside an approved folder,
overwrite (checked at the call site, not here), and the protected list.
"""

from __future__ import annotations

import os
from pathlib import Path

from jarvis.security import APPROVED_FOLDERS, PROTECTED_FOLDERS, PathIntent, check_path
from jarvis.security.paths import resolve_real_path


def test_protected_folder_denies_read() -> None:
    """Protected folder denies read."""
    pictures = PROTECTED_FOLDERS[0]
    allowed, reason = check_path(str(pictures / "photo.jpg"), PathIntent.READ)
    assert allowed is False
    assert "protected list" in reason


def test_protected_subfolder_denies_read_too() -> None:
    """Camera Roll / Screenshots are subfolders of Pictures -- protecting
    Pictures itself must already cover them (the developer's own
    reasoning), not need separate entries."""
    pictures = PROTECTED_FOLDERS[0]
    allowed, _ = check_path(str(pictures / "Camera Roll" / "img.jpg"), PathIntent.READ)
    assert allowed is False
    allowed2, _ = check_path(str(pictures / "Screenshots" / "shot.png"), PathIntent.READ)
    assert allowed2 is False


def test_unprotected_folder_allows_read() -> None:
    """Unprotected folder allows read."""
    documents = APPROVED_FOLDERS[0]  # Documents
    allowed, reason = check_path(str(documents / "resume.docx"), PathIntent.READ)
    assert allowed is True


def test_read_reaches_anywhere_except_protected() -> None:
    """The developer's own instruction: 'search can reach everywhere else
    read-only'. A folder that's neither approved nor protected (e.g. a
    Videos folder) must still be readable."""
    home = Path(os.path.expanduser("~"))
    allowed, _ = check_path(str(home / "Videos" / "clip.mp4"), PathIntent.READ)
    assert allowed is True


def test_write_outside_approved_folders_denied() -> None:
    """Write outside approved folders denied."""
    home = Path(os.path.expanduser("~"))
    allowed, reason = check_path(str(home / "Videos" / "clip.mp4"), PathIntent.WRITE)
    assert allowed is False
    assert "not inside an approved folder" in reason


def test_write_inside_approved_folder_allowed() -> None:
    """Write inside approved folder allowed."""
    downloads = APPROVED_FOLDERS[1]
    allowed, _ = check_path(str(downloads / "file.txt"), PathIntent.WRITE)
    assert allowed is True


def test_dotdot_traversal_cannot_escape_a_protected_check() -> None:
    """'..' must not let a path that looks approved actually land inside
    the protected folder undetected -- resolved before checking."""
    documents = APPROVED_FOLDERS[0]
    pictures = PROTECTED_FOLDERS[0]
    sneaky = str(documents / ".." / pictures.name / "photo.jpg")
    allowed, reason = check_path(sneaky, PathIntent.READ)
    assert allowed is False
    assert "protected list" in reason


def test_dotdot_traversal_cannot_fake_approved_for_write() -> None:
    """Dotdot traversal cannot fake approved for write."""
    home = Path(os.path.expanduser("~"))
    documents_name = APPROVED_FOLDERS[0].name
    sneaky = str(home / "Videos" / ".." / documents_name / "file.txt")
    allowed, _ = check_path(sneaky, PathIntent.WRITE)
    assert allowed is True  # resolves to the real Documents folder, correctly allowed
    sneaky_out = str(home / "Documents" / ".." / "Videos" / "file.txt")
    allowed2, _ = check_path(sneaky_out, PathIntent.WRITE)
    assert allowed2 is False  # resolves to Videos, correctly denied


def test_bare_command_name_is_not_treated_as_a_path() -> None:
    """Bare command name is not treated as a path."""
    allowed, reason = check_path("notepad.exe", PathIntent.READ)
    assert allowed is True
    assert "not a path" in reason


def test_resolve_real_path_expands_env_vars() -> None:
    """Resolve real path expands env vars."""
    resolved = resolve_real_path("%USERPROFILE%\\Documents")
    assert "%" not in str(resolved)
    assert resolved.is_absolute()


def test_pictures_folder_on_every_local_drive_is_protected() -> None:
    """Found by testing against the real machine: a second, unrelated
    D:\\Pictures (real personal photos) existed and was NOT covered by
    the settings-only protected list. Every local drive's top-level
    Pictures folder must be protected, not just the one under
    %USERPROFILE%."""
    from jarvis.security.paths import _discover_pictures_folders

    discovered = _discover_pictures_folders()
    for folder in discovered:
        assert folder in PROTECTED_FOLDERS
        allowed, reason = check_path(str(folder / "some_photo.jpg"), PathIntent.READ)
        assert allowed is False
        assert "protected list" in reason
