"""One lock for every use of pdfium in the process.

pdfium is not thread-safe - not even across separate documents - and two threads
inside it at once is a native crash, not an exception. The thumbnail renderer
and the preview run on two threads already, and the output verifier runs on a
third, so all of them take this lock around every pdfium call.
"""

from __future__ import annotations

import threading

PDFIUM_LOCK = threading.RLock()
