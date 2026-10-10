"""Command line interface.

    python -m consolidator build <paths...> -o out.pdf [options]
    python -m consolidator check
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import (
    __version__,
    assemble,
    convert,
    email_items,
    libreoffice,
    pages,
    pipeline,
)

# What is pulled out of an email item unless told otherwise. Attached messages are
# included: a forwarded email is ordinary mail, and its contents are the point.
DEFAULT_EXTRACT = ("pdf", "images", "documents", "embedded")

# Older spelling of the "documents" category, still accepted so a script or a
# habit built on it keeps working.
CATEGORY_ALIASES = {"office": "documents"}


def _cmd_build(args: argparse.Namespace) -> int:
    sources = pipeline.gather(args.paths, recursive=not args.no_recursive)
    if not sources:
        print("No input files found.", file=sys.stderr)
        return 2

    supported = [s for s in sources if convert.is_supported(s)]
    unsupported = [s for s in sources if not convert.is_supported(s)]

    chosen = {CATEGORY_ALIASES.get(c.strip().lower(), c.strip().lower())
              for c in args.extract.split(",") if c.strip()}
    unknown = chosen - set(email_items.CATEGORIES)
    if unknown:
        print("Unknown extract category: %s" % ", ".join(sorted(unknown)), file=sys.stderr)
        print("Valid categories: %s" % ", ".join(email_items.CATEGORIES), file=sys.stderr)
        return 2

    policy = email_items.ExtractionPolicy(
        body=not args.no_body,
        pdf="pdf" in chosen,
        images="images" in chosen,
        documents="documents" in chosen,
        embedded="embedded" in chosen,
        other="other" in chosen,
        max_attachment_bytes=int(args.max_attachment_mb * 1024 * 1024),
        max_depth=args.max_depth,
    )

    print("PDF Consolidator %s" % __version__)
    print("  %d file(s) selected, %d supported" % (len(sources), len(supported)))
    print("  page size : %s" % args.page_size)
    print("  extract   : %s%s" % (args.extract, "" if not args.no_body else " (no body)"))
    print("  output    : %s" % args.output)
    print()

    if not supported:
        print("Nothing supported to convert.", file=sys.stderr)
        return 2

    options = assemble.MergeOptions(
        separator=args.separator,
        numbering=assemble.PageNumbering(
            mode=args.page_numbers,
            position=args.number_position,
            template=args.number_format,
            start=args.number_start,
        ),
        header=assemble.HeaderFooter(args.header_left, args.header_center, args.header_right),
        footer=assemble.HeaderFooter(args.footer_left, args.footer_center, args.footer_right),
        title=args.title or Path(args.output).stem,
        author=args.author,
        subject=args.subject,
    )

    try:
        report = pipeline.build(
            supported,
            Path(args.output),
            page_size_name=args.page_size,
            margin_inches=args.margin,
            bookmarks=not args.no_bookmarks,
            policy=policy,
            options=options,
            on_event=lambda message: print(message, flush=True),
            keep_scratch=args.keep_scratch,
        )
    except OSError as exc:
        # Most often an unwritable destination; a traceback would only obscure it.
        print()
        print("Could not write the output: %s" % exc, file=sys.stderr)
        print("Check that the destination folder exists and is writable.", file=sys.stderr)
        return 1

    print()
    print("=" * 70)
    print("Converted : %d/%d source(s), %d segment(s)"
          % (len(report.succeeded), len(report.sources), len(report.segments)))

    for result in report.failed:
        detail = "; ".join(s.detail for s in result.segments if s.detail) or "no output"
        print("  FAILED  %s" % result.source.name)
        print("          %s" % detail)

    excluded = [s for s in report.skipped_segments if "excluded by the extraction policy" in s.detail]
    if excluded:
        print("  EXCLUDED by policy: %d segment(s)" % len(excluded))
        for segment in excluded[:10]:
            print("          %s" % segment.label)

    other_skips = [s for s in report.skipped_segments if s not in excluded]
    for segment in other_skips:
        print("  SKIPPED %s" % segment.label)
        print("          %s" % segment.detail)

    for path in unsupported:
        print("  SKIPPED %s (unsupported)" % path.name)

    if report.merged:
        merged = report.merged
        print("Merged    : %d segment(s), %d page(s), %d bookmark(s)"
              % (merged.entries, merged.pages, merged.outline_items))
        for label, why in merged.skipped:
            print("  MERGE-SKIP %s - %s" % (label, why))
        check = merged.verification
        if check is not None:
            if check.error:
                print("Checked   : could not compare with the sources (%s)" % check.error)
            elif check.problems:
                print("Checked   : %d of %d page(s) are MISSING CONTENT their source has"
                      % (len(check.problems), check.checked))
                for problem in check.problems:
                    print("  MISSING %s" % problem.describe())
            else:
                print("Checked   : all %d page(s) match their sources" % check.checked)
        size_kb = report.output.stat().st_size / 1024
        print("Written   : %s (%.1f KB)" % (report.output, size_kb))
    else:
        print("No output produced.")

    print("Elapsed   : %.1fs" % report.seconds)
    return 0 if report.output else 1


def _cmd_check(args: argparse.Namespace) -> int:
    print("PDF Consolidator %s" % __version__)
    print("  python          : %s" % sys.version.split()[0])
    exe = libreoffice.find_soffice()
    print("  LibreOffice     : %s" % (exe or "NOT FOUND"))
    try:
        import extract_msg

        print("  extract-msg     : %s" % extract_msg.__version__)
    except Exception:
        print("  extract-msg     : NOT AVAILABLE (Outlook .msg support disabled)")

    if exe is None:
        print("\n  Set CONSOLIDATOR_SOFFICE, or place a copy at")
        print("  <app>/libreoffice/program/soffice.exe")
    return 0 if exe else 1


def _cmd_selftest(args: argparse.Namespace) -> int:
    """Convert generated samples end to end and report.

    Exists because the shipped build is a windowed executable with no console:
    without this there is no way to prove that the frozen application can still
    find its bundled LibreOffice and produce a PDF. Writes to stdout when there
    is one, and always to ``selftest.log`` beside the executable.
    """
    import tempfile
    import traceback

    lines: list[str] = []

    def say(message: str = "") -> None:
        lines.append(message)
        print(message, flush=True)

    say("PDF Consolidator %s self-test" % __version__)
    say("  python      : %s" % sys.version.split()[0])
    say("  frozen      : %s" % bool(getattr(sys, "frozen", False)))
    say("  executable  : %s" % sys.executable)

    exe = libreoffice.find_soffice()
    say("  LibreOffice : %s" % (exe or "NOT FOUND"))
    if exe is None:
        say()
        say("RESULT: FAIL - no conversion engine")
        _write_selftest_log(lines)
        return 1

    ok = True
    try:
        import io

        from PIL import Image
        from pypdf import PdfReader

        work = Path(tempfile.mkdtemp(prefix="selftest_"))
        say("  scratch     : %s" % work)

        source_dir = work / "in"
        source_dir.mkdir(parents=True, exist_ok=True)

        # Plain text, an image, and an RTF document - the last one exercises
        # LibreOffice, which is the part most likely to break in a bundle.
        # (HTML is deliberately not an accepted input, so RTF stands in.)
        (source_dir / "notes.txt").write_text("self test\n" * 40, encoding="utf-8")
        Image.new("RGB", (600, 400), (200, 220, 240)).save(source_dir / "swatch.png")
        (source_dir / "memo.rtf").write_text(
            r"{\rtf1\ansi\deff0{\fonttbl{\f0 Calibri;}}"
            r"\f0\fs28 Self test\par"
            r"This paragraph proves LibreOffice converted it.\par"
            r"}",
            encoding="ascii",
        )

        dest = work / "out.pdf"
        report = pipeline.build(
            pipeline.gather([str(source_dir)]),
            dest,
            page_size_name="letter",
        )

        say("  converted   : %d/%d source(s), %d segment(s)"
            % (len(report.succeeded), len(report.sources), len(report.segments)))
        for result in report.failed:
            ok = False
            say("    FAILED %s: %s"
                % (result.source.name,
                   "; ".join(s.detail for s in result.segments if s.detail)))

        if not dest.exists():
            ok = False
            say("  output      : MISSING")
        else:
            pages = len(PdfReader(str(dest)).pages)
            say("  output      : %s, %d page(s), %d bytes"
                % (dest.name, pages, dest.stat().st_size))
            if pages < 3:
                ok = False
                say("    expected at least 3 pages")

        import shutil as _shutil

        _shutil.rmtree(work, ignore_errors=True)

    except Exception:
        ok = False
        say("  EXCEPTION:")
        for line in traceback.format_exc().splitlines():
            say("    " + line)

    say()
    say("RESULT: %s" % ("PASS" if ok else "FAIL"))
    _write_selftest_log(lines)
    return 0 if ok else 1


def _write_selftest_log(lines: list[str]) -> None:
    try:
        if getattr(sys, "frozen", False):
            base = Path(sys.executable).parent
        else:
            base = Path.cwd()
        (base / "selftest.log").write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass


def _cmd_gui(args: argparse.Namespace) -> int:
    try:
        from .gui.app import run
    except ImportError as exc:
        print("The graphical interface needs PySide6: %s" % exc, file=sys.stderr)
        print("Install it with: pip install PySide6-Essentials", file=sys.stderr)
        return 3
    return run(initial=args.paths or None)


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
                       help="do not create a bookmark per segment")
    build.add_argument("--no-recursive", action="store_true",
                       help="do not descend into subfolders")
    build.add_argument("--keep-scratch", action="store_true",
                       help="keep intermediate PDFs for inspection")

    email = build.add_argument_group("email extraction")
    email.add_argument("--extract", default=",".join(DEFAULT_EXTRACT),
                       help="attachment categories to extract from Outlook items and email, "
                            "comma separated (default: %s). Choices: %s"
                            % (",".join(DEFAULT_EXTRACT), ", ".join(email_items.CATEGORIES)))
    email.add_argument("--no-body", action="store_true",
                       help="omit the message body page")
    email.add_argument("--max-attachment-mb", type=float, default=64.0,
                       help="skip attachments larger than this (default: %(default)s)")
    email.add_argument("--max-depth", type=int,
                       default=email_items.ExtractionPolicy.max_depth,
                       help="maximum nested-message depth (default: %(default)s)")

    layout = build.add_argument_group("layout")
    layout.add_argument("--separator", default="none",
                        choices=("none", "blank", "title"),
                        help="insert a page between source documents (default: %(default)s)")
    layout.add_argument("--page-numbers", default="none",
                        choices=("none", "continuous", "per-document"),
                        help="page numbering mode (default: %(default)s)")
    layout.add_argument("--number-format", default="{n}",
                        help="page number template; placeholders {n} {total} {doc} "
                             "{n_doc} {total_doc} {date} (default: %(default)s)")
    layout.add_argument("--number-position", default="bottom-center",
                        choices=tuple("%s-%s" % (edge, slot)
                                      for edge in ("top", "bottom")
                                      for slot in ("left", "center", "right")),
                        help="where the page number goes (default: %(default)s)")
    layout.add_argument("--number-start", type=int, default=1,
                        help="first page number (default: %(default)s)")
    for edge in ("header", "footer"):
        for slot in ("left", "center", "right"):
            layout.add_argument("--%s-%s" % (edge, slot), default="",
                                help="%s %s text (placeholders as for --number-format)"
                                     % (edge, slot))
    layout.add_argument("--title", default="", help="PDF title metadata")
    layout.add_argument("--author", default="", help="PDF author metadata")
    layout.add_argument("--subject", default="", help="PDF subject metadata")
    build.set_defaults(func=_cmd_build)

    check = sub.add_parser("check", help="report the detected conversion engine")
    check.set_defaults(func=_cmd_check)

    gui = sub.add_parser("gui", help="launch the graphical interface")
    gui.add_argument("paths", nargs="*", help="files or folders to preload")
    gui.set_defaults(func=_cmd_gui)

    selftest = sub.add_parser(
        "selftest", help="convert generated samples end to end and report"
    )
    selftest.set_defaults(func=_cmd_selftest)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
