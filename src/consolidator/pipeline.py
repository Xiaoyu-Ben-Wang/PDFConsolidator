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


def build(
    sources: list[Path],
    dest: Path,
    page_size_name: str = pages.DEFAULT_PAGE_SIZE,
    margin_inches: float = 0.5,
    bookmarks: bool = True,
    policy: email_items.ExtractionPolicy | None = None,
    on_event=None,
    keep_scratch: bool = False,
    scratch_dir: str | None = None,
) -> BuildReport:
    """Convert every source into PDF segments, then merge them into ``dest``."""

    def emit(message: str) -> None:
        if on_event:
            on_event(message)

    policy = policy or email_items.ExtractionPolicy()
    dest = Path(dest).resolve()
    target = pages.resolve(page_size_name)
    margin = margin_inches * 72.0

    started = time.time()
    if scratch_dir:
        scratch = Path(scratch_dir)
        scratch.mkdir(parents=True, exist_ok=True)
        owned_scratch = False
    else:
        root = choose_scratch(preferred_parent=dest.parent)
        scratch = Path(tempfile.mkdtemp(prefix="run_", dir=str(root)))
        owned_scratch = True

    staging = scratch / "staging"
    work = scratch / "out"
    staging.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)

    report = BuildReport(policy=policy)
    results: dict[int, SourceResult] = {}

    # Partition sources by how they must be handled.
    office_batch: list[tuple[int, Path, Path]] = []
    email_list: list[tuple[int, Path]] = []
    simple_list: list[tuple[int, Path]] = []

    for index, raw in enumerate(sources):
        src = Path(raw)
        ext = src.suffix.lower()

        if not src.exists():
            results[index] = SourceResult(src, [Segment(src, src.stem, DOCUMENT, None, False, "file not found")])
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

    # ---- Email items: parse, then expand into body + attachment segments ----
    for index, src in email_list:
        emit("Reading %s..." % src.name)
        try:
            item = email_items.read_message(src)
        except Exception as exc:
            results[index] = SourceResult(
                src, [Segment(src, src.stem, DOCUMENT, None, False,
                              "could not read message: %s: %s" % (type(exc).__name__, exc))]
            )
            emit("  FAIL %s - unreadable message" % src.name)
            continue

        segments = email_items.expand_to_segments(
            item, work / ("email%04d" % index), policy, page_size=target, on_event=emit
        )
        results[index] = SourceResult(src, segments)
        kept = sum(1 for s in segments if s.ok)
        emit("  %s: %d segment(s), %d attachment(s)"
             % (src.name, kept, len(item.attachments)))

    # ---- Everything else, one file at a time ----
    for index, src in simple_list:
        out_pdf = work / ("%04d_%s.pdf" % (index, src.stem))
        segment = _convert_one_to_segment(src, out_pdf, staging, target)
        results[index] = SourceResult(src, [segment])
        emit("  %s   %s" % ("ok  " if segment.ok else "FAIL", src.name))

    report.sources = [results[i] for i in sorted(results)]

    # ---- Phase B ----
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
    report.merged = assemble.merge(
        entries, dest, page_size=target, margin=margin, bookmarks=bookmarks, title=dest.stem,
    )
    report.output = dest
    report.seconds = time.time() - started

    if owned_scratch and not keep_scratch:
        libreoffice._remove_tree(scratch)

    return report
