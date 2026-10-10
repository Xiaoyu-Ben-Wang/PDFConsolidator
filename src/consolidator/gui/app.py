"""GUI entry point."""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from pathlib import Path

# Threads the pipeline starts (its conversion pools) get the same generous stack
# as the Qt workers: deep recursion on a malformed file must be an exception, not
# a stack overflow that ends the process. Reserved address space, not memory.
THREAD_STACK_BYTES = 16 * 1024 * 1024

LOG_NAME = "consolidator-errors.log"

_log_handle = None


def _open_log():
    """The error log, beside the application if it is writable, else in scratch."""
    global _log_handle
    if _log_handle is not None:
        return _log_handle
    from .. import libreoffice

    for directory in (libreoffice.app_root(), Path.cwd()):
        try:
            _log_handle = open(directory / LOG_NAME, "a", encoding="utf-8", buffering=1)
            return _log_handle
        except OSError:
            continue
    return None


def log_error(title: str, detail: str) -> None:
    handle = _open_log()
    if handle is None:
        return
    try:
        handle.write("\n==== %s  %s\n%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), title, detail))
        handle.flush()
    except Exception:
        pass


def install_safety_nets(window_getter) -> None:
    """Make an unexpected error a logged, visible message instead of a vanishing one.

    An exception escaping a Qt slot is printed to a console a windowed build does
    not have, and the action the user took silently does nothing. It is logged
    here and named in the status bar, and the application keeps running.
    ``faulthandler`` writes the Python stack of a *native* crash to the same log,
    which is the only trace such a crash would otherwise leave.
    """
    handle = _open_log()
    if handle is not None:
        # LibreOffice runs that produce nothing are described in the same log.
        from .. import libreoffice

        libreoffice.failure_log = Path(handle.name)
        try:
            import faulthandler

            faulthandler.enable(file=handle, all_threads=True)
        except Exception:
            pass

    def report(kind: str, exc_type, exc, tb) -> None:
        detail = "".join(traceback.format_exception(exc_type, exc, tb))
        log_error(kind, detail)
        window = window_getter()
        if window is not None and threading.current_thread() is threading.main_thread():
            try:
                window.status.showMessage(
                    "Something went wrong (%s: %s) - it was logged to %s; "
                    "the application is still running."
                    % (exc_type.__name__, exc, LOG_NAME))
            except Exception:
                pass

    def excepthook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            return
        report("unhandled exception", exc_type, exc, tb)

    def thread_excepthook(args) -> None:
        if args.exc_type is SystemExit:
            return
        report("unhandled exception in thread %s" % getattr(args.thread, "name", "?"),
               args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook


def run(argv: list[str] | None = None, initial: list[str] | None = None) -> int:
    from PySide6.QtWidgets import QApplication

    from .main_window import MainWindow

    try:
        threading.stack_size(THREAD_STACK_BYTES)
    except (ValueError, RuntimeError):
        pass

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("PDF Consolidator")
    app.setOrganizationName("PDF Consolidator")

    window = MainWindow()
    install_safety_nets(lambda: window)

    # Scratch left by a run that ended abruptly is removed in the background.
    from .. import pipeline

    threading.Thread(target=pipeline.sweep_stale_scratch, daemon=True).start()

    # Paths given on the command line are pre-loaded, so the app can be used as
    # a shell extension target or launched with a selection.
    if initial:
        from .. import pipeline

        paths: list[Path] = []
        for raw in initial:
            candidate = Path(raw)
            if candidate.is_dir():
                paths.extend(pipeline.gather([str(candidate)]))
            else:
                paths.append(candidate)
        if paths:
            window._add(paths)

    window.show()
    code = app.exec()

    if window.workers_running():
        # A worker that would not stop in time. Letting Python tear down its
        # QThread while it runs makes Qt abort the process - a crash report for
        # closing a window - so leave without the teardown. Nothing is lost:
        # outputs are written atomically, and scratch the cleanup under way does
        # not finish is swept on the next start (pipeline.sweep_stale_scratch).
        log_error("exit", "left with a background task still running")
        from .. import libreoffice

        libreoffice.stop_all()      # nothing the worker started may outlive us
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(code)
    return code
