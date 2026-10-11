"""Frozen entry point.

With no arguments the executable opens the GUI. With a subcommand (``build``,
``check``) it runs the CLI. One executable serves both uses.

Startup failures are logged next to the executable and shown in a dialog,
because a windowed build has no console.
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
    """Log a GUI startup failure and show it in a dialog."""
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

        # CLI commands never show a dialog. Errors go to stderr and a log,
        # and the exit code reports the result.
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
    except BaseException as exc:  # noqa: BLE001 - top-level handler
        _report_gui(exc)
        sys.exit(1)
