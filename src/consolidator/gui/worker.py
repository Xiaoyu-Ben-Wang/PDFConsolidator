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


class ConversionWorker(QThread):
    """Runs Phase A: turn the selected files into PDF segments."""

    progressed = Signal(str)
    completed = Signal(object)   # pipeline.BuildReport
    crashed = Signal(str)

    def __init__(self, sources: list[Path], scratch: Path,
                 policy: email_items.ExtractionPolicy, parent=None,
                 policy_for=None) -> None:
        super().__init__(parent)
        self.sources = [Path(s) for s in sources]
        self.scratch = Path(scratch)
        self.policy = policy
        self.policy_for = policy_for
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

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

    ready = Signal(int, object)   # page index, QImage

    def __init__(self, jobs: list[tuple[int, Path, int]], cache, parent=None) -> None:
        super().__init__(parent)
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
            if image is not None:
                self.ready.emit(page_index, image)
