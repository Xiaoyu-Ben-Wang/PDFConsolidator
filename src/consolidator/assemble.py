"""Phase B - assemble converted PDFs into the final document.

This phase never invokes a converter or Office application. It works purely on
the intermediate PDFs produced by Phase A, which means re-running it is cheap and
safe: reordering or re-fitting pages costs nothing but a rewrite.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader, PdfWriter, Transformation
from pypdf.generic import RectangleObject


@dataclass
class MergeEntry:
    """One converted PDF to be merged, with the label shown in bookmarks."""

    path: Path
    label: str


@dataclass
class MergeReport:
    entries: int = 0
    pages: int = 0
    outline_items: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)


def visual_size(page) -> tuple[float, float]:
    """Page size as the reader sees it, accounting for the /Rotate entry."""
    box = page.mediabox
    width, height = float(box.width), float(box.height)
    try:
        rotate = int(page.get("/Rotate", 0) or 0) % 360
    except Exception:
        rotate = 0
    if rotate in (90, 270):
        width, height = height, width
    return width, height


def fit_to_page(page, target: tuple[float, float], margin: float = 0.0) -> None:
    """Scale and centre a page onto ``target``, preserving aspect ratio.

    The transformation is written straight into the content stream: this scales
    vector text and embedded images without re-encoding either.
    """
    tw, th = target
    avail_w = max(tw - 2 * margin, 1.0)
    avail_h = max(th - 2 * margin, 1.0)

    src_w, src_h = visual_size(page)
    if src_w <= 0 or src_h <= 0:
        return

    scale = min(avail_w / src_w, avail_h / src_h)

    # Work in unrotated space: pypdf applies the CTM before /Rotate is honoured,
    # so a rotated page must be scaled as if unrotated or it lands sideways.
    try:
        rotate = int(page.get("/Rotate", 0) or 0) % 360
    except Exception:
        rotate = 0
    unrot_w, unrot_h = float(page.mediabox.width), float(page.mediabox.height)

    tx = (tw - unrot_w * scale) / 2.0
    ty = (th - unrot_h * scale) / 2.0

    transform = Transformation().scale(scale, scale).translate(tx, ty)
    page.add_transformation(transform)

    page.mediabox = RectangleObject((0, 0, tw, th))
    page.cropbox = RectangleObject((0, 0, tw, th))
    if rotate:
        page[ "/Rotate"] = rotate


def merge(
    entries: list[MergeEntry],
    dest: Path,
    page_size: tuple[float, float] | None = None,
    margin: float = 0.0,
    bookmarks: bool = True,
    title: str | None = None,
) -> MergeReport:
    """Merge ``entries`` in order into ``dest``, written atomically."""
    dest = Path(dest)
    writer = PdfWriter()
    report = MergeReport()

    for entry in entries:
        path = Path(entry.path)
        try:
            reader = PdfReader(str(path))
            if reader.is_encrypted:
                report.skipped.append((entry.label, "PDF is encrypted"))
                continue
            start = len(writer.pages)
            for page in reader.pages:
                writer.add_page(page)
                if page_size:
                    # Transform *after* attaching to the writer. pypdf warns that
                    # replace_contents() on an unattached page is unreliable and
                    # is deprecated for removal.
                    fit_to_page(writer.pages[-1], page_size, margin)
            if bookmarks and entry.label:
                writer.add_outline_item(entry.label, start)
                report.outline_items += 1
            report.entries += 1
        except Exception as exc:
            report.skipped.append((entry.label, "%s: %s" % (type(exc).__name__, exc)))

    if title:
        writer.add_metadata({"/Title": title, "/Producer": "PDF Consolidator"})

    report.pages = len(writer.pages)

    # Atomic write: never leave a half-written PDF where the user expects one.
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    with open(tmp, "wb") as handle:
        writer.write(handle)
    os.replace(tmp, dest)

    return report
