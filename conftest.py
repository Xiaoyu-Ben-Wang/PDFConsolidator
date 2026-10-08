"""Keep the test suite independent of the system temp directory.

pytest creates its capture files through :mod:`tempfile`, so a ``%TEMP%`` that is
missing, full or unwritable stops the suite from starting at all — which says
nothing about the code under test.

Only :mod:`tempfile` is redirected here. The process environment is deliberately
left alone: overriding ``TMP``/``TEMP``/``TMPDIR`` for the whole process changes
what every spawned program sees, and LibreOffice crashes rendering HTML when that
happens. The application sets the environment for its own subprocesses explicitly
(see ``libreoffice.clean_env``), so it does not need it here.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _usable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / "_writetest"
        probe.write_bytes(b"x")
        probe.unlink()
        return True
    except Exception:
        return False


def _local_temp() -> Path:
    for candidate in (ROOT / "pytest-tmp", Path.cwd() / "pytest-tmp"):
        if _usable(candidate):
            return candidate
    return Path(tempfile.gettempdir())


tempfile.tempdir = str(_local_temp())
