"""Keep test files inside the project folder.

pytest and :mod:`tempfile` write to ``pytest-tmp/`` beside this file, not to the
system temp directory, so the suite does not depend on ``%TEMP%``.

Only :mod:`tempfile` is redirected. The process environment is not changed:
overriding ``TMP``/``TEMP``/``TMPDIR`` for the whole process affects every spawned
program, and LibreOffice then crashes when rendering HTML. The application sets
the environment for its own subprocesses (see ``libreoffice.clean_env``).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

LOCAL_TEMP = Path(__file__).resolve().parent / "pytest-tmp"
LOCAL_TEMP.mkdir(exist_ok=True)

tempfile.tempdir = str(LOCAL_TEMP)
