#!/usr/bin/env python3
# Copyright(c) 2026 Lattice Semiconductor Corporation. All rights reserved. DO NOT DISTRIBUTE.
# Subject to the terms of the AI Solutions Software License Agreement
"""
Build the embedded-Python packages: a bundled interpreter with the locked packages unpacked into it, and no executable
of ours.

    python scripts/build_embedded.py [--platform windows|linux] [--outdir dist] [--embed-zip FILE] [--skip-verify]

A frozen launcher (PyInstaller's, which this project used before) is a program nobody has signed, which Windows Defender
and SmartScreen distrust. This package has no such file. For Windows it is python.org's own "embeddable" Python, which the
Python Software Foundation has signed, with the locked packages unpacked beside it; the server is
`python\\python.exe -I -m diamond_mcp`. For Linux (--platform linux) it is python-build-standalone's CPython, described in
the Linux section below; the server is `python/bin/python3.12 -I -m diamond_mcp`. The Windows layout:

    lattice-diamond-mcp-<version>/
        install.py  BUILDINFO  LICENSE  LEGAL_REVIEW.txt  THIRD_PARTY_LICENSES.txt  THIRD_PARTY_LICENSES/
        python/     python.exe, python312.dll, the standard-library zip and extension modules (as published),
                    python312._pth, Lib/site-packages/ (the packages from uv.lock and this project)

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
import re
import shutil
import struct
import subprocess
import sys
import tarfile
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
EMBED_VERSION = "3.12.10"
EMBED_SHA256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"          # python-3.12.10-embed-amd64.zip, as downloaded over https from python.org
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
UV_VERSION = "0.13.5"   # the uv that resolved uv.lock and that picks the Linux interpreter below; CI installs this version
# Packages that are downloaded (at the versions and hashes in uv.lock) and then left out, with the reason. The server never
# imports them: pyjwt (mcp's token handling for OAuth servers and clients, which this stdio server does not use) and what only
# it pulls in. A test starts the server and fails if any of them is imported, and the verify jobs check the installed server.
OMITTED_PACKAGES = {
    "pyjwt": "imported only by mcp's OAuth code, which a stdio server does not load",
    "cryptography": "only pyjwt[crypto] pulls it in (it is a compiled library)",
    "cffi": "only cryptography needs it (a compiled library)",
    "pycparser": "only cffi needs it",
}
# pywin32 is needed on Windows because mcp.os.win32.utilities imports pywintypes, win32api, win32con and win32job at import
# time, but a wheel with hundreds of files and ~55 unsigned native ones is far more than that. Only these files are kept
# (paths in the wheel). Static import tables show win32api.pyd and win32job.pyd depend on no other pywin32 file than
# pywintypes312.dll; pywintypes.py loads _win32sysloader to find that DLL; win32con is plain constants.
PYWIN32_KEEP = ("pywin32.pth", "win32/win32api.pyd", "win32/win32job.pyd", "win32/_win32sysloader.pyd",
                "win32/lib/pywintypes.py", "win32/lib/win32con.py", "win32/lib/pywin32_bootstrap.py",
                "pywin32_system32/pywintypes%s.dll" % PYTHON_TAG[2:])
KEEP_ONLY = {"pywin32": PYWIN32_KEEP}
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


def gpg_wants_posix_paths(gpg):
    """True for an MSYS build of gpg, such as the one in Git for Windows. It runs on Windows but reads paths the Unix way:
    `C:\\Users\\x` is a relative name to it (it was joined to the current folder, so the throwaway keyring could not be
    created) and `/c/Users/x` is the right one. `gpg --version` shows which kind it is: its "Home:" line begins with a
    slash for an MSYS build and with a drive letter for a native Windows one (Gpg4win)."""
    env = {k: v for k, v in os.environ.items() if k != "GNUPGHOME"}
    out = subprocess.run([gpg, "--version"], env=dict(env, LC_ALL="C"), capture_output=True, text=True,
                         stdin=subprocess.DEVNULL, timeout=60).stdout
    return gpg_home_is_posix(out)


def gpg_home_is_posix(version_text):
    match = re.search(r"^Home:\s*(\S.*)$", version_text, re.M)
    return bool(match and match.group(1).strip().startswith("/"))


def to_gpg_path(path, posix):
    """`path` as the gpg in use wants it: for an MSYS gpg, C:\\Users\\x becomes /c/Users/x; otherwise unchanged."""
    text = str(path)
    if posix and re.match(r"^[A-Za-z]:[\\/]", text):
        return "/" + text[0].lower() + text[2:].replace("\\", "/")
    return text


def verify_pgp(zip_path, signature_path, key_path=None):
    """Check the OpenPGP signature of the zip against the checked-in key, in a throwaway keyring. Returns the fingerprint."""
    gpg = shutil.which("gpg") or shutil.which("gpg2")
    if gpg is None:
        raise BuildError("gpg is not installed, so the OpenPGP signature of the interpreter cannot be checked. Install GnuPG "
                         "(Git for Windows includes it), or pass --skip-pgp for a local experiment (never for a release).")
    key_path = Path(key_path or PGP_KEY)
    if not key_path.is_file():
        raise BuildError("The signing key %s is missing from the repository." % key_path)
    posix = gpg_wants_posix_paths(gpg)
    with tempfile.TemporaryDirectory() as home:
        os.chmod(home, 0o700)
        env = dict(os.environ, GNUPGHOME=to_gpg_path(home, posix), LC_ALL="C")
        base = [gpg, "--batch", "--no-tty", "--no-autostart"]
        imported = subprocess.run(base + ["--import", to_gpg_path(key_path, posix)], env=env, capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, timeout=120)
        if imported.returncode != 0:
            raise BuildError("gpg could not import %s:\n%s" % (key_path, imported.stderr))
        listing = subprocess.run(base + ["--with-colons", "--fingerprint", "--list-keys"], env=env, capture_output=True, text=True,
                                 stdin=subprocess.DEVNULL, timeout=120).stdout
        primaries = [line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")]
        if PGP_FINGERPRINT not in primaries:
            raise BuildError("%s does not contain the pinned key %s." % (key_path, PGP_FINGERPRINT))
        result = subprocess.run(base + ["--status-fd", "1", "--verify", to_gpg_path(signature_path, posix), to_gpg_path(zip_path, posix)], env=env,
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
        archive.extractall(dest)  # nosec B202 - every member name was checked above
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


def windows_marker_environment():
    """The dependency-marker values of the Windows machine this package is for. pip judges a dependency's markers
    (`pywin32; sys_platform == 'win32'`) by the machine it RUNS on, whatever --platform says, so on a Linux build machine
    it silently drops every Windows-only dependency (pywin32, colorama), which this package needs to start."""
    version = ".".join(EMBED_VERSION.split(".")[:2])
    return {"sys_platform": "win32", "platform_system": "Windows", "os_name": "nt", "platform_machine": "AMD64",
            "platform_python_implementation": "CPython", "implementation_name": "cpython",
            "python_version": version, "python_full_version": EMBED_VERSION, "implementation_version": EMBED_VERSION,
            "platform_release": "10", "platform_version": ""}


#: pip, started with its marker environment replaced by the target's (the same device cortex-bridge's create_export.py uses).
PIP_AS_TARGET = r"""
import json, runpy, sys
env = json.loads(sys.argv[1])
try:
    from pip._vendor.packaging import markers
    _host = markers.default_environment
    markers.default_environment = lambda: {**_host(), **env}
except Exception as exc:
    sys.stderr.write("error: could not retarget pip's markers: %s\n" % exc)
    sys.exit(3)
sys.argv = ["pip"] + sys.argv[2:]
runpy.run_module("pip", run_name="__main__")
"""


def requirement_parser():
    try:
        from packaging.requirements import Requirement
    except ImportError:
        try:
            from pip._vendor.packaging.requirements import Requirement
        except ImportError:
            raise BuildError("Neither packaging nor pip's copy of it is importable, so the dependencies cannot be checked.")
    return Requirement


def wheel_requirements(wheel):
    """The Requires-Dist lines of a wheel's METADATA."""
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.endswith(".dist-info/METADATA") and name.count("/") == 1:
                return [line.split(":", 1)[1].strip() for line in archive.read(name).decode("utf-8", "replace").splitlines()
                        if line.startswith("Requires-Dist:")]
    return []


def check_closure(wheels, environment, exempt=()):
    """BuildError if a package some wheel needs on the target (its markers judged with `environment`, extras left out) is not
    among the wheels. This is what would have caught a missing pywin32, which makes the server fail at import on Windows."""
    Requirement = requirement_parser()
    present = {re.sub(r"[-_.]+", "-", w.name.split("-")[0]).lower() for w in wheels.glob("*.whl")}
    missing = {}
    for wheel in sorted(wheels.glob("*.whl")):
        for line in wheel_requirements(wheel):
            requirement = Requirement(line)
            if requirement.marker is not None and not requirement.marker.evaluate({**environment, "extra": ""}):
                continue
            name = re.sub(r"[-_.]+", "-", requirement.name).lower()
            if name not in present and name not in exempt:
                missing.setdefault(name, set()).add(wheel.name.split("-")[0])
    if missing:
        raise BuildError("Needed on Windows but not downloaded: " + "; ".join(
            "%s (required by %s)" % (name, ", ".join(sorted(users))) for name, users in sorted(missing.items())))


def download_wheels(wheels, own, tags=("win_amd64",), environment=None, label="Windows"):
    locked = load_lock(REPO / "uv.lock")
    log("Downloading the %s wheels for Python %s at the versions in uv.lock ..." % (label, ".".join(EMBED_VERSION.split(".")[:2])))
    environment = environment or windows_marker_environment()
    with tempfile.TemporaryDirectory() as tmp:
        constraints = Path(tmp) / "locked.txt"
        constraints.write_text(lock_constraints_text(locked), encoding="utf-8")
        run([sys.executable, "-c", PIP_AS_TARGET, json.dumps(environment), "download", "--only-binary=:all:",
             *sum((["--platform", tag] for tag in tags), []), "--python-version", ".".join(EMBED_VERSION.split(".")[:2]), "--implementation", "cp",
             "--abi", PYTHON_TAG, "--abi", "abi3", "--abi", "none", "--dest", str(wheels),
             "--disable-pip-version-check", "-c", str(constraints), str(own)])
    check_wheels_against_lock(wheels, locked, own.name.split("-")[0] + "-")
    log("  every dependency wheel matches the SHA-256 in uv.lock")


def dist_name(wheel):
    return re.sub(r"[-_.]+", "-", wheel.name.split("-")[0]).lower()


def omit_packages(wheels, omitted=None):
    """Delete the wheels of the packages the server does not use; return their names. They were downloaded only so that
    the choice is made from uv.lock's own closure and the hash check covers them."""
    omitted = OMITTED_PACKAGES if omitted is None else omitted
    removed = []
    for wheel in sorted(wheels.glob("*.whl")):
        if dist_name(wheel) in omitted:
            removed.append(dist_name(wheel))
            wheel.unlink()
    return removed


def prune_wheel_files(wheels, site_packages, keep_only=None):
    """For the wheels named in `keep_only` ({name: paths}), delete every unpacked file but the listed ones and the wheel's
    dist-info. BuildError when a listed file is not in the wheel (its layout changed). Returns {name: files removed}."""
    keep_only = KEEP_ONLY if keep_only is None else keep_only
    pruned = {}
    for wheel in sorted(wheels.glob("*.whl")):
        name = dist_name(wheel)
        if name not in keep_only:
            continue
        with zipfile.ZipFile(wheel) as archive:
            members = [m.filename for m in archive.infolist() if not m.is_dir() and not m.filename.split("/")[0].endswith(".data")]
        missing = [path for path in keep_only[name] if path not in members]
        if missing:
            raise BuildError("%s no longer holds %s; update the list of files kept from it in build_embedded.py." % (wheel.name, ", ".join(missing)))
        count = 0
        for member in members:
            if member in keep_only[name] or member.split("/")[0].endswith(".dist-info"):
                continue
            target = site_packages.joinpath(*member.split("/"))
            if target.exists():
                target.unlink()
                count += 1
        for folder in sorted((p for p in site_packages.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            if not any(folder.iterdir()):
                folder.rmdir()
        pruned[name] = count
    return pruned


def pe_imports(path):
    """Names of the DLLs a Windows program or module imports (its import table; delay-loaded ones are not listed)."""
    data = Path(path).read_bytes()
    if data[:2] != b"MZ" or len(data) < 0x40:
        return []
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        return []
    sections_count = struct.unpack_from("<H", data, pe + 6)[0]
    optional_size = struct.unpack_from("<H", data, pe + 20)[0]
    optional = pe + 24
    directories = optional + (112 if struct.unpack_from("<H", data, optional)[0] == 0x20B else 96)
    rva = struct.unpack_from("<I", data, directories + 8)[0]          # directory entry 1: the import table
    if not rva:
        return []
    sections = [struct.unpack_from("<IIII", data, optional + optional_size + 40 * i + 8) for i in range(sections_count)]

    def offset(address):
        for virtual_size, virtual_address, raw_size, raw_pointer in sections:
            if virtual_address <= address < virtual_address + max(virtual_size, raw_size):
                return address - virtual_address + raw_pointer
        raise BuildError("%s has an import table that points outside its sections." % Path(path).name)

    names, at = [], offset(rva)
    while True:
        name_rva = struct.unpack_from("<I", data, at + 12)[0]
        if name_rva == 0:
            return names
        start = offset(name_rva)
        names.append(data[start:data.index(b"\0", start)].decode("ascii", "replace"))
        at += 20


def native_files(folder):
    return [p for p in Path(folder).rglob("*") if p.is_file() and p.suffix.lower() in (".pyd", ".dll")]


def check_native_dependencies(interpreter, files_removed_names):
    """BuildError if a native file left in the package imports a DLL that this build deleted. `files_removed_names` is the set
    of lower-case names of the DLL and module files that were in the wheels and are no longer in the package. (Imports of
    Python modules from inside a native module are invisible here; the Windows verify job imports them for real.)"""
    problems = []
    for path in native_files(interpreter):
        for imported in pe_imports(path):
            if imported.lower() in files_removed_names:
                problems.append("%s needs %s, which was removed" % (path.name, imported))
    if problems:
        raise BuildError("Trimming the packages removed a file that one that stayed still needs: " + "; ".join(sorted(set(problems))))


def unpack_wheels(wheels, site_packages):
    """Unpack every wheel into site-packages and return the scripts that were left out.

    A wheel's `<name>-<version>.data/` folder is laid out by install scheme: purelib and platlib go into site-packages
    (what pip does); scripts are NOT installed (they would be programs, and none is needed: pywin32's two are a post-install
    helper and a test runner); anything else (headers, data, include) is refused so nothing is dropped unnoticed."""
    site_packages.mkdir(parents=True, exist_ok=True)
    skipped = []
    for wheel in sorted(wheels.glob("*.whl")):
        with zipfile.ZipFile(wheel) as archive:
            for member in archive.infolist():
                name = member.filename
                parts = PurePosixPath(name).parts
                if name.startswith("/") or ".." in parts:
                    raise BuildError("%s holds an unsafe path: %s" % (wheel.name, name))
                if member.is_dir():
                    continue
                if parts and parts[0].endswith(".data"):
                    scheme = parts[1] if len(parts) > 2 else ""
                    if scheme == "scripts":
                        skipped.append("%s: %s" % (wheel.name.split("-")[0], "/".join(parts[2:])))
                        continue
                    if scheme not in ("purelib", "platlib"):
                        raise BuildError("%s has %s, a kind of .data folder this builder does not unpack." % (wheel.name, "/".join(parts[:2])))
                    target = site_packages.joinpath(*parts[2:])
                else:
                    target = site_packages.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(member))
    return skipped


def remove_programs(site_packages):
    """Delete every program (.exe) that a package's wheel put into site-packages, and return their paths (relative to it).

    The point of this package is that no unsigned program is in it. pywin32 ships two, Pythonwin.exe (an IDE) and
    pythonservice.exe (the host for Windows services); this server uses neither. The removal is recorded in BUILDINFO and
    LEGAL_REVIEW.txt because the files then differ from the wheel (whose SHA-256 was checked against uv.lock before)."""
    removed = []
    for path in sorted(site_packages.rglob("*")):
        if path.is_file() and path.suffix.lower() == ".exe":
            removed.append(path.relative_to(site_packages).as_posix())
            path.unlink()
    return removed


def write_pth(interpreter):
    """Replace the ._pth file: the standard-library zip, the interpreter folder, site-packages, and `import site`.

    The embeddable Python ignores .pth files unless `import site` is enabled, and pywin32 (which mcp needs on Windows) puts
    its folders on the path and finds its DLLs through pywin32.pth. Everything else about the embeddable's isolation stays:
    with a ._pth file present it still ignores PYTHONPATH and the current folder, and install.py starts it with -I."""
    pth = next(interpreter.glob("python*._pth"))
    pth.write_text("python%s.zip\n.\nLib\\site-packages\nimport site\n" % PYTHON_TAG[2:], encoding="utf-8")
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


def is_elf(path):
    try:
        with open(path, "rb") as handle:
            return handle.read(4) == b"\x7fELF"
    except OSError:
        return False


def native_inventory(stage, platform=PLATFORM):
    """Rows (path relative to the package, signer, SHA-256) for every program, DLL and extension module in it.
    On Linux every ELF file is listed and the signer is "not applicable": Linux has no equivalent of Authenticode."""
    rows = []
    if platform.startswith("linux"):
        for path in sorted(stage.rglob("*")):
            if path.is_file() and not path.is_symlink() and is_elf(path):
                rows.append((path.relative_to(stage).as_posix(), "not applicable (Linux)", sha256_file(path)))
        return rows
    for path in sorted(stage.rglob("*")):
        if path.is_file() and path.suffix.lower() in (".exe", ".dll", ".pyd"):
            rows.append((path.relative_to(stage).as_posix(), signer_of(path), sha256_file(path)))
    return rows


def inventory_text(rows):
    linux = bool(rows) and all(r[1].startswith("not applicable") for r in rows)
    lines = (["Every ELF program and shared library in this package. Linux has no code-signing check like Windows'",
              "Authenticode, so there is no signer to list; the SHA-256 of each file is in this list and in MANIFEST.sha256.", ""]
             if linux else
             ["Every program, DLL and extension module in this package, with who signed it.", "",
              "The signer is read from the names in the file's signature block; the signature itself is not validated by the",
              "build (Windows does that when the file is run). 'unsigned' files have no signature at all.", ""])
    width = max(len(r[0]) for r in rows) if rows else 10
    for path, signer, digest in rows:
        lines.append(("%-*s  %s" % (width, path, digest)) if linux else ("%-*s  %-27s %s" % (width, path, signer, digest)))
    counts = {}
    for _, signer, _ in rows:
        counts[signer] = counts.get(signer, 0) + 1
    lines += ["", "Totals: " + ", ".join("%d %s" % (n, name) for name, n in sorted(counts.items()))]
    return "\n".join(lines) + "\n"

def legal_review_text(*, version, python_sha256, embedded_notices, unsigned_modules, vcruntime, python_org_notices=(),
                      removed_programs=(), omitted_packages=(), pruned=None):
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
        "4. Unsigned native files from third-party wheels",
        "",
        "   " + (", ".join(unsigned_modules) if unsigned_modules else "none") + ". They are not executables of ours and carry no",
        "   signature (NATIVE_FILES.txt lists them with their SHA-256). A site that enforces signed binaries (AppLocker, WDAC)",
        "   may object to them.",
        "",
        "   Packages that were downloaded (hash-checked against uv.lock) and left out because the server never imports them: " +
        (", ".join(omitted_packages) or "none") + ". Files removed from the pywin32 wheel, which is needed only for four modules",
        "   (pywintypes, win32api, win32con, win32job, imported by mcp on Windows): " + (", ".join("%s: %d" % i for i in (pruned or {}).items()) or "none") + ".",
        "   Reasons are in OMITTED_PACKAGES and PYWIN32_KEEP in scripts/build_embedded.py. The 'unsigned' list above is therefore short.",
        "",
        "   Programs that wheels contain and this package removes (none is needed by the server): " + (", ".join(removed_programs) or "none") + ".",
        "   They would be unsigned executables. The files in the package therefore differ from those wheels in exactly these",
        "   deletions; each wheel was checked against uv.lock before the files were unpacked.",
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


def collect_notices(out_dir, notices_dir=None, platform=PLATFORM):
    """Copy the library notices of `platform` into out_dir; return their file names."""
    folder = (notices_dir or NOTICES_DIR) / platform
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
    pth = (python / ("python%s._pth" % PYTHON_TAG[2:])).read_text(encoding="utf-8").splitlines()
    if "Lib\\site-packages" not in pth or "import site" not in pth or any(line.startswith("#") for line in pth):
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


# ------------------------------------------------------------------------ Linux (python-build-standalone)

LINUX_PLATFORM = "linux-x86_64"
PBS_RELEASE = "20250529"          # the python-build-standalone release that uv 0.13.5 selects for CPython 3.12.10
PBS_ARCHIVE = "cpython-%s+%s-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz" % (EMBED_VERSION, PBS_RELEASE)
PBS_URL = "https://github.com/astral-sh/python-build-standalone/releases/download/%s/%s" % (PBS_RELEASE, PBS_ARCHIVE.replace("+", "%2B"))
PBS_SHA256 = "f50d08e0304b15d752b22932047135c1dc02f81977b284e51288be9d90627bca"   # from the release's SHA256SUMS
LINUX_TAGS = ("manylinux_2_17_x86_64", "manylinux2014_x86_64")
LINUX_MAX_GLIBC = (2, 17)         # Red Hat 7; the build fails if a file in the package needs a newer glibc
MINOR = ".".join(EMBED_VERSION.split(".")[:2])
LINUX_PROGRAM = "bin/python" + MINOR
GLIBC_RE = re.compile(r"GLIBC_(\d+)\.(\d+)")
# What python-build-standalone's `install_only_stripped` archive holds that this package does not need, relative to python/:
# headers, man pages and terminfo, Tcl/Tk and IDLE, pip, the test suite, the build helpers, the shared libpython (the
# interpreter is linked statically, so it is not used) and every symbolic link (the zip format cannot carry them well, and
# bin/python3.12 is the only program started).
LINUX_REMOVE = ("include", "share", "lib/pkgconfig", "lib/tcl*", "lib/tk*", "lib/Tix*", "lib/itcl*", "lib/thread*", "lib/libtcl*",
                "lib/libtk*", "lib/libpython3*",
                "lib/python%s/ensurepip" % MINOR, "lib/python%s/idlelib" % MINOR, "lib/python%s/test" % MINOR,
                "lib/python%s/tkinter" % MINOR, "lib/python%s/turtledemo" % MINOR, "lib/python%s/turtle.py" % MINOR,
                "lib/python%s/lib2to3" % MINOR, "lib/python%s/pydoc_data" % MINOR, "lib/python%s/config-*" % MINOR,
                "lib/python%s/lib-dynload/*" % MINOR, "lib/python%s/site-packages/*" % MINOR)


def linux_marker_environment():
    return {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix", "platform_machine": "x86_64",
            "platform_python_implementation": "CPython", "implementation_name": "cpython", "python_version": MINOR,
            "python_full_version": EMBED_VERSION, "implementation_version": EMBED_VERSION, "platform_release": "", "platform_version": ""}


def fetch_pbs(workdir, given=None):
    """Return the path of the python-build-standalone archive, checked against the pinned SHA-256."""
    target = Path(given) if given else Path(workdir) / PBS_ARCHIVE
    if not given:
        log("Downloading %s ..." % PBS_URL)
        download(PBS_URL, target)
    digest = sha256_file(target)
    if digest != PBS_SHA256:
        raise BuildError("%s has SHA-256 %s, expected %s. The release asset may have been replaced, or the download was altered; "
                         "nothing was built." % (target.name, digest, PBS_SHA256))
    return target


def unpack_pbs(archive_path, dest):
    """Unpack the archive's python/ folder into `dest`, leaving out symbolic links and hard links, and trim it."""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:gz") as tar:
        for member in tar:
            parts = PurePosixPath(member.name).parts
            if not parts or parts[0] != "python" or member.name.startswith("/") or ".." in parts:
                raise BuildError("The python-build-standalone archive holds an unexpected path: %s" % member.name)
            if member.issym() or member.islnk() or len(parts) == 1:
                continue
            target = dest.joinpath(*parts[1:])
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as source, open(target, "wb") as sink:
                    shutil.copyfileobj(source, sink)
                os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)
            else:
                raise BuildError("The python-build-standalone archive holds a member of an unsupported type: %s" % member.name)
    return trim_pbs(dest)


def trim_pbs(root):
    """Delete what LINUX_REMOVE lists and every program in bin/ but the interpreter. Returns the paths removed."""
    import glob
    removed = []
    for pattern in LINUX_REMOVE:
        for path in sorted(glob.glob(str(root / pattern))):
            path = Path(path)
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed.append(path.relative_to(root).as_posix())
    bin_dir = root / "bin"
    if not (bin_dir / ("python" + MINOR)).is_file():
        raise BuildError("The python-build-standalone archive has no %s; its layout has changed." % LINUX_PROGRAM)
    for path in sorted(bin_dir.iterdir()):
        if path.name != "python" + MINOR:
            path.unlink()
            removed.append(path.relative_to(root).as_posix())
    return removed


def parse_glibc_requirement(objdump_output):
    """Highest GLIBC_x.y symbol version named in `objdump -T` output, or None."""
    versions = [(int(m.group(1)), int(m.group(2))) for m in GLIBC_RE.finditer(objdump_output)]
    return max(versions) if versions else None


def glibc_floor(tree, limit=LINUX_MAX_GLIBC):
    """The highest glibc any ELF file under `tree` needs. BuildError if that is newer than `limit`."""
    if shutil.which("objdump") is None:
        raise BuildError("objdump (binutils) is required to check glibc requirements.")
    report = {}
    for path in sorted(Path(tree).rglob("*")):
        if path.is_file() and not path.is_symlink() and is_elf(path):
            result = subprocess.run(["objdump", "-T", str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            need = parse_glibc_requirement(result.stdout)
            if need:
                report[path.relative_to(tree).as_posix()] = need
    offenders = {name: need for name, need in report.items() if need > limit}
    if offenders:
        raise BuildError("These files need glibc newer than %d.%d, so the package would not start on the oldest supported systems:\n%s"
                         % (limit + ("\n".join("  glibc %d.%d  %s" % (need + (name,)) for name, need in sorted(offenders.items(), key=lambda kv: kv[1], reverse=True)),)))
    return max(report.values()) if report else None


def verify_layout_linux(stage, site_names):
    """Checks that need nothing but files. BuildError on the first problem."""
    python = stage / "python"
    programs = sorted(p.relative_to(python).as_posix() for p in (python / "bin").iterdir())
    if programs != [LINUX_PROGRAM]:
        raise BuildError("The package must hold no program but %s in bin/; found: %s" % (LINUX_PROGRAM, ", ".join(programs) or "nothing"))
    if not os.access(python / LINUX_PROGRAM, os.X_OK):
        raise BuildError("%s is not executable." % LINUX_PROGRAM)
    links = [p.relative_to(stage).as_posix() for p in stage.rglob("*") if p.is_symlink()]
    if links:
        raise BuildError("The package must hold no symbolic links; found: %s" % ", ".join(links[:5]))
    site = python / "lib" / ("python" + MINOR) / "site-packages"
    for name in site_names:
        if not (site / name).exists():
            raise BuildError("Expected %s in site-packages, but it is missing." % name)
    if not list(site.glob("lattice_diamond_mcp-*.dist-info")):
        raise BuildError("The project's own package is not in site-packages.")
    if (site / "pip").exists() or list(site.glob("pip-*")):
        raise BuildError("pip must not be in the package.")


def linux_legal_review_text(*, version, embedded_notices, native_files, removed, omitted_packages, floor):
    lines = [
        "Items for Legal review - %s %s (%s, python-build-standalone Python)" % (PACKAGE, version, LINUX_PLATFORM),
        "",
        "Written by scripts/build_embedded.py. These are facts found in this build, not legal conclusions.",
        "",
        "1. The interpreter is python-build-standalone's CPython, unmodified except for deleted files",
        "",
        "   python/ holds CPython %s from %s" % (EMBED_VERSION, PBS_URL),
        "   (SHA-256 %s, release %s; the project is https://github.com/astral-sh/python-build-standalone)." % (PBS_SHA256, PBS_RELEASE),
        "   %d files and folders of the archive were deleted (pip, Tcl/Tk, IDLE, the test suite, headers, the shared libpython, every" % len(removed),
        "   symbolic link and every program in bin/ but python%s); no file was changed. pip is not in the package and no" % MINOR,
        "   executable of ours is. CPython is under the PSF licence (THIRD_PARTY_LICENSES/cpython-%s/LICENSE.txt)." % EMBED_VERSION,
        "   Integrity: the archive is checked against a SHA-256 pinned in the build, taken from the release's SHA256SUMS.",
        "   python-build-standalone publishes no signature, so unlike the Windows interpreter there is no OpenPGP check.",
        "",
        "2. Notices for the libraries compiled into the interpreter",
        "",
        "   OpenSSL, zlib, expat, bzip2, liblzma, mpdecimal, libedit, ncurses, libffi, libuuid and SQLite are built into it. The notices",
        "   python-build-standalone publishes for release %s are copied unchanged from scripts/python_notices/linux-x86_64/" % PBS_RELEASE,
        "   (hashes in SOURCES.txt there) into THIRD_PARTY_LICENSES/cpython-%s/embedded-libraries/:" % EMBED_VERSION,
        "",
    ]
    lines += ["        " + name for name in embedded_notices] or ["        (none found: WARNING, run scripts/fetch_python_notices.py)"]
    lines += [
        "",
        "   Question for Legal: do these suffice for the libraries in this build?",
        "",
        "3. System libraries are the customer's, not shipped",
        "",
        "   The interpreter links the C library of the machine it runs on (glibc: libc, libm, libpthread and others), so it needs glibc",
        "   %s or later (the highest glibc symbol in any shipped file is %s). Nothing from glibc is distributed." % (
            "%d.%d" % LINUX_MAX_GLIBC, "%d.%d" % floor if floor else "none"),
        "",
        "4. Native files from third-party packages; packages left out",
        "",
        "   Shared libraries in the package: " + (", ".join(native_files) or "none") + " (NATIVE_FILES.txt lists every ELF file with its SHA-256).",
        "   Packages downloaded (hash-checked against uv.lock) and left out because the server never imports them: " + (", ".join(omitted_packages) or "none") + ".",
        "   Linux has no code-signing check comparable to Windows'.",
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


def build_linux(stage, archive):
    version = project_version()
    interpreter = stage / "python"
    removed = unpack_pbs(archive, interpreter)
    site = interpreter / "lib" / ("python" + MINOR) / "site-packages"
    python_licence = interpreter / "lib" / ("python" + MINOR) / "LICENSE.txt"

    with tempfile.TemporaryDirectory() as tmp:
        wheels = Path(tmp) / "wheels"
        wheels.mkdir()
        own = build_own_wheel(wheels)
        download_wheels(wheels, own, LINUX_TAGS, linux_marker_environment(), "Linux")
        omitted_packages = omit_packages(wheels)
        log("  left out (the server does not use them): " + ", ".join(omitted_packages))
        check_closure(wheels, linux_marker_environment(), exempt=set(OMITTED_PACKAGES))
        log("  every other package the wheels need on Linux is present")
        skipped_scripts = unpack_wheels(wheels, site)
        if skipped_scripts:
            log("  not installed (scripts in .data folders): " + ", ".join(skipped_scripts))
        (stage / "requirements.txt").write_text(requirements_text(wheels), encoding="utf-8")
        index, missing = extract_licences(wheels, stage / "THIRD_PARTY_LICENSES", own.name.split("-")[0] + "-")
        wheel_count = len(list(wheels.glob("*.whl")))
        site_names = sorted({n.split("/", 1)[0] for w in wheels.glob("*.whl") for n in zipfile.ZipFile(w).namelist()
                             if "/" in n and not n.split("/", 1)[0].endswith((".dist-info", ".data"))})
    floor = glibc_floor(interpreter)

    cpython_dir = stage / "THIRD_PARTY_LICENSES" / ("cpython-" + EMBED_VERSION)
    cpython_dir.mkdir(parents=True, exist_ok=True)
    if python_licence.is_file():
        shutil.copyfile(python_licence, cpython_dir / "LICENSE.txt")
    else:
        missing.append("cpython " + EMBED_VERSION)
    release_file = NOTICES_DIR / "RELEASE"
    release = release_file.read_text(encoding="utf-8").strip() if release_file.is_file() else ""
    if release != PBS_RELEASE:
        raise BuildError("scripts/python_notices/ holds the notices of python-build-standalone release %s, but this build uses release %s. "
                         "Run scripts/fetch_python_notices.py --release %s and have Legal read the diff." % (release or "(unknown)", PBS_RELEASE, PBS_RELEASE))
    notices = collect_notices(cpython_dir / "embedded-libraries", platform=LINUX_PLATFORM)
    if not notices:
        missing.append("notices for the libraries inside cpython")
    index += "\ncpython %s  [PSF-2.0]\n    LICENSE.txt\n" % EMBED_VERSION
    index += "".join("    embedded-libraries/%s\n" % n for n in notices)
    (stage / "THIRD_PARTY_LICENSES.txt").write_text("See LEGAL_REVIEW.txt for items that need a decision from Legal.\n\n" + index, encoding="utf-8")

    inventory = native_inventory(stage, LINUX_PLATFORM)
    (stage / "NATIVE_FILES.txt").write_text(inventory_text(inventory), encoding="utf-8")
    licence_present = (REPO / "LICENSE").is_file()
    (stage / "LEGAL_REVIEW.txt").write_text(linux_legal_review_text(
        version=version, embedded_notices=notices, native_files=[Path(r[0]).name for r in inventory if "site-packages" in r[0]],
        removed=removed, omitted_packages=omitted_packages, floor=floor), encoding="utf-8")
    if licence_present:
        shutil.copyfile(REPO / "LICENSE", stage / "LICENSE")

    info = {
        "name": PACKAGE, "version": version, "kind": "embedded", "platform": LINUX_PLATFORM,
        "python": EMBED_VERSION, "python_tag": PYTHON_TAG, "python_source": PBS_URL, "python_sha256": PBS_SHA256,
        "python_release": PBS_RELEASE, "python_pgp_fingerprint": None, "python_pgp_verified": False,
        "min_glibc": "%d.%d" % floor if floor else None, "program": LINUX_PROGRAM,
        "git": git_commit(), "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "wheel_count": wheel_count, "uv_lock_sha256": sha256_file(REPO / "uv.lock"),
        "license_sha256": sha256_file(REPO / "LICENSE") if licence_present else None,
        "omitted_packages": omitted_packages, "unsigned_native_modules": [], "removed_programs": [],
    }
    (stage / "BUILDINFO").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    shutil.copy(REPO / "scripts" / "install.py", stage / "install.py")
    guide = REPO / "docs" / "Lattice_Diamond_MCP_Server_UserGuide.md"
    shutil.copy(guide, stage / guide.name)
    write_manifest(stage)
    return info, missing, site_names


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
        omitted_packages = omit_packages(wheels)
        log("  left out (the server does not use them): " + ", ".join(omitted_packages))
        check_closure(wheels, windows_marker_environment(), exempt=set(OMITTED_PACKAGES))
        log("  every other package the wheels need on Windows is present")
        site_packages = interpreter / "Lib" / "site-packages"
        wheel_natives = {n.rsplit("/", 1)[-1].lower() for w in wheels.glob("*.whl") for n in zipfile.ZipFile(w).namelist()
                         if n.lower().endswith((".pyd", ".dll"))}
        skipped_scripts = unpack_wheels(wheels, interpreter / "Lib" / "site-packages")
        pruned = prune_wheel_files(wheels, site_packages)
        if pruned:
            log("  trimmed to the files the server needs: " + ", ".join("%s (%d files removed)" % item for item in pruned.items()))
        check_native_dependencies(interpreter, wheel_natives - {p.name.lower() for p in native_files(interpreter)})
        if skipped_scripts:
            log("  not installed (scripts in .data folders): " + ", ".join(skipped_scripts))
        removed_programs = remove_programs(interpreter / "Lib" / "site-packages")
        if removed_programs:
            log("  removed (programs from wheels; none is needed): " + ", ".join(removed_programs))
        (stage / "requirements.txt").write_text(requirements_text(wheels), encoding="utf-8")
        index, missing = extract_licences(wheels, stage / "THIRD_PARTY_LICENSES", own.name.split("-")[0] + "-")
        wheel_count = len(list(wheels.glob("*.whl")))
        site_names = sorted({n.split("/", 1)[0] for w in wheels.glob("*.whl")
                             for n in (KEEP_ONLY.get(dist_name(w)) or zipfile.ZipFile(w).namelist())
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

    unsigned = sorted(p.name for p in (interpreter / "Lib" / "site-packages").rglob("*")
                      if p.suffix.lower() in (".pyd", ".dll") and not signature_block(p))
    vcruntime = sorted(p.name for p in interpreter.glob("vcruntime*.dll"))
    licence_present = (REPO / "LICENSE").is_file()
    (stage / "LEGAL_REVIEW.txt").write_text(legal_review_text(
        version=version, python_sha256=EMBED_SHA256, embedded_notices=notices, unsigned_modules=unsigned, vcruntime=vcruntime,
        python_org_notices=python_org, removed_programs=removed_programs, omitted_packages=omitted_packages, pruned=pruned),
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
        "unsigned_native_modules": unsigned, "removed_programs": removed_programs,
        "omitted_packages": omitted_packages, "pruned_files": pruned,
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
    parser = argparse.ArgumentParser(description="Build an embedded-Python package (no executable of ours): for Windows with python.org's "
                                                 "embeddable Python, or for Linux with python-build-standalone's.")
    parser.add_argument("--platform", choices=("windows", "linux"), default="windows", help="which package to build (default: windows)")
    parser.add_argument("--outdir", default=str(REPO / "dist"), help="Output directory (default: dist).")
    parser.add_argument("--embed-zip", help="Use this already downloaded interpreter archive (python.org's zip for Windows, the "
                                            "python-build-standalone .tar.gz for Linux); it is still checked against its pinned SHA-256.")
    parser.add_argument("--embed-sig", help="Windows: the .asc signature that belongs to --embed-zip (default: downloaded from python.org).")
    parser.add_argument("--skip-pgp", action="store_true", help="Windows: do not check the OpenPGP signature (local experiments only).")
    parser.add_argument("--skip-verify", action="store_true", help="Do not test-install the package when this system can run it (not for releases).")
    args = parser.parse_args(argv)
    linux = args.platform == "linux"
    if linux and (args.embed_sig or args.skip_pgp):
        parser.error("--embed-sig and --skip-pgp are for the Windows package (python-build-standalone publishes no signature)")
    runs_here = sys.platform.startswith("linux") if linux else sys.platform == "win32"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "bundle"
            stage.mkdir()
            if linux:
                archive = fetch_pbs(tmp, args.embed_zip)
                info, missing, site_names = build_linux(stage, archive)
                verify_layout_linux(stage, site_names)
            else:
                embed_zip, fingerprint = fetch_embeddable(tmp, args.embed_zip, args.embed_sig, not args.skip_pgp)
                info, missing, site_names = build(stage, embed_zip, fingerprint)
                verify_layout(stage, site_names)
            if runs_here and not args.skip_verify:
                verify_install(stage)
            elif not runs_here:
                log("NOTE: not on %s, so the package was checked for layout only and was not run." % ("Linux" if linux else "Windows"))
            target, digest = write_zip(stage, Path(args.outdir), info)
    except BuildError as exc:
        print("\nBUILD FAILED: %s" % exc, file=sys.stderr)
        return 1
    log()
    log("Built %s" % target)
    log("  size:    %.1f MB" % (target.stat().st_size / 1e6))
    log("  sha256:  %s" % digest)
    log("  python:  %s (%s)   platform: %s   wheels: %d" % (EMBED_VERSION, "python-build-standalone" if linux else "python.org embeddable",
                                                          info["platform"], info["wheel_count"]))
    if linux:
        log("  glibc:   %s or later   integrity: SHA-256 pinned (no signature is published)" % info["min_glibc"])
    else:
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
