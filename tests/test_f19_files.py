"""F19 File processor. Temporary folders only; Groq is
mocked. Files are found through a real F4 index of a temporary folder,
read with the real pypdf / python-docx / csv, and never written to."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from jarvis.actions.file_index import FileIndex
from jarvis.controller import State
from jarvis.controller.trust import TRUST_TABLE, TrustLevel
from jarvis.dashboard.activity_feed import format_record
from jarvis.files import commands as files_mod
from jarvis.files.commands import FileCommands
from jarvis.files.extract import ExtractError, extract
from jarvis.online import OnlineErrorKind
from jarvis.speech.qa import AnswerResult
from stretch_rig import SpyAnswerer, build, wait

SECRET = "Quarterly revenue rose fourteen percent in Perth"  # must never reach Live Activity or debug.log


def make_pdf(path: Path, pages) -> Path:
    """A minimal real PDF: one line of Helvetica text per page."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        stream = f"BT /F1 12 Tf 40 700 Td ({text}) Tj ET".encode("latin-1")
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream.decode('latin-1')}\nendstream")
        content_id = len(objects)
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
                       f"/Resources << /Font << /F1 3 0 R >> >> >>")
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))
    return path


class Summariser(SpyAnswerer):
    """Groq stand-in for F19: returns a fixed summary or a set error."""
    def __init__(self) -> None:
        super().__init__()
        self.summaries = []
        self.cooling = None
        self.error = None

    def summarise(self, text, instructions, on_going_online=None):
        """Fake summarise for the tests."""
        self.summaries.append(text)
        if self.error is not None:
            return AnswerResult("Searching online isn't set up yet. Add GROQ_API_KEY to the .env file in the JARVIS folder, then try again.",
                                True, self.error)
        return AnswerResult("The report covers sales. Revenue grew. Costs fell. Hiring paused.", True)

    def cooling_until(self):
        """Fake cooling until for the tests."""
        return self.cooling


class Clock:
    """A clock the test moves by hand (sleep() advances it instantly)."""
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        """Fake sleep for the tests."""
        self.now += s


@pytest.fixture
def desk(tmp_path, monkeypatch):
    """Fixture: a folder of sample files (PDF, text, CSV) indexed, with Groq mocked."""
    files = tmp_path / "files"
    files.mkdir()
    make_pdf(files / "report.pdf", [SECRET, "Page two has a little more text on it here", "Page three ends the report with a summary"])
    # Bytes, so Windows doesn't turn \n into \r\n behind the test's back.
    (files / "notes.txt").write_bytes(b"Shopping list. Milk and eggs.\n" * 3)
    (files / "budget.csv").write_bytes(b"date,item,amount\n2026-10-01,rent,1200\n2026-10-02,food,85.5\n")
    index = FileIndex(roots=[files])
    index.rebuild_now()
    groq = Summariser()
    clock = Clock()
    temp = tmp_path / "jarvis_tmp"
    fc = FileCommands(index, groq, temp_dir=temp, sleep=clock.sleep, clock=clock)
    rig = build(tmp_path, monkeypatch, index=index, groq=groq, files=fc)
    rig.update(files=files, fc=fc, temp=temp, index=index, clock=clock)
    return rig


def _lines(rig):
    return format_record(rig["logbook"].last_completed()).lines


def _content_never_shown(rig, *texts):
    every_line = [line for r in rig["logbook"].records() for line in format_record(r).lines]
    log = rig["debug"]()
    for text in texts:
        assert not any(text.lower() in line.lower() for line in every_line), text
        assert text.lower() not in log.lower(), text


def test_trust_send_text_online_is_l3() -> None:
    """Trust send text online is L3."""
    assert TRUST_TABLE["send_text_online"] == TrustLevel.L3


# -- local answers (no network) --------------------------------------------------------------------------


def test_whats_in_a_pdf_is_local_and_shows_no_content(desk) -> None:
    """Whats in a PDF is local and shows no content."""
    reply = desk["say"]("what's in report.pdf")
    assert reply == "report.pdf has 3 pages and about 25 words. Say summarise report.pdf to have it summarised online."
    lines = _lines(desk)
    assert "reading report.pdf locally (3 pages)" in lines
    assert "saying: (about report.pdf: the file's content isn't shown here)" in lines
    assert desk["groq"].summaries == []
    _content_never_shown(desk, SECRET)


def test_csv_rows_columns_and_headers(desk) -> None:
    """CSV rows columns and headers."""
    reply = desk["say"]("ok so, what's in my budget.csv")
    assert reply.startswith("budget.csv has 2 rows and 3 columns: date, item and amount.")
    _content_never_shown(desk, "date, item and amount")


@pytest.mark.parametrize("said", ["how many pages are in report.pdf", "word count of report.pdf"])
def test_count_questions_are_local(desk, said) -> None:
    """Count questions are local."""
    assert desk["say"](said).startswith("report.pdf has 3 pages")


def test_read_says_the_start_aloud_only(desk) -> None:
    """Read says the start aloud only."""
    reply = desk["say"]("read notes.txt")
    assert reply.startswith("Here's how notes.txt starts: Shopping list. Milk and eggs.")
    _content_never_shown(desk, "Shopping list")


def test_ordinary_sentences_are_not_file_requests(desk) -> None:
    """Ordinary sentences are not file requests."""
    desk["say"]("read me a poem")
    assert desk["groq"].questions == ["read me a poem"]


# -- finding the file (F4 rules) --------------------------------------------------------------------------


def test_several_matches_use_the_pick_list(desk) -> None:
    """Several matches use the pick list."""
    (desk["files"] / "sub").mkdir()
    make_pdf(desk["files"] / "sub" / "report.pdf", ["The other report has just this one page of text"])
    desk["index"].rebuild_now()
    assert desk["say"]("what's in report.pdf") == "I found 2 files called that. Which one? Say its number."
    picked = {desk["say"]("the second one")}
    assert picked & {"report.pdf has 1 page and about 10 words. Say summarise report.pdf to have it summarised online.",
                     "report.pdf has 3 pages and about 25 words. Say summarise report.pdf to have it summarised online."}
    assert desk["groq"].questions == []  # the pick never went online


def test_missing_file_named_as_a_file(desk) -> None:
    """Missing file named as a file."""
    assert desk["say"]("summarise the tax document") == "I can't find a PDF, Word, text or CSV file called tax document."


def test_protected_folder_is_refused_before_any_read(desk, monkeypatch) -> None:
    """Protected folder is refused before any read."""
    from jarvis.security import paths

    secret_dir = desk["files"].parent / "Pictures"
    secret_dir.mkdir()
    make_pdf(secret_dir / "private.pdf", ["Very private"])
    monkeypatch.setattr(paths, "PROTECTED_FOLDERS", paths.PROTECTED_FOLDERS + [secret_dir.resolve()])
    read = []
    monkeypatch.setattr(files_mod, "extract", lambda p: read.append(p))
    desk["say"](f"what's in {secret_dir / 'private.pdf'}")  # a typed full path
    assert desk["speech"].texts[-1] == "I can't read private.pdf: it's in a protected folder."
    assert read == []


# -- extraction edge cases ------------------------------------------------------------------------------------


def test_encrypted_pdf_is_refused_with_no_password_prompt(desk) -> None:
    """Encrypted PDF is refused with no password prompt."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.append(PdfReader(str(desk["files"] / "report.pdf")))
    writer.encrypt("secret-pw")
    with open(desk["files"] / "locked.pdf", "wb") as f:
        writer.write(f)
    desk["index"].rebuild_now()
    reply = desk["say"]("summarise locked.pdf")
    assert reply == "locked.pdf is password-protected, so I can't read it. I never ask for or handle passwords."
    assert desk["c"].state == State.AWAKE and desk["groq"].summaries == []


def test_scanned_pdf_says_so_and_groq_is_not_called(desk) -> None:
    """Scanned PDF says so and Groq is not called."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(612, 792)
    with open(desk["files"] / "scan.pdf", "wb") as f:
        writer.write(f)
    desk["index"].rebuild_now()
    assert desk["say"]("summarise scan.pdf") == "scan.pdf looks like a scanned PDF; I can't read scanned pages."
    assert desk["groq"].summaries == []


def test_docx_table_cells_are_read(tmp_path) -> None:
    """Docx table cells are read."""
    import docx

    document = docx.Document()
    document.add_paragraph("Intro paragraph")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Cell alpha"
    table.rows[0].cells[1].text = "Cell beta"
    document.save(str(tmp_path / "t.docx"))
    got = extract(tmp_path / "t.docx")
    assert "Intro paragraph" in got.text and "Cell alpha | Cell beta" in got.text


def test_cp1252_text_is_decoded_and_the_encoding_reported(tmp_path) -> None:
    """Cp1252 text is decoded and the encoding reported."""
    # Test data: the French loanwords "café naïve" hold two letters outside
    # ASCII, so a file saved in Windows-1252 (not UTF-8) must still read correctly.
    (tmp_path / "w.txt").write_bytes("caf\xe9 na\xefve".encode("cp1252"))
    got = extract(tmp_path / "w.txt")
    assert got.text == "café naïve" and got.encoding == "cp1252"


def test_csv_sends_at_most_200_rows(tmp_path) -> None:
    """CSV sends at most 200 rows."""
    rows = "\n".join(f"{i},{i * 2}" for i in range(500))
    (tmp_path / "big.csv").write_text("a,b\n" + rows, encoding="utf-8")
    got = extract(tmp_path / "big.csv")
    sent_rows = got.send_text.split("First 200 rows:\n", 1)[1].strip().splitlines()[1:]  # after the header line
    assert got.rows == 500 and len(sent_rows) == 200
    assert "a: min 0, max 499" in got.send_text


def test_a_25_mb_file_is_refused(tmp_path) -> None:
    """A 25 mb file is refused."""
    with open(tmp_path / "huge.txt", "wb") as f:
        f.truncate(25_000_000)
    with pytest.raises(ExtractError) as problem:
        extract(tmp_path / "huge.txt")
    assert problem.value.key == "too_big"


# -- summarise: R4 preview, L3, hash ------------------------------------------------------------------------------


def test_summarise_previews_the_exact_text_then_sends_that_file(desk) -> None:
    """Summarise previews the exact text then sends that file."""
    desk["say"]("summarise notes.txt")
    assert desk["c"].state == State.CONFIRMING
    pending = desk["c"].pending_confirmation()
    assert pending.action_name == "send_text_online" and pending.trust_level == TrustLevel.L3
    preview = desk["fc"].preview(dict(pending.params)["preview_id"])
    original = (desk["files"] / "notes.txt").read_bytes().decode("utf-8")
    assert preview.path.read_bytes().decode("utf-8") == original  # the exact text, as a file
    assert desk["speech"].texts[-1].startswith("Here's exactly what would be sent online from notes.txt: 90 characters.")
    assert desk["groq"].summaries == []  # nothing sent before confirm
    reply = desk["confirming"]("confirm")
    assert reply == "The report covers sales. Revenue grew. Costs fell."
    assert desk["groq"].summaries == [original]  # the sent text is the previewed file's text
    assert hashlib.sha256(original.encode()).hexdigest() == preview.sha256
    assert not preview.path.exists()  # deleted after sending
    lines = _lines(desk)
    assert "temporary copy deleted" in lines and "saying: (about notes.txt: the file's content isn't shown here)" in lines
    log = desk["debug"]()
    assert "sent text, 90 chars, to Groq" in log and f"sha256 {preview.sha256[:16]}" in log
    _content_never_shown(desk, "Shopping list", "Revenue grew")


def test_cancel_sends_nothing_and_deletes_the_copy(desk) -> None:
    """Cancel sends nothing and deletes the copy."""
    desk["say"]("summarise notes.txt")
    preview_id = dict(desk["c"].pending_confirmation().params)["preview_id"]
    path = desk["fc"].preview(preview_id).path
    assert desk["confirming"]("cancel") == "Cancelled."
    assert desk["groq"].summaries == [] and wait(lambda: not path.exists())


def test_stop_while_confirming_deletes_the_copy(desk) -> None:
    """Stop while confirming deletes the copy."""
    desk["say"]("summarise notes.txt")
    path = desk["fc"].preview(dict(desk["c"].pending_confirmation().params)["preview_id"]).path
    desk["c"].request_stop()
    assert wait(lambda: not path.exists())
    assert desk["groq"].summaries == []


def test_text_changed_after_the_preview_is_not_sent(desk) -> None:
    """Text changed after the preview is not sent."""
    desk["say"]("summarise notes.txt")
    path = desk["fc"].preview(dict(desk["c"].pending_confirmation().params)["preview_id"]).path
    path.write_text("something else entirely", encoding="utf-8")
    reply = desk["confirming"]("confirm")
    assert reply == "The text changed after you confirmed; please check the preview again. Nothing was sent."
    assert desk["groq"].summaries == []


def test_long_file_asks_which_part_and_sends_at_most_48000(desk) -> None:
    """Long file asks which part and sends at most 48000."""
    (desk["files"] / "long.txt").write_text("A sentence about things. " * 2400, encoding="utf-8")  # 60,000 chars
    desk["index"].rebuild_now()
    assert desk["say"]("summarise long.txt") == "This file is long. Which part: the start, the middle, or the end?"
    assert desk["c"].state == State.AWAKE
    desk["say"]("the middle")
    params = dict(desk["c"].pending_confirmation().params)
    assert params["chars"] == 48000 and params["part"] == "middle"
    desk["confirming"]("confirm")
    assert len(desk["groq"].summaries) == 5  # 4 parts + combine
    assert all(len(t) <= 12000 for t in desk["groq"].summaries[:4])


def test_chunks_sent_are_exactly_the_previewed_text(desk) -> None:
    """Chunks sent are exactly the previewed text."""
    body = "Para one is short.\n\n" + "Another line of the notes here. " * 1400
    (desk["files"] / "chunky.txt").write_bytes(body.encode("utf-8"))
    desk["index"].rebuild_now()
    desk["say"]("summarise chunky.txt")
    preview = desk["fc"].preview(dict(desk["c"].pending_confirmation().params)["preview_id"])
    previewed = preview.path.read_bytes().decode("utf-8")
    desk["confirming"]("confirm")
    parts = desk["groq"].summaries[:-1]  # the last request combines the part summaries
    assert "".join(parts) == previewed == body
    assert hashlib.sha256("".join(parts).encode("utf-8")).hexdigest() == preview.sha256


def test_40000_chars_is_four_requests_plus_combine(desk) -> None:
    """40000 chars is four requests plus combine."""
    (desk["files"] / "mid.txt").write_text("Words go here now. " * 2105, encoding="utf-8")  # ~40,000
    desk["index"].rebuild_now()
    desk["say"]("summarise mid.txt")
    desk["confirming"]("confirm")
    assert len(desk["groq"].summaries) == 5


def test_in_a_cooldown_no_preview_is_offered(desk) -> None:
    """In a cooldown no preview is offered."""
    import time

    desk["groq"].cooling = time.time() + 120
    assert desk["say"]("summarise notes.txt").startswith("Summaries online are off until")
    assert desk["c"].state == State.AWAKE and not list(desk["temp"].glob("*.txt"))


def test_no_key_after_confirm_says_why(desk) -> None:
    """No key after confirm says why."""
    desk["groq"].error = OnlineErrorKind.NO_KEY
    desk["say"]("summarise notes.txt")
    assert desk["confirming"]("confirm").startswith("I couldn't summarise notes.txt. Searching online isn't set up yet.")


def test_leftover_copies_are_deleted_at_startup(tmp_path, monkeypatch) -> None:
    """Leftover copies are deleted at startup."""
    from jarvis.security import paths

    monkeypatch.setattr(paths, "APPROVED_FOLDERS", paths.APPROVED_FOLDERS + [tmp_path.resolve()])
    temp = tmp_path / "t"
    temp.mkdir()
    (temp / "f19-old.txt").write_text("left over", encoding="utf-8")
    (temp / "keep.txt").write_text("not ours", encoding="utf-8")
    FileCommands(FileIndex(roots=[]), None, temp_dir=temp)
    assert not (temp / "f19-old.txt").exists() and (temp / "keep.txt").exists()


def test_his_files_are_never_written(desk) -> None:
    """His files are never written."""
    before = {p: p.read_bytes() for p in desk["files"].rglob("*") if p.is_file()}
    desk["say"]("what's in report.pdf")
    desk["say"]("summarise notes.txt")
    desk["confirming"]("confirm")
    desk["say"]("read budget.csv")
    assert {p: p.read_bytes() for p in desk["files"].rglob("*") if p.is_file()} == before


def test_preview_card_has_the_text_and_both_warnings(desk) -> None:
    """Preview card has the text and both warnings."""
    from jarvis.dashboard.app import _file_preview_rows

    desk["say"]("summarise notes.txt")
    preview = desk["fc"].preview(dict(desk["c"].pending_confirmation().params)["preview_id"])
    texts = []
    for control in _file_preview_rows(preview):
        texts.append(getattr(control, "value", None))
        inner = getattr(getattr(control, "content", None), "controls", None) or []
        texts.extend(getattr(c, "value", None) for c in inner)
    assert "This will be sent online." in texts and "Online service terms not yet checked by you" in texts
    assert any(t and t.startswith("Shopping list.") for t in texts)


def test_what_are_the_columns_in_a_csv(desk) -> None:
    """What are the columns in a CSV."""
    # This was answered as a memory question.
    assert desk["say"]("What are the columns in my budget CSV?").startswith("budget.csv has 2 rows and 3 columns: date, item and amount.")
