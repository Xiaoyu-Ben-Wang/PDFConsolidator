"""Qt front end for the PDF Consolidator."""

from __future__ import annotations


def main(argv: list[str] | None = None) -> int:
    from .app import run

    return run(argv)
