"""Prototype 1 tests.

The LibreOffice-backed tests are skipped automatically when no engine is
installed, so the suite still runs on a machine without it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from consolidator import assemble, convert, libreoffice, pages, pipeline  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "generated"

requires_lo = pytest.mark.skipif(
    libreoffice.find_soffice() is None, reason="LibreOffice is not installed"
)


@pytest.fixture(scope="session")
def fixtures():
    if not FIXTURES.exists():
        pytest.skip("fixtures not generated; run tools/make_fixtures.py")
    return FIXTURES


# ----------------------------------------------------------------- gather ----


def test_gather_sorts_naturally(tmp_path):
    for name in ["file10.txt", "file2.txt", "file1.txt"]:
        (tmp_path / name).write_text("x")
    found = [p.name for p in pipeline.gather([str(tmp_path)])]
    assert found == ["file1.txt", "file2.txt", "file10.txt"]


def test_gather_skips_directories_but_recurses(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.txt").write_text("x")
    (tmp_path / "top.txt").write_text("x")
    found = {p.name for p in pipeline.gather([str(tmp_path)])}
    assert found == {"top.txt", "deep.txt"}


# ------------------------------------------------------------------ pages ----


def test_page_size_resolution():
    assert pages.resolve("letter") == (612.0, 792.0)
    assert pages.resolve("original") is None
    assert pages.resolve("LETTER") == (612.0, 792.0)


def test_unknown_page_size_rejected():
    with pytest.raises(ValueError):
        pages.resolve("A3")


# -------------------------------------------------------------- conversion ----


def test_text_conversion_treats_markdown_literally(tmp_path):
    src = tmp_path / "notes.md"
    src.write_text("# Heading\n\n- bullet\n")
    dest = tmp_path / "out.pdf"
    convert.convert_text(src, dest, tmp_path, pages.LETTER)

    from pypdf import PdfReader

    text = PdfReader(str(dest)).pages[0].extract_text()
    # The Markdown must survive verbatim rather than being interpreted.
    assert "# Heading" in text
    assert "- bullet" in text


def test_pdf_passthrough_is_byte_identical(tmp_path, fixtures):
    src = fixtures / "existing.pdf"
    dest = tmp_path / "copy.pdf"
    convert.convert(src, dest, tmp_path, pages.LETTER)
    assert dest.read_bytes() == src.read_bytes()


def test_png_with_alpha_is_flattened_onto_white(tmp_path, fixtures):
    """A transparent PNG must not come out with a black background."""
    import pypdfium2 as pdfium

    dest = tmp_path / "alpha.pdf"
    convert.convert_image(fixtures / "diagram.png", dest, tmp_path, pages.LETTER)

    image = pdfium.PdfDocument(str(dest))[0].render(scale=1.0).to_pil().convert("RGB")
    corner = image.getpixel((2, 2))
    assert min(corner) > 240, "background should be white, got %s" % (corner,)


def test_unsupported_extension_reports_clearly(tmp_path):
    src = tmp_path / "thing.pub"
    src.write_bytes(b"x")
    with pytest.raises(convert.ConversionError, match="Publisher"):
        convert.convert(src, tmp_path / "o.pdf", tmp_path, None)


def test_empty_file_is_rejected(tmp_path):
    src = tmp_path / "empty.txt"
    src.write_bytes(b"")
    with pytest.raises(convert.ConversionError, match="empty"):
        convert.convert(src, tmp_path / "o.pdf", tmp_path, pages.LETTER)


# --------------------------------------------------------------- assembly ----


def test_merge_normalises_page_size(tmp_path, fixtures):
    src = fixtures / "existing.pdf"      # A4 source
    entry = assemble.MergeEntry(src, "existing")
    dest = tmp_path / "merged.pdf"

    report = assemble.merge([entry], dest, page_size=pages.LETTER, margin=36.0)

    from pypdf import PdfReader

    reader = PdfReader(str(dest))
    assert report.pages == 2
    for page in reader.pages:
        assert round(float(page.mediabox.width), 1) == 612.0
        assert round(float(page.mediabox.height), 1) == 792.0


def test_merge_keeps_page_count_and_bookmarks(tmp_path, fixtures):
    entries = [
        assemble.MergeEntry(fixtures / "existing.pdf", "existing"),
        assemble.MergeEntry(fixtures / "photo.jpg", "photo"),
    ]
    # Images must be converted before merging.
    work = tmp_path / "w"
    work.mkdir()
    photo_pdf = tmp_path / "photo.pdf"
    convert.convert_image(fixtures / "photo.jpg", photo_pdf, work, pages.LETTER)
    entries[1] = assemble.MergeEntry(photo_pdf, "photo")

    dest = tmp_path / "merged.pdf"
    report = assemble.merge(entries, dest, page_size=pages.LETTER, margin=36.0)

    from pypdf import PdfReader

    assert report.entries == 2
    assert report.pages == 3
    assert len(PdfReader(str(dest)).outline) == 2


def test_merge_is_atomic_on_success(tmp_path, fixtures):
    dest = tmp_path / "out.pdf"
    assemble.merge([assemble.MergeEntry(fixtures / "existing.pdf", "x")], dest)
    assert dest.exists()
    assert not dest.with_suffix(".pdf.tmp").exists()


# ------------------------------------------------------------- LibreOffice ----


@requires_lo
@pytest.mark.parametrize("name", ["memo.docx", "budget.xlsx", "deck.pptx"])
def test_office_conversion_produces_pdf(tmp_path, fixtures, name):
    src = fixtures / name
    dest = tmp_path / (src.stem + ".pdf")
    libreoffice.convert_one(src, dest)
    assert dest.exists() and dest.stat().st_size > 0


@requires_lo
def test_libreoffice_env_has_no_python_vars():
    """The host's PYTHONPATH breaks LibreOffice's own bootstrap."""
    import os as _os

    _os.environ["PYTHONPATH"] = r"C:\somewhere\else"
    env = libreoffice.clean_env(Path("."))
    assert "PYTHONPATH" not in env
    assert env["TMP"] and env["TEMP"]


@requires_lo
def test_convert_one_leaves_no_scratch_behind(tmp_path, fixtures):
    """Regression: LibreOffice marks its ~lock files read-only.

    Windows refuses to delete a read-only file, so a plain rmtree fails with
    WinError 5 and every conversion silently leaks its scratch directory.
    """
    import tempfile

    root = Path(tempfile.gettempdir())
    before = {p.name for p in root.glob("consolidator_lo_*")}

    libreoffice.convert_one(fixtures / "memo.docx", tmp_path / "memo.pdf")

    after = {p.name for p in root.glob("consolidator_lo_*")}
    assert after == before, "scratch directories leaked: %s" % sorted(after - before)


# ---------------------------------------------------------------- pipeline ----


@requires_lo
def test_end_to_end_build(tmp_path, fixtures):
    sources = pipeline.gather([str(fixtures)])
    dest = tmp_path / "combined.pdf"

    report = pipeline.build(sources, dest, page_size_name="letter")

    assert not report.failed, [f"{o.source.name}: {o.detail}" for o in report.failed]
    assert dest.exists()
    assert report.merged.pages > 0
    assert len(report.sources) == len(sources)


def test_end_to_end_without_office(tmp_path, fixtures):
    """The merge half works with no Office engine involved at all."""
    sources = [p for p in pipeline.gather([str(fixtures)]) if p.suffix.lower() != ".docx"]
    sources = [p for p in sources if p.suffix.lower() in (".pdf", ".jpg", ".png", ".txt", ".md")]
    dest = tmp_path / "combined.pdf"

    report = pipeline.build(sources, dest, page_size_name="letter")

    assert not report.failed, [o.detail for o in report.failed]
    assert dest.exists()
