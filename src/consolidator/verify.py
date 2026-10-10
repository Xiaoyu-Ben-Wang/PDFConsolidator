"""Check a written PDF against its sources: is anything missing?

Assembly rewrites pages - fitting them to a size, rotating them, drawing page
numbers on them - and any of those steps can lose content without an error: a
paragraph whose drawing instructions did not survive, a picture, text pushed
outside the page. The only way to know is to look, so after the output is
written every page is compared with the page it came from:

* **Words.** Every word on the source page must be on the output page. Extra
  words are fine - page numbers and headers add some - but a source word the
  output lacks is a finding.
* **Edges.** Text the source showed must not have been pushed off the output
  page, where it is still in the file but no longer visible.
* **Pictures.** The output page must have at least as many images as the
  source page.

pdfium does the reading, because it is the engine the preview uses: what it
finds is what a viewer shows. It is a check, not a proof - text drawn as
outlines has no words to compare - but a page that fails it has certainly lost
something.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .pdfium_guard import PDFIUM_LOCK


@dataclass
class PageProblem:
    """One output page that is missing something its source page had."""

    output_page: int            # 1-based, as a reader counts
    source: str                 # the label the page came from
    source_page: int            # 1-based page within that source
    missing_words: int = 0
    words_in_source: int = 0
    sample: list[str] = field(default_factory=list)
    missing_images: int = 0
    off_page_chars: int = 0

    def describe(self) -> str:
        parts = []
        if self.missing_words:
            parts.append("%d of %d word(s) missing (e.g. %s)"
                         % (self.missing_words, self.words_in_source,
                            ", ".join(repr(w) for w in self.sample)))
        if self.missing_images:
            parts.append("%d picture(s) missing" % self.missing_images)
        if self.off_page_chars:
            parts.append("%d character(s) pushed off the page" % self.off_page_chars)
        return "page %d (%s, page %d): %s" % (
            self.output_page, self.source, self.source_page, "; ".join(parts))


@dataclass
class Verification:
    checked: int = 0
    problems: list[PageProblem] = field(default_factory=list)
    error: str = ""             # the check itself could not run

    @property
    def ok(self) -> bool:
        return not self.problems and not self.error


# How many source documents to keep open while checking.
OPEN_SOURCES = 16

# A character this far outside the page box is off the page, not on its edge.
_EDGE_TOLERANCE = 2.0


def _page_content(document, index: int) -> tuple[Counter, int, int]:
    """Words, image count and off-page character count of one page.

    Words come from the page's whole text. Restricting extraction to the page
    box looked like the way to catch clipping, but pdfium drops glyphs whose box
    merely touches the edge - a bullet, after scaling - which reads as loss when
    nothing was lost. Clipping is measured separately, by counting characters
    whose centre lies outside the visible page.
    """
    import pypdfium2.raw as pdfium_c

    page = document[index]
    try:
        left, bottom, right, top = page.get_cropbox()
        textpage = page.get_textpage()
        try:
            words = Counter(textpage.get_text_range().split())
            outside = 0
            for char in range(textpage.count_chars()):
                box = textpage.get_charbox(char)
                if box[2] - box[0] <= 0 and box[3] - box[1] <= 0:
                    continue                    # spaces and line breaks
                x = (box[0] + box[2]) / 2.0
                y = (box[1] + box[3]) / 2.0
                if (x < left - _EDGE_TOLERANCE or x > right + _EDGE_TOLERANCE
                        or y < bottom - _EDGE_TOLERANCE or y > top + _EDGE_TOLERANCE):
                    outside += 1
        finally:
            textpage.close()
        images = sum(1 for _ in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE]))
        return words, images, outside
    finally:
        page.close()


def check(output: Path, page_map: list, sample_size: int = 5,
          should_stop=None) -> Verification:
    """Compare every page of ``output`` with its source page.

    ``page_map`` has one entry per output page: ``(source_pdf, index, label)``,
    or None for a page with no source (a separator). The lock is taken per page,
    not for the whole run, so the preview stays usable while a long document is
    checked.
    """
    import pypdfium2 as pdfium

    result = Verification()
    sources: dict[str, object] = {}
    try:
        with PDFIUM_LOCK:
            out_doc = pdfium.PdfDocument(str(output))
        try:
            for out_index, entry in enumerate(page_map):
                if should_stop and should_stop():
                    break
                if entry is None:
                    continue
                source_pdf, source_index, label = entry
                with PDFIUM_LOCK:
                    key = str(source_pdf)
                    doc = sources.pop(key, None)
                    if doc is None:
                        # A few sources stay open: pages from different files
                        # interleave once the user rearranges them, and reopening
                        # on every switch cost more than the comparison itself.
                        # A few, not all - thousands of open documents is a
                        # resource problem of its own.
                        while len(sources) >= OPEN_SOURCES:
                            sources.pop(next(iter(sources))).close()
                        doc = pdfium.PdfDocument(key)
                    sources[key] = doc          # most recently used last
                    want_words, want_images, want_off = _page_content(doc, source_index)
                    got_words, got_images, got_off = _page_content(out_doc, out_index)
                result.checked += 1
                missing = want_words - got_words
                lost_images = max(0, want_images - got_images)
                # Text the source showed that the output has pushed off the page.
                clipped = max(0, got_off - want_off)
                if missing or lost_images or clipped:
                    result.problems.append(PageProblem(
                        output_page=out_index + 1,
                        source=label,
                        source_page=source_index + 1,
                        missing_words=sum(missing.values()),
                        words_in_source=sum(want_words.values()),
                        sample=[word for word, _n in missing.most_common(sample_size)],
                        missing_images=lost_images,
                        off_page_chars=clipped,
                    ))
        finally:
            with PDFIUM_LOCK:
                for doc in sources.values():
                    doc.close()
                out_doc.close()
    except Exception as exc:
        result.error = "%s: %s" % (type(exc).__name__, exc)
    return result
