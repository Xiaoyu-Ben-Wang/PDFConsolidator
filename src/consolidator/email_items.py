"""Outlook items (``.msg``) and email (``.eml``) as inputs.

Two responsibilities:

* **Parse** a message into an :class:`EmailItem` - headers, body and attachments.
  ``.msg`` goes through ``extract-msg``, which reads the OLE2 compound file
  directly, so Outlook is never involved. ``.eml`` uses the standard library.
* **Expand** an item into an ordered list of PDF *segments*: the message body
  first, then one segment per attachment that the policy says to extract.

Attachments are the point of the feature: these are frequently emails carrying
PDFs, and those PDFs belong in the assembled document. A PDF attachment is
embedded **byte-for-byte** rather than re-rendered, so its page count and quality
survive exactly.

Inline images referenced by ``cid:`` from the body are treated as part of the
body and never become separate pages - otherwise every signature logo would.
"""

from __future__ import annotations

import html as html_module
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import convert

MSG_EXTS = convert.MSG_EXTS
EML_EXTS = convert.EML_EXTS
EMAIL_EXTS = convert.EMAIL_EXTS

IMAGE_EXTS = convert.IMAGE_EXTS
OFFICE_EXTS = convert.OFFICE_EXTS

# Attachment categories the user can choose to extract.
CATEGORIES = ("body", "pdf", "images", "office", "embedded", "other")


@dataclass
class Attachment:
    filename: str
    data: bytes
    mimetype: str = ""
    content_id: str = ""
    inline: bool = False
    category: str = "other"

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass
class EmailItem:
    subject: str = ""
    sender: str = ""
    to: str = ""
    cc: str = ""
    date: str = ""
    body_html: str = ""
    body_text: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    source: Path | None = None
    body_is_rtf_only: bool = False


@dataclass
class ExtractionPolicy:
    """Which attachment categories are pulled out, and the safety limits."""

    body: bool = True
    pdf: bool = True
    images: bool = True
    office: bool = True
    embedded: bool = False
    other: bool = False

    max_attachment_bytes: int = 64 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024
    max_depth: int = 3

    def wants(self, category: str) -> bool:
        return bool(getattr(self, category, False))


# --------------------------------------------------------------- classifying


def categorise(filename: str, mimetype: str = "") -> str:
    ext = Path(filename or "").suffix.lower()
    if ext == ".pdf" or mimetype == "application/pdf":
        return "pdf"
    if ext in IMAGE_EXTS or mimetype.startswith("image/"):
        return "images"
    if ext in OFFICE_EXTS:
        return "office"
    if ext in EMAIL_EXTS:
        return "embedded"
    return "other"


def _cid_plain(value: str) -> str:
    return (value or "").strip().strip("<>").lower()


_CID_REF = re.compile(r"""cid:([^"'\s>)]+)""", re.IGNORECASE)


def referenced_cids(html: str) -> set[str]:
    return {_cid_plain(m) for m in _CID_REF.findall(html or "")}


# ------------------------------------------------------------------- parsing


def _safe(fn, default=""):
    """Read a property that a malformed message may fail to provide.

    Real ``.msg`` files contain junk: undecodable headers, missing streams,
    truncated properties. ``extract-msg`` raises on some of them, and one bad
    field must not cost the user the whole message.
    """
    try:
        value = fn()
    except Exception:
        return default
    if value is None:
        return default
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _read_msg(path: Path) -> EmailItem:
    import extract_msg

    msg = extract_msg.Message(str(path))
    try:
        body_html = _safe(lambda: msg.htmlBody)
        body_text = _safe(lambda: msg.body)

        item = EmailItem(
            subject=str(_safe(lambda: msg.subject)).strip(),
            sender=str(_safe(lambda: msg.sender)).strip(),
            to=str(_safe(lambda: msg.to)).strip(),
            cc=str(_safe(lambda: msg.cc)).strip(),
            date=str(_safe(lambda: msg.date, "")),
            body_html=body_html,
            body_text=body_text,
            source=path,
            body_is_rtf_only=bool(not body_html and not body_text),
        )

        live = referenced_cids(body_html)

        for att in _safe(lambda: msg.attachments, []) or []:
            try:
                data = att.data
            except Exception:
                data = None
            if not data:
                continue
            name = (
                _safe(lambda: att.longFilename)
                or _safe(lambda: att.shortFilename)
                or "attachment"
            )
            cid = _cid_plain(_safe(lambda: att.cid, ""))
            mimetype = str(_safe(lambda: att.mimetype, ""))
            item.attachments.append(
                Attachment(
                    filename=str(name),
                    data=bytes(data),
                    mimetype=mimetype,
                    content_id=cid,
                    inline=bool(cid and cid in live),
                    category=categorise(str(name), mimetype),
                )
            )
        return item
    finally:
        try:
            msg.close()
        except Exception:
            pass


def _decode_part(part) -> bytes:
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        payload = None
    if payload is None:
        try:
            payload = part.get_payload().encode("utf-8", "replace")
        except Exception:
            payload = b""
    return payload or b""


def _read_eml(path: Path) -> EmailItem:
    import email
    from email import policy as email_policy

    with open(path, "rb") as fh:
        message = email.message_from_binary_file(fh, policy=email_policy.default)

    def header(name: str) -> str:
        value = message.get(name, "")
        return str(value).strip()

    # First pass: find the body, so we know which content IDs it references
    # before deciding what counts as an attachment.
    body_html = ""
    body_text = ""
    candidates = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        mimetype = part.get_content_type() or ""
        filename = part.get_filename()
        if not filename:
            if mimetype == "text/html" and not body_html:
                body_html = _decode_part(part).decode("utf-8", "replace")
                continue
            if mimetype == "text/plain" and not body_text:
                body_text = _decode_part(part).decode("utf-8", "replace")
                continue
        candidates.append(part)

    live = referenced_cids(body_html)
    attachments: list[Attachment] = []

    for part in candidates:
        mimetype = part.get_content_type() or ""
        filename = part.get_filename()
        content_id = _cid_plain(part.get("Content-ID", "") or "")
        disposition = (part.get_content_disposition() or "").lower()

        # An image the body embeds by cid is part of the body, never its own page.
        if content_id and content_id in live and mimetype.startswith("image/"):
            attachments.append(
                Attachment(
                    filename=filename or content_id or "inline-image",
                    data=_decode_part(part),
                    mimetype=mimetype,
                    content_id=content_id,
                    inline=True,
                    category="images",
                )
            )
            continue

        # No filename and not referenced: nothing we can present as an attachment.
        if not filename:
            if content_id and mimetype.startswith("image/"):
                attachments.append(
                    Attachment(content_id, _decode_part(part), mimetype,
                               content_id, inline=True, category="images")
                )
            continue

        # Note: a *named* part with an inline disposition is still an attachment.
        # Real mail is inconsistent about this, and dropping it would lose data.
        attachments.append(
            Attachment(
                filename=filename,
                data=_decode_part(part),
                mimetype=mimetype,
                content_id=content_id,
                inline=False,
                category=categorise(filename, mimetype),
            )
        )

    return EmailItem(
        subject=header("Subject"),
        sender=header("From"),
        to=header("To"),
        cc=header("Cc"),
        date=header("Date"),
        body_html=body_html,
        body_text=body_text,
        attachments=attachments,
        source=path,
        body_is_rtf_only=False,
    )


def read_message(path: Path) -> EmailItem:
    """Parse a ``.msg`` or ``.eml`` file."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext in MSG_EXTS:
        return _read_msg(path)
    if ext in EML_EXTS:
        return _read_eml(path)
    raise ValueError("not an email file: %s" % path)


# ------------------------------------------------------------------ body HTML

_CSS = """
body { font-family: Segoe UI, Arial, sans-serif; font-size: 10.5pt; color: #1a1a1a; }
table.headers { border-collapse: collapse; width: 100%; margin-bottom: 14px; }
table.headers td { padding: 3px 8px; border-bottom: 1px solid #dcdcdc; vertical-align: top; }
table.headers td.label { color: #666; width: 72px; white-space: nowrap; }
h1.subject { font-size: 14pt; margin: 0 0 10px 0; }
table.attachments { border-collapse: collapse; width: 100%; margin-top: 16px; }
table.attachments td { padding: 3px 8px; border-bottom: 1px solid #eee; font-size: 9.5pt; color: #444; }
.note { color: #777; font-size: 9pt; margin-top: 10px; }
pre.body { font-family: Consolas, monospace; font-size: 10pt; white-space: pre-wrap; }
"""


def body_html(item: EmailItem, policy: ExtractionPolicy | None = None) -> str:
    """Build a self-contained HTML document for the message body.

    Headers are composed here rather than trusted to any renderer, and the list
    of attachments is included so nothing is silently invisible.
    """
    parts: list[str] = []
    parts.append("<html><head><meta charset='utf-8'><style>%s</style></head><body>" % _CSS)

    if item.subject:
        parts.append("<h1 class='subject'>%s</h1>" % html_module.escape(item.subject))

    rows = [
        ("From", item.sender),
        ("To", item.to),
        ("CC", item.cc),
        ("Date", item.date),
        ("Subject", item.subject),
    ]
    visible = [(label, value) for label, value in rows if value]
    if visible:
        parts.append("<table class='headers'>")
        for label, value in visible:
            parts.append(
                "<tr><td class='label'>%s</td><td>%s</td></tr>"
                % (html_module.escape(label), html_module.escape(value))
            )
        parts.append("</table>")

    if item.body_html:
        parts.append(item.body_html)
    elif item.body_text:
        parts.append("<pre class='body'>%s</pre>" % html_module.escape(item.body_text))
    else:
        parts.append("<p class='note'>This message has no readable body.</p>")

    if item.attachments:
        parts.append("<table class='attachments'>")
        for att in item.attachments:
            marker = " (inline)" if att.inline else ""
            parts.append(
                "<tr><td>%s</td><td>%s</td><td>%s</td></tr>"
                % (
                    html_module.escape(att.filename),
                    html_module.escape(att.category) + marker,
                    _human_size(att.size),
                )
            )
        parts.append("</table>")

    parts.append("</body></html>")
    return "".join(parts)


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%.0f %s" % (size, unit) if unit == "B" else "%.1f %s" % (size, unit)
        size /= 1024.0
    return "%d B" % size


# ------------------------------------------------------------------ expansion


def _safe_name(name: str, fallback: str) -> str:
    """A filename safe to write into the scratch directory."""
    stem = Path(name or "").name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")
    return stem or fallback


def _write_attachment(att: Attachment, directory: Path, index: int) -> Path:
    suffix = Path(att.filename or "").suffix
    if not suffix:
        # Recover an extension from the MIME type when the name lacks one.
        guessed = {
            "application/pdf": ".pdf",
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/tiff": ".tif",
        }.get((att.mimetype or "").lower(), "")
        suffix = guessed
    target = directory / ("a%03d_%s%s" % (index, Path(_safe_name(att.filename, "file")).stem, suffix))
    target.write_bytes(att.data)
    return target


def write_body_html(item: EmailItem, dest: Path) -> Path:
    """Write the message body as a standalone HTML file."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body_html(item), encoding="utf-8")
    return dest


def render_body_pdf(item: EmailItem, workdir: Path, dest: Path, timeout: int = 300) -> Path:
    """Render one message body to a PDF by way of HTML.

    Used for single conversions and for nested messages. Callers converting many
    messages should write the HTML with :func:`write_body_html` and convert them
    all in one batch instead - LibreOffice start-up dominates otherwise.
    """
    from . import libreoffice

    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    stem = _safe_name((item.source or Path("message")).stem, "message")

    html_path = write_body_html(item, workdir / ("%s_body.html" % stem))

    out_dir = workdir / ("%s_body_out" % stem)
    out_dir.mkdir(parents=True, exist_ok=True)

    result = libreoffice.convert_batch([html_path], out_dir, timeout=timeout)
    produced = result.produced.get(str(html_path))
    if not produced:
        reason = result.failed.get(str(html_path), "body rendering failed")
        raise RuntimeError(reason)

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    import shutil

    shutil.move(produced, dest)
    return dest


def expand_to_segments(
    item: EmailItem,
    workdir: Path,
    policy: ExtractionPolicy,
    page_size: tuple[float, float] | None = None,
    depth: int = 0,
    body_pdf: Path | None = None,
    body_error: str | None = None,
    on_event=None,
) -> list:
    """Expand a parsed message into an ordered list of segments.

    Body first, then attachments in their original order. PDF attachments are
    copied byte-for-byte; everything else is routed through the normal converter
    so an attached spreadsheet becomes pages too.

    ``body_pdf`` lets the caller supply an already-rendered body, which is how a
    batch of messages shares a single LibreOffice invocation.
    """
    from . import convert
    from .model import ATTACHMENT, BODY, DOCUMENT, Segment

    def emit(message: str) -> None:
        if on_event:
            on_event(message)

    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    source = Path(item.source) if item.source else Path("message.msg")
    stem = _safe_name(source.stem, "message")
    segments: list = []
    total_bytes = 0

    # ---- body ----
    if policy.body:
        label = "%s - body" % source.stem
        if body_error is not None:
            segments.append(Segment(source, label, BODY, None, False, body_error))
        elif body_pdf is not None and Path(body_pdf).exists():
            segments.append(Segment(source, label, BODY, Path(body_pdf), True))
        else:
            dest = workdir / ("%s_body.pdf" % stem)
            try:
                render_body_pdf(item, workdir, dest)
                segments.append(Segment(source, label, BODY, dest, True))
            except Exception as exc:
                segments.append(
                    Segment(source, label, BODY, None, False,
                            "%s: %s" % (type(exc).__name__, exc))
                )

    # ---- attachments ----
    for index, att in enumerate(item.attachments):
        label = "%s - %s" % (source.stem, att.filename)
        category = att.category

        if att.inline:
            # Inline images belong to the body; never separate pages.
            continue

        if not policy.wants(category):
            segments.append(
                Segment(source, label, ATTACHMENT, None, False,
                        "excluded by the extraction policy (%s)" % category)
            )
            continue

        if att.size > policy.max_attachment_bytes:
            segments.append(
                Segment(source, label, ATTACHMENT, None, False,
                        "attachment is %s, over the %s limit"
                        % (_human_size(att.size), _human_size(policy.max_attachment_bytes)))
            )
            continue

        total_bytes += att.size
        if total_bytes > policy.max_total_bytes:
            segments.append(
                Segment(source, label, ATTACHMENT, None, False,
                        "message attachments exceed the %s total limit"
                        % _human_size(policy.max_total_bytes))
            )
            continue

        try:
            staged = _write_attachment(att, workdir, index)

            if category == "embedded":
                if depth + 1 > policy.max_depth:
                    segments.append(
                        Segment(source, label, ATTACHMENT, None, False,
                                "nested message deeper than %d levels" % policy.max_depth)
                    )
                    continue
                nested = read_message(staged)
                nested.source = staged
                # An embedded message's body *is* its content, so render it even
                # when the top-level body is switched off. Otherwise enabling
                # "embedded messages" would extract nothing at all.
                nested_policy = replace(policy, body=True)
                inner = expand_to_segments(
                    nested, workdir / ("nested%03d" % index), nested_policy,
                    page_size=page_size, depth=depth + 1, on_event=on_event,
                )
                for seg in inner:
                    seg.label = "%s > %s" % (label, seg.label)
                    seg.kind = ATTACHMENT
                segments.extend(inner)
                continue

            dest = workdir / ("%s_att%03d.pdf" % (stem, index))
            convert.convert(staged, dest, workdir, page_size=page_size)
            segments.append(Segment(source, label, ATTACHMENT, dest, True))
            emit("  ok   %s" % att.filename)

        except Exception as exc:
            segments.append(
                Segment(source, label, ATTACHMENT, None, False,
                        "%s: %s" % (type(exc).__name__, exc))
            )
            emit("  FAIL %s - %s" % (att.filename, exc))

    # Anything the policy kept but that has no converter still deserves a mention.
    if not segments:
        segments.append(
            Segment(source, source.stem, DOCUMENT, None, False, "nothing to extract")
        )

    return segments

