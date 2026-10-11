"""Collect the third-party licence material that has to travel with the build.

PDF Consolidator is GPL-3.0-or-later, and the frozen bundle redistributes a lot
of other people's code: Qt under the LGPL, LibreOffice under the MPL, and a pile
of permissively licensed libraries. Those licences require their texts to be
conveyed with the program, so this script gathers them.

It reads the *installed* distributions rather than a hand-written list of texts,
because a hand-written list goes stale the moment a dependency is added. For
each distribution in ``RUNTIME`` it copies the licence files the wheel actually
ships into ``LICENSES/<name>/`` and writes ``THIRD-PARTY-NOTICES.md`` to index
them.

Usage::

    python tools/collect_licenses.py
    python tools/collect_licenses.py --verify dist/PDFConsolidator/PDFConsolidator.exe

``--verify`` reads the module list out of a built executable and fails if the
bundle contains a distribution this script does not know about - which is how a
new dependency gets caught before it ships unlicensed.
"""

from __future__ import annotations

import argparse
import datetime
import importlib.metadata as metadata
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LICENCES_DIR = ROOT / "LICENSES"
NOTICES_PATH = ROOT / "THIRD-PARTY-NOTICES.md"

# Where the Corresponding Source for *this* application is published. GPL-3.0
# section 6 lets you satisfy the source requirement by offering it from a
# network server, but the offer has to say where. Set this before you hand the
# bundle to anybody; leaving it empty makes the notices fall back to wording
# that assumes the source travelled with the build.
SOURCE_URL = "https://github.com/Xiaoyu-Ben-Wang/PDFConsolidator"

# What actually ends up in the bundle, and what it does for us. Verified against
# a real build by ``--verify``; a module in the bundle that is missing from this
# list is reported as an error rather than silently going unlicensed.
RUNTIME: list[tuple[str, str]] = [
    ("pypdf", "reading, merging and rewriting the PDFs"),
    ("reportlab", "page numbers, headers, footers and title pages"),
    ("img2pdf", "wrapping images into PDF"),
    ("pikepdf", "the PDF library behind img2pdf"),
    ("pillow", "decoding images, EXIF rotation and transparencies"),
    ("pypdfium2", "rendering page thumbnails for the interface"),
    ("PySide6-Essentials", "the Qt user interface"),
    ("shiboken6", "the binding layer PySide6 is built on"),
    ("extract-msg", "reading Outlook .msg items"),
    ("olefile", "OLE compound-file access, used by extract-msg"),
    ("beautifulsoup4", "the HTML body of .msg items"),
    ("soupsieve", "CSS selectors for Beautiful Soup"),
    ("RTFDE", "text from RTF-compressed message bodies"),
    ("compressed-rtf", "decompressing Compressed RTF streams"),
    ("ebcdic", "EBCDIC-coded message fields"),
    ("lark", "the grammar parser used by RTFDE"),
    ("oletools", "code-page tables used by RTFDE"),
    ("tzlocal", "time-zone names for message dates"),
    ("tzdata", "the time-zone database on Windows"),
    ("lxml", "XML handling used by pikepdf"),
    ("charset-normalizer", "character-set detection used by reportlab"),
    ("cryptography", "cryptographic primitives reached through oletools"),
    ("packaging", "version handling used by pikepdf"),
    ("cffi", "C bindings used by cryptography"),
    ("typing_extensions", "typing backports used by the Qt bindings"),
    ("pywin32", "collected by the PyInstaller hook; not used by the application"),
]

# Vendored by tools/vendor.py as unpacked source, so there is no installed
# metadata to read: the licence lives in a comment at the top of the module.
MANUAL: list[tuple[str, str, str, str]] = [
    (
        "red-black-tree-mod",
        "MIT",
        "ordered mapping used by extract-msg",
        "red_black_dict_mod.py",
    ),
]

# Licence texts several components share, published once at LICENSES/<file>
# instead of only under each package. The marker is looked for near the start of
# a collected file, which is how the text is found without hard-coding a path.
SHARED_TEXTS: list[tuple[str, str]] = [
    ("LGPL-3.0.txt", "GNU LESSER GENERAL PUBLIC LICENSE"),
]

# Components that ship inside the bundle but are not Python distributions, and
# so are described in prose rather than discovered from metadata.
LIBREOFFICE_VERSION_INI = Path("libreoffice/program/version.ini")

MSVC_NOTE = """\
`VCRUNTIME140.dll`, `VCRUNTIME140_1.dll`, `MSVCP140.dll` and `MSVCP140_1.dll` in
`_internal/` are the Microsoft Visual C++ runtime redistributables, shipped under
the Microsoft Software License Terms for the Visual C++ Redistributable, which
permit redistribution with an application built using Visual Studio."""

# The one place where the licences impose a duty beyond shipping a text file, so
# it is spelled out rather than left to the licence reading.
QT_NOTE = """\
**Qt** (`PySide6-Essentials`, `shiboken6`) is used under the **LGPL-3.0-only**
option. The wheels ship only a commercial licence reference, so the LGPL text is
published here as `LICENSES/LGPL-3.0.txt`.

The Qt libraries are ordinary DLLs under `_internal/PySide6/`, loaded at run time
rather than linked in statically. You may replace any of them with your own build
of the same Qt version; the program needs no rebuild or relink for that."""

# LibreOffice's own notices are the authoritative ones for that tree; this is
# only a pointer to where they are.
LIBREOFFICE_NOTE = """\
LibreOffice's own notices are the authoritative ones for that tree:
`libreoffice/license.txt`, `LICENSE.html`, `NOTICE` and `CREDITS.fodt`. They cover
the bundled Python interpreter, poppler (in `xpdfimport`), the fonts and the
UNO/Java bridge."""

# Not third-party code, so not reported by --verify.
VERIFY_IGNORE = {"consolidator", "pyinstaller", "pyinstaller-hooks-contrib"}

BINARY_SUFFIXES = {".py", ".pyc", ".pyd", ".dll", ".exe", ".so", ".json"}


def canonical(name: str) -> str:
    """PEP 503 name normalisation, so 'extract_msg' and 'extract-msg' match."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def find_distribution(name: str):
    wanted = canonical(name)
    for dist in metadata.distributions():
        installed = dist.metadata["Name"]
        if installed and canonical(installed) == wanted:
            return dist
    return None


def licence_id(dist) -> str:
    """The licence as the package declares it, preferring SPDX expressions."""
    expression = dist.metadata.get("License-Expression")
    if expression:
        return expression.strip()
    declared = (dist.metadata.get("License") or "").strip()
    if declared and declared.lower() not in {"unknown", "none"} and len(declared) < 120:
        return declared
    for classifier in dist.metadata.get_all("Classifier") or []:
        if classifier.startswith("License ::"):
            return classifier.split("::")[-1].strip()
    return "see the licence text"


def project_url(dist) -> str:
    for key in ("Home-page",):
        value = dist.metadata.get(key)
        if value and value.strip():
            return value.strip()
    for entry in dist.metadata.get_all("Project-URL") or []:
        if "," in entry:
            return entry.split(",", 1)[1].strip()
        return entry.strip()
    return ""


def licence_files(dist) -> list[tuple[Path, Path]]:
    """(path inside the distribution, real path on disk) for its licence files."""
    found: list[tuple[Path, Path]] = []
    for relative in dist.files or ():
        path = Path(str(relative))
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        name = path.name.upper()
        parent = "/".join(path.parts[:-1]).lower()
        if (
            name.startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE", "AUTHORS"))
            or "license" in parent
        ):
            try:
                real = Path(dist.locate_file(relative))
            except Exception:
                continue
            if real.is_file():
                found.append((path, real))
    # A wheel can list the same file more than once; keep the order stable.
    unique: dict[str, tuple[Path, Path]] = {}
    for relative, real in found:
        unique.setdefault(str(relative), (relative, real))
    return sorted(unique.values(), key=lambda item: str(item[0]))


def split_off_dist_info(path: Path) -> Path:
    """Drop the '<name>-<version>.dist-info/' prefix, keeping subdirectories."""
    for index, part in enumerate(path.parts):
        if part.endswith((".dist-info", ".egg-info")):
            return Path(*path.parts[index + 1:])
    return Path(path.name)


def copy_licence_files(dist, name: str) -> list[Path]:
    """Copy one distribution's licence texts; return the paths written."""
    written: list[Path] = []
    # Windows folds case, so 'LICENSE.txt' and 'License.txt' are the same file.
    # Without this, a package that ships both would overwrite one with the other
    # and be listed twice.
    seen: set[str] = set()
    for relative, real in licence_files(dist):
        tail = split_off_dist_info(relative)
        if not tail.parts:
            tail = Path(relative.name)
        destination = LICENCES_DIR / name / tail
        key = str(destination).lower()
        if key in seen:
            continue
        seen.add(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(real, destination)
        written.append(destination.relative_to(ROOT))
    return written


def promote_shared_texts() -> list[str]:
    """Publish one canonical copy of each licence text shared between packages.

    PySide6 and shiboken6 are the reason this exists: their wheels carry only the
    *commercial* licence reference and not the LGPL text, so it has to be taken
    from a package that does ship it.
    """
    written: list[str] = []
    candidates = sorted(path for path in LICENCES_DIR.rglob("*") if path.is_file())
    for filename, marker in SHARED_TEXTS:
        destination = LICENCES_DIR / filename
        if destination.exists():
            written.append(str(destination.relative_to(ROOT)).replace("\\", "/"))
            continue
        for candidate in candidates:
            try:
                head = candidate.read_text(encoding="utf-8", errors="replace")[:400]
            except OSError:
                continue
            if marker in head:
                shutil.copyfile(candidate, destination)
                written.append(str(destination.relative_to(ROOT)).replace("\\", "/"))
                break
        else:
            print("WARNING: nothing under LICENSES/ contains %r, so %s was not "
                  "written" % (marker, filename))
    return written


def extract_comment_licence(source: Path) -> str:
    """Pull the licence out of a '##'-commented header block."""
    lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    block: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("##"):
            block.append(stripped[2:].rstrip())
        elif block:
            break
    return "\n".join(block).strip() + "\n"


def write_manual_licence(name: str, source_name: str) -> list[Path]:
    import importlib.util

    spec = importlib.util.find_spec(source_name.removesuffix(".py"))
    if spec is None or not spec.origin:
        print("WARNING: %s is not importable; no licence text for %s"
              % (source_name, name))
        return []
    source = Path(spec.origin)
    destination = LICENCES_DIR / name / f"{source.stem}.txt"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(extract_comment_licence(source), encoding="utf-8")
    return [destination.relative_to(ROOT)]


def libreoffice_version(bundle: Path | None) -> str:
    """The LibreOffice build bundled beside the executable, if there is one."""
    candidates = []
    if bundle is not None:
        candidates.append(bundle / LIBREOFFICE_VERSION_INI)
    candidates.append(ROOT / "dist" / "PDFConsolidator" / LIBREOFFICE_VERSION_INI)
    candidates.append(Path(r"C:\Program Files\LibreOffice\program\version.ini"))
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        buildid = re.search(r"^buildid=(\S+)", text, re.MULTILINE)
        product = re.search(r"^UpdateID=LibreOffice_(\S+?)_", text, re.MULTILINE)
        version = product.group(1) if product else "26"
        if buildid:
            return "%s (build %s)" % (version, buildid.group(1)[:12])
        return version
    return "unknown version"


def table_row(component: str, version: str, licence: str, role: str, url: str = "") -> str:
    name = "[%s](%s)" % (component, url) if url else component
    return "| %s | %s | %s | %s |" % (name, version, licence, role)


def render_notices(bundle: Path | None, runtime: list[dict], manual: list[dict],
                   development: list[dict], shared: list[str]) -> str:
    today = datetime.date.today().isoformat()
    lines: list[str] = []
    add = lines.append

    add("# Third-party notices")
    add("")
    add(
        "PDF Consolidator is free software under the GNU General Public License, "
        "version 3 or later (see `LICENSE`). It is built with, and redistributes, "
        "the components below; each stays under its own licence."
    )
    add("")
    add("Generated by `tools/collect_licenses.py` on %s." % today)
    add("")

    add("## Redistributed with the application")
    add("")
    add("| Component | Version | Licence | Used for |")
    add("|---|---|---|---|")
    for entry in runtime:
        add(table_row(entry["name"], entry["version"], entry["licence"],
                      entry["role"], entry["url"]))
    for entry in manual:
        add(table_row(entry["name"], entry["version"], entry["licence"],
                      entry["role"], entry["url"]))
    add("")
    add(
        "Each component's licence files are copied verbatim into "
        "`LICENSES/<component>/`, under the names the package itself ships."
    )
    if shared:
        add("")
        add("Texts shared by several components are also published together: "
            + ", ".join("`%s`" % text for text in shared) + ".")
    add("")

    add("## What the licences require of this build")
    add("")
    add(QT_NOTE)
    add("")

    add("## LibreOffice")
    add("")
    add(
        "`libreoffice/` is LibreOffice %s from The Document Foundation, "
        "redistributed unmodified except that documentation, dictionaries, "
        "clipart, templates and translation data are removed (see "
        "`tools/build.py`). It is under the Mozilla Public License v2.0, with "
        "portions under other licences." % libreoffice_version(bundle)
    )
    add("")
    add(LIBREOFFICE_NOTE)
    add("")
    add("Source: <https://www.libreoffice.org/about-us/source-code/>")
    add("")

    add("## Microsoft Visual C++ runtime")
    add("")
    add(MSVC_NOTE)
    add("")

    add("## Installed for development and the build (not distributed)")
    add("")
    add(
        "Used to develop, test and freeze the application. None of it is in the "
        "bundle; it is listed so a licence review of the development environment "
        "does not have to guess."
    )
    add("")
    add(", ".join(
        "[%s](%s) %s (%s)" % (entry["name"], entry["url"], entry["version"],
                              entry["licence"])
        if entry["url"] else
        "%s %s (%s)" % (entry["name"], entry["version"], entry["licence"])
        for entry in development
    ))
    add("")

    add("## Where the source is")
    add("")
    add("Everything above is redistributed unmodified. Corresponding source:")
    add("")
    add(
        "- **LibreOffice** - published by The Document Foundation at "
        "<https://www.libreoffice.org/about-us/source-code/>; the build id in "
        "`libreoffice/program/version.ini` identifies the exact build."
    )
    add(
        "- **Python packages** - each component's own page is linked above."
    )
    if SOURCE_URL:
        add(
            "- **PDF Consolidator** - the complete source, including the build "
            "scripts, is published at %s; this executable was produced from it."
            % SOURCE_URL
        )
    else:
        add(
            "- **PDF Consolidator** - the complete source is the tree this bundle "
            "was built from, including `tools/build.py` and "
            "`tools/collect_licenses.py`. If you were given the executable on its "
            "own, the GPL entitles you to ask for that source. (Set `SOURCE_URL` in "
            "`tools/collect_licenses.py` to publish a location here instead.)"
        )
    add("")
    add(
        "Distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; "
        "without even the implied warranty of MERCHANTABILITY or FITNESS FOR A "
        "PARTICULAR PURPOSE. See `LICENSE`."
    )
    add("")
    return "\n".join(lines)


def collect(bundle: Path | None) -> tuple[list[dict], list[dict], list[dict]]:
    if LICENCES_DIR.exists():
        shutil.rmtree(LICENCES_DIR)
    LICENCES_DIR.mkdir(parents=True, exist_ok=True)

    runtime: list[dict] = []
    missing: list[str] = []
    for name, role in RUNTIME:
        dist = find_distribution(name)
        if dist is None:
            missing.append(name)
            continue
        texts = copy_licence_files(dist, name)
        runtime.append({
            "name": name,
            "version": dist.version,
            "licence": licence_id(dist),
            "role": role,
            "url": project_url(dist),
            "texts": [str(t).replace("\\", "/") for t in texts],
        })

    manual: list[dict] = []
    for name, licence, role, source in MANUAL:
        texts = write_manual_licence(name, source)
        manual.append({
            "name": name,
            "version": "vendored source",
            "licence": licence,
            "role": role,
            "url": "https://pypi.org/project/red-black-tree-mod/",
            "texts": [str(t).replace("\\", "/") for t in texts],
        })

    known = {canonical(name) for name, _ in RUNTIME}
    known |= {canonical(entry[0]) for entry in MANUAL}
    development: list[dict] = []
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if not name or canonical(name) in known or canonical(name) == "consolidator":
            continue
        development.append({
            "name": name,
            "version": dist.version,
            "licence": licence_id(dist),
            "url": project_url(dist),
        })
    development.sort(key=lambda entry: entry["name"].lower())

    if missing:
        print("WARNING: not installed, so no licence text was collected: %s"
              % ", ".join(missing))

    shared = promote_shared_texts()

    NOTICES_PATH.write_text(
        render_notices(bundle, runtime, manual, development, shared),
        encoding="utf-8")
    return runtime, manual, development


def archive_modules(exe: Path) -> set[str]:
    """Top-level module names inside a frozen executable's archive."""
    viewer = Path(sys.executable).parent / "pyi-archive_viewer.exe"
    command = [str(viewer) if viewer.exists() else "pyi-archive_viewer",
               "-r", "-b", str(exe)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit("could not read %s: %s" % (exe, result.stderr.strip()))
    modules: set[str] = set()
    for line in result.stdout.splitlines():
        name = line.strip()
        if not re.fullmatch(r"[A-Za-z_][\w.]*", name):
            continue
        modules.add(name.split(".")[0])
    return modules


def verify(exe: Path) -> int:
    """Fail if the bundle contains a distribution the notices do not cover."""
    modules = archive_modules(exe)
    by_module = metadata.packages_distributions()
    known = {canonical(name) for name, _ in RUNTIME}
    known |= {canonical(entry[0]) for entry in MANUAL}
    known |= {canonical(name) for name in VERIFY_IGNORE}

    unknown: set[str] = set()
    for module in sorted(modules):
        for dist in by_module.get(module, []):
            if canonical(dist) not in known:
                unknown.add("%s (from module %s)" % (dist, module))

    if unknown:
        print("FAIL: the bundle contains components with no notices:")
        for item in sorted(unknown):
            print("  %s" % item)
        print("Add them to RUNTIME in tools/collect_licenses.py and re-run.")
        return 1

    print("OK: every distribution in %s is covered by the notices." % exe.name)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", type=Path, metavar="EXE",
                        help="check a built executable against the notices")
    parser.add_argument("--bundle", type=Path, metavar="DIR",
                        help="bundle directory, used to read LibreOffice's version")
    args = parser.parse_args(argv)

    if args.verify:
        return verify(args.verify)

    runtime, manual, development = collect(args.bundle)
    print("wrote %s" % NOTICES_PATH.relative_to(ROOT))
    print("  %d redistributed component(s), %d vendored, %d development-only"
          % (len(runtime), len(manual), len(development)))
    print("  licence texts in %s" % LICENCES_DIR.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
