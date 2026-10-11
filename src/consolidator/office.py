"""Run Office COM conversions in a subprocess and clean up after them.

Each conversion runs in its own subprocess so it can be killed:

1. Record the Office processes already running, so the user's own Office
   windows are never killed.
2. Run the worker with a timeout.
3. On timeout, kill the worker's process tree and any Office process that
   started while it ran.
4. After every run, including successful ones, kill any Office process that
   started while it ran.

Step 4 is needed because a worker can exit cleanly and still leave
WINWORD.EXE running.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field

# Prefix for the result line, so other stdout output is not parsed as a result.
RESULT_SENTINEL = "@@RESULT@@"

OFFICE_IMAGES = {
    "word": "WINWORD.EXE",
    "excel": "EXCEL.EXE",
    "powerpoint": "POWERPNT.EXE",
}

# The src/ directory, added to PYTHONPATH so the worker can import consolidator.
_SRC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ConversionError(RuntimeError):
    """A conversion failed. The message is intended to be shown to the user."""


@dataclass
class OfficeResult:
    ok: bool
    out: str
    detail: str = ""
    killed_pids: list[int] = field(default_factory=list)
    seconds: float = 0.0


def _run_quiet(cmd: list[str], timeout: int = 30) -> str:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, creationflags=_NO_WINDOW
        )
        return proc.stdout or ""
    except Exception:
        return ""


def running_pids(image: str) -> set[int]:
    """Return PIDs of processes with this image name, or an empty set on failure."""
    out = _run_quiet(["tasklist", "/FI", "IMAGENAME eq %s" % image, "/FO", "CSV", "/NH"])
    pids: set[int] = set()
    for line in out.splitlines():
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[1].isdigit():
            pids.add(int(parts[1]))
    return pids


def snapshot_office() -> dict[str, set[int]]:
    return {kind: running_pids(image) for kind, image in OFFICE_IMAGES.items()}


def kill_pid(pid: int) -> None:
    _run_quiet(["taskkill", "/F", "/T", "/PID", str(pid)])


def kill_new_office(before: dict[str, set[int]]) -> list[int]:
    """Kill Office processes that appeared since ``before``; return their PIDs."""
    killed: list[int] = []
    for kind, image in OFFICE_IMAGES.items():
        for pid in sorted(running_pids(image) - before.get(kind, set())):
            kill_pid(pid)
            killed.append(pid)
    return killed


def convert_office(kind: str, src: str, out: str, timeout: int = 180) -> OfficeResult:
    """Convert one Office document to PDF via COM, in a killable subprocess."""
    import time

    src = os.path.abspath(src)
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if os.path.exists(out):
        os.remove(out)

    before = snapshot_office()

    env = os.environ.copy()
    env["PYTHONPATH"] = _SRC_ROOT + os.pathsep + env.get("PYTHONPATH", "")

    cmd = [
        sys.executable,
        "-m",
        "consolidator.office_worker",
        "--kind",
        kind,
        "--src",
        src,
        "--out",
        out,
    ]

    started = time.monotonic()
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        creationflags=_NO_WINDOW,
    )

    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_pid(proc.pid)
        try:
            stdout, stderr = proc.communicate(timeout=15)
        except Exception:
            stdout, stderr = "", ""

    seconds = time.monotonic() - started

    # Always clean up: a clean exit can still leave an Office process running.
    killed = kill_new_office(before)

    if timed_out:
        return OfficeResult(
            False, out,
            "Office timed out after %ds; the process was terminated." % timeout,
            killed, seconds,
        )

    payload = None
    for line in (stdout or "").splitlines():
        if line.startswith(RESULT_SENTINEL):
            try:
                payload = json.loads(line[len(RESULT_SENTINEL):])
            except json.JSONDecodeError:
                payload = None

    if payload is None:
        tail = (stderr or "").strip().splitlines()
        detail = tail[-1] if tail else "worker produced no result (exit code %s)" % proc.returncode
        return OfficeResult(False, out, detail, killed, seconds)

    if not payload.get("ok"):
        return OfficeResult(False, out, payload.get("detail", "unknown failure"), killed, seconds)

    if not os.path.exists(out) or os.path.getsize(out) == 0:
        return OfficeResult(False, out, "no output file was produced", killed, seconds)

    return OfficeResult(True, out, "", killed, seconds)
