"""Background workers.

Conversion and assembly both take seconds to minutes, and LibreOffice calls block
for their whole duration, so neither may run on the GUI thread. Thumbnail
rendering is off-thread too, otherwise scrolling a large document stutters.
"""

from __future__ import annotations

import traceback
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .. import assemble, email_items, pipeline

# Worker threads get a generous stack. Parsing a hostile or merely deeply nested
# PDF or message recurses deeply, and Windows gives a new thread 1 MB by default:
# running out of that is not an exception Python can catch but the end of the
# process. Address space is reserved, not committed, so this costs nothing.
WORKER_STACK_BYTES = 16 * 1024 * 1024


class ConversionWorker(QThread):
    """Runs Phase A: turn the selected files into PDF segments."""

    progressed = Signal(str)
    # done, total, what is happening now. A folder of several hundred files takes
    # long enough that silence looks like a hang.
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
        """Ask for the run to stop.

        Checked between sources rather than mid-file: LibreOffice conversions and
        message parses are not interruptible, and killing one mid-write would
        leave a half-written PDF behind.
        """
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def run(self) -> None:  # noqa: D102
        try:
            # page_size is deliberately None: raw pages are converted once and
            # only fitted to a page size at assembly, so changing the page size
            # later costs nothing but a re-merge.
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
    """Runs Phase B: write the assembled PDF."""

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


class ThumbnailWorker(QThread):
    """Renders page thumbnails without blocking the interface.

    Emits ``QImage`` rather than ``QPixmap`` because only the GUI thread may
    construct a pixmap.
    """

    # Row position, the (pdf, page) that was rendered, and the QImage. The page is
    # sent back so the receiver can check the row still shows that page: a list
    # rebuilt mid-render would otherwise get another page's picture.
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
