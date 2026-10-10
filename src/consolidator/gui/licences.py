"""The About box and the licences window.

The GPL's own advice is that a GUI should offer its notices from an about box,
and the LGPL wants its text reachable rather than merely present on disk. The
About box carries the copyright and the terms; the licences window shows
``THIRD-PARTY-NOTICES.md`` with the full licence texts one click away.

Every document is read from the bundle (or the repository checkout when running
from source), so there is a single copy of each text and no chance of the
interface quoting a licence the build did not ship.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

INTRO = (
    "The components this program is built from, and the licences they are under."
)


def app_root() -> Path:
    """Where the licence documents live: beside the executable, or the checkout."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    # src/consolidator/gui/licences.py -> the repository root.
    return Path(__file__).resolve().parents[3]


def version() -> str:
    from .. import __version__

    return __version__


def documents() -> list[tuple[str, Path]]:
    """(title, path) for every licence document that is actually present."""
    root = app_root()
    candidates = [
        ("Third-party notices", root / "THIRD-PARTY-NOTICES.md"),
        ("GNU General Public License v3.0", root / "LICENSE"),
        ("LibreOffice", root / "libreoffice" / "license.txt"),
    ]
    return [(title, path) for title, path in candidates if path.is_file()]


def about_text() -> str:
    """The About box, kept short: what it is, who wrote it, and the terms.

    The words that matter legally are the licence name and the warranty
    disclaimer; everything else belongs in the licences window, one button away.
    """
    return """<h2>PDF Consolidator {version}</h2>
<p>Turn a mixed folder of documents into one merged PDF.</p>
<p>Copyright (C) 2026 Xiaoyu-Ben-Wang.<br>
Free software under the <b>GNU General Public License, version 3 or later</b>,
and it comes <b>WITHOUT ANY WARRANTY</b> &mdash; without even the implied warranty
of merchantability or fitness for a particular purpose.</p>
<p>Built with Qt (LGPL-3.0) and LibreOffice (MPL-2.0), among others.<br>
<b>Licences&hellip;</b> lists every component beside its text.</p>""".format(
        version=version()
    )


class AboutDialog(QDialog):
    """The about box: what this is, who wrote it, and under what terms.

    The licensing is the point of it, so the terms are on the face of the dialog
    and the full inventory is one button away - which is what the GPL means by
    offering the notices from an about box.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About PDF Consolidator")
        self.resize(620, 460)

        layout = QVBoxLayout(self)
        heading = QTextBrowser()
        heading.setOpenExternalLinks(True)
        heading.setHtml(about_text())
        layout.addWidget(heading, 1)

        found = [str(path) for _title, path in documents()]
        where = QLabel("Licence texts are in %s" % app_root() if found else
                       "The licence texts were not found beside the application.")
        where.setWordWrap(True)
        where.setStyleSheet("QLabel { color: #666; }")
        layout.addWidget(where)

        buttons = QHBoxLayout()
        licences = QPushButton("Licences\u2026")
        licences.clicked.connect(lambda: show_licences(self))
        buttons.addWidget(licences)
        buttons.addStretch(1)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        buttons.addWidget(close)
        layout.addLayout(buttons)


class LicencesDialog(QDialog):
    """A plain viewer: a document, buttons to switch, and a close button."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Licences")
        self.resize(940, 720)

        self._documents = documents()

        layout = QVBoxLayout(self)
        self.heading = QLabel(INTRO)
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)

        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        self.view.setLineWrapMode(QTextBrowser.WidgetWidth)
        layout.addWidget(self.view, 1)

        self.where = QLabel("")
        self.where.setWordWrap(True)
        self.where.setStyleSheet("QLabel { color: #666; }")
        layout.addWidget(self.where)

        buttons = QHBoxLayout()
        for title, path in self._documents:
            button = QPushButton(title)
            button.clicked.connect(
                lambda _checked=False, t=title, p=path: self.show_document(t, p)
            )
            buttons.addWidget(button)
        buttons.addStretch(1)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        if self._documents:
            self.show_document(*self._documents[0])
        else:
            self.view.setPlainText(
                "The licence documents were not found beside the application.\n"
                "They are in the source tree: LICENSE, THIRD-PARTY-NOTICES.md "
                "and LICENSES/."
            )

    def show_document(self, title: str, path: Path) -> None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            text = "Could not read %s:\n%s" % (path, exc)
        if path.suffix.lower() in (".md", ".markdown"):
            self.view.setMarkdown(text)
        else:
            self.view.setPlainText(text)
        self.view.verticalScrollBar().setValue(0)
        # The buttons already name each document; this confirms which is showing.
        self.where.setText("%s \u2014 %s" % (title, path))


def show_licences(parent=None) -> None:
    LicencesDialog(parent).exec()


def show_about(parent=None) -> None:
    AboutDialog(parent).exec()
