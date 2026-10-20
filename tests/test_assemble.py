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
    # Fitting applies the rotation to the content, so the page keeps the
    # requested size. A /Rotate entry would fit the page in its unrotated frame
    # and clip a rotated landscape page (see test_verify).
    assert int(page.get("/Rotate", 0) or 0) % 360 == 0
    assert (round(float(page.mediabox.width)), round(float(page.mediabox.height))) ==         (round(pages.LETTER[0]), round(pages.LETTER[1]))


def _rotated_page_with_caption(path):
    """A /Rotate 90 page whose caption is a FreeText annotation with an appearance.

    The caption reads horizontally when displayed, so in the page's own
    (unrotated) coordinates its /Rect is tall and narrow, and its appearance
    /Matrix turns the text to fit.
    """
    from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject,
                               FloatObject, NameObject, NumberObject, RectangleObject)

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    page[NameObject("/Rotate")] = NumberObject(90)
    appearance = DecodedStreamObject()
    appearance.set_data(b"1 0 0 rg 0 0 300 30 re f\n")
    appearance.update({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Form"),
        NameObject("/BBox"): ArrayObject(FloatObject(v) for v in (0, 0, 300, 30)),
        NameObject("/Matrix"): ArrayObject(FloatObject(v) for v in (0, 1, -1, 0, 0, 0)),
    })
    annotation = DictionaryObject({
        NameObject("/Type"): NameObject("/Annot"),
        NameObject("/Subtype"): NameObject("/FreeText"),
        NameObject("/Rect"): RectangleObject((100, 150, 130, 450)),
        NameObject("/AP"): DictionaryObject(
            {NameObject("/N"): writer._add_object(appearance)}),
    })
    page[NameObject("/Annots")] = ArrayObject([writer._add_object(annotation)])
    with open(path, "wb") as handle:
        writer.write(handle)


@pytest.mark.parametrize("extra", [0, 90, 180])
def test_annotation_appearance_turns_with_a_rotated_page(tmp_path, extra):
    """The appearance must keep the shape of the annotation's rectangle.

    Only the /Rect used to be turned, so the viewer squeezed a wide caption
    into a tall box (or the reverse), and the text came out as smears.
    """
    source = tmp_path / "caption.pdf"
    _rotated_page_with_caption(source)
    dest = tmp_path / "out.pdf"
    entry = assemble.MergeEntry(source, "caption", pages=[assemble.PageSpec(0, extra)])
    assemble.merge([entry], dest, page_size=pages.LETTER)

    annotation = PdfReader(str(dest)).pages[0]["/Annots"][0].get_object()
    x0, y0, x1, y1 = (float(v) for v in annotation["/Rect"])
    appearance = annotation["/AP"]["/N"].get_object()
    matrix = tuple(float(v) for v in appearance["/Matrix"])
    corners = [assemble.apply(matrix, x, y) for x, y in ((0, 0), (300, 0), (0, 30), (300, 30))]
    width = max(c[0] for c in corners) - min(c[0] for c in corners)
    height = max(c[1] for c in corners) - min(c[1] for c in corners)
    assert width / height == pytest.approx((x1 - x0) / (y1 - y0), rel=1e-3)
    # Displayed upright before, so upright after: the caption's text runs
    # left to right along the bottom edge of the box (90 + extra turns in all).
    a, b, _c, _d, _e, _f = matrix
    length = (a * a + b * b) ** 0.5
    expected = {0: (1, 0), 90: (0, -1), 180: (-1, 0)}[extra]
    assert (round(a / length), round(b / length)) == expected


def test_rotation_without_fitting_stays_a_rotate_entry(tmp_path, two_pdfs):
    """Without a page size, the rotation is stored as a /Rotate entry."""
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


# ------------------------------------------------------- long header/footer


LONG_NAME = ("Quarterly_financial_report_for_the_northern_region_including_all_"
             "appendices_and_supporting_schedules_FINAL_v3_reviewed.docx")


def _words_inside(path, index):
    """Return the page width, its text and each character's box, using pdfium."""
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    try:
        page = document[index]
        width = page.get_width()
        text = page.get_textpage()
        boxes = [text.get_charbox(i) for i in range(text.count_chars())]
        return width, text.get_text_range(), boxes
    finally:
        document.close()


def test_a_long_footer_wraps_instead_of_running_off_the_page(tmp_path, two_pdfs):
    dest = tmp_path / "out.pdf"
    options = assemble.MergeOptions(
        page_size=pages.resolve("letter"),
        footer=assemble.HeaderFooter(center="{doc}"),
        numbering=assemble.PageNumbering(mode="continuous", position="bottom-right",
                                         template="{n} of {total}"),
    )
    assemble.merge([assemble.MergeEntry(two_pdfs[0], LONG_NAME)], dest, options=options)

    width, text, boxes = _words_inside(dest, 0)
    assert "".join(LONG_NAME.split()) in "".join(text.split()), "every character is kept"
    assert "1 of 2" in text
    assert all(0 <= left and right <= width for left, _b, right, _t in boxes), \
        "no character may fall outside the page"
    lines = {round(bottom) for _l, bottom, _r, _t in boxes if bottom < 72}
    assert len(lines) > 1, "the name wraps onto more than one line"


def test_wrapping_prefers_breaking_a_file_name_at_a_separator():
    lines = assemble._wrap(LONG_NAME, 200, "Helvetica", 9)

    assert "".join(lines) == LONG_NAME
    assert all(line.endswith(("_", "-", ".")) for line in lines[:-1]), lines


def test_wrapping_keeps_words_whole_when_it_can():
    lines = assemble._wrap("alpha beta gamma delta epsilon", 60, "Helvetica", 9)

    assert " ".join(lines) == "alpha beta gamma delta epsilon"
    assert all(word in ("alpha", "beta", "gamma", "delta", "epsilon")
               for line in lines for word in line.split())


def test_a_very_long_slot_stops_at_three_lines_with_an_ellipsis():
    lines = assemble._wrap("x" * 2000, 200, "Helvetica", 9)

    assert len(lines) == assemble.MAX_SLOT_LINES
    assert lines[-1].endswith("\u2026")


def test_a_short_footer_is_still_one_line():
    assert assemble._wrap("report.docx", 500, "Helvetica", 9) == ["report.docx"]
