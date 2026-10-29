#!/usr/bin/env python3
# Copyright(c) 2026 Lattice Semiconductor Corporation. All rights reserved. DO NOT DISTRIBUTE.
# Subject to the terms of the AI Solutions Software License Agreement
"""
Refresh scripts/python_notices/: the licence notices of the libraries inside the embedded CPython.

    python scripts/fetch_python_notices.py [--release 20261021]

The standalone program embeds a CPython from python-build-standalone (the build that uv fetches). That
interpreter has OpenSSL, zlib, expat and other libraries compiled in, and their licences ask for a notice to
accompany a binary. The interpreter that uv installs has none of those files; the project publishes them only in
its "full" archive of each release. This script downloads that archive for each platform, checks its SHA-256
against the release's SHA256SUMS, and copies the notices of the libraries the program can contain into
scripts/python_notices/<platform>/, with SOURCES.txt recording the release, archive and file hashes.
The folder is checked in, so a build needs no network for it; build_binary.py refuses to build when the
release of the interpreter in use (its BUILD file) is not the release recorded here.

When to run it: after changing UV_VERSION or PYTHON_VERSION in build_binary.py, or when `uv python` has
moved to a newer python-build-standalone release. Then read the diff: a library that was added or whose licence
changed needs a decision from Legal. Needs the `zstd` program for the archive; the rest is the standard library.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "scripts" / "python_notices"
PYTHON_VERSION = "3.11.17"
DEFAULT_RELEASE = "20261021"
BASE = "https://github.com/astral-sh/python-build-standalone/releases/download/%s/"

# Per platform: the archive flavour and the notices of the libraries that end up in the program, with the evidence.
# Linux: compiled into libpython3.11.so.1.0 (symbols and version strings found in it). Windows: separate DLLs and
# extension modules next to python311.dll (libcrypto, libffi, sqlite3, zlib1 and the .pyd files). Tcl/Tk, X11,
# Berkeley DB and zstd are left out: PyInstaller bundles only what the server imports, and none of them is imported.
PLATFORMS = {
    "linux-x86_64": {
        "archive": "cpython-%s+%s-x86_64-unknown-linux-gnu-pgo+lto-full.tar.zst",
        "notices": ["openssl-3", "zlib", "expat", "bzip2", "liblzma", "mpdecimal", "libedit", "ncurses", "libffi", "libuuid", "sqlite"],
    },
    "windows-x86_64": {
        "archive": "cpython-%s+%s-x86_64-pc-windows-msvc-pgo-full.tar.zst",
        "notices": ["openssl-3", "zlib", "expat", "bzip2", "liblzma", "mpdecimal", "libffi", "sqlite"],
    },
}


def fetch(url, target=None):
    request = urllib.request.Request(url, headers={"User-Agent": "lattice-diamond-mcp-notices"})
    with urllib.request.urlopen(request, timeout=120) as response:  # nosec B310 - a fixed https GitHub URL
        if target is None:
            return response.read()
        digest = hashlib.sha256()
        with open(target, "wb") as handle:
            for block in iter(lambda: response.read(1 << 20), b""):
                handle.write(block)
                digest.update(block)
        return digest.hexdigest()


CPYTHON_RAW = "https://raw.githubusercontent.com/python/cpython/v%s/%s"
LIBRARY_DIR_RE = re.compile(r"<(\w+)Dir Condition=[^>]*>\$\(ExternalsDir\)\\?([A-Za-z0-9_.+-]+)\\</", re.M)


def fetch_cpython_notices(version):
    """What python.org itself publishes for the Windows build of `version`, for the package that uses python.org's zip.

    Doc/license.rst: CPython's own "Licenses and Acknowledgements for Incorporated Software" (OpenSSL, expat, libffi,
    zlib, libmpdec and others). PCbuild/python.props: the versions of the libraries the Windows build links. Both are
    taken from the release tag in python/cpython and written to scripts/python_notices/cpython-<version>/.
    """
    folder = OUT / ("cpython-" + version)
    folder.mkdir(parents=True, exist_ok=True)
    licence = fetch(CPYTHON_RAW % (version, "Doc/license.rst"))
    props = fetch(CPYTHON_RAW % (version, "PCbuild/python.props"))
    libraries = sorted({"%s %s" % (name, release) for name, release in LIBRARY_DIR_RE.findall(props.decode("utf-8"))})
    if not licence.startswith(b".") and b"Licenses and Acknowledgements for Incorporated Software" not in licence:
        sys.exit("Doc/license.rst of v%s does not look like CPython's licence document." % version)
    if not libraries:
        sys.exit("No library versions were found in PCbuild/python.props of v%s; its layout has changed." % version)
    (folder / "license.rst").write_bytes(licence)
    (folder / "LIBRARIES.txt").write_text(
        "Libraries linked by the Windows build of CPython %s (from PCbuild/python.props at tag v%s):\n\n%s\n"
        % (version, version, "\n".join("  " + line for line in libraries)), encoding="utf-8")
    (folder / "SOURCES.txt").write_text(
        "license.rst   copied unchanged from %s\n  SHA-256 %s\n"
        "LIBRARIES.txt extracted from %s\n  SHA-256 of that file %s\n"
        % (CPYTHON_RAW % (version, "Doc/license.rst"), hashlib.sha256(licence).hexdigest(),
           CPYTHON_RAW % (version, "PCbuild/python.props"), hashlib.sha256(props).hexdigest()), encoding="utf-8")
    print("Wrote %s (%d libraries)." % (folder, len(libraries)))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Refresh the licence notices of the libraries inside the embedded CPython.")
    parser.add_argument("--release", default=DEFAULT_RELEASE, help="python-build-standalone release tag (default: %(default)s)")
    parser.add_argument("--workdir", default=None, help="where to keep the downloaded archives (default: a temporary folder)")
    parser.add_argument("--cpython-only", metavar="VERSION", nargs="?", const=PYTHON_VERSION,
                        help="only fetch python.org's own notices for the Windows build of this CPython version (no large downloads)")
    args = parser.parse_args(argv)
    if args.cpython_only:
        fetch_cpython_notices(args.cpython_only)
        return 0

    import tempfile
    work = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="pbs-notices-"))
    work.mkdir(parents=True, exist_ok=True)
    base = BASE % args.release
    sums = {}
    for line in fetch(base + "SHA256SUMS").decode("utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2:
            sums[parts[1].lstrip("*")] = parts[0]

    sources = ["Licence notices of the libraries inside the embedded CPython %s." % PYTHON_VERSION,
               "Written by scripts/fetch_python_notices.py; every file below is copied unchanged from the",
               "python-build-standalone release %s (https://github.com/astral-sh/python-build-standalone)." % args.release, ""]
    OUT.mkdir(parents=True, exist_ok=True)
    for platform, spec in PLATFORMS.items():
        name = spec["archive"] % (PYTHON_VERSION, args.release)
        expected = sums.get(name)
        if not expected:
            sys.exit("%s is not listed in the release's SHA256SUMS." % name)
        archive = work / name
        print("Downloading %s ..." % name, flush=True)
        got = fetch(base + name.replace("+", "%2B"), archive)
        if got != expected:
            sys.exit("SHA-256 of %s is %s, but SHA256SUMS says %s. Nothing was written." % (name, got, expected))
        wanted = {"python/licenses/LICENSE.%s.txt" % key: key for key in spec["notices"]}
        wanted["python/PYTHON.json"] = "PYTHON.json"
        folder = OUT / platform
        folder.mkdir(parents=True, exist_ok=True)
        for stale in folder.glob("LICENSE.*.txt"):
            stale.unlink()
        found = {}
        proc = subprocess.Popen(["zstd", "-dc", str(archive)], stdout=subprocess.PIPE)
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for member in tar:
                if member.name in wanted and member.isfile():
                    found[member.name] = tar.extractfile(member).read()
        proc.stdout.close()
        if proc.wait() != 0:
            sys.exit("zstd failed on %s" % name)
        missing = [n for n in wanted if n not in found]
        if missing:
            sys.exit("%s lacks: %s" % (name, ", ".join(missing)))
        info = json.loads(found.pop("python/PYTHON.json"))
        if info.get("python_version") != PYTHON_VERSION:
            sys.exit("%s is CPython %s, not %s." % (name, info.get("python_version"), PYTHON_VERSION))
        sources += ["%s: %s" % (platform, name), "  archive SHA-256 %s" % got]
        for path in sorted(found):
            data = found[path]
            (folder / path.rsplit("/", 1)[1]).write_bytes(data)
            sources.append("  %-28s SHA-256 %s" % (path.rsplit("/", 1)[1], hashlib.sha256(data).hexdigest()))
        sources.append("")
    (OUT / "RELEASE").write_text(args.release + "\n", encoding="utf-8")
    (OUT / "SOURCES.txt").write_text("\n".join(sources), encoding="utf-8")
    print("Wrote %s for release %s. Read the diff before committing." % (OUT, args.release))
    return 0


if __name__ == "__main__":
    sys.exit(main())
