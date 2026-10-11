"""Per-format converters that turn a source file into a PDF.

Each converter takes a source path and a destination path and either writes a
PDF or raises :class:`ConversionError` with a message for the user.

Office documents and web pages are converted by LibreOffice (see
:mod:`consolidator.libreoffice`), which renders HTML the same way as an email
body. Images go through ``img2pdf`` so JPEGs are embedded without re-encoding.
Plain text, including Markdown (not interpreted), is laid out with ReportLab.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from . import libreoffice, pages

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
TEXT_EXTS = {".txt", ".md", ".markdown", ".log", ".csv"}
PDF_EXTS = {".pdf"}
# Word, Excel and PowerPoint, including macro-enabled and template variants,
# which LibreOffice reads with the same filters, plus OpenDocument and RTF.
OFFICE_EXTS = {
    ".docx", ".doc", ".docm", ".dot", ".dotx", ".dotm",
    ".xlsx", ".xls", ".xlsm", ".xltx", ".xltm",
    ".pptx", ".ppt", ".pptm", ".potx", ".potm", ".pps", ".ppsx",
    ".rtf", ".odt", ".ods", ".odp", ".odg", ".odf",
}
# Web pages are rendered by LibreOffice, like email bodies, not shown as raw markup.
WEB_EXTS = {".html", ".htm", ".xhtml"}
MSG_EXTS = {".msg"}
EML_EXTS = {".eml"}
EMAIL_EXTS = MSG_EXTS | EML_EXTS

# Formats that can be batched into one LibreOffice invocation.
LIBREOFFICE_EXTS = OFFICE_EXTS | WEB_EXTS
# Formats laid out as documents, by LibreOffice or the text renderer.
DOCUMENT_EXTS = LIBREOFFICE_EXTS | TEXT_EXTS
# All accepted source formats.
SUPPORTED_EXTS = (PDF_EXTS | IMAGE_EXTS | DOCUMENT_EXTS | EMAIL_EXTS)

# Known unsupported formats, each with a specific error message.
KNOWN_UNSUPPORTED = {
    ".pub": "Microsoft Publisher is not supported",
    ".heic": "HEIC images need an extra codec that is not bundled",
    ".heif": "HEIF images need an extra codec that is not bundled",
    ".xlsb": "Excel binary workbooks (.xlsb) are not supported; save as .xlsx or .xlsm",
}


# Pause before retrying a conversion that produced nothing.
RETRY_PAUSE_SECONDS = 2.0


class ConversionError(RuntimeError):
    """A single source file could not be converted."""


def _is_office(ext: str) -> bool:
    return ext in OFFICE_EXTS


def convert_with_libreoffice(src: Path, dest: Path, workdir: Path) -> None:
    """Convert an Office document or web page with LibreOffice, retrying once on failure."""
    if Path(src).suffix.lower() in WEB_EXTS:
        staged = Path(workdir) / ("%s_page%s" % (Path(dest).stem, Path(src).suffix))
        try:
            stage_web_page(src, staged)
            src = staged
        except OSError:
            pass    # convert the original page, without its local images
    try:
        libreoffice.convert_one(src, dest, workdir=workdir)
        return
    except libreoffice.LibreOfficeError as exc:
        first = exc
    if libreoffice.stopping():
        raise ConversionError(str(first)) from first
    # Retry once with no other soffice running. Single conversions (e.g. Office
    # attachments in emails) can run concurrently, and LibreOffice can fail when
    # its processes overlap.
    time.sleep(RETRY_PAUSE_SECONDS)
    try:
        with libreoffice.alone():
            libreoffice.convert_one(src, dest, workdir=Path(workdir) / "retry")
    except libreoffice.LibreOfficeError as exc:
        raise ConversionError(str(first)) from exc


# Larger images stay as links, because the whole page is held in memory.
MAX_INLINE_IMAGE_BYTES = 32 * 1024 * 1024

_IMG_SRC = re.compile(
    rb"""(<img\b[^>]*?\bsrc\s*=\s*)(["']?)([^"'\s>]+)\2""", re.IGNORECASE)


def inline_local_images(html: bytes, base_dir: Path) -> bytes:
    """Embed locally linked images in the HTML as ``data:`` URIs.

    Headless LibreOffice does not load linked images (relative paths or
    ``file:`` URLs), but does render ``data:`` URIs. Remote images are left
    unchanged.
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
            elif len(parsed.scheme) == 1:          # drive letter (C:\...) parsed as a scheme
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
    """Copy a web page to ``staged`` with its local images embedded."""
    staged.write_bytes(inline_local_images(Path(src).read_bytes(), Path(src).parent))


def convert_pdf(src: Path, dest: Path, workdir: Path) -> None:
    """Copy an existing PDF unchanged.

    The pipeline opens every PDF afterwards, so an unreadable file is reported
    as a failure instead of contributing no pages.
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
            # Apply EXIF rotation and flatten transparency onto white. The PDF
            # image has no alpha channel, and the default flattens onto black.
            with Image.open(src) as img:
                img = ImageOps.exif_transpose(img)
                if img.mode in ("RGBA", "LA", "P"):
                    img = img.convert("RGBA")
                    canvas = Image.new("RGB", img.size, (255, 255, 255))
                    canvas.paste(img, mask=img.split()[-1])
                    img = canvas
                elif img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")

                # Close the descriptor from mkstemp; on Windows an open handle
                # prevents deleting the temporary file afterwards.
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
    """Render a text file as monospaced text, preserving whitespace.

    Markdown is not interpreted: ``# Title`` stays ``# Title``. See
    :mod:`consolidator.text_render` for encoding detection, font fallback and
    line wrapping.
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
        raise ConversionError(unsupported_reason(ext))

    if not dest.exists() or dest.stat().st_size == 0:
        raise ConversionError("conversion produced no output")


# --------------------------------------------------------------- sniffing

_MIME_EXTS = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg", "image/pjpeg": ".jpg", "image/png": ".png",
    "image/gif": ".gif", "image/tiff": ".tif", "image/bmp": ".bmp",
    "image/webp": ".webp",
    "text/plain": ".txt", "text/html": ".html", "text/csv": ".csv",
    "application/rtf": ".rtf", "text/rtf": ".rtf",
    "message/rfc822": ".eml",
    "application/vnd.ms-outlook": ".msg",
    "application/msword": ".doc",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/vnd.oasis.opendocument.presentation": ".odp",
}


def _sniff_ole(data: bytes) -> str:
    """Return the extension of an OLE2 file (.msg, .doc, .xls, .ppt), or ""."""
    import io

    try:
        import olefile

        with olefile.OleFileIO(io.BytesIO(data)) as ole:
            names = {"/".join(entry).lower() for entry in ole.listdir()}
    except Exception:
        return ""
    if any(name.startswith("__substg1.0_") for name in names):
        return ".msg"
    if "worddocument" in names:
        return ".doc"
    if "workbook" in names or "book" in names:
        return ".xls"
    if "powerpoint document" in names:
        return ".ppt"
    return ""


def _sniff_zip(data: bytes) -> str:
    """Return the extension of a ZIP-based document (OOXML or OpenDocument), or ""."""
    import io
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
            if "mimetype" in names:
                kind = archive.read("mimetype").decode("ascii", "replace").strip()
                return _MIME_EXTS.get(kind, "")
    except Exception:
        return ""
    if any(name.startswith("word/") for name in names):
        return ".docx"
    if any(name.startswith("xl/") for name in names):
        return ".xlsx"
    if any(name.startswith("ppt/") for name in names):
        return ".pptx"
    return ""


def sniff_extension(data: bytes) -> str:
    """Return the extension that matches the file's content, or "" if unknown.

    Email attachments are often named ``image001``, ``scan``, ``ATT00001`` or
    ``file.dat`` and typed ``application/octet-stream``, so the name alone does
    not identify a PDF or image.
    """
    head = data[:2048]
    if b"%PDF-" in head[:1024]:
        return ".pdf"
    signatures = (
        (b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"),
        (b"GIF87a", ".gif"), (b"GIF89a", ".gif"),
        (b"II*\x00", ".tif"), (b"MM\x00*", ".tif"), (b"BM", ".bmp"),
        (b"{\\rtf", ".rtf"),
    )
    for signature, extension in signatures:
        if head.startswith(signature):
            if extension == ".bmp":
                # Text can also start with "BM"; a real bitmap has one of the
                # defined info-header sizes at offset 14.
                if len(head) < 26 or int.from_bytes(head[14:18], "little") not in (
                        12, 40, 52, 56, 64, 108, 124):
                    continue
            return extension
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return _sniff_ole(data)
    if head.startswith(b"PK\x03\x04"):
        return _sniff_zip(data)

    # Text: decide between HTML, an email message and plain text.
    if b"\x00" in head and not head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return ""
    from .text_render import decode_text

    text = decode_text(head).lstrip("﻿").lstrip()
    lowered = text[:512].lower()
    if lowered.startswith(("<!doctype html", "<html")) or "<html" in lowered[:256]:
        return ".html"
    lines = text.splitlines()
    if (len(lines) > 1 and re.match(r"^[A-Za-z][A-Za-z0-9-]*:", lines[0])
            and re.search(r"^(from|subject|date|message-id):", text[:4096],
                          re.IGNORECASE | re.MULTILINE)):
        return ".eml"
    printable = sum(1 for c in text if c.isprintable() or c in "\r\n\t")
    if text and printable / len(text) > 0.95:
        return ".txt"
    return ""


def typed_name(name: str, data: bytes, mimetype: str = "") -> str:
    """Return ``name`` with an extension matching the file type, if it lacks one.

    A supported extension is kept as is. Otherwise the extension is taken from
    the content, then the MIME type, and appended to the original name
    (``scan`` becomes ``scan.pdf``).
    """
    name = name or "attachment"
    if Path(name).suffix.lower() in SUPPORTED_EXTS:
        return name
    extension = sniff_extension(data) or _MIME_EXTS.get((mimetype or "").lower(), "")
    if not extension:
        return name
    return name + extension


def unsupported_reason(ext: str) -> str:
    """Return a user-facing message explaining why the file type is unsupported."""
    if not ext:
        return ("unsupported file type: the file has no extension, and its "
                "content is not a type that can be converted")
    return "unsupported file type (%s)" % ext


def is_supported(path: Path) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTS
