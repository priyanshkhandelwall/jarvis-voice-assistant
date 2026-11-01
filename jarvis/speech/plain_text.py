"""Turn a model's reply into plain sentences fit to be spoken aloud.

Groq is asked for plain speech, but models still slip into markdown. Read
aloud, "**Note:**" or "# Steps" or "[docs](https://...)" is noise, so it
is removed here before the text reaches the dashboard or the voice. Only
the markup goes; the words inside it stay.
"""

from __future__ import annotations

import re

_CODE_FENCE = re.compile(r"^\s*(```|~~~)[^\n]*$", re.MULTILINE)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+", re.MULTILINE)
_BLOCKQUOTE = re.compile(r"^\s*>\s?", re.MULTILINE)
_RULE = re.compile(r"^\s*(?:[-*_]\s*){3,}$", re.MULTILINE)
_TABLE_DIVIDER = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$", re.MULTILINE)
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_BOLD = re.compile(r"(\*\*|__)(.+?)\1", re.DOTALL)
# Single * or _ only when they wrap a word, so "snake_case" and "2 * 3" survive.
_ITALIC_STAR = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_ITALIC_UNDERSCORE = re.compile(r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])")
_STRIKE = re.compile(r"~~(.+?)~~")
_INLINE_CODE = re.compile(r"`([^`]*)`")
_SPACES = re.compile(r"[ \t]{2,}")
_BLANK_LINES = re.compile(r"\n{2,}")


def to_speakable(text: str) -> str:
    """Strip markdown: code fences, headings, bullets and numbering,
    links (keeping their words), bold/italic/strike, inline code,
    blockquotes, rules and table pipes."""
    out = _CODE_FENCE.sub("", text)
    out = _RULE.sub("", out)
    out = _TABLE_DIVIDER.sub("", out)
    out = _HEADING.sub("", out)
    out = _BULLET.sub("", out)
    out = _BLOCKQUOTE.sub("", out)
    out = _IMAGE.sub(r"\1", out)
    out = _LINK.sub(r"\1", out)
    out = _BOLD.sub(r"\2", out)
    out = _STRIKE.sub(r"\1", out)
    out = _ITALIC_STAR.sub(r"\1", out)
    out = _ITALIC_UNDERSCORE.sub(r"\1", out)
    out = _INLINE_CODE.sub(r"\1", out)
    out = "\n".join(line.strip().strip("|").replace(" | ", ", ").strip() for line in out.splitlines())
    out = _SPACES.sub(" ", out)
    out = _BLANK_LINES.sub("\n", out)
    return out.strip()
