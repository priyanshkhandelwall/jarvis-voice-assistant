"""Tests for F4's background-refreshed file index.

walk_root/FileIndex are tested against small temporary directory trees,
never the real C:/D: drives -- keeps tests fast and independent of
what's actually on the machine running them (2.2).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from jarvis.actions.file_index import FileIndex, walk_root


@pytest.fixture
def sample_tree(tmp_path: Path) -> Path:
    """Fixture: a small folder tree of sample documents to index."""
    (tmp_path / "Documents").mkdir()
    (tmp_path / "Documents" / "resume_2024.docx").write_text("x")
    (tmp_path / "Documents" / "cover_letter.docx").write_text("x")
    (tmp_path / "Downloads").mkdir()
    (tmp_path / "Downloads" / "resume_old.pdf").write_text("x")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "resume_fake.js").write_text("x")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "resume_in_git.txt").write_text("x")
    return tmp_path


def test_walk_root_finds_real_files(sample_tree: Path) -> None:
    """Walk root finds real files."""
    found = {name for name, _path in walk_root(sample_tree)}
    assert "resume_2024.docx" in found
    assert "cover_letter.docx" in found
    assert "resume_old.pdf" in found


def test_walk_root_skips_excluded_directories(sample_tree: Path) -> None:
    """Walk root skips excluded directories."""
    found = {name for name, _path in walk_root(sample_tree)}
    assert "resume_fake.js" not in found  # node_modules
    assert "resume_in_git.txt" not in found  # .git


def test_walk_root_skips_protected_folders(monkeypatch, sample_tree: Path) -> None:
    """Walk root skips protected folders."""
    protected_dir = sample_tree / "Pictures"
    protected_dir.mkdir()
    (protected_dir / "secret_photo.jpg").write_text("x")

    import jarvis.actions.file_index as file_index_module

    monkeypatch.setattr(
        file_index_module,
        "check_path",
        lambda path, intent: (False, "protected") if "Pictures" in path else (True, "ok"),
    )

    found = {name for name, _path in walk_root(sample_tree)}
    assert "secret_photo.jpg" not in found
    assert "resume_2024.docx" in found  # everything else still found


def test_file_index_rebuild_and_search(sample_tree: Path) -> None:
    """File index rebuild and search."""
    index = FileIndex(roots=[sample_tree])
    assert index.is_ready() is False
    index.rebuild_now()
    assert index.is_ready() is True

    results = index.search("resume")
    names = [m.filename for m in results]
    assert "resume_2024.docx" in names
    assert "resume_old.pdf" in names
    assert "resume_fake.js" not in names  # excluded dir, never indexed


def test_file_index_search_ranks_exact_match_first(sample_tree: Path) -> None:
    """File index search ranks exact match first."""
    index = FileIndex(roots=[sample_tree])
    index.rebuild_now()
    results = index.search("resume_2024.docx")
    assert results[0].filename == "resume_2024.docx"
    assert results[0].score == 1.0


def test_file_index_search_returns_nothing_below_threshold(sample_tree: Path) -> None:
    """File index search returns nothing below threshold."""
    index = FileIndex(roots=[sample_tree])
    index.rebuild_now()
    assert index.search("completely unrelated xyz query") == []


def test_file_index_search_before_ready_returns_empty(sample_tree: Path) -> None:
    """File index search before ready returns empty."""
    index = FileIndex(roots=[sample_tree])
    assert index.search("resume") == []  # nothing indexed yet, honestly empty


def test_file_index_start_builds_in_background(sample_tree: Path) -> None:
    """File index start builds in background."""
    index = FileIndex(roots=[sample_tree])
    index.start()
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not index.is_ready():
            time.sleep(0.05)
        assert index.is_ready() is True
        assert index.search("resume")
    finally:
        index.stop()


def test_file_index_respects_limit(sample_tree: Path) -> None:
    """File index respects limit."""
    for i in range(10):
        (sample_tree / "Documents" / f"resume_v{i}.docx").write_text("x")
    index = FileIndex(roots=[sample_tree])
    index.rebuild_now()
    results = index.search("resume", limit=3)
    assert len(results) <= 3
