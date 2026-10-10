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
    # An attached Outlook item, already parsed. Reading it straight from the
    # carrier is what makes it reliable: exporting it to bytes and parsing those
    # again fails on plenty of real items, and the failure used to lose it.
    item: "EmailItem | None" = None
    # Why this attachment could not be read at all. It is still listed, and still
    # becomes a row that says so - an attachment that vanishes is the worst case.
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
    # What kind of Outlook item this is ("Appointment", "Contact", "Task"...), and
    # the fields that make it that kind - a meeting's time and place, a contact's
    # numbers. Empty for ordinary mail, whose headers say everything.
    kind: str = ""
    details: list[tuple[str, str]] = field(default_factory=list)


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


def _raw(fn):
    """A property as it comes, or None if reading it fails."""
    try:
        return fn()
    except Exception:
        return None


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


def _open_msg(path: Path):
    """Open a ``.msg`` of any Outlook item class, tolerating what can be tolerated.

    ``extract_msg.Message`` with its defaults is strict, and strictness costs far
    more than it protects: an attached Outlook item of a class it does not handle
    (a document dragged into a mail folder is ``IPM.Document.*``, a recalled mail
    is ``IPM.Outlook.Recall``), an attachment with a missing stream, or one it
    simply does not implement made it raise while *opening the carrier* - so the
    whole message, its body and every other attachment, was lost. Opened
    leniently, the broken part becomes one attachment that says it is broken.
    """
    import extract_msg
    from extract_msg.enums import ErrorBehavior

    lenient = (ErrorBehavior.ATTACH_SUPPRESS_ALL | ErrorBehavior.RTFDE
               | ErrorBehavior.STANDARDS_VIOLATION | ErrorBehavior.NAMED_NAME_STREAM)
    try:
        return extract_msg.openMsg(str(path), strict=False, errorBehavior=lenient)
    except Exception:
        # The last resort silences OLE defects too, which extract-msg warns can
        # misread a damaged file - still better than not reading it at all.
        return extract_msg.openMsg(str(path), strict=False,
                                   errorBehavior=ErrorBehavior.SUPPRESS_ALL)


# Outlook item classes, by prefix, and what to call them. Mail needs no name: it
# is what everyone expects an email file to be.
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
    """HTML body bytes as text, in the charset they were written in.

    Outlook stores the HTML body as bytes in whatever charset the sender used,
    often windows-1252. Decoding everything as UTF-8 turned every accented letter
    and curly quote of such a body into a replacement character.
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


# Nesting that runs deeper than this is not mail any more; the expansion has its
# own, lower, limit that the user controls.
_MAX_PARSE_DEPTH = 16


def _item_from_msg(msg, source: Path | None, depth: int = 0) -> EmailItem:
    """Everything the consolidator needs from one open Outlook item.

    Works on every item class - a message, an appointment, a contact, a task, or
    a class extract-msg has no model for - by falling back to the raw property
    streams, which every class shares.
    """
    message_class = str(_safe(lambda: msg.classType, "")).strip()
    kind = _item_kind(message_class)

    def stream(name: str) -> str:
        return str(_safe(lambda: msg.getStringStream(name), "")).strip()

    # Read raw: _safe would decode the bytes as UTF-8 before the charset is known.
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
        # The fields that make an appointment an appointment - when, where, who -
        # are what a printout of it has to show. extract-msg already knows them
        # for every class it models; the ones mail headers cover are left out.
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
        # Lenient opening makes this rare, but if the list itself cannot be read
        # the message must still say that it had attachments nobody can see.
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
    """One extract-msg attachment, whatever kind it is, as an :class:`Attachment`.

    Never returns nothing: an attachment that cannot be read is still returned,
    with :attr:`Attachment.error` saying why, so it becomes a visible row.
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
        # An attached Outlook item: a forwarded mail, a meeting, a contact, a
        # task. It is parsed straight from the carrier - re-exporting it to bytes
        # and reading those back is how such items went missing.
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
        # An attached Outlook item is named after its subject, which may carry no
        # extension at all - and extract-msg reports no MIME type for one.
        if not _has_email_extension(name):
            name = _with_email_extension(name, ".msg")
        # The serialised copy is a convenience for anything that wants the file;
        # the parsed item is what is used, so a failure here costs nothing.
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
        # image001, scan, ATT00001, file.dat: the name may say nothing, so the
        # bytes decide what the attachment is.
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


def _part_text(part) -> str:
    """A text part decoded in its declared charset."""
    data = _decode_part(part)
    charset = part.get_content_charset() or "utf-8"
    try:
        return data.decode(charset, "replace")
    except LookupError:
        return data.decode("utf-8", "replace")


def _is_body_text(part) -> bool:
    """An unnamed text part that is not marked as an attachment reads as body."""
    if part.get_content_type() not in ("text/plain", "text/html"):
        return False
    if part.get_filename():
        return False
    return (part.get_content_disposition() or "inline").lower() != "attachment"


def _alternative_choice(part):
    """The richest rendering of a ``multipart/alternative``: HTML, else the last."""
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
    """The whole body of a message, and every part that is not body.

    Taking only the *first* text part as the body lost the rest: Apple Mail and
    many phones split a body around an inline picture into text, image, text, and
    everything after the picture was dropped without a trace. Body text parts are
    therefore joined, in order. In a ``multipart/alternative`` only the richest
    rendering is body - the others say the same thing again.
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
                        # Not rendered, but kept as the plain-text form of the body.
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
        extension = ""          # says nothing; the content will decide
    return "part%d%s" % (index + 1, extension)


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
    body_html, body_text, candidates = _collect_body(message)

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

        # No filename. A picture with a content id is a body resource; anything
        # else is still content, so it is named from its type rather than
        # dropped - a PDF part nobody named is still a PDF.
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

        # Note: a *named* part with an inline disposition is still an attachment.
        # Real mail is inconsistent about this, and dropping it would lose data.
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
p.kind { color: #666; font-size: 9pt; text-transform: uppercase; letter-spacing: 1px; margin: 0 0 4px 0; }
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
                    policy: ExtractionPolicy | None = None,
                    inline_images: bool = True) -> Path:
    """Write the message body as a standalone HTML file.

    Inline pictures the body refers to are embedded in the document itself, so what
    the message actually contained ends up on the page instead of a gap.
    ``inline_images=False`` leaves them out, for a retry when a body with them
    could not be rendered.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    references = body_image_references(item) if inline_images else {}
    dest.write_text(body_html(item, policy, references), encoding="utf-8")
    return dest


class _TextOfHtml(HTMLParser):
    """The readable text of an HTML body, with its paragraph and line breaks."""

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
    """The message as plain text: headers, body, attachment list.

    The last resort for a body LibreOffice cannot render - formatting is lost,
    the words are not.
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


# Pause before retrying a body that produced nothing: the usual cause is a
# machine still settling after other soffice processes exited.
BODY_RETRY_PAUSE = 2.0


def render_body_reliably(item: EmailItem, workdir: Path, dest: Path,
                         first_error: str | None = None) -> str:
    """Render a body, falling back step by step until it exists. Returns a note.

    1. LibreOffice, as usual (skipped when ``first_error`` says a batch already
       tried and failed);
    2. LibreOffice again, *alone* - no other soffice running - after a pause;
    3. LibreOffice alone without the inline pictures, in case one of them is
       what it cannot cope with;
    4. plain text, laid out without LibreOffice at all.

    The note is empty if the first attempt worked, and otherwise says what the
    page is - so a body rendered as plain text says so instead of looking like
    the original.
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

    Used for single conversions and for nested messages. Callers converting many
    messages should write the HTML with :func:`write_body_html` and convert them
    all in one batch instead - LibreOffice start-up dominates otherwise.
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
        if body_pdf is not None and body_error is None and Path(body_pdf).exists():
            add(label, BODY, Path(body_pdf), True)
        else:
            # Rendered here, alone if need be, and as plain text as a last
            # resort: one LibreOffice failure must not cost the message its body.
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
            # Inline images belong to the body; never separate pages.
            continue

        if att.error:
            # Unreadable, but present: a row that says so, never a gap.
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
                    # Parsed from the carrier already; the staged copy (which may
                    # be empty, if the item would not serialise) only names it.
                    nested = replace(att.item)
                else:
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

