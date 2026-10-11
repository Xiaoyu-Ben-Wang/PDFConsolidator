"""Failures that could end the process are handled."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from consolidator import libreoffice, pipeline  # noqa: E402

requires_lo = pytest.mark.skipif(
    libreoffice.find_soffice() is None, reason="LibreOffice is not installed"
)


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


# ------------------------------------------------------------ rendering ---


@pytest.mark.parametrize("size", [(612, 792), (14400, 1), (1, 14400), (0, 0)])
def test_render_scale_keeps_the_bitmap_inside_the_box(size):
    from consolidator.gui.thumbs import fit_scale

    scale = fit_scale(size, 300, 232)
    width, height = max(size[0], 1) * scale, max(size[1], 1) * scale
    assert width <= 232.0001 and height <= 300.0001


def test_a_banner_shaped_page_renders_a_sane_thumbnail(qapp, tmp_path):
    from pypdf import PdfWriter

    from consolidator.gui.thumbs import ThumbnailCache

    writer = PdfWriter()
    writer.add_blank_page(width=14400, height=2)
    pdf = tmp_path / "banner.pdf"
    with open(pdf, "wb") as handle:
        writer.write(handle)

    cache = ThumbnailCache()
    image = cache.image(pdf, 0)
    preview = cache.render(pdf, 0, 640)

    assert image is not None and image.width() <= 2000
    assert preview is not None and preview.width() <= 1280
    cache.clear()


def test_a_stale_thumbnail_does_not_land_on_another_page(qapp, tmp_path):
    from PySide6.QtGui import QImage

    from consolidator.gui.main_window import MainWindow
    from test_gui import make_pdf

    window = MainWindow()
    try:
        first = make_pdf(tmp_path / "first.pdf", 1)
        second = make_pdf(tmp_path / "second.pdf", 1)
        window.model.add_segment(Path("first.pdf"), "first", first, True, page_count=1)
        window.model.add_segment(Path("second.pdf"), "second", second, True, page_count=1)
        window.refresh_pages()
        before = window.page_list.item(0).icon().cacheKey()
        image = QImage(10, 10, QImage.Format_RGB32)

        # A worker rendered row 0 as "second", but the list has changed since.
        window._on_thumbnail(0, (str(second), 0), image)
        assert window.page_list.item(0).icon().cacheKey() == before

        window._on_thumbnail(0, (str(first), 0), image)
        assert window.page_list.item(0).icon().cacheKey() != before
        window._on_thumbnail(99, (str(first), 0), image)     # out of range: ignored
    finally:
        window.close()


# --------------------------------------------------------------- shutdown ---


def test_stop_all_ends_a_running_conversion_promptly(monkeypatch, tmp_path):
    """stop_all ends a running conversion without waiting for the batch to finish."""
    script = tmp_path / "slow.py"
    script.write_text("import time\ntime.sleep(60)\n")
    command = [sys.executable, str(script)]

    outcome = {}

    def run():
        outcome["result"] = libreoffice._run(command, timeout=120, env=None)

    worker = threading.Thread(target=run)
    worker.start()
    deadline = time.monotonic() + 10
    while not libreoffice._running and time.monotonic() < deadline:
        time.sleep(0.05)
    started = time.monotonic()
    try:
        libreoffice.stop_all()
        worker.join(15)
        assert not worker.is_alive()
        assert time.monotonic() - started < 15
        assert not libreoffice._running

        # While stopped, new runs are refused.
        assert libreoffice._run(command, timeout=120, env=None)[1] == \
            "the application is closing"
    finally:
        libreoffice.resume()
    # After resume, runs work again.
    quick = tmp_path / "quick.py"
    quick.write_text("print('ok')\n")
    assert libreoffice._run([sys.executable, str(quick)], 60, None)[0].strip() == "ok"


def test_closing_the_window_reports_whether_work_is_still_running(qapp):
    from consolidator.gui.main_window import MainWindow

    window = MainWindow()
    try:
        assert window.workers_running() is False
    finally:
        window.close()


# ---------------------------------------------------------------- scratch ---


def test_stale_scratch_is_swept_but_a_live_one_is_kept(tmp_path):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()

    live = tmp_path / "gui_live"
    stale = tmp_path / "gui_stale"
    unowned = tmp_path / "gui_unowned"
    for directory in (live, stale, unowned):
        directory.mkdir()
        (directory / "segment.pdf").write_bytes(b"%PDF")
    pipeline.claim_scratch(live)
    (stale / pipeline.OWNER_FILE).write_text(str(dead.pid))

    pipeline.sweep_stale_scratch(tmp_path)

    assert live.exists(), "this process is alive; its scratch is in use"
    assert not stale.exists()
    assert unowned.exists(), "a directory nobody claimed is not ours to delete"


# ------------------------------------------------------------- error nets ---


def test_an_unexpected_exception_is_logged_and_shown_not_lost(qapp, tmp_path, monkeypatch):
    from consolidator.gui import app as gui_app
    from consolidator.gui.main_window import MainWindow

    monkeypatch.setattr(gui_app, "_log_handle", open(tmp_path / "log.txt", "a",
                                                     encoding="utf-8"))
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    window = MainWindow()
    try:
        gui_app.install_safety_nets(lambda: window)
        try:
            raise ValueError("a slot went wrong")
        except ValueError:
            sys.excepthook(*sys.exc_info())

        thread = threading.Thread(target=lambda: 1 / 0, name="probe")
        thread.start()
        thread.join()
    finally:
        import faulthandler

        faulthandler.disable()
        gui_app._log_handle.close()
        window.close()

    log = (tmp_path / "log.txt").read_text(encoding="utf-8")
    assert "ValueError: a slot went wrong" in log
    assert "ZeroDivisionError" in log and "probe" in log
    assert "still running" in window.status.currentMessage()


# ---------------------------------- bodies LibreOffice fails to render ---


def test_an_exclusive_conversion_waits_for_running_ones_and_blocks_new_ones():
    gate = libreoffice._Gate()
    order = []
    assert gate.acquire_shared()

    def exclusive():
        gate.acquire_exclusive()
        order.append("exclusive")
        assert gate.acquire_shared() is False, "re-entrant for its owner"
        gate.acquire_exclusive()            # nested
        gate.release_exclusive()
        time.sleep(0.2)
        order.append("exclusive done")
        gate.release_exclusive()

    def late_shared():
        time.sleep(0.1)
        gate.acquire_shared()
        order.append("late shared")
        gate.release_shared()

    threads = [threading.Thread(target=exclusive), threading.Thread(target=late_shared)]
    for thread in threads:
        thread.start()
    time.sleep(0.3)
    order.append("first shared done")
    gate.release_shared()
    for thread in threads:
        thread.join(5)

    assert order == ["first shared done", "exclusive", "exclusive done", "late shared"]


def _failing_renderer(monkeypatch, failures: int):
    """Patch render_body_pdf to fail the first ``failures`` calls, then work."""
    from consolidator import email_items

    calls = []
    real = email_items.render_body_pdf

    def fake(item, workdir, dest, timeout=300, inline_images=True):
        calls.append(inline_images)
        if len(calls) <= failures:
            raise RuntimeError("LibreOffice crashed while converting this file")
        return real(item, workdir, dest, timeout, inline_images)

    monkeypatch.setattr(email_items, "render_body_pdf", fake)
    monkeypatch.setattr(email_items, "BODY_RETRY_PAUSE", 0)
    return calls


def _item():
    from consolidator import email_items

    return email_items.EmailItem(
        subject="Quarterly", sender="a@example.com",
        body_html="<html><body><p>Revenue rose</p><p>Costs fell</p></body></html>",
        source=Path("q.msg"))


@requires_lo
@pytest.mark.parametrize("failures, note, calls", [
    (0, "", [True]),
    (1, "second attempt", [True, True]),
    (2, "without its inline pictures", [True, True, False]),
])
def test_a_body_is_retried_alone_then_without_pictures(tmp_path, monkeypatch,
                                                         failures, note, calls):
    from consolidator import email_items

    seen = _failing_renderer(monkeypatch, failures)
    dest = tmp_path / "body.pdf"

    result = email_items.render_body_reliably(_item(), tmp_path / "w", dest)

    assert seen == calls
    assert (note in result) if note else result == ""
    assert dest.exists()


def test_a_body_libreoffice_cannot_render_still_arrives_as_text(tmp_path, monkeypatch):
    import pypdfium2 as pdfium

    from consolidator import email_items

    _failing_renderer(monkeypatch, failures=99)
    dest = tmp_path / "body.pdf"

    note = email_items.render_body_reliably(_item(), tmp_path / "w", dest)

    assert note.startswith("shown as plain text")
    assert "crashed" in note
    document = pdfium.PdfDocument(str(dest))
    text = document[0].get_textpage().get_text_range()
    document.close()
    for words in ("Quarterly", "Revenue rose", "Costs fell", "a@example.com"):
        assert words in text


def test_a_nested_body_that_fails_keeps_its_segment(tmp_path, monkeypatch):
    from consolidator import email_items

    _failing_renderer(monkeypatch, failures=99)
    item = _item()
    policy = email_items.ExtractionPolicy()

    segments = email_items.expand_to_segments(item, tmp_path / "work", policy)

    body = segments[0]
    assert body.ok and body.pdf.exists()
    assert body.detail.startswith("shown as plain text")


def test_html_to_text_keeps_paragraphs_and_cells():
    from consolidator import email_items

    text = email_items.html_to_text(
        "<style>p{}</style><p>One  two</p><table><tr><td>a</td><td>b</td></tr></table>"
        "Caf&eacute;<br>end")

    assert text.splitlines()[0] == "One two"
    assert "a\tb" in text and "Café" in text and "end" in text
    assert "p{}" not in text


def test_a_failed_invocation_is_described_in_the_log(tmp_path, monkeypatch):
    log = tmp_path / "errors.log"
    monkeypatch.setattr(libreoffice, "failure_log", log)
    monkeypatch.setattr(libreoffice, "_run",
                        lambda cmd, timeout, env: ("", "Error: source file could not be loaded", False))
    monkeypatch.setattr(libreoffice, "require_soffice", lambda: Path("soffice.exe"))
    source = tmp_path / "x.html"
    source.write_text("<p>x</p>")

    result = libreoffice.convert_batch([source], tmp_path / "out")

    assert result.failed
    text = log.read_text(encoding="utf-8")
    assert "produced no output" in text and "source file could not be loaded" in text


def test_a_single_office_conversion_is_retried_alone(tmp_path, monkeypatch):
    from consolidator import convert

    calls = []

    def flaky(src, dest, timeout=600, workdir=None):
        calls.append(libreoffice._gate._owner is not None)
        if len(calls) == 1:
            raise libreoffice.LibreOfficeError("LibreOffice crashed while converting this file")
        Path(dest).write_bytes(b"%PDF-1.4")
        return dest

    monkeypatch.setattr(libreoffice, "convert_one", flaky)
    monkeypatch.setattr(convert, "RETRY_PAUSE_SECONDS", 0)
    src = tmp_path / "memo.docx"
    src.write_bytes(b"x")

    convert.convert_with_libreoffice(src, tmp_path / "memo.pdf", tmp_path)

    assert calls == [False, True], "the retry ran with the gate held exclusively"
