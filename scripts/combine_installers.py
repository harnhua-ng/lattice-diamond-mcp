#!/usr/bin/env python3
# Copyright(c) 2026 Lattice Semiconductor Corporation. All rights reserved. DO NOT DISTRIBUTE.
# Subject to the terms of the AI Solutions Software License Agreement
"""
Combine the per-operating-system installer zips into one zip per kind.

    python scripts/combine_installers.py --kind embedded --outdir out  lattice-diamond-mcp-*-embedded-windows-*.zip  lattice-diamond-mcp-*-embedded-linux-*.zip
    python scripts/combine_installers.py --kind wheels     --outdir out  <wheels zips of both systems, one per Python version>

Each input is a zip written by build_embedded.py (a bundled Python with the packages and no program of ours:
python.org's for Windows, python-build-standalone's for Linux; two of them make the "embedded" combined package),
build_binary.py (the standalone PyInstaller program; with one embedded zip it makes a "hybrid") or build_installer.py
(wheels). The result:

    lattice-diamond-mcp-<version>/
        install.py              one installer for all builds; it picks the folder that fits the machine
        PACKAGES.txt            which folder is which build
        windows-x86_64/         a complete single-platform bundle, as it was built (standalone)
        linux-x86_64/
        windows-x86_64-cp311/   one folder per operating system AND Python version (wheels)
        linux-x86_64-cp311/

Nothing inside a build is changed (except that its MANIFEST.sha256 loses the line for install.py, which is
the single one at the top), so its BUILDINFO, licence texts and (standalone) SBOM still describe
exactly the files next to them. The inputs must all be the same kind, version and git commit, each
operating system (and Python version) must appear once, and every install.py must be identical;
otherwise nothing is written. File permissions are kept, so the Linux program stays executable.

Uses only the standard library (Python 3.8 or later).
"""

import argparse
import hashlib
import json
import posixpath
import re
import sys
import zipfile
from pathlib import Path

PACKAGE = "lattice-diamond-mcp"
OS_ORDER = ("windows", "linux")
STANDALONE_KINDS = ("binary", "embedded")   # one folder per operating system, no Python needed on the machine


class CombineError(Exception):
    pass


def log(message=""):
    print(message, flush=True)


def read_bundle(path):
    """Return (root folder name, BUILDINFO dict, install.py bytes) of one per-platform zip."""
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise CombineError("%s is not a readable zip (%s)." % (path, exc))
    with archive:
        roots = {name.split("/", 1)[0] for name in archive.namelist() if name.strip("/")}
        if len(roots) != 1:
            raise CombineError("%s should hold exactly one top-level folder, found: %s" % (path, ", ".join(sorted(roots)) or "nothing"))
        root = roots.pop()
        try:
            info = json.loads(archive.read(root + "/BUILDINFO").decode("utf-8"))
            installer = archive.read(root + "/install.py")
        except KeyError as exc:
            raise CombineError("%s has no %s; it is not an installer zip from this project." % (path, exc.args[0].split("/", 1)[-1]))
        except ValueError as exc:
            raise CombineError("%s: BUILDINFO is not valid JSON (%s)." % (path, exc))
    return root, info, installer


def folder_name(info):
    """The name of a build's folder inside the combined zip."""
    platform = info.get("platform")
    if not platform:
        raise CombineError("A BUILDINFO has no platform.")
    if info.get("kind") in STANDALONE_KINDS:
        return platform
    if not info.get("python_tag"):
        raise CombineError("The wheels build for %s has no python_tag in BUILDINFO." % platform)
    return "%s-%s" % (platform, info["python_tag"])


def check_compatible(bundles):
    """CombineError unless the builds belong together. `bundles` is a list of (path, root, info, installer)."""
    first_path, _, first, first_installer = bundles[0]
    for path, _, info, installer in bundles:
        for field in ("kind", "version", "git"):
            if field == "kind" and info.get("kind") in STANDALONE_KINDS and first.get("kind") in STANDALONE_KINDS:
                continue  # a standalone Linux program and a Windows package with its own Python go together
            if info.get(field) != first.get(field):
                raise CombineError("%s and %s differ in %s (%r and %r); combine builds of one commit only."
                                   % (Path(first_path).name, Path(path).name, field, first.get(field), info.get(field)))
        if installer != first_installer:
            hint = ""
            if installer.replace(b"\r\n", b"\n") == first_installer.replace(b"\r\n", b"\n"):
                hint = (" They differ only in line endings (CRLF against LF): one build was made from a checkout that converted"
                        " them, so check that .gitattributes is in the commit that was built.")
            raise CombineError("%s and %s carry different install.py files (SHA-256 %s and %s).%s" % (
                Path(first_path).name, Path(path).name, hashlib.sha256(first_installer).hexdigest()[:12],
                hashlib.sha256(installer).hexdigest()[:12], hint))
    seen = {}
    for path, _, info, _ in bundles:
        name = folder_name(info)
        if name in seen:
            raise CombineError("%s and %s are both the build for %s." % (Path(seen[name]).name, Path(path).name, name))
        seen[name] = path
    operating_systems = {folder_name(info).split("-", 1)[0] for _, _, info, _ in bundles}
    missing = [name for name in OS_ORDER if name not in operating_systems]
    if missing:
        raise CombineError("Nothing for %s among the inputs; a combined zip needs both Windows and Linux builds." % " or ".join(missing))
    if first.get("git", "").endswith("+uncommitted-changes"):
        log("WARNING: the builds were made from a tree with uncommitted changes (%s)." % first["git"])


def output_name(bundles, label=""):
    infos = [info for _, _, info, _ in bundles]
    kind = "standalone" if infos[0].get("kind") in STANDALONE_KINDS else "wheels"
    if infos[0].get("kind") in STANDALONE_KINDS and all(i.get("kind") == "embedded" for i in infos):
        kind = "embedded"   # a bundled Python with the packages for each operating system, no program of ours
    elif any(i.get("kind") == "embedded" for i in infos):
        kind = "hybrid"     # an embedded Python for one system and the standalone (PyInstaller) program for the other
    operating_systems = [name for name in OS_ORDER if any(i["platform"].startswith(name) for i in infos)]
    parts = [PACKAGE, infos[0]["version"] + label, kind, "-".join(operating_systems)]
    if kind == "wheels":
        parts.append("-".join(sorted({i["python_tag"] for i in infos})))
    else:
        architectures = {i["platform"].split("-", 1)[1] for i in infos}
        if len(architectures) == 1:
            parts.append(architectures.pop())
    return "-".join(parts)


def index_text(bundles, name):
    lines = ["%s %s, combined package (%s)" % (PACKAGE, bundles[0][2]["version"], name), "",
             "Install from this folder with:  python install.py", "",
             "install.py chooses the folder below that fits this computer and says which one it used.",
             "Each folder is a complete build exactly as it was produced.", ""]
    for _, _, info, _ in sorted(bundles, key=lambda b: folder_name(b[2])):
        detail = ["platform " + info["platform"]]
        if info.get("kind") == "embedded":
            source = "python.org" if info["platform"].startswith("windows") else "python-build-standalone"
            detail.append("%s Python %s with the packages; no program of ours" % (source, info.get("python", "?")))
        elif info.get("kind") != "binary":
            detail.append("Python " + info.get("python", "?"))
        if info.get("min_glibc"):
            detail.append("glibc %s or later" % info["min_glibc"])
        detail.append("built from " + str(info.get("git", "unknown")))
        lines.append("  %-26s %s" % (folder_name(info) + "/", ", ".join(detail)))
    return "\n".join(lines) + "\n"


def write_combined(bundles, outdir, label=""):
    outdir.mkdir(parents=True, exist_ok=True)
    name = output_name(bundles, label)
    target = outdir / (name + ".zip")
    root = "%s-%s%s" % (PACKAGE, bundles[0][2]["version"], label)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as out:
        installer_info = zipfile.ZipInfo(root + "/install.py", date_time=(1980, 1, 1, 0, 0, 0))
        installer_info.external_attr = (0o755 << 16)
        installer_info.compress_type = zipfile.ZIP_DEFLATED
        out.writestr(installer_info, bundles[0][3])
        index = zipfile.ZipInfo(root + "/PACKAGES.txt", date_time=(1980, 1, 1, 0, 0, 0))
        index.external_attr = (0o644 << 16)
        index.compress_type = zipfile.ZIP_DEFLATED
        out.writestr(index, index_text(bundles, name))
        for path, source_root, info, _ in sorted(bundles, key=lambda b: folder_name(b[2])):
            folder = folder_name(info)
            with zipfile.ZipFile(path) as source:
                for member in source.infolist():
                    relative = member.filename[len(source_root) + 1:]
                    if not relative or relative == "install.py":
                        continue
                    copy = zipfile.ZipInfo(posixpath.join(root, folder, relative), date_time=member.date_time)
                    copy.external_attr = member.external_attr
                    copy.compress_type = member.compress_type
                    if member.is_dir():
                        out.writestr(copy, b"")
                    elif relative == "MANIFEST.sha256":
                        # The one change to a build: its install.py is the single one at the top of this zip, so the
                        # manifest of the folder must not list a file the folder no longer holds.
                        text = source.read(member).decode("utf-8")
                        kept = [line for line in text.splitlines() if not line.endswith("  install.py")]
                        out.writestr(copy, ("\n".join(kept) + "\n").encode("utf-8"))
                    else:
                        with source.open(member) as handle, out.open(copy, "w") as sink:
                            for block in iter(lambda: handle.read(1 << 20), b""):
                                sink.write(block)
    digest = hashlib.sha256()
    with open(target, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    # Bytes, not text: on Windows text mode would write "\r\n", and `sha256sum -c` would then look for
    # a file whose name ends in a carriage return.
    (outdir / (name + ".zip.sha256")).write_bytes(("%s  %s\n" % (digest.hexdigest(), target.name)).encode("utf-8"))
    return target, digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Combine per-operating-system installer zips into one zip.")
    parser.add_argument("zips", nargs="+", help="the per-platform zips to combine")
    parser.add_argument("--kind", required=True, choices=("embedded", "standalone", "wheels"), help="what the inputs are (embedded is the same as standalone: one folder per operating system)")
    parser.add_argument("--outdir", default="dist", help="where to write the combined zip (default: dist)")
    parser.add_argument("--label", default="", type=lambda text: text if re.fullmatch(r"[A-Za-z0-9_.]*", text) else
                        parser.error("--label may contain only letters, digits, _ and ."),
                        help="append -TEXT to the name of the zip and of the folder in it, after the version")
    args = parser.parse_args(argv)
    try:
        bundles = []
        for path in args.zips:
            root, info, installer = read_bundle(path)
            standalone = info.get("kind") in STANDALONE_KINDS
            if standalone != (args.kind in ("standalone", "embedded")):
                raise CombineError("%s is a %s zip, not a %s zip." % (Path(path).name, "standalone" if standalone else "wheels", args.kind))
            bundles.append((path, root, info, installer))
        check_compatible(bundles)
        target, digest = write_combined(bundles, Path(args.outdir), "-" + args.label if args.label else "")
    except CombineError as exc:
        print("\nCOMBINE FAILED: %s" % exc, file=sys.stderr)
        return 1
    log("Combined %d builds into %s" % (len(bundles), target))
    log("  size:    %.1f MB" % (target.stat().st_size / 1e6))
    log("  sha256:  %s" % digest)
    for _, _, info, _ in sorted(bundles, key=lambda b: folder_name(b[2])):
        log("  %s/" % folder_name(info))
    return 0


if __name__ == "__main__":
    sys.exit(main())
