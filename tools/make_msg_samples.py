"""Write the .msg test samples in tests/data/msg/.

The samples are committed; rerun this only to change them. They are built
with tests/msgbuild.py and contain no real mail.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "data" / "msg"
sys.path.insert(0, str(ROOT / "tests"))

import msgbuild as mb  # noqa: E402


def pdf(text: str, pages: int = 1) -> bytes:
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, invariant=1)
    for number in range(pages):
        page.drawString(72, 720, "%s - page %d" % (text, number + 1))
        page.showPage()
    page.save()
    return buffer.getvalue()


def png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), (40, 120, 200)).save(buffer, "PNG")
    return buffer.getvalue()


SAMPLES = {
    "unicode.msg": mb.Item(
        subject="Ünïcödé — 日本語 — Ελληνικά",
        body="Body with non-ASCII text: café, naïve, 中文, Русский.",
        sender="Zoë Example <zoe@example.com>",
        to="Team <team@example.com>",
        attachments=[mb.Att("résumé.pdf", pdf("resume", 2), mimetype="application/pdf"),
                     mb.Att("chart.png", png(), mimetype="image/png")],
    ),
    "multi-to.msg": mb.Item(
        subject="Several recipients",
        body="Sent to more than one person.",
        sender="Sender <sender@example.com>",
        to="A <a@example.com>; B <b@example.com>; C <c@example.com>",
        attachments=[mb.Att("agenda.pdf", pdf("agenda"), mimetype="application/pdf")],
    ),
    "forwarded.msg": mb.Item(
        subject="Fwd: Invoice",
        body="See the forwarded message.",
        sender="Forwarder <fwd@example.com>",
        to="Me <me@example.com>",
        attachments=[mb.Att(item=mb.Item(
            subject="Invoice",
            body="Invoice attached.",
            sender="Billing <billing@example.com>",
            attachments=[mb.Att("invoice.pdf", pdf("invoice"),
                                mimetype="application/pdf")],
        ))],
    ),
    "no-attachments.msg": mb.Item(
        subject="Just text",
        body="A message with no attachments.",
        sender="Sender <sender@example.com>",
        to="Me <me@example.com>",
    ),
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, item in SAMPLES.items():
        (OUT / name).write_bytes(mb.build(item))
        print("wrote", OUT / name)


if __name__ == "__main__":
    main()
