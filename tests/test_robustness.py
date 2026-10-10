"""Things that used to end the process, or could: each must now be survivable."""

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

        # A worker rendered row 0 as "second" - the list has since changed.
        window._on_thumbnail(0, (str(second), 0), image)
        assert window.page_list.item(0).icon().cacheKey() == before

        window._on_thumbnail(0, (str(first), 0), image)
        assert window.page_list.item(0).icon().cacheKey() != before
        window._on_thumbnail(99, (str(first), 0), image)     # out of range: ignored
    finally:
        window.close()


# --------------------------------------------------------------- shutdown ---


def test_stop_all_ends_a_running_conversion_promptly(monkeypatch, tmp_path):
    """Closing mid-conversion must not wait for LibreOffice to finish a batch."""
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

        # While stopping, nothing new starts...
        assert libreoffice._run(command, timeout=120, env=None)[1] == \
            "the application is closing"
    finally:
        libreoffice.resume()
    # ...and once resumed, conversions run again.
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
