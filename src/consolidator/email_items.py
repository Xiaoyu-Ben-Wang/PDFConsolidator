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

A message attached to a message is drilled into rather than listed: the nested
item is parsed and expanded in place, giving its body and its own attachments
their own pages, nested as deep as the policy allows. Those segments remember
which message they came from, so the interface can nest them under the message
that carried them and give each message its own colour.
"""

from __future__ import annotations

import base64

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
WEB_EXTS = convert.WEB_EXTS
TEXT_EXTS = convert.TEXT_EXTS
# Every attachment that is a document the local engines can lay out, whatever the
# engine: Word, Excel, PowerPoint, HTML, plain text, CSV.
DOCUMENT_EXTS = convert.DOCUMENT_EXTS

# Attachment categories the user can choose to extract.
CATEGORIES = ("body", "pdf", "images", "documents", "embedded", "other")


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
    # Office documents, web pages and plain text: anything that becomes pages as a
    # document rather than as an image or a PDF.
    documents: bool = True
    # Attached messages are expanded by default: a forwarded email is ordinary
    # mail, and the point of the feature is that its contents become pages. The
    # depth and size limits below are what keep that from running away.
    embedded: bool = True
    other: bool = False

    max_attachment_bytes: int = 64 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024
    # Forwards of forwards are common enough to matter: A carries B carries C
    # carries D. Four levels covers that chain with room to spare.
    max_depth: int = 4

    def wants(self, category: str) -> bool:
        return bool(getattr(self, category, False))


# --------------------------------------------------------------- classifying


def categorise(filename: str, mimetype: str = "") -> str:
    ext = Path(filename or "").suffix.lower()
    if ext == ".pdf" or mimetype == "application/pdf":
        return "pdf"
    if ext in IMAGE_EXTS or mimetype.startswith("image/"):
        return "images"
    # Word, Excel, PowerPoint, HTML, plain text, CSV: all become pages as
    # documents. Leaving text and web attachments out of this meant a mail
    # carrying notes.txt or a saved page silently contributed nothing.
    if ext in DOCUMENT_EXTS:
        return "documents"
    if ext in EMAIL_EXTS or mimetype.startswith("message/"):
        return "embedded"
    return "other"


def _cid_plain(value: str) -> str:
    return (value or "").strip().strip("<>").lower()


_CID_REF = re.compile(r"""cid:([^"'\s>)]+)""", re.IGNORECASE)


def referenced_cids(html: str) -> set[str]:
    return {_cid_plain(m) for m in _CID_REF.findall(html or "")}


# ------------------------------------------------------------------- parsing


def _clean(value: str) -> str:
    """Drop the NUL terminators that some ``.msg`` producers leave in strings.

    Seen in a real Outlook export: every string property came back as
    ``'image.png\\x00'``, ``'message/rfc822\\x00'``, ``'test\\x00'``. A NUL is not
    cosmetic here - it makes the staged filename unopenable ("embedded null
    character"), and it hides the extension, so ``email 1.msg\\x00`` was not
    recognised as an email and the attached message was never opened.
    """
    return value.replace("\x00", "")


def _safe(fn, default=""):
    """Read a property that a malformed message may fail to provide.

    Real ``.msg`` files contain junk: undecodable headers, missing streams,
    truncated properties, NUL-terminated strings. ``extract-msg`` raises on some
    of them, and one bad field must not cost the user the whole message.
    """
    try:
        value = fn()
    except Exception:
        return default
    if value is None:
        return default
    if isinstance(value, bytes):
        return _clean(value.decode("utf-8", "replace"))
    if isinstance(value, str):
        return _clean(value)
    return value


def _attachment_bytes(att) -> bytes:
    """An attachment's bytes, whatever shape ``extract-msg`` hands back.

    An ordinary attachment gives ``bytes``. An attached *message* is handed back
    as a ``Message`` object, which serialises through ``bytes()`` - and which has
    to be serialised, because the file it produces is what gets parsed next.
    """
    try:
        data = att.data
    except Exception:
        return b""
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if data is None:
        return b""
    try:
        return bytes(data)
    except Exception:
        return b""


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
            data = _attachment_bytes(att)
            if not data:
                continue
            name = (
                str(_safe(lambda: att.longFilename, "")).strip()
                or str(_safe(lambda: att.shortFilename, "")).strip()
                or "attachment"
            )
            cid = _cid_plain(_safe(lambda: att.cid, ""))
            mimetype = str(_safe(lambda: att.mimetype, "")).strip()
            embedded = _is_embedded_message(att, mimetype)
            if embedded and not _has_email_extension(name):
                # An attached Outlook item is named after its subject, which may
                # carry no extension at all - and extract-msg reports no MIME type
                # for one, so the name is the only thing that can identify it.
                name = _with_email_extension(name, ".msg" if _looks_like_msg_bytes(data)
                                             else ".eml")
            category = "embedded" if embedded else categorise(name, mimetype)
            item.attachments.append(
                Attachment(
                    filename=name,
                    data=data,
                    mimetype=mimetype,
                    content_id=cid,
                    inline=bool(cid and cid in live),
                    category=category,
                )
            )
        return item
    finally:
        try:
            msg.close()
        except Exception:
            pass


def _decode_part(part) -> bytes:
    """The bytes of one part, including a whole attached message.

    ``get_payload(decode=True)`` returns ``None`` for a ``message/rfc822`` part,
    because its payload is a list rather than a string. An attached message has
    to be re-serialised instead - otherwise a forwarded email arrives empty and
    there is nothing to drill into.
    """
    if part.get_content_maintype() == "message":
        payload = part.get_payload()
        if isinstance(payload, list) and payload:
            inner = payload[0]
            if hasattr(inner, "as_bytes"):
                return inner.as_bytes()
        return part.as_bytes()

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


def _has_email_extension(name: str) -> bool:
    return Path(name or "").suffix.lower() in EMAIL_EXTS


def _with_email_extension(name: str, fallback: str = ".eml") -> str:
    """Give an attached message a name that says it is mail.

    The extension is what chooses the parser for the staged copy, so a forwarded
    message called ``forwarded`` or ``email.dat`` has to end up as something the
    reader recognises - otherwise it is rejected later as "not an email file".
    """
    stem = Path(name or "").stem.strip() or "attached message"
    return "%s%s" % (stem, fallback)


def _attached_message_name(part) -> str:
    """A name for an attached message that arrived without one.

    Gmail and Outlook export a forwarded message as a ``message/rfc822`` part
    with **no filename**, so there is nothing to call it. Giving it the inner
    message's subject - with the .eml extension that marks it as an email - is
    what lets it be recognised, staged and shown like any other attachment.
    """
    subject = ""
    payload = part.get_payload()
    if isinstance(payload, list) and payload:
        subject = str(payload[0].get("Subject") or "").strip()
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", subject).strip(" ._")
    return "%s.eml" % (stem[:60] or "attached message")


def _walk_parts(part):
    """The parts of *this* message, with an attached message kept whole.

    ``Message.walk()`` descends into an attached ``message/rfc822``, which
    reports the attachments *of* a forwarded email as attachments of the email
    that forwarded it. That duplicates them, and it destroys the nesting the
    expansion then has to reconstruct - a forwarded email's own attachment would
    show up as a sibling of the forwarded email instead of inside it.
    """
    if part.get_content_maintype() == "multipart":
        for child in part.get_payload():
            yield from _walk_parts(child)
        return
    yield part


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
    for part in _walk_parts(message):
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

        # An attached message is an email whatever it is called. Mail exported by
        # Gmail and Outlook routinely carries the forwarded message as a
        # ``message/rfc822`` part with no filename at all; treating "no filename"
        # as "nothing to present" dropped the entire forward, silently.
        if not filename and mimetype.startswith("message/"):
            filename = _attached_message_name(part)

        # ...and a message part is often *named* something that says nothing about
        # being mail ("forwarded", "attached", "email.dat"). The extension decides
        # which parser opens the staged copy, so a message without an email
        # extension is given one here rather than being refused later as
        # "not an email file".
        if mimetype.startswith("message/") and not _has_email_extension(filename):
            filename = _with_email_extension(filename)

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


_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _looks_like_msg_bytes(data: bytes) -> bool:
    """A ``.msg`` is an OLE2 compound file, which starts with a fixed signature."""
    return data[:8] == _OLE_MAGIC


def _looks_like_msg(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return _looks_like_msg_bytes(handle.read(8))
    except OSError:
        return False


_HEADER_LINE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*:")


def _looks_like_eml(path: Path) -> bool:
    """An RFC 822 message: header lines, then a blank line, then the body.

    Deliberately loose. The only question being answered is whether a file whose
    name says nothing is mail at all, and a false yes costs nothing - the parser
    is about to look at it anyway.
    """
    try:
        head = path.read_bytes()[:8192]
    except OSError:
        return False
    if b"\n\n" not in head and b"\r\n\r\n" not in head:
        return False
    lines = head.decode("utf-8", "replace").splitlines()
    return bool(lines and _HEADER_LINE.match(lines[0]))


def _is_embedded_message(att, mimetype: str) -> bool:
    """Whether an extract-msg attachment is a message rather than a file.

    An attached Outlook item comes back as ``EmbeddedMsgAttachment``, and reports
    **no MIME type at all** - so its class is the only signal that it is mail.
    """
    if mimetype.startswith("message/"):
        return True
    return type(att).__name__ == "EmbeddedMsgAttachment"


def read_message(path: Path) -> EmailItem:
    """Parse a ``.msg`` or ``.eml`` file.

    The extension is the first guess, not the only one: an attached message is
    routinely named by whatever the sending client felt like, so a file that is
    plainly an OLE container or plainly RFC 822 is read as one rather than being
    refused with "not an email file" - which is what a user saw.
    """
    path = Path(path)
    ext = path.suffix.lower()
    if ext in MSG_EXTS:
        return _read_msg(path)
    if ext in EML_EXTS:
        return _read_eml(path)
    if _looks_like_msg(path):
        return _read_msg(path)
    if _looks_like_eml(path):
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


def body_images(item: EmailItem) -> list[Attachment]:
    """Inline images the body actually refers to, in a stable order."""
    live = referenced_cids(item.body_html)
    return [att for att in item.attachments
            if att.inline and att.data and _cid_plain(att.content_id) in live]


# Beyond this, a picture is left as a reference rather than inlined: the HTML is
# held in memory whole, and base64 costs a third again in size.
MAX_INLINE_BYTES = 32 * 1024 * 1024


def body_image_references(item: EmailItem) -> dict[str, str]:
    """Map each content id to a ``data:`` URI holding the image itself.

    Measured against the bundled LibreOffice: a relative filename and an absolute
    ``file:///`` URL both produced a page with **no** image at all, while a data URI
    produced it. Since the image is already in memory, embedding it is also the
    form that needs no temporary files and no cleanup.
    """
    references: dict[str, str] = {}
    for attachment in body_images(item):
        if len(attachment.data) > MAX_INLINE_BYTES:
            continue
        mimetype = attachment.mimetype or "application/octet-stream"
        encoded = base64.b64encode(attachment.data).decode("ascii")
        references[_cid_plain(attachment.content_id)] = "data:%s;base64,%s" % (
            mimetype, encoded)
    return references


def resolve_cids(html: str, references: dict[str, str]) -> str:
    """Point ``cid:`` references at something a renderer can actually open.

    The body arrives with ``<img src="cid:74bc873b-...">``. Nothing can resolve
    that outside a mail client, so the picture vanished from the body page - and
    because inline images are deliberately not extracted as pages of their own, it
    appeared nowhere at all in the output. Rewriting each reference to the image's
    own bytes is what puts it back.
    """
    if not references:
        return html

    def swap(match: "re.Match[str]") -> str:
        return references.get(_cid_plain(match.group(1)), match.group(0))

    return _CID_REF.sub(swap, html)


def body_html(item: EmailItem, policy: ExtractionPolicy | None = None,
              references: dict[str, str] | None = None) -> str:
    """Build a self-contained HTML document for the message body.

    Headers are composed here rather than trusted to any renderer, and the list
    of attachments is included so nothing is silently invisible. ``references``
    maps a content id to a URL - a filename beside the HTML - so that inline
    pictures survive rendering; without it the body is passed through untouched.
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
        parts.append(resolve_cids(item.body_html, references or {}))
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
    # The extension comes from the sanitised name rather than the raw one: a NUL
    # or any other stray character in the suffix would make the path unopenable.
    safe = _safe_name(att.filename, "file")
    suffix = Path(safe).suffix
    if not suffix:
        # Recover an extension from the MIME type when the name lacks one.
        guessed = {
            "application/pdf": ".pdf",
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/tiff": ".tif",
        }.get((att.mimetype or "").lower(), "")
        suffix = guessed
    target = directory / ("a%03d_%s%s" % (index, Path(safe).stem, suffix))
    target.write_bytes(att.data)
    return target


def write_body_html(item: EmailItem, dest: Path,
                    policy: ExtractionPolicy | None = None) -> Path:
    """Write the message body as a standalone HTML file.

    Inline pictures the body refers to are embedded in the document itself, so what
    the message actually contained ends up on the page instead of a gap.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body_html(item, policy, body_image_references(item)),
                    encoding="utf-8")
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
    group: str = "",
    parent: str = "",
    group_label: str = "",
) -> list:
    """Expand a parsed message into an ordered list of segments.

    Body first, then attachments in their original order. PDF attachments are
    copied byte-for-byte; everything else is routed through the normal converter
    so an attached spreadsheet becomes pages too.

    ``body_pdf`` lets the caller supply an already-rendered body, which is how a
    batch of messages shares a single LibreOffice invocation.

    An attached message is *drilled into*: the nested message is parsed and
    expanded in place, and its segments are tagged with ``group``/``parent``/
    ``group_label`` so the interface can nest them and colour them separately
    rather than flattening them into the parent's rows. ``group``, ``parent`` and
    ``group_label`` are supplied by the caller for a nested message and describe
    that message; for a top-level one they default to the item itself.
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

    # The message these pages belong to, the message that carried it, and what to
    # call it. The label prefix is the group name without its extension, so a row
    # can drop it and show just the part ("body", "invoice.pdf").
    own_group = group or str(item.source or source)
    own_parent = parent
    own_label = group_label or source.name
    label_prefix = Path(own_label).stem or source.stem

    def add(label: str, kind: str, pdf: Path | None, ok: bool,
            detail: str = "") -> None:
        segments.append(
            Segment(source, label, kind, pdf, ok, detail,
                    group=own_group, parent=own_parent,
                    group_label=own_label, depth=depth)
        )

    # ---- body ----
    if policy.body:
        label = "%s - body" % label_prefix
        if body_error is not None:
            add(label, BODY, None, False, body_error)
        elif body_pdf is not None and Path(body_pdf).exists():
            add(label, BODY, Path(body_pdf), True)
        else:
            dest = workdir / ("%s_body.pdf" % stem)
            try:
                render_body_pdf(item, workdir, dest)
                add(label, BODY, dest, True)
            except Exception as exc:
                add(label, BODY, None, False, "%s: %s" % (type(exc).__name__, exc))

    # ---- attachments ----
    for index, att in enumerate(item.attachments):
        label = "%s - %s" % (label_prefix, att.filename)
        category = att.category

        if att.inline:
            # Inline images belong to the body; never separate pages.
            continue

        if not policy.wants(category):
            add(label, ATTACHMENT, None, False,
                "excluded by the extraction policy (%s)" % category)
            continue

        if att.size > policy.max_attachment_bytes:
            add(label, ATTACHMENT, None, False,
                "attachment is %s, over the %s limit"
                % (_human_size(att.size), _human_size(policy.max_attachment_bytes)))
            continue

        total_bytes += att.size
        if total_bytes > policy.max_total_bytes:
            add(label, ATTACHMENT, None, False,
                "message attachments exceed the %s total limit"
                % _human_size(policy.max_total_bytes))
            continue

        try:
            staged = _write_attachment(att, workdir, index)

            if category == "embedded":
                if depth + 1 > policy.max_depth:
                    add(label, ATTACHMENT, None, False,
                        "nested message deeper than %d levels" % policy.max_depth)
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
                    # The nested message is its own group, inside this one, and is
                    # named after the attachment that carried it.
                    group=str(staged),
                    parent=own_group,
                    group_label=att.filename or staged.name,
                )
                segments.extend(inner)
                continue

            dest = workdir / ("%s_att%03d.pdf" % (stem, index))
            convert.convert(staged, dest, workdir, page_size=page_size)
            add(label, ATTACHMENT, dest, True)
            emit("  ok   %s" % att.filename)

        except Exception as exc:
            add(label, ATTACHMENT, None, False, "%s: %s" % (type(exc).__name__, exc))
            emit("  FAIL %s - %s" % (att.filename, exc))

    # Anything the policy kept but that has no converter still deserves a mention.
    if not segments:
        add(source.stem, DOCUMENT, None, False, "nothing to extract")

    return segments

