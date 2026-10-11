"""Page thumbnail rendering.

Rendering runs off the GUI thread, so the cache returns ``QImage`` rather than
``QPixmap``: Qt only allows pixmaps on the GUI thread, and creating one in a
worker crashes.

Both caches are bounded, so large batches do not use excessive memory or, on
Windows, exceed the process file-handle limit.

Two threads use this class: the thumbnail worker renders rows and the GUI thread
renders the preview. Eviction closes documents, so every entry point takes a
lock; otherwise one thread could close a document the other is rendering from.
"""

from __future__ import annotations

import io
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtGui import QImage

from ..pdfium_guard import PDFIUM_LOCK

# Thumbnail size in pixels.
THUMB_HEIGHT = 150
THUMB_WIDTH = 116

# Maximum number of cached thumbnails and of open documents. Both are well
# above what the page list requests near the viewport, and small in memory.
IMAGE_CACHE_SIZE = 600
DOCUMENT_CACHE_SIZE = 24


def fit_scale(page_size: tuple[float, float], height: int, width: int) -> float:
    """Return the pdfium scale that fits a page in ``width`` x ``height``.

    Width is bounded as well as height, because a very wide, short page scaled
    by height alone would request a huge bitmap that can crash pdfium.
    """
    page_width = max(float(page_size[0]), 1.0)
    page_height = max(float(page_size[1]), 1.0)
    return max(0.01, min(height / page_height, width / page_width))


class ThumbnailCache:
    """Render and cache page thumbnails, keyed by (pdf, page index).

    Documents stay open while in use, because reopening a PDF per page is slow.
    Least recently used documents are closed to keep the handle count bounded.
    """

    def __init__(self, height: int = THUMB_HEIGHT,
                 image_cache: int = IMAGE_CACHE_SIZE,
                 document_cache: int = DOCUMENT_CACHE_SIZE) -> None:
        self.height = height
        self._image_limit = image_cache
        self._document_limit = document_cache
        self._documents: OrderedDict[str, object] = OrderedDict()
        self._images: OrderedDict[tuple[str, int], QImage] = OrderedDict()
        # Held for each whole render, so another thread cannot close the
        # document mid-render. Uses the process-wide pdfium lock because pdfium
        # is not thread-safe and the output verifier also uses it. Re-entrant
        # because public methods call helpers that also take it.
        self._lock = PDFIUM_LOCK
        # Count and last message of failed renders, so the GUI can report them.
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
                # Render at twice the displayed size so the thumbnail stays sharp.
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
        """Render one page at the given height for the preview pane.

        Not cached, because caching every requested size would grow without bound.
        """
        with self._lock:
            try:
                document = self._document(Path(pdf))
                if index < 0 or index >= len(document):
                    return None
                page = document[index]
                # Also limit width to twice the height.
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

