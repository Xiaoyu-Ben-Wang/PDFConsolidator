"""GUI entry point."""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from pathlib import Path

# Stack size for threads the pipeline starts (its conversion pools), matching
# the Qt workers. Deep recursion on a malformed file then raises an exception
# instead of overflowing the stack and ending the process. This reserves address
# space, not memory.
THREAD_STACK_BYTES = 16 * 1024 * 1024

LOG_NAME = "consolidator-errors.log"

_log_handle = None


def _open_log():
    """Open the error log beside the application, or in the working directory."""
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
    """Log unhandled exceptions and show them in the status bar.

    An exception raised in a Qt slot is otherwise printed to a console that a
    windowed build does not have. Exceptions are logged, reported in the status
    bar, and the application keeps running. ``faulthandler`` writes the Python
    stack of native crashes to the same log.
    """
    handle = _open_log()
    if handle is not None:
        # LibreOffice runs that produce no output are logged to the same file.
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

    # Remove scratch directories left by earlier runs, in the background.
    from .. import pipeline

    threading.Thread(target=pipeline.sweep_stale_scratch, daemon=True).start()

    # Load paths given on the command line, e.g. from a shell extension.
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
        # A worker did not stop in time. Destroying a running QThread makes Qt
        # abort the process, so exit without teardown. Outputs are written
        # atomically, and leftover scratch is removed on the next start
        # (pipeline.sweep_stale_scratch).
        log_error("exit", "left with a background task still running")
        from .. import libreoffice

        libreoffice.stop_all()      # end soffice processes the worker started
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(code)
    return code
