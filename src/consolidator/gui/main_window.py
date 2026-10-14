"""The main application window."""

from __future__ import annotations

import dataclasses
import hashlib
import time
from pathlib import Path

from PySide6.QtCore import QFileInfo, Qt, QSize, QTimer
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QIcon,
    QKeySequence,
    QPixmap,
    QShortcut,
    QTransform,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFileIconProvider,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
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
    QStyle,
    QToolBar,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import assemble, convert, email_items, pages, pipeline
from .document import DocumentModel
from .duplicates_dialog import DuplicatesDialog
from .licences import show_about, show_licences
from .thumbs import ThumbnailCache
from .worker import ConversionWorker, DuplicateWorker, MergeWorker, ThumbnailWorker

POLICY_EXCLUSION = "excluded by the extraction policy"

# Row and thumbnail sizes, in pixels. Kept compact so more pages fit.
LIST_THUMB_W = 64
LIST_THUMB_H = 84
ROW_HEIGHT = 92
PREVIEW_HEIGHT = 640
TREE_ROW_HEIGHT = 20
TREE_ICON = 16

# Thumbnails are rendered only for the rows on screen plus a margin on either
# side, so scrolling shows finished rows. This keeps large batches (tens of
# thousands of pages) from rendering every page up front.
VISIBLE_PAGES = 40
THUMBNAIL_MARGIN = 25

# Row suffix for each segment kind.
KIND_SUFFIX = {"body": "body", "attachment": "attachment", "document": ""}

# Item data role holding the row type ("source", "message" or "segment").
# Stored explicitly because the payload type alone cannot tell three row kinds apart.
ROW_ROLE = Qt.UserRole + 1

# Tooltip suffix for navigator rows that explains how to jump to their pages.
JUMP_HINT = "\n\nClick the arrow at the end of the row (or press Ctrl+J) to see its pages."

FOOTER_HELP = (
    "Text printed at the bottom centre of every page. Leave empty for no footer.\n\n"
    "Placeholders:\n"
    "  {doc}\tfile name\n"
    "  {n}\tpage number in the merged PDF\n"
    "  {total}\tpage count of the merged PDF\n"
    "  {n_doc}\tpage number within the file\n"
    "  {total_doc}\tpage count of the file\n"
    "  {date}\ttoday's date (YYYY-MM-DD)\n\n"
    "Example: {doc} \u2013 page {n} of {total}"
)

# Navigator columns. The jump arrow has its own narrow column so it stays
# aligned and does not compete with long names for space.
NAME_COLUMN, PAGES_COLUMN, JUMP_COLUMN = 0, 1, 2
JUMP_COLUMN_WIDTH = 24


# File dialog filter built from convert.SUPPORTED_EXTS so it always matches
# the supported formats.
SUPPORTED_FILTER = " ".join(
    "*" + ext for ext in sorted(convert.SUPPORTED_EXTS)
)


def is_policy_exclusion(segment) -> bool:
    """Return True if a segment was skipped by the extraction policy.

    Attachment categories the user switched off are not reported as failures.
    """
    return POLICY_EXCLUSION in (segment.detail or "")


def group_hue(key: str) -> int:
    """Return a stable hue (0-359) for a message or file key."""
    digest = hashlib.md5(str(key).encode("utf-8", "replace")).digest()
    return digest[0] * 359 // 255


def group_colour(key: str, parent_hue: int | None = None) -> QColor:
    """Return a stable pastel colour for a source file or nested message.

    All pages from the same file share a colour, so an email's body and
    attachments have the same tint. If ``parent_hue`` is given and the hue is
    within 45 degrees of it, the hue is shifted so a nested message stays
    distinct from the message that contains it.
    """
    hue = group_hue(key)
    if parent_hue is not None:
        distance = (hue - parent_hue) % 360
        if distance < 45 or distance > 315:
            hue = (hue + 150) % 360
    return QColor.fromHsv(hue, 55, 246)


def source_colour(key: str) -> QColor:
    """Return a stable pastel colour for a source file."""
    return group_colour(key)


def node_colour(node, parent=None) -> QColor:
    """Return the colour for a segment's pages, taken from its message, not its file."""
    if node is None:
        return source_colour("?")
    key = node.group or str(node.source)
    owner = parent if parent is not None else None
    parent_hue = None
    if owner is not None:
        parent_hue = group_hue(owner.group or str(owner.source))
    return group_colour(key, parent_hue)


def part_name(node) -> str:
    """Return a segment's label without the message-name prefix.

    For example, ``with_pdf.eml`` with label ``with_pdf - invoice.pdf`` gives
    ``invoice.pdf``. For a nested message, the prefix is the name of the
    attachment that contained it.
    """
    label = node.label or ""
    group_label = getattr(node, "group_label", "") or ""
    stem = Path(group_label).stem if group_label else node.source.stem
    prefix = stem + " - "
    if label.startswith(prefix):
        return label[len(prefix):]
    return label


def row_description(node, page, position: int, trail: list[str] | None = None) -> str:
    """Return the one-line row text for a page.

    Starts with the source filename, then an arrow and the part name for email
    parts. ``trail`` lists the nested messages the page was found inside.
    """
    if node is None:
        return "%d.  (unknown)" % position

    text = "%d.  %s" % (position, node.source.name)
    route = list(trail or [])
    if node.kind != "document" or route:
        route.append(part_name(node))
        text += "  \u2192  " + " \u203a ".join(part for part in route if part)
    if page.rotation:
        text += "   [%d\u00b0]" % page.rotation
    if page.duplicate:
        text += "   [duplicate %d%%]" % page.duplicate
    return text


def page_data(page) -> tuple:
    """Return the page fields a page-list row carries, so a drag keeps them."""
    return (page.segment_id, page.index, page.rotation,
            page.duplicate, page.duplicate_note, page.duplicate_of, page.uid)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PDF Consolidator")
        self.resize(1240, 820)

        self.model = DocumentModel()
        self.thumbs = ThumbnailCache()
        self.icon_provider = QFileIconProvider()
        self._icons: dict[str, QIcon] = {}
        self.selected_sources: list[Path] = []
        self.scratch: Path | None = None
        self.output_path: Path | None = None

        # Extraction settings. The checkboxes edit the default policy, or the
        # policy of the email selected in the tree.
        self.default_policy = email_items.ExtractionPolicy()
        self.source_policies: dict[str, email_items.ExtractionPolicy] = {}
        self._loading_policy = False

        # Sources already converted, so adding a file only converts the new one.
        self._converted_sources: set[str] = set()
        self._run_counter = 0

        self._conversion: ConversionWorker | None = None
        self._merge: MergeWorker | None = None
        self._scan: DuplicateWorker | None = None
        self._scan_keys: list[tuple[int, int]] = []
        self._thumbs: ThumbnailWorker | None = None
        # All live thumbnail workers, so shutdown can wait for them.
        self._thumb_workers: set[ThumbnailWorker] = set()
        self._thumb_range: tuple[int, int] | None = None

        # Request thumbnails for newly visible rows after scrolling stops, so
        # rendering does not compete with scrollbar dragging.
        self._thumb_timer = QTimer(self)
        self._thumb_timer.setSingleShot(True)
        self._thumb_timer.setInterval(120)
        self._thumb_timer.timeout.connect(self._start_thumbnails)

        # Convert added files automatically after a short delay. Uses the
        # incremental path so existing page edits are kept.
        self._pending_convert = False
        self._autoconvert = QTimer(self)
        self._autoconvert.setSingleShot(True)
        self._autoconvert.setInterval(350)
        self._autoconvert.timeout.connect(self._convert_new_sources)

        self._build_menu()
        self._build_toolbar()
        self._build_body()
        self._build_options()
        self._build_status()
        self._install_shortcuts()
        self._load_policy_into_widgets(self.default_policy)
        self._update_actions()

    # ------------------------------------------------------------------ UI

    def _build_menu(self) -> None:
        """Build the Help menu with About and Licences entries.

        The GPL recommends that a GUI show its notices in an About box, and the
        LGPL requires the licence texts to be reachable from the program.
        """
        menu = self.menuBar().addMenu("&Help")

        self.act_about = QAction("About PDF Consolidator", self)
        self.act_about.setToolTip(
            "Version, copyright and the licences this program is under."
        )
        self.act_about.triggered.connect(lambda: show_about(self))
        menu.addAction(self.act_about)

        self.act_licences = QAction("Licences\u2026", self)
        self.act_licences.setToolTip(
            "The licence texts for this program and the third-party\n"
            "components it is built from."
        )
        self.act_licences.triggered.connect(lambda: show_licences(self))
        menu.addAction(self.act_licences)

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
        # The jump button sits in the sources heading, so jumping to a row's
        # pages is an explicit action, not a side effect of selecting a row.
        heading = QHBoxLayout()
        heading.addWidget(QLabel("Sources"))
        heading.addStretch(1)
        self.btn_jump = QToolButton()
        self.btn_jump.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown)
        )
        self.btn_jump.setToolTip(
            "Show the selected item's pages in the page list (Ctrl+J)"
        )
        self.btn_jump.setAutoRaise(True)
        self.btn_jump.clicked.connect(self.jump_to_selected)
        heading.addWidget(self.btn_jump)
        left_layout.addLayout(heading)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Source", "Pages", ""])
        self.tree.setColumnWidth(NAME_COLUMN, 250)
        header_view = self.tree.header()
        header_view.setStretchLastSection(False)
        header_view.setSectionResizeMode(NAME_COLUMN, QHeaderView.Stretch)
        header_view.setSectionResizeMode(PAGES_COLUMN, QHeaderView.ResizeToContents)
        header_view.setSectionResizeMode(JUMP_COLUMN, QHeaderView.Fixed)
        header_view.resizeSection(JUMP_COLUMN, JUMP_COLUMN_WIDTH)
        self.jump_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowRight)
        self.tree.setIconSize(QSize(TREE_ICON, TREE_ICON))
        self.tree.setIndentation(14)
        self.tree.setRootIsDecorated(True)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        # Compact rows; the default padding uses too much vertical space.
        self.tree.setStyleSheet(
            "QTreeWidget::item { height: %dpx; padding: 0px; }" % TREE_ROW_HEIGHT
        )
        self.tree.itemChanged.connect(self._on_tree_item_changed)
        self.tree.itemSelectionChanged.connect(self._on_tree_selection_changed)
        self.tree.itemClicked.connect(self._on_tree_item_clicked)
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
        self.btn_scan = QPushButton("Scan for duplicates")
        self.btn_scan.setToolTip(
            "Look for pages that repeat an earlier page, by their text or, for\n"
            "pages without text, by how they look. Each match gets a confidence\n"
            "score; text that differs only in whitespace or letter case still matches."
        )
        self.btn_scan.clicked.connect(self.scan_for_duplicates)
        header.addWidget(self.btn_scan)
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
        self.page_list.itemDoubleClicked.connect(
            lambda item: self.go_to_original(self.page_list.row(item)))
        self.page_list.verticalScrollBar().valueChanged.connect(self._on_pages_scrolled)
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
        self.preview_link = QLabel("")
        self.preview_link.setAlignment(Qt.AlignCenter)
        self.preview_link.setTextFormat(Qt.RichText)
        self.preview_link.setTextInteractionFlags(Qt.LinksAccessibleByMouse
                                                  | Qt.LinksAccessibleByKeyboard)
        self.preview_link.linkActivated.connect(lambda _href: self.go_to_original())
        self.preview_link.setVisible(False)
        preview_layout.addWidget(self.preview_link)
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
        layout.addWidget(self._build_banner())
        layout.addWidget(splitter, 1)
        self.setCentralWidget(central)

    def _build_banner(self) -> QWidget:
        """Build the non-blocking banner that reports failures.

        Used instead of a modal dialog so a large batch with a few failures does
        not block the window. Offers details, retry and dismiss.
        """
        self.banner = QFrame()
        self.banner.setFrameShape(QFrame.StyledPanel)
        self.banner.setStyleSheet(
            "QFrame { background: #fff4e5; border: 1px solid #e0b070; }"
            "QLabel { color: #5a3d0b; }"
        )
        row = QHBoxLayout(self.banner)
        row.setContentsMargins(8, 4, 8, 4)
        self.lbl_banner = QLabel("")
        self.lbl_banner.setWordWrap(True)
        row.addWidget(self.lbl_banner, 1)

        self.btn_banner_details = QPushButton("Details\u2026")
        self.btn_banner_details.clicked.connect(self.show_failure_details)
        row.addWidget(self.btn_banner_details)

        self.btn_banner_retry = QPushButton("Retry failed")
        self.btn_banner_retry.clicked.connect(self.retry_failed)
        row.addWidget(self.btn_banner_retry)

        dismiss = QPushButton("Dismiss")
        dismiss.clicked.connect(lambda: self.banner.setVisible(False))
        row.addWidget(dismiss)

        self.banner.setVisible(False)
        self._failures: list = []
        self._merge_skips: list = []
        return self.banner

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
            ("documents", "Documents (Office, text, web)"),
            ("embedded", "Embedded messages (drilled into)"),
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
        self.edt_footer = QLineEdit()
        self.edt_footer.setPlaceholderText("No footer - e.g. {doc} \u2013 page {n} of {total}")
        self.edt_footer.setToolTip(FOOTER_HELP)
        footer_row = QHBoxLayout()
        footer_row.setContentsMargins(0, 0, 0, 0)
        footer_row.addWidget(self.edt_footer, 1)
        self.lbl_footer_info = QLabel()
        self.lbl_footer_info.setPixmap(
            self.style().standardIcon(QStyle.SP_MessageBoxInformation).pixmap(16, 16))
        self.lbl_footer_info.setToolTip(FOOTER_HELP)
        footer_row.addWidget(self.lbl_footer_info)
        grid.addLayout(footer_row, 1, 1, 1, 4)

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
        self.progress.setMaximumWidth(260)
        self.progress.setMinimumWidth(180)
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(True)
        self.progress.setVisible(False)
        self.status.addPermanentWidget(self.progress)
        # Cancel button for long conversion runs.
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setVisible(False)
        self.btn_cancel.setToolTip("Stop converting after the file in progress")
        self.btn_cancel.clicked.connect(self.cancel_conversion)
        self.status.addPermanentWidget(self.btn_cancel)
        # About button in the status bar, so licence information is one click away.
        self.btn_about = QPushButton("About")
        self.btn_about.setToolTip(
            "Version, copyright and the licences this program is under."
        )
        self.btn_about.clicked.connect(lambda: show_about(self))
        self.status.addPermanentWidget(self.btn_about)
        self.status.showMessage("Add files or a folder to begin.")

    # ------------------------------------------------------------- progress

    def _begin_progress(self, total: int, label: str) -> None:
        """Show a determinate progress bar that counts files."""
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(0)
        self.progress.setFormat("0 / %d" % total)
        self.progress.setVisible(True)
        self.btn_cancel.setVisible(True)
        self.btn_cancel.setEnabled(True)
        self.status.showMessage(label)

    def _on_progress(self, done: int, total: int, label: str) -> None:
        if self.progress.maximum() != max(total, 1):
            self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)
        self.progress.setFormat("%d / %d" % (done, total))
        if label:
            self.status.showMessage(label)

    def _begin_busy(self, label: str) -> None:
        """Show an indeterminate progress bar for work of unknown size."""
        self.progress.setRange(0, 0)
        self.progress.setFormat("")
        self.progress.setVisible(True)
        self.btn_cancel.setVisible(False)
        self.status.showMessage(label)

    def _end_progress(self) -> None:
        self.progress.setVisible(False)
        self.btn_cancel.setVisible(False)

    def cancel_conversion(self) -> None:
        """Stop after the current file. Already converted files are kept.

        The same Cancel button also stops a duplicate scan.
        """
        if self._scan is not None and self._scan.isRunning():
            self._scan.cancel()
            self.btn_cancel.setEnabled(False)
            self.status.showMessage("Stopping the duplicate scan\u2026")
            return
        worker = self._conversion
        if worker is None or not worker.isRunning():
            return
        worker.cancel()
        self.btn_cancel.setEnabled(False)
        self.status.showMessage("Stopping after the current file\u2026")


    def _install_shortcuts(self) -> None:
        """Install keyboard shortcuts: Delete/Backspace remove, Ctrl+J jumps."""
        for sequence in (QKeySequence.Delete, QKeySequence("Backspace")):
            for widget, slot in (
                (self.tree, self.remove_selected_sources),
                (self.page_list, self.delete_selected),
            ):
                shortcut = QShortcut(sequence, widget)
                shortcut.setContext(Qt.WidgetWithChildrenShortcut)
                shortcut.activated.connect(slot)

        # Ctrl+J is the keyboard equivalent of the jump button.
        for widget in (self.tree, self.page_list):
            jump = QShortcut(QKeySequence("Ctrl+J"), widget)
            jump.setContext(Qt.WidgetWithChildrenShortcut)
            jump.activated.connect(self.jump_to_selected)

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
        """Return the email source selected in the tree, or None."""
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
        """Return the extraction policy for a source, or the default."""
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
        """Return the Windows shell icon for this file type.

        Cached by extension, because the shell lookup is slow and a large folder
        usually has only a few distinct extensions.
        """
        key = path.suffix.lower()
        icon = self._icons.get(key)
        if icon is None:
            try:
                icon = self.icon_provider.icon(QFileInfo(str(path)))
            except Exception:
                icon = QIcon()
            self._icons[key] = icon
        return icon

    def add_files(self) -> None:
        names, _ = QFileDialog.getOpenFileNames(
            self, "Add files", "",
            "All supported (%s);;All files (*)" % SUPPORTED_FILTER,
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
        if not added:
            return
        self.status.showMessage("Added %d file(s). Converting\u2026" % added)
        self.refresh_tree()
        self._update_actions()
        # Convert new files automatically so pages appear without pressing Convert.
        self._autoconvert.start()

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
        names = {root.text(0) for root in roots}

        self.selected_sources = [p for p in self.selected_sources if str(p) not in doomed]
        # Also remove the source's segments and pages, not just the row.
        for path in list(doomed):
            self.model.drop_source(Path(path))
            self.source_policies.pop(path, None)

        self.refresh_tree()
        self.refresh_pages()
        self._update_actions()
        if names:
            self.status.showMessage("Removed %d source(s)." % len(names))

    def clear_all(self) -> None:
        self.selected_sources.clear()
        self.model.clear()
        self.thumbs.clear()
        self.source_policies.clear()
        self._converted_sources.clear()
        self._run_counter = 0
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

        shown: set[str] = set()
        for path in sorted(self.selected_sources, key=self._sort_key):
            shown.add(str(path))
            root = QTreeWidgetItem(self.tree, [path.name, ""])
            root.setToolTip(0, str(path) + JUMP_HINT)
            root.setIcon(0, self.file_icon(path))
            root.setData(0, Qt.UserRole, str(path))
            root.setData(0, ROW_ROLE, "source")
            root.setFlags(root.flags() | Qt.ItemIsUserCheckable)
            root.setCheckState(0, Qt.Checked)
            self._fill_source(root, path)
            pages_here = sum(node.page_count for node in self.model.segments_of(path)
                             if node.ok)
            if pages_here:
                root.setText(PAGES_COLUMN, str(pages_here))
            self._set_jump(root, pages_here)
            root.setExpanded(True)

        # Show segments whose source has been removed.
        leftovers: dict[str, list] = {}
        for segment in self.model.segments:
            if str(segment.source) not in shown:
                leftovers.setdefault(str(segment.source), []).append(segment)
        for source, segments in leftovers.items():
            root = QTreeWidgetItem(self.tree, [Path(source).name, ""])
            root.setData(0, Qt.UserRole, source)
            root.setData(0, ROW_ROLE, "source")
            root.setForeground(0, Qt.gray)
            for segment in segments:
                self._add_segment_item(root, segment)
            root.setExpanded(True)

        self.tree.blockSignals(False)
        self._on_tree_selection_changed()

    def _fill_source(self, root: QTreeWidgetItem, path: Path) -> None:
        """Add a source's parts and nested messages under its row.

        Entries keep the order they had in the email, so a forwarded message
        appears between the attachments around it.
        """
        self._add_entries(root, self.model.navigator_entries(path))

    def _add_entries(self, parent_item: QTreeWidgetItem, entries: list) -> None:
        from .document import MessageNode

        for entry in entries:
            if isinstance(entry, MessageNode):
                self._add_message_item(parent_item, entry)
            else:
                self._add_segment_item(parent_item, entry)

    def _set_jump(self, item: QTreeWidgetItem, page_count: int) -> None:
        """Add the jump arrow to a row that has pages."""
        if page_count > 0:
            item.setIcon(JUMP_COLUMN, self.jump_icon)
            item.setToolTip(JUMP_COLUMN, "Show in the page list")

    def _on_tree_item_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        """Jump to a row's pages when its arrow column is clicked."""
        if column != JUMP_COLUMN:
            return
        position = self.first_page_of(item)
        if position is None:
            self.status.showMessage("%s contributes no pages." % item.text(0).strip())
            return
        self.go_to_page(position)

    def part_icon(self, segment) -> QIcon:
        """Return the file-type icon for an attachment, or a generic icon for a body."""
        if segment.kind == "body":
            return self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        name = Path(part_name(segment) or "")
        if name.suffix:
            return self.file_icon(name)
        return self.file_icon(segment.source)

    def _add_message_item(self, parent_item: QTreeWidgetItem, message) -> None:
        """Add a nested message as its own node, with its parts as children.

        A forwarded email's body and attachments appear under it rather than as
        siblings of the outer message's parts.
        """
        item = QTreeWidgetItem(parent_item, ["  " + message.label,
                                             str(message.page_count)])
        item.setData(0, Qt.UserRole, message.key)
        item.setData(0, ROW_ROLE, "message")
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        contents = message.descendants()
        enabled = [node for node in contents if node.enabled]
        item.setCheckState(0, Qt.Checked if len(enabled) == len(contents)
                           else Qt.Unchecked if not enabled else Qt.PartiallyChecked)
        item.setToolTip(0, "Message attached to the message above - %d page(s)%s"
                        % (message.page_count, JUMP_HINT))
        if message.label:
            item.setIcon(0, self.file_icon(Path(message.label)))
        self._set_jump(item, message.page_count)
        self._add_entries(item, message.entries or message.segments + message.children)
        item.setExpanded(True)

    def _add_segment_item(self, parent_item: QTreeWidgetItem, segment) -> None:
        if segment.ok:
            label = "  " + part_name(segment)
            child = QTreeWidgetItem(parent_item, [label, str(segment.page_count)])
            child.setToolTip(0, segment.label + JUMP_HINT)
            child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
            child.setCheckState(0, Qt.Checked if segment.enabled else Qt.Unchecked)
            child.setData(0, Qt.UserRole, segment.id)
            child.setData(0, ROW_ROLE, "segment")
            child.setIcon(0, self.part_icon(segment))
            self._set_jump(child, segment.page_count)
            # Same tint as the page rows, so both views show the same grouping.
            child.setBackground(
                0, QBrush(node_colour(segment, self.model.group_owner(segment)))
            )
        else:
            child = QTreeWidgetItem(
                parent_item,
                ["  %s (%s)" % (part_name(segment), segment.detail or "failed"), "\u2014"],
            )
            child.setForeground(0, Qt.gray)
            child.setToolTip(0, segment.detail)
            child.setIcon(0, self.style().standardIcon(
                QStyle.StandardPixmap.SP_MessageBoxWarning))
            child.setData(0, Qt.UserRole, segment.id)
            child.setData(0, ROW_ROLE, "segment")

    def _on_tree_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        role = item.data(0, ROW_ROLE)
        if role == "segment":
            segment_id = item.data(0, Qt.UserRole)
            if isinstance(segment_id, int):
                self.model.set_segment_enabled(
                    segment_id, item.checkState(0) == Qt.Checked
                )
                self.refresh_pages()
            return

        # A source or nested message: its checkbox applies to the whole subtree.
        wanted = item.checkState(0) == Qt.Checked
        self.tree.blockSignals(True)
        self._set_subtree_enabled(item, wanted)
        self.tree.blockSignals(False)
        self.refresh_pages()

    def _set_subtree_enabled(self, item: QTreeWidgetItem, wanted: bool) -> None:
        for index in range(item.childCount()):
            child = item.child(index)
            if child.data(0, ROW_ROLE) == "segment":
                segment_id = child.data(0, Qt.UserRole)
                child.setCheckState(0, Qt.Checked if wanted else Qt.Unchecked)
                if isinstance(segment_id, int):
                    self.model.set_segment_enabled(segment_id, wanted)
            else:
                child.setCheckState(0, Qt.Checked if wanted else Qt.Unchecked)
                self._set_subtree_enabled(child, wanted)

    # ------------------------------------------------- navigator to pages

    def _item_segment_ids(self, item: QTreeWidgetItem) -> list[int]:
        """Return the segment ids for a navigator row and its subtree."""
        if item.data(0, ROW_ROLE) == "segment":
            segment_id = item.data(0, Qt.UserRole)
            return [segment_id] if isinstance(segment_id, int) else []
        found: list[int] = []
        for index in range(item.childCount()):
            found.extend(self._item_segment_ids(item.child(index)))
        return found

    def first_page_of(self, item: QTreeWidgetItem) -> int | None:
        """Return the page-list position of a navigator row's first page.

        Uses the current page-list order, not the source order, because the user
        may have reordered pages.
        """
        wanted = set(self._item_segment_ids(item))
        if not wanted:
            return None
        for position, page in enumerate(self.model.pages):
            if page.segment_id in wanted:
                return position
        return None

    def jump_to_selected(self) -> bool:
        """Scroll the page list to the selected navigator row's first page.

        Returns True if it jumped, False if there was nothing to jump to.
        """
        items = self.tree.selectedItems()
        if not items:
            self.status.showMessage("Select a source or an item to jump to its pages.")
            return False
        position = self.first_page_of(items[0])
        if position is None:
            self.status.showMessage(
                "%s contributes no pages." % items[0].text(0).strip()
            )
            return False
        self.go_to_page(position)
        return True

    def go_to_page(self, position: int) -> None:
        """Select one page row and scroll it into view."""
        row = self.page_list.item(position)
        if row is None:
            return
        self.page_list.clearSelection()
        row.setSelected(True)
        self.page_list.setCurrentItem(row)
        self.page_list.scrollToItem(row, QAbstractItemView.PositionAtCenter)
        # An unchanged selection emits no signal, so refresh the preview directly.
        self._update_preview()

    # ---------------------------------------------------------------- pages

    def refresh_pages(self) -> None:
        self.page_list.blockSignals(True)
        self.page_list.clear()
        # Repopulating makes the first row current, which some styles treat as
        # selected. Clear it so actions only apply to pages the user selected.
        self.page_list.clearSelection()

        for position, page in enumerate(self.model.pages, start=1):
            node = self.model.segment(page.segment_id)
            tint = node_colour(node, self.model.group_owner(node))
            trail = self.model.group_trail(node)
            item = QListWidgetItem(row_description(node, page, position, trail))
            item.setData(Qt.UserRole, page_data(page))
            tip = "%s\npage %d of %d" % (node.label if node else "?",
                                         page.index + 1,
                                         node.page_count if node else 0)
            if page.duplicate:
                tip += "\n\n" + page.duplicate_note + "\nDouble-click to show that page."
            item.setToolTip(tip)
            # The row tint identifies the message the page came from; a nested
            # message gets a different colour from its parent.
            item.setBackground(QBrush(tint))
            item.setForeground(QBrush(QColor("#202020")))
            item.setSizeHint(QSize(0, ROW_HEIGHT))
            self.page_list.addItem(item)

        self.page_list.blockSignals(False)
        # The list was rebuilt, so all thumbnails must be requested again.
        self._thumb_range = None
        self._start_thumbnails()
        self._update_preview()
        self.status.showMessage(self.model.summary())

    def group_by_document(self) -> None:
        """Group pages by source file, keeping each file's page order."""
        if not self.model.pages:
            return
        self.model.group_by_source()
        self.refresh_pages()
        self.status.showMessage(
            "Grouped %d page(s) by source document." % self.model.page_count
        )

    # ----------------------------------------------------------- duplicates

    def page_name(self, position: int) -> str:
        """Return "n. file" for a page-list position, as the duplicates dialog shows it.

        The duplicate tag is left out; the dialog has its own confidence column.
        """
        page = dataclasses.replace(self.model.pages[position], duplicate=0)
        node = self.model.segment(page.segment_id)
        return row_description(node, page, position + 1, self.model.group_trail(node))

    def scan_for_duplicates(self) -> None:
        """Read every page in the background and report pages that repeat."""
        if len(self.model.pages) < 2:
            QMessageBox.information(self, "Scan for duplicates",
                                    "There must be at least two pages to compare.")
            return
        if self._scan is not None and self._scan.isRunning():
            return
        jobs = []
        for page in self.model.pages:
            node = self.model.segment(page.segment_id)
            jobs.append((node.pdf if node and node.pdf else Path(), page.index))
        self._scan_keys = [page.key for page in self.model.pages]
        self.btn_scan.setEnabled(False)
        self._begin_progress(len(jobs), "Scanning %d page(s) for duplicates\u2026" % len(jobs))

        self._scan = DuplicateWorker(jobs, self)
        self._scan.advanced.connect(self._on_progress)
        self._scan.completed.connect(self._on_duplicates_found)
        self._scan.crashed.connect(self._on_scan_crash)
        self._scan.start()

    def _on_duplicates_found(self, matches) -> None:
        self._end_progress()
        self.btn_scan.setEnabled(True)
        cancelled = self._scan is not None and self._scan.cancelled
        if cancelled:
            self.status.showMessage("Duplicate scan stopped.")
            return
        if [page.key for page in self.model.pages] != self._scan_keys:
            self.status.showMessage(
                "The pages changed during the scan. Scan again for up-to-date results.")
            return
        if not matches:
            self.mark_duplicates([])
            self.status.showMessage("No duplicate pages found.")
            QMessageBox.information(self, "Scan for duplicates", "No duplicate pages found.")
            return
        self.mark_duplicates(matches)
        self.status.showMessage("%d possible duplicate page(s) found." % len(matches))
        self.duplicates_dialog = DuplicatesDialog(self, matches)
        self.duplicates_dialog.finished.connect(lambda _result: self.mark_for_deletion([]))
        # Window-modal, so the page list cannot change while the dialog's
        # positions are in use.
        self.duplicates_dialog.open()

    def _on_scan_crash(self, detail: str) -> None:
        self._end_progress()
        self.btn_scan.setEnabled(True)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle("Duplicate scan failed")
        box.setText("The pages could not be scanned for duplicates.")
        box.setDetailedText(detail)
        box.exec()

    def mark_duplicates(self, matches: list) -> None:
        """Tag each matched page's row with its confidence, replacing earlier tags."""
        for page in self.model.pages:
            page.duplicate, page.duplicate_note, page.duplicate_of = 0, "", 0
        for match in matches:
            page = self.model.pages[match.position]
            original = self.model.pages[match.original]
            node = self.model.segment(original.segment_id)
            page.duplicate = match.confidence
            page.duplicate_of = original.uid
            page.duplicate_note = "Duplicate (%d%%) of %s, page %d\n%s" % (
                match.confidence, node.label if node else "?",
                original.index + 1, match.reason)
        self.refresh_pages()

    def mark_for_deletion(self, positions) -> None:
        """Strike through the rows the duplicates dialog would delete."""
        doomed = set(positions)
        for row in range(self.page_list.count()):
            item = self.page_list.item(row)
            font = item.font()
            font.setStrikeOut(row in doomed)
            item.setFont(font)
            item.setForeground(QBrush(QColor("#b00020" if row in doomed else "#202020")))

    def _update_duplicate_link(self) -> None:
        """Under the preview, link a duplicate page to the page it repeats."""
        indices = self.selected_page_indices()
        page = self.model.pages[indices[0]] if indices else None
        if page is None or not page.duplicate:
            self.preview_link.setVisible(False)
            return
        original = self.model.position_of(page.duplicate_of)
        if original is None:
            self.preview_link.setText(
                "Duplicate (%d%%) of a page that is no longer in the list" % page.duplicate)
        else:
            self.preview_link.setText(
                '<a href="original">Duplicate (%d%%) of page %d \u2013 show it</a>'
                % (page.duplicate, original + 1))
        self.preview_link.setVisible(True)

    def go_to_original(self, position: int | None = None) -> bool:
        """Show the page that a duplicate repeats; return False if there is none.

        ``position`` defaults to the first selected page.
        """
        if position is None:
            indices = self.selected_page_indices()
            position = indices[0] if indices else -1
        if not 0 <= position < len(self.model.pages):
            return False
        page = self.model.pages[position]
        if not page.duplicate:
            return False
        original = self.model.position_of(page.duplicate_of)
        if original is None:
            self.status.showMessage("The page this one duplicates is no longer in the list.")
            return False
        self.go_to_page(original)
        return True

    def delete_duplicate_pages(self, positions: list[int]) -> None:
        removed = self.model.delete(positions)
        self.refresh_pages()
        self.status.showMessage("Deleted %d duplicate page(s)." % removed)

    def _start_thumbnails(self) -> None:
        """Render thumbnails for the rows near the viewport.

        Only visible rows plus a margin are rendered, so large batches stay
        responsive. Scrolling requests more.
        """
        if self._thumbs is not None and self._thumbs.isRunning():
            if self._thumb_range == self._visible_page_range():
                return
            # Do not wait for the old worker: it stops after its current page,
            # and the cache lock prevents concurrent renders. Waiting here would
            # block the GUI thread on slow pages.
            self._thumbs.cancel()

        first, last = self._visible_page_range()
        self._thumb_range = (first, last)
        jobs = []
        for position in range(first, last):
            page = self.model.pages[position]
            node = self.model.segment(page.segment_id)
            if node and node.pdf:
                jobs.append((position, node.pdf, page.index))
        if not jobs:
            return

        worker = ThumbnailWorker(jobs, self.thumbs, self)
        worker.ready.connect(self._on_thumbnail)
        # Bound method of the window, so the slot runs on the GUI thread.
        worker.finished.connect(self._on_thumbnail_worker_finished)
        self._thumb_workers.add(worker)
        self._thumbs = worker
        worker.start()

    def _on_thumbnail_worker_finished(self) -> None:
        self._on_thumbnails_finished(self.sender())

    def _on_thumbnails_finished(self, worker=None) -> None:
        """Release a finished worker and report any thumbnail render failures.

        Each scroll starts a worker, so finished workers are deleted here.
        Failures are reported because a missing thumbnail is otherwise not visible.
        """
        if worker is not None:
            self._thumb_workers.discard(worker)
            if self._thumbs is worker:
                self._thumbs = None
            worker.deleteLater()
        failures = getattr(self.thumbs, "failed", 0)
        if failures:
            self.status.showMessage(
                "%d page(s) could not be rendered as thumbnails (%s). "
                "The pages themselves are unaffected."
                % (failures, getattr(self.thumbs, "last_error", "") or "unknown")
            )

    def _visible_page_range(self) -> tuple[int, int]:
        """Return the (first, last) rows to render: visible rows plus a margin."""
        count = self.page_list.count()
        if count == 0:
            return 0, 0
        viewport = self.page_list.viewport()
        top = self.page_list.indexAt(viewport.rect().topLeft())
        bottom = self.page_list.indexAt(viewport.rect().bottomLeft())
        first = top.row() if top.isValid() else 0
        last = bottom.row() if bottom.isValid() else min(count, first + VISIBLE_PAGES)
        first = max(0, first - THUMBNAIL_MARGIN)
        last = min(count, max(last, first) + THUMBNAIL_MARGIN + 1)
        # Render at least VISIBLE_PAGES rows, so the first paint is not empty
        # before the widget has been laid out.
        if last - first < VISIBLE_PAGES:
            last = min(count, first + VISIBLE_PAGES)
        return first, last

    def _on_pages_scrolled(self) -> None:
        """Ask for more thumbnails once scrolling settles."""
        self._thumb_timer.start()

    def _on_thumbnail(self, position: int, rendered, image) -> None:
        """Show a thumbnail if its row still shows the page it was rendered for."""
        if not (0 <= position < self.page_list.count()
                and position < len(self.model.pages)):
            return
        page = self.model.pages[position]
        node = self.model.segment(page.segment_id)
        if node is None or not node.pdf or (str(node.pdf), page.index) != tuple(rendered):
            return
        self.page_list.item(position).setIcon(QIcon(QPixmap.fromImage(image)))

    # -------------------------------------------------------------- preview

    def _on_page_selection_changed(self) -> None:
        self._update_preview()

    def _update_preview(self) -> None:
        self._update_duplicate_link()
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
        if image is not None and page.rotation:
            # Apply the page's rotation so the preview matches the output.
            image = image.transformed(
                QTransform().rotate(page.rotation), Qt.SmoothTransformation
            )
            if image.height() > PREVIEW_HEIGHT:
                image = image.scaledToHeight(PREVIEW_HEIGHT, Qt.SmoothTransformation)

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
        """Update the model with the order after a drag-and-drop."""
        from .document import PageRef

        reordered = []
        for row in range(self.page_list.count()):
            data = self.page_list.item(row).data(Qt.UserRole)
            if data is None:
                continue
            segment_id, index, rotation, score, note, original, uid = data
            reordered.append(PageRef(segment_id, index, rotation,
                                     duplicate=score, duplicate_note=note,
                                     duplicate_of=original, uid=uid))
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
            item.setData(Qt.UserRole, page_data(page))

    # ----------------------------------------------------------- conversion

    def _ensure_scratch(self, reset: bool = False) -> Path:
        """Return the scratch directory, creating it if needed.

        Earlier segments point into this directory, so it is only cleared
        (``reset=True``) when everything is converted again.
        """
        import tempfile

        from .. import libreoffice

        if reset and self.scratch and self.scratch.exists():
            libreoffice._remove_tree(self.scratch)
            self.scratch = None
        if self.scratch is None:
            self.scratch = Path(
                tempfile.mkdtemp(prefix="gui_", dir=str(pipeline.choose_scratch()))
            )
            # Mark the directory as in use so a later start does not sweep it.
            pipeline.claim_scratch(self.scratch)
        return self.scratch

    def _next_run_dir(self) -> Path:
        """Create and return a new scratch subdirectory for this run.

        Intermediate files are named by index within their batch, so sharing a
        directory across runs would overwrite them.
        """
        scratch = self._ensure_scratch()
        self._run_counter += 1
        run_dir = scratch / ("run%d" % self._run_counter)
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def start_conversion(self) -> None:
        """Convert all sources again, discarding the current pages."""
        if not self.selected_sources:
            return
        self._converted_sources.clear()
        self._run_conversion(self.selected_sources, replace=True)

    def _convert_new_sources(self) -> None:
        """Convert only sources added since the last run.

        Appends to the model instead of rebuilding, so existing page edits are kept.
        """
        pending = [p for p in self.selected_sources if str(p) not in self._converted_sources]
        if pending:
            self._run_conversion(pending, replace=False)

    def _run_conversion(self, sources: list[Path], replace: bool) -> None:
        if self._conversion is not None and self._conversion.isRunning():
            # Files were added during a conversion; convert them when it finishes.
            self._pending_convert = True
            return

        if replace:
            self._ensure_scratch(reset=True)
            self.model.clear()
            self.thumbs.clear()
            self.refresh_pages()

        # Clear failures from the previous run.
        self.banner.setVisible(False)
        self._failures = []

        self._begin_progress(len(sources), "Converting %d file(s)\u2026" % len(sources))
        self.act_convert.setEnabled(False)

        self._conversion = ConversionWorker(
            [Path(s) for s in sources],
            self._next_run_dir(),
            self.default_policy,
            self,
            policy_for=self.policy_for,
        )
        self._conversion.progressed.connect(self.status.showMessage)
        self._conversion.advanced.connect(self._on_progress)
        self._conversion.completed.connect(self._on_converted)
        self._conversion.crashed.connect(self._on_worker_crash)
        self._conversion.start()

    def _resume_pending_conversion(self) -> None:
        """Convert sources that were added while a run was in progress."""
        if self._pending_convert:
            self._pending_convert = False
            self._autoconvert.start()

    def _on_converted(self, report) -> None:
        self._end_progress()
        self.act_convert.setEnabled(True)
        cancelled = getattr(report, "cancelled", False)

        for result in report.sources:
            self._converted_sources.add(str(result.source))
            for segment in result.segments:
                self.model.add_segment(
                    result.source,
                    segment.label,
                    segment.pdf,
                    segment.ok,
                    kind=segment.kind,
                    detail=segment.detail,
                    # Use the pipeline's page count; count here only if it is missing.
                    page_count=segment.page_count or (
                        pipeline.count_pages(segment.pdf)
                        if segment.ok and segment.pdf else 0),
                    group=segment.group,
                    parent=segment.parent,
                    group_label=segment.group_label,
                    depth=segment.depth,
                )

        self.refresh_tree()
        self.refresh_pages()
        self._update_actions()
        self._resume_pending_conversion()

        if cancelled:
            # The user cancelled, so do not report unconverted files as failures.
            self.status.showMessage(
                "Stopped. %d segment(s), %d page(s) kept."
                % (len(report.segments), self.model.page_count)
            )
            return

        failures = [s for s in report.all_segments if not s.ok and not is_policy_exclusion(s)]
        excluded = [s for s in report.all_segments if not s.ok and is_policy_exclusion(s)]
        if failures:
            self._report_failures(failures)
        else:
            message = ("Converted %d segment(s), %d page(s)."
                       % (len(report.segments), self.model.page_count))
            if excluded:
                message += " %d item(s) excluded by the extraction settings." % len(excluded)
            retried = getattr(report, "retried_batches", 0)
            if retried:
                # Some batches failed with parallel LibreOffice processes and were
                # retried one file at a time. Suggest fewer workers.
                message += (" LibreOffice needed a second, single-file pass on %d "
                            "batch(es) - set CONSOLIDATOR_WORKERS=2 if that keeps "
                            "happening." % retried)
            self.status.showMessage(message)

    def _report_failures(self, failures: list) -> None:
        """Show conversion failures in the banner."""
        self._failures = list(failures)
        first = self._failures[0]
        self.lbl_banner.setText(
            "%d item(s) could not be converted, starting with %s. "
            "Everything else was converted."
            % (len(self._failures), first.source.name)
        )
        # Conversion failures can be retried (merge skips cannot), so show Retry.
        self.btn_banner_retry.setVisible(True)
        self.banner.setVisible(True)

    def failure_lines(self) -> list[str]:
        """Return one entry per failure for the details view, capped at 200."""
        lines = []
        for segment in self._failures[:200]:
            lines.append("%s\n    %s" % (segment.display, segment.detail or "failed"))
        if len(self._failures) > 200:
            lines.append("\u2026 and %d more" % (len(self._failures) - 200))
        return lines

    def show_failure_details(self) -> None:
        """Show the list of failures in a dialog."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Items that could not be converted")
        box.setText("%d item(s) failed. The rest were converted." % len(self._failures))
        box.setDetailedText("\n\n".join(self.failure_lines()))
        box.exec()

    def retry_failed(self) -> None:
        """Convert the failed sources again, keeping everything else."""
        unique: list[Path] = []
        seen = set()
        for segment in self._failures:
            if str(segment.source) not in seen:
                seen.add(str(segment.source))
                unique.append(segment.source)
        self.banner.setVisible(False)
        self.retry_sources(unique)

    def retry_sources(self, sources: list[Path]) -> None:
        """Convert the given sources again, leaving everything else unchanged.

        Each retry uses its own scratch subdirectory, because intermediate files
        are named by index within their batch and would otherwise collide.
        """
        if not sources or self.scratch is None:
            return
        if self._conversion is not None and self._conversion.isRunning():
            self.status.showMessage("A conversion is already running; retry when it finishes.")
            return

        attempts = len(list(Path(self.scratch).glob("retry*")))
        retry_root = Path(self.scratch) / ("retry%d" % attempts)

        self._begin_progress(len(sources), "Retrying %d file(s)\u2026" % len(sources))
        self.act_convert.setEnabled(False)

        self._conversion = ConversionWorker(
            sources, retry_root, self.default_policy, self, policy_for=self.policy_for
        )
        self._conversion.progressed.connect(self.status.showMessage)
        self._conversion.advanced.connect(self._on_progress)
        self._conversion.completed.connect(self._on_retried)
        self._conversion.crashed.connect(self._on_worker_crash)
        self._conversion.start()

    def _on_retried(self, report) -> None:
        self._end_progress()
        self.act_convert.setEnabled(True)

        recovered = 0
        for result in report.sources:
            # Replace in place: recovered parts return to their original position
            # instead of being appended at the end.
            recovered += self.model.replace_source(
                result.source, result.segments, count_pages=pipeline.count_pages)

        self.refresh_tree()
        self.refresh_pages()
        self._resume_pending_conversion()

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
            # The status bar already reports success, so just hide the banner.
            self.banner.setVisible(False)

    def _on_worker_crash(self, detail: str) -> None:
        self._end_progress()
        self.act_convert.setEnabled(True)
        self._resume_pending_conversion()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle("Conversion failed")
        box.setText("Something went wrong while converting.")
        box.setDetailedText(detail)
        box.exec()

    # -------------------------------------------------------------- output

    def choose_output(self) -> None:
        """Choose the output path with the native save dialog."""
        start = str(self.output_path) if self.output_path else "consolidated.pdf"
        # Skip the overwrite prompt: an existing file is never replaced; the
        # output is written beside it as "name (1).pdf".
        name, _ = QFileDialog.getSaveFileName(
            self, "Save merged PDF as", start, "PDF (*.pdf)",
            options=QFileDialog.Option.DontConfirmOverwrite,
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
        self._begin_busy("Assembling %d page(s)\u2026" % len(self.model.pages))
        self.btn_create.setEnabled(False)

        self._merge = MergeWorker(entries, self.output_path, options, self)
        self._merge.completed.connect(self._on_merged)
        self._merge.crashed.connect(self._on_merge_crash)
        self._merge.start()

    def _merge_options(self, dest: Path) -> assemble.MergeOptions:
        footer_text = self.edt_footer.text().strip()
        # The footer is drawn bottom centre and takes priority over the page
        # number, so the number moves to bottom right when there is a footer.
        numbering = assemble.PageNumbering(
            mode=self.cmb_numbering.currentText(),
            position="bottom-right" if footer_text else "bottom-center",
            template="{n} of {total}",
        )
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
        self._end_progress()
        self.btn_create.setEnabled(True)
        written = getattr(report, "output", None) or self.output_path
        message = "Wrote %d page(s)" % report.pages
        if written is not None and self.output_path is not None \
                and Path(written) != Path(self.output_path):
            message += " to %s (%s already exists, so it was left alone)" % (
                Path(written).name, Path(self.output_path).name)
        if report.separators:
            message += " including %d separator(s)" % report.separators
        self.status.showMessage(message + ".")

        skipped = list(getattr(report, "skipped", []) or [])
        check = getattr(report, "verification", None)
        problems = list(check.problems) if check is not None else []
        pictures = list(getattr(report, "as_images", []) or [])
        if pictures:
            message += ". %d page(s) could not be copied as they are and were "                        "included as images (page %s)" % (
                           len(pictures), ", ".join(str(p[0]) for p in pictures[:8]))
        if problems:
            # The file was written, but some output pages lack content that
            # their source pages have.
            pages_named = ", ".join(str(p.output_page) for p in problems[:8])
            if len(problems) > 8:
                pages_named += ", \u2026"
            message += ". %d page(s) may be missing content (page %s)" % (
                len(problems), pages_named)
        elif check is not None and check.error:
            message += ". The output could not be checked against its sources"
        elif check is not None and check.checked:
            message += ", every page checked against its source"
        self.status.showMessage(message + ".")

        if skipped:
            # Report items the merge left out, so a short output is not missed.
            self.status.showMessage(
                "%s. %d item(s) could not be merged." % (message, len(skipped))
            )
            self._merge_skips = skipped
            self.lbl_banner.setText(
                "%d item(s) could not be merged and are not in the output: %s."
                % (len(skipped), ", ".join(label for label, _why in skipped[:3]))
            )
            self.btn_banner_retry.setVisible(False)
            self.banner.setVisible(True)
        else:
            self._merge_skips = []
            self.btn_banner_retry.setVisible(True)
            self.banner.setVisible(False)

        if (problems or pictures) and not skipped:
            self._merge_skips = []
            self.lbl_banner.setText(
                "%d page(s) of the output need a look - see the details in the "
                "Done dialog." % (len(problems) + len(pictures)))
            self.btn_banner_retry.setVisible(False)
            self.banner.setVisible(True)

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning if (skipped or problems or pictures)
                    else QMessageBox.Information)
        box.setWindowTitle("Done")
        box.setText("%s.\n\n%s" % (message, written))
        details = []
        if skipped:
            details.append(
                "These were left out of the merged PDF:\n\n"
                + "\n".join("%s\n    %s" % (label, why) for label, why in skipped))
        if pictures:
            details.append(
                "These pages could not be copied as they are, so each was included "
                "as an image of itself. They look the same, but their text cannot be "
                "selected or searched:\n\n"
                + "\n".join("page %d (%s, page %d): %s" % picture for picture in pictures))
        if problems:
            details.append(
                "These pages of the output lack content that their source page has. "
                "Compare them with the source before relying on the file:\n\n"
                + "\n".join(problem.describe() for problem in problems))
        if check is not None and check.error:
            details.append("The output could not be checked: %s" % check.error)
        if details:
            box.setDetailedText("\n\n".join(details))
        box.exec()

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

    def workers_running(self) -> bool:
        """Return True if any background worker is still running.

        Checked at exit: destroying a running QThread makes Qt abort the process,
        so ``app.run`` exits with ``os._exit`` in that case.
        """
        workers = [self._conversion, self._merge, self._scan, *self._thumb_workers]
        for worker in workers:
            try:
                if worker is not None and worker.isRunning():
                    return True
            except RuntimeError:        # already deleted on the C++ side
                continue
        return False

    def confirm_close(self) -> bool:
        """Ask the user to confirm closing, and say what would be lost."""
        if self._conversion is not None and self._conversion.isRunning():
            detail = "A conversion is still running and will be stopped."
        elif self._merge is not None and self._merge.isRunning():
            detail = "The PDF is still being written and will not be finished."
        elif self.model.pages:
            detail = "The page arrangement is not kept once the application closes."
        else:
            detail = ""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Close PDF Consolidator")
        box.setText("Are you sure you want to close the application?")
        if detail:
            box.setInformativeText(detail)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        return box.exec() == QMessageBox.Yes

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Stop workers, wait briefly, and delete scratch in the background.

        Waits are short so closing stays fast, and the scratch tree is deleted
        on a background thread because it can take seconds for a large run.
        """
        import threading

        from .. import libreoffice

        # Confirm only user-initiated closes (title bar button, Alt+F4).
        # Programmatic closes skip the dialog so they do not block the caller.
        if event.spontaneous() and not self.confirm_close():
            event.ignore()
            return

        self._autoconvert.stop()

        workers = [w for w in (self._conversion, self._merge, self._scan, *self._thumb_workers)
                   if w is not None]
        for worker in workers:
            if worker.isRunning() and hasattr(worker, "cancel"):
                worker.cancel()

        # A conversion blocks inside LibreOffice for a whole batch, so end the
        # soffice processes to make the call return and the worker finish.
        converting = self._conversion is not None and self._conversion.isRunning()
        if converting:
            libreoffice.stop_all()

        deadline = time.monotonic() + (3.0 if converting else 0.5)
        for worker in workers:
            if worker.isRunning():
                remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
                worker.wait(remaining_ms)
        if converting:
            # Closing the window does not end the process (for example in tests),
            # so allow LibreOffice to start again. app.run stops it again if it
            # exits with a worker still running.
            libreoffice.resume()

        scratch = self.scratch
        self.scratch = None
        if scratch and Path(scratch).exists():
            from .. import libreoffice

            # Daemon thread, so a slow delete does not delay exit.
            threading.Thread(
                target=libreoffice._remove_tree, args=(Path(scratch),), daemon=True
            ).start()

        super().closeEvent(event)
