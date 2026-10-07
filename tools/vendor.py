"""Install packages without relying on pip's build machinery.

Some dependencies this project needs publish source-only distributions whose
``setup.py`` cannot be built in this environment - notably
``red-black-tree-mod``, a hard import-time dependency of ``extract-msg``. pip
fails outright on them.

For a *pure-Python* sdist no build step is actually required: unpacking the
archive and copying its importable packages into site-packages is sufficient.
This script does exactly that, and handles wheels the same way.

Usage::

    python tools/vendor.py <target-dir> <cache-dir> <package> [package ...]
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tarfile
import urllib.request
import zipfile

# Best-first for a standard (non free-threaded) CPython 3.14 on win_amd64.
# The cp314-cp314t tag is the FREE-THREADED ABI and must not be selected.
WHEEL_PREFERENCE = (
    "cp314-cp314-win_amd64",
    "cp314-abi3-win_amd64",
    "cp313-abi3-win_amd64",
    "cp312-abi3-win_amd64",
    "cp311-abi3-win_amd64",
    "cp310-abi3-win_amd64",
    "py3-none-win_amd64",
    "py3-none-any",
    "py2.py3-none-any",
)

SDIST_SUFFIXES = (".tar.gz", ".zip")


def pypi_json(package: str) -> dict:
    url = "https://pypi.org/pypi/%s/json" % package
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def choose_distribution(package: str):
    """Return (version, url, filename, requires, is_sdist) for a package."""
    data = pypi_json(package)
    version = data["info"]["version"]
    files = data["releases"].get(version, [])
    requires = data["info"].get("requires_dist") or []

    wheels = [f for f in files if f["filename"].endswith(".whl")]
    for preference in WHEEL_PREFERENCE:
        for f in wheels:
            if preference in f["filename"]:
                return version, f["url"], f["filename"], requires, False

    sdists = [f for f in files if f["filename"].endswith(SDIST_SUFFIXES)]
    if sdists:
        f = sdists[0]
        return version, f["url"], f["filename"], requires, True

    return version, None, None, requires, False


def install_sdist(archive: str, target: str) -> list[str]:
    """Unpack a pure-Python sdist, copying its importable packages into target."""
    extract_to = archive + ".unpacked"
    os.makedirs(extract_to, exist_ok=True)

    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(extract_to)
    else:
        with tarfile.open(archive) as tf:
            tf.extractall(extract_to, filter="data")

    copied: list[str] = []
    for root, dirs, files in os.walk(extract_to):
        for name in list(dirs):
            candidate = os.path.join(root, name)
            if os.path.exists(os.path.join(candidate, "__init__.py")):
                shutil.copytree(candidate, os.path.join(target, name), dirs_exist_ok=True)
                copied.append(name)
                dirs.remove(name)  # do not descend into a package we just copied
        for name in files:
            # Single-module distributions ship a bare .py file.
            if name.endswith(".py") and name != "setup.py":
                shutil.copy2(os.path.join(root, name), os.path.join(target, name))
                copied.append(name)

    shutil.rmtree(extract_to, ignore_errors=True)
    return copied


def requirement_name(spec: str) -> str | None:
    """'packaging>=22.0; extra == "x"' -> 'packaging', or None if extra-gated."""
    if ";" in spec:
        marker = spec.split(";", 1)[1]
        if "extra" in marker:
            return None
    base = spec.split(";", 1)[0].strip()
    for separator in ("[", "=", "<", ">", "!", "~", " "):
        base = base.split(separator)[0]
    return base.strip() or None


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print(__doc__)
        return 2

    target, cache = argv[1], argv[2]
    roots = argv[3:]
    os.makedirs(target, exist_ok=True)
    os.makedirs(cache, exist_ok=True)

    seen: set[str] = set()
    queue = list(roots)
    installed: list[str] = []
    failed: list[str] = []
    from_sdist: list[str] = []

    while queue:
        package = queue.pop(0)
        key = package.lower().replace("_", "-")
        if key in seen:
            continue
        seen.add(key)

        try:
            version, url, filename, requires, is_sdist = choose_distribution(package)
            if not url:
                failed.append("%s (no wheel or sdist for %s)" % (package, version))
                continue

            archive = os.path.join(cache, filename)
            if not os.path.exists(archive):
                urllib.request.urlretrieve(url, archive)

            if is_sdist:
                copied = install_sdist(archive, target)
                from_sdist.append("%s==%s -> %s" % (package, version, ", ".join(copied) or "nothing"))
                print("  sdist %-26s %s" % (package, ", ".join(copied) or "?"))
            else:
                with zipfile.ZipFile(archive) as zf:
                    zf.extractall(target)
                print("  wheel %-26s %s" % (package, filename))

            installed.append("%s==%s" % (package, version))
            for requirement in requires:
                name = requirement_name(requirement)
                if name:
                    queue.append(name)

        except Exception as exc:
            failed.append("%s (%s: %s)" % (package, type(exc).__name__, exc))

    print("\ninstalled %d package(s) into %s" % (len(installed), target))
    if from_sdist:
        print("unpacked from source: " + "; ".join(from_sdist))
    if failed:
        print("FAILED: " + "; ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
