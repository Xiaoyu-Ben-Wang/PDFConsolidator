"""Dialog listing the pages found by the duplicate scan.

Each row is a page that repeats an earlier one. Clicking a row shows that page
in the page list; clicking its "Duplicate of" cell shows the earlier page.
Checked rows can be deleted in one step, and are struck through in the page
list while the dialog is open. Rows at or above CHECK_AT are checked to start
with; lower scores are left for the user to confirm.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

CHECK_AT = 95

PAGE_COLUMN, ORIGINAL_COLUMN, CONFIDENCE_COLUMN, REASON_COLUMN = range(4)


class DuplicatesDialog(QDialog):
    """Show duplicate-scan matches; ``window`` is the MainWindow."""

    def __init__(self, window, matches: list) -> None:
        super().__init__(window)
        self.window_ = window
        self.matches = matches
        self.setWindowTitle("Duplicate pages")
        self.resize(720, 420)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "%d page(s) repeat an earlier page. Click a row to show the page, or its "
            "\"Duplicate of\" cell to show the earlier copy. Pages scoring %d or "
            "more are checked." % (len(matches), CHECK_AT)
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.tree = QTreeWidget()
        self.tree.setRootIsDecorated(False)
        self.tree.setHeaderLabels(["Page", "Duplicate of", "Confidence", "Why"])
        header = self.tree.header()
        for column in (PAGE_COLUMN, ORIGINAL_COLUMN, CONFIDENCE_COLUMN):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(REASON_COLUMN, QHeaderView.Stretch)
        for match in matches:
            item = QTreeWidgetItem([
                window.page_name(match.position),
                window.page_name(match.original),
                "%d%%" % match.confidence,
                match.reason,
            ])
            item.setCheckState(PAGE_COLUMN,
                               Qt.Checked if match.confidence >= CHECK_AT else Qt.Unchecked)
            item.setData(PAGE_COLUMN, Qt.UserRole, match)
            item.setTextAlignment(CONFIDENCE_COLUMN, Qt.AlignRight | Qt.AlignVCenter)
            self.tree.addTopLevelItem(item)
        self.tree.itemClicked.connect(self._on_clicked)
        # Checking a row strikes its page through in the page list, so the
        # pages that would be deleted are visible there too.
        self.tree.itemChanged.connect(self._show_pending_deletion)
        layout.addWidget(self.tree, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self.btn_delete = QPushButton("Delete checked pages")
        self.btn_delete.setToolTip("Remove the checked pages from the page list. "
                                   "The earlier copies are kept.")
        buttons.addButton(self.btn_delete, QDialogButtonBox.ActionRole)
        self.btn_delete.clicked.connect(self.delete_checked)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._show_pending_deletion()

    def _show_pending_deletion(self, *_args) -> None:
        self.window_.mark_for_deletion(self.checked_positions())

    def checked_positions(self) -> list[int]:
        positions = []
        for row in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(row)
            if item.checkState(PAGE_COLUMN) == Qt.Checked:
                positions.append(item.data(PAGE_COLUMN, Qt.UserRole).position)
        return positions

    def _on_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        match = item.data(PAGE_COLUMN, Qt.UserRole)
        self.window_.go_to_page(match.original if column == ORIGINAL_COLUMN
                                else match.position)

    def delete_checked(self) -> None:
        positions = self.checked_positions()
        if positions:
            self.window_.delete_duplicate_pages(positions)
        self.accept()
