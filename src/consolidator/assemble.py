"""Phase B - assemble converted PDFs into the final document.

This phase never invokes a converter. It works purely on the intermediate PDFs
produced by Phase A, which makes re-running it cheap: reordering, rotating or
re-fitting pages costs nothing but a rewrite.

An important detail makes overlays tractable: a page's ``/Rotate`` entry is baked
into its content stream before anything else happens. Otherwise a page number
drawn onto a rotated page would itself appear rotated, because the viewer applies
``/Rotate`` to the whole page including anything merged onto it.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from pypdf import PdfReader, PdfWriter, Transformation
from pypdf.generic import RectangleObject


@dataclass
class PageSpec:
    """One source page, with an optional clockwise rotation in degrees."""

    index: int
    rotation: int = 0


@dataclass
class MergeEntry:
    """One converted PDF to merge, with the label used in bookmarks and headers."""

    path: Path
    label: str = ""
    pages: list[PageSpec] | None = None  # None means every page, unrotated


@dataclass
class PageNumbering:
    mode: str = "none"                  # none | continuous | per-document
    position: str = "bottom-center"     # {top,bottom}-{left,center,right}
    template: str = "{n}"
    start: int = 1
    font: str = "Helvetica"
    size: float = 9.0
    grey: float = 0.25


@dataclass
class HeaderFooter:
    """Text for the three slots on one edge. Placeholders are substituted."""

    left: str = ""
    center: str = ""
    right: str = ""

    def any(self) -> bool:
        return bool(self.left or self.center or self.right)


@dataclass
class MergeOptions:
    page_size: tuple[float, float] | None = None
    margin: float = 36.0
    bookmarks: bool = True
    separator: str = "none"             # none | blank | title
    numbering: PageNumbering = field(default_factory=PageNumbering)
    header: HeaderFooter = field(default_factory=HeaderFooter)
    footer: HeaderFooter = field(default_factory=HeaderFooter)
    title: str = ""
    author: str = ""
    subject: str = ""


@dataclass
class MergeReport:
    entries: int = 0
    pages: int = 0
    outline_items: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    separators: int = 0


def visual_size(page) -> tuple[float, float]:
    """Page size as the reader sees it, accounting for the ``/Rotate`` entry."""
    box = page.mediabox
    width, height = float(box.width), float(box.height)
    try:
        rotate = int(page.get("/Rotate", 0) or 0) % 360
    except Exception:
        rotate = 0
    if rotate in (90, 270):
        width, height = height, width
    return width, height


def bake_rotation(page) -> None:
    """Fold ``/Rotate`` into the content stream and clear it.

    Done before any transformation so that scaling and overlays always operate in
    the page's displayed orientation.
    """
    try:
        rotate = int(page.get("/Rotate", 0) or 0) % 360
    except Exception:
        rotate = 0
    if rotate:
        page.transfer_rotation_to_content()


def rotate_page(page, degrees: int) -> None:
    """Rotate a page clockwise by 90, 180 or 270 degrees, in place."""
    degrees %= 360
    if not degrees:
        return
    page.rotate(degrees)


def fit_to_page(page, target: tuple[float, float], margin: float = 0.0) -> None:
    """Scale and centre a page onto ``target``, preserving aspect ratio.

    The transformation is written into the content stream: this scales vector
    text and embedded images without re-encoding either.
    """
    tw, th = target
    avail_w = max(tw - 2 * margin, 1.0)
    avail_h = max(th - 2 * margin, 1.0)

    src_w, src_h = visual_size(page)
    if src_w <= 0 or src_h <= 0:
        return

    scale = min(avail_w / src_w, avail_h / src_h)
    unrot_w, unrot_h = float(page.mediabox.width), float(page.mediabox.height)

    tx = (tw - unrot_w * scale) / 2.0
    ty = (th - unrot_h * scale) / 2.0

    page.add_transformation(Transformation().scale(scale, scale).translate(tx, ty))
    page.mediabox = RectangleObject((0, 0, tw, th))
    page.cropbox = RectangleObject((0, 0, tw, th))


# ------------------------------------------------------------------ overlays


def _draw_slot(canvas, text: str, slot: str, width: float, height: float,
               y: float, font: str, size: float) -> None:
    """Draw one header/footer slot at the given vertical position."""
    if not text:
        return
    canvas.setFont(font, size)
    left_margin = 40.0
    right_edge = width - 40.0

    if slot == "left":
        canvas.drawString(left_margin, y, text)
    elif slot == "right":
        canvas.drawRightString(right_edge, y, text)
    else:
        canvas.drawCentredString(width / 2.0, y, text)


def _substitute(template: str, *, number: int, total: int, document: str,
                page_in_document: int, pages_in_document: int) -> str:
    return (
        template.replace("{n}", str(number))
        .replace("{total}", str(total))
        .replace("{doc}", document)
        .replace("{n_doc}", str(page_in_document))
        .replace("{total_doc}", str(pages_in_document))
        .replace("{date}", date.today().isoformat())
    )


def _make_overlay(width: float, height: float, draw) -> object:
    """Build a single transparent page of the given size and return it.

    ``showPage()`` is called explicitly: ReportLab only emits a page when
    something has been drawn, so a deliberately blank overlay would otherwise
    produce a zero-page PDF and blow up on the next access.
    """
    from reportlab.pdfgen import canvas as rl_canvas

    buffer = io.BytesIO()
    canvas = rl_canvas.Canvas(buffer, pagesize=(width, height))
    draw(canvas)
    canvas.showPage()
    canvas.save()
    buffer.seek(0)
    return PdfReader(buffer).pages[0]


def _decorate(page, options: MergeOptions, number: int, total: int,
              document: str, page_in_doc: int, pages_in_doc: int) -> None:
    """Merge a header/footer overlay onto ``page`` if anything needs drawing."""
    numbering = options.numbering
    wants_numbers = numbering.mode != "none" and bool(numbering.template)
    if not (wants_numbers or options.header.any() or options.footer.any()):
        return

    width = float(page.mediabox.width)
    height = float(page.mediabox.height)
    slots = ("left", "center", "right")

    def resolve(template: str) -> str:
        return _substitute(
            template, number=number, total=total, document=document,
            page_in_document=page_in_doc, pages_in_document=pages_in_doc,
        )

    top = {slot: resolve(getattr(options.header, slot)) for slot in slots}
    bottom = {slot: resolve(getattr(options.footer, slot)) for slot in slots}

    if wants_numbers:
        edge, _, horizontal = numbering.position.partition("-")
        if horizontal not in slots:
            horizontal = "center"
        target = top if edge == "top" else bottom
        # An explicit header/footer slot wins over the automatic page number.
        if not target.get(horizontal):
            target[horizontal] = resolve(numbering.template)

    top_y = height - 40.0
    bottom_y = 30.0

    def draw(canvas) -> None:
        canvas.setFillGray(numbering.grey)
        for slot in slots:
            _draw_slot(canvas, top[slot], slot, width, height, top_y,
                       numbering.font, numbering.size)
        for slot in slots:
            _draw_slot(canvas, bottom[slot], slot, width, height, bottom_y,
                       numbering.font, numbering.size)

    page.merge_page(_make_overlay(width, height, draw))


def _title_page(width: float, height: float, label: str, index: int) -> object:
    """A generated cover page for one source document."""

    def draw(canvas) -> None:
        canvas.setFillGray(0.1)
        canvas.setFont("Helvetica-Bold", 20)
        canvas.drawCentredString(width / 2.0, height / 2.0 + 20, label[:80])
        canvas.setFillGray(0.45)
        canvas.setFont("Helvetica", 10)
        canvas.drawCentredString(width / 2.0, height / 2.0 - 10,
                                 "Document %d" % (index + 1))
        canvas.setStrokeGray(0.8)
        canvas.line(width * 0.25, height / 2.0, width * 0.75, height / 2.0)

    return _make_overlay(width, height, draw)


# --------------------------------------------------------------------- merge


def merge(
    entries: list[MergeEntry],
    dest: Path,
    page_size: tuple[float, float] | None = None,
    margin: float = 0.0,
    bookmarks: bool = True,
    title: str | None = None,
    options: MergeOptions | None = None,
) -> MergeReport:
    """Merge ``entries`` in order into ``dest``, written atomically.

    ``options`` carries the richer settings (numbering, headers, separators);
    the individual keyword arguments are kept for simple callers and tests.
    """
    if options is None:
        options = MergeOptions(page_size=page_size, margin=margin,
                               bookmarks=bookmarks, title=title or "")
    else:
        if page_size is not None:
            options.page_size = page_size
        if margin:
            options.margin = margin
        options.bookmarks = bookmarks
        if title:
            options.title = title

    dest = Path(dest)
    writer = PdfWriter()
    report = MergeReport()

    # A first pass is needed to know the total page count for "{total}".
    planned: list[tuple[MergeEntry, list]] = []
    total_pages = 0
    for entry in entries:
        path = Path(entry.path)
        try:
            reader = PdfReader(str(path))
            if reader.is_encrypted:
                report.skipped.append((entry.label or path.name, "PDF is encrypted"))
                continue
            specs = entry.pages
            if specs is None:
                specs = [PageSpec(i) for i in range(len(reader.pages))]
            specs = [s for s in specs if 0 <= s.index < len(reader.pages)]
            if not specs:
                report.skipped.append((entry.label or path.name, "no pages selected"))
                continue
            planned.append((entry, specs))
            total_pages += len(specs)
            if options.separator == "title" and len(planned) > 1:
                total_pages += 1
            elif options.separator == "blank" and len(planned) > 1:
                total_pages += 1
        except Exception as exc:
            report.skipped.append((entry.label or path.name,
                                   "%s: %s" % (type(exc).__name__, exc)))

    page_number = options.numbering.start
    first = True

    for entry_index, (entry, specs) in enumerate(planned):
        path = Path(entry.path)
        label = entry.label or path.stem

        # Per-document numbering restarts for each source document.
        if options.numbering.mode == "per-document":
            page_number = options.numbering.start

        # Separator between documents (not before the first).
        if not first and options.separator in ("blank", "title") and options.page_size:
            width, height = options.page_size
            if options.separator == "title":
                separator_page = _title_page(width, height, label, entry_index)
            else:
                separator_page = _make_overlay(width, height, lambda c: None)
            writer.add_page(separator_page)
            # A separator is a page in the output, so it is numbered like one.
            _decorate(writer.pages[-1], options, number=page_number, total=total_pages,
                      document=label, page_in_doc=0, pages_in_doc=0)
            report.separators += 1
            page_number += 1

        reader = PdfReader(str(path))
        start_index = len(writer.pages)

        for position, spec in enumerate(specs, start=1):
            writer.add_page(reader.pages[spec.index])
            page = writer.pages[-1]

            # Order matters: bake /Rotate first, then rotate, then scale, then
            # decorate - each step assumes the previous one has settled.
            bake_rotation(page)
            rotate_page(page, spec.rotation)
            if options.page_size:
                fit_to_page(page, options.page_size, options.margin)

            width = float(page.mediabox.width)
            height = float(page.mediabox.height)
            _decorate(
                page, options,
                number=page_number,
                total=total_pages,
                document=label,
                page_in_doc=position,
                pages_in_doc=len(specs),
            )
            page_number += 1
            first = False

        if options.bookmarks and label:
            writer.add_outline_item(label, start_index)
            report.outline_items += 1
        report.entries += 1

    if options.title or options.author or options.subject:
        writer.add_metadata({
            "/Title": options.title,
            "/Author": options.author,
            "/Subject": options.subject,
            "/Producer": "PDF Consolidator",
        })

    report.pages = len(writer.pages)

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        with open(tmp, "wb") as handle:
            writer.write(handle)
        os.replace(tmp, dest)
    except OSError as exc:
        # Never leave a half-written file where a complete one is expected.
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise OSError(
            "could not write %s: %s" % (dest, getattr(exc, "strerror", exc) or exc)
        ) from exc

    return report
