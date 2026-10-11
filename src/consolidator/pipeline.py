"""Phase A orchestration: gather sources, convert each to PDF segment(s), assemble.

* **A source can yield several segments.** An Outlook item produces a body
  segment plus one per extracted attachment, so the pipeline works in segments,
  not files.
* **LibreOffice start-up time dominates.** Documents it converts (Office files
  and web pages) are staged under unique names and converted in batched
  invocations instead of one invocation per file. The index prefix on staged
  names also prevents collisions between files with the same name, since
  ``soffice`` names its output after its input.

Conversion and assembly are separate entry points so the GUI can convert once
and let the user edit without repeating the conversion.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from . import assemble, convert, email_items, libreoffice, pages
from .model import DOCUMENT, Segment, SourceResult

OFFICE_BATCH_TIMEOUT = 900

# Maximum number of concurrent LibreOffice conversions. Start-up time overlaps
# well across processes; memory is the limit, since each invocation has its own
# profile and loaded document. On a 16-core machine, 48 documents took 63s
# sequentially and 23s with 4 workers. CONSOLIDATOR_WORKERS can only lower this
# value, for machines that have trouble running several soffice processes.
MAX_CONVERT_WORKERS = 4


def conversion_workers() -> int:
    """How many LibreOffice invocations to allow at once."""
    override = os.environ.get("CONSOLIDATOR_WORKERS", "").strip()
    if override.isdigit():
        return max(1, min(MAX_CONVERT_WORKERS, int(override)))
    cores = os.cpu_count() or 1
    return max(1, min(MAX_CONVERT_WORKERS, cores // 2))


# LibreOffice reports nothing until it exits, so a large batch is split into
# chunks to allow progress updates. Each chunk costs one extra start-up (about
# five seconds). The minimum avoids extra start-ups on small batches; the
# maximum keeps updates frequent on large ones.
BATCH_TARGET_CHUNKS = 15
BATCH_CHUNK_MIN = 25
BATCH_CHUNK_MAX = 100

# How long to wait before re-running a conversion chunk that produced nothing.
RETRY_PAUSE_SECONDS = 2.0


def _chunk_size(total: int, smallest: int = BATCH_CHUNK_MIN,                largest: int = BATCH_CHUNK_MAX,
                target: int = BATCH_TARGET_CHUNKS) -> int:
    """How many files to hand one LibreOffice invocation."""
    if total <= smallest:
        return max(total, 1)
    return max(smallest, min(largest, total // target or smallest))


def _chunks(items: list, size: int) -> list[list]:
    return [items[start:start + size] for start in range(0, len(items), size)]


def _convert_jobs(jobs: list[tuple[list, Path]], timeout: int,
                  workers: int, on_done=None, on_fallback=None,
                  should_stop=None) -> list:
    """Run LibreOffice batches concurrently and return the results in job order.

    Each invocation is a separate ``soffice`` process with its own profile and
    output directory, so concurrent runs do not collide.

    A chunk that produces no output at all indicates that the invocation failed,
    not the documents. The likely cause is concurrency: LibreOffice can fail when
    several of its processes run at once. Such chunks are deferred and re-run one
    at a time after all other invocations finish.

    Once ``should_stop`` returns True, or the application is closing, no new
    chunk starts and nothing is retried. Chunks not started get no result.
    """
    results: list = [None] * len(jobs)

    def stopping() -> bool:
        if libreoffice.stopping():
            return True
        try:
            return bool(should_stop and should_stop())
        except Exception:
            return False

    def convert(index: int):
        if stopping():
            return None
        group, outdir = jobs[index]
        return libreoffice.convert_batch(group, outdir, timeout=timeout)

    def finish(index: int, result) -> None:
        results[index] = result
        if on_done:
            on_done(index, result)

    def convert_with_retry(index: int):
        """Run one invocation, retrying once if it produced nothing."""
        result = convert(index)
        if result is not None and jobs[index][0] and not result.produced and not stopping():
            time.sleep(RETRY_PAUSE_SECONDS)
            retry = convert(index)
            if retry is not None and retry.produced:
                return retry
        return result

    if workers <= 1 or len(jobs) <= 1:
        for index in range(len(jobs)):
            result = convert_with_retry(index)
            if result is not None:
                finish(index, result)
        return results

    deferred: list[int] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(convert, index): index for index in range(len(jobs))}
        for future in as_completed(futures):
            index = futures[future]
            result = future.result()
            if result is None:
                continue            # stopped before it started
            if jobs[index][0] and not result.produced and not stopping():
                deferred.append(index)
            else:
                finish(index, result)

    if deferred:
        if on_fallback:
            on_fallback(len(deferred), len(jobs))
        for index in sorted(deferred):
            result = convert_with_retry(index)
            if result is not None:
                finish(index, result)

    return results


@dataclass
class BuildReport:
    sources: list[SourceResult] = field(default_factory=list)
    merged: assemble.MergeReport | None = None
    output: Path | None = None
    seconds: float = 0.0
    policy: email_items.ExtractionPolicy | None = None
    cancelled: bool = False
    # Number of batches that produced nothing when run concurrently and were
    # re-run one at a time. Non-zero suggests the machine has trouble running
    # several soffice processes at once.
    retried_batches: int = 0

    @property
    def segments(self) -> list[Segment]:
        """Every successfully produced segment, in source order."""
        return [s for result in self.sources for s in result.segments if s.ok]

    @property
    def all_segments(self) -> list[Segment]:
        return [s for result in self.sources for s in result.segments]

    @property
    def succeeded(self) -> list[SourceResult]:
        return [r for r in self.sources if r.ok]

    @property
    def failed(self) -> list[SourceResult]:
        return [r for r in self.sources if not r.ok]

    @property
    def skipped_segments(self) -> list[Segment]:
        """Segments deliberately not produced (excluded, too large, nested)."""
        return [s for s in self.all_segments if not s.ok]


def choose_scratch(preferred_parent: Path | None = None) -> Path:
    """Pick a scratch directory and verify it is writable by writing a test file.

    The system temp directory is not the first choice: it can be missing, full,
    or not writable by the process. LibreOffice reports that case as the
    misleading error ``source file could not be loaded``.

    A portable install lives in a writable location, so its own directory is
    tried first. The system temp directory is the last resort, for read-only
    installations.
    """
    candidates: list[Path] = [libreoffice.app_root() / ".scratch"]
    if preferred_parent is not None:
        candidates.append(Path(preferred_parent) / ".consolidator-scratch")
    candidates.append(Path.cwd() / ".consolidator-scratch")
    candidates.append(Path(tempfile.gettempdir()) / "pdfconsolidator")

    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / "_writetest"
            probe.write_bytes(b"x")
            probe.unlink()
            return candidate
        except Exception:
            continue

    raise RuntimeError(
        "no writable scratch directory found (tried: %s)"
        % ", ".join(str(c) for c in candidates)
    )


OWNER_FILE = ".owner"


def _pid_alive(pid: int) -> bool:
    """Return whether a process exists, without signalling it.

    On Windows, os.kill would terminate the process, so the Win32 API is used.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE,
                                                ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
        if not handle:
            # Access denied still means it exists; anything else means it does not.
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == 259                    # STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def claim_scratch(directory: Path) -> None:
    """Mark a scratch directory as belonging to this process."""
    try:
        (Path(directory) / OWNER_FILE).write_text(str(os.getpid()), encoding="ascii")
    except OSError:
        pass


def sweep_stale_scratch(root: Path | None = None) -> int:
    """Remove scratch directories left by application runs that have exited.

    A run that ends abruptly (killed, closed mid-conversion, power loss) leaves
    its scratch files behind. Only directories whose owning process is gone are
    removed, so a concurrently running instance keeps its files. Returns the
    number of directories removed.
    """
    removed = 0
    roots = []
    try:
        roots.append(Path(root) if root else choose_scratch())
    except Exception:
        pass
    for scratch_root in roots:
        for candidate in scratch_root.glob("gui_*"):
            try:
                owner = int((candidate / OWNER_FILE).read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                continue        # no owner recorded: leave it
            if not _pid_alive(owner):
                libreoffice._remove_tree(candidate)
                removed += not candidate.exists()
    profiles = libreoffice.app_root() / "lo-profiles"
    for profile in profiles.glob("p*-*") if profiles.exists() else []:
        try:
            owner = int(profile.name[1:].split("-", 1)[0])
        except ValueError:
            continue
        if not _pid_alive(owner):
            libreoffice._remove_tree(profile)
            removed += not profile.exists()
    return removed


# Content sniffing may read the whole file for containers (OLE, ZIP). Files
# larger than this are not sniffed.
SNIFF_LIMIT_BYTES = 256 * 1024 * 1024


def _sniff_file(path: Path) -> str:
    try:
        if path.stat().st_size > SNIFF_LIMIT_BYTES:
            return ""
        return convert.sniff_extension(path.read_bytes())
    except OSError:
        return ""


def _natural_key(path: Path):
    """Sort so that ``file2`` precedes ``file10``."""
    parts = re.split(r"(\d+)", path.name.lower())
    return [int(p) if p.isdigit() else p for p in parts]


def gather(paths: list[str], recursive: bool = True) -> list[Path]:
    """Expand a mix of files and folders into a sorted list of files."""
    found: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            it = p.rglob("*") if recursive else p.glob("*")
            found.extend(sorted((f for f in it if f.is_file()), key=_natural_key))
        elif p.is_file():
            found.append(p)
        else:
            found.append(p)  # let the converter report it as missing
    return found


def _convert_one_to_segment(
    src: Path, out_pdf: Path, workdir: Path, page_size, label: str | None = None
) -> Segment:
    """Convert a single ordinary file into one segment."""
    started = time.monotonic()
    try:
        convert.convert(src, out_pdf, workdir, page_size=page_size)
        return Segment(src, label or src.stem, DOCUMENT, out_pdf, True, "", time.monotonic() - started)
    except convert.ConversionError as exc:
        return Segment(src, label or src.stem, DOCUMENT, None, False, str(exc), time.monotonic() - started)
    except Exception as exc:
        return Segment(
            src, label or src.stem, DOCUMENT, None, False,
            "%s: %s" % (type(exc).__name__, exc), time.monotonic() - started,
        )


def convert_sources(
    sources: list[Path],
    workdir: Path,
    policy: email_items.ExtractionPolicy | None = None,
    page_size: tuple[float, float] | None = None,
    on_event=None,
    policy_for=None,
    on_progress=None,
    should_stop=None,
) -> BuildReport:
    """Convert sources into segments without assembling anything.

    ``workdir`` must outlive the result, because the returned segments point to
    files inside it.

    ``policy_for`` optionally returns a per-source extraction policy, so each
    email can be handled differently.

    ``on_progress`` is called as ``(done, total, label)``: sources finished,
    total sources, and the current step. ``should_stop`` is polled between
    sources so the run can be cancelled part-way.
    """
    policy = policy or email_items.ExtractionPolicy()
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    def policy_of(source: Path):
        if policy_for is None:
            return policy
        try:
            return policy_for(source) or policy
        except Exception:
            return policy

    def emit(message: str) -> None:
        if on_event:
            on_event(message)

    total = len(sources)
    done = 0

    def progress(label: str = "", step: bool = False) -> bool:
        """Report progress and return True if the caller should stop."""
        nonlocal done
        if step:
            done += 1
        if on_progress:
            try:
                on_progress(done, total, label)
            except Exception:
                pass
        if should_stop:
            try:
                return bool(should_stop())
            except Exception:
                return False
        return False

    stopped = False

    def _asked_to_stop() -> bool:
        """Check for a stop request immediately, not at the next progress update."""
        if libreoffice.stopping():
            return True
        try:
            return bool(should_stop and should_stop())
        except Exception:
            return False

    started = time.monotonic()
    report = BuildReport(policy=policy)
    staging = workdir / "staging"
    work = workdir / "out"
    staging.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)

    results: dict[int, SourceResult] = {}
    document_batch: list[tuple[int, Path, Path]] = []
    email_list: list[tuple[int, Path]] = []
    simple_list: list[tuple[int, Path]] = []

    progress("Sorting %d file(s)\u2026" % total)
    for index, raw in enumerate(sources):
        src = Path(raw)
        ext = src.suffix.lower()
        convertible = src          # what the converters are pointed at

        # A file with no extension or an unrecognised one is identified by its
        # content and converted from a copy with the correct extension. The
        # source keeps its original name.
        if (src.exists() and ext not in convert.SUPPORTED_EXTS
                and ext not in convert.KNOWN_UNSUPPORTED):
            sniffed = _sniff_file(src)
            if sniffed:
                ext = sniffed
                if sniffed not in convert.EMAIL_EXTS | convert.LIBREOFFICE_EXTS:
                    convertible = staging / ("%04d_%s%s" % (index, src.stem or "file", sniffed))
                    try:
                        shutil.copyfile(src, convertible)
                    except OSError as exc:
                        results[index] = SourceResult(src, [Segment(
                            src, src.stem, DOCUMENT, None, False,
                            "could not stage: %s" % exc)])
                        continue

        if not src.exists():
            results[index] = SourceResult(
                src, [Segment(src, src.stem, DOCUMENT, None, False, "file not found")]
            )
        elif ext in convert.KNOWN_UNSUPPORTED:
            results[index] = SourceResult(
                src, [Segment(src, src.stem, DOCUMENT, None, False, convert.KNOWN_UNSUPPORTED[ext])]
            )
        elif ext in convert.EMAIL_EXTS:
            email_list.append((index, src))
        elif ext in convert.LIBREOFFICE_EXTS:
            staged = staging / ("%04d_%s%s" % (index, src.stem, ext))
            try:
                if ext in convert.WEB_EXTS:
                    convert.stage_web_page(src, staged)
                else:
                    shutil.copyfile(src, staged)
                document_batch.append((index, src, staged))
            except OSError as exc:
                results[index] = SourceResult(
                    src, [Segment(src, src.stem, DOCUMENT, None, False, "could not stage: %s" % exc)]
                )
        elif ext in convert.SUPPORTED_EXTS:
            simple_list.append((index, src, convertible))
        else:
            results[index] = SourceResult(
                src, [Segment(src, src.stem, DOCUMENT, None, False,
                              convert.unsupported_reason(ext))]
            )

    # ---- Documents (Office, web): chunked LibreOffice invocations ----
    if document_batch and not stopped:
        total_documents = len(document_batch)
        size = _chunk_size(total_documents)
        groups = _chunks(document_batch, size)
        # LibreOffice converts the staged copies; the triples in ``groups`` map
        # each output back to its source.
        jobs = [([staged for _index, _src, staged in group],
                 work / ("office%03d" % index))
                for index, group in enumerate(groups)]
        for _group, outdir in jobs:
            outdir.mkdir(parents=True, exist_ok=True)

        emit("Converting %d document(s) with LibreOffice, %d at a time..."
             % (total_documents, conversion_workers()))
        stopped = progress(
            "Converting %d document(s)\u2026" % total_documents
        )

        done_documents = 0

        def finished(index: int, batch) -> None:
            nonlocal done_documents, stopped
            group = groups[index]
            per_file = batch.seconds / max(len(group), 1)
            for index_, src, staged in group:
                produced = batch.produced.get(str(staged))
                if produced:
                    results[index_] = SourceResult(
                        src, [Segment(src, src.stem, DOCUMENT, Path(produced),
                                      True, "", per_file)]
                    )
                    emit("  ok   %s" % src.name)
                else:
                    reason = batch.failed.get(str(staged), "conversion failed")
                    results[index_] = SourceResult(
                        src, [Segment(src, src.stem, DOCUMENT, None, False,
                                      reason, per_file)]
                    )
                    emit("  FAIL %s - %s" % (src.name, reason))
                done_documents += 1
                stopped = progress(src.name, step=True)

        def office_fallback(count: int, total: int) -> None:
            report.retried_batches += count
            emit("LibreOffice produced nothing for %d of %d batch(es) when run "
                 "concurrently; retrying them one at a time." % (count, total))

        _convert_jobs(jobs, OFFICE_BATCH_TIMEOUT, conversion_workers(),
                      on_done=finished, on_fallback=office_fallback,
                      should_stop=lambda: stopped or _asked_to_stop())

    # ---- Email items: parse, render all bodies in one batch, then expand ----
    if email_list and not stopped:
        emit("Reading %d email item(s)..." % len(email_list))
        stopped = progress("Reading %d email item(s)\u2026" % len(email_list))
        parsed: list[tuple[int, Path, object, Path | None]] = []
        body_dir = work / "bodies"
        body_dir.mkdir(parents=True, exist_ok=True)

        for index, src in email_list:
            if stopped:
                break
            try:
                item = email_items.read_message(src)
            except Exception as exc:
                results[index] = SourceResult(
                    src,
                    [Segment(src, src.stem, DOCUMENT, None, False,
                             "could not read message: %s: %s" % (type(exc).__name__, exc))],
                )
                emit("  FAIL %s - unreadable message" % src.name)
                stopped = progress(src.name, step=True)
                continue

            html_path = None
            if policy_of(src).body:
                html_path = email_items.write_body_html(
                    item, body_dir / ("email%04d_body.html" % index)
                )
            parsed.append((index, src, item, html_path))
            emit("  read %s (%d attachment(s))" % (src.name, len(item.attachments)))

        # Render message bodies in batches to avoid one LibreOffice start-up per
        # message.
        bodies: dict[str, str] = {}
        body_errors: dict[str, str] = {}
        htmls = [h for _, _, _, h in parsed if h is not None]
        if htmls and not stopped:
            size = _chunk_size(len(htmls))
            groups = _chunks(htmls, size)
            jobs = [(list(group), body_dir / ("chunk%03d" % index))
                    for index, group in enumerate(groups)]
            for _group, outdir in jobs:
                outdir.mkdir(parents=True, exist_ok=True)

            rendered = 0

            def rendered_body(index: int, batch) -> None:
                nonlocal rendered, stopped
                bodies.update(batch.produced)
                body_errors.update(batch.failed)
                rendered += len(groups[index])
                stopped = progress("Rendering message bodies (%d/%d)\u2026"
                                   % (min(rendered, len(htmls)), len(htmls)))

            stopped = progress("Rendering %d message body/bodies\u2026" % len(htmls))
            def body_fallback(count: int, total: int) -> None:
                report.retried_batches += count
                emit("LibreOffice produced nothing for %d of %d body batch(es) when "
                     "run concurrently; retrying them one at a time." % (count, total))

            _convert_jobs(jobs, OFFICE_BATCH_TIMEOUT, conversion_workers(),
                          on_done=rendered_body, on_fallback=body_fallback,
                          should_stop=lambda: stopped or _asked_to_stop())

        def expand_one(job) -> None:
            nonlocal stopped
            if stopped or _asked_to_stop():
                stopped = True
                return          # leave unprocessed, as a sequential run would
            index, src, item, html_path = job
            key = str(html_path) if html_path else ""
            try:
                segments = email_items.expand_to_segments(
                    item,
                    work / ("email%04d" % index),
                    policy_of(src),
                    page_size=page_size,
                    body_pdf=Path(bodies[key]) if key in bodies else None,
                    body_error=body_errors.get(key) if key and key not in bodies else None,
                    on_event=emit,
                )
            except Exception as exc:
                segments = [Segment(src, src.stem, DOCUMENT, None, False,
                                    "%s: %s" % (type(exc).__name__, exc))]
            results[index] = SourceResult(src, segments)
            stopped = progress(src.name, step=True)

        # Expansion converts each email's attachments; each Office attachment and
        # each nested message body is a separate LibreOffice run. Messages are
        # independent, so they are expanded concurrently, up to the same limit.
        workers = conversion_workers()
        if workers > 1 and len(parsed) > 1 and not stopped:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(expand_one, parsed))
        else:
            for job in parsed:
                if stopped:
                    break
                expand_one(job)

    # ---- Everything else, one file at a time ----
    for index, src, convertible in simple_list:
        if stopped:
            break
        out_pdf = work / ("%04d_%s.pdf" % (index, src.stem))
        segment = _convert_one_to_segment(convertible, out_pdf, staging, page_size,
                                          label=src.stem)
        segment.source = src
        results[index] = SourceResult(src, [segment])
        emit("  %s   %s" % ("ok  " if segment.ok else "FAIL", src.name))
        stopped = progress(src.name, step=True)

    # ---- Verify every produced PDF by opening it once ----
    #
    # A conversion can report success but leave an unreadable or truncated PDF.
    # Such a segment is marked failed with a reason instead of contributing zero
    # pages. Reading the files here also records page counts for the interface.
    for result in results.values():
        for segment in result.segments:
            if not (segment.ok and segment.pdf):
                continue
            pages = count_pages(segment.pdf)
            if pages <= 0:
                segment.ok = False
                segment.detail = ("the PDF produced for this item could not be read "
                                  "- it may be truncated or incomplete")
                emit("  FAIL %s - unreadable PDF" % segment.label)
            else:
                segment.page_count = pages

    report.sources = [results[i] for i in sorted(results)]
    report.cancelled = stopped
    report.seconds = time.monotonic() - started
    return report


def build(
    sources: list[Path],
    dest: Path,
    page_size_name: str = pages.DEFAULT_PAGE_SIZE,
    margin_inches: float = 0.5,
    bookmarks: bool = True,
    policy: email_items.ExtractionPolicy | None = None,
    options: assemble.MergeOptions | None = None,
    on_event=None,
    keep_scratch: bool = False,
    scratch_dir: str | None = None,
    on_progress=None,
    should_stop=None,
) -> BuildReport:
    """Convert every source into PDF segments, then merge them into ``dest``."""

    def emit(message: str) -> None:
        if on_event:
            on_event(message)

    dest = Path(dest).resolve()
    target = pages.resolve(page_size_name)
    margin = margin_inches * 72.0

    if options is None:
        options = assemble.MergeOptions()
    options.page_size = target
    options.margin = margin
    options.bookmarks = bookmarks
    if not options.title:
        options.title = dest.stem

    started = time.monotonic()
    if scratch_dir:
        scratch = Path(scratch_dir)
        scratch.mkdir(parents=True, exist_ok=True)
        owned_scratch = False
    else:
        root = choose_scratch(preferred_parent=dest.parent)
        scratch = Path(tempfile.mkdtemp(prefix="run_", dir=str(root)))
        owned_scratch = True

    report = convert_sources(sources, scratch, policy=policy,
                             page_size=target, on_event=on_event,
                             on_progress=on_progress, should_stop=should_stop)

    entries = [
        assemble.MergeEntry(segment.pdf, segment.label)
        for segment in report.segments
        if segment.pdf is not None
    ]

    if not entries:
        emit("Nothing converted successfully - no PDF written.")
        report.seconds = time.monotonic() - started
        if owned_scratch and not keep_scratch:
            libreoffice._remove_tree(scratch)
        return report

    emit("Assembling %d segment(s)..." % len(entries))
    report.merged = assemble.merge(entries, dest, options=options)
    # May differ from dest, because an existing file is never overwritten.
    report.output = report.merged.output or dest
    report.seconds = time.monotonic() - started

    if owned_scratch and not keep_scratch:
        libreoffice._remove_tree(scratch)

    return report


def count_pages(pdf: Path) -> int:
    """Return the page count of a PDF, or 0 if it cannot be read."""
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(pdf)).pages)
    except Exception:
        return 0
