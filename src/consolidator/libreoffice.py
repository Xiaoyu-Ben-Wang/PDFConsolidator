"""LibreOffice headless conversion engine.

LibreOffice is driven as a subprocess rather than through COM, which makes it
deterministic, dialog-free and safely killable - the properties Office COM could
not provide here.

Three behaviours of the command line drive the design:

1. **Startup dominates.** Converting N files in one invocation is dramatically
   faster than N invocations, because each ``soffice`` start costs seconds. So the
   primary entry point takes a batch.
2. **The output name is not controllable.** Output is always
   ``<input-stem>.pdf`` in ``--outdir``, so two inputs sharing a stem collide. The
   caller must give each file a unique stem in its own scratch directory.
3. **Exit codes lie.** ``soffice`` can exit 0 having done nothing, and exit 1
   having succeeded. Every result is therefore verified by checking that a
   non-empty output file actually exists.

A further trap: two ``soffice`` processes sharing a user profile fail *silently*.
Each run therefore gets its own profile directory via ``-env:UserInstallation``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# A start-up plus conversion budget for a batch. Large batches may need more.
DEFAULT_TIMEOUT = 600


class LibreOfficeError(RuntimeError):
    """Raised when the engine is missing or a conversion cannot be completed."""


@dataclass
class BatchResult:
    """Outcome of one soffice invocation."""

    produced: dict[str, str] = field(default_factory=dict)  # source path -> output path
    failed: dict[str, str] = field(default_factory=dict)    # source path -> reason
    seconds: float = 0.0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


def app_root() -> Path:
    """Directory that holds the application's bundled resources.

    Frozen by PyInstaller this is the folder containing the executable; running
    from source it is the repository root (two levels above this file).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent.parent


def find_soffice() -> Path | None:
    """Locate ``soffice.exe``: explicit override, bundled copy, then installed."""
    override = os.environ.get("CONSOLIDATOR_SOFFICE")
    if override and Path(override).exists():
        return Path(override)

    root = app_root()
    candidates = [
        root / "libreoffice" / "program" / "soffice.exe",
        root / "vendor" / "libreoffice" / "program" / "soffice.exe",
        Path(r"C:\Program Files\LibreOffice\program\soffice.exe"),
        Path(r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate

    found = shutil.which("soffice")
    return Path(found) if found else None


def require_soffice() -> Path:
    exe = find_soffice()
    if exe is None:
        raise LibreOfficeError(
            "LibreOffice was not found. Expected a bundled copy at "
            "<app>/libreoffice/program/soffice.exe, or set CONSOLIDATOR_SOFFICE."
        )
    return exe


def _remove_tree(path: Path, attempts: int = 6, delay: float = 0.4) -> None:
    """Remove a scratch directory, including LibreOffice's lock files.

    Two obstacles, both silent under ``rmtree(ignore_errors=True)``:

    * ``soffice`` can still hold its ``.~lock.<name>#`` file open for a moment
      after it appears to have exited, so removal is retried briefly.
    * It marks those lock files **read-only**, and Windows refuses to delete a
      read-only file (``WinError 5``). The attribute must be cleared first -
      otherwise every conversion leaves its scratch directory behind.
    """
    for _ in range(attempts):
        if not path.exists():
            return

        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    os.chmod(os.path.join(root, name), 0o666)
                except OSError:
                    pass

        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(delay)


def _profile_dir(workdir: Path) -> Path:
    """A per-run LibreOffice user profile.

    Two ``soffice`` processes sharing a profile fail *silently*, so every run
    gets its own.
    """
    profile = workdir / "lo_profile"
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def clean_env(tmpdir: Path | None = None) -> dict[str, str]:
    """Environment for launching ``soffice``.

    Two corrections are applied, both learned the hard way:

    **1. Strip the host's Python variables.** LibreOffice bundles its own Python
    and its bootstrap reads ``PYTHONPATH``/``PYTHONHOME``. Because this
    application is itself Python, those variables point at the *host's*
    libraries and break LibreOffice's start-up with::

        Could not find platform independent libraries <prefix>

    A user with ``PYTHONPATH`` set globally would hit this too, and a subprocess
    inherits our environment, so they must be removed explicitly.

    **2. Point TMP/TEMP at a directory we know is writable.** LibreOffice creates
    work files while loading a document, and if it cannot, it fails with the
    unhelpful ``Error: source file could not be loaded``. The system temp
    directory can be missing, full, or - as on the machine this was developed on
    - simply not writable by the process launching it. Giving the engine its own
    scratch directory removes an entire class of environmental failure, which
    matters a great deal for an application meant to run from a USB stick on an
    unfamiliar machine.
    """
    env = os.environ.copy()
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONEXECUTABLE",
        "PYTHONWARNINGS",
        "UNO_PATH",
        "URE_BASE_PATH",
    ):
        env.pop(name, None)

    if tmpdir is not None:
        tmpdir = Path(tmpdir)
        tmpdir.mkdir(parents=True, exist_ok=True)
        for name in ("TMP", "TEMP", "TMPDIR"):
            env[name] = str(tmpdir)

    return env


def convert_batch(
    sources: list[Path],
    workdir: Path,
    timeout: int = DEFAULT_TIMEOUT,
) -> BatchResult:
    """Convert several files in a single ``soffice`` invocation.

    Each source must have a unique stem; outputs land in ``workdir`` named
    ``<stem>.pdf``. Callers that cannot guarantee unique stems should convert
    files one at a time.
    """
    exe = require_soffice()
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    sources = [Path(s) for s in sources]
    if not sources:
        return BatchResult()

    profile = _profile_dir(workdir)
    lo_tmp = workdir / "lo_tmp"

    cmd = [
        str(exe),
        "-env:UserInstallation=%s" % profile.as_uri(),
        "--headless",
        "--norestore",
        "--invisible",
        "--nolockcheck",
        "--nodefault",
        "--nofirststartwizard",
        "--convert-to",
        "pdf",
        "--outdir",
        str(workdir),
    ] + [str(s) for s in sources]

    # A stale output would masquerade as success, so clear expected names first.
    for src in sources:
        stale = workdir / (src.stem + ".pdf")
        if stale.exists():
            stale.unlink()

    started = time.time()
    timed_out = False
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            creationflags=_NO_WINDOW, env=clean_env(lo_tmp),
        )
        stdout, stderr = proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = (exc.stderr or b"").decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")

    seconds = time.time() - started

    result = BatchResult(seconds=seconds, stdout=stdout, stderr=stderr, timed_out=timed_out)

    # Never trust the exit code - verify each expected artifact.
    for src in sources:
        out = workdir / (src.stem + ".pdf")
        if out.exists() and out.stat().st_size > 0:
            result.produced[str(src)] = str(out)
        elif timed_out:
            result.failed[str(src)] = "LibreOffice timed out after %ds" % timeout
        else:
            detail = (stderr or stdout or "").strip().splitlines()
            result.failed[str(src)] = detail[-1] if detail else "no output produced"

    return result


def convert_one(
    src: Path,
    dest: Path,
    timeout: int = DEFAULT_TIMEOUT,
    workdir: Path | None = None,
) -> Path:
    """Convert a single file to ``dest``.

    ``soffice`` always names its output after the input, so the file is converted
    into a private directory and then moved into place. A private directory this
    function created is removed again; one the caller supplied is left alone.
    """
    src = Path(src)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    owns_scratch = workdir is None
    scratch = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="consolidator_lo_"))

    try:
        result = convert_batch([src], scratch, timeout=timeout)

        produced = result.produced.get(str(src))
        if not produced:
            reason = result.failed.get(str(src), "conversion failed")
            raise LibreOfficeError("%s: %s" % (src.name, reason))

        shutil.move(produced, dest)
        return dest
    finally:
        if owns_scratch:
            _remove_tree(scratch)

