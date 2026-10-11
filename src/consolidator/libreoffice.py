"""Convert documents to PDF with headless LibreOffice.

LibreOffice runs as a subprocess, which shows no dialogs and can be killed
safely.

Command-line behaviour that shapes this module:

1. **Startup is slow.** Each ``soffice`` start takes seconds, so converting
   many files in one invocation is much faster. The main entry point takes a
   batch.
2. **Output names are fixed.** Output is always ``<input-stem>.pdf`` in
   ``--outdir``, so inputs with the same stem collide. Callers must give each
   file a unique stem.
3. **Exit codes are unreliable.** ``soffice`` can exit 0 without output and
   exit 1 after succeeding. Success is checked by looking for a non-empty
   output file.

Two ``soffice`` processes that share a user profile fail without an error, so
each run gets its own profile via ``-env:UserInstallation``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import itertools
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Gives concurrent profiles in one process distinct names.
_profile_counter = itertools.count(1)

# Timeout in seconds for one batch, including startup. Large batches may need more.
DEFAULT_TIMEOUT = 600


# All running soffice processes, so they can be stopped when the application
# closes. A conversion blocks for up to minutes; if the worker thread is still
# running when Python shuts down, Qt aborts the process.
_running: set[subprocess.Popen] = set()
_running_lock = threading.Lock()
_stopping = threading.Event()


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill ``soffice`` and the ``soffice.bin`` process it starts.

    ``soffice.exe`` is only a launcher. ``Popen.kill`` and the timeout in
    ``subprocess.run`` kill only the launcher, leaving ``soffice.bin`` running
    with its files open.
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15, creationflags=_NO_WINDOW)
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def stop_all() -> None:
    """Stop all running conversions and block new ones.

    Used at shutdown. Running conversions report failure; partial outputs are
    not used because outputs are verified before use.
    """
    _stopping.set()
    with _running_lock:
        running = list(_running)
    for proc in running:
        _kill_tree(proc)


def stopping() -> bool:
    """Return True if :func:`stop_all` was called and :func:`resume` was not."""
    return _stopping.is_set()


def resume() -> None:
    """Allow conversions again after :func:`stop_all` (a window closed, the app still runs)."""
    _stopping.clear()


class _Gate:
    """Shared/exclusive lock for conversions.

    LibreOffice can fail when several of its processes run at once. Normal
    conversions take the gate shared. A retry of a failed conversion takes it
    exclusively: it waits for running conversions to finish and blocks new ones
    until it is done. The exclusive holder must still be able to convert, so
    the gate is re-entrant for that thread.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._shared = 0
        self._owner: int | None = None
        self._depth = 0

    def _mine(self) -> bool:
        return self._owner == threading.get_ident()

    def acquire_shared(self) -> bool:
        with self._condition:
            if self._mine():
                return False                    # this thread holds the gate exclusively
            while self._owner is not None:
                self._condition.wait()
            self._shared += 1
            return True

    def release_shared(self) -> None:
        with self._condition:
            self._shared -= 1
            self._condition.notify_all()

    def acquire_exclusive(self) -> None:
        with self._condition:
            if self._mine():
                self._depth += 1
                return
            while self._owner is not None:
                self._condition.wait()
            self._owner = threading.get_ident()
            self._depth = 1
            while self._shared:
                self._condition.wait()

    def release_exclusive(self) -> None:
        with self._condition:
            self._depth -= 1
            if self._depth == 0:
                self._owner = None
                self._condition.notify_all()


_gate = _Gate()


class alone:
    """Context manager that runs conversions with no other soffice running."""

    def __enter__(self):
        _gate.acquire_exclusive()
        return self

    def __exit__(self, *_exc) -> None:
        _gate.release_exclusive()


# Optional log file for failed invocations. It records LibreOffice's output,
# which helps diagnose failures without the user's documents and contains no
# document content.
failure_log: Path | None = None


def _log_failure(cmd: list[str], detail: str) -> None:
    if failure_log is None:
        return
    try:
        with open(failure_log, "a", encoding="utf-8") as handle:
            handle.write("\n==== %s  LibreOffice produced no output\n%s\n%s\n"
                         % (time.strftime("%Y-%m-%d %H:%M:%S"),
                            " ".join(cmd[-3:]), detail.strip()))
    except OSError:
        pass


def _run(cmd: list[str], timeout: int, env: dict[str, str]) -> tuple[str, str, bool]:
    """Run ``soffice`` under the shared gate. See :func:`_run_now`."""
    shared = _gate.acquire_shared()
    try:
        return _run_now(cmd, timeout, env)
    finally:
        if shared:
            _gate.release_shared()


def _run_now(cmd: list[str], timeout: int, env: dict[str, str]) -> tuple[str, str, bool]:
    """Run ``soffice`` to completion. Returns stdout, stderr and whether it timed out."""
    if _stopping.is_set():
        return "", "the application is closing", False
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace",
                            creationflags=_NO_WINDOW, env=env)
    with _running_lock:
        _running.add(proc)
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return stdout or "", stderr or "", False
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            try:
                stdout, stderr = proc.communicate(timeout=30)
            except Exception:
                stdout, stderr = "", ""
            return stdout or "", stderr or "", True
    finally:
        with _running_lock:
            _running.discard(proc)
        if proc.poll() is None:
            _kill_tree(proc)


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
    """Return the directory that holds the application's bundled resources.

    In a PyInstaller build this is the executable's folder; from source it is
    the repository root (two levels above this file).
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

    ``rmtree(ignore_errors=True)`` hides two problems:

    * ``soffice`` can hold its ``.~lock.<name>#`` file open briefly after it
      exits, so removal is retried.
    * The lock files are read-only, and Windows does not delete read-only files
      (``WinError 5``), so the attribute is cleared first.
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


def _remove_lock(directory: Path, name: str) -> None:
    """Delete LibreOffice's lock file for ``name``, which it marks read-only."""
    lock = directory / (".~lock.%s#" % name)
    try:
        if lock.exists():
            os.chmod(lock, 0o666)
            lock.unlink()
    except OSError:
        pass


def _profile_dir(workdir: Path) -> Path:
    """Create a per-run LibreOffice user profile at a short path.

    * Two ``soffice`` processes that share a profile fail without an error, so
      each run needs its own.
    * LibreOffice does not opt in to Windows long paths. Its profile contains
      deep internal paths, and placing it under a deep working directory pushes
      them past ``MAX_PATH`` and crashes LibreOffice.

    The profile is therefore created under a short fixed directory beside the
    application. ``workdir`` is accepted but not used for the profile location.
    """
    home = app_root() / "lo-profiles"
    home.mkdir(parents=True, exist_ok=True)
    profile = home / ("p%d-%d" % (os.getpid(), next(_profile_counter)))
    if profile.exists():
        _remove_tree(profile)
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def clean_env(tmpdir: Path | None = None) -> dict[str, str]:
    """Return the environment for launching ``soffice``.

    **1. Remove Python variables.** LibreOffice bundles its own Python, and its
    startup reads ``PYTHONPATH``/``PYTHONHOME``. The subprocess inherits this
    application's environment (or a user's global ``PYTHONPATH``), which points
    at the wrong libraries and makes startup fail with::

        Could not find platform independent libraries <prefix>

    **2. Set TMP/TEMP to a writable directory.** LibreOffice writes temporary
    files while loading a document; if it cannot, it fails with ``Error: source
    file could not be loaded``. The system temp directory may be missing, full
    or not writable, so ``tmpdir`` is used instead when given.
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

    # LibreOffice fails on a relative source path or --outdir with "Error: source
    # file could not be loaded", so all paths passed to it are absolute. Results
    # stay keyed by the caller's original paths.
    outdir = workdir.resolve()
    targets = [(src, src.resolve()) for src in sources]

    profile = _profile_dir(outdir)
    lo_tmp = outdir / "lo_tmp"

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
        str(outdir),
    ] + [str(target) for _src, target in targets]

    # Remove old outputs so they are not mistaken for new ones. Also remove old
    # locks: LibreOffice leaves a read-only ``.~lock.<name>#`` beside each PDF it
    # writes, even on success, and cannot write that name again while the lock
    # exists (it aborts with an I/O error and no output).
    for src in sources:
        stale = outdir / (src.stem + ".pdf")
        if stale.exists():
            stale.unlink()
        _remove_lock(outdir, stale.name)

    started = time.monotonic()
    stdout, stderr, timed_out = _run(cmd, timeout, clean_env(lo_tmp))

    seconds = time.monotonic() - started

    result = BatchResult(seconds=seconds, stdout=stdout, stderr=stderr, timed_out=timed_out)

    # A crash dump in the profile can explain an empty result.
    crashed = (profile / "crash" / "dump.ini").exists()

    # Ignore the exit code; check each expected output file.
    missing = []
    for src in sources:
        out = outdir / (src.stem + ".pdf")
        if out.exists() and out.stat().st_size > 0:
            result.produced[str(src)] = str(out)
        else:
            missing.append(src)

    if missing:
        # LibreOffice can write a crash dump on a normal exit too, so report a
        # crash only when the whole batch produced nothing. Otherwise use the
        # last line of stderr, which usually gives the reason.
        detail = (stderr or stdout or "").strip().splitlines()
        fallback = detail[-1] if detail else "no output produced"
        if timed_out:
            reason = "LibreOffice timed out after %ds" % timeout
        elif crashed and not result.produced:
            reason = "LibreOffice crashed while converting this file"
        else:
            reason = fallback
        for src in missing:
            result.failed[str(src)] = reason
        _log_failure(cmd, "%s (crash dump: %s, %d of %d produced)\nstderr: %s\nstdout: %s"
                     % (reason, "yes" if crashed else "no", len(result.produced),
                        len(sources), (stderr or "").strip()[-2000:],
                        (stdout or "").strip()[-1000:]))

    for src in sources:
        _remove_lock(outdir, src.stem + ".pdf")

    # The profile is large and used only by this run.
    _remove_tree(profile)

    return result


def convert_one(
    src: Path,
    dest: Path,
    timeout: int = DEFAULT_TIMEOUT,
    workdir: Path | None = None,
) -> Path:
    """Convert a single file to ``dest``.

    ``soffice`` always names its output after the input, so the file is
    converted in a scratch directory and then moved to ``dest``. The scratch
    directory is removed only if this function created it.
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

