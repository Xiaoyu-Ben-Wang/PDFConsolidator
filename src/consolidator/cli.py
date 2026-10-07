"""Command line interface.

    python -m consolidator build <paths...> -o out.pdf
    python -m consolidator check
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, convert, libreoffice, pages, pipeline


def _cmd_build(args: argparse.Namespace) -> int:
    sources = pipeline.gather(args.paths, recursive=not args.no_recursive)
    if not sources:
        print("No input files found.", file=sys.stderr)
        return 2

    supported = [s for s in sources if convert.is_supported(s)]
    skipped = [s for s in sources if not convert.is_supported(s)]

    print("PDF Consolidator %s" % __version__)
    print("  %d file(s) selected, %d supported" % (len(sources), len(supported)))
    print("  page size : %s" % args.page_size)
    print("  output    : %s" % args.output)
    print()

    if not supported:
        print("Nothing supported to convert.", file=sys.stderr)
        return 2

    def on_event(message: str) -> None:
        print(message, flush=True)

    report = pipeline.build(
        supported,
        Path(args.output),
        page_size_name=args.page_size,
        margin_inches=args.margin,
        bookmarks=not args.no_bookmarks,
        on_event=on_event,
        keep_scratch=args.keep_scratch,
    )

    print()
    print("=" * 68)
    print("Converted : %d/%d" % (len(report.succeeded), len(report.outcomes)))
    for outcome in report.failed:
        print("  FAILED  %s" % outcome.source.name)
        print("          %s" % outcome.detail)
    for path in skipped:
        print("  SKIPPED %s (unsupported)" % path.name)

    if report.merged:
        merged = report.merged
        print("Merged    : %d document(s), %d page(s), %d bookmark(s)"
              % (merged.entries, merged.pages, merged.outline_items))
        for label, why in merged.skipped:
            print("  MERGE-SKIP %s - %s" % (label, why))
        print("Written   : %s (%.1f KB)"
              % (report.output, report.output.stat().st_size / 1024))
    else:
        print("No output produced.")

    print("Elapsed   : %.1fs" % report.seconds)
    return 0 if report.output else 1


def _cmd_check(args: argparse.Namespace) -> int:
    print("PDF Consolidator %s" % __version__)
    print("  python          : %s" % sys.version.split()[0])
    exe = libreoffice.find_soffice()
    print("  LibreOffice     : %s" % (exe or "NOT FOUND"))
    if exe is None:
        print("\n  Set CONSOLIDATOR_SOFFICE, or place a copy at")
        print("  <app>/libreoffice/program/soffice.exe")
    return 0 if exe else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="consolidator",
        description="Convert a mixed folder of documents into a single merged PDF.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="convert and merge files into one PDF")
    build.add_argument("paths", nargs="+", help="files and/or folders to include")
    build.add_argument("-o", "--output", required=True, help="destination PDF")
    build.add_argument("--page-size", default=pages.DEFAULT_PAGE_SIZE,
                       choices=sorted(pages.PAGE_SIZES),
                       help="page size for the assembled document (default: %(default)s)")
    build.add_argument("--margin", type=float, default=0.5,
                       help="margin in inches when fitting to a page size (default: %(default)s)")
    build.add_argument("--no-bookmarks", action="store_true",
                       help="do not create a bookmark per source document")
    build.add_argument("--no-recursive", action="store_true",
                       help="do not descend into subfolders")
    build.add_argument("--keep-scratch", action="store_true",
                       help="keep intermediate PDFs for inspection")
    build.set_defaults(func=_cmd_build)

    check = sub.add_parser("check", help="report the detected conversion engine")
    check.set_defaults(func=_cmd_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
