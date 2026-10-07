"""Phase A orchestration: gather sources, convert each to PDF segment(s), assemble.

Two things shape this module:

* **A source can yield several segments.** An Outlook item produces a body
  segment plus one per extracted attachment, and those all become pages. So the
  pipeline works in segments, not files.
* **LibreOffice start-up dominates.** Plain Office documents are staged under
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
from dataclasses import dataclass, field
from pathlib import Path

from . import assemble, convert, email_items, libreoffice, pages
from .model import DOCUMENT, Segment, SourceResult

OFFICE_BATCH_TIMEOUT = 900


@dataclass
class BuildReport:
    sources: list[SourceResult] = field(default_factory=list)
    merged: assemble.MergeReport | None = None
    output: Path | None = None
    seconds: float = 0.0
    policy: email_items.ExtractionPolicy | None = None

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
    """Return a scratch directory, verified writable by actually writing to it.

    ``%TEMP%`` cannot be assumed to exist or be writable - it can be missing,
    full, or restricted by policy. Rather than trusting it, each candidate is
    probed and the first that works wins. This matters for an application meant
    to run from removable media on an unfamiliar machine.
    """
    candidates: list[Path] = [Path(tempfile.gettempdir()) / "pdfconsolidator"]
    if preferred_parent is not None:
        candidates.append(Path(preferred_parent) / ".consolidator-scratch")
    candidates.append(Path.cwd() / ".consolidator-scratch")

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
    started = time.time()
    try:
        convert.convert(src, out_pdf, workdir, page_size=page_size)
        return Segment(src, label or src.stem, DOCUMENT, out_pdf, True, "", time.time() - started)
    except convert.ConversionError as exc:
        return Segment(src, label or src.stem, DOCUMENT, None, False, str(exc), time.time() - started)
    except Exception as exc:
        return Segment(
            src, label or src.stem, DOCUMENT, None, False,
            "%s: %s" % (type(exc).__name__, exc), time.time() - started,
        )


def convert_sources(
    sources: list[Path],
    workdir: Path,
    policy: email_items.ExtractionPolicy | None = None,
    page_size: tuple[float, float] | None = None,
    on_event=None,
    policy_for=None,
) -> BuildReport:
    """Convert sources into segments without assembling anything.

    This is the expensive half, and the GUI calls it once and then edits freely.
    ``workdir`` must outlive the result, because the segments it returns are
    paths into it.

    ``policy_for`` optionally supplies a per-source policy, so what gets extracted
    can be decided separately for each email rather than globally for the batch.
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

    started = time.time()
    report = BuildReport(policy=policy)
    staging = workdir / "staging"
    work = workdir / "out"
    staging.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)

    results: dict[int, SourceResult] = {}
    office_batch: list[tuple[int, Path, Path]] = []
    email_list: list[tuple[int, Path]] = []
    simple_list: list[tuple[int, Path]] = []

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
        elif ext in convert.OFFICE_EXTS:
            staged = staging / ("%04d_%s%s" % (index, src.stem, ext))
            try:
                shutil.copyfile(src, staged)
                office_batch.append((index, src, staged))
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

    # ---- Office documents: one LibreOffice invocation for the whole batch ----
    if office_batch:
        emit("Converting %d Office document(s) with LibreOffice..." % len(office_batch))
        batch = libreoffice.convert_batch(
            [staged for _, _, staged in office_batch], work, timeout=OFFICE_BATCH_TIMEOUT
        )
        per_file = batch.seconds / max(len(office_batch), 1)
        for index, src, staged in office_batch:
            produced = batch.produced.get(str(staged))
            if produced:
                results[index] = SourceResult(
                    src, [Segment(src, src.stem, DOCUMENT, Path(produced), True, "", per_file)]
                )
                emit("  ok   %s" % src.name)
            else:
                reason = batch.failed.get(str(staged), "conversion failed")
                results[index] = SourceResult(
                    src, [Segment(src, src.stem, DOCUMENT, None, False, reason, per_file)]
                )
                emit("  FAIL %s - %s" % (src.name, reason))

    # ---- Email items: parse, render all bodies in one batch, then expand ----
    if email_list:
        emit("Reading %d email item(s)..." % len(email_list))
        parsed: list[tuple[int, Path, object, Path | None]] = []
        body_dir = work / "bodies"
        body_dir.mkdir(parents=True, exist_ok=True)

        for index, src in email_list:
            try:
                item = email_items.read_message(src)
            except Exception as exc:
                results[index] = SourceResult(
                    src,
                    [Segment(src, src.stem, DOCUMENT, None, False,
                             "could not read message: %s: %s" % (type(exc).__name__, exc))],
                )
                emit("  FAIL %s - unreadable message" % src.name)
                continue

            html_path = None
            if policy_of(src).body:
                html_path = email_items.write_body_html(
                    item, body_dir / ("email%04d_body.html" % index)
                )
            parsed.append((index, src, item, html_path))
            emit("  read %s (%d attachment(s))" % (src.name, len(item.attachments)))

        # One LibreOffice invocation for every body: start-up dominates, and
        # rendering them one at a time costs seconds per message.
        bodies: dict[str, str] = {}
        body_errors: dict[str, str] = {}
        htmls = [h for _, _, _, h in parsed if h is not None]
        if htmls:
            emit("Rendering %d message body/bodies..." % len(htmls))
            batch = libreoffice.convert_batch(htmls, body_dir, timeout=OFFICE_BATCH_TIMEOUT)
            bodies = batch.produced
            body_errors = batch.failed

        for index, src, item, html_path in parsed:
            key = str(html_path) if html_path else ""
            segments = email_items.expand_to_segments(
                item,
                work / ("email%04d" % index),
                policy_of(src),
                page_size=page_size,
                body_pdf=Path(bodies[key]) if key in bodies else None,
                body_error=body_errors.get(key) if key and key not in bodies else None,
                on_event=emit,
            )
            results[index] = SourceResult(src, segments)

    # ---- Everything else, one file at a time ----
    for index, src in simple_list:
        out_pdf = work / ("%04d_%s.pdf" % (index, src.stem))
        segment = _convert_one_to_segment(src, out_pdf, staging, page_size)
        results[index] = SourceResult(src, [segment])
        emit("  %s   %s" % ("ok  " if segment.ok else "FAIL", src.name))

    report.sources = [results[i] for i in sorted(results)]
    report.seconds = time.time() - started
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

    started = time.time()
    if scratch_dir:
        scratch = Path(scratch_dir)
        scratch.mkdir(parents=True, exist_ok=True)
        owned_scratch = False
    else:
        root = choose_scratch(preferred_parent=dest.parent)
        scratch = Path(tempfile.mkdtemp(prefix="run_", dir=str(root)))
        owned_scratch = True

    report = convert_sources(sources, scratch, policy=policy,
                             page_size=target, on_event=on_event)

    entries = [
        assemble.MergeEntry(segment.pdf, segment.label)
        for segment in report.segments
        if segment.pdf is not None
    ]

    if not entries:
        emit("Nothing converted successfully - no PDF written.")
        report.seconds = time.time() - started
        if owned_scratch and not keep_scratch:
            libreoffice._remove_tree(scratch)
        return report

    emit("Assembling %d segment(s)..." % len(entries))
    report.merged = assemble.merge(entries, dest, options=options)
    report.output = dest
    report.seconds = time.time() - started

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
