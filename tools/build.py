"""Build the standalone distribution.

Steps:

1. Freeze the application with PyInstaller (``--onedir``, never ``--onefile``:
   onefile re-extracts ~250 MB of Qt to a temp directory on every launch).
2. Copy a trimmed LibreOffice beside it, so the result converts Office documents
   on a machine that has none installed.
3. Report the size and what was trimmed.

Trimming removes only material that a conversion engine never touches: help
files, the spell-check dictionaries, clipart, document templates and the
translation XSLTs. Everything needed to render a document is kept.

Usage::

    python tools/build.py [--out dist] [--skip-libreoffice] [--no-trim]
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LO = Path(r"C:\Program Files\LibreOffice")

# Directories inside the LibreOffice tree that conversion never needs.
TRIM_PATHS = [
    "help",
    "readmes",
    "share/extensions",   # spell-check and hyphenation dictionaries
    "share/gallery",      # clipart
    "share/template",     # document templates for new files
    "share/wizards",
    "share/xslt",
    "share/autotext",
    "share/autocorr",
    # Only used to benchmark OpenCL devices - and it caches a read-only lock
    # file inside the bundle, which then blocks the next build's cleanup.
    "program/opencl",
]


def force_remove(path: Path) -> None:
    """Remove a tree, clearing read-only attributes first.

    LibreOffice marks its ``.~lock.*#`` files read-only, and Windows refuses to
    delete a read-only file, so a plain ``rmtree`` fails with ``WinError 5``.
    Running the bundled LibreOffice leaves exactly such a file behind, which is
    what breaks a subsequent rebuild.
    """
    if not path.exists():
        return
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                os.chmod(os.path.join(root, name), 0o666)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        time.sleep(1.0)          # a handle may still be closing
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    os.chmod(os.path.join(root, name), 0o666)
                except OSError:
                    pass
        shutil.rmtree(path, ignore_errors=True)


def directory_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def human(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return "%.1f %s" % (value, unit)
        value /= 1024.0
    return "%.1f GB" % value


def run_pyinstaller(out_dir: Path) -> Path:
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onedir", "--windowed",
        "--name", "PDFConsolidator",
        "--distpath", str(out_dir),
        "--workpath", str(ROOT / "build" / "_work"),
        "--specpath", str(ROOT / "build"),
        "--paths", str(ROOT / "src"),
        # Not used, and each pulls in a large Qt module.
        "--exclude-module", "PySide6.QtWebEngineCore",
        "--exclude-module", "PySide6.QtWebEngineWidgets",
        "--exclude-module", "PySide6.QtQml",
        "--exclude-module", "PySide6.QtQuick",
        "--exclude-module", "PySide6.Qt3DCore",
        "--exclude-module", "PySide6.QtCharts",
        "--exclude-module", "PySide6.QtDataVisualization",
        "--exclude-module", "PySide6.QtMultimedia",
        "--exclude-module", "tkinter",
        "--exclude-module", "matplotlib",
        str(ROOT / "build" / "consolidator_gui.py"),
    ]
    print("running PyInstaller ...", flush=True)
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        raise SystemExit("PyInstaller failed with code %d" % result.returncode)
    bundle = out_dir / "PDFConsolidator"
    if not bundle.exists():
        raise SystemExit("PyInstaller produced no bundle at %s" % bundle)
    return bundle


def copy_libreoffice(bundle: Path, source: Path, trim: bool) -> int:
    if not source.exists():
        print("WARNING: LibreOffice not found at %s - the bundle will need it "
              "installed on the target machine" % source)
        return 0

    destination = bundle / "libreoffice"
    if destination.exists():
        print("removing previous LibreOffice copy ...", flush=True)
        force_remove(destination)

    print("copying LibreOffice from %s ..." % source, flush=True)
    shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=False)

    freed = 0
    if trim:
        for relative in TRIM_PATHS:
            target = destination / relative
            if target.exists():
                before = directory_size(target)
                shutil.rmtree(target, ignore_errors=True)
                freed += before
                print("  trimmed %-22s %s" % (relative, human(before)))

    soffice = destination / "program" / "soffice.exe"
    if not soffice.exists():
        raise SystemExit("LibreOffice copy is missing program/soffice.exe")
    print("  LibreOffice bundled: %s" % human(directory_size(destination)))
    if freed:
        print("  trimmed away      : %s" % human(freed))
    return freed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "dist"), help="output directory")
    parser.add_argument("--libreoffice", default=str(DEFAULT_LO),
                        help="LibreOffice installation to bundle")
    parser.add_argument("--skip-libreoffice", action="store_true")
    parser.add_argument("--no-trim", action="store_true")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    started = time.time()

    # Clear any previous bundle before PyInstaller tries to clean it, since it
    # does not cope with the read-only lock files LibreOffice leaves behind.
    force_remove(out_dir / "PDFConsolidator")

    bundle = run_pyinstaller(out_dir)
    print("application bundle: %s" % human(directory_size(bundle)))

    if not args.skip_libreoffice:
        copy_libreoffice(bundle, Path(args.libreoffice), trim=not args.no_trim)

    total = directory_size(bundle)
    print()
    print("=" * 68)
    print("Bundle : %s" % bundle)
    print("Size   : %s" % human(total))
    print("Exe    : %s" % (bundle / "PDFConsolidator.exe"))
    print("Time   : %.1fs" % (time.time() - started))
    print("=" * 68)
    print()
    print("Copy that whole folder to the target machine and run")
    print("PDFConsolidator.exe. No installer, no Python, no Office needed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
