"""Prototype 1 tests.

Tests that need LibreOffice are skipped when it is not installed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from consolidator import assemble, convert, libreoffice, pages, pipeline  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "generated"

requires_lo = pytest.mark.skipif(
    libreoffice.find_soffice() is None, reason="LibreOffice is not installed"
)


@pytest.fixture(scope="session")
def fixtures():
    if not FIXTURES.exists():
        pytest.skip("fixtures not generated; run tools/make_fixtures.py")
    return FIXTURES


# ----------------------------------------------------------------- gather ----


def test_gather_sorts_naturally(tmp_path):
    for name in ["file10.txt", "file2.txt", "file1.txt"]:
        (tmp_path / name).write_text("x")
    found = [p.name for p in pipeline.gather([str(tmp_path)])]
    assert found == ["file1.txt", "file2.txt", "file10.txt"]


def test_gather_skips_directories_but_recurses(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.txt").write_text("x")
    (tmp_path / "top.txt").write_text("x")
    found = {p.name for p in pipeline.gather([str(tmp_path)])}
    assert found == {"top.txt", "deep.txt"}


# -------------------------------------------------------------- big batches ---


def _blank_pdfs(tmp_path, count: int) -> list[Path]:
    """Write ``count`` blank one-page PDFs. PDFs are copied as-is, so no converter runs."""
    from pypdf import PdfWriter

    sources = []
    for index in range(count):
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        path = tmp_path / ("doc%02d.pdf" % index)
        with open(path, "wb") as handle:
            writer.write(handle)
        sources.append(path)
    return sources


def test_progress_is_reported_for_every_file(tmp_path):
    """Progress is reported for every file, in order, ending at the total."""
    sources = _blank_pdfs(tmp_path, 6)
    seen: list[tuple[int, int, str]] = []

    report = pipeline.convert_sources(
        sources, tmp_path / "work",
        on_progress=lambda done, total, label: seen.append((done, total, label)),
    )

    assert len(report.sources) == 6
    assert seen, "progress must be reported at all"
    assert all(total == 6 for _done, total, _label in seen)
    done = [value for value, _total, _label in seen]
    assert done == sorted(done), "progress must never go backwards"
    assert done[-1] == 6, "the last report must say the batch is finished"


def test_web_pages_are_supported_documents_not_text():
    """Web pages are rendered by LibreOffice, not treated as plain text."""
    from consolidator import convert

    for ext in (".html", ".htm", ".xhtml"):
        assert convert.is_supported(Path("page" + ext)), ext
        assert ext in convert.WEB_EXTS
        assert ext in convert.LIBREOFFICE_EXTS, "it batches with the Office files"
        assert ext in convert.DOCUMENT_EXTS
        assert ext not in convert.TEXT_EXTS, "rendering it as text would show markup"
        assert ext not in convert.OFFICE_EXTS


def test_plain_text_is_a_supported_document():
    from consolidator import convert

    for ext in (".txt", ".md", ".log", ".csv"):
        assert convert.is_supported(Path("notes" + ext)), ext
        assert ext in convert.TEXT_EXTS
        assert ext in convert.DOCUMENT_EXTS


@requires_lo
def test_a_web_page_becomes_a_rendered_page(tmp_path):
    """A web page is rendered with its text and without raw markup."""
    from pypdf import PdfReader

    from consolidator import convert

    page = tmp_path / "page.htm"
    page.write_text(
        "<html><body><h1>Quarterly report</h1>"
        "<p>Rendered by <b>LibreOffice</b>, not by a text fallback.</p></body></html>",
        encoding="utf-8",
    )
    out = tmp_path / "page.pdf"
    convert.convert(page, out, tmp_path)

    assert out.exists() and out.stat().st_size > 0
    text = "\n".join(p.extract_text() or "" for p in PdfReader(str(out)).pages)
    assert "Quarterly report" in text and "LibreOffice" in text
    assert "<h1>" not in text, "the markup itself must not be on the page"


@requires_lo
def test_an_html_source_converts_end_to_end(tmp_path):
    """An HTML file converts through the full pipeline."""
    page = tmp_path / "saved-page.html"
    page.write_text("<html><body><h2>Saved page</h2><p>Body text.</p></body></html>",
                    encoding="utf-8")

    report = pipeline.build([page], tmp_path / "out.pdf")

    assert report.merged is not None and report.merged.pages >= 1
    assert any(s.ok for s in report.segments)


def test_bookmarks_can_actually_be_switched_off(tmp_path):
    """Bookmarks are omitted when the options or the keyword turn them off.

    Guards against ``merge`` overriding ``MergeOptions.bookmarks`` with the
    default of its own ``bookmarks`` keyword.
    """
    from pypdf import PdfReader, PdfWriter

    from consolidator import assemble

    source = tmp_path / "two.pdf"
    writer = PdfWriter()
    for _ in range(2):
        writer.add_blank_page(width=612, height=792)
    with open(source, "wb") as handle:
        writer.write(handle)

    def outline_count(path):
        outline = PdfReader(str(path)).outline
        return sum(1 for item in outline if not isinstance(item, list))

    entries = [assemble.MergeEntry(source, "first"), assemble.MergeEntry(source, "second")]

    off = tmp_path / "off.pdf"
    report = assemble.merge(entries, off,
                            options=assemble.MergeOptions(bookmarks=False))
    assert report.outline_items == 0, "the option must be honoured"
    assert outline_count(off) == 0, "and the file must have no outline"

    on = tmp_path / "on.pdf"
    report = assemble.merge(entries, on, options=assemble.MergeOptions(bookmarks=True))
    assert report.outline_items == 2 and outline_count(on) == 2

    # The keyword works when no options object is passed.
    keyword = tmp_path / "keyword.pdf"
    report = assemble.merge(entries, keyword, bookmarks=False)
    assert report.outline_items == 0 and outline_count(keyword) == 0


def test_a_permissions_only_pdf_is_merged_not_dropped(tmp_path):
    """A PDF with only an owner password is merged with all its pages.

    Scanners and banks often set an owner password and permissions. Any reader
    opens these files without a password, so they are not treated as encrypted.
    """
    from pypdf import PdfReader, PdfWriter

    from consolidator import assemble

    source = tmp_path / "locked.pdf"
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=612, height=792)
    writer.encrypt(user_password="", owner_password="owner", permissions_flag=4)
    with open(source, "wb") as handle:
        writer.write(handle)
    assert PdfReader(str(source)).is_encrypted, "the fixture must look locked"

    dest = tmp_path / "merged.pdf"
    report = assemble.merge(
        [assemble.MergeEntry(source, "locked")], dest,
        options=assemble.MergeOptions(page_size=(612.0, 792.0)),
    )

    assert report.pages == 3, "all three pages should be in the output"
    assert not report.skipped, report.skipped
    assert len(PdfReader(str(dest)).pages) == 3


def test_a_password_protected_pdf_is_reported_not_ignored(tmp_path):
    """A PDF that needs a user password is reported as skipped."""
    from pypdf import PdfReader, PdfWriter

    from consolidator import assemble

    source = tmp_path / "secret.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt(user_password="letmein", owner_password="owner")
    with open(source, "wb") as handle:
        writer.write(handle)
    assert PdfReader(str(source)).is_encrypted

    dest = tmp_path / "merged.pdf"
    report = assemble.merge([assemble.MergeEntry(source, "secret")], dest)

    assert report.pages == 0
    assert report.skipped and "password" in report.skipped[0][1], report.skipped


def test_pages_sharing_one_object_keep_their_place(tmp_path):
    """Two page entries that point to the same object are both kept."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import NameObject

    from consolidator import assemble

    source = tmp_path / "repeated.pdf"
    writer = PdfWriter()
    for _ in range(4):
        writer.add_blank_page(width=612, height=792)
    kids = writer._root_object["/Pages"][NameObject("/Kids")]
    kids.insert(2, kids[1])                    # five entries, one object twice
    with open(source, "wb") as handle:
        writer.write(handle)

    reader = PdfReader(str(source))
    assert len(reader.pages) == 5
    assert (reader.pages[1].indirect_reference.idnum
            == reader.pages[2].indirect_reference.idnum), "the fixture should share"

    dest = tmp_path / "merged.pdf"
    report = assemble.merge([assemble.MergeEntry(source, "repeated")], dest)

    assert report.pages == 5, "a repeated page must not collapse into one"
    assert len(PdfReader(str(dest)).pages) == 5


def test_asking_for_pages_beyond_the_end_of_a_file_is_reported(tmp_path):
    """Requested pages past the end of a file are reported as skipped."""
    from pypdf import PdfReader, PdfWriter

    from consolidator import assemble

    source = tmp_path / "two.pdf"
    writer = PdfWriter()
    for _ in range(2):
        writer.add_blank_page(width=612, height=792)
    with open(source, "wb") as handle:
        writer.write(handle)

    dest = tmp_path / "merged.pdf"
    report = assemble.merge(
        [assemble.MergeEntry(source, "two",
                             [assemble.PageSpec(0), assemble.PageSpec(1),
                              assemble.PageSpec(2), assemble.PageSpec(3)])],
        dest,
    )

    assert report.pages == 2
    assert any("beyond the end" in why for _label, why in report.skipped), report.skipped
    assert len(PdfReader(str(dest)).pages) == 2


def test_macro_enabled_and_template_office_formats_are_supported():
    """Macro-enabled and template Office formats are supported.

    An .xlsm uses the same container as an .xlsx.
    """
    from consolidator import convert

    for ext in (".xlsm", ".docm", ".pptm", ".xltx", ".dotx", ".potx",
                ".xlsx", ".docx", ".pptx"):
        assert convert.is_supported(Path("file" + ext)), ext
        assert ext in convert.OFFICE_EXTS


def test_binary_workbooks_say_what_to_do_instead():
    from consolidator import convert

    assert not convert.is_supported(Path("book.xlsb"))
    assert "xlsb" in convert.KNOWN_UNSUPPORTED[".xlsb"].lower()


@requires_lo
def test_a_macro_enabled_workbook_converts(tmp_path):
    """LibreOffice reads the .xlsm container through the same Calc filter."""
    from openpyxl import Workbook

    from consolidator import convert

    book = tmp_path / "book.xlsx"
    sheet = Workbook()
    sheet.active["A1"] = "macro-enabled"
    sheet.save(book)
    target = tmp_path / "book.xlsm"
    target.write_bytes(book.read_bytes())

    out = tmp_path / "book.pdf"
    convert.convert(target, out, tmp_path)

    assert out.exists() and out.stat().st_size > 0


def test_batch_chunk_sizes_are_adaptive():
    from consolidator.pipeline import BATCH_CHUNK_MAX, BATCH_CHUNK_MIN, _chunk_size

    assert _chunk_size(0) == 1
    assert _chunk_size(10) == 10, "a small batch is one invocation"
    assert _chunk_size(BATCH_CHUNK_MIN) == BATCH_CHUNK_MIN
    assert _chunk_size(60) == BATCH_CHUNK_MIN, "a floor keeps start-ups down"
    assert _chunk_size(1000) <= BATCH_CHUNK_MAX, "a ceiling keeps the ticks frequent"
    assert _chunk_size(100000) == BATCH_CHUNK_MAX


def test_conversion_worker_count_is_adaptive(monkeypatch):
    """Workers are half the cores, capped, and CONSOLIDATOR_WORKERS overrides it."""
    from consolidator import pipeline

    monkeypatch.delenv("CONSOLIDATOR_WORKERS", raising=False)
    monkeypatch.setattr(pipeline.os, "cpu_count", lambda: 2)
    assert pipeline.conversion_workers() == 1, "two cores get one converter"
    monkeypatch.setattr(pipeline.os, "cpu_count", lambda: 8)
    assert pipeline.conversion_workers() == 4
    monkeypatch.setattr(pipeline.os, "cpu_count", lambda: 64)
    assert pipeline.conversion_workers() == pipeline.MAX_CONVERT_WORKERS

    monkeypatch.setenv("CONSOLIDATOR_WORKERS", "1")
    assert pipeline.conversion_workers() == 1, "the escape hatch has to be honoured"
    monkeypatch.setenv("CONSOLIDATOR_WORKERS", "3")
    assert pipeline.conversion_workers() == 3
    monkeypatch.setenv("CONSOLIDATOR_WORKERS", "64")
    assert pipeline.conversion_workers() == pipeline.MAX_CONVERT_WORKERS, \
        "the cap holds even for an explicit override"
    monkeypatch.setenv("CONSOLIDATOR_WORKERS", "nonsense")
    assert pipeline.conversion_workers() >= 1, "a typo must not disable conversion"


def test_a_chunk_that_fails_only_while_concurrent_is_redone_alone(tmp_path, monkeypatch):
    """A chunk that fails only under concurrency is retried alone afterwards.

    The fake converter produces nothing while another call is running. Retries
    run one at a time after the parallel run, because retrying while other
    processes are still running fails the same way.
    """
    from consolidator import libreoffice as lo
    from consolidator import pipeline

    monkeypatch.setattr(pipeline, "RETRY_PAUSE_SECONDS", 0)
    jobs = [([tmp_path / ("f%d.docx" % i)], tmp_path / ("out%d" % i)) for i in range(4)]

    active = 0
    peak = 0
    fallbacks: list[tuple[int, int]] = []

    def fake_batch(files, workdir, timeout=0):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            result = lo.BatchResult()
            if active > 1:                      # a second process is running
                for src in files:
                    result.failed[str(src)] = "LibreOffice crashed"
                return result
            for src in files:
                out = Path(workdir) / (Path(src).stem + ".pdf")
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"%PDF-1.4 fake")
                result.produced[str(src)] = str(out)
            return result
        finally:
            active -= 1

    monkeypatch.setattr(lo, "convert_batch", fake_batch)

    results = pipeline._convert_jobs(
        jobs, 60, 4, on_fallback=lambda count, total: fallbacks.append((count, total))
    )

    assert peak > 1, "the test must actually have run things at once"
    for index, result in enumerate(results):
        assert result.produced, "chunk %d was lost" % index
    assert fallbacks and fallbacks[0][1] == 4, "the fallback should be reported"


def test_concurrent_jobs_keep_their_order_and_retry_empty_chunks(tmp_path, monkeypatch):
    """Results come back per job, and a chunk that produced nothing is retried."""
    from consolidator import libreoffice as lo
    from consolidator import pipeline

    jobs = [([tmp_path / ("a%d.docx" % i), tmp_path / ("b%d.docx" % i)],
             tmp_path / ("out%d" % i)) for i in range(4)]

    calls: list[str] = []
    retried: list[int] = []

    def fake_batch(files, workdir, timeout=0):
        # The first attempt at job 2 produces nothing, as if the process crashed.
        workdir = Path(workdir)
        attempt = (str(workdir), len(calls))
        calls.append(str(workdir))
        result = lo.BatchResult()
        empty = "out2" in str(workdir) and calls.count(str(workdir)) == 1
        for src in files:
            if empty:
                result.failed[str(src)] = "LibreOffice crashed"
                continue
            out = workdir / (Path(src).stem + ".pdf")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"%PDF-1.4 fake")
            result.produced[str(src)] = str(out)
        if empty:
            retried.append(2)
        return result

    monkeypatch.setattr(lo, "convert_batch", fake_batch)
    monkeypatch.setattr(pipeline, "RETRY_PAUSE_SECONDS", 0)

    done: list[int] = []
    results = pipeline._convert_jobs(jobs, 60, 4, on_done=lambda i, r: done.append(i))

    assert len(results) == 4
    assert sorted(done) == [0, 1, 2, 3], "every job reports exactly once"
    for index, result in enumerate(results):
        assert len(result.produced) == 2, "job %d should have converted both" % index
    assert retried == [2], "only the chunk that produced nothing was retried"


def test_a_failed_document_is_not_retried_as_a_failed_chunk(tmp_path, monkeypatch):
    """A per-document failure does not retry the chunk.

    Only a call that produces nothing is treated as a crash.
    """
    from consolidator import libreoffice as lo
    from consolidator import pipeline

    attempts = []

    def fake_batch(files, workdir, timeout=0):
        attempts.append(len(files))
        result = lo.BatchResult()
        result.produced[str(files[0])] = str(tmp_path / "ok.pdf")
        result.failed[str(files[1])] = "unsupported content"
        return result

    monkeypatch.setattr(lo, "convert_batch", fake_batch)

    jobs = [([tmp_path / "good.docx", tmp_path / "bad.docx"], tmp_path / "out")]
    results = pipeline._convert_jobs(jobs, 60, 4)

    assert len(attempts) == 1, "a half-successful chunk must not be re-run"
    assert len(results[0].produced) == 1 and len(results[0].failed) == 1


def test_a_big_office_batch_reports_progress_while_it_runs(tmp_path, monkeypatch):
    """A large Office batch is split into chunks so progress updates during the run."""
    from consolidator import libreoffice as lo

    sources = []
    for index in range(60):
        path = tmp_path / ("doc%03d.docx" % index)
        path.write_bytes(b"never opened - the converter is stubbed")
        sources.append(path)

    calls: list[int] = []

    def fake_batch(files, workdir, timeout=0):
        calls.append(len(files))
        result = lo.BatchResult()
        for src in files:
            out = Path(workdir) / (Path(src).stem + ".pdf")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"%PDF-1.4 fake")
            result.produced[str(src)] = str(out)
        return result

    monkeypatch.setattr(lo, "convert_batch", fake_batch)
    seen: list[tuple[int, int]] = []

    report = pipeline.convert_sources(
        sources, tmp_path / "work",
        on_progress=lambda done, total, label: seen.append((done, total)),
    )

    assert len(calls) > 1, "60 files must not be one invocation"
    assert len(report.sources) == 60
    assert seen[-1] == (60, 60)
    assert len({done for done, _total in seen}) > 2, "the bar has to move during the run"


@requires_lo
def test_batch_conversion_survives_relative_paths(tmp_path, monkeypatch):
    """Batch conversion works with relative paths.

    LibreOffice cannot load a relative source path. It prints only
    "Error: source file could not be loaded" and produces nothing.
    """
    source = tmp_path / "note.html"
    source.write_text("<html><body><p>relative paths</p></body></html>",
                      encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = libreoffice.convert_batch([Path("note.html")], Path("out"))

    assert result.produced.get("note.html"), result.failed
    assert (tmp_path / "out" / "note.pdf").exists()


@requires_lo
def test_a_lock_left_behind_does_not_block_the_next_conversion(tmp_path):
    """A leftover LibreOffice lock file does not block the next conversion.

    While ``.~lock.note.pdf#`` exists, LibreOffice cannot write ``note.pdf``
    and produces nothing.
    """
    import os
    import stat

    source = tmp_path / "note.html"
    source.write_text("<html><body><p>again</p></body></html>", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    lock = out / ".~lock.note.pdf#"
    lock.write_text(",someone,HOST,10.10.2026 12:59,file:///x;")
    os.chmod(lock, stat.S_IREAD)

    result = libreoffice.convert_batch([source], out)

    assert result.produced.get(str(source)), result.failed
    assert not lock.exists(), "the run must also leave no lock of its own"


def test_a_batch_can_be_stopped_part_way(tmp_path):
    """Stopping a batch part way keeps the files already converted."""
    sources = _blank_pdfs(tmp_path, 5)
    converted: list[Path] = []

    def stop_after_two():
        return len(converted) >= 2

    # should_stop is checked after each source, so count sources as they convert.
    original = pipeline._convert_one_to_segment

    def counting(src, *args, **kwargs):
        converted.append(src)
        return original(src, *args, **kwargs)

    pipeline._convert_one_to_segment = counting
    try:
        report = pipeline.convert_sources(
            sources, tmp_path / "work", should_stop=stop_after_two,
        )
    finally:
        pipeline._convert_one_to_segment = original

    assert report.cancelled is True
    assert 0 < len(report.sources) < len(sources), \
        "stopping must keep the files already converted, not discard them"


# ------------------------------------------------------------------ pages ----


def test_page_size_resolution():
    assert pages.resolve("letter") == (612.0, 792.0)
    assert pages.resolve("original") is None
    assert pages.resolve("LETTER") == (612.0, 792.0)


def test_unknown_page_size_rejected():
    with pytest.raises(ValueError):
        pages.resolve("A3")


# -------------------------------------------------------------- conversion ----


def test_text_conversion_treats_markdown_literally(tmp_path):
    src = tmp_path / "notes.md"
    src.write_text("# Heading\n\n- bullet\n")
    dest = tmp_path / "out.pdf"
    convert.convert_text(src, dest, tmp_path, pages.LETTER)

    from pypdf import PdfReader

    text = PdfReader(str(dest)).pages[0].extract_text()
    # The Markdown appears verbatim, not interpreted.
    assert "# Heading" in text
    assert "- bullet" in text


def test_pdf_passthrough_is_byte_identical(tmp_path, fixtures):
    src = fixtures / "existing.pdf"
    dest = tmp_path / "copy.pdf"
    convert.convert(src, dest, tmp_path, pages.LETTER)
    assert dest.read_bytes() == src.read_bytes()


def test_png_with_alpha_is_flattened_onto_white(tmp_path, fixtures):
    """A transparent PNG must not come out with a black background."""
    import pypdfium2 as pdfium

    dest = tmp_path / "alpha.pdf"
    convert.convert_image(fixtures / "diagram.png", dest, tmp_path, pages.LETTER)

    image = pdfium.PdfDocument(str(dest))[0].render(scale=1.0).to_pil().convert("RGB")
    corner = image.getpixel((2, 2))
    assert min(corner) > 240, "background should be white, got %s" % (corner,)


def test_unsupported_extension_reports_clearly(tmp_path):
    src = tmp_path / "thing.pub"
    src.write_bytes(b"x")
    with pytest.raises(convert.ConversionError, match="Publisher"):
        convert.convert(src, tmp_path / "o.pdf", tmp_path, None)


def test_empty_file_is_rejected(tmp_path):
    src = tmp_path / "empty.txt"
    src.write_bytes(b"")
    with pytest.raises(convert.ConversionError, match="empty"):
        convert.convert(src, tmp_path / "o.pdf", tmp_path, pages.LETTER)


# --------------------------------------------------------------- assembly ----


def test_merge_normalises_page_size(tmp_path, fixtures):
    src = fixtures / "existing.pdf"      # A4 source
    entry = assemble.MergeEntry(src, "existing")
    dest = tmp_path / "merged.pdf"

    report = assemble.merge([entry], dest, page_size=pages.LETTER, margin=36.0)

    from pypdf import PdfReader

    reader = PdfReader(str(dest))
    assert report.pages == 2
    for page in reader.pages:
        assert round(float(page.mediabox.width), 1) == 612.0
        assert round(float(page.mediabox.height), 1) == 792.0


def test_merge_keeps_page_count_and_bookmarks(tmp_path, fixtures):
    entries = [
        assemble.MergeEntry(fixtures / "existing.pdf", "existing"),
        assemble.MergeEntry(fixtures / "photo.jpg", "photo"),
    ]
    # Images must be converted before merging.
    work = tmp_path / "w"
    work.mkdir()
    photo_pdf = tmp_path / "photo.pdf"
    convert.convert_image(fixtures / "photo.jpg", photo_pdf, work, pages.LETTER)
    entries[1] = assemble.MergeEntry(photo_pdf, "photo")

    dest = tmp_path / "merged.pdf"
    report = assemble.merge(entries, dest, page_size=pages.LETTER, margin=36.0)

    from pypdf import PdfReader

    assert report.entries == 2
    assert report.pages == 3
    assert len(PdfReader(str(dest)).outline) == 2


def test_merge_is_atomic_on_success(tmp_path, fixtures):
    dest = tmp_path / "out.pdf"
    assemble.merge([assemble.MergeEntry(fixtures / "existing.pdf", "x")], dest)
    assert dest.exists()
    assert not dest.with_suffix(".pdf.tmp").exists()


# ------------------------------------------------------------- LibreOffice ----


@requires_lo
@pytest.mark.parametrize("name", ["memo.docx", "budget.xlsx", "deck.pptx"])
def test_office_conversion_produces_pdf(tmp_path, fixtures, name):
    src = fixtures / name
    dest = tmp_path / (src.stem + ".pdf")
    libreoffice.convert_one(src, dest)
    assert dest.exists() and dest.stat().st_size > 0


@requires_lo
def test_libreoffice_env_has_no_python_vars(tmp_path, monkeypatch):
    """PYTHONPATH is removed from LibreOffice's environment.

    The host's PYTHONPATH breaks LibreOffice's startup.
    """
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "elsewhere"))
    env = libreoffice.clean_env(tmp_path)
    assert "PYTHONPATH" not in env
    assert env["TMP"] and env["TEMP"]


@requires_lo
def test_convert_one_leaves_no_scratch_behind(tmp_path, fixtures, monkeypatch):
    """convert_one removes its scratch directory.

    LibreOffice marks its lock files read-only. Windows does not delete
    read-only files, so a plain rmtree fails with WinError 5.
    """
    import tempfile

    root = tmp_path / "temp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))

    libreoffice.convert_one(fixtures / "memo.docx", tmp_path / "memo.pdf")

    leaked = sorted(p.name for p in root.glob("consolidator_lo_*"))
    assert not leaked, "scratch directories leaked: %s" % leaked


# ---------------------------------------------------------------- pipeline ----


@requires_lo
def test_end_to_end_build(tmp_path, fixtures):
    sources = pipeline.gather([str(fixtures)])
    dest = tmp_path / "combined.pdf"

    report = pipeline.build(sources, dest, page_size_name="letter")

    assert not report.failed, [f"{o.source.name}: {o.detail}" for o in report.failed]
    assert dest.exists()
    assert report.merged.pages > 0
    assert len(report.sources) == len(sources)


def test_end_to_end_without_office(tmp_path, fixtures):
    """Merging works without LibreOffice."""
    sources = [p for p in pipeline.gather([str(fixtures)]) if p.suffix.lower() != ".docx"]
    sources = [p for p in sources if p.suffix.lower() in (".pdf", ".jpg", ".png", ".txt", ".md")]
    dest = tmp_path / "combined.pdf"

    report = pipeline.build(sources, dest, page_size_name="letter")

    assert not report.failed, [o.detail for o in report.failed]
    assert dest.exists()


# ---------------------------------------------------------------- selftest ----


@requires_lo
def test_selftest_passes(tmp_path, monkeypatch):
    """The self-test passes. The frozen build uses it to check itself."""
    from consolidator import cli

    monkeypatch.chdir(tmp_path)
    assert cli.main(["selftest"]) == 0
    assert (tmp_path / "selftest.log").exists()
    assert "RESULT: PASS" in (tmp_path / "selftest.log").read_text(encoding="utf-8")


def test_selftest_writes_a_log(tmp_path, monkeypatch):
    from consolidator import cli

    monkeypatch.chdir(tmp_path)
    cli.main(["selftest"])
    log = tmp_path / "selftest.log"
    assert log.exists()
    assert "LibreOffice" in log.read_text(encoding="utf-8")
