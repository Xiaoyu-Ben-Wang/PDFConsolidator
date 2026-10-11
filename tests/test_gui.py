"""GUI tests.

The document model is deliberately Qt-free, so most of it is tested without a
display. The window tests need a QApplication but not a visible window.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pypdf import PdfWriter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from consolidator.gui.document import DocumentModel, PageRef  # noqa: E402
from consolidator.model import DOCUMENT  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def make_pdf(path: Path, pages: int) -> Path:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    with open(path, "wb") as handle:
        writer.write(handle)
    return path


@pytest.fixture
def model(tmp_path):
    """Two documents: A with 3 pages, B with 2."""
    a = make_pdf(tmp_path / "a.pdf", 3)
    b = make_pdf(tmp_path / "b.pdf", 2)
    m = DocumentModel()
    m.add_segment(Path("A.docx"), "A", a, True, page_count=3)
    m.add_segment(Path("B.docx"), "B", b, True, page_count=2)
    return m


# ------------------------------------------------------------ document model


def test_segments_expand_into_pages(model):
    assert model.page_count == 5
    assert [(p.segment_id, p.index) for p in model.pages] == [
        (0, 0), (0, 1), (0, 2), (1, 0), (1, 1)
    ]


def test_failed_segment_contributes_no_pages(tmp_path):
    m = DocumentModel()
    m.add_segment(Path("bad.docx"), "bad", None, False, detail="boom")
    assert m.page_count == 0
    assert not m.segments[0].ok


def test_rotate_accumulates_and_wraps(model):
    model.rotate([0], 90)
    model.rotate([0], 90)
    assert model.pages[0].rotation == 180
    model.rotate([0], 180)
    assert model.pages[0].rotation == 0


def test_rotate_only_affects_selected(model):
    model.rotate([1], 90)
    assert [p.rotation for p in model.pages] == [0, 90, 0, 0, 0]


def test_delete_removes_pages(model):
    removed = model.delete([0, 3])
    assert removed == 2
    assert model.page_count == 3
    assert [(p.segment_id, p.index) for p in model.pages] == [(0, 1), (0, 2), (1, 1)]


def test_duplicate_inserts_after(model):
    model.duplicate([0])
    assert model.page_count == 6
    assert (model.pages[0].segment_id, model.pages[1].segment_id) == (0, 0)
    assert model.pages[0].index == model.pages[1].index


def test_move_up_and_down(model):
    model.move_up([3])           # B page 1 to the front of its own block
    assert model.pages[2].segment_id == 1
    model.move_down([2])
    assert model.pages[3].segment_id == 1


def test_group_by_source_clusters_pages(model):
    # Interleave: A0, B0, A1, B1, A2
    model.pages = [model.pages[0], model.pages[3], model.pages[1],
                   model.pages[4], model.pages[2]]
    model.group_by_source()
    assert [p.segment_id for p in model.pages] == [0, 0, 0, 1, 1]
    # Each document keeps its own page order.
    assert [p.index for p in model.pages[:3]] == [0, 1, 2]


def test_group_by_source_is_stable_on_already_grouped(model):
    before = [(p.segment_id, p.index) for p in model.pages]
    model.group_by_source()
    assert [(p.segment_id, p.index) for p in model.pages] == before


def test_toggle_segment_off_then_on(model):
    model.set_segment_enabled(0, False)
    assert model.page_count == 2
    model.set_segment_enabled(0, True)
    assert model.page_count == 5


def test_to_entries_regroups_interleaved_pages(model):
    """Pages from A, then B, then A again must become three entries."""
    model.pages = [model.pages[0], model.pages[3], model.pages[1]]
    entries = model.to_entries()
    assert [e.label for e in entries] == ["A", "B", "A"]
    assert [[s.index for s in e.pages] for e in entries] == [[0], [0], [1]]


def test_to_entries_carries_rotation(model):
    model.rotate([0], 90)
    entries = model.to_entries()
    assert entries[0].pages[0].rotation == 90


def test_to_entries_skips_disabled_segments(model):
    model.set_segment_enabled(0, False)
    entries = model.to_entries()
    assert len(entries) == 1 and entries[0].label == "B"


def test_interleaving_preserves_output_order(model):
    """The assembled order must match the on-screen order exactly."""
    model.pages = [model.pages[3], model.pages[0], model.pages[4]]
    entries = model.to_entries()
    ordered = [(e.label, s.index) for e in entries for s in e.pages]
    assert ordered == [("B", 0), ("A", 0), ("B", 1)]


# --------------------------------------------------------------- main window


@pytest.fixture
def window(qapp):
    from consolidator.gui.main_window import MainWindow

    win = MainWindow()
    yield win
    win.close()


def test_window_constructs(window):
    assert window.windowTitle() == "PDF Consolidator"
    assert window.page_list.count() == 0


def test_page_list_is_a_single_vertical_column(window):
    """The user asked for one column with a preview beside it."""
    from PySide6.QtWidgets import QListView

    assert window.page_list.viewMode() == QListView.ListMode


def test_drag_reordering_is_enabled(window):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QAbstractItemView, QListView

    assert window.page_list.dragDropMode() == QAbstractItemView.InternalMove
    assert window.page_list.defaultDropAction() == Qt.MoveAction
    # Static movement keeps rows aligned in the column while still allowing
    # drag reordering.
    assert window.page_list.movement() == QListView.Static


def test_rows_are_colour_coded_by_source(window, tmp_path):
    a = make_pdf(tmp_path / "alpha.pdf", 1)
    b = make_pdf(tmp_path / "beta.pdf", 1)
    window.model.add_segment(Path("alpha.docx"), "alpha", a, True, page_count=1)
    window.model.add_segment(Path("beta.docx"), "beta", b, True, page_count=1)

    window.refresh_pages()
    first = window.page_list.item(0).background().color()
    second = window.page_list.item(1).background().color()
    assert first != second, "different sources must be distinguishable"


def test_row_label_names_the_origin(window, tmp_path):
    pdf = make_pdf(tmp_path / "alpha.pdf", 2)
    window.model.add_segment(Path("alpha.docx"), "alpha", pdf, True, page_count=2)
    window.refresh_pages()

    assert "alpha.docx" in window.page_list.item(0).text()
    assert window.page_list.item(0).text().startswith("1.")


def test_rotation_shows_in_the_row(window, tmp_path):
    pdf = make_pdf(tmp_path / "alpha.pdf", 1)
    window.model.add_segment(Path("alpha.docx"), "alpha", pdf, True, page_count=1)
    window.refresh_pages()

    window.page_list.clearSelection()
    window.page_list.item(0).setSelected(True)
    window.rotate_selected(90)

    assert "[90°]" in window.page_list.item(0).text()


def test_dropped_order_is_adopted(window, tmp_path):
    """Simulates what Qt does on drop: rows move, then rowsMoved fires."""
    for name, count in (("a.docx", 2), ("b.docx", 2)):
        pdf = make_pdf(tmp_path / (name + ".pdf"), count)
        window.model.add_segment(Path(name), name, pdf, True, page_count=count)
    window.refresh_pages()

    before = [(p.segment_id, p.index) for p in window.model.pages]

    # Move the last row to the top, as a drag would.
    item = window.page_list.takeItem(window.page_list.count() - 1)
    window.page_list.insertItem(0, item)
    window._on_pages_reordered()

    after = [(p.segment_id, p.index) for p in window.model.pages]
    assert after[0] == before[-1]
    assert after[1:] == before[:-1]


def test_group_by_document_button_reorders(window, tmp_path):
    for name in ("a.docx", "b.docx"):
        pdf = make_pdf(tmp_path / (name + ".pdf"), 2)
        window.model.add_segment(Path(name), name, pdf, True, page_count=2)
    window.refresh_pages()

    # Interleave A B A B
    window.model.pages = [window.model.pages[0], window.model.pages[2],
                          window.model.pages[1], window.model.pages[3]]
    window.group_by_document()

    assert [p.segment_id for p in window.model.pages] == [0, 0, 1, 1]


def test_preview_updates_on_selection(window, tmp_path):
    pdf = make_pdf(tmp_path / "alpha.pdf", 2)
    window.model.add_segment(Path("alpha.docx"), "alpha", pdf, True, page_count=2)
    window.refresh_pages()

    window.page_list.clearSelection()
    window.page_list.item(0).setSelected(True)
    window._update_preview()

    assert not window.preview.pixmap().isNull(), "preview should render a page"
    assert "alpha" in window.preview_caption.text()
    assert "Page 1 of 2" in window.preview_caption.text()


def test_preview_clears_with_no_selection(window, tmp_path):
    pdf = make_pdf(tmp_path / "alpha.pdf", 1)
    window.model.add_segment(Path("alpha.docx"), "alpha", pdf, True, page_count=1)
    window.refresh_pages()

    window.page_list.clearSelection()
    window._update_preview()
    assert window.preview.pixmap().isNull()


def test_policy_exclusions_are_not_failures():
    from consolidator.gui.main_window import is_policy_exclusion
    from consolidator.model import ATTACHMENT, Segment

    excluded = Segment(Path("x.msg"), "x - archive.zip", ATTACHMENT, None, False,
                       "excluded by the extraction policy (other)")
    failed = Segment(Path("x.msg"), "x - body", ATTACHMENT, None, False, "LibreOffice timed out")

    assert is_policy_exclusion(excluded)
    assert not is_policy_exclusion(failed)


# ------------------------------------------------- row labels and grouping


def test_row_label_leads_with_the_original_filename(tmp_path):
    """The filename must lead the row so the column sorts and scans by source."""
    from consolidator.gui.main_window import row_description
    from consolidator.model import Segment

    node = Segment(Path("Q3 report.docx"), "Q3 report", DOCUMENT, tmp_path / "a.pdf", True)
    assert row_description(node, PageRef(0, 0), 3) == "3.  Q3 report.docx"


def test_row_label_names_the_email_part(tmp_path):
    """An email's body and attachments must be distinguishable, and the repeated
    message stem must not be repeated on every row."""
    from consolidator.gui.main_window import row_description
    from consolidator.model import ATTACHMENT, Segment

    node = Segment(Path("invoice.msg"), "invoice - invoice.pdf", ATTACHMENT,
                   tmp_path / "a.pdf", True)
    text = row_description(node, PageRef(0, 3), 7)
    assert text.startswith("7.  invoice.msg")
    assert "invoice.pdf" in text
    assert text.count("invoice") == 2, "the message stem should not be repeated"


def test_row_label_marks_rotation(tmp_path):
    from consolidator.gui.main_window import row_description
    from consolidator.model import Segment

    node = Segment(Path("a.docx"), "a", DOCUMENT, tmp_path / "a.pdf", True)
    page = PageRef(0, 0)
    page.rotation = 90
    assert "[90\u00b0]" in row_description(node, page, 1)


def test_colour_is_stable_and_source_specific():
    from consolidator.gui.main_window import source_colour

    assert source_colour("a.docx") == source_colour("a.docx")
    assert source_colour("a.docx") != source_colour("b.docx")


def test_embedded_messages_are_drilled_into_by_default():
    """A forwarded email is ordinary mail, so its contents are extracted."""
    from consolidator import email_items

    policy = email_items.ExtractionPolicy()
    assert policy.embedded is True
    assert policy.max_depth >= 2, "a forwarded email must not stop at the first level"
    assert policy.max_depth == 4, "forwards of forwards are common enough to matter"


def test_the_file_dialog_filter_covers_everything_that_converts():
    """The dialog list is built from the converters, so it cannot drift."""
    from consolidator import convert
    from consolidator.gui.main_window import SUPPORTED_FILTER

    for ext in convert.SUPPORTED_EXTS:
        assert "*" + ext in SUPPORTED_FILTER, "%s missing from the file dialog" % ext
    assert "*.xlsm" in SUPPORTED_FILTER, "the format that prompted this"
    assert "*.htm" in SUPPORTED_FILTER and "*.html" in SUPPORTED_FILTER
    assert "*.txt" in SUPPORTED_FILTER


# ------------------------------------------------- nested messages in the UI


def add_forwarded_message(model, tmp_path, outer: Path) -> str:
    """Give ``model`` an email carrying a forwarded message.

    The forwarded message has a body of two pages and an attachment of one, so
    the tests can tell the levels of the nesting apart by page counts.
    """
    inner = "staged/forwarded.msg"
    model.add_segment(outer, "outer - body", make_pdf(tmp_path / "outer.pdf", 1), True,
                      kind="body", page_count=1, group=str(outer),
                      group_label="outer.msg")
    model.add_segment(outer, "forwarded - body",
                      make_pdf(tmp_path / "inner_body.pdf", 2), True,
                      kind="body", page_count=2, group=inner, parent=str(outer),
                      group_label="forwarded.msg", depth=1)
    model.add_segment(outer, "forwarded - note.pdf",
                      make_pdf(tmp_path / "inner_note.pdf", 1), True,
                      kind="attachment", page_count=1, group=inner, parent=str(outer),
                      group_label="forwarded.msg", depth=1)
    return inner


@pytest.fixture
def nested_model(tmp_path):
    outer = Path("outer.msg")
    model = DocumentModel()
    inner = add_forwarded_message(model, tmp_path, outer)
    return model, outer, inner


def test_a_forwarded_message_is_its_own_node(nested_model):
    model, outer, inner = nested_model
    top_level, messages = model.navigator(outer)

    assert [s.group for s in top_level] == [str(outer)], \
        "the carrier's own pages stay at its level"
    assert len(messages) == 1
    message = messages[0]
    assert message.label == "forwarded.msg"
    assert message.parent == str(outer)
    assert [s.group for s in message.segments] == [inner, inner]
    assert message.children == []
    assert message.page_count == 3, "a node counts every page inside it"
    assert [s.group for s in message.descendants()] == [inner, inner]


def test_a_forwarded_message_gets_a_colour_of_its_own(nested_model):
    from consolidator.gui.main_window import node_colour

    model, outer, _inner = nested_model
    top_level, messages = model.navigator(outer)
    carrier = top_level[0]
    body, note = messages[0].segments

    carrier_colour = node_colour(carrier, model.group_owner(carrier))
    body_colour = node_colour(body, model.group_owner(body))

    assert carrier_colour != body_colour, "a nested message must not blend into its carrier"
    assert node_colour(note, model.group_owner(note)) == body_colour, \
        "every page of one message shares its colour"


def test_a_nested_row_names_the_messages_it_was_found_inside(nested_model):
    from consolidator.gui.main_window import row_description

    model, outer, _inner = nested_model
    _top_level, messages = model.navigator(outer)
    body = messages[0].segments[0]

    text = row_description(body, PageRef(0, 0), 2, model.group_trail(body))
    assert text.startswith("2.  outer.msg")
    assert "forwarded.msg" in text and "body" in text
    assert "\u203a" in text, "the nesting has to be visible in the row"


def test_the_navigator_nests_a_forwarded_message(window, tmp_path):
    outer = Path("outer.msg")
    window.selected_sources = [outer]
    add_forwarded_message(window.model, tmp_path, outer)
    window.refresh_tree()

    root = window.tree.topLevelItem(0)
    assert root.text(0) == "outer.msg"
    assert root.childCount() == 2, "the carrier's body, then the forwarded message"
    assert root.child(0).text(0).strip() == "body"

    message = root.child(1)
    assert message.text(0).strip() == "forwarded.msg"
    assert message.childCount() == 2, "the nested body and its attachment"
    assert message.text(1) == "3"


def test_unticking_a_nested_message_leaves_out_everything_inside_it(window, tmp_path):
    from PySide6.QtCore import Qt

    outer = Path("outer.msg")
    window.selected_sources = [outer]
    add_forwarded_message(window.model, tmp_path, outer)
    window.refresh_tree()
    window.refresh_pages()
    assert window.model.page_count == 4

    window.tree.topLevelItem(0).child(1).setCheckState(0, Qt.Unchecked)

    assert window.model.page_count == 1, "only the carrier's own body should remain"
    assert window.page_list.count() == 1


# ------------------------------------------------------- scale and progress


def test_indexes_keep_up_with_removal(tmp_path):
    """The lookup indexes must not go stale when segments come and go."""
    model = DocumentModel()
    a = model.add_segment(Path("a.docx"), "a", make_pdf(tmp_path / "a.pdf", 2),
                          True, page_count=2)
    b = model.add_segment(Path("b.docx"), "b", make_pdf(tmp_path / "b.pdf", 1),
                          True, page_count=1)

    assert model.segment(b.id) is b
    model.drop_source(Path("a.docx"))
    assert model.segment(a.id) is None
    assert model.segment(b.id) is b
    assert model.page_count == 1

    model.remove_failed_for(Path("b.docx"))
    assert model.segment(b.id) is b, "a successful segment survives a retry cleanup"

    model.clear()
    assert model.segment(b.id) is None and model.page_count == 0


def test_progress_bar_counts_files_and_shows_what_is_happening(window):
    # isHidden() rather than isVisible(): the test window is never shown, so
    # every child would report itself invisible.
    window._begin_progress(4, "Converting 4 file(s)\u2026")
    assert not window.progress.isHidden()
    assert (window.progress.minimum(), window.progress.maximum()) == (0, 4)
    assert not window.btn_cancel.isHidden(), "a long batch has to be stoppable"

    window._on_progress(3, 4, "third.pdf")
    assert window.progress.value() == 3
    assert window.progress.text() == "3 / 4"
    assert "third.pdf" in window.status.currentMessage()

    window._end_progress()
    assert window.progress.isHidden()
    assert window.btn_cancel.isHidden()


def test_thumbnails_survive_two_threads_and_a_tiny_document_cache(tmp_path):
    """The reported bug: a page that "fails to render" on a large folder.

    The thumbnail worker renders on one thread while the preview renders on the
    GUI thread. The document cache evicts and *closes* documents once it is full -
    which a large folder always fills - so without a lock a document could be
    closed midway through rendering it. pypdfium2 raises, the failure was
    swallowed, and those thumbnails simply never appeared.
    """
    import threading
    from consolidator.gui.thumbs import ThumbnailCache

    pdfs = [make_pdf(tmp_path / ("doc%d.pdf" % i), 3) for i in range(8)]
    cache = ThumbnailCache(document_cache=2, image_cache=8)
    errors: list[str] = []

    def hammer(offset: int) -> None:
        for round_number in range(6):
            for index, pdf in enumerate(pdfs):
                for page in range(3):
                    image = cache.image(pdf, page)
                    if image is None:
                        errors.append("worker %d: %s page %d" % (offset, pdf.name, page))

    def preview() -> None:
        for round_number in range(6):
            for pdf in pdfs:
                cache.render(pdf, 0, 400)

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(3)]
    threads.append(threading.Thread(target=preview))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, "renders failed while the cache was evicting: %s" % errors[:5]
    assert cache.failed == 0, cache.last_error


def test_a_failed_thumbnail_is_reported_not_hidden(window, tmp_path):
    """A silent gap cannot be told from a page that was never there."""
    pdf = make_pdf(tmp_path / "alpha.pdf", 1)
    window.model.add_segment(Path("alpha.docx"), "alpha", pdf, True, page_count=1)
    window.refresh_pages()

    window.thumbs.failed = 3
    window.thumbs.last_error = "ValueError: page is closed"
    window._on_thumbnails_finished()

    message = window.status.currentMessage()
    assert "3 page(s) could not be rendered" in message
    assert "unaffected" in message


def test_a_clean_thumbnail_run_says_nothing(window, tmp_path):
    pdf = make_pdf(tmp_path / "alpha.pdf", 1)
    window.model.add_segment(Path("alpha.docx"), "alpha", pdf, True, page_count=1)
    window.refresh_pages()

    window.thumbs.failed = 0
    window.status.showMessage("unchanged")
    window._on_thumbnails_finished()

    assert window.status.currentMessage() == "unchanged"


def test_only_the_visible_pages_are_rendered(window, tmp_path):
    """Several hundred files must not mean several hundred rendered thumbnails."""
    from consolidator.gui.main_window import (
        THUMBNAIL_MARGIN, VISIBLE_PAGES,
    )

    pdf = make_pdf(tmp_path / "big.pdf", 1)
    for index in range(300):
        window.model.add_segment(Path("file%d.docx" % index), "file%d" % index,
                                 pdf, True, page_count=1)
    window.refresh_pages()

    assert window.page_list.count() == 300, "every page still gets a row"
    first, last = window._thumb_range
    limit = VISIBLE_PAGES + 2 * THUMBNAIL_MARGIN
    assert 0 <= first < last <= 300
    assert last - first <= limit, \
        "asked to render %d thumbnails for a %d-row list" % (last - first, 300)


def test_cancel_stops_a_batch_part_way(window):
    """Cancelling asks the running worker to stop at the next file."""
    asked = []

    class FakeWorker:
        def cancel(self):
            asked.append(True)

        def isRunning(self):
            return True

        def wait(self, *_args):     # consulted by MainWindow.closeEvent
            return True

    window.btn_cancel.setVisible(True)
    window.btn_cancel.setEnabled(True)
    window._conversion = FakeWorker()

    window.cancel_conversion()

    assert asked == [True]
    assert not window.btn_cancel.isEnabled(), "no second cancel while stopping"
    assert "Stopping" in window.status.currentMessage()


def test_a_cancelled_run_is_reported_as_stopped_not_as_failures(window):
    """Whatever did not get converted was cancelled, not broken."""
    from types import SimpleNamespace

    report = SimpleNamespace(sources=[], all_segments=[], segments=[], cancelled=True)
    window._on_converted(report)

    assert "Stopped." in window.status.currentMessage()


def test_a_second_pass_is_reported_so_the_user_can_act_on_it(window):
    """If LibreOffice needed a single-file pass, say so and suggest the remedy."""
    from types import SimpleNamespace

    report = SimpleNamespace(sources=[], all_segments=[], segments=[],
                             cancelled=False, retried_batches=3)
    window._on_converted(report)

    message = window.status.currentMessage()
    assert "second, single-file pass on 3 batch(es)" in message
    assert "CONSOLIDATOR_WORKERS=2" in message


def test_a_clean_run_says_nothing_about_a_second_pass(window):
    from types import SimpleNamespace

    report = SimpleNamespace(sources=[], all_segments=[], segments=[],
                             cancelled=False, retried_batches=0)
    window._on_converted(report)

    assert "CONSOLIDATOR_WORKERS" not in window.status.currentMessage()


def test_a_merge_that_skipped_something_says_so(window, monkeypatch):
    """A document that is quietly short is worse than one that fails."""
    from types import SimpleNamespace
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)

    report = SimpleNamespace(pages=7, separators=0,
                             skipped=[("locked.pdf", "PDF is encrypted"),
                                      ("secret.pdf", "needs a password")])
    window._on_merged(report)

    assert "2 item(s) could not be merged" in window.status.currentMessage()
    assert not window.banner.isHidden()
    assert "locked.pdf" in window.lbl_banner.text()
    assert window._merge_skips == report.skipped


def test_a_merge_without_skips_hides_the_banner(window, monkeypatch):
    from types import SimpleNamespace
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)

    window.banner.setVisible(True)
    window._on_merged(SimpleNamespace(pages=3, separators=1, skipped=[]))

    assert window.banner.isHidden()
    assert "Wrote 3 page(s) including 1 separator(s)" in window.status.currentMessage()


# --------------------------------------------------------- failure reporting


def test_failures_are_reported_without_blocking_the_window(window):
    """A few failures in a big batch must not stop the interface to announce it."""
    from consolidator.model import ATTACHMENT, Segment

    failures = [
        Segment(Path("a.msg"), "a - body", ATTACHMENT, None, False,
                "LibreOffice timed out"),
        Segment(Path("b.msg"), "b - body", ATTACHMENT, None, False,
                "no output produced"),
    ]
    window._report_failures(failures)

    assert not window.banner.isHidden()
    assert "2 item(s)" in window.lbl_banner.text()
    assert "a.msg" in window.lbl_banner.text()
    assert window.failure_lines() == [
        "a - body\n    LibreOffice timed out",
        "b - body\n    no output produced",
    ]


def test_retry_from_the_banner_covers_each_source_once(window):
    """Retrying a message with three broken parts must not convert it three times."""
    from consolidator.model import ATTACHMENT, Segment

    asked = []
    window.retry_sources = lambda sources: asked.append(list(sources))
    window._failures = [
        Segment(Path("a.msg"), "a - body", ATTACHMENT, None, False, "x"),
        Segment(Path("a.msg"), "a - note.pdf", ATTACHMENT, None, False, "y"),
        Segment(Path("b.msg"), "b - body", ATTACHMENT, None, False, "z"),
    ]
    window.banner.setVisible(True)

    window.retry_failed()

    assert asked == [[Path("a.msg"), Path("b.msg")]]
    assert window.banner.isHidden(), "the banner goes away once its retry is running"


def test_a_long_failure_list_stays_readable(window):
    from consolidator.model import ATTACHMENT, Segment

    window._report_failures([
        Segment(Path("f%d.msg" % index), "f%d - body" % index, ATTACHMENT, None,
                False, "boom")
        for index in range(250)
    ])

    lines = window.failure_lines()
    assert len(lines) == 201, "the list is capped, with a count of the remainder"
    assert "and 50 more" in lines[-1]


# ---------------------------------------------------------------- licensing


def test_about_text_states_the_licence_and_the_warranty_position():
    from consolidator.gui.licences import about_text

    text = about_text()
    assert "GNU General Public License, version 3 or later" in text
    assert "WITHOUT ANY WARRANTY" in text
    assert "The authors are not liable for any damages." in text
    assert "2026 Xiaoyu-Ben-Wang" in text


def test_licences_are_found_beside_a_frozen_executable(qapp, tmp_path, monkeypatch):
    """In the bundle the texts sit beside the exe, not in a checkout."""
    from consolidator.gui import licences

    bundle = tmp_path / "PDFConsolidator"
    (bundle / "libreoffice").mkdir(parents=True)
    (bundle / "LICENSE").write_text("gpl text", encoding="utf-8")
    (bundle / "THIRD-PARTY-NOTICES.md").write_text("notices text", encoding="utf-8")
    (bundle / "libreoffice" / "license.txt").write_text("libreoffice text",
                                                        encoding="utf-8")

    monkeypatch.setattr(licences.sys, "frozen", True, raising=False)
    monkeypatch.setattr(licences.sys, "executable", str(bundle / "PDFConsolidator.exe"))

    assert [title for title, _path in licences.documents()] == [
        "Third-party notices", "GNU General Public License v3.0", "LibreOffice",
    ]

    dialog = licences.LicencesDialog()
    assert dialog.view.toPlainText().strip() == "notices text"
    dialog.show_document("GNU General Public License v3.0", bundle / "LICENSE")
    assert dialog.view.toPlainText().strip() == "gpl text"


def test_the_window_offers_the_licences(qapp):
    """The About button and the Help menu both have to exist and be wired."""
    from consolidator.gui.main_window import MainWindow

    win = MainWindow()
    try:
        assert win.btn_about.text() == "About"
        assert win.act_about.text().startswith("About")
        assert win.act_licences.text().startswith("Licences")
        help_menu = win.menuBar().actions()[0].menu()
        assert [action.text() for action in help_menu.actions()] == [
            win.act_about.text(), win.act_licences.text(),
        ]
    finally:
        win.close()


# ------------------------------------------------- navigator to page row


def _tree_row(window, text: str):
    """The navigator row whose label contains *text*."""
    queue = [window.tree.topLevelItem(i) for i in range(window.tree.topLevelItemCount())]
    while queue:
        item = queue.pop(0)
        if text in item.text(0):
            return item
        queue.extend(item.child(i) for i in range(item.childCount()))
    raise AssertionError("no navigator row matching %r" % text)


def _jump(window, text: str) -> bool:
    """Select a navigator row and press the jump button."""
    window.tree.setCurrentItem(_tree_row(window, text))
    return window.jump_to_selected()


def test_the_jump_is_an_icon_button_not_a_row_click(window, tmp_path):
    """Clicking a row must not move the page list; the button is the action."""
    from PySide6.QtWidgets import QStyle

    assert window.btn_jump.toolTip().startswith("Show the selected item's pages")
    assert not window.btn_jump.icon().isNull(), "the button is an icon"
    assert window.btn_jump.autoRaise(), "a bare icon, not a chunky button"

    pdf = make_pdf(tmp_path / "alpha.pdf", 2)
    window.selected_sources = [Path("alpha.pdf")]
    window.model.add_segment(Path("alpha.pdf"), "alpha", pdf, True, page_count=2)
    window.refresh_tree()
    window.refresh_pages()
    window.page_list.setCurrentRow(1)

    window.tree.setCurrentItem(_tree_row(window, "alpha.pdf"))
    window.tree.itemClicked.emit(_tree_row(window, "alpha.pdf"), 0)

    assert window.page_list.currentRow() == 1, "clicking a row must not jump"


def test_the_jump_button_needs_a_selection(window):
    window.tree.clearSelection()
    assert window.jump_to_selected() is False
    assert "Select a source" in window.status.currentMessage()


def test_clicking_a_source_jumps_to_its_first_page(window, tmp_path):
    first, second = Path("alpha.pdf"), Path("beta.pdf")
    window.selected_sources = [first, second]
    window.model.add_segment(first, "alpha", make_pdf(tmp_path / "a.pdf", 2),
                             True, page_count=2)
    window.model.add_segment(second, "beta", make_pdf(tmp_path / "b.pdf", 1),
                             True, page_count=1)
    window.refresh_tree()
    window.refresh_pages()

    assert _jump(window, "beta.pdf") is True

    assert window.page_list.currentRow() == 2
    assert [window.page_list.row(i) for i in window.page_list.selectedItems()] == [2]


def test_clicking_a_part_jumps_to_that_part_not_the_next_one(window, tmp_path):
    source = Path("mail.msg")
    window.selected_sources = [source]
    window.model.add_segment(source, "mail - body", make_pdf(tmp_path / "b.pdf", 1),
                             True, kind="body", page_count=1,
                             group=str(source), group_label="mail.msg")
    window.model.add_segment(source, "mail - invoice.pdf",
                             make_pdf(tmp_path / "i.pdf", 3), True,
                             kind="attachment", page_count=3,
                             group=str(source), group_label="mail.msg")
    window.refresh_tree()
    window.refresh_pages()

    _jump(window, "invoice.pdf")

    assert window.page_list.currentRow() == 1, "the attachment starts at row 1"


def test_clicking_a_forwarded_message_lands_on_its_own_body(window, tmp_path):
    outer = Path("outer.msg")
    window.selected_sources = [outer]
    add_forwarded_message(window.model, tmp_path, outer)
    window.refresh_tree()
    window.refresh_pages()
    assert window.model.page_count == 4

    _jump(window, "forwarded.msg")
    assert window.page_list.currentRow() == 1, "the carrier's body is row 0"

    _jump(window, "note.pdf")
    assert window.page_list.currentRow() == 3, "the nested attachment is last"


def test_the_jump_follows_pages_the_user_has_moved(window, tmp_path):
    """The jump lands where the page is now, not where it was converted."""
    outer = Path("outer.msg")
    window.selected_sources = [outer]
    add_forwarded_message(window.model, tmp_path, outer)
    window.refresh_pages()

    moved = window.model.pages.pop(3)          # the nested attachment's page
    window.model.pages.insert(0, moved)
    window.refresh_tree()
    window.refresh_pages()

    _jump(window, "note.pdf")
    assert window.page_list.currentRow() == 0


def test_the_jump_scrolls_the_page_into_view(window, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QAbstractItemView

    pdf = make_pdf(tmp_path / "long.pdf", 200)
    source = Path("long.pdf")
    window.selected_sources = [source]
    window.model.add_segment(source, "long", pdf, True, page_count=200)
    window.refresh_tree()
    window.refresh_pages()

    scrolled = []
    monkeypatch.setattr(window.page_list, "scrollToItem",
                        lambda item, hint=None: scrolled.append((item, hint)))

    window.go_to_page(150)

    assert window.page_list.currentRow() == 150
    assert scrolled and scrolled[0][0] is window.page_list.item(150)
    assert scrolled[0][1] == QAbstractItemView.PositionAtCenter


def test_the_jump_has_a_keyboard_shortcut(window):
    """Ctrl+J from either list does the same as the button."""
    from PySide6.QtGui import QKeySequence, QShortcut

    sequences = {
        shortcut.key().toString()
        for widget in (window.tree, window.page_list)
        for shortcut in widget.findChildren(QShortcut)
    }
    assert QKeySequence("Ctrl+J").toString() in sequences


def test_jumping_to_a_row_with_no_pages_says_so(window):
    source = Path("broken.pdf")
    window.selected_sources = [source]
    window.model.add_segment(source, "broken", None, False, detail="boom")
    window.refresh_tree()
    window.refresh_pages()

    assert _jump(window, "broken") is False
    assert "no pages" in window.status.currentMessage()


def _mail_with_a_forward_in_the_middle(window, tmp_path):
    """mail.msg: body, first.pdf, a forwarded message (body, inner.pdf), last.pdf."""
    source = Path("mail.msg")
    window.selected_sources = [source]
    inner = "staged/fwd.msg"
    add = window.model.add_segment
    add(source, "mail - body", make_pdf(tmp_path / "b.pdf", 1), True, kind="body",
        page_count=1, group=str(source), group_label="mail.msg")
    add(source, "mail - first.pdf", make_pdf(tmp_path / "f.pdf", 2), True,
        kind="attachment", page_count=2, group=str(source), group_label="mail.msg")
    add(source, "fwd - body", make_pdf(tmp_path / "fb.pdf", 1), True, kind="body",
        page_count=1, group=inner, parent=str(source), group_label="fwd.msg", depth=1)
    add(source, "fwd - inner.pdf", make_pdf(tmp_path / "fi.pdf", 1), True,
        kind="attachment", page_count=1, group=inner, parent=str(source),
        group_label="fwd.msg", depth=1)
    add(source, "mail - last.pdf", make_pdf(tmp_path / "l.pdf", 1), True,
        kind="attachment", page_count=1, group=str(source), group_label="mail.msg")
    add(source, "mail - gone.pdf", None, False, kind="attachment",
        detail="the attachment is empty", group=str(source), group_label="mail.msg")
    window.refresh_tree()
    window.refresh_pages()
    return source


def _labels(item, depth=0):
    rows = [("  " * depth) + item.text(0).strip()]
    for index in range(item.childCount()):
        rows.extend(_labels(item.child(index), depth + 1))
    return rows


def test_attachments_nest_under_their_email_in_their_original_order(window, tmp_path):
    _mail_with_a_forward_in_the_middle(window, tmp_path)

    rows = _labels(window.tree.topLevelItem(0))

    assert rows == [
        "mail.msg",
        "  body",
        "  first.pdf",
        "  fwd.msg",
        "    body",
        "    inner.pdf",
        "  last.pdf",
        "  gone.pdf (the attachment is empty)",
    ]


def test_every_row_with_pages_has_a_jump_arrow(window, tmp_path):
    from consolidator.gui.main_window import JUMP_COLUMN

    _mail_with_a_forward_in_the_middle(window, tmp_path)

    for text in ("mail.msg", "first.pdf", "fwd.msg", "inner.pdf", "last.pdf"):
        assert not _tree_row(window, text).icon(JUMP_COLUMN).isNull(), text
    assert _tree_row(window, "gone.pdf").icon(JUMP_COLUMN).isNull(), \
        "a row with no pages offers nowhere to go"


def test_clicking_a_rows_arrow_jumps_to_its_first_page(window, tmp_path):
    from consolidator.gui.main_window import JUMP_COLUMN

    _mail_with_a_forward_in_the_middle(window, tmp_path)

    for text, expected in (("inner.pdf", 4), ("fwd.msg", 3), ("last.pdf", 5),
                           ("first.pdf", 1), ("mail.msg", 0)):
        window.tree.itemClicked.emit(_tree_row(window, text), JUMP_COLUMN)
        assert window.page_list.currentRow() == expected, text


def test_a_row_click_away_from_the_arrow_still_does_not_jump(window, tmp_path):
    from consolidator.gui.main_window import NAME_COLUMN

    _mail_with_a_forward_in_the_middle(window, tmp_path)
    window.page_list.setCurrentRow(5)

    window.tree.itemClicked.emit(_tree_row(window, "first.pdf"), NAME_COLUMN)

    assert window.page_list.currentRow() == 5


def test_a_reticked_attachment_returns_to_its_place(tmp_path):
    """Its pages used to be re-inserted at the very top of the document."""
    model = DocumentModel()
    for name, count in (("a", 2), ("b", 1), ("c", 2)):
        model.add_segment(Path(name + ".pdf"), name,
                          make_pdf(tmp_path / (name + ".pdf"), count), True,
                          page_count=count)
    middle = model.segments[1].id

    model.set_segment_enabled(middle, False)
    model.set_segment_enabled(middle, True)

    assert [p.segment_id for p in model.pages] == [0, 0, 1, 2, 2]


def test_a_reticked_first_attachment_goes_back_before_the_rest(tmp_path):
    model = DocumentModel()
    for name in ("a", "b"):
        model.add_segment(Path(name + ".pdf"), name,
                          make_pdf(tmp_path / (name + ".pdf"), 1), True, page_count=1)

    model.set_segment_enabled(0, False)
    model.set_segment_enabled(0, True)

    assert [p.segment_id for p in model.pages] == [0, 1]


# ------------------------------------------------ per-email extraction policy


@pytest.fixture
def window_with_emails(window, tmp_path):
    """Two email sources, each with a one-page segment."""
    from PySide6.QtCore import Qt

    window.selected_sources = [Path("first.eml"), Path("second.eml")]
    for name in ("first.eml", "second.eml"):
        pdf = make_pdf(tmp_path / (name + ".pdf"), 1)
        window.model.add_segment(Path(name), name, pdf, True, kind=DOCUMENT, page_count=1)
    window.refresh_tree()
    return window


def _select_source(window, name: str):
    for i in range(window.tree.topLevelItemCount()):
        item = window.tree.topLevelItem(i)
        if item.text(0) == name:
            window.tree.setCurrentItem(item)
            return
    raise AssertionError("source %s not in the tree" % name)


def test_policy_defaults_to_the_global_setting(window_with_emails):
    window = window_with_emails
    window.default_policy = window._policy_from_widgets()
    assert window.policy_for(Path("first.eml")) is window.default_policy


def test_each_email_keeps_its_own_policy(window_with_emails):
    """The user asked for extraction to be decided email by email."""
    window = window_with_emails

    _select_source(window, "first.eml")
    window.policy_checks["documents"].setChecked(False)
    first = window.policy_for(Path("first.eml"))
    assert first.documents is False

    _select_source(window, "second.eml")
    # The second email must still have the default.
    assert window.policy_for(Path("second.eml")).documents is True
    assert window.policy_checks["documents"].isChecked() is True

    window.policy_checks["embedded"].setChecked(False)
    second = window.policy_for(Path("second.eml"))
    assert second.embedded is False

    # And the first email is unchanged.
    assert window.policy_for(Path("first.eml")).documents is False
    assert window.policy_for(Path("first.eml")).embedded is True


def test_selecting_an_email_loads_its_policy_into_the_checkboxes(window_with_emails):
    window = window_with_emails

    _select_source(window, "first.eml")
    window.policy_checks["pdf"].setChecked(False)

    _select_source(window, "second.eml")
    assert window.policy_checks["pdf"].isChecked() is True

    _select_source(window, "first.eml")
    assert window.policy_checks["pdf"].isChecked() is False


def test_non_email_selection_edits_the_default(window_with_emails, tmp_path):
    window = window_with_emails
    window.selected_sources.append(Path("plain.docx"))
    pdf = make_pdf(tmp_path / "plain.pdf", 1)
    window.model.add_segment(Path("plain.docx"), "plain", pdf, True, page_count=1)
    window.refresh_tree()

    _select_source(window, "plain.docx")
    window.policy_checks["other"].setChecked(True)
    assert window.default_policy.other is True
    assert "Default for newly added emails" in window.lbl_policy_scope.text()


def test_apply_to_all_emails_resets_overrides(window_with_emails):
    window = window_with_emails

    _select_source(window, "first.eml")
    window.policy_checks["documents"].setChecked(False)
    assert window.source_policies

    _select_source(window, "second.eml")
    window.apply_policy_to_all_emails()
    assert window.source_policies == {}
    assert window.policy_for(Path("first.eml")) is window.default_policy


def test_scope_label_names_the_selected_email(window_with_emails):
    window = window_with_emails
    _select_source(window, "first.eml")
    assert "first.eml" in window.lbl_policy_scope.text()


# ------------------------------------------------------------------- retry


def test_remove_failed_for_only_drops_that_sources_failures(tmp_path):
    m = DocumentModel()
    m.add_segment(Path("a.docx"), "a", None, False, detail="boom")
    m.add_segment(Path("a.docx"), "a-part", make_pdf(tmp_path / "a.pdf", 1), True, page_count=1)
    m.add_segment(Path("b.docx"), "b", None, False, detail="boom")

    removed = m.remove_failed_for(Path("a.docx"))

    assert removed == 1
    assert [s.label for s in m.segments] == ["a-part", "b"]
    assert m.page_count == 1, "the good segment keeps its pages"


def test_remove_failed_for_ignores_successes(tmp_path):
    m = DocumentModel()
    m.add_segment(Path("a.docx"), "a", make_pdf(tmp_path / "a.pdf", 1), True, page_count=1)
    assert m.remove_failed_for(Path("a.docx")) == 0
    assert len(m.segments) == 1


def test_retry_replaces_failed_segments_with_pages(window, tmp_path, monkeypatch):
    """What 'Retry failed' does once a conversion finally succeeds."""
    from PySide6.QtWidgets import QMessageBox

    from consolidator.gui import main_window as mw
    from consolidator.model import Segment, SourceResult
    from consolidator.pipeline import BuildReport

    # The success path pops a dialog; answering it is not what is under test.
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)

    window.model.add_segment(Path("broken.doc"), "broken", None, False,
                             detail="LibreOffice timed out")
    assert window.model.page_count == 0

    pdf = make_pdf(tmp_path / "broken.pdf", 2)
    segment = Segment(Path("broken.doc"), "broken", DOCUMENT, pdf, True)
    window._on_retried(BuildReport(sources=[SourceResult(Path("broken.doc"), [segment])]))

    assert window.model.page_count == 2
    assert all(s.ok for s in window.model.segments)
    assert window.page_list.count() == 2


def test_retry_without_a_scratch_dir_is_safe(window):
    window.scratch = None
    window.retry_sources([Path("a.doc")])   # must not raise
    assert window.scratch is None


def test_retry_uses_a_separate_scratch_subdirectory(window, tmp_path, monkeypatch):
    from consolidator.gui import main_window as mw

    captured = {}

    class FakeWorker:
        def __init__(self, sources, scratch, policy, parent=None, policy_for=None):
            captured["scratch"] = scratch
            captured["sources"] = list(sources)

        def start(self):
            captured["started"] = True

        def isRunning(self):        # consulted by MainWindow.closeEvent
            return False

        def cancel(self):
            pass

        def wait(self, *_a):
            return True

        # Signal stand-ins so connect() succeeds.
        class _Sig:
            def connect(self, *_a, **_k):
                pass

        progressed = _Sig()
        advanced = _Sig()
        completed = _Sig()
        crashed = _Sig()

    monkeypatch.setattr(mw, "ConversionWorker", FakeWorker)

    window.scratch = tmp_path
    (tmp_path / "retry0").mkdir()          # pretend a retry already happened
    window.retry_sources([Path("a.doc")])

    assert captured["started"]
    assert captured["scratch"] == tmp_path / "retry1"
    assert captured["scratch"].name != "retry0"


# ------------------------------------------------------- adding and removing


def test_adding_files_schedules_conversion(window, tmp_path):
    """Pages should appear without the user pressing Convert."""
    pdf = make_pdf(tmp_path / "a.pdf", 1)
    window._add([pdf])
    assert window._autoconvert.isActive(), "conversion should be queued"


def test_autoconvert_timer_runs_the_incremental_path(window, tmp_path, monkeypatch):
    """Regression: the timer was wired to the full re-convert.

    That would have discarded page edits every time a file was added, which is
    the exact thing the incremental design exists to prevent. This asserts the
    wiring rather than just the method.
    """
    calls = []
    monkeypatch.setattr(
        window, "_run_conversion",
        lambda sources, replace: calls.append((list(sources), replace)),
    )

    pdf = make_pdf(tmp_path / "a.pdf", 1)
    window.selected_sources = [pdf]
    window._converted_sources = set()
    window._autoconvert.start()
    window._autoconvert.timeout.emit()      # fire it without waiting

    assert calls, "the timer should have started a conversion"
    sources, replace = calls[0]
    assert replace is False, "auto-convert must be incremental, not a full rebuild"
    assert sources == [pdf]


def test_convert_button_is_still_a_full_reconvert(window, tmp_path, monkeypatch):
    """The Convert button stays, and does rebuild everything."""
    calls = []
    monkeypatch.setattr(
        window, "_run_conversion",
        lambda sources, replace: calls.append((list(sources), replace)),
    )
    pdf = make_pdf(tmp_path / "a.pdf", 1)
    window.selected_sources = [pdf]
    window._converted_sources = {str(pdf)}

    window.start_conversion()

    assert calls and calls[0][1] is True
    assert window._converted_sources == set(), "a full rebuild forgets what was done"


def test_only_new_sources_are_converted(window, tmp_path, monkeypatch):
    from consolidator.gui import main_window as mw

    captured = {}

    class FakeWorker:
        def __init__(self, sources, scratch, policy, parent=None, policy_for=None):
            captured["sources"] = list(sources)

        def start(self):
            captured["started"] = True

        def isRunning(self):
            return False

        class _Sig:
            def connect(self, *_a, **_k):
                pass

        progressed = _Sig()
        advanced = _Sig()
        completed = _Sig()
        crashed = _Sig()

    monkeypatch.setattr(mw, "ConversionWorker", FakeWorker)

    first = make_pdf(tmp_path / "first.pdf", 1)
    second = make_pdf(tmp_path / "second.pdf", 1)
    window.selected_sources = [first, second]
    window._converted_sources = {str(first)}          # first is already done

    window._convert_new_sources()

    assert captured["started"]
    assert captured["sources"] == [second]


def test_deleting_a_source_removes_its_pages(window, tmp_path):
    pdf = make_pdf(tmp_path / "a.pdf", 3)
    window.model.add_segment(Path("a.docx"), "a", pdf, True, page_count=3)
    window.selected_sources = [Path("a.docx")]
    window.refresh_tree()
    assert window.model.page_count == 3

    window.tree.setCurrentItem(window.tree.topLevelItem(0))
    window.remove_selected_sources()

    assert window.selected_sources == []
    assert window.model.page_count == 0, "its pages must go too"
    assert window.page_list.count() == 0


def test_drop_source_leaves_other_sources_alone(tmp_path):
    m = DocumentModel()
    m.add_segment(Path("a.docx"), "a", make_pdf(tmp_path / "a.pdf", 2), True, page_count=2)
    m.add_segment(Path("b.docx"), "b", make_pdf(tmp_path / "b.pdf", 1), True, page_count=1)

    assert m.drop_source(Path("a.docx")) == 1
    assert m.page_count == 1
    assert [s.label for s in m.segments] == ["b"]


def test_delete_and_backspace_remove_the_selected_source(window, tmp_path):
    from PySide6.QtGui import QKeySequence, QShortcut

    shortcuts = window.tree.findChildren(QShortcut)
    keys = {s.key().toString() for s in shortcuts}
    assert QKeySequence(QKeySequence.Delete).toString() in keys
    assert QKeySequence("Backspace").toString() in keys


def test_page_list_also_handles_delete(window, tmp_path):
    from PySide6.QtGui import QShortcut

    assert window.page_list.findChildren(QShortcut), "pages need a delete shortcut too"


# ------------------------------------------------------------ preview rotate


def test_preview_reflects_rotation(window, tmp_path):
    """Rotating must be visible in the preview, or it looks like nothing happened."""
    pdf = make_pdf(tmp_path / "a.pdf", 1)          # 612x792, portrait
    window.model.add_segment(Path("a.docx"), "a", pdf, True, page_count=1)
    window.refresh_pages()

    window.page_list.clearSelection()
    window.page_list.item(0).setSelected(True)
    window._update_preview()
    before = window.preview.pixmap().size()
    assert before.height() > before.width(), "portrait before rotating"

    window.rotate_selected(90)
    after = window.preview.pixmap().size()
    assert after.width() > after.height(), "landscape after rotating"


def test_qt_rotation_is_clockwise_matching_pdf():
    """PDF /Rotate turns clockwise; the preview must agree."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QImage, QTransform

    image = QImage(8, 8, QImage.Format_RGB32)
    image.fill(QColor("white"))
    image.setPixelColor(0, 0, QColor("red"))       # marker at top-left

    rotated = image.transformed(QTransform().rotate(90), Qt.FastTransformation)

    assert rotated.width() == 8 and rotated.height() == 8
    # Clockwise: the top-left corner ends up top-right.
    assert rotated.pixelColor(rotated.width() - 1, 0) == QColor("red")


def test_rotating_270_returns_the_marker_to_the_bottom_left():
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QImage, QTransform

    image = QImage(8, 8, QImage.Format_RGB32)
    image.fill(QColor("white"))
    image.setPixelColor(0, 0, QColor("red"))

    rotated = image.transformed(QTransform().rotate(270), Qt.FastTransformation)
    assert rotated.pixelColor(0, rotated.height() - 1) == QColor("red")


# ---------------------------------------------------------------- shutdown


def test_closing_is_prompt(window):
    """Waiting the full cancel timeout per worker made closing feel sluggish."""
    import time as _time

    from PySide6.QtGui import QCloseEvent

    started = _time.monotonic()
    window.closeEvent(QCloseEvent())
    elapsed = _time.monotonic() - started
    assert elapsed < 1.5, "closing took %.2fs" % elapsed



# ------------------------------------------------------------ close prompt


def _close_event(spontaneous: bool):
    from PySide6.QtGui import QCloseEvent

    class Event(QCloseEvent):
        def spontaneous(self):          # noqa: D401 - Qt naming
            return spontaneous

    return Event()


def test_closing_from_the_window_asks_first(window, monkeypatch):
    asked = []
    monkeypatch.setattr(window, "confirm_close", lambda: asked.append(1) or False)

    event = _close_event(spontaneous=True)
    window.closeEvent(event)

    assert asked == [1]
    assert not event.isAccepted(), "answering No keeps the window open"


def test_answering_yes_closes(window, monkeypatch):
    monkeypatch.setattr(window, "confirm_close", lambda: True)

    event = _close_event(spontaneous=True)
    window.closeEvent(event)

    assert event.isAccepted()


def test_a_close_issued_by_code_does_not_ask(window, monkeypatch):
    monkeypatch.setattr(window, "confirm_close",
                        lambda: pytest.fail("a programmatic close must not prompt"))

    event = _close_event(spontaneous=False)
    window.closeEvent(event)

    assert event.isAccepted()


def test_the_prompt_says_what_would_be_lost(window, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    seen = {}

    def fake_exec(box):
        seen["text"] = box.text()
        seen["info"] = box.informativeText()
        seen["default"] = box.defaultButton() is box.button(QMessageBox.No)
        return QMessageBox.No

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    window.model.add_segment(Path("a.pdf"), "a", make_pdf(tmp_path / "a.pdf", 1),
                             True, page_count=1)

    assert window.confirm_close() is False
    assert seen["text"] == "Are you sure you want to close the application?"
    assert "page arrangement" in seen["info"]
    assert seen["default"], "No is the default, so Enter does not discard work"


# -------------------------------------------------- retry puts things back


def _email_with_a_failed_middle(window, tmp_path):
    from consolidator.model import Segment

    source = Path("mail.msg")
    window.selected_sources = [source]
    group = str(source)
    add = window.model.add_segment
    add(source, "mail - body", make_pdf(tmp_path / "b.pdf", 1), True, kind="body",
        page_count=1, group=group, group_label="mail.msg")
    add(source, "mail - first.pdf", make_pdf(tmp_path / "f.pdf", 2), True,
        kind="attachment", page_count=2, group=group, group_label="mail.msg")
    add(source, "mail - middle.docx", None, False, kind="attachment",
        detail="LibreOffice crashed", group=group, group_label="mail.msg")
    add(source, "mail - last.pdf", make_pdf(tmp_path / "l.pdf", 1), True,
        kind="attachment", page_count=1, group=group, group_label="mail.msg")
    other = Path("other.pdf")
    add(other, "other", make_pdf(tmp_path / "o.pdf", 1), True, page_count=1)
    window.refresh_tree()
    window.refresh_pages()

    # What the retry brings back: the whole email again, now fully converted.
    retried = [
        Segment(source, "mail - body", "body", make_pdf(tmp_path / "rb.pdf", 1), True,
                page_count=1, group=group, group_label="mail.msg"),
        Segment(source, "mail - first.pdf", "attachment",
                make_pdf(tmp_path / "rf.pdf", 2), True, page_count=2,
                group=group, group_label="mail.msg"),
        Segment(source, "mail - middle.docx", "attachment",
                make_pdf(tmp_path / "rm.pdf", 3), True, page_count=3,
                group=group, group_label="mail.msg"),
        Segment(source, "mail - last.pdf", "attachment",
                make_pdf(tmp_path / "rl.pdf", 1), True, page_count=1,
                group=group, group_label="mail.msg"),
    ]
    return source, retried


def _page_labels(window):
    return [window.model.segment(p.segment_id).label for p in window.model.pages]


def test_a_retried_attachment_goes_back_where_it_was(window, tmp_path):
    from consolidator.model import SourceResult
    from consolidator.pipeline import BuildReport

    source, retried = _email_with_a_failed_middle(window, tmp_path)
    window.model.rotate([1], 90)                     # an edit to keep

    window._on_retried(BuildReport(sources=[SourceResult(source, retried)]))

    assert _page_labels(window) == [
        "mail - body", "mail - first.pdf", "mail - first.pdf",
        "mail - middle.docx", "mail - middle.docx", "mail - middle.docx",
        "mail - last.pdf", "other",
    ]
    assert window.model.pages[1].rotation == 90, "edits to parts that worked survive"
    labels = [s.label for s in window.model.segments_of(source)]
    assert labels == ["mail - body", "mail - first.pdf", "mail - middle.docx",
                      "mail - last.pdf"], "nothing duplicated, email order kept"
    rows = _labels(window.tree.topLevelItem(0))
    assert rows[1:] == ["  body", "  first.pdf", "  middle.docx", "  last.pdf"]


def test_a_retry_after_reordering_follows_the_part_before_it(window, tmp_path):
    from consolidator.model import SourceResult
    from consolidator.pipeline import BuildReport

    source, retried = _email_with_a_failed_middle(window, tmp_path)
    # The user moved "other" to the front.
    window.model.pages.insert(0, window.model.pages.pop())

    window._on_retried(BuildReport(sources=[SourceResult(source, retried)]))

    assert _page_labels(window) == [
        "other", "mail - body", "mail - first.pdf", "mail - first.pdf",
        "mail - middle.docx", "mail - middle.docx", "mail - middle.docx",
        "mail - last.pdf",
    ]


def test_a_part_that_still_fails_replaces_its_old_failure(window, tmp_path):
    from consolidator.model import Segment, SourceResult
    from consolidator.pipeline import BuildReport

    source, retried = _email_with_a_failed_middle(window, tmp_path)
    retried[2] = Segment(source, "mail - middle.docx", "attachment", None, False,
                         "still broken", group=str(source), group_label="mail.msg")

    window._on_retried(BuildReport(sources=[SourceResult(source, retried)]))

    nodes = window.model.segments_of(source)
    assert [n.label for n in nodes].count("mail - middle.docx") == 1
    assert [n.detail for n in nodes if not n.ok] == ["still broken"]
    assert len(window.model.pages) == 5


# ------------------------------------------------- output check reporting


def _merged_report(problems=(), error="", checked=5):
    from types import SimpleNamespace

    from consolidator.verify import Verification

    check = Verification(checked=checked, problems=list(problems), error=error)
    return SimpleNamespace(pages=checked, separators=0, skipped=[], verification=check)


def test_pages_missing_content_are_named_after_a_merge(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from consolidator.verify import PageProblem

    shown = {}

    def capture(box):
        shown["details"] = box.detailedText()
        shown["icon"] = box.icon()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", capture)
    problem = PageProblem(output_page=4, source="mail - invoice.pdf", source_page=2,
                          missing_words=12, words_in_source=300, sample=["Total"])

    window._on_merged(_merged_report([problem]))

    assert "1 page(s) may be missing content (page 4)" in window.status.currentMessage()
    assert not window.banner.isHidden()
    assert "page 4 (mail - invoice.pdf, page 2)" in shown["details"]
    assert "'Total'" in shown["details"]
    assert shown["icon"] == QMessageBox.Warning


def test_a_clean_check_says_every_page_was_checked(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)

    window._on_merged(_merged_report())

    assert "every page checked against its source" in window.status.currentMessage()
    assert window.banner.isHidden()


def test_a_check_that_could_not_run_is_not_reported_as_clean(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)

    window._on_merged(_merged_report(error="OSError: locked", checked=0))

    assert "could not be checked" in window.status.currentMessage()


def test_an_export_beside_an_existing_file_says_where_it_went(window, monkeypatch, tmp_path):
    from types import SimpleNamespace

    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    window.output_path = tmp_path / "report.pdf"

    window._on_merged(SimpleNamespace(pages=3, separators=0, skipped=[],
                                      output=tmp_path / "report (1).pdf"))

    message = window.status.currentMessage()
    assert "report (1).pdf" in message
    assert "report.pdf already exists, so it was left alone" in message


def test_pages_included_as_images_are_named(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    shown = {}
    monkeypatch.setattr(QMessageBox, "exec",
                        lambda box: shown.setdefault("details", box.detailedText()) and 0)
    report = _merged_report()
    report.as_images = [(412, "scan - page", 3, "LimitReachedError: too long")]

    window._on_merged(report)

    assert "included as images (page 412)" in window.status.currentMessage()
    assert not window.banner.isHidden()
    assert "page 412 (scan - page, page 3): LimitReachedError" in shown["details"]
