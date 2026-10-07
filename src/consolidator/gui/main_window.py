"""The main application window."""

from __future__ import annotations

import hashlib
from pathlib import Path

from PySide6.QtCore import QFileInfo, Qt, QSize
from PySide6.QtGui import QAction, QBrush, QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFileIconProvider,
    QGridLayout,
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

from .. import assemble, convert, email_items, pages, pipeline
from .document import DocumentModel
from .thumbs import ThumbnailCache
from .worker import ConversionWorker, MergeWorker, ThumbnailWorker

POLICY_EXCLUSION = "excluded by the extraction policy"

# Row metrics. The page column is deliberately compact so more pages fit.
LIST_THUMB_W = 64
LIST_THUMB_H = 84
ROW_HEIGHT = 92
PREVIEW_HEIGHT = 640
TREE_ROW_HEIGHT = 20
TREE_ICON = 16

# How each kind of segment is described in a row.
KIND_SUFFIX = {"body": "body", "attachment": "attachment", "document": ""}


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
    return QColor.fromHsv(hue, 55, 246)


def part_name(node) -> str:
    """The part of a segment, without the repeated source stem.

    ``with_pdf.eml`` plus the label ``with_pdf - invoice.pdf`` yields
    ``invoice.pdf``, which is what the row should show.
    """
    label = node.label or ""
    prefix = node.source.stem + " - "
    if label.startswith(prefix):
        return label[len(prefix):]
    return label


def row_description(node, page, position: int) -> str:
    """One line naming a page and the file it came from.

    The original filename always leads, so the column sorts and scans by source,
    with the particular part of an email appended after an arrow.
    """
    if node is None:
        return "%d.  (unknown)" % position

    text = "%d.  %s" % (position, node.source.name)
    if node.kind != "document":
        text += "  \u2192  %s" % part_name(node)
    if page.rotation:
        text += "   [%d\u00b0]" % page.rotation
    return text


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PDF Consolidator")
        self.resize(1240, 820)

        self.model = DocumentModel()
        self.thumbs = ThumbnailCache()
        self.icon_provider = QFileIconProvider()
        self.selected_sources: list[Path] = []
        self.scratch: Path | None = None
        self.output_path: Path | None = None

        # Extraction settings. The checkboxes edit the default policy, or the
        # policy of the email currently selected in the tree.
        self.default_policy = email_items.ExtractionPolicy()
        self.source_policies: dict[str, email_items.ExtractionPolicy] = {}
        self._loading_policy = False

        self._conversion: ConversionWorker | None = None
        self._merge: MergeWorker | None = None
        self._thumbs: ThumbnailWorker | None = None

        self._build_toolbar()
        self._build_body()
        self._build_options()
        self._build_status()
        self._load_policy_into_widgets(self.default_policy)
        self._update_actions()

    # ------------------------------------------------------------------ UI

    def _build_toolbar(self) -> None:
        bar = QToolBar("Main")
        bar.setMovable(False)
        self.addToolBar(bar)

        self.act_add_files = QAction("Add Files\u2026", self)
        self.act_add_files.triggered.connect(self.add_files)
        bar.addAction(self.act_add_files)

        self.act_add_folder = QAction("Add Folder\u2026", self)
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
        self.tree.setColumnWidth(0, 250)
        self.tree.setIconSize(QSize(TREE_ICON, TREE_ICON))
        self.tree.setIndentation(14)
        self.tree.setRootIsDecorated(True)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        # Compact rows: the default padding wastes a lot of vertical space.
        self.tree.setStyleSheet(
            "QTreeWidget::item { height: %dpx; padding: 0px; }" % TREE_ROW_HEIGHT
        )
        self.tree.itemChanged.connect(self._on_tree_item_changed)
        self.tree.itemSelectionChanged.connect(self._on_tree_selection_changed)
        left_layout.addWidget(self.tree)
        splitter.addWidget(left)

        # ---- right: pages ----
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(6, 6, 6, 6)

        header = QHBoxLayout()
        header.addWidget(QLabel("Pages"))
        header.addStretch(1)
        for label, slot in (("\u21ba Left", lambda: self.rotate_selected(-90)),
                            ("\u21b3 Right", lambda: self.rotate_selected(90)),
                            ("180\u00b0", lambda: self.rotate_selected(180))):
            button = QPushButton(label)
            button.clicked.connect(slot)
            header.addWidget(button)
        for label, slot in (("\u25b2", self.move_selected_up),
                            ("\u25bc", self.move_selected_down),
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
        self.preview.setMinimumWidth(260)
        self.preview.setStyleSheet("QLabel { background: #303030; color: #c8c8c8; }")
        preview_layout.addWidget(self.preview, 1)
        self.preview_caption = QLabel("")
        self.preview_caption.setAlignment(Qt.AlignCenter)
        self.preview_caption.setWordWrap(True)
        preview_layout.addWidget(self.preview_caption)
        pages_split.addWidget(preview_panel)

        pages_split.setStretchFactor(0, 3)
        pages_split.setStretchFactor(1, 2)
        pages_split.setSizes([600, 340])
        right_layout.addWidget(pages_split, 1)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([430, 810])

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter, 1)
        self.setCentralWidget(central)

    def _build_options(self) -> None:
        central = self.centralWidget()
        central.layout().addWidget(self._options_widget())

    def _options_widget(self) -> QWidget:
        box = QGroupBox("Options")
        outer = QHBoxLayout(box)
        outer.setSpacing(10)

        # ---------------- email extraction ----------------
        email_box = QGroupBox()
        email_layout = QVBoxLayout(email_box)
        email_layout.setSpacing(4)

        self.lbl_policy_scope = QLabel()
        self.lbl_policy_scope.setWordWrap(True)
        email_layout.addWidget(self.lbl_policy_scope)

        self.policy_checks: dict[str, QCheckBox] = {}
        for key, label in (
            ("body", "Message body"),
            ("pdf", "PDF documents"),
            ("images", "Images (excludes inline)"),
            ("office", "Office documents"),
            ("embedded", "Embedded messages"),
            ("other", "Other attachments (listed only)"),
        ):
            check = QCheckBox(label)
            check.stateChanged.connect(self._on_policy_toggled)
            email_layout.addWidget(check)
            self.policy_checks[key] = check

        apply_all = QPushButton("Apply to all emails")
        apply_all.setToolTip("Give every Outlook item and email these settings")
        apply_all.clicked.connect(self.apply_policy_to_all_emails)
        email_layout.addWidget(apply_all)
        outer.addWidget(email_box, 2)

        # ---------------- layout ----------------
        layout_box = QGroupBox("Layout")
        grid = QGridLayout(layout_box)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)

        grid.addWidget(QLabel("Page size:"), 0, 0)
        self.cmb_page_size = QComboBox()
        self.cmb_page_size.addItems(["letter", "a4", "legal", "original"])
        self.cmb_page_size.setCurrentText(pages.DEFAULT_PAGE_SIZE)
        grid.addWidget(self.cmb_page_size, 0, 1)

        grid.addWidget(QLabel("Numbers:"), 0, 2)
        self.cmb_numbering = QComboBox()
        self.cmb_numbering.addItems(["none", "continuous", "per-document"])
        grid.addWidget(self.cmb_numbering, 0, 3)

        grid.addWidget(QLabel("Separator:"), 0, 4)
        self.cmb_separator = QComboBox()
        self.cmb_separator.addItems(["none", "blank", "title"])
        grid.addWidget(self.cmb_separator, 0, 5)

        grid.addWidget(QLabel("Footer:"), 1, 0)
        self.edt_footer = QLineEdit("{doc}")
        self.edt_footer.setPlaceholderText("{doc}  {n}  {total}  {date}")
        grid.addWidget(self.edt_footer, 1, 1, 1, 4)

        self.chk_bookmarks = QCheckBox("Bookmarks")
        self.chk_bookmarks.setChecked(True)
        grid.addWidget(self.chk_bookmarks, 1, 5)

        for column in (1, 3, 5):
            grid.setColumnStretch(column, 1)
        outer.addWidget(layout_box, 3)

        # ---------------- output ----------------
        out_box = QGroupBox("Output")
        out_layout = QVBoxLayout(out_box)

        self.btn_output = QPushButton("Save as\u2026")
        self.btn_output.setToolTip("Choose where to write the merged PDF")
        self.btn_output.clicked.connect(self.choose_output)
        out_layout.addWidget(self.btn_output)

        self.lbl_output = QLabel("No destination chosen")
        self.lbl_output.setWordWrap(True)
        self.lbl_output.setStyleSheet("QLabel { color: #555; }")
        out_layout.addWidget(self.lbl_output)

        out_layout.addStretch(1)
        self.btn_create = QPushButton("Create PDF")
        self.btn_create.clicked.connect(self.create_pdf)
        out_layout.addWidget(self.btn_create)
        outer.addWidget(out_box, 2)

        return box

    def _build_status(self) -> None:
        self.status = self.statusBar()
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        self.status.addPermanentWidget(self.progress)
        self.status.showMessage("Add files or a folder to begin.")

    # ------------------------------------------------- extraction policies

    def _load_policy_into_widgets(self, policy: email_items.ExtractionPolicy) -> None:
        self._loading_policy = True
        for key, check in self.policy_checks.items():
            check.setChecked(bool(getattr(policy, key, False)))
        self._loading_policy = False

    def _policy_from_widgets(self) -> email_items.ExtractionPolicy:
        return email_items.ExtractionPolicy(
            **{key: check.isChecked() for key, check in self.policy_checks.items()}
        )

    def selected_email(self) -> Path | None:
        """The email source currently selected in the tree, if any."""
        items = self.tree.selectedItems()
        if not items:
            return None
        item = items[0]
        root = item
        while root.parent() is not None:
            root = root.parent()
        path = root.data(0, Qt.UserRole)
        if path is None:
            return None
        candidate = Path(path)
        return candidate if candidate.suffix.lower() in convert.EMAIL_EXTS else None

    def policy_for(self, source: Path) -> email_items.ExtractionPolicy:
        """Per-source extraction policy, falling back to the default."""
        override = self.source_policies.get(str(source))
        return override or self.default_policy

    def _on_tree_selection_changed(self) -> None:
        email = self.selected_email()
        if email is None:
            self.lbl_policy_scope.setText(
                "<b>Extract from email items</b><br>"
                "<span style='color:#666'>Default for newly added emails.</span>"
            )
            self._load_policy_into_widgets(self.default_policy)
        else:
            self.lbl_policy_scope.setText(
                "<b>Extract from email items</b><br>"
                "<span style='color:#666'>Editing: <b>%s</b></span>" % email.name
            )
            self._load_policy_into_widgets(self.policy_for(email))

    def _on_policy_toggled(self, *_args) -> None:
        if self._loading_policy:
            return
        policy = self._policy_from_widgets()
        email = self.selected_email()
        if email is None:
            self.default_policy = policy
        else:
            self.source_policies[str(email)] = policy
            self.status.showMessage("Extraction updated for %s." % email.name)

    def apply_policy_to_all_emails(self) -> None:
        policy = self._policy_from_widgets()
        self.default_policy = policy
        self.source_policies.clear()
        self.status.showMessage("Extraction settings applied to all emails.")

    # -------------------------------------------------------------- sources

    def file_icon(self, path: Path) -> QIcon:
        """The real Windows icon for this file type, via the shell."""
        try:
            return self.icon_provider.icon(QFileInfo(str(path)))
        except Exception:
            return QIcon()

    def add_files(self) -> None:
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
        items = self.tree.selectedItems()
        if not items:
            return
        roots = []
        for item in items:
            root = item
            while root.parent() is not None:
                root = root.parent()
            roots.append(root)
        doomed = {root.data(0, Qt.UserRole) for root in roots if root.data(0, Qt.UserRole)}

        self.selected_sources = [p for p in self.selected_sources if str(p) not in doomed]
        self.refresh_tree()
        self._update_actions()

    def clear_all(self) -> None:
        self.selected_sources.clear()
        self.model.clear()
        self.thumbs.clear()
        self.source_policies.clear()
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

        for path in sorted(self.selected_sources, key=self._sort_key):
            root = QTreeWidgetItem(self.tree, [path.name, ""])
            root.setToolTip(0, str(path))
            root.setIcon(0, self.file_icon(path))
            root.setData(0, Qt.UserRole, str(path))
            root.setFlags(root.flags() | Qt.ItemIsUserCheckable)
            root.setCheckState(0, Qt.Checked)

            for segment in by_source.pop(str(path), []):
                if segment.ok:
                    label = "  " + part_name(segment)
                    child = QTreeWidgetItem(root, [label, str(segment.page_count)])
                    child.setToolTip(0, segment.label)
                    child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                    child.setCheckState(0, Qt.Checked if segment.enabled else Qt.Unchecked)
                    child.setData(0, Qt.UserRole, segment.id)
                else:
                    child = QTreeWidgetItem(
                        root, ["  %s (%s)" % (part_name(segment), segment.detail or "failed"), "\u2014"]
                    )
                    child.setForeground(0, Qt.gray)
                    child.setToolTip(0, segment.detail)
            root.setExpanded(True)

        # Segments left over (their source was removed) still need showing.
        for source, segments in by_source.items():
            root = QTreeWidgetItem(self.tree, [Path(source).name, ""])
            root.setForeground(0, Qt.gray)
            for segment in segments:
                QTreeWidgetItem(root, ["  " + part_name(segment), str(segment.page_count)])
            root.setExpanded(True)

        self.tree.blockSignals(False)
        self._on_tree_selection_changed()

    def _on_tree_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        # A source-level checkbox toggles all of its segments at once.
        segment_id = item.data(0, Qt.UserRole)
        if isinstance(segment_id, int):
            self.model.set_segment_enabled(segment_id, item.checkState(0) == Qt.Checked)
            self.refresh_pages()
            return

        path = item.data(0, Qt.UserRole)
        if path is None:
            return
        wanted = item.checkState(0) == Qt.Checked
        self.tree.blockSignals(True)
        for index in range(item.childCount()):
            child = item.child(index)
            child_id = child.data(0, Qt.UserRole)
            if isinstance(child_id, int):
                child.setCheckState(0, Qt.Checked if wanted else Qt.Unchecked)
                self.model.set_segment_enabled(child_id, wanted)
        self.tree.blockSignals(False)
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
            # document read as a block even before they are grouped.
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
            self.page_list.item(position).setIcon(QIcon(QPixmap.fromImage(image)))

    # -------------------------------------------------------------- preview

    def _on_page_selection_changed(self) -> None:
        self._update_preview()

    def _update_preview(self) -> None:
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

        extra = "   (+%d more selected)" % (len(indices) - 1) if len(indices) > 1 else ""
        rotation = "   rotated %d\u00b0" % page.rotation if page.rotation else ""
        self.preview_caption.setText(
            "Page %d of %d\n%s%s%s"
            % (position + 1, self.model.page_count, node.source.name, rotation, extra)
        )

    def selected_page_indices(self) -> list[int]:
        return sorted(self.page_list.row(item) for item in self.page_list.selectedItems())

    def _on_pages_reordered(self, *_args) -> None:
        """Adopt the order the user dragged the list into."""
        from .document import PageRef

        reordered = []
        for row in range(self.page_list.count()):
            data = self.page_list.item(row).data(Qt.UserRole)
            if data is None:
                continue
            segment_id, index, rotation = data
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

        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.act_convert.setEnabled(False)

        self._conversion = ConversionWorker(
            self.selected_sources, self.scratch, self.default_policy, self,
            policy_for=self.policy_for,
        )
        self._conversion.progressed.connect(self.status.showMessage)
        self._conversion.completed.connect(self._on_converted)
        self._conversion.crashed.connect(self._on_worker_crash)
        self._conversion.start()

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
                    page_count=pipeline.count_pages(segment.pdf)
                    if segment.ok and segment.pdf else 0,
                )

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
            lines.append("\u2026 and %d more" % (len(failures) - 14))

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Some items could not be converted")
        box.setText("%d item(s) failed. The rest were converted." % len(failures))
        box.setDetailedText("\n\n".join(lines))
        retry = box.addButton("Retry failed", QMessageBox.AcceptRole)
        box.addButton("Continue", QMessageBox.RejectRole)
        box.exec()

        if box.clickedButton() is retry:
            unique: list[Path] = []
            seen = set()
            for segment in failures:
                if str(segment.source) not in seen:
                    seen.add(str(segment.source))
                    unique.append(segment.source)
            self.retry_sources(unique)

    def retry_sources(self, sources: list[Path]) -> None:
        """Convert just the failed sources again, leaving everything else alone.

        The retry uses its own scratch subdirectory: reusing the first run's
        directory would collide with the intermediate files already there, since
        those are named by index within their batch.
        """
        if not sources or self.scratch is None:
            return

        attempts = len(list(Path(self.scratch).glob("retry*")))
        retry_root = Path(self.scratch) / ("retry%d" % attempts)

        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.act_convert.setEnabled(False)
        self.status.showMessage("Retrying %d file(s)\u2026" % len(sources))

        self._conversion = ConversionWorker(
            sources, retry_root, self.default_policy, self, policy_for=self.policy_for
        )
        self._conversion.progressed.connect(self.status.showMessage)
        self._conversion.completed.connect(self._on_retried)
        self._conversion.crashed.connect(self._on_worker_crash)
        self._conversion.start()

    def _on_retried(self, report) -> None:
        self.progress.setVisible(False)
        self.act_convert.setEnabled(True)

        recovered = 0
        for result in report.sources:
            self.model.remove_failed_for(result.source)
            for segment in result.segments:
                if segment.ok:
                    recovered += 1
                self.model.add_segment(
                    result.source,
                    segment.label,
                    segment.pdf,
                    segment.ok,
                    kind=segment.kind,
                    detail=segment.detail,
                    page_count=pipeline.count_pages(segment.pdf)
                    if segment.ok and segment.pdf else 0,
                )

        self.refresh_tree()
        self.refresh_pages()

        still_failing = [
            s for s in report.all_segments if not s.ok and not is_policy_exclusion(s)
        ]
        if recovered:
            self.status.showMessage(
                "Retry recovered %d segment(s); %d page(s) now."
                % (recovered, self.model.page_count)
            )
        if still_failing:
            self._report_failures(still_failing)
        elif recovered:
            QMessageBox.information(self, "Retry succeeded",
                                    "%d segment(s) were recovered." % recovered)

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
        """Pick the destination with the native Windows save dialog."""
        start = str(self.output_path) if self.output_path else "consolidated.pdf"
        name, _ = QFileDialog.getSaveFileName(
            self, "Save merged PDF as", start, "PDF (*.pdf)"
        )
        if not name:
            return
        path = Path(name)
        if path.suffix.lower() != ".pdf":
            path = path.with_suffix(".pdf")
        self.output_path = path
        self.lbl_output.setText(str(path))
        self.status.showMessage("Output set to %s" % path.name)

    def create_pdf(self) -> None:
        if not self.model.pages:
            QMessageBox.information(self, "Nothing to assemble", "Convert some files first.")
            return

        if self.output_path is None:
            self.choose_output()
            if self.output_path is None:
                return

        entries = self.model.to_entries()
        if not entries:
            QMessageBox.information(self, "Nothing to assemble", "No usable pages.")
            return

        options = self._merge_options(self.output_path)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.btn_create.setEnabled(False)

        self._merge = MergeWorker(entries, self.output_path, options, self)
        self._merge.completed.connect(self._on_merged)
        self._merge.crashed.connect(self._on_merge_crash)
        self._merge.start()

    def _merge_options(self, dest: Path) -> assemble.MergeOptions:
        numbering = assemble.PageNumbering(
            mode=self.cmb_numbering.currentText(),
            position="bottom-center",
            template="{n} of {total}",
        )
        footer_text = self.edt_footer.text().strip()
        return assemble.MergeOptions(
            page_size=pages.resolve(self.cmb_page_size.currentText()),
            margin=36.0,
            bookmarks=self.chk_bookmarks.isChecked(),
            separator=self.cmb_separator.currentText(),
            numbering=numbering,
            footer=assemble.HeaderFooter(center=footer_text)
            if footer_text else assemble.HeaderFooter(),
            title=dest.stem,
        )

    def _on_merged(self, report) -> None:
        self.progress.setVisible(False)
        self.btn_create.setEnabled(True)
        message = "Wrote %d page(s)" % report.pages
        if report.separators:
            message += " including %d separator(s)" % report.separators
        self.status.showMessage(message + ".")
        QMessageBox.information(
            self, "Done", "%s.\n\n%s" % (message, self.output_path)
        )

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
