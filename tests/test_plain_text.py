"""Markdown is stripped from Groq answers before they are shown or spoken
(developer): the markup goes, the words stay."""

from __future__ import annotations

import pytest

from jarvis.speech.plain_text import to_speakable


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("**Photosynthesis** is how plants make food.", "Photosynthesis is how plants make food."),
        ("__Bold__ and *italic* and _also italic_.", "Bold and italic and also italic."),
        ("# Heading\nSome text.", "Heading\nSome text."),
        ("### Deep heading", "Deep heading"),
        ("- one\n- two\n* three\n+ four", "one\ntwo\nthree\nfour"),
        ("1. first\n2) second", "first\nsecond"),
        ("See [the docs](https://example.com) for more.", "See the docs for more."),
        ("![a cat](cat.png) sat", "a cat sat"),
        ("Run `pip install groq` now.", "Run pip install groq now."),
        ("```python\nprint('hi')\n```", "print('hi')"),
        ("> quoted line", "quoted line"),
        ("before\n---\nafter", "before\nafter"),
        ("~~old~~ new", "old new"),
        ("| a | b |\n|---|---|\n| 1 | 2 |", "a, b\n1, 2"),
    ],
)
def test_markdown_is_removed(raw: str, expected: str) -> None:
    """Markdown is removed."""
    assert to_speakable(raw) == expected


@pytest.mark.parametrize(
    "plain",
    [
        "I'm doing well, thanks for asking.",
        "Use snake_case names in Python.",
        "Two * three is six.",
        "It costs 5 - 10 pounds.",
    ],
)
def test_plain_text_is_left_alone(plain: str) -> None:
    """Plain text is left alone."""
    assert to_speakable(plain) == plain


def test_extra_blank_lines_and_spaces_collapse() -> None:
    """Extra blank lines and spaces collapse."""
    assert to_speakable("a  b\n\n\n\nc") == "a b\nc"
