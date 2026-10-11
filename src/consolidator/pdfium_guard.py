"""Process-wide lock for all pdfium calls.

pdfium is not thread-safe, even across separate documents, and concurrent calls
crash the process instead of raising. The thumbnail renderer, the preview and
the output verifier run on separate threads, so each holds this lock around
every pdfium call.
"""

from __future__ import annotations

import threading

PDFIUM_LOCK = threading.RLock()
