"""F19: reading a file's text locally (built). No network,
no writes to his files.

- PDF: pypdf, page by page with page numbers kept, the first [file_processor]
  max_pdf_pages pages. Password-protected: refused, and no password is ever
  asked for (R11). Almost no text: "a scanned PDF".
- DOCX: python-docx, paragraphs, then table cells.
- TXT/MD: utf-8, then utf-8 with BOM, then cp1252; which one worked is kept.
- CSV: the standard library, dialect sniffed: rows, columns, headers and
  simple per-column numbers, all local. Only the header, those numbers and
  the first [file_processor] max_csv_rows_sent rows are candidates to send.

The caller has already passed the path through R9's read check.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from jarvis.settings import SETTINGS

_CFG = SETTINGS["file_processor"]
# Put before each PDF page's text so page numbers are kept (spec F19).
PAGE_MARK = re.compile(r"\[Page \d+\]")


class ExtractError(Exception):
    """A file that can't be read. `key` picks the reply in
    [file_processor.replies]; `reason` fills "unreadable"."""

    def __init__(self, key: str, reason: str = "") -> None:
        super().__init__(f"{key}: {reason}")
        self.key = key
        self.reason = reason


@dataclass
class Extracted:
    """A file's text as read locally: its kind, the text, what would be sent, and its size in pages, words
    or rows."""
    kind: str  # "pdf" | "docx" | "text" | "csv"
    text: str  # everything read, for word counts and "read"
    send_text: str  # what "summarise" would send (CSV: header, numbers, first rows)
    pages: Optional[int] = None  # PDFs: pages in the file
    pages_read: Optional[int] = None
    encoding: Optional[str] = None  # text files: which encoding worked
    rows: Optional[int] = None  # CSV: data rows (header not counted)
    columns: Optional[int] = None
    headers: List[str] = field(default_factory=list)

    @property
    def words(self) -> int:
        """His words only: the "[Page N]" markers added while reading a PDF
        don't count."""
        return len(re.findall(r"\w+", PAGE_MARK.sub(" ", self.text)))


def _decode(data: bytes) -> tuple:
    for encoding in (str(e) for e in _CFG["text_encodings_tried"]):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise ExtractError("unreadable", "its text encoding isn't one I can read")


def _pdf(path: Path) -> Extracted:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:
        raise ExtractError("missing", "pypdf") from exc
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            raise ExtractError("encrypted")
        total = len(reader.pages)
        limit = min(total, int(_CFG["max_pdf_pages"]))
        parts = []
        chars = 0
        for n in range(limit):
            page_text = (reader.pages[n].extract_text() or "").strip()
            chars += len(page_text)
            parts.append(f"[Page {n + 1}]\n{page_text}")
    except ExtractError:
        raise
    except (PdfReadError, ValueError, OSError, KeyError) as exc:
        raise ExtractError("unreadable", "it doesn't look like a valid PDF") from exc
    if limit and chars / limit < float(_CFG["min_chars_per_page"]):
        raise ExtractError("scanned")
    text = "\n\n".join(parts)
    return Extracted("pdf", text, text, pages=total, pages_read=limit)


def _docx(path: Path) -> Extracted:
    try:
        import docx
        from docx.opc.exceptions import PackageNotFoundError
    except ImportError as exc:
        raise ExtractError("missing", "python-docx") from exc
    try:
        document = docx.Document(str(path))
    except (PackageNotFoundError, KeyError, ValueError, OSError) as exc:
        raise ExtractError("unreadable", "it doesn't look like a valid Word document") from exc
    except Exception as exc:  # zipfile.BadZipFile and other damage
        raise ExtractError("unreadable", "it doesn't look like a valid Word document") from exc
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    text = "\n".join(parts)
    return Extracted("docx", text, text)


def _text(path: Path) -> Extracted:
    text, encoding = _decode(path.read_bytes())
    return Extracted("text", text, text, encoding=encoding)


def _number(value: str) -> Optional[float]:
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


def _csv(path: Path) -> Extracted:
    raw, encoding = _decode(path.read_bytes())
    sample = raw[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample)
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(raw), dialect) if any(cell.strip() for cell in r)]
    if not rows:
        raise ExtractError("empty")
    headers = [h.strip() for h in rows[0]]
    data = rows[1:]
    lines = [f"Columns: {', '.join(headers)}", f"Rows: {len(data)}"]
    for i, header in enumerate(headers):
        values = [_number(r[i]) for r in data if i < len(r) and r[i].strip()]
        numbers = [v for v in values if v is not None]
        if numbers and len(numbers) == len(values):
            lines.append(f"{header}: min {min(numbers):g}, max {max(numbers):g}, mean {sum(numbers) / len(numbers):.4g}")
    shown_rows = data[: int(_CFG["max_csv_rows_sent"])]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(shown_rows)
    send = "\n".join(lines) + f"\nFirst {len(shown_rows)} rows:\n" + out.getvalue()
    return Extracted("csv", raw, send, encoding=encoding, rows=len(data), columns=len(headers), headers=headers)


_READERS = {".pdf": _pdf, ".docx": _docx, ".txt": _text, ".md": _text, ".csv": _csv}


def extract(path: Path) -> Extracted:
    """Read `path` locally. Raises ExtractError with the reply to give."""
    reader = _READERS.get(path.suffix.lower())
    if reader is None:
        raise ExtractError("unsupported")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ExtractError("unreadable", "it can't be opened") from exc
    if size > int(_CFG["max_file_bytes"]):
        raise ExtractError("too_big", f"{size / 1_000_000:.0f}")
    try:
        result = reader(path)
    except OSError as exc:
        raise ExtractError("unreadable", "it can't be opened") from exc
    if not result.text.strip():
        raise ExtractError("empty")
    return result
