"""Parent-side driver for Office COM conversions.

Every conversion runs in its own killable subprocess. This module owns the safety
net around that subprocess:

1. Snapshot the Office processes already running *before* we start, so a user's
   open Word window is never mistaken for one of ours.
2. Run the worker with a hard timeout.
3. On timeout, kill the worker's process tree, then kill any Office process that
   appeared while it ran.
4. Even on apparent success, sweep up any Office process we caused to appear.

Step 4 matters: a worker can exit cleanly while leaving WINWORD.EXE behind, which
is exactly the failure this design exists to prevent.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field

# Results are sentinel-prefixed so ordinary stdout noise cannot be mistaken for them.
RESULT_SENTINEL = "@@RESULT@@"

OFFICE_IMAGES = {
    "word": "WINWORD.EXE",
    "excel": "EXCEL.EXE",
    "powerpoint": "POWERPNT.EXE",
}

# src/ - so the worker subprocess can `import consolidator`
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
    """PIDs of processes with this image name, or an empty set if we cannot tell."""
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

    # Always sweep: even a clean exit can leave an Office process behind.
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
