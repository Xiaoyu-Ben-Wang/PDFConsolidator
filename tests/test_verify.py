"""Output verification: it reports real content loss and nothing else."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from consolidator import assemble, pages, verify  # noqa: E402


def _pdf(path: Path, pagesize=(612, 792), words=("alpha", "beta"), image=False,
         at=None) -> Path:
    """Write a one-page PDF with words at its four edges and an optional picture."""
    from PIL import Image
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    width, height = pagesize
    page = canvas.Canvas(str(path), pagesize=pagesize)
    page.setFont("Helvetica", 10)
    spots = at or [(8, height / 2), (width - 60, height / 2),
                   (width / 2, 8), (width / 2, height - 16)]
    for word, (x, y) in zip(list(words) * 4, spots):
        page.drawString(x, y, word)
    if image:
        buffer = io.BytesIO()
        Image.new("RGB", (20, 20), (200, 0, 0)).save(buffer, "PNG")
        buffer.seek(0)
        page.drawImage(ImageReader(buffer), width / 3, height / 3, 40, 40)
    page.showPage()
    page.save()
    return path


def _merge(tmp_path, source, rotation=0, **options):
    dest = tmp_path / "out.pdf"
    report = assemble.merge(
        [assemble.MergeEntry(source, "src", [assemble.PageSpec(0, rotation)])],
        dest, options=assemble.MergeOptions(**options))
    return dest, report


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("size", [(612, 792), (842, 595), (720, 540)])
def test_fitting_a_rotated_page_keeps_every_edge(tmp_path, rotation, size):
    """A rotated page fitted to the target size keeps all four edges on the page."""
    import pypdfium2 as pdfium

    source = _pdf(tmp_path / "src.pdf", size)
    target = pages.resolve("a4")

    dest, report = _merge(tmp_path, source, rotation, page_size=target, margin=36)

    assert report.verification.ok, [p.describe() for p in report.verification.problems]
    document = pdfium.PdfDocument(str(dest))
    width, height = document[0].get_size()
    assert (round(width), round(height)) == (round(target[0]), round(target[1])), \
        "the sheet is the size asked for, not turned with the content"
    document.close()


def test_every_variant_of_assembly_verifies_clean(tmp_path):
    source = _pdf(tmp_path / "src.pdf", image=True)
    entries = [assemble.MergeEntry(source, "a", [assemble.PageSpec(0, 90)]),
               assemble.MergeEntry(source, "b", None)]
    options = assemble.MergeOptions(
        page_size=pages.resolve("letter"), margin=36, separator="title",
        numbering=assemble.PageNumbering(mode="continuous", template="{n}/{total}"),
        header=assemble.HeaderFooter(left="{doc}"))

    report = assemble.merge(entries, tmp_path / "out.pdf", options=options)

    assert report.pages == 3
    assert report.page_map[1] is None, "the separator has no source"
    assert report.verification.checked == 2
    assert report.verification.ok


def test_missing_words_are_found(tmp_path):
    source = _pdf(tmp_path / "src.pdf", words=("alpha", "beta", "gamma", "delta"))
    other = _pdf(tmp_path / "other.pdf", words=("alpha", "beta"))

    result = verify.check(other, [(source, 0, "src")])

    assert len(result.problems) == 1
    problem = result.problems[0]
    assert problem.missing_words == 2
    assert set(problem.sample) == {"gamma", "delta"}
    assert "page 1 (src, page 1)" in problem.describe()


def test_a_missing_picture_is_found(tmp_path):
    source = _pdf(tmp_path / "src.pdf", image=True)
    other = _pdf(tmp_path / "other.pdf", image=False)

    result = verify.check(other, [(source, 0, "src")])

    assert [p.missing_images for p in result.problems] == [1]


def test_text_pushed_off_the_page_is_found(tmp_path):
    source = _pdf(tmp_path / "src.pdf", words=("edge",), at=[(300, 400)])
    off = _pdf(tmp_path / "off.pdf", words=("edge",), at=[(-80, 400)])

    result = verify.check(off, [(source, 0, "src")])

    assert [p.off_page_chars for p in result.problems] == [4]


def test_a_check_that_cannot_run_says_so_and_does_not_raise(tmp_path):
    result = verify.check(tmp_path / "missing.pdf", [(tmp_path / "x.pdf", 0, "x")])

    assert result.error and not result.ok


def test_the_check_can_be_switched_off(tmp_path):
    source = _pdf(tmp_path / "src.pdf")
    _dest, report = _merge(tmp_path, source, verify=False)
    assert report.verification is None


# ------------------------------------- page content is copied, not parsed


def _raw_page_pdf(path: Path, content: bytes, mediabox=(0, 0, 612, 792), rotate=0,
                  link=None) -> Path:
    """Write a one-page PDF whose content stream is exactly ``content``."""
    from pypdf import PdfWriter
    from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject,
                               FloatObject, NameObject, NumberObject, RectangleObject,
                               TextStringObject)

    writer = PdfWriter()
    writer.add_blank_page(612, 792)
    page = writer.pages[0]
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject(
        {NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(content)
    page[NameObject("/Contents")] = writer._add_object(stream)
    page[NameObject("/MediaBox")] = RectangleObject(mediabox)
    page[NameObject("/Rotate")] = NumberObject(rotate)
    if link:
        annotation = DictionaryObject({
            NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Link"),
            NameObject("/Rect"): ArrayObject([FloatObject(v) for v in link]),
            NameObject("/A"): DictionaryObject({NameObject("/S"): NameObject("/URI"),
                                               NameObject("/URI"): TextStringObject("https://example.com")})})
        page[NameObject("/Annots")] = ArrayObject([writer._add_object(annotation)])
    writer.write(str(path))
    return path


LONG_NUMBER = b"0." + b"0" * 108 + b"1"          # 112 characters


def _text(content: bytes, x=72, y=700) -> bytes:
    return b"BT /F1 12 Tf %d %d Td (%s) Tj ET\n" % (x, y, content)


@pytest.mark.parametrize("options", [
    dict(page_size=pages.resolve("a4"), margin=36),
    dict(page_size=pages.resolve("a4"), margin=36,
         numbering=assemble.PageNumbering(mode="continuous")),
    dict(numbering=assemble.PageNumbering(mode="continuous")),
    dict(),
])
def test_a_page_pypdf_cannot_parse_is_still_exported(tmp_path, options):
    """A page pypdf cannot parse is still exported and verifies clean.

    Guards against a 112-character number raising pypdf's LimitReachedError.
    """
    content = _text(b"Paragraph before") + LONG_NUMBER + b" w\n" + \
        _text(b"Paragraph after", y=650)
    source = _raw_page_pdf(tmp_path / "long.pdf", content)

    _dest, report = _merge(tmp_path, source, 90, **options)

    assert report.pages == 1 and not report.skipped and not report.as_images
    assert report.verification.ok, [p.describe() for p in report.verification.problems]


def test_page_content_is_kept_byte_for_byte(tmp_path):
    from pypdf import PdfReader

    content = _text(b"Exactly these bytes") + LONG_NUMBER + b" w\n"
    source = _raw_page_pdf(tmp_path / "src.pdf", content)

    dest, _report = _merge(tmp_path, source, 0, page_size=pages.resolve("a4"), margin=36)

    page = PdfReader(str(dest)).pages[0]
    streams = [s.get_object().get_data() for s in page["/Contents"]]
    assert content in streams, "the original stream travels unchanged, wrapped"


def test_a_page_whose_box_does_not_start_at_zero_is_fitted_whole(tmp_path):
    """A page whose MediaBox has a non-zero origin is fitted without clipping."""
    content = b"".join(_text(word, x, y) for word, x, y in (
        (b"left", 2010, 1400), (b"right", 2550, 1400),
        (b"bottom", 2300, 1010), (b"top", 2300, 1780)))
    source = _raw_page_pdf(tmp_path / "offset.pdf", content,
                           mediabox=(2000, 1000, 2612, 1792))

    _dest, report = _merge(tmp_path, source, 0, page_size=pages.resolve("a4"), margin=36)

    assert report.verification.ok, [p.describe() for p in report.verification.problems]


def test_a_link_moves_with_its_page_and_its_twin_does_not(tmp_path):
    from pypdf import PdfReader

    source = _raw_page_pdf(tmp_path / "link.pdf", _text(b"click"),
                           link=(72, 690, 200, 715))
    entries = [assemble.MergeEntry(source, "a", [assemble.PageSpec(0, 0)]),
               assemble.MergeEntry(source, "b", [assemble.PageSpec(0, 90)])]
    dest = tmp_path / "out.pdf"

    assemble.merge(entries, dest, options=assemble.MergeOptions(
        page_size=pages.resolve("letter"), margin=0))

    first, second = PdfReader(str(dest)).pages
    rect_a = [round(float(v)) for v in first["/Annots"][0].get_object()["/Rect"]]
    rect_b = [round(float(v)) for v in second["/Annots"][0].get_object()["/Rect"]]
    assert rect_a == [72, 690, 200, 715], "unrotated, letter onto letter: unchanged"
    assert rect_b != rect_a, "the rotated copy's link moved with its content"


def test_an_untouched_page_keeps_its_content_and_only_gains_a_rotation(tmp_path):
    from pypdf import PdfReader

    content = _text(b"as it was")
    source = _raw_page_pdf(tmp_path / "src.pdf", content, rotate=90)

    dest, _report = _merge(tmp_path, source, 90)

    page = PdfReader(str(dest)).pages[0]
    assert int(page["/Rotate"]) == 180
    assert page["/Contents"].get_object().get_data() == content


def test_a_page_that_cannot_be_copied_is_included_as_an_image(tmp_path, monkeypatch):
    import pypdfium2 as pdfium

    good = _pdf(tmp_path / "good.pdf", words=("kept",))
    bad = _pdf(tmp_path / "bad.pdf", words=("pictured",))
    real = assemble.normalise
    calls = []

    def flaky(page, *args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise ValueError("cannot copy this page")
        return real(page, *args, **kwargs)

    monkeypatch.setattr(assemble, "normalise", flaky)
    entries = [assemble.MergeEntry(good, "good"), assemble.MergeEntry(bad, "bad"),
               assemble.MergeEntry(good, "good again")]
    dest = tmp_path / "out.pdf"

    report = assemble.merge(entries, dest, options=assemble.MergeOptions(
        page_size=pages.resolve("letter"), margin=36))

    assert report.pages == 3, "one bad page does not cost the export"
    assert [(p[0], p[1], p[2]) for p in report.as_images] == [(2, "bad", 1)]
    assert "cannot copy this page" in report.as_images[0][3]
    assert report.page_map[1] is None, "an image page is not compared word for word"
    document = pdfium.PdfDocument(str(dest))
    images = list(document[1].get_objects())
    assert images and document[1].get_size() == document[0].get_size()
    document.close()
