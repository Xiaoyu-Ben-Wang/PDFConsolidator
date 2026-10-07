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
