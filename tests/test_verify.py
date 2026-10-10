"""The output check: it must find real loss, and only real loss."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from consolidator import assemble, pages, verify  # noqa: E402


def _pdf(path: Path, pagesize=(612, 792), words=("alpha", "beta"), image=False,
         at=None) -> Path:
    """A one-page PDF with words at its four edges, optionally a picture."""
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
    """Rotating a landscape page and fitting it to portrait clipped its sides."""
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
