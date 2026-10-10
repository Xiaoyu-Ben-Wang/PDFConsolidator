"""Outlook items attached to mail: appointments, contacts, tasks, odd classes.

Real mailboxes attach far more than files. Dragging a meeting, a contact or
another mail onto a message attaches it as an *Outlook item*, and items of a
class extract-msg refuses - a document stored in a mail folder, a recall - used
to make it raise while opening the carrier, which lost the whole message. These
build such messages from scratch (see ``msgbuild``) so no real mail is needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import msgbuild as mb  # noqa: E402
from consolidator import email_items, libreoffice, pipeline  # noqa: E402
from consolidator.model import ATTACHMENT  # noqa: E402

requires_lo = pytest.mark.skipif(
    libreoffice.find_soffice() is None, reason="LibreOffice is not installed"
)


def _pdf_bytes(pages: int = 2) -> bytes:
    import io

    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    page = canvas.Canvas(buffer)
    for number in range(pages):
        page.drawString(72, 720, "page %d" % (number + 1))
        page.showPage()
    page.save()
    return buffer.getvalue()


PDF = _pdf_bytes()


def _write(tmp_path: Path, name: str, item: mb.Item) -> Path:
    path = tmp_path / name
    path.write_bytes(mb.build(item))
    return path


def _names(item: email_items.EmailItem) -> list[str]:
    return [att.filename for att in item.attachments]


# ------------------------------------------------------------------ parsing ---


@pytest.mark.parametrize("message_class", [
    "IPM.Document.Word.Document.12",     # refused by extract-msg
    "IPM.Outlook.Recall",                # refused by extract-msg
    "Custom.Unknown.Thing",              # not recognised at all
])
def test_an_item_of_an_unhandled_class_does_not_lose_the_carrier(tmp_path, message_class):
    odd = mb.Item(message_class, subject="odd item", body="odd body",
                  attachments=[mb.Att("inside.pdf", PDF)])
    path = _write(tmp_path, "carrier.msg", mb.Item(
        subject="Carrier", body="carrier body",
        attachments=[mb.Att(item=odd), mb.Att("after.pdf", PDF)]))

    item = email_items.read_message(path)

    assert item.subject == "Carrier"
    assert item.body_text == "carrier body"
    assert _names(item) == ["odd item.msg", "after.pdf"]
    nested = item.attachments[0].item
    assert nested is not None and nested.body_text == "odd body"
    # ...and the odd item's own attachment is still there, inside it.
    assert [a.filename for a in nested.attachments] == ["inside.pdf"]
    assert nested.attachments[0].data == PDF


def test_an_attachment_without_data_is_listed_with_its_reason(tmp_path):
    path = _write(tmp_path, "nodata.msg", mb.Item(
        subject="Top", body="b",
        attachments=[mb.Att("missing.pdf", None, method=mb.ATTACH_BY_VALUE),
                     mb.Att("kept.pdf", PDF)]))

    item = email_items.read_message(path)

    assert _names(item) == ["missing.pdf", "kept.pdf"]
    assert item.attachments[0].error
    assert not item.attachments[1].error and item.attachments[1].data == PDF


def test_a_cloud_attachment_says_it_is_only_a_link(tmp_path):
    path = _write(tmp_path, "web.msg", mb.Item(
        subject="Top", body="b",
        attachments=[mb.Att("shared.docx", None, method=mb.ATTACH_BY_WEB_REFERENCE)]))

    item = email_items.read_message(path)

    assert _names(item) == ["shared.docx"]
    assert "link" in item.attachments[0].error


@pytest.mark.parametrize("message_class, kind", [
    ("IPM.Appointment", "Appointment"),
    ("IPM.Contact", "Contact"),
    ("IPM.Task", "Task"),
    ("IPM.Schedule.Meeting.Request", "Meeting request"),
    ("IPM.StickyNote", "Note"),
])
def test_attached_outlook_items_are_read_as_what_they_are(tmp_path, message_class, kind):
    inner = mb.Item(message_class, subject="The item", body="what it says",
                    attachments=[mb.Att("its.pdf", PDF)])
    path = _write(tmp_path, "carrier.msg", mb.Item(
        subject="Carrier", body="b", attachments=[mb.Att(item=inner)]))

    item = email_items.read_message(path)

    attachment = item.attachments[0]
    assert attachment.category == "embedded"
    assert attachment.filename == "The item.msg"
    assert attachment.item.kind == kind
    assert attachment.item.body_text == "what it says"
    assert attachment.item.attachments[0].data == PDF
    assert kind in email_items.body_html(attachment.item)


def test_an_item_that_will_not_serialise_is_still_read(tmp_path, monkeypatch):
    """Export-and-reparse was the old route, and its failures lost the item."""
    from extract_msg.msg_classes.msg import MSGFile

    def refuse(self, *args, **kwargs):
        raise ValueError("cannot export this embedded item")

    monkeypatch.setattr(MSGFile, "exportBytes", refuse)
    inner = mb.Item(subject="Inner", body="inner body",
                    attachments=[mb.Att("inner.pdf", PDF)])
    path = _write(tmp_path, "carrier.msg", mb.Item(
        subject="Carrier", body="b", attachments=[mb.Att(item=inner)]))

    item = email_items.read_message(path)

    attachment = item.attachments[0]
    assert attachment.data == b""
    assert not attachment.error
    assert attachment.item.attachments[0].data == PDF


def test_a_windows_1252_html_body_keeps_its_characters(tmp_path):
    html = "<html><head><meta charset='windows-1252'></head><body>Café – “quoted”</body></html>"
    path = _write(tmp_path, "cp1252.msg", mb.Item(
        subject="Top", html=html.encode("cp1252")))

    item = email_items.read_message(path)

    assert "Café – “quoted”" in item.body_html
    assert "�" not in item.body_html


def test_files_attached_by_value_of_every_supported_kind_are_categorised(tmp_path):
    eml = b"From: a@example.com\r\nSubject: attached eml\r\n\r\nbody\r\n"
    inner_msg = mb.build(mb.Item(subject="attached msg file", body="x"))
    path = _write(tmp_path, "files.msg", mb.Item(
        subject="Top", body="b",
        attachments=[mb.Att("note.txt", b"plain text"),
                     mb.Att("page.htm", b"<html><body>web</body></html>"),
                     mb.Att("mail.eml", eml),
                     mb.Att("mail.msg", inner_msg)]))

    item = email_items.read_message(path)

    assert [(a.filename, a.category) for a in item.attachments] == [
        ("note.txt", "documents"), ("page.htm", "documents"),
        ("mail.eml", "embedded"), ("mail.msg", "embedded")]


# ---------------------------------------------------------------- expansion ---


def test_an_unreadable_attachment_becomes_a_failed_row(tmp_path):
    path = _write(tmp_path, "nodata.msg", mb.Item(
        subject="Top", body="b",
        attachments=[mb.Att("missing.pdf", None, method=mb.ATTACH_BY_VALUE),
                     mb.Att("kept.pdf", PDF)]))
    item = email_items.read_message(path)
    policy = email_items.ExtractionPolicy(body=False)

    segments = email_items.expand_to_segments(item, tmp_path / "work", policy)

    rows = [(s.label, s.kind, s.ok) for s in segments]
    assert rows == [("nodata - missing.pdf", ATTACHMENT, False),
                    ("nodata - kept.pdf", ATTACHMENT, True)]
    assert segments[0].detail


@requires_lo
def test_outlook_items_nested_three_deep_keep_every_page(tmp_path):
    deepest = mb.Item("IPM.Appointment", subject="Meeting", body="agenda",
                      attachments=[mb.Att("minutes.pdf", _pdf_bytes(3))])
    middle = mb.Item(subject="Middle", body="middle body",
                     attachments=[mb.Att(item=deepest)])
    path = _write(tmp_path, "top.msg", mb.Item(
        subject="Top", body="top body",
        attachments=[mb.Att(item=middle), mb.Att("top.pdf", PDF),
                     mb.Att(item=mb.Item("IPM.Document.Excel.Sheet.12",
                                         subject="sheet"))]))

    report = pipeline.convert_sources([path], tmp_path / "work")

    rows = [(s.label, s.depth, s.ok, s.page_count) for s in report.sources[0].segments]
    assert rows == [
        ("top - body", 0, True, 1),
        ("Middle - body", 1, True, 1),
        ("Meeting - body", 2, True, 1),
        ("Meeting - minutes.pdf", 2, True, 3),
        ("top - top.pdf", 0, True, 2),
        ("sheet - body", 1, True, 1),
    ]
