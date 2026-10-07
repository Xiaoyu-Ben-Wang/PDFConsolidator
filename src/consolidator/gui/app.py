"""GUI entry point."""

from __future__ import annotations

import sys
from pathlib import Path


def run(argv: list[str] | None = None, initial: list[str] | None = None) -> int:
    from PySide6.QtWidgets import QApplication

    from .main_window import MainWindow

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("PDF Consolidator")
    app.setOrganizationName("PDF Consolidator")

    window = MainWindow()

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
    return app.exec()
