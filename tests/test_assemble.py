"""Phase B assembly tests: separators, numbering, headers, rotation, ranges."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from consolidator import assemble, pages  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures" / "generated"


@pytest.fixture
def two_pdfs(tmp_path):
    """Two small PDFs of 2 and 3 pages."""
    made = []
    for name, count in (("alpha.pdf", 2), ("beta.pdf", 3)):
        writer = PdfWriter()
        for _ in range(count):
            writer.add_blank_page(width=612, height=792)
        path = tmp_path / name
        with open(path, "wb") as handle:
            writer.write(handle)
        made.append(path)
    return made


def page_text(path, index):
    return PdfReader(str(path)).pages[index].extract_text() or ""


def test_merge_counts_pages_and_bookmarks(tmp_path, two_pdfs):
    entries = [assemble.MergeEntry(two_pdfs[0], "alpha"), assemble.MergeEntry(two_pdfs[1], "beta")]
    dest = tmp_path / "out.pdf"
    report = assemble.merge(entries, dest)

    assert report.entries == 2
    assert report.pages == 5
    assert report.outline_items == 2
    assert len(PdfReader(str(dest)).outline) == 2


def test_page_range_selects_a_subset(tmp_path, two_pdfs):
    """Only the pages listed are included, in the order given."""
    entry = assemble.MergeEntry(
        two_pdfs[1], "beta", pages=[assemble.PageSpec(2), assemble.PageSpec(0)]
    )
    dest = tmp_path / "subset.pdf"
    report = assemble.merge([entry], dest)

    assert report.pages == 2, "two pages selected out of three"


def test_rotation_is_applied(tmp_path, two_pdfs):
    entry = assemble.MergeEntry(two_pdfs[0], "alpha", pages=[assemble.PageSpec(0, 90)])
    dest = tmp_path / "rot.pdf"
    assemble.merge([entry], dest, page_size=pages.LETTER)

    page = PdfReader(str(dest)).pages[0]
    # Fitting folds the rotation into the content, so the sheet keeps the size
    # asked for. Leaving it as /Rotate fitted the page in its unrotated frame,
    # which clipped the sides of a rotated landscape page (see test_verify).
    assert int(page.get("/Rotate", 0) or 0) % 360 == 0
    assert (round(float(page.mediabox.width)), round(float(page.mediabox.height))) ==         (round(pages.LETTER[0]), round(pages.LETTER[1]))


def test_rotation_without_fitting_stays_a_rotate_entry(tmp_path, two_pdfs):
    """Without a page size nothing is rewritten: the rotation is just /Rotate."""
    entry = assemble.MergeEntry(two_pdfs[0], "alpha", pages=[assemble.PageSpec(0, 90)])
    dest = tmp_path / "rot.pdf"
    assemble.merge([entry], dest)

    page = PdfReader(str(dest)).pages[0]
    assert int(page.get("/Rotate", 0) or 0) % 360 == 90


def test_separator_blank_adds_pages(tmp_path, two_pdfs):
    entries = [assemble.MergeEntry(two_pdfs[0], "alpha"), assemble.MergeEntry(two_pdfs[1], "beta")]
    dest = tmp_path / "sep.pdf"
    options = assemble.MergeOptions(page_size=pages.LETTER, separator="blank")
    report = assemble.merge(entries, dest, options=options)

    assert report.separators == 1, "one separator between two documents"
    assert report.pages == 6, "5 content pages plus 1 separator"


def test_separator_title_page_shows_the_label(tmp_path, two_pdfs):
    entries = [assemble.MergeEntry(two_pdfs[0], "alpha"), assemble.MergeEntry(two_pdfs[1], "beta")]
    dest = tmp_path / "septitle.pdf"
    options = assemble.MergeOptions(page_size=pages.LETTER, separator="title")
    assemble.merge(entries, dest, options=options)

    # Page 0 is alpha's first page; page 2 is the title separator for beta.
    assert "beta" in page_text(dest, 2)


def test_page_numbers_are_continuous(tmp_path, two_pdfs):
    entries = [assemble.MergeEntry(two_pdfs[0], "alpha"), assemble.MergeEntry(two_pdfs[1], "beta")]
    dest = tmp_path / "num.pdf"
    options = assemble.MergeOptions(
        page_size=pages.LETTER,
        numbering=assemble.PageNumbering(mode="continuous", template="{n} of {total}"),
    )
    assemble.merge(entries, dest, options=options)

    assert "1 of 5" in page_text(dest, 0)
    assert "5 of 5" in page_text(dest, 4)


def test_page_numbers_restart_per_document(tmp_path, two_pdfs):
    entries = [assemble.MergeEntry(two_pdfs[0], "alpha"), assemble.MergeEntry(two_pdfs[1], "beta")]
    dest = tmp_path / "numdoc.pdf"
    options = assemble.MergeOptions(
        page_size=pages.LETTER,
        numbering=assemble.PageNumbering(mode="per-document", template="{n}"),
    )
    assemble.merge(entries, dest, options=options)

    assert "1" in page_text(dest, 0)
    assert "1" in page_text(dest, 2), "beta restarts at 1"


def test_number_start_is_honoured(tmp_path, two_pdfs):
    dest = tmp_path / "start.pdf"
    options = assemble.MergeOptions(
        page_size=pages.LETTER,
        numbering=assemble.PageNumbering(mode="continuous", template="{n}", start=100),
    )
    assemble.merge([assemble.MergeEntry(two_pdfs[0], "alpha")], dest, options=options)
    assert "100" in page_text(dest, 0)


def test_header_and_footer_text(tmp_path, two_pdfs):
    dest = tmp_path / "hf.pdf"
    options = assemble.MergeOptions(
        page_size=pages.LETTER,
        header=assemble.HeaderFooter(left="Confidential"),
        footer=assemble.HeaderFooter(right="{doc}"),
    )
    assemble.merge([assemble.MergeEntry(two_pdfs[0], "alpha-report")], dest, options=options)

    text = page_text(dest, 0)
    assert "Confidential" in text
    assert "alpha-report" in text


def test_number_position_top_right(tmp_path, two_pdfs):
    dest = tmp_path / "pos.pdf"
    options = assemble.MergeOptions(
        page_size=pages.LETTER,
        numbering=assemble.PageNumbering(mode="continuous", position="top-right", template="p{n}"),
    )
    assemble.merge([assemble.MergeEntry(two_pdfs[0], "alpha")], dest, options=options)
    assert "p1" in page_text(dest, 0)


def test_metadata_is_written(tmp_path, two_pdfs):
    dest = tmp_path / "meta.pdf"
    options = assemble.MergeOptions(
        page_size=pages.LETTER, title="The pack", author="A Person", subject="Testing"
    )
    assemble.merge([assemble.MergeEntry(two_pdfs[0], "alpha")], dest, options=options)

    info = PdfReader(str(dest)).metadata
    assert info.get("/Title") == "The pack"
    assert info.get("/Author") == "A Person"


def test_encrypted_pdf_is_reported_not_fatal(tmp_path, two_pdfs):
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt("secret")
    locked = tmp_path / "locked.pdf"
    with open(locked, "wb") as handle:
        writer.write(handle)

    dest = tmp_path / "out.pdf"
    report = assemble.merge(
        [assemble.MergeEntry(locked, "locked"), assemble.MergeEntry(two_pdfs[0], "alpha")], dest
    )

    assert report.skipped and "encrypted" in report.skipped[0][1].lower()
    assert report.pages == 2, "the readable document still made it in"


def test_all_pages_are_the_target_size(tmp_path, two_pdfs):
    entries = [assemble.MergeEntry(two_pdfs[0], "alpha"), assemble.MergeEntry(two_pdfs[1], "beta")]
    dest = tmp_path / "sized.pdf"
    assemble.merge(entries, dest, page_size=pages.LETTER, margin=36.0)

    for page in PdfReader(str(dest)).pages:
        assert round(float(page.mediabox.width), 1) == 612.0
        assert round(float(page.mediabox.height), 1) == 792.0


def test_merge_is_atomic(tmp_path, two_pdfs):
    dest = tmp_path / "atomic.pdf"
    assemble.merge([assemble.MergeEntry(two_pdfs[0], "alpha")], dest)
    assert dest.exists()
    assert not dest.with_suffix(".pdf.tmp").exists()


# ------------------------------------------------ never overwrite an export


def test_an_existing_output_is_left_alone_and_numbered(tmp_path, two_pdfs):
    dest = tmp_path / "report.pdf"
    dest.write_bytes(b"the user's own file")
    entry = assemble.MergeEntry(two_pdfs[0], "alpha")

    first = assemble.merge([entry], dest)
    second = assemble.merge([entry], dest)

    assert dest.read_bytes() == b"the user's own file", "never replaced"
    assert first.output == tmp_path / "report (1).pdf"
    assert second.output == tmp_path / "report (2).pdf"
    assert first.requested == dest
    assert len(PdfReader(str(second.output)).pages) == 2
    assert second.verification.ok, "the check reads the file actually written"
    assert not list(tmp_path.glob("*.tmp"))


def test_a_free_name_is_used_as_given(tmp_path, two_pdfs):
    dest = tmp_path / "fresh.pdf"
    report = assemble.merge([assemble.MergeEntry(two_pdfs[0], "alpha")], dest)
    assert report.output == dest


def test_a_numbered_name_continues_its_numbering(tmp_path):
    (tmp_path / "report (3).pdf").write_bytes(b"x")
    assert assemble.reserve_output(tmp_path / "report (3).pdf") == \
        tmp_path / "report (4).pdf"


def test_overwrite_can_still_be_asked_for(tmp_path, two_pdfs):
    dest = tmp_path / "report.pdf"
    dest.write_bytes(b"old")
    options = assemble.MergeOptions(overwrite=True)

    report = assemble.merge([assemble.MergeEntry(two_pdfs[0], "alpha")], dest,
                            options=options)

    assert report.output == dest and dest.read_bytes().startswith(b"%PDF")


def test_a_failed_write_leaves_no_placeholder(tmp_path, two_pdfs, monkeypatch):
    dest = tmp_path / "report.pdf"
    dest.write_bytes(b"x")

    def refuse(self, stream):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(assemble.PdfWriter, "write", refuse)
    with pytest.raises(OSError):
        assemble.merge([assemble.MergeEntry(two_pdfs[0], "alpha")], dest)

    assert sorted(p.name for p in tmp_path.iterdir() if p.suffix != ".pdf" or
                  p.name.startswith("report")) == ["report.pdf"]
