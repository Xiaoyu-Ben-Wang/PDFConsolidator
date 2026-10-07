"""Phase A orchestration: gather sources, convert each to a PDF, then assemble.

Two things drive the shape of this module:

* **LibreOffice start-up dominates.** Ten documents converted in one invocation
  take far less time than ten invocations, so Office formats are staged with
  unique names and converted as a single batch.
* **Staging with unique names avoids stem collisions.** ``soffice`` always names
  its output after the input, so two files called ``report.docx`` in different
  folders would otherwise overwrite each other. Every source is therefore copied
  to ``<index>_<stem>.<ext>`` before conversion.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import assemble, convert, libreoffice, pages

OFFICE_BATCH_TIMEOUT = 900


@dataclass
class Outcome:
    """What happened to one source file."""

    source: Path
    pdf: Path | None = None
    ok: bool = False
    detail: str = ""
    seconds: float = 0.0


@dataclass
class BuildReport:
    outcomes: list[Outcome] = field(default_factory=list)
    merged: assemble.MergeReport | None = None
    output: Path | None = None
    seconds: float = 0.0

    @property
    def succeeded(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.ok]

    @property
    def failed(self) -> list[Outcome]:
        return [o for o in self.outcomes if not o.ok]


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


def choose_scratch(preferred_parent: Path | None = None) -> Path:
    """Return a scratch directory, verified writable by actually writing to it.

    ``%TEMP%`` cannot be assumed to exist or be writable - it can be missing,
    full, or restricted by policy. Rather than trusting it, each candidate is
    probed, and the first that works wins. This matters for an application meant
    to run on an unfamiliar machine from removable media.
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


def build(
    sources: list[Path],
    dest: Path,
    page_size_name: str = pages.DEFAULT_PAGE_SIZE,
    margin_inches: float = 0.5,
    bookmarks: bool = True,
    on_event=None,
    keep_scratch: bool = False,
    scratch_dir: str | None = None,
) -> BuildReport:
    """Convert every source to a PDF then merge them into ``dest``."""

    def emit(message: str) -> None:
        if on_event:
            on_event(message)

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

    report = BuildReport()

    # Index every source so intermediate names are unique and stable.
    indexed: list[tuple[int, Path]] = [(i, Path(s)) for i, s in enumerate(sources)]
    office_items: list[tuple[int, Path, Path]] = []
    simple_items: list[tuple[int, Path]] = []

    for index, src in indexed:
        ext = src.suffix.lower()
        if not src.exists():
            report.outcomes.append(Outcome(src, None, False, "file not found"))
        elif ext in convert.KNOWN_UNSUPPORTED:
            report.outcomes.append(Outcome(src, None, False, convert.KNOWN_UNSUPPORTED[ext]))
        elif ext in convert.OFFICE_EXTS:
            staged = staging / ("%04d_%s%s" % (index, src.stem, ext))
            try:
                shutil.copyfile(src, staged)
                office_items.append((index, src, staged))
            except OSError as exc:
                report.outcomes.append(Outcome(src, None, False, "could not stage: %s" % exc))
        elif ext in (convert.PDF_EXTS | convert.IMAGE_EXTS | convert.TEXT_EXTS):
            simple_items.append((index, src))
        else:
            report.outcomes.append(
                Outcome(src, None, False, "unsupported file type %r" % (ext or "(none)"))
            )

    # ---- Office formats: one LibreOffice invocation for the whole batch ----
    if office_items:
        emit("Converting %d Office document(s) with LibreOffice..." % len(office_items))
        batch = libreoffice.convert_batch(
            [staged for _, _, staged in office_items], work, timeout=OFFICE_BATCH_TIMEOUT
        )
        produced = batch.produced
        for index, src, staged in office_items:
            pdf = produced.get(str(staged))
            if pdf:
                report.outcomes.append(Outcome(src, Path(pdf), True, "", batch.seconds / len(office_items)))
                emit("  ok   %s" % src.name)
            else:
                reason = batch.failed.get(str(staged), "conversion failed")
                report.outcomes.append(Outcome(src, None, False, reason, batch.seconds / len(office_items)))
                emit("  FAIL %s - %s" % (src.name, reason))

    # ---- Everything else, one file at a time ----
    for index, src in simple_items:
        out_pdf = work / ("%04d_%s.pdf" % (index, src.stem))
        t0 = time.time()
        try:
            convert.convert(src, out_pdf, staging, page_size=target)
            report.outcomes.append(Outcome(src, out_pdf, True, "", time.time() - t0))
            emit("  ok   %s" % src.name)
        except convert.ConversionError as exc:
            report.outcomes.append(Outcome(src, None, False, str(exc), time.time() - t0))
            emit("  FAIL %s - %s" % (src.name, exc))
        except Exception as exc:  # never let one file abort the run
            report.outcomes.append(
                Outcome(src, None, False, "%s: %s" % (type(exc).__name__, exc), time.time() - t0)
            )
            emit("  FAIL %s - %s" % (src.name, exc))

    # ---- Phase B ----
    # Preserve the original selection order rather than grouping by engine.
    order = {str(src): i for i, src in enumerate(sources)}
    entries = [
        assemble.MergeEntry(o.pdf, o.source.stem)
        for o in sorted(report.outcomes, key=lambda o: order.get(str(o.source), 0))
        if o.ok and o.pdf is not None
    ]

    if not entries:
        emit("Nothing converted successfully - no PDF written.")
        report.seconds = time.time() - started
        return report

    emit("Assembling %d document(s)..." % len(entries))
    report.merged = assemble.merge(
        entries, dest, page_size=target, margin=margin, bookmarks=bookmarks,
        title=dest.stem,
    )
    report.output = dest
    report.seconds = time.time() - started

    if owned_scratch and not keep_scratch:
        shutil.rmtree(scratch, ignore_errors=True)

    return report
