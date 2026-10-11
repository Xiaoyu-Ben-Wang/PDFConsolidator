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
import itertools
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from pypdf import PdfReader, PdfWriter


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
    # Compare the written file with its sources, page by page (see verify.py).
    verify: bool = True
    # An existing file is never replaced: the output goes to "name (1).pdf",
    # "name (2).pdf"... instead. Only a caller that means to replace sets this.
    overwrite: bool = False


@dataclass
class MergeReport:
    entries: int = 0
    pages: int = 0
    outline_items: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    separators: int = 0
    # Where each output page came from: (source pdf, page index, label), or None
    # for a generated page such as a separator.
    page_map: list = field(default_factory=list)
    # The page-by-page comparison of the output with its sources, if it ran.
    verification: object = None
    # Pages that could not be copied and were included as pictures of themselves:
    # (output page, label, source page, why).
    as_images: list = field(default_factory=list)
    # The file actually written - not the one asked for, if that already existed.
    output: Path | None = None
    requested: Path | None = None


# ------------------------------------------------- page geometry, unparsed
#
# Fitting, rotating and decorating a page all used to go through pypdf's content
# stream parser (add_transformation, transfer_rotation_to_content, merge_page),
# which reads and re-writes every drawing instruction on the page. It is strict
# where viewers are lenient - a number longer than 64 characters, as some
# generators write, raised LimitReachedError and aborted an entire 2000-page
# export - and anything it misreads is silently lost. None of it is needed: a
# page's own instructions can be left exactly as they are, byte for byte, with a
# transformation placed *around* them and decorations drawn *after* them as a
# separate form. That is what everything below does; nothing here parses a page.

Matrix = tuple[float, float, float, float, float, float]


def then(first: Matrix, second: Matrix) -> Matrix:
    """The transformation that applies ``first`` and then ``second``."""
    a1, b1, c1, d1, e1, f1 = first
    a2, b2, c2, d2, e2, f2 = second
    return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2,
            c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2)


def apply(matrix: Matrix, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    return a * x + c * y + e, b * x + d * y + f


def page_rotation(page) -> int:
    try:
        return int(page.get("/Rotate", 0) or 0) % 360
    except Exception:
        return 0


def visible_box(page) -> tuple[float, float, float, float]:
    """``(left, bottom, width, height)`` of what a viewer shows: crop box within media box.

    A box that does not start at the origin is common (scanners, imposition
    software). Fitting used to measure only width and height and assume (0, 0),
    which shifted such pages and could push part of them off the sheet.
    """
    media = page.mediabox
    try:
        crop = page.cropbox
    except Exception:
        crop = media
    left = max(float(media.left), float(crop.left))
    bottom = max(float(media.bottom), float(crop.bottom))
    right = min(float(media.right), float(crop.right))
    top = min(float(media.top), float(crop.top))
    if right - left <= 0 or top - bottom <= 0:
        left, bottom = float(media.left), float(media.bottom)
        right, top = float(media.right), float(media.top)
    return left, bottom, right - left, top - bottom


def visual_size(page) -> tuple[float, float]:
    """Page size as the reader sees it: the visible box, turned by ``/Rotate``."""
    _left, _bottom, width, height = visible_box(page)
    if page_rotation(page) in (90, 270):
        width, height = height, width
    return width, height


def rotation_matrix(box: tuple[float, float, float, float], degrees: int) -> Matrix:
    """Turn the content of ``box`` clockwise by ``degrees`` onto a box at the origin."""
    left, bottom, width, height = box
    degrees %= 360
    if degrees == 90:
        return (0.0, -1.0, 1.0, 0.0, -bottom, width + left)
    if degrees == 180:
        return (-1.0, 0.0, 0.0, -1.0, left + width, bottom + height)
    if degrees == 270:
        return (0.0, 1.0, -1.0, 0.0, bottom + height, -left)
    return (1.0, 0.0, 0.0, 1.0, -left, -bottom)


def _writer_of(page):
    return page.indirect_reference.pdf


def _stream(writer, data: bytes, extra: dict | None = None):
    from pypdf.generic import DecodedStreamObject

    stream = DecodedStreamObject()
    stream.set_data(data)
    if extra:
        stream.update(extra)
    return writer._add_object(stream)


def _contents(page) -> list:
    """The page's content streams as references - located, never read."""
    from pypdf.generic import ArrayObject, IndirectObject, NameObject

    writer = _writer_of(page)
    key = NameObject("/Contents")
    if key not in page:
        return []
    raw = dict.__getitem__(page, key)
    resolved = raw.get_object()
    items = list(resolved) if isinstance(resolved, ArrayObject) else [raw]
    references = []
    for item in items:
        if not isinstance(item, IndirectObject):
            item = writer._add_object(item)      # a direct stream: give it a home
        references.append(item)
    return references


def _number(value: float) -> bytes:
    text = ("%.6f" % value).rstrip("0").rstrip(".")
    return (text if text not in ("", "-0") else "0").encode("ascii")


def wrap_contents(page, prefix: bytes = b"", suffix: bytes = b"") -> None:
    """Put drawing instructions before and after the page's own, untouched."""
    from pypdf.generic import ArrayObject, NameObject

    writer = _writer_of(page)
    parts = []
    if prefix:
        parts.append(_stream(writer, prefix))
    parts.extend(_contents(page))
    if suffix:
        parts.append(_stream(writer, suffix))
    page[NameObject("/Contents")] = ArrayObject(parts)


def _transform_annotations(page, matrix: Matrix) -> None:
    """Move link and other annotation rectangles along with the content.

    Each annotation is copied for this page first: a page used twice shares its
    annotations with its twin, which must not move as well.
    """
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject, RectangleObject

    if "/Annots" not in page:
        return
    writer = _writer_of(page)
    try:
        annotations = list(page["/Annots"])
    except Exception:
        return
    moved = ArrayObject()
    for reference in annotations:
        try:
            copy = DictionaryObject(dict.items(reference.get_object()))
            copy.pop(NameObject("/P"), None)
            if "/Rect" in copy:
                x0, y0, x1, y1 = (float(v) for v in copy["/Rect"])
                corners = [apply(matrix, x, y)
                           for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
                xs, ys = [c[0] for c in corners], [c[1] for c in corners]
                copy[NameObject("/Rect")] = RectangleObject(
                    (min(xs), min(ys), max(xs), max(ys)))
            moved.append(writer._add_object(copy))
        except Exception:
            continue                    # an unreadable annotation is dropped, not fatal
    page[NameObject("/Annots")] = moved


def place(page, matrix: Matrix, sheet: tuple[float, float],
          clip: tuple[float, float, float, float]) -> None:
    """Draw the page's content through ``matrix`` onto a sheet of size ``sheet``.

    The content is clipped to ``clip`` - its visible box, in its own coordinates -
    so whatever a viewer hid before (outside the crop box) stays hidden.
    """
    from pypdf.generic import NameObject, NumberObject, RectangleObject

    left, bottom, width, height = clip
    prefix = (b"q " + b" ".join(_number(v) for v in matrix) + b" cm "
              + b" ".join(_number(v) for v in (left, bottom, width, height))
              + b" re W n\n")
    wrap_contents(page, prefix, b"\nQ\n")
    _transform_annotations(page, matrix)
    page[NameObject("/MediaBox")] = RectangleObject((0, 0, sheet[0], sheet[1]))
    page[NameObject("/CropBox")] = RectangleObject((0, 0, sheet[0], sheet[1]))
    for name in ("/TrimBox", "/BleedBox", "/ArtBox"):
        page.pop(NameObject(name), None)
    page[NameObject("/Rotate")] = NumberObject(0)


def normalise(page, extra_rotation: int = 0,
              target: tuple[float, float] | None = None, margin: float = 0.0) -> None:
    """Turn the page upright and, given ``target``, scale and centre it onto it.

    Upright means its own /Rotate plus ``extra_rotation`` (clockwise) folded into
    its geometry. Afterwards the page has no /Rotate, its box starts at the
    origin, and its content is exactly the original's, transformed - which is
    what overlays need.
    """
    box = visible_box(page)
    degrees = (page_rotation(page) + extra_rotation) % 360
    matrix = rotation_matrix(box, degrees)
    width, height = (box[3], box[2]) if degrees in (90, 270) else (box[2], box[3])
    sheet = (width, height)
    if target is not None and width > 0 and height > 0:
        target_width, target_height = target
        available_w = max(target_width - 2 * margin, 1.0)
        available_h = max(target_height - 2 * margin, 1.0)
        scale = min(available_w / width, available_h / height)
        matrix = then(matrix, (scale, 0.0, 0.0, scale,
                               (target_width - width * scale) / 2.0,
                               (target_height - height * scale) / 2.0))
        sheet = (target_width, target_height)
    place(page, matrix, sheet, box)


def rotate_page(page, degrees: int) -> None:
    """Rotate a page clockwise by 90, 180 or 270 degrees, by its /Rotate entry only."""
    from pypdf.generic import NameObject, NumberObject

    degrees %= 360
    if degrees:
        page[NameObject("/Rotate")] = NumberObject((page_rotation(page) + degrees) % 360)


def fit_to_page(page, target: tuple[float, float], margin: float = 0.0) -> None:
    """Scale and centre a page onto ``target``, preserving aspect ratio.

    The transformation is placed around the page's own drawing instructions,
    which are never read: vector text and images scale without re-encoding,
    and there is nothing for a parser to misread.
    """
    normalise(page, 0, target, margin)


def bake_rotation(page) -> None:
    """Fold ``/Rotate`` into the page's geometry, so overlays draw upright."""
    if page_rotation(page):
        normalise(page)


# ------------------------------------------------------------------ overlays


EDGE_MARGIN = 40.0      # distance of header/footer text from the page sides
SLOT_GAP = 12.0         # space kept between neighbouring slots
MAX_SLOT_LINES = 3      # a slot never grows past this many lines


def _slot_widths(texts: dict[str, str], width: float, font: str,
                 size: float) -> dict[str, float]:
    """How wide each slot on one edge may be before its text wraps.

    The centre slot is centred on the page, so it gets whatever the wider of
    the two sides leaves on both sides of it. A side slot takes the room it
    needs, up to a third of the line.
    """
    from reportlab.pdfbase.pdfmetrics import stringWidth

    usable = max(width - 2 * EDGE_MARGIN, 1.0)
    if not texts.get("center"):
        sides = [s for s in ("left", "right") if texts.get(s)]
        share = usable if len(sides) < 2 else (usable - SLOT_GAP) / 2
        return {"left": share, "center": usable, "right": share}
    side = max((min(stringWidth(texts[s], font, size), usable / 3)
                for s in ("left", "right") if texts.get(s)), default=0.0)
    center = usable - 2 * (side + SLOT_GAP) if side else usable
    return {"left": usable / 3, "center": max(center, usable / 3), "right": usable / 3}


def _wrap(text: str, limit: float, font: str, size: float) -> list[str]:
    """Break ``text`` into lines no wider than ``limit``.

    Lines break at spaces first. A single word too long for a line (a file name,
    usually) breaks after ``_``, ``-`` or ``.`` where it can, and anywhere if not.
    Past MAX_SLOT_LINES the last line ends in an ellipsis.
    """
    from reportlab.pdfbase.pdfmetrics import stringWidth

    def fits(candidate: str) -> bool:
        return stringWidth(candidate, font, size) <= limit

    def split_word(word: str) -> list[str]:
        pieces, current = [], ""
        for char in word:
            if not current or fits(current + char):
                current += char
                continue
            cut = max(current.rfind(c) for c in "_-.")
            if cut > 0 and cut < len(current) - 1:
                pieces.append(current[:cut + 1])
                current = current[cut + 1:] + char
            else:
                pieces.append(current)
                current = char
        return pieces + ([current] if current else [])

    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = word if not current else current + " " + word
        if fits(candidate):
            current = candidate
            continue
        if current:
            lines.append(current)
        parts = split_word(word) if not fits(word) else [word]
        lines.extend(parts[:-1])
        current = parts[-1] if parts else ""
    if current:
        lines.append(current)

    if len(lines) > MAX_SLOT_LINES:
        lines = lines[:MAX_SLOT_LINES]
        last = lines[-1]
        while last and not fits(last + "…"):
            last = last[:-1]
        lines[-1] = last + "…"
    return lines


def _draw_slot(canvas, text: str, slot: str, width: float, y: float,
               font: str, size: float, limit: float, grow: int) -> None:
    """Draw one header/footer slot, wrapped to ``limit`` points.

    ``y`` is the baseline of the line nearest the page edge; further lines go
    towards the middle of the page (``grow`` is +1 for a footer, -1 for a header).
    """
    if not text:
        return
    canvas.setFont(font, size)
    lines = _wrap(text, limit, font, size)
    leading = size * 1.2
    # Lines are drawn in reading order, so text selection and extraction
    # read them in that order too. A footer's last line sits on the baseline.
    for index, line in enumerate(lines):
        steps = len(lines) - 1 - index if grow > 0 else index
        line_y = y + grow * steps * leading
        if slot == "left":
            canvas.drawString(EDGE_MARGIN, line_y, line)
        elif slot == "right":
            canvas.drawRightString(width - EDGE_MARGIN, line_y, line)
        else:
            canvas.drawCentredString(width / 2.0, line_y, line)


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


def _wants_decoration(options: MergeOptions) -> bool:
    numbering = options.numbering
    wants_numbers = numbering.mode != "none" and bool(numbering.template)
    return wants_numbers or options.header.any() or options.footer.any()


def _decorate(page, options: MergeOptions, number: int, total: int,
              document: str, page_in_doc: int, pages_in_doc: int) -> None:
    """Draw the header/footer overlay onto ``page`` if anything needs drawing."""
    numbering = options.numbering
    wants_numbers = numbering.mode != "none" and bool(numbering.template)
    if not _wants_decoration(options):
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

    font, size = numbering.font, numbering.size
    top_widths = _slot_widths(top, width, font, size)
    bottom_widths = _slot_widths(bottom, width, font, size)

    def draw(canvas) -> None:
        canvas.setFillGray(numbering.grey)
        for slot in slots:
            _draw_slot(canvas, top[slot], slot, width, top_y, font, size,
                       top_widths[slot], grow=-1)
        for slot in slots:
            _draw_slot(canvas, bottom[slot], slot, width, bottom_y, font, size,
                       bottom_widths[slot], grow=+1)

    stamp(page, _make_overlay(width, height, draw))


_stamp_counter = itertools.count(1)


def stamp(page, overlay) -> None:
    """Draw ``overlay`` (a page of our own making) on top of ``page``.

    The overlay becomes a form XObject with its own resources, drawn after the
    page's untouched content. ``merge_page`` instead parsed the page's content to
    merge the two - the step a strict parser can fail on, losing the export -
    and renamed resources to avoid collisions; a form needs neither.
    """
    from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject

    writer = _writer_of(page)
    width = float(overlay.mediabox.width)
    height = float(overlay.mediabox.height)
    form = _stream(writer, overlay.get_contents().get_data(), {
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Form"),
        NameObject("/BBox"): ArrayObject([FloatObject(0), FloatObject(0),
                                          FloatObject(width), FloatObject(height)]),
        NameObject("/Resources"): overlay["/Resources"].clone(writer),
    })
    name = NameObject("/PCStamp%d" % next(_stamp_counter))

    # A copy of the page's resources, so a dictionary other pages share is not
    # changed under them.
    existing = page.get("/Resources")
    resources = DictionaryObject(dict.items(existing.get_object())) if existing else DictionaryObject()
    xobjects = resources.get("/XObject")
    xobjects = DictionaryObject(dict.items(xobjects.get_object())) if xobjects else DictionaryObject()
    xobjects[name] = form
    resources[NameObject("/XObject")] = xobjects
    page[NameObject("/Resources")] = resources

    wrap_contents(page, b"q\n", b"\nQ\nq " + name.encode("ascii") + b" Do Q\n")


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


# Resolution of a page that has to be included as a picture of itself.
FALLBACK_DPI = 200


def _page_as_image(path: Path, index: int):
    """A one-page PDF holding a rendering of one page, for a page that cannot be
    copied. Rendered by pdfium, the preview's engine, so it looks as the preview
    did; the text is no longer selectable, which the report says."""
    import img2pdf
    import pypdfium2 as pdfium

    from .pdfium_guard import PDFIUM_LOCK

    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(str(path))
        try:
            page = document[index]
            image = page.render(scale=FALLBACK_DPI / 72.0).to_pil().convert("RGB")
            page.close()
        finally:
            document.close()
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    data = img2pdf.convert(buffer.getvalue(),
                           layout_fun=img2pdf.get_fixed_dpi_layout_fun((FALLBACK_DPI, FALLBACK_DPI)))
    return PdfReader(io.BytesIO(data))


def open_pdf(path: Path):
    """Open a PDF for merging, unlocking one that only *looks* locked.

    Plenty of real attachments are "protected" with no user password at all: a
    scanner or a bank sets an owner password and prints permissions, and every
    reader opens the file for anyone who asks. Refusing those dropped the whole
    attachment from the output, which is how a section of a document goes missing
    without a word. Returns ``(reader, reason)``; ``reader`` is None when the file
    really does need a password.
    """
    reader = PdfReader(str(path))
    if not reader.is_encrypted:
        return reader, ""
    try:
        unlocked = reader.decrypt("")
    except Exception:
        unlocked = 0
    if not unlocked:
        return None, "PDF is encrypted and needs a password"
    return reader, ""


_NUMBERED = re.compile(r"^(?P<base>.*) \((?P<n>\d+)\)$")


def reserve_output(dest: Path) -> Path:
    """Claim a name for the output that no existing file has.

    ``dest`` itself if it is free, otherwise ``name (1).pdf``, ``name (2).pdf``
    and so on - the way Explorer names copies. A name that already ends in
    ``(N)`` continues from N rather than growing ``(1) (1)``. The name is claimed
    by creating it exclusively, so two exports at the same moment cannot both
    pick it; the caller replaces the empty placeholder with the real file.
    """
    dest = Path(dest)
    stem, suffix = dest.stem, dest.suffix
    base, number = stem, 0
    match = _NUMBERED.match(stem)
    if match:
        base, number = match.group("base"), int(match.group("n"))
    candidate = dest
    while True:
        try:
            with open(candidate, "xb"):
                return candidate
        except FileExistsError:
            number += 1
            candidate = dest.with_name("%s (%d)%s" % (base, number, suffix))


def merge(
    entries: list[MergeEntry],
    dest: Path,
    page_size: tuple[float, float] | None = None,
    margin: float = 0.0,
    bookmarks: bool | None = None,
    title: str | None = None,
    options: MergeOptions | None = None,
) -> MergeReport:
    """Merge ``entries`` in order into ``dest``, written atomically.

    ``options`` carries the richer settings (numbering, headers, separators);
    the individual keyword arguments are kept for simple callers and tests. Each
    of those arguments overrides ``options`` **only when given**: ``bookmarks``
    used to default to ``True`` and be assigned unconditionally, so a caller who
    passed ``options`` with bookmarks switched off got them anyway - which is why
    ``--no-bookmarks`` produced a document full of bookmarks.
    """
    if options is None:
        options = MergeOptions(page_size=page_size, margin=margin,
                               bookmarks=True if bookmarks is None else bookmarks,
                               title=title or "")
    else:
        if page_size is not None:
            options.page_size = page_size
        if margin:
            options.margin = margin
        if bookmarks is not None:
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
            reader, reason = open_pdf(path)
            if reader is None:
                report.skipped.append((entry.label or path.name, reason))
                continue
            specs = entry.pages
            if specs is None:
                specs = [PageSpec(i) for i in range(len(reader.pages))]
            requested = len(specs)
            specs = [s for s in specs if 0 <= s.index < len(reader.pages)]
            if not specs:
                report.skipped.append((entry.label or path.name, "no pages selected"))
                continue
            if len(specs) != requested:
                # Pages were asked for that the file does not have. Saying so is
                # the difference between "the document is shorter than it looks"
                # and a mystery.
                report.skipped.append(
                    (entry.label or path.name,
                     "%d page(s) requested beyond the end of the file"
                     % (requested - len(specs)))
                )
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
    decorating = _wants_decoration(options)
    # Readers of pages included as images; their pages are written at the end.
    keep_alive: list = []

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
            report.page_map.append(None)
            # A separator is a page in the output, so it is numbered like one.
            _decorate(writer.pages[-1], options, number=page_number, total=total_pages,
                      document=label, page_in_doc=0, pages_in_doc=0)
            report.separators += 1
            page_number += 1

        reader, reason = open_pdf(path)
        if reader is None:
            # It opened in the planning pass, so this is unexpected - report it
            # rather than writing a document that is quietly short.
            report.skipped.append((label, reason or "could not be opened"))
            continue
        start_index = len(writer.pages)

        for position, spec in enumerate(specs, start=1):
            def finish(page, extra_rotation: int) -> None:
                # The rotation is folded into the geometry whenever anything
                # else is done to the page: left as /Rotate, a fit was computed
                # in the unrotated frame (a landscape page turned to portrait
                # lost its sides) and page numbers came out sideways. A page
                # nothing is done to keeps its content as is, rotated by /Rotate.
                if options.page_size:
                    normalise(page, extra_rotation, options.page_size, options.margin)
                elif decorating:
                    normalise(page, extra_rotation)
                else:
                    rotate_page(page, extra_rotation)
                _decorate(page, options, number=page_number, total=total_pages,
                          document=label, page_in_doc=position,
                          pages_in_doc=len(specs))

            before = len(writer.pages)
            try:
                writer.add_page(reader.pages[spec.index])
                finish(writer.pages[-1], spec.rotation)
                report.page_map.append((path, spec.index, label))
            except Exception as exc:
                # One page that cannot be copied must not cost the whole export.
                # It is included as a picture of itself instead, and named.
                while len(writer.pages) > before:
                    writer.remove_page(len(writer.pages) - 1)
                reason = "%s: %s" % (type(exc).__name__, exc)
                try:
                    picture = _page_as_image(path, spec.index)
                    keep_alive.append(picture)
                    writer.add_page(picture.pages[0])
                    finish(writer.pages[-1], spec.rotation)
                    report.page_map.append(None)
                    report.as_images.append((len(writer.pages), label, spec.index + 1, reason))
                except Exception as fallback:
                    while len(writer.pages) > before:
                        writer.remove_page(len(writer.pages) - 1)
                    report.skipped.append((
                        label, "page %d could not be included (%s; as an image: %s: %s)"
                        % (spec.index + 1, reason, type(fallback).__name__, fallback)))
                    continue
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
    report.requested = dest
    target = dest if options.overwrite else reserve_output(dest)
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        with open(tmp, "wb") as handle:
            writer.write(handle)
        os.replace(tmp, target)
    except OSError as exc:
        # Never leave a half-written file where a complete one is expected -
        # nor the empty placeholder that reserved a numbered name.
        leftovers = [tmp]
        if not options.overwrite:
            leftovers.append(target)        # the empty placeholder we created
        for leftover in leftovers:
            try:
                if leftover.exists() and (leftover == tmp or leftover.stat().st_size == 0):
                    leftover.unlink()
            except OSError:
                pass
        raise OSError(
            "could not write %s: %s" % (target, getattr(exc, "strerror", exc) or exc)
        ) from exc
    dest = target
    report.output = target

    if options.verify:
        from . import verify

        report.verification = verify.check(dest, report.page_map)

    return report
