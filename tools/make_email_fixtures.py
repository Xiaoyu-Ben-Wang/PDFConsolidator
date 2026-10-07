"""Generate .eml fixtures covering the attachment-expansion cases.

.eml is plain MIME text, so these are built directly and cover exactly the
scenarios that matter: a PDF attachment (the required case), an image, an Office
document, a nested message, an inline signature image, and a message with no
attachments at all.
"""
from __future__ import annotations

import os
import sys
from email.message import EmailMessage
from pathlib import Path

WS = Path(r"C:\Users\benwa\Documents\CS\PDFConslidator")
GEN = WS / "fixtures" / "generated"
OUT = WS / "fixtures" / "email"
OUT.mkdir(parents=True, exist_ok=True)

PDF = GEN / "existing.pdf"
DOCX = GEN / "memo.docx"
PNG = GEN / "diagram.png"
JPG = GEN / "photo.jpg"

for required in (PDF, DOCX, PNG, JPG):
    if not required.exists():
        sys.exit("missing fixture %s - run tools/make_fixtures.py first" % required)


def write(name: str, message: EmailMessage) -> Path:
    path = OUT / name
    path.write_bytes(message.as_bytes())
    print("  %-28s %8d bytes" % (name, path.stat().st_size))
    return path


def base(subject: str, body: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = "Alice Sender <alice@example.com>"
    message["To"] = "Bob Receiver <bob@example.com>"
    message["Cc"] = "carol@example.com"
    message["Subject"] = subject
    message["Date"] = "Mon, 6 Oct 2026 09:15:00 -0700"
    message["Message-ID"] = "<%s@example.com>" % subject.lower().replace(" ", "-")
    message.set_content(body)
    return message


print("writing .eml fixtures to %s" % OUT)

# 1. The required case: a PDF attachment.
m = base("Invoice for March", "Please find the invoice attached.\n\nRegards,\nAlice")
m.add_attachment(PDF.read_bytes(), maintype="application", subtype="pdf", filename="invoice.pdf")
write("with_pdf.eml", m)

# 2. Several attachments of different kinds.
m = base("Contract pack", "Three attachments: a PDF, a spreadsheet-ish doc and a photo.")
m.add_attachment(PDF.read_bytes(), maintype="application", subtype="pdf", filename="contract.pdf")
m.add_attachment(DOCX.read_bytes(),
                 maintype="application",
                 subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
                 filename="terms.docx")
m.add_attachment(JPG.read_bytes(), maintype="image", subtype="jpeg", filename="site.jpg")
write("with_pdf_and_docx.eml", m)

# 3. No attachments at all.
m = base("Just a note", "This message has no attachments.\nNothing to extract here.")
write("no_attachment.eml", m)

# 4. HTML body with an inline signature image referenced by cid.
m = EmailMessage()
m["From"] = "Dana Designer <dana@example.com>"
m["To"] = "bob@example.com"
m["Subject"] = "Design review"
m["Date"] = "Tue, 7 Oct 2026 14:02:00 -0700"
m.set_content("HTML message")
m.add_alternative(
    """<html><body>
    <h2>Design review</h2>
    <p>Please review the <b>attached</b> figures.</p>
    <table border="1" cellpadding="6" cellspacing="0">
      <tr><th align="left">Item</th><th align="right">Amount</th></tr>
      <tr><td>Widgets</td><td align="right">1,200.00</td></tr>
      <tr><td>Gadgets</td><td align="right">340.50</td></tr>
    </table>
    <p>Regards,<br>Dana<br>
    <img src="cid:signature-logo@example.com" width="120"></p>
    </body></html>""",
    subtype="html",
)
# The inline image goes in a related part so it carries the Content-ID the HTML
# refers to, without being promoted to an attachment.
m.get_payload()[1].add_related(
    PNG.read_bytes(), maintype="image", subtype="png",
    cid="<signature-logo@example.com>", filename="logo.png",
)
m.add_attachment(PDF.read_bytes(), maintype="application", subtype="pdf", filename="figures.pdf")
write("html_inline.eml", m)

# 5. Duplicate attachment filenames, to prove unique staging.
m = base("Two invoices", "Both called invoice.pdf on purpose.")
m.add_attachment(PDF.read_bytes(), maintype="application", subtype="pdf", filename="invoice.pdf")
m.add_attachment(PDF.read_bytes(), maintype="application", subtype="pdf", filename="invoice.pdf")
write("duplicate_names.eml", m)

# 6. A nested message (message/rfc822 attachment).
inner = base("Forwarded: original", "This is the original message that was forwarded.")
m = base("Fwd: Forwarded: original", "Forwarding this for your records.")
m.add_attachment(inner, filename="forwarded.eml")
write("nested.eml", m)

# 7. An attachment type with no converter, to prove it is reported not crashed.
m = base("Backup archive", "Contains a zip we cannot render.")
m.add_attachment(b"PK\x03\x04 not really a zip", maintype="application",
                 subtype="zip", filename="backup.zip")
write("unsupported_attachment.eml", m)

print("\n%d .eml fixtures" % len(list(OUT.glob("*.eml"))))
