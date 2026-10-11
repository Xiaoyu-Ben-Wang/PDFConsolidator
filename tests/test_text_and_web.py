"""Text files and web pages convert completely: encodings, scripts, long lines and pictures."""

from __future__ import annotations

import codecs
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from consolidator import convert, libreoffice, pages, text_render  # noqa: E402


def _text_of(pdf: Path) -> str:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(pdf))
    try:
        return "".join(page.get_textpage().get_text_range() for page in document)
    finally:
        document.close()


def _squash(text: str) -> str:
    return "".join(text.split())


def _convert(tmp_path: Path, data: bytes, name: str = "sample.txt") -> str:
    src = tmp_path / name
    src.write_bytes(data)
    dest = tmp_path / "out.pdf"
    convert.convert(src, dest, tmp_path, page_size=pages.LETTER)
    return _text_of(dest)


SCRIPTS = "English, Русский, Ελληνικά, 日本語, 中文, 한국어"

needs_fonts = pytest.mark.skipif(not text_render._load_faces(),
                                 reason="no TrueType fonts on this machine")


@pytest.mark.parametrize("encoding, bom", [
    ("utf-8", b""), ("utf-8", codecs.BOM_UTF8),
    ("utf-16-le", codecs.BOM_UTF16_LE), ("utf-16-be", codecs.BOM_UTF16_BE),
    ("utf-16-le", b""),
])
def test_unicode_encodings_are_detected(encoding, bom):
    text = "Notepad wrote this – café\r\nsecond line"
    assert text_render.decode_text(bom + text.encode(encoding)) == text


def test_windows_1252_is_the_fallback():
    assert text_render.decode_text("“quoted” café".encode("cp1252")) == \
        "“quoted” café"


@needs_fonts
def test_every_script_reaches_the_page(tmp_path):
    out = _convert(tmp_path, SCRIPTS.encode("utf-16"))
    assert _squash(SCRIPTS) in _squash(out)


@needs_fonts
def test_a_line_without_spaces_is_wrapped_not_cut_off(tmp_path):
    url = "https://example.com/" + "".join("segment%03d/" % n for n in range(60))
    out = _convert(tmp_path, url.encode())
    assert _squash(url) in _squash(out)


@needs_fonts
def test_long_lines_wrap_at_spaces_and_keep_every_word(tmp_path):
    words = " ".join("word%03d" % n for n in range(400))
    out = _convert(tmp_path, words.encode())
    assert _squash(words) == _squash(out)


def test_a_form_feed_starts_a_new_page(tmp_path):
    dest = tmp_path / "ff.pdf"
    assert text_render.render("page one\fpage two", dest, pages.LETTER) == 2


def test_wrapping_never_loses_or_reorders_characters():
    measure = text_render._Measure(9.5)
    for line in ("", "a", "x" * 500, "ab " * 200, " leading", "trailing ",
                 "mixed " + "y" * 300 + " tail"):
        pieces = text_render.wrap(line, 200.0, measure)
        assert _squash("".join(pieces)) == _squash(line)
        assert all(sum(measure.width(c) for c in p) <= 200.0 or len(p) == 1
                   for p in pieces)


# --------------------------------------------------------------- web pages ---


def test_a_saved_web_page_brings_its_pictures(tmp_path):
    from PIL import Image

    (tmp_path / "page_files").mkdir()
    Image.new("RGB", (40, 20), (200, 30, 30)).save(tmp_path / "page_files" / "pic one.png")
    html = (b"<html><body><img src='page_files/pic%20one.png'>"
            b"<img src=\"https://example.com/remote.png\"><img src=missing.png></body></html>")
    page = tmp_path / "page.htm"
    page.write_bytes(html)
    staged = tmp_path / "staged.htm"

    convert.stage_web_page(page, staged)

    out = staged.read_bytes()
    assert out.count(b"data:image/png;base64,") == 1
    assert b"https://example.com/remote.png" in out
    assert b"missing.png" in out


@pytest.mark.skipif(libreoffice.find_soffice() is None,
                    reason="LibreOffice is not installed")
def test_a_web_page_converts_with_its_picture(tmp_path):
    import pypdf
    from PIL import Image

    from consolidator import pipeline

    (tmp_path / "site" / "page_files").mkdir(parents=True)
    Image.new("RGB", (200, 100), (200, 30, 30)).save(tmp_path / "site" / "page_files" / "pic.png")
    page = tmp_path / "site" / "page.htm"
    page.write_text("<html><body><p>text</p><img src='page_files/pic.png'></body></html>")

    report = pipeline.convert_sources([page], tmp_path / "work")

    segment = report.sources[0].segments[0]
    assert segment.ok
    assert len(pypdf.PdfReader(str(segment.pdf)).pages[0].images) == 1
