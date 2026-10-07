"""Office COM conversion worker - one process, one file, then exit.

Invoked by :mod:`consolidator.office` as::

    python -m consolidator.office_worker --kind word --src <abs> --out <abs>

Why a separate process: Microsoft does not support unattended Office automation,
and the failure mode is a modal dialog that blocks forever. A separate process is
the only thing that can be reliably killed. This module therefore assumes it may
be terminated at any moment and does not try to be clever about it.

The teardown order at the end of each converter is deliberate and load-bearing.
Word's COM object graph is heavily cyclic; Python's reference counting never frees
it, and a leaked reference is the root cause of orphaned WINWORD.EXE processes.
``gc.collect()`` *before* ``Quit()`` is what makes the difference.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import traceback

# Word's ExportAsFixedFormat / SaveAs file-format constants.
WD_EXPORT_FORMAT_PDF = 17
# Excel's XlFixedFormatType.
XL_TYPE_PDF = 0
# PowerPoint's PpFixedFormatType.
PP_TYPE_PDF = 2
# msoAutomationSecurityForceDisable - refuse to run macros in the source document.
MSO_AUTOMATION_SECURITY_FORCE_DISABLE = 3


def emit(payload: dict) -> None:
    """Write the single JSON result line the parent parses."""
    sys.stdout.write("@@RESULT@@" + json.dumps(payload) + "\n")
    sys.stdout.flush()


def convert_word(src: str, out: str) -> None:
    import win32com.client as win32

    app = win32.DispatchEx("Word.Application")
    try:
        app.Visible = False
        app.DisplayAlerts = 0
        app.AutomationSecurity = MSO_AUTOMATION_SECURITY_FORCE_DISABLE

        doc = app.Documents.Open(
            FileName=src,
            ConfirmConversions=False,
            ReadOnly=True,
            AddToRecentFiles=False,
            Visible=False,
            NoEncodingDialog=True,
        )
        try:
            doc.ExportAsFixedFormat(out, WD_EXPORT_FORMAT_PDF, OpenAfterExport=False)
        finally:
            doc.Close(SaveChanges=0)
            del doc
            gc.collect()
    finally:
        app.Quit()
        del app
        gc.collect()


def convert_excel(src: str, out: str) -> None:
    import win32com.client as win32

    app = win32.DispatchEx("Excel.Application")
    try:
        app.Visible = False
        app.DisplayAlerts = False
        app.AutomationSecurity = MSO_AUTOMATION_SECURITY_FORCE_DISABLE

        wb = app.Workbooks.Open(FileName=src, ReadOnly=True, UpdateLinks=0, AddToRecentFiles=False)
        try:
            # Without explicit page setup, wide sheets fragment across many pages.
            for sheet in wb.Worksheets:
                try:
                    ps = sheet.PageSetup
                    ps.Zoom = False
                    ps.FitToPagesWide = 1
                    ps.FitToPagesTall = False
                    ps.Orientation = 1 if ps.Orientation != 2 else 2
                except Exception:
                    pass  # chartsheets and some sheet types have no PageSetup
            wb.ExportAsFixedFormat(XL_TYPE_PDF, out, OpenAfterPublish=False)
        finally:
            wb.Close(SaveChanges=False)
            del wb
            gc.collect()
    finally:
        app.Quit()
        del app
        gc.collect()


def convert_powerpoint(src: str, out: str) -> None:
    import win32com.client as win32

    app = win32.DispatchEx("PowerPoint.Application")
    try:
        # PowerPoint historically refuses to run headless; WithWindow=False is the
        # supported way to keep the window out of the user's face.
        pres = app.Presentations.Open(src, ReadOnly=True, Untitled=False, WithWindow=False)
        try:
            pres.ExportAsFixedFormat(out, PP_TYPE_PDF)
        finally:
            pres.Close()
            del pres
            gc.collect()
    finally:
        app.Quit()
        del app
        gc.collect()


CONVERTERS = {
    "word": convert_word,
    "excel": convert_excel,
    "powerpoint": convert_powerpoint,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Office COM conversion worker")
    parser.add_argument("--kind", required=True, choices=sorted(CONVERTERS))
    parser.add_argument("--src", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    src = os.path.abspath(args.src)
    out = os.path.abspath(args.out)

    if not os.path.exists(src):
        emit({"ok": False, "detail": "source not found: %s" % src})
        return 2

    try:
        import pythoncom

        pythoncom.CoInitialize()
    except Exception as exc:  # pragma: no cover - environment failure
        emit({"ok": False, "detail": "CoInitialize failed: %s" % exc})
        return 3

    try:
        CONVERTERS[args.kind](src, out)
    except Exception as exc:
        detail = "%s: %s" % (type(exc).__name__, exc)
        emit({"ok": False, "detail": detail, "traceback": traceback.format_exc()})
        return 1

    if not os.path.exists(out) or os.path.getsize(out) == 0:
        emit({"ok": False, "detail": "Office reported success but produced no output"})
        return 4

    emit({"ok": True, "out": out, "size": os.path.getsize(out)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
