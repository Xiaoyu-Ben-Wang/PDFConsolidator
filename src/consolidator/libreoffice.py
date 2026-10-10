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
import itertools
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Distinguishes concurrent profiles within one process.
_profile_counter = itertools.count(1)

# A start-up plus conversion budget for a batch. Large batches may need more.
DEFAULT_TIMEOUT = 600


# Every soffice this process has running, so they can all be stopped at once when
# the application closes. A conversion is a blocking call that can last minutes;
# without a way to end it, closing the window mid-run either waited that long or
# left the worker thread running while Python shut down underneath it - which Qt
# answers by aborting the whole process.
_running: set[subprocess.Popen] = set()
_running_lock = threading.Lock()
_stopping = threading.Event()


def _kill_tree(proc: subprocess.Popen) -> None:
    """End ``soffice`` *and* the ``soffice.bin`` it starts.

    ``soffice.exe`` is only a launcher. Killing it alone - which is all
    ``Popen.kill`` and ``subprocess.run``'s timeout do - leaves the real process
    running, holding the files it was converting open.
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
    """Stop every running conversion, and refuse to start new ones.

    For shutdown. The conversions in progress fail and report so; nothing is
    half-written in their place, because outputs are verified before use.
    """
    _stopping.set()
    with _running_lock:
        running = list(_running)
    for proc in running:
        _kill_tree(proc)


def stopping() -> bool:
    """Whether :func:`stop_all` has been called and not yet undone."""
    return _stopping.is_set()


def resume() -> None:
    """Allow conversions again after :func:`stop_all` (a window closed, the app did not)."""
    _stopping.clear()


class _Gate:
    """Lets conversions run side by side, or one run entirely alone.

    LibreOffice is known to fall over when several of its processes work at once
    on one machine. Ordinary conversions share the gate; a retry of something
    that failed takes it exclusively - it waits for the running ones to finish,
    and no new one starts until it is done - so the retry has no concurrency to
    lose to. The thread holding it exclusively may still convert (that is the
    point), so the gate is re-entrant for that thread.
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
                return False                    # inside our own exclusive section
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
    """``with libreoffice.alone():`` - convert with no other soffice running."""

    def __enter__(self):
        _gate.acquire_exclusive()
        return self

    def __exit__(self, *_exc) -> None:
        _gate.release_exclusive()


# Where failed invocations are described, if the application has set it. A
# failure on a user's machine is otherwise impossible to diagnose without their
# documents; this records what LibreOffice said, which names no content.
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
    """Run ``soffice`` to completion, through the gate. See :func:`_run_now`."""
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
    """A per-run LibreOffice user profile, at a deliberately **short** path.

    Two constraints shape this:

    * Two ``soffice`` processes sharing a profile fail *silently*, so every run
      needs its own.
    * LibreOffice is not manifested for long paths. Its profile holds very deep
      internal paths, and nesting the profile under a deep working directory
      pushes those past ``MAX_PATH`` and crashes it - Windows' long-path setting
      does not help, because it only applies to applications that opt in.

    So the profile lives at a short fixed prefix beside the application rather
    than under the run's working directory. ``workdir`` is accepted so callers
    have one place to pass it, but is deliberately not where the profile goes.
    """
    home = app_root() / "lo-profiles"
    home.mkdir(parents=True, exist_ok=True)
    profile = home / ("p%d-%d" % (os.getpid(), next(_profile_counter)))
    if profile.exists():
        _remove_tree(profile)
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

    # LibreOffice cannot cope with a relative source path or a relative --outdir:
    # it reports "Error: source file could not be loaded" and produces nothing,
    # with no hint that the path was the problem. Everything it is told about is
    # therefore absolute. The result dictionaries stay keyed by the paths the
    # caller passed, so callers can still look up their own paths.
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

    # A stale output would masquerade as success, so clear expected names first.
    # So must a stale *lock*: LibreOffice leaves a read-only ``.~lock.<name>#``
    # beside every PDF it writes, even on success, and while one exists it cannot
    # write that name again - it fails with an I/O abort and no output, which
    # read as "LibreOffice crashed" on every retry or re-conversion into the
    # same directory.
    for src in sources:
        stale = outdir / (src.stem + ".pdf")
        if stale.exists():
            stale.unlink()
        _remove_lock(outdir, stale.name)

    started = time.monotonic()
    stdout, stderr, timed_out = _run(cmd, timeout, clean_env(lo_tmp))

    seconds = time.monotonic() - started

    result = BatchResult(seconds=seconds, stdout=stdout, stderr=stderr, timed_out=timed_out)

    # A crash dump in the profile explains an otherwise silent empty result.
    crashed = (profile / "crash" / "dump.ini").exists()

    # Never trust the exit code - verify each expected artifact.
    missing = []
    for src in sources:
        out = outdir / (src.stem + ".pdf")
        if out.exists() and out.stat().st_size > 0:
            result.produced[str(src)] = str(out)
        else:
            missing.append(src)

    if missing:
        # LibreOffice leaves a crash dump behind on an ordinary exit as well as a
        # real one, so "there is a dump" only explains a failure when the whole
        # invocation produced nothing. Blaming a crash for one file in a batch
        # that otherwise converted was simply wrong, and hid the real reason -
        # which is usually on stderr.
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

    # The profile is large and belongs to this run only.
    _remove_tree(profile)

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

