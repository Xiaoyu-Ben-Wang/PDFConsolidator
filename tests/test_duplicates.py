"""Tests for the duplicate-page scan."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from consolidator import duplicates  # noqa: E402

LETTER = (
    "Dear customer, thank you for your order of 12 March. The invoice for "
    "the goods delivered is attached, payable within thirty days of receipt."
)


def text_pdf(path: Path, pages: list[str]) -> Path:
    """Write a PDF with one page per string; an empty string makes a blank page."""
    from reportlab.pdfgen import canvas

    page = canvas.Canvas(str(path), pagesize=(612, 792))
    for text in pages:
        y = 720
        for line in text.split("\n"):
            page.drawString(72, y, line)
            y -= 14
        page.showPage()
    page.save()
    return path


def picture_pdf(path: Path, shapes: list[str]) -> Path:
    """Write a PDF with one page per shape, drawn without any text."""
    from reportlab.pdfgen import canvas

    page = canvas.Canvas(str(path), pagesize=(612, 792))
    for shape in shapes:
        if shape == "box":
            page.rect(100, 400, 300, 300, fill=1)
        elif shape == "stripes":
            for x in range(50, 560, 60):
                page.rect(x, 50, 25, 690, fill=1)
        page.showPage()
    page.save()
    return path


def scan(pages):
    return duplicates.find_duplicates(duplicates.read_pages(pages))


def found(matches):
    return {(m.position, m.original): m.confidence for m in matches}


def test_identical_text_on_different_pages(tmp_path):
    pdf = text_pdf(tmp_path / "a.pdf", [LETTER, "Something else entirely, on its own page.",
                                        LETTER])
    matches = scan([(pdf, 0), (pdf, 1), (pdf, 2)])
    assert found(matches) == {(2, 0): 99}


def test_the_same_page_twice_is_certain(tmp_path):
    pdf = text_pdf(tmp_path / "a.pdf", [LETTER])
    assert found(scan([(pdf, 0), (pdf, 0)])) == {(1, 0): 100}


def test_whitespace_and_case_differences_still_match(tmp_path):
    reflowed = LETTER.upper().replace(", ", ",\n").replace(" the ", "  the   ")
    a = text_pdf(tmp_path / "a.pdf", [LETTER])
    b = text_pdf(tmp_path / "b.pdf", [reflowed])
    (match,) = scan([(a, 0), (b, 0)])
    assert match.confidence == 96
    assert "whitespace" in match.reason


def test_mostly_matching_text_scores_below_an_exact_match(tmp_path):
    edited = LETTER.replace("12 March", "14 March").replace("thirty", "sixty")
    pdf = text_pdf(tmp_path / "a.pdf", [LETTER, edited])
    (match,) = scan([(pdf, 0), (pdf, 1)])
    assert 70 <= match.confidence <= duplicates.SIMILAR_CAP
    assert "% of the text matches" in match.reason


def test_unrelated_pages_do_not_match(tmp_path):
    pdf = text_pdf(tmp_path / "a.pdf", [
        LETTER,
        "Minutes of the board meeting held on Tuesday. Present: five directors. "
        "The budget for next year was approved without changes.",
    ])
    assert scan([(pdf, 0), (pdf, 1)]) == []


def test_a_repeat_is_reported_against_the_first_copy(tmp_path):
    pdf = text_pdf(tmp_path / "a.pdf", [LETTER, LETTER, LETTER])
    assert found(scan([(pdf, 0), (pdf, 1), (pdf, 2)])) == {(1, 0): 99, (2, 0): 99}


def test_blank_pages_are_not_duplicates(tmp_path):
    pdf = text_pdf(tmp_path / "a.pdf", ["", ""])
    assert scan([(pdf, 0), (pdf, 1)]) == []


def test_pages_without_text_are_compared_by_image(tmp_path):
    pdf = picture_pdf(tmp_path / "a.pdf", ["box", "stripes", "box"])
    matches = scan([(pdf, 0), (pdf, 1), (pdf, 2)])
    assert found(matches) == {(2, 0): duplicates.IMAGE_CAP}
    assert "no text" in matches[0].reason


def test_a_text_page_never_matches_a_picture(tmp_path):
    a = text_pdf(tmp_path / "a.pdf", [LETTER])
    b = picture_pdf(tmp_path / "b.pdf", ["box"])
    assert scan([(a, 0), (b, 0)]) == []


def test_an_unreadable_page_is_skipped(tmp_path):
    pdf = text_pdf(tmp_path / "a.pdf", [LETTER])
    missing = tmp_path / "missing.pdf"
    assert scan([(missing, 0), (pdf, 0), (missing, 1)]) == []


def test_progress_is_reported_per_page(tmp_path):
    pdf = text_pdf(tmp_path / "a.pdf", [LETTER, LETTER])
    seen = []
    duplicates.read_pages([(pdf, 0), (pdf, 1)], on_progress=lambda d, t, _l: seen.append((d, t)))
    assert seen == [(1, 2), (2, 2)]


def test_many_pages_only_score_candidates():
    # 400 distinct pages plus one repeat; the sketch index keeps this fast.
    prints = [duplicates.make_fingerprint(("x", i), "page %d %s" % (i, "lorem ipsum " * i))
              for i in range(1, 400)]
    prints.append(duplicates.make_fingerprint(("y", 0), prints[200].text))
    matches = duplicates.find_duplicates(prints)
    assert (399, 200) in found(matches)
