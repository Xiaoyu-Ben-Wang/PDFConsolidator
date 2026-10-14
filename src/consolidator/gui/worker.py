"""Background workers.

Conversion and assembly can take minutes, and LibreOffice calls block for their
whole duration, so both run off the GUI thread. Thumbnail rendering also runs
off-thread so scrolling stays smooth.
"""

from __future__ import annotations

import traceback
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .. import assemble, duplicates, email_items, pipeline

# Worker thread stack size. Parsing deeply nested PDFs or messages recurses
# deeply, and the Windows default of 1 MB per thread can overflow, which ends
# the process instead of raising an exception. The space is reserved, not
# committed, so it does not use memory.
WORKER_STACK_BYTES = 16 * 1024 * 1024


class ConversionWorker(QThread):
    """Run phase A: convert the selected files into PDF segments."""

    progressed = Signal(str)
    # done, total, current status text.
    advanced = Signal(int, int, str)
    completed = Signal(object)   # pipeline.BuildReport
    crashed = Signal(str)

    def __init__(self, sources: list[Path], scratch: Path,
                 policy: email_items.ExtractionPolicy, parent=None,
                 policy_for=None) -> None:
        super().__init__(parent)
        self.setStackSize(WORKER_STACK_BYTES)
        self.sources = [Path(s) for s in sources]
        self.scratch = Path(scratch)
        self.policy = policy
        self.policy_for = policy_for
        self._cancelled = False

    def cancel(self) -> None:
        """Request that the run stop.

        Checked between sources, not mid-file: LibreOffice conversions and
        message parsing cannot be interrupted safely and would leave a partial
        PDF.
        """
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def run(self) -> None:  # noqa: D102
        try:
            # page_size is None: pages are fitted to a page size at assembly,
            # so changing the page size only needs a re-merge.
            report = pipeline.convert_sources(
                self.sources,
                self.scratch,
                policy=self.policy,
                page_size=None,
                on_event=self.progressed.emit,
                policy_for=self.policy_for,
                on_progress=self.advanced.emit,
                should_stop=lambda: self._cancelled,
            )
            self.completed.emit(report)
        except Exception:
            self.crashed.emit(traceback.format_exc())


class MergeWorker(QThread):
    """Run phase B: write the assembled PDF."""

    completed = Signal(object)   # assemble.MergeReport
    crashed = Signal(str)

    def __init__(self, entries: list[assemble.MergeEntry], dest: Path,
                 options: assemble.MergeOptions, parent=None) -> None:
        super().__init__(parent)
        self.setStackSize(WORKER_STACK_BYTES)
        self.entries = entries
        self.dest = Path(dest)
        self.options = options

    def run(self) -> None:  # noqa: D102
        try:
            report = assemble.merge(self.entries, self.dest, options=self.options)
            self.completed.emit(report)
        except Exception:
            self.crashed.emit(traceback.format_exc())


class DuplicateWorker(QThread):
    """Read every page and look for pages that repeat an earlier one."""

    advanced = Signal(int, int, str)
    completed = Signal(object)   # list[duplicates.Match]
    crashed = Signal(str)

    def __init__(self, pages: list[tuple[Path, int]], parent=None) -> None:
        super().__init__(parent)
        self.setStackSize(WORKER_STACK_BYTES)
        self.pages = pages
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def run(self) -> None:  # noqa: D102
        try:
            stop = lambda: self._cancelled  # noqa: E731
            prints = duplicates.read_pages(self.pages, should_stop=stop,
                                           on_progress=self.advanced.emit)
            self.completed.emit(duplicates.find_duplicates(prints, should_stop=stop))
        except Exception:
            self.crashed.emit(traceback.format_exc())


class ThumbnailWorker(QThread):
    """Render page thumbnails off the GUI thread.

    Emits ``QImage`` rather than ``QPixmap`` because only the GUI thread may
    create a pixmap.
    """

    # Row position, the rendered (pdf, page), and the QImage. The page is
    # included so the receiver can check the row still shows that page after
    # the list is rebuilt.
    ready = Signal(int, object, object)

    def __init__(self, jobs: list[tuple[int, Path, int]], cache, parent=None) -> None:
        super().__init__(parent)
        self.setStackSize(WORKER_STACK_BYTES)
        self.jobs = jobs
        self.cache = cache
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:  # noqa: D102
        for page_index, pdf, index_in_pdf in self.jobs:
            if self._cancelled:
                return
            image = self.cache.image(pdf, index_in_pdf)
            if image is not None and not self._cancelled:
                self.ready.emit(page_index, (str(pdf), index_in_pdf), image)
