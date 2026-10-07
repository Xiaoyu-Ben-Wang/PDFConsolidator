"""Page thumbnail rendering.

Rendering happens off the GUI thread, so the cache returns ``QImage`` rather than
``QPixmap``: Qt only permits pixmaps to be built on the GUI thread, and creating
one in a worker is a crash rather than an error.
"""

from __future__ import annotations

import io
from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtGui import QImage

# Thumbnails are rendered at this pixel height and cached.
THUMB_HEIGHT = 150
THUMB_WIDTH = 116


class ThumbnailCache:
    """Lazily renders and caches page thumbnails, keyed by (pdf, page index).

    Documents are opened once and kept, because reopening a PDF per page is far
    more expensive than rendering from an open handle.
    """

    def __init__(self, height: int = THUMB_HEIGHT) -> None:
        self.height = height
        self._documents: dict[str, object] = {}
        self._images: dict[tuple[str, int], QImage] = {}

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
        return doc

    def page_count(self, pdf: Path) -> int:
        try:
            return len(self._document(Path(pdf)))
        except Exception:
            return 0

    def image(self, pdf: Path, index: int) -> QImage | None:
        key = (str(pdf), index)
        cached = self._images.get(key)
        if cached is not None:
            return cached

        try:
            document = self._document(Path(pdf))
            if index < 0 or index >= len(document):
                return None
            page = document[index]
            # Render at a scale that yields roughly the requested height.
            scale = max(0.1, self.height / max(float(page.get_size()[1]) / 3.2, 1.0))
            pil = page.render(scale=scale).to_pil()
            pil.thumbnail((THUMB_WIDTH * 2, THUMB_HEIGHT * 2))

            buffer = io.BytesIO()
            pil.save(buffer, "PNG")
            image = QImage.fromData(buffer.getvalue(), "PNG")
            if image.isNull():
                return None
            self._images[key] = image
            return image
        except Exception:
            return None

    def invalidate(self, pdf: Path) -> None:
        key = str(pdf)
        self._documents.pop(key, None)
        for cached_key in [k for k in self._images if k[0] == key]:
            self._images.pop(cached_key, None)

    def render(self, pdf: Path, index: int, height: int) -> QImage | None:
        """Render one page at an arbitrary height, for the preview pane.

        Not cached: the preview changes as the user clicks around, and caching
        every size would grow without bound.
        """
        try:
            document = self._document(Path(pdf))
            if index < 0 or index >= len(document):
                return None
            page = document[index]
            intrinsics = page.get_size()
            scale = max(0.05, height / max(float(intrinsics[1]) / 3.2, 1.0))
            pil = page.render(scale=scale).to_pil()
            if pil.height > height:
                ratio = height / float(pil.height)
                pil = pil.resize((max(1, int(pil.width * ratio)), height))

            buffer = io.BytesIO()
            pil.save(buffer, "PNG")
            return QImage.fromData(buffer.getvalue(), "PNG")
        except Exception:
            return None

    def clear(self) -> None:
        self._documents.clear()
        self._images.clear()
