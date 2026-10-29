#!/usr/bin/env python3
# Copyright(c) 2026 Lattice Semiconductor Corporation. All rights reserved. DO NOT DISTRIBUTE.
# Subject to the terms of the AI Solutions Software License Agreement
"""
Build the Windows package that contains no executable of ours.

    python scripts/build_embedded.py [--outdir dist] [--embed-zip FILE] [--skip-verify]

The standalone package is a PyInstaller program: `diamond-mcp.exe` is a launcher nobody has signed, which
Windows Defender and SmartScreen distrust. This package has no such file. It is python.org's own
"embeddable" Python for Windows, which the Python Software Foundation has signed, with the locked packages
unpacked beside it; the server is `python\\python.exe -I -m diamond_mcp`.

    lattice-diamond-mcp-<version>/
        install.py  BUILDINFO  LICENSE  LEGAL_REVIEW.txt  THIRD_PARTY_LICENSES.txt  THIRD_PARTY_LICENSES/
        python/     python.exe, python313.dll, the standard-library zip and extension modules (as published),
                    python313._pth, Lib/site-packages/ (the packages from uv.lock and this project)

What is checked: the embeddable zip against the SHA-256 pinned below; that python.exe carries a signature
from the Python Software Foundation (the name is looked for in its signature block; the signature is NOT
validated here, Windows does that); every package against the SHA-256 in uv.lock; and that the folder holds
no program but python.exe. On Windows the finished package is also installed with its own install.py and
the server is started. On other systems only the layout can be checked, so build releases on Windows.

The build runs on Windows or Linux (it downloads and unpacks files, and runs nothing from the Python it
packs). Needs Python 3.11 or later with `build` and `pip`, and network access (python.org and the package
index). pip honours PIP_INDEX_URL and proxy settings.

python.org publishes an embeddable zip only for releases it builds binaries for, which stopped for
3.11 and 3.12 at 3.11.9 and 3.12.10. EMBED_VERSION must be a release that still has one.
"""

import argparse
import hashlib
import json
import os
import shutil
import struct
import sys
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_installer import (BuildError, check_wheels_against_lock, extract_licences, git_commit, load_lock,  # noqa: E402
                             lock_constraints_text, log, project_version, requirements_text, run, sha256_file)

REPO = Path(__file__).resolve().parents[1]
PACKAGE = "lattice-diamond-mcp"
EMBED_VERSION = "3.13.17"
EMBED_SHA256 = "9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38"          # python-3.13.17-embed-amd64.zip, as downloaded over https from python.org
EMBED_URL = "https://www.python.org/ftp/python/%s/python-%s-embed-amd64.zip" % (EMBED_VERSION, EMBED_VERSION)
PYTHON_TAG = "cp%s" % "".join(EMBED_VERSION.split(".")[:2])
PLATFORM = "windows-x86_64"
SIGNER = b"Python Software Foundation"
NOTICES_DIR = REPO / "scripts" / "python_notices"
ONLY_PROGRAMS = {"python.exe"}                    # the only .exe allowed in the package (pythonw.exe is removed)


# ------------------------------------------------------------------ the interpreter

def download(url, target):
    request = urllib.request.Request(url, headers={"User-Agent": "lattice-diamond-mcp-build"})
    with urllib.request.urlopen(request, timeout=120) as response, open(target, "wb") as handle:  # nosec B310 - a fixed https URL
        shutil.copyfileobj(response, handle)


def signature_block(path):
    """The Authenticode signature bytes of a PE file, or b"" when it has none."""
    data = Path(path).read_bytes()
    if data[:2] != b"MZ":
        return b""
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    magic = struct.unpack_from("<H", data, pe + 24)[0]
    directories = pe + 24 + (112 if magic == 0x20B else 96)
    offset, size = struct.unpack_from("<II", data, directories + 4 * 8)   # entry 4: the certificate table (a file offset)
    return data[offset:offset + size] if offset and size and offset + size <= len(data) else b""


def fetch_embeddable(workdir, given=None):
    """Return the path of the verified embeddable zip."""
    if EMBED_SHA256.startswith("EMBED-"):
        raise BuildError("EMBED_SHA256 is not set in build_embedded.py.")
    target = Path(given) if given else Path(workdir) / ("python-%s-embed-amd64.zip" % EMBED_VERSION)
    if not given:
        log("Downloading %s ..." % EMBED_URL)
        download(EMBED_URL, target)
    digest = sha256_file(target)
    if digest != EMBED_SHA256:
        raise BuildError("%s has SHA-256 %s, expected %s. python.org may have replaced the file, or the download was altered; "
                         "nothing was built." % (target.name, digest, EMBED_SHA256))
    return target


def unpack_interpreter(embed_zip, dest):
    """Unpack the embeddable zip into `dest`, check what it holds, and drop pythonw.exe."""
    with zipfile.ZipFile(embed_zip) as archive:
        for name in archive.namelist():
            if name.startswith("/") or ".." in PurePosixPath(name).parts:
                raise BuildError("The embeddable zip holds an unsafe path: %s" % name)
        archive.extractall(dest)
    exes = sorted(p.name for p in dest.rglob("*.exe"))
    if "python.exe" not in exes:
        raise BuildError("The embeddable zip has no python.exe.")
    if not (dest / ("python%s.dll" % PYTHON_TAG[2:])).is_file() or not list(dest.glob("python*._pth")):
        raise BuildError("The embeddable zip lacks the interpreter library or its ._pth file; its layout has changed.")
    for name in exes:
        if name not in ONLY_PROGRAMS:
            (dest / name).unlink()
    signature = signature_block(dest / "python.exe")
    if SIGNER not in signature:
        raise BuildError("python.exe carries no signature naming the %s. The point of this package is a launcher that is "
                         "already signed; do not ship it." % SIGNER.decode())
    return dest / "LICENSE.txt"


# --------------------------------------------------------------------- the packages

def build_own_wheel(wheels):
    log("Building the %s wheel ..." % PACKAGE)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            run([sys.executable, "-m", "build", "--wheel", "--outdir", tmp, str(REPO)])
        except BuildError as exc:
            if "No module named build" in str(exc):
                raise BuildError("The 'build' package is missing. Install it with: pip install build")
            raise
        built = list(Path(tmp).glob("*.whl"))
        if len(built) != 1:
            raise BuildError("Expected one wheel, found %d." % len(built))
        shutil.copy(built[0], wheels / built[0].name)
    for leftover in (REPO / "build", *REPO.glob("src/*.egg-info")):
        shutil.rmtree(leftover, ignore_errors=True)
    return next(wheels.glob("lattice_diamond_mcp-*.whl"))


def download_wheels(wheels, own):
    locked = load_lock(REPO / "uv.lock")
    log("Downloading the Windows wheels for Python %s at the versions in uv.lock ..." % ".".join(EMBED_VERSION.split(".")[:2]))
    with tempfile.TemporaryDirectory() as tmp:
        constraints = Path(tmp) / "locked.txt"
        constraints.write_text(lock_constraints_text(locked), encoding="utf-8")
        run([sys.executable, "-m", "pip", "download", "--only-binary=:all:", "--platform", "win_amd64",
             "--python-version", ".".join(EMBED_VERSION.split(".")[:2]), "--implementation", "cp",
             "--abi", PYTHON_TAG, "--abi", "abi3", "--abi", "none", "--dest", str(wheels),
             "--disable-pip-version-check", "-c", str(constraints), str(own)])
    check_wheels_against_lock(wheels, locked, own.name.split("-")[0] + "-")
    log("  every dependency wheel matches the SHA-256 in uv.lock")


def unpack_wheels(wheels, site_packages):
    """Unpack every wheel into site-packages. The wheels here are all plain library wheels (no scripts)."""
    site_packages.mkdir(parents=True)
    for wheel in sorted(wheels.glob("*.whl")):
        with zipfile.ZipFile(wheel) as archive:
            for name in archive.namelist():
                parts = PurePosixPath(name).parts
                if name.startswith("/") or ".." in parts:
                    raise BuildError("%s holds an unsafe path: %s" % (wheel.name, name))
                if parts and parts[0].endswith(".data"):
                    raise BuildError("%s has a .data folder (scripts or headers), which this builder does not unpack." % wheel.name)
            archive.extractall(site_packages)


def write_pth(interpreter):
    """Replace the ._pth file: the standard-library zip, the interpreter folder and site-packages, nothing else."""
    pth = next(interpreter.glob("python*._pth"))
    pth.write_text("python%s.zip\n.\nLib\\site-packages\n" % PYTHON_TAG[2:], encoding="utf-8")
    return pth


# ------------------------------------------------------------------ notices and legal

def legal_review_text(*, version, python_sha256, embedded_notices, unsigned_modules, vcruntime, python_org_notices=()):
    lines = [
        "Items for Legal review - %s %s (%s, python.org embeddable Python)" % (PACKAGE, version, PLATFORM),
        "",
        "Written by scripts/build_embedded.py. These are facts found in this build, not legal conclusions.",
        "",
        "1. The interpreter is python.org's, unmodified",
        "",
        "   python/ holds the files of python-%s-embed-amd64.zip as published by python.org (SHA-256 %s)," % (EMBED_VERSION, python_sha256),
        "   except that pythonw.exe was removed and python%s._pth was rewritten to add the packages folder." % PYTHON_TAG[2:],
        "   CPython is under the PSF licence (python/LICENSE.txt, also in THIRD_PARTY_LICENSES/cpython-%s/)." % EMBED_VERSION,
        "   python.exe is signed by the Python Software Foundation; this build looked for that name in the signature",
        "   block and did not validate the signature. No executable of ours is in the package, and pip is not in it.",
        "",
        "2. Notices for the libraries that python.org builds into the interpreter",
        "",
        "   python.org's LICENSE.txt in the embeddable zip does not reproduce every library's notice. Two sets are shipped:",
        "",
        "   a) From python.org, for this release (THIRD_PARTY_LICENSES/cpython-%s/python.org/, copied unchanged from the" % EMBED_VERSION,
        "      python/cpython tag v%s, hashes in SOURCES.txt there): license.rst, CPython's own list of the licences of" % EMBED_VERSION,
        "      incorporated software (OpenSSL, expat, libffi, zlib, libmpdec and others), and LIBRARIES.txt, the library",
        "      versions its Windows build links (nasm there is a build tool and is not in the package).",
        "      Present in this zip: " + (", ".join(python_org_notices) if python_org_notices else "NONE (WARNING)") + ".",
        "",
        "   b) From python-build-standalone, for the same upstream libraries, copied unchanged from",
        "      scripts/python_notices/windows-x86_64/ (hashes in SOURCES.txt there). They add the text of the licences that",
        "      license.rst does not carry (bzip2, liblzma, SQLite) and a standalone copy of each. python-build-standalone did",
        "      not publish them for this build.",
        "",
    ]
    lines += ["        " + name for name in embedded_notices] or ["        (none found: WARNING, run scripts/fetch_python_notices.py)"]
    lines += [
        "",
        "   Question for Legal: do these suffice for the libraries in python.org's build?",
        "",
        "3. Microsoft's Visual C++ runtime files are shipped by python.org in the zip",
        "",
        "   " + (", ".join(vcruntime) if vcruntime else "none found") + ". They are redistributed by python.org under Microsoft's",
        "   terms for the Visual C++ runtime. Their licence text is not in the package.",
        "",
        "   Question for Legal: is redistribution of these files, as python.org does, covered for this product?",
        "",
        "4. Unsigned native modules from third-party wheels",
        "",
        "   " + (", ".join(unsigned_modules) if unsigned_modules else "none") + ". They are not executables of ours and carry no",
        "   signature. A site that enforces signed binaries (AppLocker, WDAC) may object to them.",
        "",
        "5. The product licence text in this zip was copied, not written for this product",
        "",
        "   LICENSE is the AI Solutions Software License Agreement text from the cortex-bridge repository, copied byte",
        "   for byte (its SHA-256 is recorded in BUILDINFO as license_sha256).",
        "",
        "   Question for Legal: is this the approved licence text for lattice-diamond-mcp?",
        "",
    ]
    return "\n".join(lines)


def collect_python_org_notices(out_dir, notices_dir=None):
    """Copy what python.org publishes for this release (license.rst, LIBRARIES.txt, SOURCES.txt); return the names."""
    folder = (notices_dir or NOTICES_DIR) / ("cpython-" + EMBED_VERSION)
    files = sorted(p for p in folder.glob("*") if p.is_file()) if folder.is_dir() else []
    if files:
        out_dir.mkdir(parents=True, exist_ok=True)
        for path in files:
            shutil.copyfile(path, out_dir / path.name)
    return [p.name for p in files]


def collect_notices(out_dir, notices_dir=None):
    """Copy the library notices into out_dir; return their file names."""
    folder = (notices_dir or NOTICES_DIR) / PLATFORM
    files = sorted(folder.glob("LICENSE.*.txt")) if folder.is_dir() else []
    out_dir.mkdir(parents=True, exist_ok=True)
    for path in files:
        shutil.copyfile(path, out_dir / path.name)
    return [p.name for p in files]


# ------------------------------------------------------------------------ checks

def verify_layout(stage, site_names):
    """Checks that need no Windows. BuildError on the first problem."""
    python = stage / "python"
    programs = sorted(p.name for p in stage.rglob("*.exe"))
    if programs != sorted(ONLY_PROGRAMS):
        raise BuildError("The package must hold no program but python.exe; found: %s" % ", ".join(programs))
    pth = (python / ("python%s._pth" % PYTHON_TAG[2:])).read_text(encoding="utf-8").split()
    if "Lib\\site-packages" not in pth or "site" in pth:
        raise BuildError("python%s._pth does not look as expected: %r" % (PYTHON_TAG[2:], pth))
    site = python / "Lib" / "site-packages"
    for name in site_names:
        if not (site / name).exists():
            raise BuildError("Expected %s in site-packages, but it is missing." % name)
    if not list(site.glob("lattice_diamond_mcp-*.dist-info")):
        raise BuildError("The project's own package is not in site-packages.")


def verify_install(stage):
    """On Windows: install the package with its own install.py and start the server."""
    log("Verifying: installing the package into a temporary folder ...")
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "installed"
        out = run([sys.executable, str(stage / "install.py"), "--no-register", "--install-dir", str(target)])
        if "OK: lattice-diamond-mcp" not in out:
            raise BuildError("The throwaway install did not report success:\n" + out)
    log("  install verified")


# ------------------------------------------------------------------------ build

def build(stage, embed_zip):
    version = project_version()
    interpreter = stage / "python"
    interpreter.mkdir(parents=True)
    python_licence = unpack_interpreter(embed_zip, interpreter)

    with tempfile.TemporaryDirectory() as tmp:
        wheels = Path(tmp) / "wheels"
        wheels.mkdir()
        own = build_own_wheel(wheels)
        download_wheels(wheels, own)
        unpack_wheels(wheels, interpreter / "Lib" / "site-packages")
        (stage / "requirements.txt").write_text(requirements_text(wheels), encoding="utf-8")
        index, missing = extract_licences(wheels, stage / "THIRD_PARTY_LICENSES", own.name.split("-")[0] + "-")
        wheel_count = len(list(wheels.glob("*.whl")))
        site_names = sorted({n.split("/", 1)[0] for w in wheels.glob("*.whl") for n in zipfile.ZipFile(w).namelist()
                             if "/" in n and not n.split("/", 1)[0].endswith((".dist-info", ".data"))})
    write_pth(interpreter)

    cpython_dir = stage / "THIRD_PARTY_LICENSES" / ("cpython-" + EMBED_VERSION)
    cpython_dir.mkdir(parents=True, exist_ok=True)
    if python_licence.is_file():
        shutil.copyfile(python_licence, cpython_dir / "LICENSE.txt")
    else:
        missing.append("cpython " + EMBED_VERSION)
    notices = collect_notices(cpython_dir / "embedded-libraries")
    python_org = collect_python_org_notices(cpython_dir / "python.org")
    if not notices:
        missing.append("notices for the libraries inside cpython")
    if "license.rst" not in python_org:
        missing.append("python.org's licence list for cpython (run scripts/fetch_python_notices.py --cpython-only)")
    index += "\ncpython %s  [PSF-2.0]\n    LICENSE.txt\n" % EMBED_VERSION
    index += "".join("    embedded-libraries/%s\n" % n for n in notices)
    index += "".join("    python.org/%s\n" % n for n in python_org)
    (stage / "THIRD_PARTY_LICENSES.txt").write_text("See LEGAL_REVIEW.txt for items that need a decision from Legal.\n\n" + index,
                                                    encoding="utf-8")

    unsigned = sorted(p.name for p in (interpreter / "Lib" / "site-packages").rglob("*.pyd") if not signature_block(p))
    vcruntime = sorted(p.name for p in interpreter.glob("vcruntime*.dll"))
    licence_present = (REPO / "LICENSE").is_file()
    (stage / "LEGAL_REVIEW.txt").write_text(legal_review_text(
        version=version, python_sha256=EMBED_SHA256, embedded_notices=notices, unsigned_modules=unsigned, vcruntime=vcruntime,
        python_org_notices=python_org),
        encoding="utf-8")
    if licence_present:
        shutil.copyfile(REPO / "LICENSE", stage / "LICENSE")

    info = {
        "name": PACKAGE, "version": version, "kind": "embedded", "platform": PLATFORM,
        "python": EMBED_VERSION, "python_tag": PYTHON_TAG, "python_source": EMBED_URL, "python_sha256": EMBED_SHA256,
        "git": git_commit(), "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "wheel_count": wheel_count, "uv_lock_sha256": sha256_file(REPO / "uv.lock"),
        "license_sha256": sha256_file(REPO / "LICENSE") if licence_present else None,
        "unsigned_native_modules": unsigned,
    }
    (stage / "BUILDINFO").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    shutil.copy(REPO / "scripts" / "install.py", stage / "install.py")
    guide = REPO / "docs" / "Lattice_Diamond_MCP_Server_UserGuide.md"
    shutil.copy(guide, stage / guide.name)
    return info, missing, site_names


def write_zip(stage, outdir, info):
    outdir.mkdir(parents=True, exist_ok=True)
    name = "%s-%s-%s-%s" % (PACKAGE, info["version"], "embedded", info["platform"])
    target = outdir / (name + ".zip")
    root = "%s-%s" % (PACKAGE, info["version"])
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                archive.write(path, "%s/%s" % (root, path.relative_to(stage).as_posix()))
    digest = sha256_file(target)
    (outdir / (name + ".zip.sha256")).write_bytes(("%s  %s\n" % (digest, target.name)).encode("utf-8"))
    return target, digest


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the Windows package with python.org's embeddable Python (no executable of ours).")
    parser.add_argument("--outdir", default=str(REPO / "dist"), help="Output directory (default: dist).")
    parser.add_argument("--embed-zip", help="Use this already downloaded embeddable zip (it is still checked against the pinned SHA-256).")
    parser.add_argument("--skip-verify", action="store_true", help="Do not test-install the package on Windows (not for releases).")
    args = parser.parse_args(argv)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            embed_zip = fetch_embeddable(tmp, args.embed_zip)
            stage = Path(tmp) / "bundle"
            stage.mkdir()
            info, missing, site_names = build(stage, embed_zip)
            verify_layout(stage, site_names)
            if sys.platform == "win32" and not args.skip_verify:
                verify_install(stage)
            elif sys.platform != "win32":
                log("NOTE: not on Windows, so the package was checked for layout only and was not run.")
            target, digest = write_zip(stage, Path(args.outdir), info)
    except BuildError as exc:
        print("\nBUILD FAILED: %s" % exc, file=sys.stderr)
        return 1
    log()
    log("Built %s" % target)
    log("  size:    %.1f MB" % (target.stat().st_size / 1e6))
    log("  sha256:  %s" % digest)
    log("  python:  %s (python.org embeddable)   platform: %s   wheels: %d" % (EMBED_VERSION, PLATFORM, info["wheel_count"]))
    if info["unsigned_native_modules"]:
        log("  unsigned native modules (third-party): " + ", ".join(info["unsigned_native_modules"]))
    if missing:
        log()
        log("WARNING: no licence file was found for: " + ", ".join(missing) + ". Check before release.")
    log()
    log("FOR LEGAL: LEGAL_REVIEW.txt inside the zip lists the items that need a decision.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
