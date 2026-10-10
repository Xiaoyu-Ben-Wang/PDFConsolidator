"""Outlook item and email expansion tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from consolidator import email_items, libreoffice, pages, pipeline  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EMAIL_FIXTURES = ROOT / "fixtures" / "email"
MSG_FIXTURES = ROOT / "fixtures" / "msg"

requires_lo = pytest.mark.skipif(
    libreoffice.find_soffice() is None, reason="LibreOffice is not installed"
)


@pytest.fixture(scope="session")
def emails():
    if not EMAIL_FIXTURES.exists() or not list(EMAIL_FIXTURES.glob("*.eml")):
        pytest.skip("email fixtures missing; run tools/make_email_fixtures.py")
    return EMAIL_FIXTURES


@pytest.fixture(scope="session")
def messages():
    found = list(MSG_FIXTURES.glob("*.msg")) if MSG_FIXTURES.exists() else []
    if not found:
        pytest.skip("no .msg samples; run tools/get_msg_samples.py")
    return MSG_FIXTURES


# ------------------------------------------------------------------ parsing ---


def test_reads_html_body_and_flags(emails):
    item = email_items.read_message(emails / "html_inline.eml")
    assert item.subject == "Design review"
    assert "Design review" in item.body_html
    assert "<table" in item.body_html.lower()


def test_inline_image_is_not_an_attachment_page(emails):
    item = email_items.read_message(emails / "html_inline.eml")
    inline = [a for a in item.attachments if a.category == "images"]
    assert inline, "the inline signature image should still be parsed"
    assert all(a.inline for a in inline), "it must be marked inline, not standalone"

    attached = [a for a in item.attachments if a.category == "pdf"]
    assert len(attached) == 1 and not attached[0].inline


def test_named_inline_part_is_still_an_attachment(tmp_path):
    """Real mail marks genuine attachments 'inline'; dropping them loses data."""
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = "inline-dispositioned attachment"
    message.set_content("see file")
    message.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf",
                           filename="report.pdf")
    # Force the dishonest disposition that caused the original bug.
    message.get_payload()[-1].replace_header(
        "Content-Disposition", 'inline; filename="report.pdf"')

    path = tmp_path / "inline_disposition.eml"
    path.write_bytes(message.as_bytes())

    item = email_items.read_message(path)
    assert [a.filename for a in item.attachments] == ["report.pdf"]


def test_multiple_attachment_categories(emails):
    item = email_items.read_message(emails / "with_pdf_and_docx.eml")
    assert sorted(a.category for a in item.attachments) == ["documents", "images", "pdf"]


def test_duplicate_filenames_both_survive(emails):
    item = email_items.read_message(emails / "duplicate_names.eml")
    names = [a.filename for a in item.attachments]
    assert names == ["invoice.pdf", "invoice.pdf"]
    assert item.attachments[0].data == item.attachments[1].data


def test_reads_real_outlook_msg(messages):
    """Genuine Outlook output, including one with binary attachments."""
    item = email_items.read_message(messages / "unicode.msg")
    assert item.subject
    assert len(item.attachments) == 2
    assert all(a.data for a in item.attachments)


def test_msg_survives_a_malformed_header(messages):
    """A real sample whose recipient header fails to decode must still load.

    extract-msg raises on some malformed headers; one bad field must not cost
    the whole message.
    """
    for path in messages.glob("*.msg"):
        item = email_items.read_message(path)
        assert item.subject is not None


# --------------------------------------------------- .msg quirks (real files) ---


class FakeAttachment:
    """Stands in for extract-msg's attachment objects."""

    def __init__(self, data=None, long=None, short=None, mime=None, cid=None):
        if data is not None:
            self.data = data
        self.longFilename = long
        self.shortFilename = short
        self.mimetype = mime
        self.cid = cid


def test_nul_terminated_strings_from_a_msg_are_cleaned():
    """Measured on a real export: 'test\\x00', 'image.png\\x00', 'message/rfc822\\x00'."""
    assert email_items._clean("image.png\x00") == "image.png"
    assert email_items._safe(lambda: "email 1.msg\x00") == "email 1.msg"
    assert email_items._safe(lambda: b"test\x00") == "test"


def test_a_nul_terminated_name_still_identifies_an_email():
    """'email 1.msg\\x00' is not a '.msg' by suffix, which is the whole bug.

    Left uncleaned, the attached message was classified as a stranger and never
    opened - the attached email simply did not appear.
    """
    assert email_items.categorise("email 1.msg", "") == "embedded"
    assert email_items.categorise(email_items._clean("email 1.msg\x00"), "") == "embedded"


def test_an_attachment_with_a_stray_terminator_is_still_staged(tmp_path):
    """A NUL in the suffix made the staged path unopenable."""
    att = email_items.Attachment("image.png\x00", b"PNG" * 4, "image/png\x00")
    path = email_items._write_attachment(att, tmp_path, 0)

    assert path.exists() and path.read_bytes() == b"PNG" * 4
    assert path.name.endswith(".png"), "the extension decides how it is read"


def test_an_attached_message_is_serialised_for_staging():
    """extract-msg hands back a Message object for an embedded message."""
    class MessageLike:
        def __bytes__(self):
            return b"the nested message, serialised"

    class Hostile:
        @property
        def data(self):
            raise RuntimeError("no stream")

    assert email_items._attachment_bytes(FakeAttachment(b"plain")) == b"plain"
    assert email_items._attachment_bytes(FakeAttachment(MessageLike())) == \
        b"the nested message, serialised"
    assert email_items._attachment_bytes(FakeAttachment(None)) == b""
    assert email_items._attachment_bytes(Hostile()) == b""


def test_real_msg_samples_keep_every_attachment(messages):
    """Genuine Outlook output: nothing may be dropped or left with a stray NUL."""
    for path in sorted(messages.glob("*.msg")):
        item = email_items.read_message(path)
        assert "\x00" not in item.subject
        assert "\x00" not in item.sender
        for att in item.attachments:
            assert "\x00" not in att.filename, path.name
            assert "\x00" not in att.mimetype, path.name
            assert att.data, "%s: %r has no data" % (path.name, att.filename)
            assert att.category in email_items.CATEGORIES


# ----------------------------------------------------------------- expansion ---


@requires_lo
def test_pdf_attachment_is_merged_verbatim(emails, tmp_path):
    """The PDF must be byte-identical, so its page count is exact."""
    item = email_items.read_message(emails / "with_pdf.eml")
    policy = email_items.ExtractionPolicy(body=False)

    segments = email_items.expand_to_segments(item, tmp_path, policy)
    assert len(segments) == 1 and segments[0].ok

    source_pdf = ROOT / "fixtures" / "generated" / "existing.pdf"
    assert segments[0].pdf.read_bytes() == source_pdf.read_bytes()


@requires_lo
def test_expansion_order_is_body_then_attachments(emails, tmp_path):
    item = email_items.read_message(emails / "with_pdf_and_docx.eml")
    segments = email_items.expand_to_segments(
        item, tmp_path, email_items.ExtractionPolicy(), page_size=pages.LETTER
    )
    failures = [s.detail for s in segments if not s.ok]
    kinds = [s.kind for s in segments if s.ok]
    assert kinds and kinds[0] == "body", "body failed: %s" % failures
    assert kinds[1:] == ["attachment"] * (len(kinds) - 1)


@requires_lo
def test_policy_excludes_categories(emails, tmp_path):
    item = email_items.read_message(emails / "with_pdf_and_docx.eml")
    policy = email_items.ExtractionPolicy(body=False, pdf=True, images=False, documents=False)

    segments = email_items.expand_to_segments(item, tmp_path, policy)
    ok = [s for s in segments if s.ok]
    excluded = [s for s in segments if not s.ok]

    assert len(ok) == 1, "only the PDF should survive"
    assert len(excluded) == 2
    assert all("extraction policy" in s.detail for s in excluded)


@requires_lo
def test_nested_message_respects_depth_limit(emails, tmp_path):
    item = email_items.read_message(emails / "nested.eml")

    policy = email_items.ExtractionPolicy(body=False, embedded=False)
    segments = email_items.expand_to_segments(item, tmp_path, policy)
    assert not any(s.ok for s in segments), "embedded messages are excluded by default"

    policy_on = email_items.ExtractionPolicy(body=False, embedded=True, max_depth=2)
    segments = email_items.expand_to_segments(item, tmp_path / "on", policy_on)
    assert any(s.ok for s in segments), "with the policy on, the nested body is produced"


# ------------------------------------------------------ nesting attached mail ---


def forwarded_chain(depth: int):
    """An email carrying another email, ``depth`` levels down."""
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = "innermost"
    message["From"] = "dana@example.com"
    message["To"] = "sam@example.com"
    message.set_content("The innermost message.")

    for level in range(depth):
        outer = EmailMessage()
        outer["Subject"] = "level %d" % level
        outer["From"] = "dana@example.com"
        outer["To"] = "sam@example.com"
        outer.set_content("Cover note at level %d." % level)
        outer.add_attachment(message, filename="forwarded%d.eml" % level)
        message = outer
    return message


def test_attached_message_keeps_its_content(tmp_path):
    """An attached message must arrive whole, not as an empty part.

    ``get_payload(decode=True)`` returns None for ``message/rfc822``, so a
    forwarded email used to be written out as a zero-byte file and then read back
    as an empty message - nothing to drill into.
    """
    path = tmp_path / "forwarded.eml"
    path.write_bytes(forwarded_chain(1).as_bytes())

    item = email_items.read_message(path)
    assert [a.filename for a in item.attachments] == ["forwarded0.eml"]
    assert item.attachments[0].category == "embedded"
    assert b"innermost" in item.attachments[0].data


def test_nested_messages_are_tagged_with_their_place_in_the_chain(tmp_path):
    """Each attached message is its own group, inside the one that carried it."""
    path = tmp_path / "chain.eml"
    path.write_bytes(forwarded_chain(2).as_bytes())

    item = email_items.read_message(path)
    segments = email_items.expand_to_segments(
        item, tmp_path / "work",
        email_items.ExtractionPolicy(body=False, embedded=True),
    )
    assert segments, "the nested messages should produce segments"

    outer = str(path)
    first = [s for s in segments if s.depth == 1]
    second = [s for s in segments if s.depth == 2]

    assert first and second, "both levels of nesting should be recorded"
    assert all(s.parent == outer for s in first)
    assert all(s.group != outer for s in first), "a nested message is its own group"
    assert all(s.group_label == "forwarded1.eml" for s in first)
    assert all(s.parent == first[0].group for s in second)
    assert all(s.group_label == "forwarded0.eml" for s in second)
    # Labels stay local to their own message: the chain is the navigator's job,
    # and repeating it in every label made rows unreadable.
    assert all(" > " not in s.label for s in segments)


def test_nesting_beyond_the_depth_limit_is_reported(tmp_path):
    path = tmp_path / "deep.eml"
    path.write_bytes(forwarded_chain(3).as_bytes())

    item = email_items.read_message(path)
    segments = email_items.expand_to_segments(
        item, tmp_path / "work",
        email_items.ExtractionPolicy(body=False, embedded=True, max_depth=1),
    )
    stopped = [s for s in segments if "deeper than" in (s.detail or "")]
    assert stopped, "the depth limit must be reported rather than silently dropped"
    assert max(s.depth for s in segments) == 1


@requires_lo
def test_nested_body_becomes_pages(emails, tmp_path):
    """The whole point: a forwarded message's own body yields pages."""
    item = email_items.read_message(emails / "nested.eml")
    segments = email_items.expand_to_segments(
        item, tmp_path, email_items.ExtractionPolicy(body=False, embedded=True)
    )
    nested = [s for s in segments if s.depth == 1 and s.ok and s.pdf]
    assert nested, "the forwarded body should have been rendered: %s" % [
        s.detail for s in segments
    ]
    assert nested[0].group_label == "forwarded.eml"


# ------------------------------------- attachments *inside* an attached message


def _pdf(path, pages: int = 1):
    """A real PDF, so the byte-for-byte copy can be checked exactly."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    with open(path, "wb") as handle:
        writer.write(handle)
    return path


def message_with_attachment(subject: str, body: str, payload, filename: str):
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = "dana@example.com"
    message["To"] = "sam@example.com"
    message.set_content(body)
    message.add_attachment(payload, maintype="application", subtype="pdf",
                           filename=filename)
    return message


def test_an_attached_message_keeps_its_own_attachments(tmp_path):
    """A carries B; B carries a PDF of its own. Both survive, one level apart."""
    from email.message import EmailMessage

    note = _pdf(tmp_path / "note.pdf", 2)

    inner = EmailMessage()
    inner["Subject"] = "The original message"
    inner["From"] = "kim@example.com"
    inner["To"] = "sam@example.com"
    inner.set_content("Here is the note.")
    inner.add_attachment(note.read_bytes(), maintype="application", subtype="pdf",
                         filename="note.pdf")

    cover = _pdf(tmp_path / "cover.pdf", 1)
    outer = EmailMessage()
    outer["Subject"] = "Fwd: The original message"
    outer["From"] = "sam@example.com"
    outer["To"] = "dana@example.com"
    outer.set_content("Forwarding this.")
    outer.add_attachment(inner, filename="forwarded.eml")
    outer.add_attachment(cover.read_bytes(), maintype="application", subtype="pdf",
                         filename="cover.pdf")

    path = tmp_path / "outer.eml"
    path.write_bytes(outer.as_bytes())

    # Parsing: B's attachment belongs to B, not to A.
    item = email_items.read_message(path)
    assert [a.filename for a in item.attachments] == ["forwarded.eml", "cover.pdf"]

    segments = email_items.expand_to_segments(
        item, tmp_path / "work",
        email_items.ExtractionPolicy(body=False, embedded=True),
    )

    cover_segments = [s for s in segments if s.label.endswith("cover.pdf")]
    assert len(cover_segments) == 1
    assert cover_segments[0].depth == 0, "A's own attachment sits at A's level"
    assert cover_segments[0].ok and cover_segments[0].pdf

    note_segments = [s for s in segments if s.label.endswith("note.pdf")]
    assert len(note_segments) == 1, "B's attachment must appear exactly once"
    nested = note_segments[0]
    assert nested.depth == 1, "it belongs to the message inside A"
    assert nested.group != str(path), "it is grouped under B, not under A"
    assert nested.parent == str(path), "and it is nested inside A"
    assert nested.group_label == "forwarded.eml"
    assert nested.ok and nested.pdf, nested.detail
    assert nested.pdf.read_bytes() == note.read_bytes(), "copied byte-for-byte"

    from pypdf import PdfReader

    assert len(PdfReader(str(nested.pdf)).pages) == 2


def test_an_attached_message_without_a_filename_is_still_an_email(tmp_path):
    """The shape Gmail and Outlook actually export a forward in.

    Measured on a real export: the forwarded message arrives as a
    ``message/rfc822`` part with **no filename** and an attachment disposition.
    Treating "no filename" as "nothing to present" dropped the entire forward,
    silently - the body of the message the user cared about simply was not there.
    """
    from email.message import EmailMessage

    inner = EmailMessage()
    inner["Subject"] = "hello"
    inner["From"] = "dana@example.com"
    inner.set_content("the forwarded body")

    outer = EmailMessage()
    outer["Subject"] = "hi there"
    outer.set_content("see below")
    outer.add_attachment(inner)          # no filename: exactly the real case

    path = tmp_path / "hi there.eml"
    path.write_bytes(outer.as_bytes())

    item = email_items.read_message(path)
    assert [a.category for a in item.attachments] == ["embedded"], \
        "an unnamed message/rfc822 is still an email"
    assert "hello" in item.attachments[0].filename, \
        "it should be named after the message it carries"
    assert item.attachments[0].filename.endswith(".eml")

    segments = email_items.expand_to_segments(
        item, tmp_path / "work",
        email_items.ExtractionPolicy(body=False, embedded=True),
    )
    assert [s.depth for s in segments] == [1], "the forward should have been expanded"
    assert segments[0].parent == str(path)
    assert segments[0].group_label == item.attachments[0].filename


def test_an_unnamed_attached_message_without_a_subject_still_gets_a_name(tmp_path):
    from email.message import EmailMessage

    inner = EmailMessage()
    inner.set_content("no subject at all")
    outer = EmailMessage()
    outer["Subject"] = "outer"
    outer.set_content("see below")
    outer.add_attachment(inner)

    path = tmp_path / "plain.eml"
    path.write_bytes(outer.as_bytes())

    attachments = email_items.read_message(path).attachments
    assert [a.filename for a in attachments] == ["attached message.eml"]


def test_an_attached_message_is_embedded_whatever_its_content_type():
    assert email_items.categorise("", "message/rfc822") == "embedded"
    assert email_items.categorise("forwarded", "message/rfc822") == "embedded"
    assert email_items.categorise("note.pdf", "application/pdf") == "pdf"
    assert email_items.categorise("photo.jpg", "image/jpeg") == "images"


def _inline_image_email(tmp_path: Path) -> Path:
    """A message whose HTML body shows a picture by content id."""
    import io
    from email.message import EmailMessage

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 30, 30)).save(buffer, "PNG")
    png = buffer.getvalue()

    message = EmailMessage()
    message["Subject"] = "with a picture"
    message["From"] = "dana@example.com"
    message.set_content("a picture follows")
    message.add_alternative(
        "<html><body><p>Look:</p><img src=\"cid:pic@example\" width=64 "
        "height=64></body></html>", subtype="html")
    message.get_payload()[1].add_related(png, maintype="image", subtype="png",
                                         cid="<pic@example>",
                                         filename="pic.png")
    path = tmp_path / "inline.eml"
    path.write_bytes(message.as_bytes())
    return path


def test_an_inline_image_is_referenced_not_left_as_a_cid(tmp_path):
    """A body saying src="cid:..." renders as a broken image everywhere else."""
    source = _inline_image_email(tmp_path)
    item = email_items.read_message(source)

    assert email_items.referenced_cids(item.body_html) == {"pic@example"}
    assert [a.filename for a in email_items.body_images(item)] == ["pic.png"]

    plain = email_items.body_html(item)
    assert "cid:pic@example" in plain, "without references the body is untouched"

    html_path = email_items.write_body_html(item, tmp_path / "out" / "body.html")
    written = html_path.read_text(encoding="utf-8")

    assert "cid:pic@example" not in written, "the reference must be resolved"
    # A data URI, not a filename: measured against the bundled LibreOffice, a
    # relative filename and an absolute file:/// URL both produced no image at all.
    assert "data:image/png;base64," in written
    assert "inline_001.png" not in written


@requires_lo
def test_an_inline_image_reaches_the_body_page(tmp_path):
    """End to end: the picture is on the page, not a placeholder."""
    from pypdf import PdfReader

    source = _inline_image_email(tmp_path)
    item = email_items.read_message(source)
    dest = tmp_path / "body.pdf"

    email_items.render_body_pdf(item, tmp_path / "work", dest)

    page = PdfReader(str(dest)).pages[0]
    resources = page.get("/Resources") or {}
    xobjects = resources.get("/XObject") or {}
    images = [key for key in xobjects
              if xobjects[key].get("/Subtype") == "/Image"]
    assert images, "the inline image did not make it onto the page"


def test_a_pdf_cut_short_in_a_message_is_reported_not_ignored(tmp_path):
    """The reported case: a PDF attachment that was cut off inside a message.

    A PDF that was cut short still reads up to the cut, so the pages before it look
    fine. What must never happen is that it quietly becomes a shorter document:
    the item is either converted or reported, never silently reduced.
    """
    from email.message import EmailMessage

    from pypdf import PdfWriter

    whole = tmp_path / "whole.pdf"
    writer = PdfWriter()
    for _ in range(6):
        writer.add_blank_page(width=612, height=792)
    with open(whole, "wb") as handle:
        writer.write(handle)
    data = whole.read_bytes()

    for fraction in (0.4, 0.7, 0.95):
        message = EmailMessage()
        message["Subject"] = "cut short"
        message.set_content("see attached")
        message.add_attachment(data[: int(len(data) * fraction)],
                               maintype="application", subtype="pdf",
                               filename="part.pdf")
        source = tmp_path / ("cut%d.eml" % int(fraction * 100))
        source.write_bytes(message.as_bytes())

        report = pipeline.convert_sources([source], tmp_path / ("run%d" % int(fraction * 100)),
                                          policy=email_items.ExtractionPolicy())
        segments = [s for result in report.sources for s in result.segments]
        attachment = [s for s in segments if s.label.endswith("part.pdf")]
        assert attachment, "the attachment must appear either way"
        segment = attachment[0]
        if segment.ok:
            assert segment.page_count >= 1
        else:
            assert "could not be read" in segment.detail, segment.detail


def test_a_produced_pdf_that_cannot_be_read_is_a_failure(tmp_path, monkeypatch):
    """Zero pages used to look like success: the item contributed nothing."""
    from consolidator import pipeline

    source = tmp_path / "notes.pdf"
    source.write_bytes(b"%PDF-1.4 not really a pdf at all")

    report = pipeline.convert_sources([source], tmp_path / "run",
                                      policy=email_items.ExtractionPolicy())

    segments = [s for result in report.sources for s in result.segments]
    assert segments and not segments[0].ok, \
        "an unreadable result must not be reported as a zero-page success"
    assert "could not be read" in segments[0].detail


def test_the_pipeline_reports_how_many_pages_each_segment_has(tmp_path):
    from pypdf import PdfWriter

    source = tmp_path / "doc.pdf"
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=612, height=792)
    with open(source, "wb") as handle:
        writer.write(handle)

    report = pipeline.convert_sources([source], tmp_path / "run",
                                      policy=email_items.ExtractionPolicy())
    segment = report.sources[0].segments[0]

    assert segment.ok and segment.page_count == 3


def test_text_and_web_attachments_are_documents_not_strangers():
    """A mail carrying notes.txt or a saved page used to contribute nothing."""
    for name, mime in (("notes.txt", "text/plain"),
                       ("readme.md", "text/markdown"),
                       ("data.csv", "text/csv"),
                       ("page.htm", "text/html"),
                       ("page.html", "text/html"),
                       ("page.xhtml", "application/xhtml+xml"),
                       ("memo.docx", "")):
        assert email_items.categorise(name, mime) == "documents", name
    assert email_items.categorise("archive.zip", "application/zip") == "other"


def test_a_text_attachment_is_extracted_not_merely_listed(tmp_path):
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = "notes attached"
    message.set_content("see the file")
    message.add_attachment(b"line one\nline two\n", maintype="text", subtype="plain",
                           filename="notes.txt")

    path = tmp_path / "notes.eml"
    path.write_bytes(message.as_bytes())

    item = email_items.read_message(path)
    assert [a.category for a in item.attachments] == ["documents"]

    segments = email_items.expand_to_segments(
        item, tmp_path / "work", email_items.ExtractionPolicy(body=False)
    )
    assert [s.ok for s in segments] == [True], [s.detail for s in segments]
    assert segments[0].label.endswith("notes.txt")


def test_the_old_office_category_name_still_works_on_the_command_line():
    """'--extract office' keeps working; the category is simply called documents."""
    from consolidator import email_items
    from consolidator.cli import CATEGORY_ALIASES

    assert CATEGORY_ALIASES["office"] == "documents"
    assert "documents" in email_items.CATEGORIES
    assert "office" not in email_items.CATEGORIES
    assert email_items.ExtractionPolicy().documents is True


def test_an_attached_message_named_without_an_extension_is_still_read(tmp_path):
    """A forwarded part called 'forwarded' or 'email.dat' is still mail.

    Reported as "ValueError: not an email file". The staged copy is opened by
    extension, so a message part whose name carries no email extension was handed
    to the reader as a stranger and refused - the attached email never appeared.
    """
    from email.message import EmailMessage

    inner = EmailMessage()
    inner["Subject"] = "Attached thing"
    inner.set_content("the forwarded body")

    outer = EmailMessage()
    outer["Subject"] = "outer"
    outer.set_content("see below")
    outer.add_attachment(inner, filename="forwarded")     # no extension at all
    outer.add_attachment(inner, filename="email.dat")     # the wrong one

    path = tmp_path / "odd.eml"
    path.write_bytes(outer.as_bytes())

    item = email_items.read_message(path)
    assert [a.category for a in item.attachments] == ["embedded", "embedded"]
    assert [a.filename for a in item.attachments] == ["forwarded.eml", "email.eml"]

    segments = email_items.expand_to_segments(
        item, tmp_path / "work",
        email_items.ExtractionPolicy(body=False, embedded=True),
    )
    assert [s.depth for s in segments] == [1, 1], \
        "both forwards should have been expanded: %s" % [
            (s.label, s.detail) for s in segments
        ]
    assert all(s.ok for s in segments)


def test_a_msg_container_is_recognised_whatever_it_is_called(messages, tmp_path):
    """The extension is a first guess, not a requirement: sniff the container."""
    # Pick a sample that actually carries attachments, so the rename is tested
    # against a file with something to lose.
    sample, expected = None, 0
    for candidate in sorted(messages.glob("*.msg")):
        found = len(email_items.read_message(candidate).attachments)
        if found:
            sample, expected = candidate, found
            break
    if sample is None:
        pytest.skip("no .msg sample with attachments")

    renamed = tmp_path / "no-extension-at-all"
    renamed.write_bytes(sample.read_bytes())

    item = email_items.read_message(renamed)

    assert item.subject is not None
    assert len(item.attachments) == expected, \
        "%s lost attachments when renamed" % sample.name


def test_something_that_is_not_mail_is_still_refused(tmp_path):
    """Sniffing must not turn every file into an email."""
    plain = tmp_path / "notes.dat"
    plain.write_bytes(b"just some bytes, no headers and no OLE container\n")

    with pytest.raises(ValueError):
        email_items.read_message(plain)


def test_attachments_survive_several_levels_of_forwarding(tmp_path):
    """A carries B, B carries C, C carries a PDF: three levels, three attachments."""
    depth_pdf = {
        2: _pdf(tmp_path / "at2.pdf", 1),
        1: _pdf(tmp_path / "at1.pdf", 1),
        0: _pdf(tmp_path / "at0.pdf", 1),
    }

    message = message_with_attachment("innermost", "body c", depth_pdf[2].read_bytes(),
                                      "at2.pdf")
    for level in (1, 0):
        outer = message_with_attachment("level %d" % level, "body %d" % level,
                                        depth_pdf[level].read_bytes(),
                                        "at%d.pdf" % level)
        outer.add_attachment(message, filename="fwd%d.eml" % level)
        message = outer

    path = tmp_path / "chain.eml"
    path.write_bytes(message.as_bytes())

    item = email_items.read_message(path)
    segments = email_items.expand_to_segments(
        item, tmp_path / "work",
        email_items.ExtractionPolicy(body=False, embedded=True, max_depth=3),
    )

    wanted = {(0, "at0.pdf"), (1, "at1.pdf"), (2, "at2.pdf")}
    attachments = {}
    for segment in segments:
        for level, name in wanted:
            if segment.label.endswith(name):
                attachments[(level, name)] = segment

    assert set(attachments) == wanted, \
        "every level's attachment should be found once, at its own depth: %s" % sorted(
            (s.depth, s.label) for s in segments
        )

    for (level, name), segment in attachments.items():
        assert segment.ok and segment.pdf, "%s failed: %s" % (name, segment.detail)
        assert segment.pdf.read_bytes() == depth_pdf[level].read_bytes()


@requires_lo
def test_oversized_attachment_is_skipped_not_fatal(emails, tmp_path):
    item = email_items.read_message(emails / "with_pdf.eml")
    policy = email_items.ExtractionPolicy(body=False, max_attachment_bytes=10)

    segments = email_items.expand_to_segments(item, tmp_path, policy)
    assert len(segments) == 1
    assert not segments[0].ok
    assert "over the" in segments[0].detail


@requires_lo
def test_unsupported_attachment_reports_clearly(emails, tmp_path):
    item = email_items.read_message(emails / "unsupported_attachment.eml")
    policy = email_items.ExtractionPolicy(body=False, other=True)

    segments = email_items.expand_to_segments(item, tmp_path, policy)
    failed = [s for s in segments if not s.ok]
    assert failed, "a zip cannot be converted and must be reported"
    assert "unsupported" in failed[0].detail.lower()


# ---------------------------------------------------------------- end to end ---


@requires_lo
def test_email_end_to_end_produces_pages(emails, tmp_path):
    sources = pipeline.gather([str(emails)])
    dest = tmp_path / "emails.pdf"

    report = pipeline.build(
        sources, dest,
        policy=email_items.ExtractionPolicy(pdf=True, images=True, documents=True),
    )

    assert dest.exists()
    assert report.merged.pages > 0
    # Every source should have contributed at least its body.
    assert len(report.succeeded) == len(sources)
    # A PDF attachment and an Office attachment were converted.
    labels = [s.label for s in report.segments]
    assert any("invoice.pdf" in label for label in labels)
    assert any("terms.docx" in label for label in labels)


# ------------------------------------------------------- body completeness ---


def _eml(tmp_path, message) -> Path:
    path = tmp_path / "message.eml"
    path.write_bytes(message.as_bytes())
    return path


def test_a_body_split_around_a_picture_keeps_every_piece(tmp_path):
    """Apple Mail and phones send text, picture, text; the second text is body too."""
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = "split"
    message.make_mixed()
    for html in ("<p>first paragraph</p>", None, "<p>after the picture</p>"):
        part = EmailMessage()
        if html is None:
            part.set_content(b"\x89PNG fake", maintype="image", subtype="png",
                             disposition="inline", filename="photo.png")
        else:
            part.set_content(html, subtype="html")
        message.attach(part)

    item = email_items.read_message(_eml(tmp_path, message))

    assert "first paragraph" in item.body_html
    assert "after the picture" in item.body_html
    assert [a.filename for a in item.attachments] == ["photo.png"]


def test_plain_pieces_between_html_ones_keep_their_place(tmp_path):
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = "mixed pieces"
    message.make_mixed()
    for subtype, content in (("html", "<p>one</p>"), ("plain", "two"),
                             ("html", "<p>three</p>")):
        part = EmailMessage()
        part.set_content(content, subtype=subtype)
        message.attach(part)

    item = email_items.read_message(_eml(tmp_path, message))

    body = item.body_html
    assert body.index("one") < body.index("two") < body.index("three")


def test_an_alternative_body_is_not_repeated(tmp_path):
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = "alt"
    message.set_content("plain version")
    message.add_alternative("<p>html version</p>", subtype="html")

    item = email_items.read_message(_eml(tmp_path, message))

    assert "html version" in item.body_html
    assert "plain version" not in item.body_html
    assert item.body_text.strip() == "plain version"
    assert item.attachments == []


def test_an_unnamed_pdf_part_is_still_an_attachment(tmp_path):
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = "unnamed"
    message.set_content("see attached")
    message.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf")
    del message.get_payload()[1]["Content-Disposition"]

    item = email_items.read_message(_eml(tmp_path, message))

    assert [(a.filename, a.category) for a in item.attachments] == [("part1.pdf", "pdf")]


def test_a_body_in_a_legacy_charset_is_decoded_in_it(tmp_path):
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = "latin"
    message.set_content("Café crème", charset="iso-8859-1", cte="8bit")

    item = email_items.read_message(_eml(tmp_path, message))

    assert "Café crème" in item.body_text
