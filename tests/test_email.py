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
    assert sorted(a.category for a in item.attachments) == ["images", "office", "pdf"]


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
    kinds = [s.kind for s in segments if s.ok]
    assert kinds[0] == "body"
    assert kinds[1:] == ["attachment"] * (len(kinds) - 1)


@requires_lo
def test_policy_excludes_categories(emails, tmp_path):
    item = email_items.read_message(emails / "with_pdf_and_docx.eml")
    policy = email_items.ExtractionPolicy(body=False, pdf=True, images=False, office=False)

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
        policy=email_items.ExtractionPolicy(pdf=True, images=True, office=True),
    )

    assert dest.exists()
    assert report.merged.pages > 0
    # Every source should have contributed at least its body.
    assert len(report.succeeded) == len(sources)
    # A PDF attachment and an Office attachment were converted.
    labels = [s.label for s in report.segments]
    assert any("invoice.pdf" in label for label in labels)
    assert any("terms.docx" in label for label in labels)
