"""The main application window."""

from __future__ import annotations

import hashlib
from pathlib import Path

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QAction, QBrush, QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import assemble, email_items, pages, pipeline
from .document import DocumentModel
from .thumbs import ThumbnailCache
from .worker import ConversionWorker, MergeWorker, ThumbnailWorker

KIND_LABELS = {"document": "document", "body": "message body", "attachment": "attachment"}

POLICY_EXCLUSION = "excluded by the extraction policy"

# Row height and thumbnail size in the vertical page column.
LIST_THUMB_W = 64
LIST_THUMB_H = 84
ROW_HEIGHT = 92
PREVIEW_HEIGHT = 640


def is_policy_exclusion(segment) -> bool:
    """True when a segment was deliberately skipped rather than failing.

    Attachment categories the user switched off are not failures, and reporting
    them as such would train the user to ignore the dialog.
    """
    return POLICY_EXCLUSION in (segment.detail or "")


def source_colour(key: str) -> QColor:
    """A stable pastel colour per source file.

    Every page originating from the same file shares a colour, which is what
    makes grouping visible at a glance - an email's body and its attachments all
    carry the same tint.
    """
    digest = hashlib.md5(str(key).encode("utf-8", "replace")).digest()
    hue = digest[0] * 359 // 255
    # Light enough that the black label stays readable on top.
    return QColor.fromHsv(hue, 55, 246)


def row_description(node, page, position: int) -> str:
    """One line identifying a page and where it came from."""
    if node is None:
        return "%d.  (unknown)" % position
    # For a plain document the label is the file stem, so showing the full name
    # with its extension is more informative. For an email segment the label
    # already names the message and the part.
    if node.label == node.source.stem:
        origin = node.source.name
    else:
        origin = node.label
    text = "%d.  %s" % (position, origin)
    if page.rotation:
        text += "   [%d°]" % page.rotation
    return text


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PDF Consolidator")
        self.resize(1180, 780)

        self.model = DocumentModel()
        self.thumbs = ThumbnailCache()
        self.selected_sources: list[Path] = []
        self.scratch: Path | None = None

        self._conversion: ConversionWorker | None = None
        self._merge: MergeWorker | None = None
        self._thumbs: ThumbnailWorker | None = None

        self._build_toolbar()
        self._build_body()
        self._build_status()
        self._update_actions()

    # ------------------------------------------------------------------ UI

    def _build_toolbar(self) -> None:
        bar = QToolBar("Main")
        bar.setMovable(False)
        self.addToolBar(bar)

        self.act_add_files = QAction("Add Files…", self)
        self.act_add_files.triggered.connect(self.add_files)
        bar.addAction(self.act_add_files)

        self.act_add_folder = QAction("Add Folder…", self)
        self.act_add_folder.triggered.connect(self.add_folder)
        bar.addAction(self.act_add_folder)

        bar.addSeparator()

        self.act_remove = QAction("Remove", self)
        self.act_remove.triggered.connect(self.remove_selected_sources)
        bar.addAction(self.act_remove)

        self.act_clear = QAction("Clear", self)
        self.act_clear.triggered.connect(self.clear_all)
        bar.addAction(self.act_clear)

        bar.addSeparator()
        bar.addWidget(QLabel("  Sort: "))
        self.cmb_sort = QComboBox()
        self.cmb_sort.addItems(["Name (natural)", "Date modified", "Size", "Type"])
        self.cmb_sort.currentIndexChanged.connect(self.refresh_tree)
        bar.addWidget(self.cmb_sort)

        self.act_convert = QAction("Convert", self)
        self.act_convert.triggered.connect(self.start_conversion)
        bar.addAction(self.act_convert)

    def _build_body(self) -> None:
        splitter = QSplitter(Qt.Horizontal)

        # ---- left: sources ----
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(6, 6, 6, 6)
        left_layout.addWidget(QLabel("Sources"))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Source", "Pages"])
        self.tree.setColumnWidth(0, 280)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.itemChanged.connect(self._on_tree_item_changed)
        left_layout.addWidget(self.tree)
        splitter.addWidget(left)

        # ---- right: pages ----
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(6, 6, 6, 6)

        header = QHBoxLayout()
        header.addWidget(QLabel("Pages"))
        header.addStretch(1)
        for label, slot in (("⟲ Left", lambda: self.rotate_selected(-90)),
                            ("⟳ Right", lambda: self.rotate_selected(90)),
                            ("180°", lambda: self.rotate_selected(180))):
            button = QPushButton(label)
            button.clicked.connect(slot)
            header.addWidget(button)
        for label, slot in (("▲", self.move_selected_up),
                            ("▼", self.move_selected_down),
                            ("Duplicate", self.duplicate_selected),
                            ("Delete", self.delete_selected)):
            button = QPushButton(label)
            button.clicked.connect(slot)
            header.addWidget(button)
        group_button = QPushButton("Group by document")
        group_button.setToolTip(
            "Cluster the pages so every page from one file sits together,\n"
            "keeping each document's own page order."
        )
        group_button.clicked.connect(self.group_by_document)
        header.addWidget(group_button)
        right_layout.addLayout(header)

        # A single vertical column of pages, with a large preview beside it.
        pages_split = QSplitter(Qt.Horizontal)

        self.page_list = QListWidget()
        self.page_list.setViewMode(QListView.ListMode)
        self.page_list.setIconSize(QSize(LIST_THUMB_W, LIST_THUMB_H))
        self.page_list.setUniformItemSizes(True)
        self.page_list.setMovement(QListView.Static)
        self.page_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.page_list.setDefaultDropAction(Qt.MoveAction)
        self.page_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.page_list.setWordWrap(False)
        self.page_list.model().rowsMoved.connect(self._on_pages_reordered)
        self.page_list.itemSelectionChanged.connect(self._on_page_selection_changed)
        pages_split.addWidget(self.page_list)

        preview_panel = QWidget()
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        self.preview = QLabel("Select a page to preview it")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumWidth(280)
        self.preview.setStyleSheet("QLabel { background: #303030; color: #c8c8c8; }")
        preview_layout.addWidget(self.preview, 1)
        self.preview_caption = QLabel("")
        self.preview_caption.setAlignment(Qt.AlignCenter)
        self.preview_caption.setWordWrap(True)
        preview_layout.addWidget(self.preview_caption)
        pages_split.addWidget(preview_panel)

        pages_split.setStretchFactor(0, 3)
        pages_split.setStretchFactor(1, 2)
        pages_split.setSizes([560, 340])
        right_layout.addWidget(pages_split, 1)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([420, 760])

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter, 1)
        layout.addWidget(self._build_options())
        self.setCentralWidget(central)

    def _build_options(self) -> QWidget:
        box = QGroupBox("Options")
        outer = QHBoxLayout(box)

        # Email extraction checklist.
        email_box = QGroupBox("Extract from email items")
        email_layout = QVBoxLayout(email_box)
        self.chk_body = QCheckBox("Message body")
        self.chk_pdf = QCheckBox("PDF documents")
        self.chk_images = QCheckBox("Images (excludes inline)")
        self.chk_office = QCheckBox("Office documents")
        self.chk_embedded = QCheckBox("Embedded messages")
        self.chk_other = QCheckBox("Other attachments (listed only)")
        for widget, on in ((self.chk_body, True), (self.chk_pdf, True),
                           (self.chk_images, True), (self.chk_office, True),
                           (self.chk_embedded, False), (self.chk_other, False)):
            widget.setChecked(on)
            email_layout.addWidget(widget)
        outer.addWidget(email_box)

        # Layout.
        layout_box = QGroupBox("Layout")
        grid = QVBoxLayout(layout_box)
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Page size:"))
        self.cmb_page_size = QComboBox()
        self.cmb_page_size.addItems([name for name in ("letter", "a4", "legal", "original")])
        self.cmb_page_size.setCurrentText(pages.DEFAULT_PAGE_SIZE)
        row1.addWidget(self.cmb_page_size)
        row1.addWidget(QLabel("Numbers:"))
        self.cmb_numbering = QComboBox()
        self.cmb_numbering.addItems(["none", "continuous", "per-document"])
        row1.addWidget(self.cmb_numbering)
        row1.addWidget(QLabel("Separator:"))
        self.cmb_separator = QComboBox()
        self.cmb_separator.addItems(["none", "blank", "title"])
        row1.addWidget(self.cmb_separator)
        grid.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Footer:"))
        self.edt_footer = QLineEdit("{doc}")
        self.edt_footer.setPlaceholderText("e.g. {doc} - page {n} of {total}")
        row2.addWidget(self.edt_footer, 1)
        self.chk_bookmarks = QCheckBox("Bookmarks")
        self.chk_bookmarks.setChecked(True)
        row2.addWidget(self.chk_bookmarks)
        grid.addLayout(row2)
        outer.addWidget(layout_box, 1)

        # Output.
        out_box = QGroupBox("Output")
        out_layout = QVBoxLayout(out_box)
        row3 = QHBoxLayout()
        self.edt_output = QLineEdit()
        self.edt_output.setPlaceholderText("destination PDF…")
        row3.addWidget(self.edt_output, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.choose_output)
        row3.addWidget(browse)
        out_layout.addLayout(row3)

        row4 = QHBoxLayout()
        row4.addStretch(1)
        self.btn_create = QPushButton("Create PDF")
        self.btn_create.setDefault(True)
        self.btn_create.clicked.connect(self.create_pdf)
        row4.addWidget(self.btn_create)
        out_layout.addLayout(row4)
        outer.addWidget(out_box)

        return box

    def _build_status(self) -> None:
        self.status = self.statusBar()
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.setRange(0, 0)      # indeterminate until work starts
        self.progress.setVisible(False)
        self.status.addPermanentWidget(self.progress)
        self.status.showMessage("Add files or a folder to begin.")

    # -------------------------------------------------------------- sources

    def add_files(self) -> None:
        # Native Windows dialog: no DontUseNativeDialog, so Quick Access,
        # mapped drives and shell extensions all work.
        names, _ = QFileDialog.getOpenFileNames(
            self, "Add files", "",
            "All supported (*.pdf *.docx *.doc *.rtf *.odt *.xlsx *.xls *.ods "
            "*.pptx *.ppt *.odp *.msg *.eml *.jpg *.jpeg *.png *.tif *.tiff "
            "*.bmp *.gif *.webp *.txt *.md *.log *.csv);;All files (*)",
        )
        self._add([Path(n) for n in names])

    def add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Add folder")
        if not folder:
            return
        found = pipeline.gather([folder])
        if not found:
            QMessageBox.information(self, "Empty folder", "No files found in that folder.")
            return
        self._add(found)

    def _add(self, paths: list[Path]) -> None:
        known = {str(p) for p in self.selected_sources}
        added = 0
        for path in paths:
            if str(path) not in known:
                self.selected_sources.append(Path(path))
                known.add(str(path))
                added += 1
        self.status.showMessage("Added %d file(s). %d selected."
                                % (added, len(self.selected_sources)))
        self.refresh_tree()
        self._update_actions()

    def remove_selected_sources(self) -> None:
        chosen = {self.tree.indexOfTopLevelItem(item) for item in self.tree.selectedItems()}
        # Removing a child removes its whole source, which is the least
        # surprising behaviour.
        top = set()
        for item in self.tree.selectedItems():
            root = item
            while root.parent() is not None:
                root = root.parent()
            top.add(root.text(0))
        if not top:
            return
        self.selected_sources = [p for p in self.selected_sources if p.name not in top]
        self.refresh_tree()
        self._update_actions()

    def clear_all(self) -> None:
        self.selected_sources.clear()
        self.model.clear()
        self.thumbs.clear()
        if self.scratch and self.scratch.exists():
            from .. import libreoffice

            libreoffice._remove_tree(self.scratch)
        self.scratch = None
        self.refresh_tree()
        self.refresh_pages()
        self._update_actions()
        self.status.showMessage("Cleared.")

    # ----------------------------------------------------------------- tree

    def _sort_key(self, path: Path):
        from ..pipeline import _natural_key

        mode = self.cmb_sort.currentText()
        if mode == "Date modified":
            try:
                return (0, path.stat().st_mtime)
            except OSError:
                return (0, 0)
        if mode == "Size":
            try:
                return (0, path.stat().st_size)
            except OSError:
                return (0, 0)
        if mode == "Type":
            return (0, path.suffix.lower(), _natural_key(path))
        return (0, _natural_key(path))

    def refresh_tree(self) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()

        by_source: dict[str, list] = {}
        for segment in self.model.segments:
            by_source.setdefault(str(segment.source), []).append(segment)

        ordered = sorted(self.selected_sources, key=self._sort_key)
        for path in ordered:
            root = QTreeWidgetItem(self.tree, [path.name, ""])
            root.setToolTip(0, str(path))
            root.setFlags(root.flags() | Qt.ItemIsUserCheckable)
            root.setCheckState(0, Qt.Checked)

            segments = by_source.pop(str(path), [])
            for segment in segments:
                child = QTreeWidgetItem(root, ["  " + segment.label,
                                               str(segment.page_count) if segment.ok else "—"])
                child.setToolTip(0, segment.detail or segment.label)
                if segment.ok:
                    child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                    child.setCheckState(0, Qt.Checked if segment.enabled else Qt.Unchecked)
                    child.setData(0, Qt.UserRole, segment.id)
                else:
                    child.setForeground(0, Qt.gray)
                    child.setText(0, "  %s (%s)" % (segment.label, segment.detail or "failed"))
            root.setExpanded(True)

        # Segments whose source is no longer in the selection still need showing.
        for source, segments in by_source.items():
            root = QTreeWidgetItem(self.tree, [Path(source).name, ""])
            root.setForeground(0, Qt.gray)
            for segment in segments:
                QTreeWidgetItem(root, ["  " + segment.label, str(segment.page_count)])
            root.setExpanded(True)

        self.tree.blockSignals(False)

    def _on_tree_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        segment_id = item.data(0, Qt.UserRole)
        if segment_id is None:
            return
        self.model.set_segment_enabled(segment_id, item.checkState(0) == Qt.Checked)
        self.refresh_pages()

    # ---------------------------------------------------------------- pages

    def refresh_pages(self) -> None:
        self.page_list.blockSignals(True)
        self.page_list.clear()
        # Repopulating leaves the first row current, and in some styles current
        # also means selected - which would silently act on a page the user never
        # chose. Selection is always explicit.
        self.page_list.clearSelection()

        for position, page in enumerate(self.model.pages, start=1):
            node = self.model.segment(page.segment_id)
            tint = source_colour(str(node.source) if node else "?")
            item = QListWidgetItem(row_description(node, page, position))
            item.setData(Qt.UserRole, (page.segment_id, page.index, page.rotation))
            item.setToolTip("%s\npage %d of %d"
                            % (node.label if node else "?",
                               page.index + 1,
                               node.page_count if node else 0))
            # The row tint identifies the originating file, so pages from one
            # document are obvious as a block even before they are grouped.
            item.setBackground(QBrush(tint))
            item.setForeground(QBrush(QColor("#202020")))
            item.setSizeHint(QSize(0, ROW_HEIGHT))
            self.page_list.addItem(item)

        self.page_list.blockSignals(False)
        self._start_thumbnails()
        self._update_preview()
        self.status.showMessage(self.model.summary())

    def group_by_document(self) -> None:
        """Bring every page from the same file together, keeping its own order."""
        if not self.model.pages:
            return
        self.model.group_by_source()
        self.refresh_pages()
        self.status.showMessage(
            "Grouped %d page(s) by source document." % self.model.page_count
        )

    def _start_thumbnails(self) -> None:
        if self._thumbs is not None and self._thumbs.isRunning():
            self._thumbs.cancel()
            self._thumbs.wait(2000)

        jobs = []
        for position, page in enumerate(self.model.pages):
            node = self.model.segment(page.segment_id)
            if node and node.pdf:
                jobs.append((position, node.pdf, page.index))
        if not jobs:
            return

        self._thumbs = ThumbnailWorker(jobs, self.thumbs, self)
        self._thumbs.ready.connect(self._on_thumbnail)
        self._thumbs.start()

    def _on_thumbnail(self, position: int, image) -> None:
        if 0 <= position < self.page_list.count():
            item = self.page_list.item(position)
            item.setIcon(QIcon(QPixmap.fromImage(image)))

    # -------------------------------------------------------------- preview

    def _on_page_selection_changed(self) -> None:
        self._update_preview()

    def _update_preview(self) -> None:
        """Show a large render of the selected page, or clear the pane."""
        indices = self.selected_page_indices()
        if not indices:
            self.preview.setPixmap(QPixmap())
            self.preview.setText("Select a page to preview it")
            self.preview_caption.setText("")
            return

        position = indices[0]
        page = self.model.pages[position]
        node = self.model.segment(page.segment_id)

        if node is None or not node.pdf:
            self.preview.setText("Preview unavailable")
            self.preview_caption.setText("")
            return

        image = self.thumbs.render(node.pdf, page.index, PREVIEW_HEIGHT)
        if image is None:
            self.preview.setText("Preview unavailable")
        else:
            self.preview.setText("")
            self.preview.setPixmap(QPixmap.fromImage(image))

        extra = ""
        if len(indices) > 1:
            extra = "   (+%d more selected)" % (len(indices) - 1)
        rotation = "   rotated %d°" % page.rotation if page.rotation else ""
        self.preview_caption.setText(
            "Page %d of %d\n%s%s%s"
            % (position + 1, self.model.page_count, node.label, rotation, extra)
        )

    def selected_page_indices(self) -> list[int]:
        return sorted(self.page_list.row(item) for item in self.page_list.selectedItems())

    def _on_pages_reordered(self, *_args) -> None:
        """Adopt the order the user dragged the grid into."""
        reordered = []
        for row in range(self.page_list.count()):
            data = self.page_list.item(row).data(Qt.UserRole)
            if data is None:
                continue
            segment_id, index, rotation = data
            from .document import PageRef

            reordered.append(PageRef(segment_id, index, rotation))
        if len(reordered) == len(self.model.pages):
            self.model.pages = reordered

    def rotate_selected(self, degrees: int) -> None:
        indices = self.selected_page_indices()
        if not indices:
            return
        self.model.rotate(indices, degrees)
        self._renumber_pages()
        self.refresh_pages_preserving_selection(indices)

    def delete_selected(self) -> None:
        indices = self.selected_page_indices()
        if not indices:
            return
        self.model.delete(indices)
        self.refresh_pages()

    def duplicate_selected(self) -> None:
        indices = self.selected_page_indices()
        if not indices:
            return
        self.model.duplicate(indices)
        self.refresh_pages()

    def move_selected_up(self) -> None:
        indices = self.selected_page_indices()
        if not indices:
            return
        self.model.move_up(indices)
        self.refresh_pages_preserving_selection([max(0, i - 1) for i in indices])

    def move_selected_down(self) -> None:
        indices = self.selected_page_indices()
        if not indices:
            return
        self.model.move_down(indices)
        self.refresh_pages_preserving_selection(
            [min(self.model.page_count - 1, i + 1) for i in indices]
        )

    def refresh_pages_preserving_selection(self, indices: list[int]) -> None:
        self.refresh_pages()
        self.page_list.clearSelection()
        for i in indices:
            if 0 <= i < self.page_list.count():
                self.page_list.item(i).setSelected(True)

    def _renumber_pages(self) -> None:
        for position, page in enumerate(self.model.pages, start=1):
            item = self.page_list.item(position - 1)
            if item is None:
                continue
            node = self.model.segment(page.segment_id)
            item.setText(row_description(node, page, position))
            item.setData(Qt.UserRole, (page.segment_id, page.index, page.rotation))

    # ----------------------------------------------------------- conversion

    def start_conversion(self) -> None:
        if not self.selected_sources:
            QMessageBox.information(self, "Nothing to convert", "Add some files first.")
            return
        if self._conversion is not None and self._conversion.isRunning():
            return

        from .. import libreoffice

        if self.scratch and self.scratch.exists():
            libreoffice._remove_tree(self.scratch)
        root = pipeline.choose_scratch()
        import tempfile

        self.scratch = Path(tempfile.mkdtemp(prefix="gui_", dir=str(root)))

        self.model.clear()
        self.thumbs.clear()
        self.refresh_pages()

        policy = self._policy()
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.act_convert.setEnabled(False)

        self._conversion = ConversionWorker(
            self.selected_sources, self.scratch, policy, self
        )
        self._conversion.progressed.connect(self.status.showMessage)
        self._conversion.completed.connect(self._on_converted)
        self._conversion.crashed.connect(self._on_worker_crash)
        self._conversion.start()

    def _policy(self) -> email_items.ExtractionPolicy:
        return email_items.ExtractionPolicy(
            body=self.chk_body.isChecked(),
            pdf=self.chk_pdf.isChecked(),
            images=self.chk_images.isChecked(),
            office=self.chk_office.isChecked(),
            embedded=self.chk_embedded.isChecked(),
            other=self.chk_other.isChecked(),
        )

    def _on_converted(self, report) -> None:
        self.progress.setVisible(False)
        self.act_convert.setEnabled(True)

        for result in report.sources:
            for segment in result.segments:
                self.model.add_segment(
                    result.source,
                    segment.label,
                    segment.pdf,
                    segment.ok,
                    kind=segment.kind,
                    detail=segment.detail,
                    page_count=pipeline.count_pages(segment.pdf) if segment.ok and segment.pdf else 0,
                )

        # Segments produced for sources not in the visible list still belong.
        for source_result in report.sources:
            if source_result.source not in self.selected_sources:
                self.selected_sources.append(source_result.source)

        self.refresh_tree()
        self.refresh_pages()
        self._update_actions()

        failures = [s for s in report.all_segments if not s.ok and not is_policy_exclusion(s)]
        excluded = [s for s in report.all_segments if not s.ok and is_policy_exclusion(s)]
        if failures:
            self._report_failures(failures)
        else:
            message = ("Converted %d segment(s), %d page(s)."
                       % (len(report.segments), self.model.page_count))
            if excluded:
                message += " %d item(s) excluded by the extraction settings." % len(excluded)
            self.status.showMessage(message)

    def _report_failures(self, failures: list) -> None:
        lines = []
        for segment in failures[:14]:
            lines.append("%s\n    %s" % (segment.display, segment.detail or "failed"))
        if len(failures) > 14:
            lines.append("… and %d more" % (len(failures) - 14))

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Some items could not be converted")
        box.setText("%d item(s) failed. The rest were converted." % len(failures))
        box.setDetailedText("\n\n".join(lines))
        retry = box.addButton("Retry failed", QMessageBox.AcceptRole)
        box.addButton("Continue", QMessageBox.RejectRole)
        box.exec()

        if box.clickedButton() is retry:
            self.status.showMessage("Retrying failed items is not implemented yet.")

    def _on_worker_crash(self, detail: str) -> None:
        self.progress.setVisible(False)
        self.act_convert.setEnabled(True)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle("Conversion failed")
        box.setText("Something went wrong while converting.")
        box.setDetailedText(detail)
        box.exec()

    # -------------------------------------------------------------- output

    def choose_output(self) -> None:
        name, _ = QFileDialog.getSaveFileName(self, "Save merged PDF", "", "PDF (*.pdf)")
        if name:
            self.edt_output.setText(name)

    def create_pdf(self) -> None:
        if not self.model.pages:
            QMessageBox.information(self, "Nothing to assemble",
                                    "Convert some files first.")
            return
        dest = self.edt_output.text().strip()
        if not dest:
            self.choose_output()
            dest = self.edt_output.text().strip()
            if not dest:
                return
        if not dest.lower().endswith(".pdf"):
            dest += ".pdf"

        entries = self.model.to_entries()
        if not entries:
            QMessageBox.information(self, "Nothing to assemble", "No usable pages.")
            return

        options = self._merge_options(Path(dest))
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.btn_create.setEnabled(False)

        self._merge = MergeWorker(entries, Path(dest), options, self)
        self._merge.completed.connect(self._on_merged)
        self._merge.crashed.connect(self._on_merge_crash)
        self._merge.start()

    def _merge_options(self, dest: Path) -> assemble.MergeOptions:
        size = pages.resolve(self.cmb_page_size.currentText())
        numbering = assemble.PageNumbering(
            mode=self.cmb_numbering.currentText(),
            position="bottom-center",
            template="{n} of {total}",
        )
        footer_text = self.edt_footer.text().strip()
        return assemble.MergeOptions(
            page_size=size,
            margin=36.0,
            bookmarks=self.chk_bookmarks.isChecked(),
            separator=self.cmb_separator.currentText(),
            numbering=numbering,
            footer=assemble.HeaderFooter(center=footer_text) if footer_text else assemble.HeaderFooter(),
            title=dest.stem,
        )

    def _on_merged(self, report) -> None:
        self.progress.setVisible(False)
        self.btn_create.setEnabled(True)
        message = "Wrote %d page(s)" % report.pages
        if report.separators:
            message += " including %d separator(s)" % report.separators
        self.status.showMessage(message + ".")
        QMessageBox.information(self, "Done", "%s.\n\n%s" % (message, self.edt_output.text()))

    def _on_merge_crash(self, detail: str) -> None:
        self.progress.setVisible(False)
        self.btn_create.setEnabled(True)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle("Could not write the PDF")
        box.setText("Assembly failed.")
        box.setDetailedText(detail)
        box.exec()

    # -------------------------------------------------------------- helpers

    def _update_actions(self) -> None:
        has_sources = bool(self.selected_sources)
        self.act_remove.setEnabled(has_sources)
        self.act_clear.setEnabled(has_sources)
        self.act_convert.setEnabled(has_sources)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        for worker in (self._conversion, self._merge, self._thumbs):
            if worker is not None and worker.isRunning():
                if hasattr(worker, "cancel"):
                    worker.cancel()
                worker.wait(3000)
        if self.scratch and self.scratch.exists():
            from .. import libreoffice

            libreoffice._remove_tree(self.scratch)
        super().closeEvent(event)
