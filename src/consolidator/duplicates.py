"""Find pages that repeat an earlier page.

Each page is reduced to a fingerprint: its text with whitespace removed and
letters case-folded, and, for pages with little or no text (scans, images), a
small difference hash of the rendered page. A later page is compared with the
earlier pages and reported against its best match, with a confidence score:

* 100  the same page of the same file appears twice;
* 99   the extracted text is identical;
* 96   the text is identical once whitespace and letter case are ignored;
* <=94 most of the text matches (character trigrams, ignoring whitespace);
* <=90 neither page has text and the rendered pages look alike.

Comparing every pair is quadratic, so text pages are first matched through a
small MinHash sketch and only pages that share part of their sketch are scored
in full.

This module does not import Qt, so it can be tested without a display.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .pdfium_guard import PDFIUM_LOCK

# A page with fewer non-space characters than this is compared by its image,
# because a few characters (a page number, "Page 2") match too easily.
MIN_TEXT = 20

# Lowest text similarity (trigram Dice coefficient) reported, and the most a
# merely similar page can score, so it stays below an exact match.
MIN_SIMILARITY = 0.70
SIMILAR_CAP = 94

# Image comparison: hash size, lowest similarity reported, and top score.
HASH_WIDTH, HASH_HEIGHT = 16, 16
MIN_IMAGE_SIMILARITY = 0.92
IMAGE_CAP = 90
# Rendered pages whose grey levels span less than this are treated as blank.
BLANK_RANGE = 8

# MinHash sketch size and the share of it two pages must have in common to be
# scored in full. A Jaccard index of about 0.5 corresponds to a Dice
# coefficient of about 0.67, just under MIN_SIMILARITY.
SKETCH_SIZE = 48
MIN_SHARED = 12

_SPACE = re.compile(r"\s+")


@dataclass
class Fingerprint:
    """What is compared for one page."""

    key: tuple[str, int]                # (pdf, page index)
    text: str = ""                      # text as extracted
    squeezed: str = ""                  # no whitespace, case-folded
    trigrams: Counter = field(default_factory=Counter)
    sketch: frozenset = frozenset()
    image_hash: int | None = None       # pages without enough text
    blank: bool = False

    @property
    def has_text(self) -> bool:
        return len(self.squeezed) >= MIN_TEXT


@dataclass
class Match:
    """A page that repeats an earlier one. Positions are 0-based."""

    position: int
    original: int
    confidence: int
    reason: str


def squeeze(text: str) -> str:
    """Return ``text`` without whitespace and with letter case folded."""
    return _SPACE.sub("", text).casefold()


def trigrams(squeezed: str) -> Counter:
    return Counter(squeezed[i:i + 3] for i in range(max(len(squeezed) - 2, 0)))


def sketch(grams) -> frozenset:
    """Return the SKETCH_SIZE smallest trigram hashes (bottom-k MinHash)."""
    hashes = sorted(
        int.from_bytes(hashlib.blake2b(g.encode("utf-8", "replace"), digest_size=8).digest(),
                       "little")
        for g in grams
    )
    return frozenset(hashes[:SKETCH_SIZE])


def dice(a: Counter, b: Counter) -> float:
    """Return the Dice coefficient of two trigram multisets (0..1)."""
    total = sum(a.values()) + sum(b.values())
    if not total:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    shared = sum(min(count, b[gram]) for gram, count in a.items() if gram in b)
    return 2.0 * shared / total


def make_fingerprint(key: tuple[str, int], text: str, image=None) -> Fingerprint:
    """Build a fingerprint from a page's text and, optionally, a PIL image."""
    squeezed = squeeze(text)
    found = Fingerprint(key=key, text=text, squeezed=squeezed)
    if found.has_text:
        found.trigrams = trigrams(squeezed)
        found.sketch = sketch(found.trigrams)
    elif image is not None:
        found.image_hash, found.blank = image_hash(image)
    else:
        found.blank = not squeezed
    return found


def image_hash(image) -> tuple[int, bool]:
    """Return a difference hash of a PIL image, and whether the image is blank."""
    grey = image.convert("L")
    low, high = grey.getextrema()
    if high - low < BLANK_RANGE:
        return 0, True
    small = grey.resize((HASH_WIDTH + 1, HASH_HEIGHT))
    pixels = small.tobytes()            # one byte per pixel in "L" mode
    value = 0
    for row in range(HASH_HEIGHT):
        start = row * (HASH_WIDTH + 1)
        for column in range(HASH_WIDTH):
            value = (value << 1) | (pixels[start + column] > pixels[start + column + 1])
    return value, False


def compare(a: Fingerprint, b: Fingerprint) -> tuple[int, str] | None:
    """Return (confidence, reason) if ``b`` looks like a repeat of ``a``."""
    if a.key == b.key:
        return 100, "Same page of the same file"
    if a.has_text and b.has_text:
        if a.text == b.text:
            return 99, "Identical text"
        if a.squeezed == b.squeezed:
            return 96, "Same text; only whitespace or letter case differs"
        similarity = dice(a.trigrams, b.trigrams)
        if similarity < MIN_SIMILARITY:
            return None
        percent = int(similarity * 100)
        return (min(SIMILAR_CAP, round(similarity * SIMILAR_CAP)),
                "%d%% of the text matches (ignoring whitespace)" % percent)
    if a.has_text or b.has_text or a.blank or b.blank:
        return None
    if a.image_hash is None or b.image_hash is None:
        return None
    bits = HASH_WIDTH * HASH_HEIGHT
    similarity = 1.0 - bin(a.image_hash ^ b.image_hash).count("1") / bits
    if similarity < MIN_IMAGE_SIMILARITY:
        return None
    if similarity == 1.0:
        return IMAGE_CAP, "Pages look identical (no text to compare)"
    return (round(similarity * IMAGE_CAP),
            "Pages look alike (%d%%; no text to compare)" % int(similarity * 100))


def find_duplicates(prints: list[Fingerprint], should_stop=None) -> list[Match]:
    """Report each page that repeats an earlier page, against its best match."""
    matches: list[Match] = []
    by_key: dict[tuple[str, int], int] = {}
    by_text: dict[str, int] = {}
    by_sketch: dict[int, list[int]] = {}
    pictures: list[int] = []

    for position, page in enumerate(prints):
        if should_stop and should_stop():
            break
        candidates: set[int] = set()
        if page.key in by_key:
            candidates.add(by_key[page.key])
        if page.has_text:
            if page.squeezed in by_text:
                candidates.add(by_text[page.squeezed])
            shared: Counter = Counter()
            for value in page.sketch:
                shared.update(by_sketch.get(value, ()))
            candidates.update(i for i, count in shared.items() if count >= MIN_SHARED)
        elif not page.blank:
            candidates.update(pictures)

        best: tuple[int, int, str] | None = None
        for earlier in sorted(candidates):
            result = compare(prints[earlier], page)
            if result and (best is None or result[0] > best[0]):
                best = (result[0], earlier, result[1])
        if best is not None:
            matches.append(Match(position, best[1], best[0], best[2]))

        # Index the page so later pages can match it. A repeat is not
        # indexed under the same keys again, so matches point at the first copy.
        by_key.setdefault(page.key, position)
        if page.has_text:
            by_text.setdefault(page.squeezed, position)
            for value in page.sketch:
                by_sketch.setdefault(value, []).append(position)
        elif not page.blank and page.image_hash is not None:
            pictures.append(position)
    return matches


def read_pages(pages: list[tuple[Path, int]], should_stop=None,
               on_progress=None) -> list[Fingerprint]:
    """Extract a fingerprint for each (pdf, page index), in order.

    A page that cannot be read gets an empty, blank fingerprint, so it is never
    reported. The pdfium lock is held per page so the GUI preview stays
    responsive.
    """
    import pypdfium2 as pdfium

    prints: list[Fingerprint] = []
    documents: dict[str, object] = {}
    seen: dict[tuple[str, int], Fingerprint] = {}
    total = len(pages)
    try:
        for done, (pdf, index) in enumerate(pages):
            if should_stop and should_stop():
                break
            key = (str(pdf), index)
            if key in seen:
                prints.append(seen[key])
            else:
                with PDFIUM_LOCK:
                    seen[key] = _read_page(pdfium, documents, key)
                prints.append(seen[key])
            if on_progress:
                on_progress(done + 1, total, "Reading page %d of %d…" % (done + 1, total))
    finally:
        with PDFIUM_LOCK:
            for document in documents.values():
                try:
                    document.close()
                except Exception:
                    pass
    return prints


def _read_page(pdfium, documents: dict, key: tuple[str, int]) -> Fingerprint:
    pdf, index = key
    try:
        document = documents.get(pdf)
        if document is None:
            document = documents[pdf] = pdfium.PdfDocument(pdf)
        page = document[index]
        try:
            textpage = page.get_textpage()
            try:
                text = textpage.get_text_range()
            finally:
                textpage.close()
            image = None
            if len(squeeze(text)) < MIN_TEXT:
                width, height = page.get_size()
                scale = 96.0 / max(width, height, 1.0)
                image = page.render(scale=scale, grayscale=True).to_pil()
            return make_fingerprint(key, text, image)
        finally:
            page.close()
    except Exception:
        return Fingerprint(key=key, blank=True)
