"""Phase A orchestration: gather sources, convert each to PDF segment(s), assemble.

Two things shape this module:

* **A source can yield several segments.** An Outlook item produces a body
  segment plus one per extracted attachment, and those all become pages. So the
  pipeline works in segments, not files.
* **LibreOffice start-up dominates.** Documents it lays out - Office files and web
  unique names and converted in a single batched invocation, which is far faster
  than one invocation each. Staging with an index prefix also removes any chance
  of two files with the same name colliding, since ``soffice`` always names its
  output after its input.

Conversion and assembly are separate entry points on purpose: the GUI converts
once, then lets the user edit for as long as they like without repeating any of
the expensive work.
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

# How many LibreOffice conversions to run at once. Most of the wall clock for many
# small documents is start-up, and start-ups overlap perfectly; what does not scale
# is memory, because each invocation gets its own profile and a loaded document.
# Measured on a sixteen-core machine converting 48 documents: one at a time 63s,
# four at a time 23s (2.7x). Eight was faster still (17s) but is deliberately not
# used. CONSOLIDATOR_WORKERS can only lower this, for a machine that dislikes
# several soffice processes.
MAX_CONVERT_WORKERS = 4


def conversion_workers() -> int:
    """How many LibreOffice invocations to allow at once."""
    override = os.environ.get("CONSOLIDATOR_WORKERS", "").strip()
    if override.isdigit():
        return max(1, min(MAX_CONVERT_WORKERS, int(override)))
    cores = os.cpu_count() or 1
    return max(1, min(MAX_CONVERT_WORKERS, cores // 2))


# LibreOffice reports nothing until it exits, so handing it every file at once
# means a progress bar that does not move for minutes - which on a folder of a
# thousand files is indistinguishable from a crash. The batch is split into a
# handful of chunks instead: each costs one extra start-up (about five seconds
# measured here) and buys a progress update. The floor avoids pointless
# start-ups on small batches; the ceiling keeps the ticks frequent enough on
# huge ones.
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
    """Run LibreOffice batches, several at a time, and return the results in order.

    Each invocation is a separate ``soffice`` process with its own profile and its
    own output directory, so they do not collide - they just cost memory.

    A chunk that produces *nothing at all* is the signature of an **invocation**
    that failed rather than of documents that failed, and running several at once
    is the likeliest reason: LibreOffice is known to fall over when several of its
    processes work at the same time on the same machine. Those chunks are
    therefore not retried on the spot. They are set aside and re-run **one at a
    time, after every other invocation has finished** - the point being that the
    retry then has no concurrency to lose to. It costs a second pass on the
    affected chunks and turns "the whole batch failed" into "the batch took
    slightly longer".

    Once ``should_stop`` says so, or the application is closing, no further chunk
    is started and nothing is retried: a chunk not started is left without a
    result, exactly as if the run had not reached it. Without this a cancelled
    batch carried on through every remaining chunk, and a closing window waited
    for retries of chunks whose soffice it had just ended.
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
        """One invocation, and one quiet second attempt if it produced nothing."""
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
    # Batches LibreOffice produced nothing for while running concurrently, and
    # which therefore had to be redone one at a time. Non-zero is worth telling
    # the user about: it means their machine dislikes several soffice at once.
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
    """Pick a scratch directory, verified writable by actually writing to it.

    The system temp directory is deliberately **not** the first choice. It can be
    missing, full, or - as on the machine this was developed on - simply not
    writable by the process that needs it. LibreOffice reports that failure as a
    misleading ``source file could not be loaded``, which costs a lot of time to
    diagnose once and should never have to be diagnosed twice.

    A portable application is copied somewhere writable by definition, so its own
    directory is the natural home for scratch files. The system temp directory is
    kept only as a last resort, for a read-only installation.
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
    """Whether a process exists. Never signals it (os.kill would end it on Windows)."""
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
    """Remove scratch left by runs of the application that are no longer running.

    A run that ended abruptly - closed mid-conversion, killed, a power cut -
    leaves its scratch behind, and on a portable install that is a growing pile
    beside the executable. Only directories whose owner is gone are touched, so a
    second instance running at the same time keeps its files. Returns how many
    were removed.
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
                continue        # unowned: not ours to judge
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

    This is the expensive half, and the GUI calls it once and then edits freely.
    ``workdir`` must outlive the result, because the segments it returns are
    paths into it.

    ``policy_for`` optionally supplies a per-source policy, so what gets extracted
    can be decided separately for each email rather than globally for the batch.

    ``on_progress`` is called as ``(done, total, label)``: how many sources have
    finished, how many there are, and what is happening now. A folder of several
    hundred files takes long enough that a still window is indistinguishable from
    a hung one. ``should_stop`` is polled between sources so that work can be
    abandoned part-way - a batch that big has to be interruptible.
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
        """Report progress. Returns True when the caller should stop."""
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
        """The caller's stop request, read directly rather than at the next tick."""
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
        elif convert.is_supported(src):
            simple_list.append((index, src))
        else:
            results[index] = SourceResult(
                src, [Segment(src, src.stem, DOCUMENT, None, False,
                              "unsupported file type %r" % (ext or "(none)"))]
            )

    # ---- Documents (Office, web): chunked LibreOffice invocations ----
    if document_batch and not stopped:
        total_documents = len(document_batch)
        size = _chunk_size(total_documents)
        groups = _chunks(document_batch, size)
        # The staged copy is what LibreOffice is pointed at; the triples in
        # ``groups`` are what maps a produced file back to the source it came from.
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

        # Message bodies are rendered in batches too: start-up dominates, and
        # rendering them one at a time costs seconds per message.
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
                return          # not reached, exactly as in a sequential run
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

        # Expanding is where an email's *attachments* get converted, and an Office
        # attachment is one LibreOffice run of its own, as is a nested message's
        # body. A mailbox of messages carrying documents is therefore the slowest
        # shape there is, and each message is independent of the others - so they
        # run several at a time, bounded by the same limit.
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
    for index, src in simple_list:
        if stopped:
            break
        out_pdf = work / ("%04d_%s.pdf" % (index, src.stem))
        segment = _convert_one_to_segment(src, out_pdf, staging, page_size)
        results[index] = SourceResult(src, [segment])
        emit("  %s   %s" % ("ok  " if segment.ok else "FAIL", src.name))
        stopped = progress(src.name, step=True)

    # ---- every produced PDF is opened once, and only then believed ----
    #
    # A converter that "succeeded" and left behind a PDF nobody can read used to
    # become a segment with zero pages: the item appeared in the list and simply
    # contributed nothing, which is how a document goes missing without a word.
    # A PDF cut short is exactly this case - the pages before the cut may read
    # while the file as a whole does not - so it is reported, with the reason,
    # instead of quietly becoming a shorter document.
    #
    # Reading the files back here also gives the interface its page counts, so it
    # never has to reopen every one of them.
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
    report.output = dest
    report.seconds = time.monotonic() - started

    if owned_scratch and not keep_scratch:
        libreoffice._remove_tree(scratch)

    return report


def count_pages(pdf: Path) -> int:
    """Page count of a produced PDF, or 0 if it cannot be read."""
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(pdf)).pages)
    except Exception:
        return 0
