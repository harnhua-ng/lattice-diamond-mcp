#!/usr/bin/env python3
# Copyright(c) 2026 Lattice Semiconductor Corporation. All rights reserved. DO NOT DISTRIBUTE.
# Subject to the terms of the AI Solutions Software License Agreement
"""
Build the standalone installer zip for lattice-diamond-mcp: a PyInstaller build that needs no
Python on the machine it is installed on.

    python scripts/build_binary.py [--outdir dist]

Run it with any Python 3.11 or later and with `uv` installed (the build uses the pinned version
below). The Python that goes INTO the program is a separate, uv-managed CPython, pinned below,
so the result does not depend on which Python runs this script or on the build machine's
system libraries. Build one zip per operating system, each on that system.

What it does, in order (any failure stops the build and writes nothing):

  1. checks that uv.lock matches pyproject.toml and that the uv version is the pinned one;
  2. installs the pinned CPython with uv and builds with `uv run --frozen` from the lock file,
     so every package is the exact, hash-checked version recorded there;
  3. on Linux, fails if any bundled native file needs a newer glibc than MAX_GLIBC. This is the
     check that keeps the program runnable on Red Hat 7 (glibc 2.17);
  4. starts the frozen program and talks MCP to it (initialize, tools/list, a tool call);
  5. writes a CycloneDX SBOM: the locked Python packages from `uv export`, plus the embedded
     interpreter and every bundled native file with its SHA-256, which `uv export` cannot know;
  6. collects licence texts, copies LICENSE, writes BUILDINFO and LEGAL_REVIEW.txt (items for
     Legal, built from the licence file that ships), zips everything with install.py, and installs the zip into a
     temporary folder and runs it again.

`--smoke-test PROGRAM` skips the build and only runs step 4 against an existing program; the CI
workflow uses it to check the installed result on a clean runner.

Reproducibility: the package set, the interpreter and the compiled libraries are identical from
build to build, and SOURCE_DATE_EPOCH (set from the commit time) makes the diamond-mcp program
itself identical too. The zip as a whole is NOT bit-for-bit reproducible: PyInstaller's
base_library.zip, some packages' RECORD files, BUILDINFO and the SBOM's serial number and
timestamp differ between builds.

Output, in --outdir:
    lattice-diamond-mcp-<version>-<platform>.zip
    lattice-diamond-mcp-<version>-<platform>.zip.sha256
    lattice-diamond-mcp-<version>-<platform>.cdx.json   (the SBOM, also inside the zip)
"""

import argparse
import ast
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python 3.10 and older
    sys.exit("build_binary.py needs Python 3.11 or later (it reads pyproject.toml with tomllib).")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_installer import (LICENSE_NAME_RE, BuildError, git_commit, log, normalise, platform_name, run,  # noqa: E402
                             write_manifest)

REPO = Path(__file__).resolve().parents[1]
PACKAGE = "lattice-diamond-mcp"
UV_VERSION = "0.13.5"          # the uv that resolved the lock and fetches the interpreter below
PYTHON_VERSION = "3.12.10"     # the CPython embedded in the program (uv-managed, glibc 2.17 build)
NOTICES_DIR = REPO / "scripts" / "python_notices"   # notices of the libraries inside the embedded CPython (fetch_python_notices.py)
MAX_GLIBC = (2, 17)            # Red Hat 7; the build fails if a bundled file needs newer
IS_WINDOWS = sys.platform == "win32"
EXE_NAME = "diamond-mcp.exe" if IS_WINDOWS else "diamond-mcp"
NATIVE_SUFFIXES = (".so", ".dll", ".pyd", ".dylib")
GLIBC_RE = re.compile(r"GLIBC_(\d+)\.(\d+)")
MIN_TOOLS = 30
SMOKE_TIMEOUT_S = 180


# ------------------------------------------------------------------------- small helpers

def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean_env(**extra):
    """The environment for child tools: nothing from the Python that runs this script leaks in."""
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")}
    env["UV_NO_PROGRESS"] = "1"
    env.update(extra)
    return env


def find_uv():
    override = os.environ.get("UV")
    if override:
        return [override]
    found = shutil.which("uv")
    if found:
        return [found]
    probe = subprocess.run([sys.executable, "-m", "uv", "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if probe.returncode == 0:
        return [sys.executable, "-m", "uv"]
    raise BuildError("uv is not installed. Install the pinned version with: pip install uv==%s" % UV_VERSION)


def check_uv_version(uv, allow_mismatch):
    out = run(uv + ["--version"], env=clean_env()).strip()
    match = re.match(r"uv (\d+\.\d+\.\d+)", out)
    found = match.group(1) if match else out
    if found != UV_VERSION and not allow_mismatch:
        raise BuildError("uv %s is installed but this build is pinned to uv %s (pip install uv==%s). "
                         "Use --allow-uv-mismatch only for local experiments." % (found, UV_VERSION, UV_VERSION))
    return found


def locked_version(name):
    with open(REPO / "uv.lock", "rb") as handle:
        lock = tomllib.load(handle)
    for package in lock.get("package", []):
        if package["name"] == name:
            return package["version"]
    raise BuildError("%s is not in uv.lock" % name)


def project_version():
    with open(REPO / "pyproject.toml", "rb") as handle:
        return tomllib.load(handle)["project"]["version"]


# --------------------------------------------------------------------- glibc requirements

def parse_glibc_requirement(objdump_output):
    """Highest GLIBC_x.y symbol version named in `objdump -T` output, or None."""
    versions = [(int(m.group(1)), int(m.group(2))) for m in GLIBC_RE.finditer(objdump_output)]
    return max(versions) if versions else None


def is_elf(path):
    try:
        with open(path, "rb") as handle:
            return handle.read(4) == b"\x7fELF"
    except OSError:
        return False


def glibc_report(dist_dir):
    """Map each bundled ELF file to the glibc version it needs (files needing none are omitted)."""
    if shutil.which("objdump") is None:
        raise BuildError("objdump (binutils) is required to check glibc requirements.")
    report = {}
    for path in sorted(Path(dist_dir).rglob("*")):
        if path.is_file() and not path.is_symlink() and is_elf(path):
            result = subprocess.run(["objdump", "-T", str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            need = parse_glibc_requirement(result.stdout)
            if need:
                report[path.relative_to(dist_dir).as_posix()] = need
    return report


def enforce_glibc(report, limit):
    offenders = {name: need for name, need in report.items() if need > limit}
    if offenders:
        lines = ["  glibc %d.%d  %s" % (need + (name,)) for name, need in sorted(offenders.items(), key=lambda kv: kv[1], reverse=True)]
        raise BuildError(
            "These bundled files need glibc newer than %d.%d, so the program would not start on the oldest "
            "supported systems:\n%s\nA library copied from the build machine is the usual cause: add it to "
            "HOST_LIBRARIES in scripts/diamond-mcp.spec." % (limit + ("\n".join(lines),)))
    return max(report.values()) if report else None


# --------------------------------------------------------------------- inventory and SBOM

def native_files(dist_dir):
    """Every bundled native file (libraries, extension modules, the executable) with its SHA-256."""
    found = []
    for path in sorted(Path(dist_dir).rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        name = path.name
        native = name.endswith(NATIVE_SUFFIXES) or ".so." in name or name == EXE_NAME
        if native:
            found.append({"path": path.relative_to(dist_dir).as_posix(), "sha256": sha256_file(path), "size": path.stat().st_size})
    return found


def augment_sbom(sbom, *, python_version, pyinstaller_version, files, properties):
    """Add what `uv export` cannot know: the embedded interpreter, bundled native files, build tools."""
    out = json.loads(json.dumps(sbom))
    metadata = out.setdefault("metadata", {})
    tools = metadata.get("tools")
    if isinstance(tools, list):
        tools.append({"name": "pyinstaller", "version": pyinstaller_version})
    metadata.setdefault("properties", []).extend({"name": "diamond-mcp:" + k, "value": str(v)} for k, v in properties.items())

    python_ref = "cpython@" + python_version
    components = out.setdefault("components", [])
    components.append({
        "type": "library",
        "bom-ref": python_ref,
        "name": "cpython",
        "version": python_version,
        "description": "CPython interpreter embedded in the executable; a python-build-standalone build fetched by uv "
                       "(https://github.com/astral-sh/python-build-standalone)",
        "purl": "pkg:generic/cpython@" + python_version,
        "licenses": [{"license": {"id": "Python-2.0"}}],
    })
    file_refs = []
    for item in files:
        ref = "file:" + item["path"]
        file_refs.append(ref)
        components.append({
            "type": "file",
            "bom-ref": ref,
            "name": item["path"],
            "hashes": [{"alg": "SHA-256", "content": item["sha256"]}],
            "properties": [{"name": "diamond-mcp:size-bytes", "value": str(item["size"])}],
        })

    root = metadata.get("component", {}).get("bom-ref")
    dependencies = out.setdefault("dependencies", [])
    entry = next((d for d in dependencies if d.get("ref") == root), None)
    if entry is None:
        entry = {"ref": root, "dependsOn": []}
        dependencies.append(entry)
    entry.setdefault("dependsOn", []).extend([python_ref] + file_refs)
    dependencies.append({"ref": python_ref, "dependsOn": []})
    dependencies.extend({"ref": ref, "dependsOn": []} for ref in file_refs)
    return out


def distribution_modules(site_packages):
    """Map each installed distribution (normalised name) to the top-level module names it provides."""
    found = {}
    for dist_info in site_packages.glob("*.dist-info"):
        metadata = (dist_info / "METADATA").read_text(encoding="utf-8", errors="replace").splitlines()
        name = next((l.split(":", 1)[1].strip() for l in metadata if l.startswith("Name:")), None)
        if not name:
            continue
        modules = set()
        record = dist_info / "RECORD"
        if record.is_file():
            for line in record.read_text(encoding="utf-8", errors="replace").splitlines():
                first = line.split(",")[0]
                if first.startswith("..") or ".dist-info" in first or "__pycache__" in first:
                    continue
                modules.add(re.sub(r"\.py$", "", first.split("/")[0]))
        found[normalise(name)] = modules
    return found


def frozen_top_level_names(pyz_toc, native_paths):
    """Top-level names of the third-party code in the program: frozen modules plus native files."""
    names = set()
    entries = ast.literal_eval(Path(pyz_toc).read_text(encoding="utf-8"))[1]
    for entry in entries:
        source = entry[1].replace("\\", "/")
        if "site-packages/" in source:
            names.add(re.sub(r"\.py$", "", source.split("site-packages/", 1)[1].split("/")[0]))
    for path in native_paths:
        parts = path.split("/")
        if len(parts) > 2 and parts[0] == "_internal":
            names.add(parts[1])
    return names


def bundled_distributions(site_packages, pyz_toc, native_paths, project):
    """Names of the installed distributions whose code is inside the program (the project itself excluded)."""
    frozen = frozen_top_level_names(pyz_toc, native_paths)
    return {name for name, modules in distribution_modules(site_packages).items() if modules & frozen and name != normalise(project)}


def check_sbom_matches_bundle(locked, bundled):
    """Fail if the program contains a package the SBOM would not list. Returns the locked packages that are absent."""
    unlisted = sorted(bundled - locked)
    if unlisted:
        raise BuildError("These packages are inside the program but are not dependencies in uv.lock, so the SBOM would not "
                         "list them: %s. Exclude them in scripts/diamond-mcp.spec (BUILD_TOOLS) or add them to the "
                         "project's dependencies." % ", ".join(unlisted))
    return sorted(locked - bundled)


def prune_sbom(sbom, absent):
    """Remove locked packages that are not in the program, so scanners do not report code that never ships."""
    out = json.loads(json.dumps(sbom))
    gone = {c["bom-ref"] for c in out.get("components", []) if c.get("type") == "library" and normalise(c["name"]) in set(absent)}
    out["components"] = [c for c in out.get("components", []) if c.get("bom-ref") not in gone]
    out["dependencies"] = [
        {**d, "dependsOn": [r for r in d.get("dependsOn", []) if r not in gone]} for d in out.get("dependencies", []) if d.get("ref") not in gone
    ]
    out.setdefault("metadata", {}).setdefault("properties", []).append(
        {"name": "diamond-mcp:locked-but-not-bundled", "value": ", ".join(sorted(absent)) or "none"})
    return out


def validate_sbom(sbom):
    """Structural checks that catch a broken SBOM before it ships. Returns the component count."""
    if sbom.get("bomFormat") != "CycloneDX" or not sbom.get("specVersion"):
        raise BuildError("The SBOM is not a CycloneDX document.")
    refs = [c["bom-ref"] for c in sbom.get("components", []) if "bom-ref" in c]
    if len(refs) != len(set(refs)):
        raise BuildError("The SBOM has duplicate bom-ref values.")
    known = set(refs) | {sbom.get("metadata", {}).get("component", {}).get("bom-ref")}
    for dependency in sbom.get("dependencies", []):
        for ref in [dependency.get("ref")] + dependency.get("dependsOn", []):
            if ref not in known:
                raise BuildError("The SBOM names a dependency that is not a component: %s" % ref)
    return len(sbom.get("components", []))


# --------------------------------------------------------------------------- build steps

def source_date_epoch():
    """Timestamp for PyInstaller's embedded bytecode: SOURCE_DATE_EPOCH if set, else the commit time."""
    if os.environ.get("SOURCE_DATE_EPOCH"):
        return os.environ["SOURCE_DATE_EPOCH"]
    try:
        return run(["git", "-C", str(REPO), "log", "-1", "--format=%ct"]).strip() or None
    except (BuildError, OSError):
        return None


def build_executable(uv, work, dist):
    extra = {"SOURCE_DATE_EPOCH": source_date_epoch()} if source_date_epoch() else {}
    env = clean_env(UV_PROJECT_ENVIRONMENT=str(work / "venv"), **extra)
    log("Installing CPython %s with uv (no shortcuts or registry entries are created) ..." % PYTHON_VERSION)
    run(uv + ["python", "install", PYTHON_VERSION, "--no-bin", "--no-registry"], env=env)
    log("Building with PyInstaller from the locked environment ...")
    run(uv + ["run", "--frozen", "--no-dev", "--no-editable", "--group", "build", "--python", PYTHON_VERSION,
              "--managed-python", "--no-python-downloads",
              "pyinstaller", "--noconfirm", "--clean", "--distpath", str(dist), "--workpath", str(work / "pyinstaller"),
              str(REPO / "scripts" / "diamond-mcp.spec")], env=env, cwd=str(REPO))
    bundle = dist / "diamond-mcp"
    if not (bundle / EXE_NAME).is_file():
        raise BuildError("PyInstaller did not produce %s" % (bundle / EXE_NAME))
    return bundle, env


def mcp_smoke_test(exe, args=()):
    """Run the program (with `args`, for `python -I -m diamond_mcp`) and speak MCP to it over stdio. Returns the tool names."""
    env = clean_env(DIAMOND_MCP_AUDIT_LOG="off")
    env.pop("DIAMOND_EXE", None)
    command = [str(exe)] + list(args)
    help_text = run(command + ["--help"], env=env)
    if "diamond-mcp" not in help_text:
        raise BuildError("--help did not describe diamond-mcp:\n" + help_text)

    errors = tempfile.TemporaryFile("w+")
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True, env=env)
    watchdog = threading.Timer(SMOKE_TIMEOUT_S, proc.kill)  # a hung program must not hang the build
    watchdog.start()

    def stderr_tail():
        errors.seek(0)
        return errors.read()[-400:]

    def exchange(request_id, method, params=None):
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
        except OSError:
            raise BuildError("The program exited before it read %s. stderr: %s" % (method, stderr_tail()))
        while True:
            line = proc.stdout.readline()
            if not line:
                raise BuildError("The program closed its output during %s (or timed out). stderr: %s" % (method, stderr_tail()))
            try:
                reply = json.loads(line)
            except ValueError:
                raise BuildError("The program wrote something that is not JSON-RPC to stdout during %s: %r" % (method, line[:120]))
            if reply.get("id") == request_id:
                if "error" in reply:
                    raise BuildError("%s returned an error: %s" % (method, reply["error"]))
                return reply["result"]

    try:
        exchange(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "build-smoke", "version": "1"}})
        try:
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            proc.stdin.flush()
        except OSError:
            raise BuildError("The program exited right after initialize. stderr: %s" % stderr_tail())
        names = [tool["name"] for tool in exchange(2, "tools/list")["tools"]]
        if len(names) < MIN_TOOLS or not {"compile_project", "run_tcl", "open_project"} <= set(names):
            raise BuildError("tools/list returned %d tools (%s ...)" % (len(names), ", ".join(names[:5])))
        status = exchange(3, "tools/call", {"name": "get_session_status", "arguments": {"log_lines": 0}})
        if status.get("isError"):
            raise BuildError("get_session_status failed: %s" % status)
    finally:
        watchdog.cancel()
        try:
            proc.stdin.close()
            proc.wait(timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
        errors.close()
    return names


def venv_site_packages(venv):
    python = venv / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")
    return Path(run([str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"], env=clean_env()).strip())


def runtime_package_names(sbom):
    root = sbom.get("metadata", {}).get("component", {}).get("name")
    return {normalise(c["name"]) for c in sbom.get("components", []) if c.get("type") == "library" and c.get("name") not in (root, "cpython")}


def collect_embedded_notices(platform, python_root, out_dir, notices_dir=None):
    """Copy the notices of the libraries compiled into the embedded CPython (OpenSSL, zlib, expat and others).

    The interpreter uv installs carries none of them, so they come from scripts/python_notices/, which
    fetch_python_notices.py fills from the python-build-standalone release. Returns (index lines, problems).
    A BuildError if the interpreter in use is from another release than the notices: its libraries may differ.
    """
    folder = (notices_dir or NOTICES_DIR) / platform
    files = sorted(folder.glob("LICENSE.*.txt")) if folder.is_dir() else []
    if not files:
        return [], ["notices for the libraries inside cpython (nothing in %s; run scripts/fetch_python_notices.py)" % folder]
    release_file = (notices_dir or NOTICES_DIR) / "RELEASE"
    release = release_file.read_text(encoding="utf-8").strip() if release_file.is_file() else ""
    build_file = python_root / "BUILD"
    used = build_file.read_text(encoding="utf-8").strip() if build_file.is_file() else ""
    if used and used != release:
        raise BuildError("The embedded CPython is from python-build-standalone release %s, but scripts/python_notices/ holds the "
                         "notices of release %s. Run scripts/fetch_python_notices.py --release %s and have Legal read the diff."
                         % (used, release or "(unknown)", used))
    target = out_dir / ("cpython-" + PYTHON_VERSION) / "embedded-libraries"
    target.mkdir(parents=True, exist_ok=True)
    index = ["    Libraries compiled into this interpreter (python-build-standalone release %s), notices in embedded-libraries/:"
             % (release or "unknown")]
    for path in files:
        shutil.copyfile(path, target / path.name)
        index.append("        embedded-libraries/" + path.name)
    problems = [] if used else ["the python-build-standalone release of the embedded cpython could not be read (no BUILD file), so "
                                "its match with release %s of the notices is unconfirmed" % release]
    return index, problems


def collect_licences(site_packages, wanted, python_root, out_dir, platform=None):
    """Copy licence files for the shipped packages (plus PyInstaller, whose bootloader is in the output).

    With `platform`, also the notices of the libraries inside the embedded CPython."""
    index = ["Third-party software in this installer, with the licence files found for each.", ""]
    missing = []
    wanted = set(wanted) | {"pyinstaller"}
    for dist_info in sorted(site_packages.glob("*.dist-info")):
        metadata = (dist_info / "METADATA").read_text(encoding="utf-8", errors="replace").splitlines()
        name = next((l.split(":", 1)[1].strip() for l in metadata if l.startswith("Name:")), "")
        version = next((l.split(":", 1)[1].strip() for l in metadata if l.startswith("Version:")), "")
        if normalise(name) not in wanted:
            continue
        stated = next((l.split(":", 1)[1].strip() for l in metadata if l.startswith("License-Expression:")), "")
        copied = []
        for path in sorted(dist_info.rglob("*")):
            relative = path.relative_to(dist_info).as_posix()
            if path.is_file() and LICENSE_NAME_RE.search(relative):
                target = out_dir / ("%s-%s" % (normalise(name), version)) / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                copied.append(relative)
        index.append("%s %s  [%s]" % (name, version, stated or "licence field not stated"))
        if copied:
            index.extend("    " + c for c in copied)
        else:
            index.append("    (no licence file in the package)")
            missing.append("%s %s" % (name, version))
    candidates = [python_root / "LICENSE.txt", python_root / "lib" / ("python" + PYTHON_VERSION.rsplit(".", 1)[0]) / "LICENSE.txt"]
    licence = next((c for c in candidates if c.is_file()), None)
    index.append("cpython %s  [PSF-2.0]" % PYTHON_VERSION)
    if licence:
        target = out_dir / ("cpython-" + PYTHON_VERSION) / "LICENSE.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(licence, target)
        index.append("    LICENSE.txt")
    else:
        index.append("    (no licence file found)")
        missing.append("cpython " + PYTHON_VERSION)
    if platform:
        lines, problems = collect_embedded_notices(platform, python_root, out_dir)
        index.extend(lines)
        missing.extend(problems)
    return "\n".join(index) + "\n", missing


# ------------------------------------------------------------------------- legal notes

BOOTLOADER_EXCEPTION = "unlimited permission to link or embed compiled bootloader"
HOOKS_APACHE = "Apache License"


def legal_review_text(*, version, platform, pyinstaller_version, licence_relpath, licence_text, product_licence_included=True,
                      embedded_notices=()):
    """The plain-text list of items for Legal, built from the licence file that ships in the zip.

    It states what was found, not a legal conclusion. The summary of PyInstaller's terms is only
    written when the shipped file still contains the wording it relies on; otherwise it says so.
    """
    lines = [
        "Items for Legal review - %s %s (%s)" % (PACKAGE, version, platform),
        "",
        "Written by scripts/build_binary.py. These are facts found in this build, not legal conclusions.",
        "",
        "1. PyInstaller's bootloader is embedded in the program",
        "",
        "   What is in the zip: the program 'diamond-mcp' IS PyInstaller's compiled bootloader, and",
        "   PyInstaller's loader modules (pyimod01_archive, pyimod02_importers, pyimod03_ctypes) are",
        "   embedded in it. The PyInstaller build tool itself is not shipped.",
        "",
        "   Licence file shipped with this zip, unmodified: %s (PyInstaller %s)." % (licence_relpath or "NOT FOUND", pyinstaller_version),
    ]
    if licence_text and BOOTLOADER_EXCEPTION in licence_text:
        lines += [
            "   It says PyInstaller is GPL-2.0-or-later, with a \"Bootloader Exception\" giving \"unlimited",
            "   permission to link or embed compiled bootloader and related files into combinations with",
            "   other programs, and to distribute those combinations without any restriction coming from",
            "   the use of those files\". " + ("Its run-time hooks are Apache-2.0." if HOOKS_APACHE in licence_text else
                                           "(The wording about Apache-2.0 run-time hooks was not found.)"),
        ]
    else:
        lines += [
            "   WARNING: the Bootloader Exception wording this note relies on was NOT found in that file.",
            "   PyInstaller's terms may have changed. Legal must read the file before this is distributed.",
        ]
    lines += [
        "",
        "   Question for Legal: does the exception suffice for distribution under the AI Solutions",
        "   Software License Agreement, and must a notice accompany the program? A copy of the licence",
        "   file is shipped as a precaution.",
        "",
        "   Applies to: this standalone zip only. The wheels zip contains no PyInstaller code.",
        "",
    ]
    if product_licence_included:
        lines += [
            "2. The product licence text in this zip was copied, not written for this product",
            "",
            "   LICENSE at the top of the zip is the AI Solutions Software License Agreement text from the",
            "   cortex-bridge repository, copied byte for byte (its SHA-256 is recorded in BUILDINFO as",
            "   license_sha256). Its own notice allows free distribution of the text if the copyright notice",
            "   is kept, and forbids changing its substantive terms.",
            "",
            "   Question for Legal: is this the approved licence text for lattice-diamond-mcp?",
            "",
        ]
    else:
        lines += [
            "2. No product licence text is in this zip",
            "",
            "   The source repository has no LICENSE file, so none was bundled. The approved licence text",
            "   must be added before release.",
            "",
        ]
    lines += [
        "3. Libraries compiled into the embedded Python interpreter",
        "",
        "   The program embeds CPython %s from python-build-standalone (PSF-2.0; its LICENSE.txt is in" % PYTHON_VERSION,
        "   THIRD_PARTY_LICENSES/cpython-%s/). That interpreter has other libraries compiled in or" % PYTHON_VERSION,
        "   shipped beside it (OpenSSL, zlib, expat and others). The interpreter that uv installs carries no notices",
        "   for them, so the build copies the ones that python-build-standalone publishes for the same release",
        "   (scripts/python_notices/, recorded with their SHA-256 in SOURCES.txt there), unmodified.",
        "",
    ]
    if embedded_notices:
        lines.append("   Shipped in this zip, under THIRD_PARTY_LICENSES/cpython-%s/embedded-libraries/:" % PYTHON_VERSION)
        lines += ["     " + name for name in embedded_notices]
        lines += [
            "",
            "   Which libraries are in the program was read from symbols and version strings in the interpreter's",
            "   library (Linux) or from the DLLs beside python311.dll (Windows). Tcl/Tk, X11 and Berkeley DB are not in",
            "   the program, so their notices are not shipped. The C library (glibc, or the Windows system libraries)",
            "   is the customer's own and is not distributed.",
            "",
            "   Question for Legal: are these notices, and the PSF-2.0 and PyInstaller texts above, enough for the",
            "   product's distribution terms?",
            "",
        ]
    else:
        lines += [
            "   WARNING: no notices for these libraries were found for this platform, so none are shipped.",
            "   Run scripts/fetch_python_notices.py and rebuild before this is distributed.",
            "",
        ]
    return "\n".join(lines)


# --------------------------------------------------------------------- packaging and checks

def write_zip(stage, outdir, name, root):
    target = outdir / (name + ".zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                archive.write(path, "%s/%s" % (root, path.relative_to(stage).as_posix()))
    digest = sha256_file(target)
    # Bytes, not text: on Windows text mode would write "\r\n", and `sha256sum -c` would then look for
    # a file whose name ends in a carriage return.
    (outdir / (name + ".zip.sha256")).write_bytes(("%s  %s\n" % (digest, target.name)).encode("utf-8"))
    return target, digest


def verify_zip(zip_path):
    """Unpack the finished zip, install it with its own install.py, and run the installed program."""
    log("Verifying: installing the zip into a temporary folder ...")
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(tmp)
            if not IS_WINDOWS:
                # extractall drops permission bits; restore them from the archive, as `unzip` does.
                for info in archive.infolist():
                    perms = (info.external_attr >> 16) & 0o777
                    if perms and not info.is_dir():
                        os.chmod(Path(tmp) / info.filename, perms)
        bundle = next(p for p in Path(tmp).iterdir() if p.is_dir())
        if not (bundle / "diamond-mcp" / EXE_NAME).is_file():
            raise BuildError("The zip does not contain diamond-mcp/%s" % EXE_NAME)
        target = Path(tmp) / "installed"
        out = run([sys.executable, str(bundle / "install.py"), "--no-register", "--install-dir", str(target)], env=clean_env())
        if "OK: diamond-mcp" not in out:
            raise BuildError("The temporary install did not report success:\n" + out)
        mcp_smoke_test(target / EXE_NAME)
        log("  install verified")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the standalone installer zip for lattice-diamond-mcp.")
    parser.add_argument("--outdir", default=str(REPO / "dist"), help="Output directory (default: dist).")
    parser.add_argument("--max-glibc", default="%d.%d" % MAX_GLIBC, help="Highest glibc a bundled file may need (Linux; default %(default)s).")
    parser.add_argument("--allow-uv-mismatch", action="store_true", help="Accept a uv version other than the pinned one (local experiments only).")
    parser.add_argument("--skip-verify", action="store_true", help="Do not test-install the finished zip (not for releases).")
    parser.add_argument("--smoke-test", metavar="PROGRAM",
                        help="Only talk MCP to an existing diamond-mcp program (for example an installed one) and exit.")
    parser.add_argument("--smoke-test-args", metavar="ARGS", default="",
                        help='Arguments to start PROGRAM with, as one string; write it with an equals sign, for example '
                             '--smoke-test-args="-I -m diamond_mcp" to test a Python environment.')
    args = parser.parse_args(argv)

    if args.smoke_test:
        try:
            log("MCP smoke test passed (%d tools)" % len(mcp_smoke_test(Path(args.smoke_test), shlex.split(args.smoke_test_args))))
        except BuildError as exc:
            print("\nSMOKE TEST FAILED: %s" % exc, file=sys.stderr)
            return 1
        return 0
    limit = tuple(int(part) for part in args.max_glibc.split("."))

    try:
        uv = find_uv()
        uv_version = check_uv_version(uv, args.allow_uv_mismatch)
        run(uv + ["lock", "--check"], env=clean_env(), cwd=str(REPO))
        pyinstaller_version = locked_version("pyinstaller")
        version, platform = project_version(), platform_name()
        name = "%s-%s-%s" % (PACKAGE, version, platform)
        outdir = Path(args.outdir).resolve()
        outdir.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            dist = Path(tmp) / "dist"
            stage = Path(tmp) / "stage"
            work.mkdir(), dist.mkdir(), stage.mkdir()

            bundle, env = build_executable(uv, work, dist)

            floor = None
            if sys.platform.startswith("linux"):
                floor = enforce_glibc(glibc_report(bundle), limit)
                log("glibc check passed: highest requirement %s (limit %d.%d)" % ("%d.%d" % floor if floor else "none", limit[0], limit[1]))

            tools = mcp_smoke_test(bundle / EXE_NAME)
            log("MCP smoke test passed (%d tools)" % len(tools))

            files = native_files(bundle)
            site_packages = venv_site_packages(work / "venv")
            bundled = bundled_distributions(site_packages, work / "pyinstaller" / "diamond-mcp" / "PYZ-00.toc",
                                            [f["path"] for f in files], PACKAGE)
            lock_hash = sha256_file(REPO / "uv.lock")
            commit = git_commit()
            sbom_path = Path(tmp) / "sbom.json"
            run(uv + ["export", "--frozen", "--no-dev", "--format", "cyclonedx1.5", "--preview-features", "sbom-export",
                      "--output-file", str(sbom_path)], env=clean_env(), cwd=str(REPO))
            exported = json.loads(sbom_path.read_text(encoding="utf-8"))
            locked = runtime_package_names(exported)
            absent = check_sbom_matches_bundle(locked, bundled)
            if absent:
                log("Locked but not in the program (left out of the SBOM): %s" % ", ".join(absent))
            sbom = augment_sbom(
                prune_sbom(exported, absent),
                python_version=PYTHON_VERSION, pyinstaller_version=pyinstaller_version, files=files,
                properties={"platform": platform, "git": commit, "uv-lock-sha256": lock_hash,
                            "max-glibc": "%d.%d" % floor if floor else "n/a"},
            )
            count = validate_sbom(sbom)
            log("SBOM written: %d components (%d bundled native files)" % (count, len(files)))

            python_root = Path(run([str(work / "venv" / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")), "-c",
                                    "import sys; print(sys.base_prefix)"], env=clean_env()).strip())
            licence_index, missing = collect_licences(site_packages, bundled, python_root, stage / "THIRD_PARTY_LICENSES", platform=platform)

            licence_present = (REPO / "LICENSE").is_file()
            licence_file = next(iter(sorted((stage / "THIRD_PARTY_LICENSES").glob("pyinstaller-*/licenses/COPYING.txt"))), None)
            legal = legal_review_text(
                version=version, platform=platform, pyinstaller_version=pyinstaller_version,
                licence_relpath=licence_file.relative_to(stage).as_posix() if licence_file else None,
                licence_text=licence_file.read_text(encoding="utf-8", errors="replace") if licence_file else "",
                product_licence_included=licence_present,
                embedded_notices=sorted(p.name for p in (stage / "THIRD_PARTY_LICENSES" / ("cpython-" + PYTHON_VERSION) / "embedded-libraries").glob("LICENSE.*.txt")))
            (stage / "LEGAL_REVIEW.txt").write_text(legal, encoding="utf-8")
            if licence_present:
                shutil.copyfile(REPO / "LICENSE", stage / "LICENSE")
            licence_index = "See LEGAL_REVIEW.txt for items that need a decision from Legal.\n\n" + licence_index

            shutil.copytree(bundle, stage / "diamond-mcp", symlinks=True)
            shutil.copy(REPO / "scripts" / "install.py", stage / "install.py")
            shutil.copy(REPO / "docs" / "Lattice_Diamond_MCP_Server_UserGuide.md", stage / "Lattice_Diamond_MCP_Server_UserGuide.md")
            (stage / "THIRD_PARTY_LICENSES.txt").write_text(licence_index, encoding="utf-8")
            (stage / "sbom.cdx.json").write_text(json.dumps(sbom, indent=2) + "\n", encoding="utf-8")
            (outdir / (name + ".cdx.json")).write_text(json.dumps(sbom, indent=2) + "\n", encoding="utf-8")
            info = {
                "name": PACKAGE, "version": version, "kind": "binary", "platform": platform,
                "python": PYTHON_VERSION, "min_glibc": "%d.%d" % floor if floor else None,
                "uv": uv_version, "pyinstaller": pyinstaller_version, "uv_lock_sha256": lock_hash, "git": commit,
                "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "native_files": len(files),
                "license_sha256": sha256_file(REPO / "LICENSE") if licence_present else None,
            }
            (stage / "BUILDINFO").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
            write_manifest(stage)

            target, digest = write_zip(stage, outdir, name, "%s-%s" % (PACKAGE, version))
            if not args.skip_verify:
                verify_zip(target)
    except BuildError as exc:
        print("\nBUILD FAILED: %s" % exc, file=sys.stderr)
        return 1

    log()
    log("Built %s" % target)
    log("  size:    %.1f MB" % (target.stat().st_size / 1e6))
    log("  sha256:  %s" % digest)
    log("  platform: %s   embedded CPython: %s   glibc floor: %s   uv: %s   PyInstaller: %s" % (
        platform, PYTHON_VERSION, "%d.%d" % floor if floor else "n/a", uv_version, pyinstaller_version))
    if missing:
        log()
        log("WARNING: no licence file was found for: " + ", ".join(missing) + ". Check before release.")
    log()
    log("FOR LEGAL: LEGAL_REVIEW.txt inside the zip lists the items that need a Legal decision: PyInstaller's")
    log("bootloader is embedded in the program (its licence file is included), and %s." % (
        "the product LICENSE is a copy of the cortex-bridge text" if (REPO / "LICENSE").is_file() else "the product has no LICENSE text"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
