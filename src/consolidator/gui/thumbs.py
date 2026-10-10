"""Page thumbnail rendering.

Rendering happens off the GUI thread, so the cache returns ``QImage`` rather than
``QPixmap``: Qt only permits pixmaps to be built on the GUI thread, and creating
one in a worker is a crash rather than an error.

Both caches are bounded. A folder of several hundred files can hold tens of
thousands of pages, and keeping every rendered thumbnail and every PDF handle for
that would cost more memory than the documents themselves - and, on Windows, more
file handles than the process is allowed.

**Two threads use this**: the thumbnail worker renders rows, and the GUI thread
renders the preview. Bound caches mean eviction, and eviction closes documents, so
every entry point takes a lock. Without it, the preview opening a new document
could close the very document the worker was rendering from - and a thumbnail that
fails to render is invisible, so a scatter of pages simply never appeared. That is
what "a section of the document fails to render" looked like on a large folder.
"""

from __future__ import annotations

import io
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtGui import QImage

from ..pdfium_guard import PDFIUM_LOCK

# Thumbnails are rendered at this pixel height and cached.
THUMB_HEIGHT = 150
THUMB_WIDTH = 116

# How many rendered thumbnails to keep, and how many documents to hold open. The
# defaults cover far more than a screenful - the page column only ever asks for
# what is near the viewport - while staying small enough to be irrelevant to
# memory.
IMAGE_CACHE_SIZE = 600
DOCUMENT_CACHE_SIZE = 24


def fit_scale(page_size: tuple[float, float], height: int, width: int) -> float:
    """The pdfium render scale that fits a page inside ``width`` x ``height``.

    Fitting by height alone is fine for paper and dangerous for anything else: a
    banner or a long drawing a few points tall asked pdfium for a bitmap millions
    of pixels wide, which fails at best and takes the process down at worst.
    """
    page_width = max(float(page_size[0]), 1.0)
    page_height = max(float(page_size[1]), 1.0)
    return max(0.01, min(height / page_height, width / page_width))


class ThumbnailCache:
    """Lazily renders and caches page thumbnails, keyed by (pdf, page index).

    Documents are opened once and kept while they are being used, because
    reopening a PDF per page is far more expensive than rendering from an open
    handle. Least recently used documents are closed again, which is what keeps
    the handle count flat as files are added.
    """

    def __init__(self, height: int = THUMB_HEIGHT,
                 image_cache: int = IMAGE_CACHE_SIZE,
                 document_cache: int = DOCUMENT_CACHE_SIZE) -> None:
        self.height = height
        self._image_limit = image_cache
        self._document_limit = document_cache
        self._documents: OrderedDict[str, object] = OrderedDict()
        self._images: OrderedDict[tuple[str, int], QImage] = OrderedDict()
        # Held for the whole of every render, so a document cannot be closed by
        # another thread midway through using it. It is the process-wide pdfium
        # lock, because pdfium is not safe to enter from two threads at all - and
        # the output verifier uses it from a third. Re-entrant because the public
        # methods call the private helpers that also take it.
        self._lock = PDFIUM_LOCK
        # Renders that raised. A missing thumbnail is otherwise silent, and a
        # silent gap is impossible to tell from a page that was never there.
        self.failed = 0
        self.last_error = ""

    @staticmethod
    def size() -> QSize:
        return QSize(THUMB_WIDTH, THUMB_HEIGHT)

    def _document(self, pdf: Path):
        key = str(pdf)
        doc = self._documents.get(key)
        if doc is None:
            import pypdfium2 as pdfium

            doc = pdfium.PdfDocument(key)
            self._documents[key] = doc
            self._trim_documents()
        else:
            self._documents.move_to_end(key)
        return doc

    def _trim_documents(self) -> None:
        while len(self._documents) > self._document_limit:
            _key, doc = self._documents.popitem(last=False)
            try:
                doc.close()
            except Exception:
                pass

    def _remember(self, key: tuple[str, int], image: QImage) -> None:
        self._images[key] = image
        self._images.move_to_end(key)
        while len(self._images) > self._image_limit:
            self._images.popitem(last=False)

    def page_count(self, pdf: Path) -> int:
        with self._lock:
            try:
                return len(self._document(Path(pdf)))
            except Exception:
                return 0

    def image(self, pdf: Path, index: int) -> QImage | None:
        with self._lock:
            key = (str(pdf), index)
            cached = self._images.get(key)
            if cached is not None:
                self._images.move_to_end(key)
                return cached

            try:
                document = self._document(Path(pdf))
                if index < 0 or index >= len(document):
                    return None
                page = document[index]
                # Twice the displayed size, so the thumbnail is sharp when shrunk.
                scale = fit_scale(page.get_size(), THUMB_HEIGHT * 2, THUMB_WIDTH * 2)
                pil = page.render(scale=scale).to_pil()
                pil.thumbnail((THUMB_WIDTH * 2, THUMB_HEIGHT * 2))

                buffer = io.BytesIO()
                pil.save(buffer, "PNG")
                image = QImage.fromData(buffer.getvalue(), "PNG")
                if image.isNull():
                    self._note_failure("render produced no image")
                    return None
                self._remember(key, image)
                return image
            except Exception as exc:
                self._note_failure("%s: %s" % (type(exc).__name__, exc))
                return None

    def _note_failure(self, detail: str) -> None:
        self.failed += 1
        self.last_error = detail

    def invalidate(self, pdf: Path) -> None:
        with self._lock:
            key = str(pdf)
            doc = self._documents.pop(key, None)
            if doc is not None:
                try:
                    doc.close()
                except Exception:
                    pass
            for cached_key in [k for k in self._images if k[0] == key]:
                self._images.pop(cached_key, None)

    def render(self, pdf: Path, index: int, height: int) -> QImage | None:
        """Render one page at an arbitrary height, for the preview pane.

        Not cached: the preview changes as the user clicks around, and caching
        every size would grow without bound.
        """
        with self._lock:
            try:
                document = self._document(Path(pdf))
                if index < 0 or index >= len(document):
                    return None
                page = document[index]
                # Bounded in width too: the preview pane is never wider than this.
                scale = fit_scale(page.get_size(), height, height * 2)
                pil = page.render(scale=scale).to_pil()
                if pil.height > height:
                    ratio = height / float(pil.height)
                    pil = pil.resize((max(1, int(pil.width * ratio)), height))

                buffer = io.BytesIO()
                pil.save(buffer, "PNG")
                return QImage.fromData(buffer.getvalue(), "PNG")
            except Exception as exc:
                self._note_failure("%s: %s" % (type(exc).__name__, exc))
                return None

    def clear(self) -> None:
        with self._lock:
            for doc in self._documents.values():
                try:
                    doc.close()
                except Exception:
                    pass
            self._documents.clear()
            self._images.clear()
            self.failed = 0
            self.last_error = ""

