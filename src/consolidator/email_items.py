"""Outlook items (``.msg``) and email (``.eml``) as inputs.

* **Parse** a message into an :class:`EmailItem` (headers, body, attachments).
  ``.msg`` is read with ``extract-msg``, which parses the OLE2 compound file
  directly without Outlook. ``.eml`` uses the standard library.
* **Expand** an item into an ordered list of PDF *segments*: the body first,
  then one segment per attachment that the policy selects.

PDF attachments are copied byte-for-byte rather than re-rendered, so their pages
and quality are unchanged.

Inline images referenced by ``cid:`` from the body are part of the body and do
not become separate pages (this keeps signature logos out of the output).

An attached message is parsed and expanded in place, up to the policy's depth
limit. Its segments record which message they came from, so the interface can
nest them under the carrying message.
"""

from __future__ import annotations

import base64

import html as html_module
from html.parser import HTMLParser
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import convert
from . import pages as pages_module

MSG_EXTS = convert.MSG_EXTS
EML_EXTS = convert.EML_EXTS
EMAIL_EXTS = convert.EMAIL_EXTS

IMAGE_EXTS = convert.IMAGE_EXTS
OFFICE_EXTS = convert.OFFICE_EXTS
WEB_EXTS = convert.WEB_EXTS
TEXT_EXTS = convert.TEXT_EXTS
# Document types the local converters handle: Word, Excel, PowerPoint, HTML,
# plain text, CSV.
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
    # An attached Outlook item, parsed directly from the carrier. Exporting it to
    # bytes and parsing those again fails on many real items.
    item: "EmailItem | None" = None
    # Why this attachment could not be read. Unreadable attachments are still
    # listed and shown as failed rows.
    error: str = ""

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
    # Outlook item kind ("Appointment", "Contact", "Task", ...) and its
    # kind-specific fields, such as a meeting's time and place. Empty for
    # ordinary mail.
    kind: str = ""
    details: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class ExtractionPolicy:
    """Which attachment categories to extract, and size and depth limits."""

    body: bool = True
    pdf: bool = True
    images: bool = True
    # Office documents, web pages and plain text.
    documents: bool = True
    # Attached messages are expanded by default; the depth and size limits
    # below bound the expansion.
    embedded: bool = True
    other: bool = False

    max_attachment_bytes: int = 64 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024
    # Maximum nesting depth for attached messages (forwards of forwards).
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
    # Word, Excel, PowerPoint, HTML, plain text and CSV are all converted as
    # documents.
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
    """Remove NUL terminators that some ``.msg`` producers leave in strings.

    Some exports return every string property NUL-terminated, for example
    ``'image.png\\x00'``. A NUL makes a filename unopenable ("embedded null
    character") and hides the extension, so ``email 1.msg\\x00`` would not be
    recognised as an email.
    """
    return value.replace("\x00", "")


def _safe(fn, default=""):
    """Read a property, returning ``default`` if it is missing or unreadable.

    ``.msg`` files can contain undecodable headers, missing streams, truncated
    properties and NUL-terminated strings. ``extract-msg`` raises on some of
    these; one bad field should not prevent reading the rest of the message.
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


def _raw(fn):
    """Return a property unchanged, or None if reading it fails."""
    try:
        return fn()
    except Exception:
        return None


def _attachment_bytes(att) -> bytes:
    """Return an attachment's data as bytes.

    ``extract-msg`` returns ``bytes`` for an ordinary attachment and a
    ``Message`` object for an attached message, which is serialised with
    ``bytes()``.
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


def _open_msg(path: Path):
    """Open a ``.msg`` of any Outlook item class in lenient mode.

    With default settings, ``extract-msg`` raises while opening the carrier if
    an attachment is an unhandled item class (such as ``IPM.Document.*`` or
    ``IPM.Outlook.Recall``), has a missing stream, or is unsupported. That loses
    the whole message. In lenient mode the bad attachment is reported as broken
    and the rest of the message is still read.
    """
    import extract_msg
    from extract_msg.enums import ErrorBehavior

    lenient = (ErrorBehavior.ATTACH_SUPPRESS_ALL | ErrorBehavior.RTFDE
               | ErrorBehavior.STANDARDS_VIOLATION | ErrorBehavior.NAMED_NAME_STREAM)
    try:
        return extract_msg.openMsg(str(path), strict=False, errorBehavior=lenient)
    except Exception:
        # Last resort: also suppress OLE errors. extract-msg warns this can
        # misread a damaged file.
        return extract_msg.openMsg(str(path), strict=False,
                                   errorBehavior=ErrorBehavior.SUPPRESS_ALL)


# Display names for Outlook item classes, matched by prefix. Mail classes map
# to an empty name.
_ITEM_KINDS = (
    ("ipm.note", ""),
    ("report", ""),
    ("ipm.schedule.meeting.request", "Meeting request"),
    ("ipm.schedule.meeting.canceled", "Meeting cancellation"),
    ("ipm.schedule.meeting.resp", "Meeting response"),
    ("ipm.schedule.meeting.notification.forward", "Meeting forward notification"),
    ("ipm.appointment", "Appointment"),
    ("ipm.contact", "Contact"),
    ("ipm.distlist", "Contact group"),
    ("ipm.taskrequest", "Task request"),
    ("ipm.task", "Task"),
    ("ipm.stickynote", "Note"),
    ("ipm.activity", "Journal entry"),
    ("ipm.post", "Post"),
    ("ipm.document", "Document"),
    ("ipm.outlook.recall", "Recall"),
)


def _item_kind(message_class: str) -> str:
    lowered = (message_class or "").lower()
    if not lowered or lowered == "ipm":
        return ""
    for prefix, kind in _ITEM_KINDS:
        if lowered.startswith(prefix):
            return kind
    return message_class


def _flatten_header(value, out: list[tuple[str, str]]) -> None:
    """Flatten extract-msg's ``headerFormatProperties`` into label/value pairs."""
    if not isinstance(value, dict):
        return
    for label, entry in value.items():
        if isinstance(entry, dict):
            _flatten_header(entry, out)
            continue
        if isinstance(entry, tuple):
            entry = entry[0] if entry else None
        if entry is None:
            continue
        text = _clean(str(entry)).strip()
        if text:
            out.append((_clean(str(label)), text))


_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?([A-Za-z0-9_.:-]+)""",
                           re.IGNORECASE)


def _decode_html(data) -> str:
    """Decode HTML body bytes using their actual charset.

    Outlook stores the HTML body in the sender's charset, often windows-1252.
    Tries UTF-8, then the ``<meta charset>`` declaration, then cp1252.
    """
    if data is None:
        return ""
    if isinstance(data, str):
        return _clean(data)
    data = bytes(data)
    try:
        return _clean(data.decode("utf-8"))
    except UnicodeDecodeError:
        pass
    match = _META_CHARSET.search(data[:4096])
    if match:
        try:
            return _clean(data.decode(match.group(1).decode("ascii"), "replace"))
        except LookupError:
            pass
    return _clean(data.decode("cp1252", "replace"))


# Hard limit on parse nesting depth. Expansion has its own lower, user-set
# limit (ExtractionPolicy.max_depth).
_MAX_PARSE_DEPTH = 16


def _item_from_msg(msg, source: Path | None, depth: int = 0) -> EmailItem:
    """Build an :class:`EmailItem` from an open Outlook item.

    Works for any item class, including ones extract-msg has no model for, by
    falling back to the raw property streams that all classes share.
    """
    message_class = str(_safe(lambda: msg.classType, "")).strip()
    kind = _item_kind(message_class)

    def stream(name: str) -> str:
        return str(_safe(lambda: msg.getStringStream(name), "")).strip()

    # Read raw: _safe would decode as UTF-8 before the charset is known.
    body_html = _decode_html(_raw(lambda: getattr(msg, "htmlBody"))
                             or _raw(lambda: msg.getStream("__substg1.0_10130102")))
    body_text = str(_safe(lambda: getattr(msg, "body"), "")
                    or stream("__substg1.0_1000"))

    subject = (str(_safe(lambda: getattr(msg, "subject"), "")).strip()
               or stream("__substg1.0_0037")
               or str(_safe(lambda: getattr(msg, "displayName"), "")).strip()
               or stream("__substg1.0_3001"))

    details: list[tuple[str, str]] = []
    if kind:
        # Include kind-specific fields (time, location, attendees, ...) from
        # extract-msg, minus those already shown as mail headers.
        _flatten_header(_safe(lambda: msg.headerFormatProperties, None), details)
        shown = {"from", "to", "cc", "subject", "sent", "date"}
        details = [(label, value) for label, value in details
                   if label.lower() not in shown]

    item = EmailItem(
        subject=subject,
        sender=str(_safe(lambda: getattr(msg, "sender"), "")).strip(),
        to=str(_safe(lambda: getattr(msg, "to"), "")).strip(),
        cc=str(_safe(lambda: getattr(msg, "cc"), "")).strip(),
        date=str(_safe(lambda: getattr(msg, "date"), "") or ""),
        body_html=body_html,
        body_text=body_text,
        source=source,
        body_is_rtf_only=bool(not body_html and not body_text),
        kind=kind,
        details=details,
    )

    try:
        attachments = list(msg.attachments or [])
    except Exception as exc:
        # If the attachment list cannot be read, record a placeholder so the
        # failure is visible.
        item.attachments.append(Attachment(
            "attachments", b"", category="other",
            error="the attachments of this item could not be read (%s: %s)"
                  % (type(exc).__name__, exc)))
        return item

    live = referenced_cids(body_html)
    for att in attachments:
        item.attachments.append(_msg_attachment(att, live, depth))
    return item


def _msg_attachment(att, live: set[str], depth: int) -> Attachment:
    """Convert an extract-msg attachment of any kind to an :class:`Attachment`.

    Always returns an attachment. If it cannot be read, :attr:`Attachment.error`
    holds the reason.
    """
    kind = type(att).__name__
    name = (
        str(_safe(lambda: att.longFilename, "")).strip()
        or str(_safe(lambda: att.shortFilename, "")).strip()
        or str(_safe(lambda: att.displayName, "")).strip()
    )
    cid = _cid_plain(_safe(lambda: att.cid, ""))
    mimetype = str(_safe(lambda: att.mimetype, "")).strip()

    if kind == "EmbeddedMsgAttachment":
        # An attached Outlook item (mail, meeting, contact, task). Parse it
        # directly from the carrier; re-exporting to bytes and re-parsing is
        # unreliable.
        nested_msg = _safe(lambda: att.data, None)
        if nested_msg is None:
            return Attachment(_with_email_extension(name or "attached item", ".msg"),
                              b"", category="embedded",
                              error="the attached Outlook item could not be opened")
        if depth + 1 > _MAX_PARSE_DEPTH:
            return Attachment(_with_email_extension(name or "attached item", ".msg"),
                              b"", category="embedded",
                              error="Outlook items nested more than %d deep"
                                    % _MAX_PARSE_DEPTH)
        try:
            nested = _item_from_msg(nested_msg, None, depth + 1)
        except Exception as exc:
            return Attachment(_with_email_extension(name or "attached item", ".msg"),
                              b"", category="embedded",
                              error="the attached Outlook item could not be read "
                                    "(%s: %s)" % (type(exc).__name__, exc))
        if not name:
            name = nested.subject or nested.kind or "attached item"
        # The name may be a subject with no extension, and extract-msg reports
        # no MIME type for an attached item, so add ``.msg``.
        if not _has_email_extension(name):
            name = _with_email_extension(name, ".msg")
        # The serialised bytes are optional; the parsed item is what is used,
        # so a failure here is ignored.
        data = _raw(lambda: nested_msg.exportBytes(allowBadEmbed=True))
        if not isinstance(data, (bytes, bytearray)):
            data = b""
        return Attachment(name, bytes(data), mimetype, cid, inline=False,
                          category="embedded", item=nested)

    if kind == "WebAttachment":
        url = str(_safe(lambda: att.url, "")).strip()
        return Attachment(name or url or "linked file", b"", mimetype, cid,
                          category=categorise(name, mimetype),
                          error="a link to a file stored online%s, not the file "
                                "itself" % ((" (%s)" % url) if url else ""))

    if kind in ("BrokenAttachment", "UnsupportedAttachment"):
        return Attachment(name or "attachment", b"", mimetype, cid,
                          category=categorise(name, mimetype),
                          error="this attachment is damaged or of a kind that "
                                "cannot be read")

    data = _attachment_bytes(att)
    name = name or "attachment"
    if data:
        # Names like image001, ATT00001 or file.dat are uninformative, so the
        # type is determined from the content.
        name = convert.typed_name(name, data, mimetype)
    if not data:
        return Attachment(name, b"", mimetype, cid,
                          category=categorise(name, mimetype),
                          error="the attachment is empty")

    embedded = _is_embedded_message(att, mimetype)
    if embedded and not _has_email_extension(name):
        name = _with_email_extension(name, ".msg" if _looks_like_msg_bytes(data)
                                     else ".eml")
    category = "embedded" if embedded else categorise(name, mimetype)
    return Attachment(
        filename=name,
        data=data,
        mimetype=mimetype,
        content_id=cid,
        inline=bool(cid and cid in live),
        category=category,
    )


def _read_msg(path: Path) -> EmailItem:
    msg = _open_msg(path)
    try:
        return _item_from_msg(msg, path)
    finally:
        try:
            msg.close()
        except Exception:
            pass


def _decode_part(part) -> bytes:
    """Return the decoded bytes of a MIME part, including attached messages.

    ``get_payload(decode=True)`` returns ``None`` for a ``message/rfc822`` part
    because its payload is a list, so an attached message is re-serialised
    instead.
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
    """Replace an attached message's extension with an email extension.

    The extension selects the parser for the staged copy. Without it, a message
    named ``forwarded`` or ``email.dat`` is rejected as "not an email file".
    """
    stem = Path(name or "").stem.strip() or "attached message"
    return "%s%s" % (stem, fallback)


def _attached_message_name(part) -> str:
    """Return a filename for an attached message that has none.

    Gmail and Outlook export a forwarded message as a ``message/rfc822`` part
    with no filename. The name is built from the inner message's subject plus
    ``.eml``.
    """
    subject = ""
    payload = part.get_payload()
    if isinstance(payload, list) and payload:
        subject = str(payload[0].get("Subject") or "").strip()
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", subject).strip(" ._")
    return "%s.eml" % (stem[:60] or "attached message")


def _walk_parts(part):
    """Yield the leaf parts of this message, keeping attached messages whole.

    ``Message.walk()`` descends into attached ``message/rfc822`` parts, which
    would list a forwarded email's attachments as attachments of the outer
    message, duplicating them and losing the nesting.
    """
    if part.get_content_maintype() == "multipart":
        for child in part.get_payload():
            yield from _walk_parts(child)
        return
    yield part


def _part_text(part) -> str:
    """Decode a text part using its declared charset."""
    data = _decode_part(part)
    charset = part.get_content_charset() or "utf-8"
    try:
        return data.decode(charset, "replace")
    except LookupError:
        return data.decode("utf-8", "replace")


def _is_body_text(part) -> bool:
    """Return whether a part is body text: unnamed text, not an attachment."""
    if part.get_content_type() not in ("text/plain", "text/html"):
        return False
    if part.get_filename():
        return False
    return (part.get_content_disposition() or "inline").lower() != "attachment"


def _alternative_choice(part):
    """Pick the ``multipart/alternative`` part to render: HTML, else the last."""
    children = [c for c in part.get_payload() if hasattr(c, "get_content_type")]
    for child in children:
        if child.get_content_type() == "text/html":
            return child
    for child in children:
        if child.get_content_maintype() == "multipart":
            inner = list(_walk_parts(child))
            if any(p.get_content_type() == "text/html" for p in inner):
                return child
    return children[-1] if children else None


_BODY_BREAK = "<br>"


def _collect_body(message):
    """Return ``(body_html, body_text, other_parts)`` for a message.

    All body text parts are joined in order, because Apple Mail and many phone
    clients split the body around inline images (text, image, text). In a
    ``multipart/alternative`` only the preferred rendering is used.
    """
    html_pieces: list[str] = []
    text_pieces: list[str] = []
    any_html = False
    candidates: list = []
    ordered: list[tuple[str, str]] = []      # ("html" | "text", content)
    alternative_text: list[str] = []

    def visit(part) -> None:
        nonlocal any_html
        if part.get_content_maintype() == "multipart":
            subtype = part.get_content_subtype()
            if subtype == "alternative":
                chosen = _alternative_choice(part)
                for child in part.get_payload():
                    if child is chosen:
                        visit(child)
                    elif _is_body_text(child) and child.get_content_type() == "text/plain":
                        # Not rendered; kept as the plain-text form of the body.
                        alternative_text.append(_part_text(child))
                    elif child.get_filename():
                        # A named part inside an alternative is a real file.
                        candidates.append(child)
                return
            for child in part.get_payload():
                visit(child)
            return
        if _is_body_text(part):
            kind = "html" if part.get_content_type() == "text/html" else "text"
            any_html = any_html or kind == "html"
            ordered.append((kind, _part_text(part)))
            return
        candidates.append(part)

    visit(message)

    for kind, content in ordered:
        if kind == "html":
            html_pieces.append(content)
        else:
            text_pieces.append(content)
            if any_html:
                # Plain pieces between HTML ones keep their place in the body.
                html_pieces.append("<pre class='body'>%s</pre>"
                                   % html_module.escape(content))
    body_html = _BODY_BREAK.join(html_pieces) if any_html else ""
    body_text = "\n".join(text_pieces or alternative_text)
    return body_html, body_text, candidates


def _name_for_unnamed(mimetype: str, index: int) -> str:
    import mimetypes

    extension = {
        "application/pdf": ".pdf", "text/plain": ".txt", "text/html": ".html",
        "image/jpeg": ".jpg", "image/png": ".png", "image/tiff": ".tif",
    }.get(mimetype) or mimetypes.guess_extension(mimetype or "") or ""
    if extension == ".bin":
        extension = ""          # uninformative; the content decides the type
    return "part%d%s" % (index + 1, extension)


def _read_eml(path: Path) -> EmailItem:
    import email
    from email import policy as email_policy

    with open(path, "rb") as fh:
        message = email.message_from_binary_file(fh, policy=email_policy.default)

    def header(name: str) -> str:
        value = message.get(name, "")
        return str(value).strip()

    # Find the body first, to know which content IDs it references before
    # classifying attachments.
    body_html, body_text, candidates = _collect_body(message)

    live = referenced_cids(body_html)
    attachments: list[Attachment] = []

    for part in candidates:
        mimetype = part.get_content_type() or ""
        filename = part.get_filename()
        content_id = _cid_plain(part.get("Content-ID", "") or "")
        disposition = (part.get_content_disposition() or "").lower()

        # Gmail and Outlook often export a forwarded message as a
        # ``message/rfc822`` part with no filename; give it one.
        if not filename and mimetype.startswith("message/"):
            filename = _attached_message_name(part)

        # A message part may be named without an email extension ("forwarded",
        # "email.dat"). The extension selects the parser, so add one.
        if mimetype.startswith("message/") and not _has_email_extension(filename):
            filename = _with_email_extension(filename)

        # An image the body references by cid is part of the body, not a page.
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

        # No filename: an image with a content ID is a body resource; any other
        # part is named from its MIME type and kept.
        if not filename:
            if content_id and mimetype.startswith("image/"):
                attachments.append(
                    Attachment(content_id, _decode_part(part), mimetype,
                               content_id, inline=True, category="images")
                )
                continue
            filename = _name_for_unnamed(mimetype, len(attachments))

        data = _decode_part(part)
        if not mimetype.startswith("message/"):
            filename = convert.typed_name(filename, data, mimetype)

        # A named part with an inline disposition is still treated as an
        # attachment, since mail clients set disposition inconsistently.
        attachments.append(
            Attachment(
                filename=filename,
                data=data,
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
    """Return whether data starts with the OLE2 signature used by ``.msg``."""
    return data[:8] == _OLE_MAGIC


def _looks_like_msg(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return _looks_like_msg_bytes(handle.read(8))
    except OSError:
        return False


_HEADER_LINE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*:")


def _looks_like_eml(path: Path) -> bool:
    """Return whether a file looks like RFC 822: headers, a blank line, a body.

    The check is loose on purpose; a false positive is harmless because the
    parser reads the file next anyway.
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
    """Return whether an extract-msg attachment is a message rather than a file.

    An attached Outlook item is an ``EmbeddedMsgAttachment`` with no MIME type,
    so its class is the only indicator.
    """
    if mimetype.startswith("message/"):
        return True
    return type(att).__name__ == "EmbeddedMsgAttachment"


def read_message(path: Path) -> EmailItem:
    """Parse a ``.msg`` or ``.eml`` file.

    The extension is checked first. Otherwise the content is checked, because
    attached messages often have arbitrary names: an OLE2 file is read as
    ``.msg`` and an RFC 822 file as ``.eml``.
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
p.kind { color: #666; font-size: 9pt; text-transform: uppercase; letter-spacing: 1px; margin: 0 0 4px 0; }
table.attachments { border-collapse: collapse; width: 100%; margin-top: 16px; }
table.attachments td { padding: 3px 8px; border-bottom: 1px solid #eee; font-size: 9.5pt; color: #444; }
.note { color: #777; font-size: 9pt; margin-top: 10px; }
pre.body { font-family: Consolas, monospace; font-size: 10pt; white-space: pre-wrap; }
"""


def body_images(item: EmailItem) -> list[Attachment]:
    """Return the inline images the body references, in attachment order."""
    live = referenced_cids(item.body_html)
    return [att for att in item.attachments
            if att.inline and att.data and _cid_plain(att.content_id) in live]


# Images larger than this are not inlined: the HTML is held in memory, and
# base64 adds about 33% to the size.
MAX_INLINE_BYTES = 32 * 1024 * 1024


def body_image_references(item: EmailItem) -> dict[str, str]:
    """Map each content ID to a ``data:`` URI containing the image.

    The bundled LibreOffice does not render images referenced by relative
    filename or ``file:///`` URL, but does render ``data:`` URIs. This also
    avoids temporary files.
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
    """Replace ``cid:`` image references with something a renderer can open.

    ``cid:`` URLs point at MIME parts and cannot be resolved outside a mail
    client. Inline images are not extracted as separate pages, so each
    reference is replaced with the URI from ``references`` to keep the image
    in the body.
    """
    if not references:
        return html

    def swap(match: "re.Match[str]") -> str:
        return references.get(_cid_plain(match.group(1)), match.group(0))

    return _CID_REF.sub(swap, html)


def body_html(item: EmailItem, policy: ExtractionPolicy | None = None,
              references: dict[str, str] | None = None) -> str:
    """Build a self-contained HTML document for the message body.

    Includes a header table and a list of all attachments. ``references`` maps
    content IDs to URLs used to replace ``cid:`` image references; without it
    the body HTML is used unchanged.
    """
    parts: list[str] = []
    parts.append("<html><head><meta charset='utf-8'><style>%s</style></head><body>" % _CSS)

    if item.kind:
        parts.append("<p class='kind'>%s</p>" % html_module.escape(item.kind))
    if item.subject:
        parts.append("<h1 class='subject'>%s</h1>" % html_module.escape(item.subject))

    rows = [
        ("From", item.sender),
        ("To", item.to),
        ("CC", item.cc),
        ("Date", item.date),
        ("Subject", item.subject),
    ] + list(item.details)
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
            if att.error:
                marker += " - " + att.error
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
    """Return a filename safe to write into the scratch directory."""
    stem = Path(name or "").name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")
    return stem or fallback


def _write_attachment(att: Attachment, directory: Path, index: int) -> Path:
    # Take the extension from the sanitised name; a NUL or other invalid
    # character in the raw suffix would make the path unopenable.
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
                    policy: ExtractionPolicy | None = None,
                    inline_images: bool = True) -> Path:
    """Write the message body as a standalone HTML file.

    Inline images referenced by the body are embedded as ``data:`` URIs.
    ``inline_images=False`` omits them, for retrying a body that failed to
    render with them.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    references = body_image_references(item) if inline_images else {}
    dest.write_text(body_html(item, policy, references), encoding="utf-8")
    return dest


class _TextOfHtml(HTMLParser):
    """Extract the text of an HTML body, keeping paragraph and line breaks."""

    BREAKS = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
              "table", "blockquote", "pre", "hr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs) -> None:
        if tag in ("style", "script", "head", "title"):
            self._skip += 1
        elif tag in self.BREAKS:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append("\t")

    def handle_endtag(self, tag) -> None:
        if tag in ("style", "script", "head", "title"):
            self._skip = max(0, self._skip - 1)
        elif tag in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data) -> None:
        if not self._skip:
            self.parts.append(" ".join(data.split()) if data.strip() else
                              (" " if data else ""))


def html_to_text(html: str) -> str:
    parser = _TextOfHtml()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:
        pass
    text = "".join(parser.parts)
    return re.sub(r"\n[ \t]*\n([ \t]*\n)+", "\n\n", text).strip()


def body_text_page(item: EmailItem) -> str:
    """Return the message as plain text: headers, body, attachment list.

    Used as the fallback when LibreOffice cannot render the body.
    """
    lines = []
    if item.kind:
        lines.append(item.kind.upper())
    if item.subject:
        lines += [item.subject, ""]
    for label, value in [("From", item.sender), ("To", item.to), ("CC", item.cc),
                         ("Date", item.date)] + list(item.details):
        if value:
            lines.append("%-10s %s" % (label + ":", value))
    lines.append("")
    body = item.body_text if item.body_text.strip() else html_to_text(item.body_html)
    lines.append(body or "(This message has no readable body.)")
    if item.attachments:
        lines += ["", "Attachments:"]
        for att in item.attachments:
            lines.append("  %s  (%s, %s)" % (att.filename, att.category, _human_size(att.size)))
    return "\n".join(lines)


# Pause in seconds before retrying a body that produced nothing, to let other
# soffice processes finish exiting.
BODY_RETRY_PAUSE = 2.0


def render_body_reliably(item: EmailItem, workdir: Path, dest: Path,
                         first_error: str | None = None) -> str:
    """Render a body to PDF, trying fallbacks in order. Returns a note.

    1. LibreOffice (skipped when ``first_error`` is set, meaning a batch
       already failed);
    2. LibreOffice again, with no other soffice running, after a pause;
    3. LibreOffice alone, without inline images;
    4. plain text, rendered without LibreOffice.

    The note is empty if the first attempt succeeded; otherwise it describes
    which fallback produced the page.
    """
    import time

    from . import libreoffice, text_render

    errors = [first_error] if first_error else []
    if first_error is None:
        try:
            render_body_pdf(item, workdir, dest)
            return ""
        except Exception as exc:
            errors.append(str(exc))
    if libreoffice.stopping():
        raise RuntimeError(errors[-1] if errors else "the application is closing")

    time.sleep(BODY_RETRY_PAUSE)
    for attempt, images in (("retry", True), ("retry without pictures", False)):
        try:
            with libreoffice.alone():
                render_body_pdf(item, Path(workdir) / attempt.replace(" ", "_"),
                                dest, inline_images=images)
            if images:
                return "rendered on a second attempt"
            return ("rendered without its inline pictures; LibreOffice could not "
                    "render it with them (%s)" % errors[0])
        except Exception as exc:
            errors.append(str(exc))
            if libreoffice.stopping():
                raise RuntimeError(errors[-1])

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    text_render.render(body_text_page(item), dest, pages_module.LETTER,
                       title=item.subject or "message")
    return ("shown as plain text: LibreOffice could not render this body (%s)"
            % errors[0])


def render_body_pdf(item: EmailItem, workdir: Path, dest: Path, timeout: int = 300,
                    inline_images: bool = True) -> Path:
    """Render one message body to a PDF by way of HTML.

    Used for single conversions and nested messages. To convert many messages,
    write the HTML with :func:`write_body_html` and convert in one batch to
    avoid repeated LibreOffice start-up.
    """
    from . import libreoffice

    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    stem = _safe_name((item.source or Path("message")).stem, "message")

    html_path = write_body_html(item, workdir / ("%s_body.html" % stem),
                                inline_images=inline_images)

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
    copied byte-for-byte; other attachments go through the normal converter.

    ``body_pdf`` supplies an already-rendered body, so a batch of messages can
    share one LibreOffice invocation.

    An attached message is parsed and expanded recursively. Its segments are
    tagged with ``group``, ``parent`` and ``group_label`` so the interface can
    nest them. The caller passes these for a nested message; for a top-level
    message they default to the item itself.
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

    # This message's group, its parent group, and its display name. The label
    # prefix is the group name without its extension, so a row can strip it and
    # show just the part ("body", "invoice.pdf").
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
        if body_pdf is not None and body_error is None and Path(body_pdf).exists():
            add(label, BODY, Path(body_pdf), True)
        else:
            # Render here with fallbacks (solo LibreOffice run, then plain text)
            # so a single LibreOffice failure does not lose the body.
            dest = workdir / ("%s_body.pdf" % stem)
            try:
                note = render_body_reliably(item, workdir / "body", dest,
                                            first_error=body_error)
                add(label, BODY, dest, True, note)
                if note:
                    emit("  note %s - %s" % (label, note))
            except Exception as exc:
                add(label, BODY, None, False, "%s: %s" % (type(exc).__name__, exc))

    # ---- attachments ----
    for index, att in enumerate(item.attachments):
        label = "%s - %s" % (label_prefix, att.filename)
        category = att.category

        if att.inline:
            # Inline images are part of the body, not separate pages.
            continue

        if att.error:
            # Unreadable attachment: record a failed segment with the reason.
            add(label, ATTACHMENT, None, False, att.error)
            emit("  FAIL %s - %s" % (att.filename, att.error))
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
                if att.item is not None:
                    # Already parsed from the carrier. The staged copy (empty if
                    # the item did not serialise) only provides its name.
                    nested = replace(att.item)
                else:
                    nested = read_message(staged)
                nested.source = staged
                # Always render an embedded message's body, even when the
                # top-level body is disabled; otherwise nothing would be extracted.
                nested_policy = replace(policy, body=True)
                inner = expand_to_segments(
                    nested, workdir / ("nested%03d" % index), nested_policy,
                    page_size=page_size, depth=depth + 1, on_event=on_event,
                    # The nested message is a child group, named after the
                    # attachment that carried it.
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

    # Record a failed segment if the message produced nothing.
    if not segments:
        add(source.stem, DOCUMENT, None, False, "nothing to extract")

    return segments

