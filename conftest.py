"""Keep test files inside the project folder.

pytest and :mod:`tempfile` write to ``pytest-tmp/`` beside this file, not to the
system temp directory, so the suite does not depend on ``%TEMP%``.

Only :mod:`tempfile` is redirected here. The process environment is deliberately
left alone: overriding ``TMP``/``TEMP``/``TMPDIR`` for the whole process changes
what every spawned program sees, and LibreOffice crashes rendering HTML when that
happens. The application sets the environment for its own subprocesses explicitly
(see ``libreoffice.clean_env``), so it does not need it here.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

LOCAL_TEMP = Path(__file__).resolve().parent / "pytest-tmp"
LOCAL_TEMP.mkdir(exist_ok=True)

tempfile.tempdir = str(LOCAL_TEMP)
