"""The main application window."""

from __future__ import annotations

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
from .licences import show_about, show_licences
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

# Thumbnails are rendered for the rows on screen plus this much either side, so
# that scrolling reveals finished rows rather than placeholders. Bounding this is
# what keeps a batch of several hundred files - tens of thousands of pages - from
# trying to render every page before the window becomes usable.
VISIBLE_PAGES = 40
THUMBNAIL_MARGIN = 25

# How each kind of segment is described in a row.
KIND_SUFFIX = {"body": "body", "attachment": "attachment", "document": ""}

# Which role a navigator row plays, so a checkbox knows what it is toggling.
# Segment ids are ints and source paths are strings, so guessing from the payload
# stops being safe once nested messages introduce a third kind of row.
ROW_ROLE = Qt.UserRole + 1

# Every navigator row can take the page column to what it holds; the tooltip is
# where that is discoverable.
JUMP_HINT = "\n\nSelect it and press the jump button (Ctrl+J) to see its pages."


# Every extension the converters know about, built from the sets themselves so
# the file dialog cannot drift from what actually converts - which is how .xlsm
# and .htm came to be missing from it.
SUPPORTED_FILTER = " ".join(
    "*" + ext for ext in sorted(convert.SUPPORTED_EXTS)
)


def is_policy_exclusion(segment) -> bool:
    """True when a segment was deliberately skipped rather than failing.

    Attachment categories the user switched off are not failures, and reporting
    them as such would train the user to ignore the dialog.
    """
    return POLICY_EXCLUSION in (segment.detail or "")


def group_hue(key: str) -> int:
    """A stable hue per message or file, derived from its identity."""
    digest = hashlib.md5(str(key).encode("utf-8", "replace")).digest()
    return digest[0] * 359 // 255


def group_colour(key: str, parent_hue: int | None = None) -> QColor:
    """A stable pastel colour per source file or nested message.

    Every page originating from the same file shares a colour, which is what
    makes grouping visible at a glance - an email's body and its attachments all
    carry the same tint. A nested message gets its own colour, nudged away from
    the colour of the message it arrived inside, so a forwarded email never
    blends into its carrier.
    """
    hue = group_hue(key)
    if parent_hue is not None:
        distance = (hue - parent_hue) % 360
        if distance < 45 or distance > 315:
            hue = (hue + 150) % 360
    return QColor.fromHsv(hue, 55, 246)


def source_colour(key: str) -> QColor:
    """A stable pastel colour per source file."""
    return group_colour(key)


def node_colour(node, parent=None) -> QColor:
    """The colour of a segment's pages: its message's colour, not its file's."""
    if node is None:
        return source_colour("?")
    key = node.group or str(node.source)
    owner = parent if parent is not None else None
    parent_hue = None
    if owner is not None:
        parent_hue = group_hue(owner.group or str(owner.source))
    return group_colour(key, parent_hue)


def part_name(node) -> str:
    """The part of a segment, without the repeated message name.

    ``with_pdf.eml`` plus the label ``with_pdf - invoice.pdf`` yields
    ``invoice.pdf``, which is what the row should show. A nested message's label
    leads with the name of the attachment that carried it instead.
    """
    label = node.label or ""
    group_label = getattr(node, "group_label", "") or ""
    stem = Path(group_label).stem if group_label else node.source.stem
    prefix = stem + " - "
    if label.startswith(prefix):
        return label[len(prefix):]
    return label


def row_description(node, page, position: int, trail: list[str] | None = None) -> str:
    """One line naming a page and the file it came from.

    The original filename always leads, so the column sorts and scans by source,
    with the particular part of an email appended after an arrow. A page from a
    message attached to that email carries the nesting with it: the trail names
    the messages it was found inside.
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
    return text


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
        # policy of the email currently selected in the tree.
        self.default_policy = email_items.ExtractionPolicy()
        self.source_policies: dict[str, email_items.ExtractionPolicy] = {}
        self._loading_policy = False

        # Sources already converted, so adding a file only converts the new one.
        self._converted_sources: set[str] = set()
        self._run_counter = 0

        self._conversion: ConversionWorker | None = None
        self._merge: MergeWorker | None = None
        self._thumbs: ThumbnailWorker | None = None
        self._thumb_range: tuple[int, int] | None = None

        # Scrolling asks for the thumbnails that just came into view, once the
        # user stops moving: rendering while they are still dragging would fight
        # the scrollbar.
        self._thumb_timer = QTimer(self)
        self._thumb_timer.setSingleShot(True)
        self._thumb_timer.setInterval(120)
        self._thumb_timer.timeout.connect(self._start_thumbnails)

        # Adding files converts automatically, shortly after the user stops
        # adding. It must be the *incremental* entry point: a full re-convert
        # would discard page edits the user has already made.
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
        """A Help menu for the notices the GPL asks a GUI to offer.

        The About box is the conventional home for a licence summary, and the
        LGPL wants the texts themselves reachable rather than merely on disk.
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
        # The sources heading carries the jump button, so going to a row's pages
        # is an explicit action rather than a side effect of clicking a row.
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
        """A non-blocking strip for reporting what went wrong.

        A modal dialog is the wrong shape for this: a batch of several hundred
        files is expected to contain a few failures, and stopping the interface
        to announce them - right after an eleven-minute wait - is both an
        interruption and a dead end. The banner says what happened, offers the
        details and the retry, and gets out of the way.
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
        self.progress.setMaximumWidth(260)
        self.progress.setMinimumWidth(180)
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(True)
        self.progress.setVisible(False)
        self.status.addPermanentWidget(self.progress)
        # A batch of several hundred files is long enough to need a way out of.
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setVisible(False)
        self.btn_cancel.setToolTip("Stop converting after the file in progress")
        self.btn_cancel.clicked.connect(self.cancel_conversion)
        self.status.addPermanentWidget(self.btn_cancel)
        # A visible About button, so the licensing is one click away without
        # hunting through a menu.
        self.btn_about = QPushButton("About")
        self.btn_about.setToolTip(
            "Version, copyright and the licences this program is under."
        )
        self.btn_about.clicked.connect(lambda: show_about(self))
        self.status.addPermanentWidget(self.btn_about)
        self.status.showMessage("Add files or a folder to begin.")

    # ------------------------------------------------------------- progress

    def _begin_progress(self, total: int, label: str) -> None:
        """Show a determinate bar: converting is a list of files, so count them."""
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
        """Show an indeterminate bar for work whose size is not known up front."""
        self.progress.setRange(0, 0)
        self.progress.setFormat("")
        self.progress.setVisible(True)
        self.btn_cancel.setVisible(False)
        self.status.showMessage(label)

    def _end_progress(self) -> None:
        self.progress.setVisible(False)
        self.btn_cancel.setVisible(False)

    def cancel_conversion(self) -> None:
        """Stop after the file in progress; whatever is converted is kept."""
        worker = self._conversion
        if worker is None or not worker.isRunning():
            return
        worker.cancel()
        self.btn_cancel.setEnabled(False)
        self.status.showMessage("Stopping after the current file\u2026")


    def _install_shortcuts(self) -> None:
        """Delete and Backspace remove what the focused list has selected."""
        for sequence in (QKeySequence.Delete, QKeySequence("Backspace")):
            for widget, slot in (
                (self.tree, self.remove_selected_sources),
                (self.page_list, self.delete_selected),
            ):
                shortcut = QShortcut(sequence, widget)
                shortcut.setContext(Qt.WidgetWithChildrenShortcut)
                shortcut.activated.connect(slot)

        # Ctrl+J is the keyboard half of the jump button: pick a row in the
        # navigator, then go to its pages without reaching for the mouse.
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
        """The real Windows icon for this file type, via the shell.

        Cached by extension. The shell lookup is the expensive part, and a folder
        of a thousand files has only a handful of distinct extensions - without
        this, drawing the navigator costs one shell round-trip per row, which at
        ten thousand rows is seconds of the user's time per rebuild.
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
        # Convert on our own: making the user press Convert just to see pages
        # was a pointless extra step.
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
        # Removing a source removes what it contributed, not just the row.
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
            root.setExpanded(True)

        # Segments left over (their source was removed) still need showing.
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
        """Put a source's own parts and its nested messages under its row."""
        top_level, messages = self.model.navigator(path)
        for segment in top_level:
            self._add_segment_item(root, segment)
        for message in messages:
            self._add_message_item(root, message)

    def _add_message_item(self, parent_item: QTreeWidgetItem, message) -> None:
        """One nested message: a node of its own, holding its parts.

        This is what makes a forwarded email navigable: its body and its own
        attachments hang inside it rather than appearing as siblings of the
        carrier's, with the message's own colour on every row.
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
        for segment in message.segments:
            self._add_segment_item(item, segment)
        for child in message.children:
            self._add_message_item(item, child)
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
            # Same tint as the pages, so the navigator and the page column agree
            # about which message a row came from.
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

        # A source, or a message nested inside one: its checkbox governs the whole
        # subtree, so unticking a forwarded email leaves out everything inside it.
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
        """Every segment a navigator row stands for, itself or its subtree."""
        if item.data(0, ROW_ROLE) == "segment":
            segment_id = item.data(0, Qt.UserRole)
            return [segment_id] if isinstance(segment_id, int) else []
        found: list[int] = []
        for index in range(item.childCount()):
            found.extend(self._item_segment_ids(item.child(index)))
        return found

    def first_page_of(self, item: QTreeWidgetItem) -> int | None:
        """The row of the earliest page belonging to a navigator row.

        Earliest in the *page list*, not in the source: the user may have moved
        pages around, and the point of the jump is to land on the page where it
        lives now rather than where it started.
        """
        wanted = set(self._item_segment_ids(item))
        if not wanted:
            return None
        for position, page in enumerate(self.model.pages):
            if page.segment_id in wanted:
                return position
        return None

    def jump_to_selected(self) -> bool:
        """Take the page column to whatever the navigator has selected.

        Returns whether it moved, so a caller - or a test - can tell the
        difference between "jumped" and "there was nothing to jump to".
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
        """Show and select one page row, scrolling it into view."""
        row = self.page_list.item(position)
        if row is None:
            return
        self.page_list.clearSelection()
        row.setSelected(True)
        self.page_list.setCurrentItem(row)
        self.page_list.scrollToItem(row, QAbstractItemView.PositionAtCenter)
        # A selection that did not change produces no signal, so refresh the
        # preview directly rather than relying on it.
        self._update_preview()

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
            tint = node_colour(node, self.model.group_owner(node))
            trail = self.model.group_trail(node)
            item = QListWidgetItem(row_description(node, page, position, trail))
            item.setData(Qt.UserRole, (page.segment_id, page.index, page.rotation))
            item.setToolTip("%s\npage %d of %d"
                            % (node.label if node else "?",
                               page.index + 1,
                               node.page_count if node else 0))
            # The row tint identifies the message the page came from, so an
            # email's own pages read as one block and a forwarded message inside
            # it reads as a block of a different colour.
            item.setBackground(QBrush(tint))
            item.setForeground(QBrush(QColor("#202020")))
            item.setSizeHint(QSize(0, ROW_HEIGHT))
            self.page_list.addItem(item)

        self.page_list.blockSignals(False)
        # The list was rebuilt, so every row is a placeholder again.
        self._thumb_range = None
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
        """Render the thumbnails near the viewport, not the whole document.

        A folder of several hundred files can run to tens of thousands of pages.
        Rendering a thumbnail for every one of them, every time the list is
        rebuilt, is what makes a large batch unusable - so only what the user can
        actually see (plus a margin, so scrolling stays ahead of the rendering) is
        asked for, and scrolling asks for more.
        """
        if self._thumbs is not None and self._thumbs.isRunning():
            if self._thumb_range == self._visible_page_range():
                return
            self._thumbs.cancel()
            self._thumbs.wait(2000)

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

        self._thumbs = ThumbnailWorker(jobs, self.thumbs, self)
        self._thumbs.ready.connect(self._on_thumbnail)
        self._thumbs.finished.connect(self._on_thumbnails_finished)
        self._thumbs.start()

    def _on_thumbnails_finished(self) -> None:
        """Say so if any page could not be rendered as a thumbnail.

        A missing thumbnail is otherwise invisible, and an invisible gap cannot be
        told apart from a page that was never there.
        """
        failures = getattr(self.thumbs, "failed", 0)
        if failures:
            self.status.showMessage(
                "%d page(s) could not be rendered as thumbnails (%s). "
                "The pages themselves are unaffected."
                % (failures, getattr(self.thumbs, "last_error", "") or "unknown")
            )

    def _visible_page_range(self) -> tuple[int, int]:
        """The rows worth rendering: what is on screen, padded either side."""
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
        # A single screenful is the floor, so the first paint is never empty even
        # before the widget has been laid out.
        if last - first < VISIBLE_PAGES:
            last = min(count, first + VISIBLE_PAGES)
        return first, last

    def _on_pages_scrolled(self) -> None:
        """Ask for more thumbnails once scrolling settles."""
        self._thumb_timer.start()

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
        if image is not None and page.rotation:
            # Show the page as it will be written, not as it sits in the source
            # PDF - otherwise rotating a page appears to do nothing.
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

    def _ensure_scratch(self, reset: bool = False) -> Path:
        """A scratch directory that survives incremental conversions.

        Segments produced earlier point into this directory, so it must only be
        cleared when everything is being converted again.
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
        return self.scratch

    def _next_run_dir(self) -> Path:
        """A fresh subdirectory per run.

        Intermediate files are named by index within their batch, so reusing a
        directory across runs would overwrite them.
        """
        scratch = self._ensure_scratch()
        self._run_counter += 1
        run_dir = scratch / ("run%d" % self._run_counter)
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def start_conversion(self) -> None:
        """Convert everything again from scratch, discarding current pages."""
        if not self.selected_sources:
            return
        self._converted_sources.clear()
        self._run_conversion(self.selected_sources, replace=True)

    def _convert_new_sources(self) -> None:
        """Convert only sources added since the last run.

        Adding a file must not throw away page edits the user has already made,
        so this appends rather than rebuilding.
        """
        pending = [p for p in self.selected_sources if str(p) not in self._converted_sources]
        if pending:
            self._run_conversion(pending, replace=False)

    def _run_conversion(self, sources: list[Path], replace: bool) -> None:
        if self._conversion is not None and self._conversion.isRunning():
            # Files arrived mid-conversion; pick them up when it finishes.
            self._pending_convert = True
            return

        if replace:
            self._ensure_scratch(reset=True)
            self.model.clear()
            self.thumbs.clear()
            self.refresh_pages()

        # Whatever the last run failed on has been superseded by this one.
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
        """Convert anything that arrived while a run was in progress."""
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
                    # The pipeline verified this when it produced the segment;
                    # count it here only if it came from somewhere that did not.
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
            # The user asked to stop; listing what did not get converted would be
            # reporting the thing they just cancelled.
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
                # Worth saying out loud: it means this machine dislikes several
                # LibreOffice processes at once, and the second pass cost time.
                message += (" LibreOffice needed a second, single-file pass on %d "
                            "batch(es) - set CONSOLIDATOR_WORKERS=2 if that keeps "
                            "happening." % retried)
            self.status.showMessage(message)

    def _report_failures(self, failures: list) -> None:
        """Say what failed without stopping the interface to do it."""
        self._failures = list(failures)
        first = self._failures[0]
        self.lbl_banner.setText(
            "%d item(s) could not be converted, starting with %s. "
            "Everything else was converted."
            % (len(self._failures), first.source.name)
        )
        # A conversion failure is retryable, unlike a merge that had to skip
        # something, so the button comes back.
        self.btn_banner_retry.setVisible(True)
        self.banner.setVisible(True)

    def failure_lines(self) -> list[str]:
        """One line per failure, for the details view."""
        lines = []
        for segment in self._failures[:200]:
            lines.append("%s\n    %s" % (segment.display, segment.detail or "failed"))
        if len(self._failures) > 200:
            lines.append("\u2026 and %d more" % (len(self._failures) - 200))
        return lines

    def show_failure_details(self) -> None:
        """The list of failures, on request rather than on top of everything."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Items that could not be converted")
        box.setText("%d item(s) failed. The rest were converted." % len(self._failures))
        box.setDetailedText("\n\n".join(self.failure_lines()))
        box.exec()

    def retry_failed(self) -> None:
        """Convert just the failed sources again, keeping everything else."""
        unique: list[Path] = []
        seen = set()
        for segment in self._failures:
            if str(segment.source) not in seen:
                seen.add(str(segment.source))
                unique.append(segment.source)
        self.banner.setVisible(False)
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
                    # The pipeline verified this when it produced the segment;
                    # count it here only if it came from somewhere that did not.
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
            # The user asked for this, and the status bar already says it worked;
            # a dialog to confirm a click is just one more thing to dismiss.
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
        self._begin_busy("Assembling %d page(s)\u2026" % len(self.model.pages))
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
        self._end_progress()
        self.btn_create.setEnabled(True)
        message = "Wrote %d page(s)" % report.pages
        if report.separators:
            message += " including %d separator(s)" % report.separators
        self.status.showMessage(message + ".")

        skipped = list(getattr(report, "skipped", []) or [])
        if skipped:
            # Anything the merge left out has to be said out loud: a document that
            # is quietly short is worse than one that fails.
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

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning if skipped else QMessageBox.Information)
        box.setWindowTitle("Done")
        box.setText("%s.\n\n%s" % (message, self.output_path))
        if skipped:
            box.setDetailedText(
                "These were left out of the merged PDF:\n\n"
                + "\n".join("%s\n    %s" % (label, why) for label, why in skipped)
            )
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

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Shut down promptly.

        Waiting the full cancel timeout for each worker made closing feel
        sluggish, and deleting the scratch tree on the GUI thread could take
        seconds for a large run. So: ask everything to stop, wait only briefly,
        and clean up in the background.
        """
        import threading

        self._autoconvert.stop()

        workers = [w for w in (self._conversion, self._merge, self._thumbs) if w is not None]
        for worker in workers:
            if worker.isRunning() and hasattr(worker, "cancel"):
                worker.cancel()

        deadline = time.monotonic() + 0.5
        for worker in workers:
            if worker.isRunning():
                remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
                worker.wait(remaining_ms)

        scratch = self.scratch
        self.scratch = None
        if scratch and Path(scratch).exists():
            from .. import libreoffice

            # A daemon thread so a slow delete cannot hold up the exit.
            threading.Thread(
                target=libreoffice._remove_tree, args=(Path(scratch),), daemon=True
            ).start()

        super().closeEvent(event)
