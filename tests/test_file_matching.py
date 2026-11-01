"""F4 matching quality: "find my resume" must not surface files that merely
share letters with the query, and must not list one file more than once."""

from __future__ import annotations

from pathlib import Path

import pytest

import jarvis.actions.find_file as find_file
from jarvis.actions.file_index import MATCH_THRESHOLD, FileIndex, _score, clean_query


@pytest.mark.parametrize("name", ["resource.pyi", "resample.pyi", "resolve.txt", "subresumeplan.txt", "resonance.doc"])
def test_look_alike_words_stay_below_the_threshold(name: str) -> None:
    """Look alike words stay below the threshold."""
    assert _score("resume", name) < MATCH_THRESHOLD
    assert _score("my resume", name) < MATCH_THRESHOLD


@pytest.mark.parametrize("name", ["resume.md", "Resume.docx", "resume_2024.docx", "My Resume (final).pdf", "alex-resume.pdf"])
def test_real_resumes_clear_the_threshold(name: str) -> None:
    """Real resumes clear the threshold."""
    assert _score("my resume", name) >= MATCH_THRESHOLD


def test_small_typos_still_match() -> None:
    """Small typos still match."""
    assert _score("resumee", "resume.md") >= MATCH_THRESHOLD
    assert _score("resum", "resume.md") >= MATCH_THRESHOLD


def test_filler_words_are_dropped_from_the_query() -> None:
    """Filler words are dropped from the query."""
    assert clean_query("my resume") == "resume"
    assert clean_query("the resume file") == "resume"
    assert clean_query("the") == "the"  # nothing but filler: used as typed


def test_exact_name_still_scores_one_and_tighter_names_rank_higher() -> None:
    """Exact name still scores one and tighter names rank higher."""
    assert _score("resume_2024.docx", "resume_2024.docx") == 1.0
    assert _score("resume", "resume.md") == 1.0
    assert _score("resume", "resume_final.docx") > _score("resume", "resume_final_v2_backup_copy.docx")


def test_hidden_folders_are_not_indexed(tmp_path: Path) -> None:
    """Hidden folders are not indexed."""
    (tmp_path / ".vscode").mkdir()
    (tmp_path / ".vscode" / "resume-abc123.js").write_text("x")
    (tmp_path / "Documents").mkdir()
    (tmp_path / "Documents" / "resume.docx").write_text("x")
    index = FileIndex(roots=[tmp_path])
    index.rebuild_now()
    assert [m.filename for m in index.search("my resume")] == ["resume.docx"]


def test_overlapping_roots_do_not_list_a_file_twice(tmp_path: Path) -> None:
    """Overlapping roots do not list a file twice."""
    (tmp_path / "Documents").mkdir()
    (tmp_path / "Documents" / "resume.docx").write_text("x")
    index = FileIndex(roots=[tmp_path, tmp_path / "Documents"])  # second root is inside the first
    index.rebuild_now()
    assert len(index.search("resume")) == 1


def test_results_are_a_real_list_one_per_line_with_their_folder(tmp_path: Path) -> None:
    """Results are a real list one per line with their folder."""
    for folder in ("stdlib2", "stdlib3"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "notes.txt").write_text("x")
    index = FileIndex(roots=[tmp_path])
    index.rebuild_now()
    steps = []
    finder = find_file.FileFinder(index)
    finder._report_step = lambda text, replaces=None: steps.append(text)
    answer = finder.find_matcher("find notes")
    # Only the count is said; the numbered names are shown, one per
    # line, never a folder or a path; two files with one name get (1), (2).
    assert answer.text == "I found 2 files. Which one would you like to open?"
    assert "1. notes.txt (1)" in steps and "2. notes.txt (2)" in steps
    assert not any("stdlib" in s or str(tmp_path) in s for s in steps)


def test_find_my_resume_end_to_end_only_returns_the_resume(tmp_path: Path) -> None:
    """Find my resume end to end only returns the resume."""
    (tmp_path / "Documents").mkdir()
    (tmp_path / "Documents" / "resume.md").write_text("x")
    for name in ("resource.pyi", "resample.pyi", "resolver.py"):
        (tmp_path / "Documents" / name).write_text("x")
    index = FileIndex(roots=[tmp_path])
    index.rebuild_now()
    result = find_file.FileFinder(index).find_matcher("find my resume")
    assert result.action_name == "open_file"  # exactly one match -> opened directly
    assert result.params["display_name"] == "resume.md"
