#!/usr/bin/env python3
# Copyright(c) 2026 Lattice Semiconductor Corporation. All rights reserved. DO NOT DISTRIBUTE.
# Subject to the terms of the AI Solutions Software License Agreement
"""
Build the offline installer zip for lattice-diamond-mcp.

    python scripts/build_installer.py [--outdir dist]

The zip is built for the Python and operating system that run this script, and
contains everything the installer needs, so the target machine needs no network:

    install.py              the installer (standard library only)
    BUILDINFO               version, Python version and tag, platform, glibc floor, git commit
    requirements.txt        every wheel by exact version and SHA-256, installed with --require-hashes
    wheels/                 our wheel and every dependency wheel
    LICENSE                 the product licence text, when the repository has one
    THIRD_PARTY_LICENSES*   licence files taken from the dependency wheels
    Lattice_Diamond_MCP_Server_UserGuide.md

Build one zip per Python version and per operating system, each on that system
(pip evaluates dependency markers for the machine it runs on, so a Windows zip
built on Linux would silently miss Windows-only dependencies).

The versions come from uv.lock, not from whatever the package index offers on the day:
pip is given the locked versions as constraints, and every wheel it downloads must have the
SHA-256 that uv.lock records for it, or the build stops and writes nothing. The installer
then installs from requirements.txt with --require-hashes, so a wheel that was altered or
replaced after the build is refused. (Whoever can replace requirements.txt as well as a
wheel defeats this; only a signature on the zip closes that.)

Before the zip is written, the bundle is installed into a throwaway environment
by running its own install.py, so a bundle that cannot install is never produced.

pip honours PIP_INDEX_URL, PIP_FIND_LINKS, proxy settings and pip.conf when it
downloads dependencies.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

try:
    import tomllib
except ImportError:  # Python 3.10 and older
    sys.exit("build_installer.py needs Python 3.11 or later (it reads pyproject.toml with tomllib).")

REPO = Path(__file__).resolve().parents[1]
PACKAGE = "lattice-diamond-mcp"
LICENSE_NAME_RE = re.compile(r"(^|/)(licen[sc]e|copying|notice|authors)[^/]*$", re.IGNORECASE)
MANYLINUX_RE = re.compile(r"manylinux_(\d+)_(\d+)")
LEGACY_MANYLINUX = {"manylinux1": (2, 5), "manylinux2010": (2, 12), "manylinux2014": (2, 17)}


class BuildError(Exception):
    pass


def log(message=""):
    print(message, flush=True)


def run(command, **kwargs):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kwargs)
    if result.returncode != 0:
        raise BuildError("Command failed (exit %d): %s\n%s" % (result.returncode, " ".join(map(str, command)), result.stdout.strip()))
    return result.stdout


# --------------------------------------------------------------- wheel helpers

def parse_wheel_name(filename):
    """Return (distribution, version, [platform tags]) from a wheel file name."""
    stem = filename[:-4] if filename.endswith(".whl") else filename
    parts = stem.split("-")
    if len(parts) < 5:
        raise BuildError("Unexpected wheel file name: " + filename)
    return parts[0], parts[1], parts[-1].split(".")


def normalise(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def glibc_floor(platform_tags):
    """Lowest glibc a wheel with these platform tags can run on, as (major, minor), or None."""
    floors = []
    for tag in platform_tags:
        match = MANYLINUX_RE.search(tag)
        if match:
            floors.append((int(match.group(1)), int(match.group(2))))
        elif tag.split("_")[0] in LEGACY_MANYLINUX:
            floors.append(LEGACY_MANYLINUX[tag.split("_")[0]])
    return min(floors) if floors else None


def bundle_glibc_floor(wheel_names):
    """The glibc level the whole bundle needs: the highest of the per-wheel minimums."""
    floors = [f for f in (glibc_floor(parse_wheel_name(n)[2]) for n in wheel_names) if f]
    return max(floors) if floors else None


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_lock(lock_path):
    """Read uv.lock. Returns {(normalised name, version): {wheel file name: sha256}} for every locked package."""
    try:
        with open(lock_path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise BuildError("Cannot read %s (%s). The wheels are checked against it, so the build cannot go on." % (lock_path, exc))
    locked = {}
    for package in data.get("package", []):
        source = package.get("source", {})
        if "version" not in package or "editable" in source or "virtual" in source:
            continue  # the project itself
        hashes = {}
        for wheel in package.get("wheels", []):
            name = unquote(wheel["url"].rsplit("/", 1)[-1])
            hashes[name] = wheel["hash"].split(":", 1)[1]
        locked[(normalise(package["name"]), package["version"])] = hashes
    return locked


def lock_constraints_text(locked):
    """pip constraints that pin every package to the version uv.lock records."""
    return "".join("%s==%s\n" % key for key in sorted(locked))


def check_wheels_against_lock(wheels_dir, locked, own_prefix):
    """BuildError unless every dependency wheel is a file uv.lock lists, with the SHA-256 it records."""
    problems = []
    for wheel in sorted(wheels_dir.glob("*.whl")):
        if wheel.name.startswith(own_prefix):
            continue
        dist, version, _ = parse_wheel_name(wheel.name)
        recorded = locked.get((normalise(dist), version))
        if recorded is None:
            problems.append("%s: %s %s is not in uv.lock" % (wheel.name, normalise(dist), version))
        elif wheel.name not in recorded:
            problems.append("%s: uv.lock lists no wheel of that name for %s %s" % (wheel.name, normalise(dist), version))
        elif recorded[wheel.name] != sha256_file(wheel):
            problems.append("%s: its SHA-256 differs from the one in uv.lock" % wheel.name)
    if problems:
        raise BuildError("Downloaded wheels that do not match uv.lock:\n  " + "\n  ".join(problems) +
                         "\nRun `uv lock` if a dependency changed on purpose; otherwise treat this as a possible tampering.")


def requirements_text(wheels_dir):
    """requirements.txt: each wheel in the folder by exact version and SHA-256, for pip --require-hashes."""
    entries = sorted(
        ("%s==%s" % (normalise(parse_wheel_name(w.name)[0]), parse_wheel_name(w.name)[1]), sha256_file(w))
        for w in wheels_dir.glob("*.whl")
    )
    return "".join("%s \\\n    --hash=sha256:%s\n" % entry for entry in entries)


def extract_licences(wheels_dir, out_dir, own_wheel_prefix):
    """Copy licence files from each dependency wheel. Returns (index_text, packages_without_licence)."""
    index_lines = ["Third-party packages in this installer, with the licence files found in each wheel.", ""]
    missing = []
    for wheel in sorted(wheels_dir.glob("*.whl")):
        if wheel.name.startswith(own_wheel_prefix):
            continue
        dist, version, _ = parse_wheel_name(wheel.name)
        folder = out_dir / ("%s-%s" % (normalise(dist), version))
        copied, licence_field = [], ""
        with zipfile.ZipFile(wheel) as archive:
            for member in archive.namelist():
                if ".dist-info/" in member and LICENSE_NAME_RE.search(member.split(".dist-info/", 1)[1]):
                    if member.endswith("/"):
                        continue
                    target = folder / member.split(".dist-info/", 1)[1]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(member))
                    copied.append(member.split(".dist-info/", 1)[1])
                if member.endswith(".dist-info/METADATA") and not licence_field:
                    for line in archive.read(member).decode("utf-8", "replace").splitlines():
                        if line.startswith("License-Expression:") or (line.startswith("License:") and len(line) < 120):
                            licence_field = line.split(":", 1)[1].strip()
                            break
                        if not line.strip():
                            break
        index_lines.append("%s %s  [%s]" % (dist, version, licence_field or "licence field not stated"))
        if copied:
            index_lines.extend("    " + name for name in copied)
        else:
            index_lines.append("    (no licence file in the wheel)")
            missing.append("%s %s" % (dist, version))
    return "\n".join(index_lines) + "\n", missing


# ----------------------------------------------------------------------- build

def project_version():
    with open(REPO / "pyproject.toml", "rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def git_commit():
    try:
        commit = run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"]).strip()
        dirty = run(["git", "-C", str(REPO), "status", "--porcelain", "--", "src", "pyproject.toml", "scripts"]).strip()
        return commit + ("+uncommitted-changes" if dirty else "")
    except (BuildError, OSError):
        return "unknown"


def platform_name():
    if sys.platform == "win32":
        return "windows-" + ("x86_64" if sys.maxsize > 2**32 else "x86")
    if sys.platform.startswith("linux"):
        import platform
        return "linux-" + platform.machine()
    raise BuildError("Unsupported build platform: " + sys.platform)


def build_bundle(stage, python_tag):
    wheels = stage / "wheels"
    wheels.mkdir(parents=True)
    version = project_version()

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
    own = next(wheels.glob("*.whl"))

    lock_path = REPO / "uv.lock"
    locked = load_lock(lock_path)
    log("Downloading dependency wheels (binary wheels only, for this Python and operating system, at the versions in uv.lock) ...")
    with tempfile.TemporaryDirectory() as tmp:
        constraints = Path(tmp) / "locked.txt"
        constraints.write_text(lock_constraints_text(locked), encoding="utf-8")
        run([sys.executable, "-m", "pip", "download", "--only-binary=:all:", "--dest", str(wheels),
             "--disable-pip-version-check", "-c", str(constraints), str(own)])
    own_prefix = own.name.split("-")[0] + "-"
    check_wheels_against_lock(wheels, locked, own_prefix)
    log("  every dependency wheel matches the SHA-256 in uv.lock")

    names = sorted(p.name for p in wheels.glob("*.whl"))
    (stage / "requirements.txt").write_text(requirements_text(wheels), encoding="utf-8")

    index_text, missing = extract_licences(wheels, stage / "THIRD_PARTY_LICENSES", own_prefix)
    (stage / "THIRD_PARTY_LICENSES.txt").write_text(index_text, encoding="utf-8")

    floor = bundle_glibc_floor(names) if sys.platform.startswith("linux") else None
    info = {
        "name": PACKAGE,
        "version": version,
        "python": "%d.%d" % sys.version_info[:2],
        "python_tag": python_tag,
        "platform": platform_name(),
        "min_glibc": "%d.%d" % floor if floor else None,
        "git": git_commit(),
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "wheel_count": len(names),
        "uv_lock_sha256": sha256_file(lock_path),
    }
    (stage / "BUILDINFO").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")

    shutil.copy(REPO / "scripts" / "install.py", stage / "install.py")
    if (REPO / "LICENSE").is_file():
        shutil.copy(REPO / "LICENSE", stage / "LICENSE")
    guide = REPO / "docs" / "Lattice_Diamond_MCP_Server_UserGuide.md"
    shutil.copy(guide, stage / guide.name)
    return info, missing


def verify_bundle(stage):
    """Install the bundle into a throwaway environment, as a user would, and check it."""
    log("Verifying: installing the bundle into a throwaway environment ...")
    with tempfile.TemporaryDirectory() as tmp:
        venv = Path(tmp) / "venv"
        env = {k: v for k, v in os.environ.items() if k not in ("PIP_FIND_LINKS", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL")}
        out = run([sys.executable, str(stage / "install.py"), "--no-register", "--venv-dir", str(venv),
                   "--python", sys.executable], env=env)
        if "OK: lattice-diamond-mcp" not in out:
            raise BuildError("The throwaway install did not report success:\n" + out)
        log("  install verified")


def write_zip(stage, outdir, info):
    outdir.mkdir(parents=True, exist_ok=True)
    name = "%s-%s-%s-%s.zip" % (PACKAGE, info["version"], info["python_tag"], info["platform"])
    target = outdir / name
    root = "%s-%s" % (PACKAGE, info["version"])
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                archive.write(path, "%s/%s" % (root, path.relative_to(stage).as_posix()))
    digest = sha256_file(target)
    # Bytes, not text: on Windows text mode would write "\r\n", and `sha256sum -c` would then look for
    # a file whose name ends in a carriage return.
    (outdir / (name + ".sha256")).write_bytes(("%s  %s\n" % (digest, name)).encode("utf-8"))
    return target, digest


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the offline installer zip for lattice-diamond-mcp.")
    parser.add_argument("--outdir", default=str(REPO / "dist"), help="Output directory (default: dist).")
    parser.add_argument("--skip-verify", action="store_true", help="Do not test-install the bundle (not for releases).")
    args = parser.parse_args(argv)

    python_tag = "cp%d%d" % sys.version_info[:2]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "bundle"
            stage.mkdir()
            info, missing = build_bundle(stage, python_tag)
            if not args.skip_verify:
                verify_bundle(stage)
            target, digest = write_zip(stage, Path(args.outdir), info)
    except BuildError as exc:
        print("\nBUILD FAILED: %s" % exc, file=sys.stderr)
        return 1

    log()
    log("Built %s" % target)
    log("  size:    %.1f MB" % (target.stat().st_size / 1e6))
    log("  sha256:  %s" % digest)
    log("  python:  %s   platform: %s   glibc floor: %s   wheels: %d" % (
        info["python_tag"], info["platform"], info["min_glibc"] or "n/a", info["wheel_count"]))
    if missing:
        log()
        log("WARNING: no licence file was found in these wheels; check them before release:")
        for item in missing:
            log("  " + item)
    log()
    if (REPO / "LICENSE").is_file():
        log("LICENSE (a byte-for-byte copy of the cortex-bridge text) is in the zip; Legal should confirm it is the approved text.")
    else:
        log("Not included: a LICENSE file. This repository has none; add the approved licence text before release.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
