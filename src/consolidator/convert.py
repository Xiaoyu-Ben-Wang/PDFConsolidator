"""Per-format converters: everything that turns a source file into a PDF.

Each converter takes a source path and a destination path and either produces a
PDF or raises :class:`ConversionError` with a message worth showing a user.

Office documents and web pages are delegated to LibreOffice (see
:mod:`consolidator.libreoffice`), which lays out an HTML page the same way it
renders an email body.
Images go through ``img2pdf`` so JPEGs are copied losslessly rather than
re-encoded. Plain text - including Markdown, which is deliberately *not*
interpreted - is escaped and laid out with ReportLab.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

from . import libreoffice, pages

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
TEXT_EXTS = {".txt", ".md", ".markdown", ".log", ".csv"}
PDF_EXTS = {".pdf"}
# Word, Excel and PowerPoint, plus the macro-enabled and template variants of the
# same containers: LibreOffice reads them all through the same filters, and
# refusing an .xlsm because it was not on a list was simply a gap.
OFFICE_EXTS = {
    ".docx", ".doc", ".docm", ".dot", ".dotx", ".dotm",
    ".xlsx", ".xls", ".xlsm", ".xltx", ".xltm",
    ".pptx", ".ppt", ".pptm", ".potx", ".potm", ".pps", ".ppsx",
    ".rtf", ".odt", ".ods", ".odp", ".odg", ".odf",
}
# Web pages are rendered by LibreOffice, the same way an email body is - not read
# as literal text, which would put markup on the page.
WEB_EXTS = {".html", ".htm", ".xhtml"}
MSG_EXTS = {".msg"}
EML_EXTS = {".eml"}
EMAIL_EXTS = MSG_EXTS | EML_EXTS

# What one LibreOffice invocation can be handed: the same engine, batched.
LIBREOFFICE_EXTS = OFFICE_EXTS | WEB_EXTS
# Everything that becomes pages as a document: laid out by LibreOffice or, for
# plain text, by the text renderer.
DOCUMENT_EXTS = LIBREOFFICE_EXTS | TEXT_EXTS
# Everything the application accepts as a source file.
SUPPORTED_EXTS = (PDF_EXTS | IMAGE_EXTS | DOCUMENT_EXTS | EMAIL_EXTS)

# Formats we knowingly do not handle, so the error is a clear message rather
# than a confusing "unsupported extension".
KNOWN_UNSUPPORTED = {
    ".pub": "Microsoft Publisher is not supported",
    ".heic": "HEIC images need an extra codec that is not bundled",
    ".heif": "HEIF images need an extra codec that is not bundled",
    ".xlsb": "Excel binary workbooks (.xlsb) are not supported; save as .xlsx or .xlsm",
}


class ConversionError(RuntimeError):
    """A single source file could not be converted."""


def _is_office(ext: str) -> bool:
    return ext in OFFICE_EXTS


def convert_with_libreoffice(src: Path, dest: Path, workdir: Path) -> None:
    """Lay a document out with LibreOffice: Office formats and web pages alike."""
    if Path(src).suffix.lower() in WEB_EXTS:
        staged = Path(workdir) / ("%s_page%s" % (Path(dest).stem, Path(src).suffix))
        try:
            stage_web_page(src, staged)
            src = staged
        except OSError:
            pass    # convert the page as it is, without its pictures
    try:
        libreoffice.convert_one(src, dest, workdir=workdir)
    except libreoffice.LibreOfficeError as exc:
        raise ConversionError(str(exc)) from exc


# Beyond this a picture is left as a link: the page is held in memory whole.
MAX_INLINE_IMAGE_BYTES = 32 * 1024 * 1024

_IMG_SRC = re.compile(
    rb"""(<img\b[^>]*?\bsrc\s*=\s*)(["']?)([^"'\s>]+)\2""", re.IGNORECASE)


def inline_local_images(html: bytes, base_dir: Path) -> bytes:
    """Embed the pictures a saved web page links to, as ``data:`` URIs.

    LibreOffice, run headless, does not load linked pictures at all - not by a
    relative path, not by a ``file:`` URL - so every image of a page saved with
    "Save page as..." (``page_files/...``) was missing from its PDF. A ``data:``
    URI is the one form it renders. Remote pictures are left alone.
    """
    import base64
    import mimetypes
    from urllib.parse import unquote, urlparse

    def swap(match: "re.Match[bytes]") -> bytes:
        reference = match.group(3).decode("latin-1")
        parsed = urlparse(reference)
        if parsed.scheme in ("", "file") or len(parsed.scheme) == 1:
            if parsed.scheme == "file":
                target = Path(unquote(parsed.path.lstrip("/")))
            elif len(parsed.scheme) == 1:          # C:\... read as a scheme
                target = Path(reference)
            else:
                target = base_dir / unquote(parsed.path)
            try:
                if target.is_file() and target.stat().st_size <= MAX_INLINE_IMAGE_BYTES:
                    mimetype = mimetypes.guess_type(target.name)[0] or "image/png"
                    data = base64.b64encode(target.read_bytes())
                    return (match.group(1) + b'"data:' + mimetype.encode("ascii")
                            + b";base64," + data + b'"')
            except OSError:
                pass
        return match.group(0)

    return _IMG_SRC.sub(swap, html)


def stage_web_page(src: Path, staged: Path) -> None:
    """Copy a web page for conversion, bringing its local pictures with it."""
    staged.write_bytes(inline_local_images(Path(src).read_bytes(), Path(src).parent))


def convert_pdf(src: Path, dest: Path, workdir: Path) -> None:
    """Existing PDFs are copied through untouched - never re-encoded.

    The copy is verified by the pipeline, which opens every PDF it is handed: a
    file that cannot be read is reported as a failure rather than becoming a
    segment that silently contributes no pages.
    """
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

                # mkstemp returns an open descriptor as well as a path. Leaving
                # it open leaks a handle per image, and on Windows an open handle
                # prevents the file being deleted afterwards - so the scratch
                # copy would survive every conversion.
                handle, name = tempfile.mkstemp(suffix=".png", dir=str(workdir))
                os.close(handle)
                temp_path = Path(name)
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
    The encoding is detected, every script is drawn and long lines are wrapped
    rather than cut off; see :mod:`consolidator.text_render`.
    """
    from . import text_render

    try:
        text = text_render.decode_text(src.read_bytes())
    except OSError as exc:
        raise ConversionError("could not read text file: %s" % exc) from exc

    try:
        text_render.render(text, dest, page_size or pages.LETTER, title=src.stem)
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
    elif ext in LIBREOFFICE_EXTS:
        convert_with_libreoffice(src, dest, workdir)
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
    return Path(path).suffix.lower() in SUPPORTED_EXTS
