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

What is checked: the embeddable zip against the SHA-256 pinned below and against its OpenPGP signature (the .asc
python.org publishes beside it, made with the Windows release signer's key, which is checked in; needs gpg);
that python.exe carries a signature
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
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_installer import (BuildError, check_wheels_against_lock, extract_licences, git_commit, load_lock,  # noqa: E402
                             lock_constraints_text, log, project_version, requirements_text, run, sha256_file,
                             write_manifest)

REPO = Path(__file__).resolve().parents[1]
PACKAGE = "lattice-diamond-mcp"
EMBED_VERSION = "3.13.17"
EMBED_SHA256 = "9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38"          # python-3.13.17-embed-amd64.zip, as downloaded over https from python.org
EMBED_URL = "https://www.python.org/ftp/python/%s/python-%s-embed-amd64.zip" % (EMBED_VERSION, EMBED_VERSION)
PYTHON_TAG = "cp%s" % "".join(EMBED_VERSION.split(".")[:2])
PLATFORM = "windows-x86_64"
SIGNER = b"Python Software Foundation"
# python.org signs each Windows file with an OpenPGP signature (the .asc beside it), made with the key of the
# Windows release signer. The fingerprint below is the pin; the key itself is checked in so that a build never has to
# trust a key server or python.org for it. Where the pin came from: the key ID in python.org's .asc for this release
# (FC624643487034E5), the key fetched from a key server by this fingerprint, and the user ID it carries
# ("Steve Dower (Python Release Signing)"). Whoever updates it should confirm the fingerprint against python.org's
# published list of release-signing keys.
PGP_FINGERPRINT = "7ED10B6531D7C8E1BC296021FC624643487034E5"
PGP_KEY = REPO / "scripts" / "keys" / "python-windows-release-signing.asc"
PGP_BAD_STATUS = ("BADSIG", "EXPSIG", "EXPKEYSIG", "REVKEYSIG", "ERRSIG", "NO_PUBKEY")
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


def check_gpg_status(status):
    """BuildError unless gpg's --status-fd output shows a good signature by the pinned key. Returns the fingerprint."""
    lines = [line.split() for line in status.splitlines() if line.startswith("[GNUPG:]")]
    words = {line[1] for line in lines if len(line) > 1}
    bad = sorted(words & set(PGP_BAD_STATUS))
    if bad:
        raise BuildError("The OpenPGP signature check failed (%s)." % ", ".join(bad))
    valid = [line for line in lines if len(line) > 2 and line[1] == "VALIDSIG"]
    if "GOODSIG" not in words or not valid:
        raise BuildError("gpg did not report a good signature.")
    fingerprints = {valid[0][2], valid[0][-1]}   # the signing (sub)key and its primary key
    if PGP_FINGERPRINT not in fingerprints:
        raise BuildError("The zip is signed by %s, not by the pinned key %s." % (valid[0][-1], PGP_FINGERPRINT))
    return PGP_FINGERPRINT


def verify_pgp(zip_path, signature_path, key_path=None):
    """Check the OpenPGP signature of the zip against the checked-in key, in a throwaway keyring. Returns the fingerprint."""
    gpg = shutil.which("gpg") or shutil.which("gpg2")
    if gpg is None:
        raise BuildError("gpg is not installed, so the OpenPGP signature of the interpreter cannot be checked. Install GnuPG "
                         "(Git for Windows includes it), or pass --skip-pgp for a local experiment (never for a release).")
    key_path = Path(key_path or PGP_KEY)
    if not key_path.is_file():
        raise BuildError("The signing key %s is missing from the repository." % key_path)
    with tempfile.TemporaryDirectory() as home:
        os.chmod(home, 0o700)
        env = dict(os.environ, GNUPGHOME=home, LC_ALL="C")
        base = [gpg, "--batch", "--no-tty", "--no-autostart"]
        imported = subprocess.run(base + ["--import", str(key_path)], env=env, capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, timeout=120)
        if imported.returncode != 0:
            raise BuildError("gpg could not import %s:\n%s" % (key_path, imported.stderr))
        listing = subprocess.run(base + ["--with-colons", "--fingerprint", "--list-keys"], env=env, capture_output=True, text=True,
                                 stdin=subprocess.DEVNULL, timeout=120).stdout
        primaries = [line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")]
        if PGP_FINGERPRINT not in primaries:
            raise BuildError("%s does not contain the pinned key %s." % (key_path, PGP_FINGERPRINT))
        result = subprocess.run(base + ["--status-fd", "1", "--verify", str(signature_path), str(zip_path)], env=env,
                                capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
        if result.returncode != 0 and "[GNUPG:] GOODSIG" not in result.stdout:
            raise BuildError("The OpenPGP signature of %s does not verify:\n%s" % (Path(zip_path).name, result.stderr.strip()))
        return check_gpg_status(result.stdout)


def fetch_embeddable(workdir, given=None, signature=None, check_pgp=True):
    """Return (path of the verified embeddable zip, fingerprint of the key that signed it or None if skipped)."""
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
    fingerprint = None
    if check_pgp:
        sig = Path(signature) if signature else Path(workdir) / (target.name + ".asc")
        if not signature:
            download(EMBED_URL + ".asc", sig)
        fingerprint = verify_pgp(target, sig)
        log("  OpenPGP signature verified (key %s)" % fingerprint)
    else:
        log("WARNING: the OpenPGP signature was NOT checked (--skip-pgp). Do not ship this build.")
    return target, fingerprint


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

MICROSOFT_NAMES = (b"Microsoft Windows Software Compatibility Publisher", b"Microsoft Corporation")


def signer_of(path):
    """Who signed a native file, judged from the names in its signature block (the signature is not validated)."""
    block = signature_block(path)
    if not block:
        return "unsigned"
    if SIGNER in block:
        return "Python Software Foundation"
    if any(name in block for name in MICROSOFT_NAMES):
        return "Microsoft"
    return "signed (other)"


def native_inventory(stage):
    """Rows (path relative to the package, signer, SHA-256) for every program, DLL and extension module in it."""
    rows = []
    for path in sorted(stage.rglob("*")):
        if path.is_file() and path.suffix.lower() in (".exe", ".dll", ".pyd"):
            rows.append((path.relative_to(stage).as_posix(), signer_of(path), sha256_file(path)))
    return rows


def inventory_text(rows):
    lines = ["Every program, DLL and extension module in this package, with who signed it.", "",
             "The signer is read from the names in the file's signature block; the signature itself is not validated by the",
             "build (Windows does that when the file is run). 'unsigned' files have no signature at all.", ""]
    width = max(len(r[0]) for r in rows) if rows else 10
    for path, signer, digest in rows:
        lines.append("%-*s  %-27s %s" % (width, path, signer, digest))
    counts = {}
    for _, signer, _ in rows:
        counts[signer] = counts.get(signer, 0) + 1
    lines += ["", "Totals: " + ", ".join("%d %s" % (n, name) for name, n in sorted(counts.items()))]
    return "\n".join(lines) + "\n"

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
        "3. Microsoft Distributable Code in python.org's Windows binaries",
        "",
        "   " + (", ".join(vcruntime) if vcruntime else "none found") + " are Microsoft's Visual C++ runtime (version info:",
        "   'Microsoft(R) C Runtime Library', (c) Microsoft Corporation), signed by Microsoft. python.org's LICENSE.txt, shipped",
        "   in THIRD_PARTY_LICENSES/cpython-%s/, says in 'Additional Conditions for this Windows binary build' that" % EMBED_VERSION,
        "   Microsoft Distributable Code is embedded in each .exe, .dll and .pyd file it builds, that redistributing the",
        "   interpreter complies with its terms provided four things are not done (alter notices, use Microsoft's trademarks,",
        "   run it on a non-Microsoft platform, put it in malicious programs), and that anyone who further distributes it",
        "   'must require distributors and external end users to agree to terms that protect the Microsoft Distributable Code",
        "   at least as much as Microsoft's own requirements'.",
        "",
        "   The product LICENSE (AI Solutions Software License Agreement) has no reverse-engineering or similar clause and is not",
        "   edited. This build therefore ships THIRD_PARTY_LICENSES/microsoft-distributable-code.txt, listed in",
        "   THIRD_PARTY_LICENSES.txt, which section 4 of the LICENSE makes binding. Its four restrictions are the ones in",
        "   python.org's text; its last item (no reverse engineering, decompiling or disassembling except as law permits) was",
        "   supplied by the project owner and has NOT been checked against Microsoft's own terms.",
        "",
        "   Questions for Legal: does that notice, reached through section 4, satisfy the requirement to bind end users? Is the",
        "   reverse-engineering sentence consistent with Microsoft's actual redistribution terms?",
        "",
        "4. Unsigned native modules from third-party wheels",
        "",
        "   " + (", ".join(unsigned_modules) if unsigned_modules else "none") + ". They are not executables of ours and carry no",
        "   signature (NATIVE_FILES.txt lists them with their SHA-256). A site that enforces signed binaries (AppLocker, WDAC)",
        "   may object to them.",
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


MICROSOFT_CLAUSE = "Microsoft Distributable Code"
MICROSOFT_RESTRICTIONS = ("alter any copyright, trademark or patent notice", "use Microsoft's trademarks",
                          "other\nthan Microsoft operating systems", "malicious, deceptive or\nunlawful programs")


def microsoft_clause_present(licence_text):
    """True if python.org's LICENSE.txt still has the 'Additional Conditions for this Windows binary build' clause
    that the notice below relies on, with the four restrictions it lists."""
    return MICROSOFT_CLAUSE in licence_text and all(item in licence_text for item in MICROSOFT_RESTRICTIONS)


def microsoft_notice_text(python_files):
    """THIRD_PARTY_LICENSES/microsoft-distributable-code.txt. Section 4 of the product licence makes the terms in
    THIRD_PARTY_LICENSES.txt binding on the user, so this is where the protection Microsoft's terms ask for goes (the
    product LICENSE is a copy of an approved text and is not edited)."""
    return "\n".join([
        "Microsoft Distributable Code",
        "",
        "The Windows programs and libraries from python.org in the python folder of this package (python.exe,",
        "python%s.dll, vcruntime140.dll, vcruntime140_1.dll and the other .dll and .pyd files that python.org builds)" % PYTHON_TAG[2:],
        "contain or link Microsoft Distributable Code, copyright Microsoft Corporation, distributed here unmodified,",
        "as python.org distributes it. The Python Software Foundation's licence for these builds (cpython-%s/LICENSE.txt," % EMBED_VERSION,
        "section 'Additional Conditions for this Windows binary build') requires that users of this package agree to terms",
        "that protect the Microsoft Distributable Code at least as much as Microsoft's own requirements. By using this",
        "package you agree that you will not:",
        "",
        "  - alter any copyright, trademark or patent notice in Microsoft's Distributable Code;",
        "  - use Microsoft's trademarks in your programs' names or in a way that suggests your programs come from or are",
        "    endorsed by Microsoft;",
        "  - distribute Microsoft's Distributable Code to run on a platform other than Microsoft operating systems,",
        "    run-time technologies or application platforms;",
        "  - include Microsoft Distributable Code in malicious, deceptive or unlawful programs; or",
        "  - reverse engineer, decompile or disassemble the Microsoft Distributable Code, except and only to the extent that",
        "    applicable law expressly permits.",
        "",
        "These terms apply only to the Microsoft Distributable Code, not to Python or to this software.",
        "",
        "Files of this kind in the package: " + (", ".join(python_files) if python_files else "see NATIVE_FILES.txt") + ".",
        "",
    ])


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

def build(stage, embed_zip, pgp_fingerprint=None):
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
    inventory = native_inventory(stage)
    (stage / "NATIVE_FILES.txt").write_text(inventory_text(inventory), encoding="utf-8")

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
    python_licence_text = python_licence.read_text(encoding="utf-8", errors="replace") if python_licence.is_file() else ""
    clause = microsoft_clause_present(python_licence_text)
    if clause:
        (stage / "THIRD_PARTY_LICENSES" / "microsoft-distributable-code.txt").write_text(
            microsoft_notice_text(sorted(p.name for p in interpreter.iterdir() if p.suffix.lower() in (".exe", ".dll", ".pyd"))),
            encoding="utf-8")
        index += "\nMicrosoft Distributable Code (python.org's Windows binaries, vcruntime140*.dll)\n    microsoft-distributable-code.txt\n"
    else:
        missing.append("the Microsoft Distributable Code clause in python.org's LICENSE.txt (its wording changed: Legal must read it)")
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
        "python_pgp_fingerprint": pgp_fingerprint, "python_pgp_verified": bool(pgp_fingerprint),
        "wheel_count": wheel_count, "uv_lock_sha256": sha256_file(REPO / "uv.lock"),
        "license_sha256": sha256_file(REPO / "LICENSE") if licence_present else None,
        "unsigned_native_modules": unsigned,
    }
    (stage / "BUILDINFO").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    shutil.copy(REPO / "scripts" / "install.py", stage / "install.py")
    guide = REPO / "docs" / "Lattice_Diamond_MCP_Server_UserGuide.md"
    shutil.copy(guide, stage / guide.name)
    write_manifest(stage)
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
    parser.add_argument("--embed-sig", help="The .asc signature that belongs to --embed-zip (default: downloaded from python.org).")
    parser.add_argument("--skip-pgp", action="store_true", help="Do not check the OpenPGP signature (local experiments only).")
    parser.add_argument("--skip-verify", action="store_true", help="Do not test-install the package on Windows (not for releases).")
    args = parser.parse_args(argv)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            embed_zip, fingerprint = fetch_embeddable(tmp, args.embed_zip, args.embed_sig, not args.skip_pgp)
            stage = Path(tmp) / "bundle"
            stage.mkdir()
            info, missing, site_names = build(stage, embed_zip, fingerprint)
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
    log("  OpenPGP: " + ("verified, key " + info["python_pgp_fingerprint"] if info["python_pgp_verified"] else "NOT CHECKED"))
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
