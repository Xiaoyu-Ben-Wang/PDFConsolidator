"""Per-format converters: everything that turns a source file into a PDF.

Each converter takes a source path and a destination path and either produces a
PDF or raises :class:`ConversionError` with a message worth showing a user.

Office documents are delegated to LibreOffice (see :mod:`consolidator.libreoffice`).
Images go through ``img2pdf`` so JPEGs are copied losslessly rather than
re-encoded. Plain text - including Markdown, which is deliberately *not*
interpreted - is escaped and laid out with ReportLab.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from . import libreoffice, pages

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
TEXT_EXTS = {".txt", ".md", ".markdown", ".log", ".csv"}
PDF_EXTS = {".pdf"}
OFFICE_EXTS = {".docx", ".doc", ".rtf", ".odt", ".xlsx", ".xls", ".ods", ".pptx", ".ppt", ".odp"}
MSG_EXTS = {".msg"}
EML_EXTS = {".eml"}
EMAIL_EXTS = MSG_EXTS | EML_EXTS

# Formats we knowingly do not handle, so the error is a clear message rather
# than a confusing "unsupported extension".
KNOWN_UNSUPPORTED = {
    ".pub": "Microsoft Publisher is not supported",
    ".heic": "HEIC images need an extra codec that is not bundled",
    ".heif": "HEIF images need an extra codec that is not bundled",
}


class ConversionError(RuntimeError):
    """A single source file could not be converted."""


def _is_office(ext: str) -> bool:
    return ext in OFFICE_EXTS


def convert_office(src: Path, dest: Path, workdir: Path) -> None:
    try:
        libreoffice.convert_one(src, dest, workdir=workdir)
    except libreoffice.LibreOfficeError as exc:
        raise ConversionError(str(exc)) from exc


def convert_pdf(src: Path, dest: Path, workdir: Path) -> None:
    """Existing PDFs are copied through untouched - never re-encoded."""
    try:
        shutil.copyfile(src, dest)
    except OSError as exc:
        raise ConversionError("could not copy PDF: %s" % exc) from exc


# --------------------------------------------------------------------- images


def _needs_reencode(img) -> bool:
    from PIL import Image

    if img.mode in ("RGBA", "LA", "P"):
        return True
    try:
        if img.getexif().get(274):  # 274 = Orientation
            return True
    except Exception:
        pass
    return False


def convert_image(src: Path, dest: Path, workdir: Path, page_size: tuple | None) -> None:
    import img2pdf
    from PIL import Image, ImageOps

    prepared = src
    temp_path = None

    try:
        with Image.open(src) as img:
            reencode = _needs_reencode(img)

        if reencode:
            # Bake EXIF rotation and flatten transparency onto white. PDF has no
            # alpha channel, and flattening onto black (the default) looks broken.
            with Image.open(src) as img:
                img = ImageOps.exif_transpose(img)
                if img.mode in ("RGBA", "LA", "P"):
                    img = img.convert("RGBA")
                    canvas = Image.new("RGB", img.size, (255, 255, 255))
                    canvas.paste(img, mask=img.split()[-1])
                    img = canvas
                elif img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                temp_path = Path(tempfile.mkstemp(suffix=".png", dir=str(workdir))[1])
                img.save(temp_path, "PNG")
            prepared = temp_path

        kwargs = {"rotation": img2pdf.Rotation.ifvalid}
        if page_size:
            kwargs["layout_fun"] = img2pdf.get_layout_fun(page_size)

        with open(dest, "wb") as fh:
            fh.write(img2pdf.convert(str(prepared), **kwargs))

    except Exception as exc:
        raise ConversionError("image conversion failed: %s" % exc) from exc
    finally:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


# ----------------------------------------------------------------------- text


def convert_text(src: Path, dest: Path, workdir: Path, page_size: tuple | None) -> None:
    """Render a text file as literal, monospaced, whitespace-preserving text.

    Markdown is intentionally *not* interpreted: ``# Title`` stays ``# Title``.
    """
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfgen import canvas as rl_canvas

    width, height = page_size or pages.LETTER
    margin = 54.0          # 0.75 inch
    font, size, leading = "Courier", 9.5, 12.0

    try:
        text = src.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ConversionError("could not read text file: %s" % exc) from exc

    try:
        c = rl_canvas.Canvas(str(dest), pagesize=(width, height))
        c.setTitle(src.stem)
        usable = width - 2 * margin
        y = height - margin

        for raw_line in text.splitlines() or [""]:
            # Expand tabs so column alignment survives, then wrap to the page.
            line = raw_line.expandtabs(4)
            for chunk in (simpleSplit(line, font, size, usable) or [""]):
                if y < margin:
                    c.showPage()
                    y = height - margin
                c.setFont(font, size)
                c.drawString(margin, y, chunk)
                y -= leading

        c.save()
    except Exception as exc:
        raise ConversionError("text layout failed: %s" % exc) from exc


# --------------------------------------------------------------------- router


def convert(src: Path, dest: Path, workdir: Path, page_size: tuple | None = None) -> None:
    """Convert ``src`` to a PDF at ``dest``, choosing the engine by extension."""
    src, dest = Path(src), Path(dest)
    ext = src.suffix.lower()

    if not src.exists():
        raise ConversionError("file not found")
    if src.stat().st_size == 0:
        raise ConversionError("file is empty (0 bytes)")

    dest.parent.mkdir(parents=True, exist_ok=True)

    if ext in PDF_EXTS:
        convert_pdf(src, dest, workdir)
    elif ext in IMAGE_EXTS:
        convert_image(src, dest, workdir, page_size)
    elif ext in TEXT_EXTS:
        convert_text(src, dest, workdir, page_size)
    elif ext in OFFICE_EXTS:
        convert_office(src, dest, workdir)
    elif ext in EMAIL_EXTS:
        raise ConversionError(
            "email items expand into several segments; use "
            "email_items.expand_to_segments instead"
        )
    elif ext in KNOWN_UNSUPPORTED:
        raise ConversionError(KNOWN_UNSUPPORTED[ext])
    else:
        raise ConversionError("unsupported file type %r" % (ext or "(none)"))

    if not dest.exists() or dest.stat().st_size == 0:
        raise ConversionError("conversion produced no output")


def is_supported(path: Path) -> bool:
    return Path(path).suffix.lower() in (
        PDF_EXTS | IMAGE_EXTS | TEXT_EXTS | OFFICE_EXTS | EMAIL_EXTS
    )
