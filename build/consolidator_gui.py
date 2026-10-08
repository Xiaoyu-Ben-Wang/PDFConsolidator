"""Frozen entry point.

One executable serves both faces: run it with no arguments and the GUI opens;
pass a subcommand (``build``, ``check``) and it behaves as the CLI. That keeps a
single artifact to ship while preserving scripted use.

Startup failures are logged next to the executable and shown in a dialog, because
a windowed build has no console to print to and a silent failure is the worst
possible outcome for the user.
"""

from __future__ import annotations

import sys
import traceback

CLI_COMMANDS = {"build", "check", "gui", "selftest"}


def _log_path(name: str = "startup-error.log"):
    from pathlib import Path

    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path.cwd()
    return base / name


def _write_log(detail: str, name: str = "startup-error.log") -> None:
    try:
        _log_path(name).write_text(detail, encoding="utf-8")
    except Exception:
        pass


def _report_gui(exc: BaseException) -> None:
    """GUI startup failed: log it and say so, since there is no console."""
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    _write_log(detail)
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance() or QApplication([])
        QMessageBox.critical(
            None,
            "PDF Consolidator could not start",
            "The application failed to start.\n\n%s\n\nDetails were written to:\n%s"
            % (exc, _log_path()),
        )
    except Exception:
        pass


def main() -> int:
    argv = sys.argv[1:]

    if argv and argv[0] in CLI_COMMANDS:
        from consolidator.cli import main as cli_main

        # A command run from a script must never raise a dialog: report on
        # stderr and in a log, and let the exit code carry the outcome.
        try:
            return cli_main(argv)
        except SystemExit:
            raise
        except BaseException as exc:  # noqa: BLE001
            detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            sys.stderr.write(detail)
            _write_log(detail, "cli-error.log")
            return 1

    from consolidator.gui.app import run

    return run()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - last line of defence
        _report_gui(exc)
        sys.exit(1)
