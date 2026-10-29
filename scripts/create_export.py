#!/usr/bin/env python3

# Copyright(c) 2026 Lattice Semiconductor Corporation. All rights reserved. DO NOT DISTRIBUTE.
# Subject to the terms of the AI Solutions Software License Agreement
"""Assemble the lattice-diamond-mcp installers: the single entry point, with the same options, names and sidecar
files as cortex-bridge's scripts/create_export.py.

Run `python scripts/create_export.py --help` for what it produces, how to build from a ref, and worked examples.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
import zipfile
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python 3.10 and older
    sys.exit("create_export.py needs Python 3.11 or later (it reads pyproject.toml with tomllib).")

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
NAME = "lattice-diamond-mcp"
WORKTREE = "WORKTREE"          # --ref default: the working tree, uncommitted changes included
IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
EMBEDDED_GLOB = NAME + "-*-embedded-windows-x86_64.zip"
LINUX_GLOB = NAME + "-*-linux-x86_64.zip"
LICENCE_GAP_MARK = "WARNING: no licence file was found for:"

EPILOG = """
what it produces
  One Windows + Linux package for the customer, built from the existing builders (scripts/build_embedded.py,
  scripts/build_binary.py, scripts/combine_installers.py). Nothing here is Python-version specific, so there is
  no --python or --online: Windows gets python.org's signed Python with the packages (no executable of ours),
  Linux gets the standalone program.

  <outdir>/lattice-diamond-mcp-<version>-hybrid-windows-linux-x86_64.zip      + .sha256   (--both)
      one install.py; PACKAGES.txt; windows-x86_64/ (the embedded package) and linux-x86_64/ (the standalone
      program), each with its own BUILDINFO, MANIFEST.sha256, LICENSE, LEGAL_REVIEW.txt and
      THIRD_PARTY_LICENSES. install.py picks the folder for the machine and checks its MANIFEST.sha256 first.
  <outdir>/lattice-diamond-mcp-<version>-embedded-windows-x86_64.zip           + .sha256   (--windows)
  <outdir>/lattice-diamond-mcp-<version>-linux-x86_64.zip                      + .sha256   (--linux)
      each half alone, for CI and for testing; either installs by itself.

  beside every zip (not inside it): <zip>.sha256, <zip>.sbom.cdx.json (a CycloneDX SBOM of everything the zip
  installs: the locked packages with their SHA-256, the interpreter, and for Linux the embedded CPython and
  PyInstaller) and <zip>.buildinfo (an internal record of the commit and the parts; never hand it to a customer).
  --label TEXT adds -TEXT after the version in the names.

  One of --windows, --linux or --both is required, so that whoever runs this says what it wants.

which operating system can build what
  The Windows package only downloads and unpacks files, so it builds anywhere; it can be started and tested only
  on Windows, so on another system it is checked for layout only (the output says so). The Linux program is a
  PyInstaller build and must be built on Linux (glibc 2.17 floor, uv-managed CPython). So --both works on Linux; on
  Windows give the Linux half as a zip built elsewhere: --both --linux-zip FILE (and --windows-zip FILE to reuse a
  Windows half too; CI does this). Both zips must come from the same commit.

building from a ref
  --ref REF reads the repository at that branch, tag or SHA (git archive) and runs the builders from that copy, so
  the packages are reproducible from the commit recorded in <zip>.buildinfo. Without --ref the working tree is
  built, uncommitted changes included: fine for local work, not for a release. Files the builders read
  (uv.lock, scripts/python_notices/, scripts/keys/, LICENSE, the user guide) must be committed first.

third-party licences
  Each builder collects the licence texts of what it ships. A package with no licence text makes this script
  FAIL; --allow-license-gaps turns that into a warning (local work only, never a release).

examples
  python scripts/create_export.py --both
      The hybrid package, built from the working tree (on Linux).

  python scripts/create_export.py --windows --outdir out
      The Windows half only (on any system; run it on Windows for the install test).

  python scripts/create_export.py --linux --outdir out
      The Linux half only.

  python scripts/create_export.py --both --ref main --label rc1 --outdir dist/main
      Build main, name the zip ...-0.1.0-rc1-hybrid-...

  python scripts/create_export.py --both --windows-zip parts/lattice-diamond-mcp-0.1.0-embedded-windows-x86_64.zip \\
      --linux-zip parts/lattice-diamond-mcp-0.1.0-linux-x86_64.zip
      Combine two zips that were built on their own systems.
"""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

class ExportError(Exception):
    pass


def say(message=""):
    print(message, flush=True)


def run(cmd, cwd=None, env=None):
    """Run `cmd`, echo its output as it arrives, return (exit code, output)."""
    say("  $ " + " ".join(str(c) for c in cmd))
    proc = subprocess.Popen([str(c) for c in cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", cwd=cwd, env=env, stdin=subprocess.DEVNULL)
    lines = []
    for line in proc.stdout:
        sys.stdout.write("    " + line)
        lines.append(line)
    proc.wait()
    return proc.returncode, "".join(lines)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def project_version(root):
    with open(Path(root) / "pyproject.toml", "rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo)] + list(args), capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
    if result.returncode != 0:
        raise ExportError("git %s failed in %s: %s" % (" ".join(args), repo, result.stderr.strip()))
    return result.stdout.strip()


def git_dirty(repo):
    try:
        return bool(git(repo, "status", "--porcelain"))
    except (ExportError, OSError, subprocess.SubprocessError):
        return False


def commit_of(repo, ref):
    """Short commit of `ref`; for the working tree, HEAD plus a note when there are uncommitted changes."""
    try:
        commit = git(repo, "rev-parse", "--short", "HEAD" if ref == WORKTREE else ref + "^{commit}")
    except (ExportError, OSError, subprocess.SubprocessError):
        return "unknown"
    return commit + ("+uncommitted-changes" if ref == WORKTREE and git_dirty(repo) else "")


def export_repo(repo, ref, dest):
    """Unpack `ref` of `repo` into `dest` (git archive) and return `dest`; for the working tree, return the repo itself."""
    if ref == WORKTREE:
        return Path(repo)
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(["git", "-C", str(repo), "archive", "--format=tar", ref], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for member in tar:
                if member.name.startswith("/") or ".." in Path(member.name).parts or member.issym() or member.islnk():
                    continue                       # nothing in this repository needs a link; never follow one out of dest
                try:
                    tar.extract(member, dest, filter="data")     # Python 3.12 and later (and security releases of older ones)
                except TypeError:
                    tar.extract(member, dest)
    finally:
        proc.stdout.close()
        error = proc.stderr.read().decode("utf-8", "replace")
        proc.stderr.close()
        if proc.wait() != 0:
            raise ExportError("git archive %s failed in %s: %s" % (ref, repo, error.strip()))
    return dest


def labelled(path, version, label):
    """Insert -label after the version in a zip's name, moving its .sha256 along; returns the new path."""
    if not label:
        return path
    new = path.with_name(path.name.replace("%s-%s" % (NAME, version), "%s-%s-%s" % (NAME, version, label), 1))
    shutil.move(str(path), str(new))
    old_sum = Path(str(path) + ".sha256")
    if old_sum.exists():
        old_sum.unlink()
    Path(str(new) + ".sha256").write_bytes(("%s  %s\n" % (sha256_file(new), new.name)).encode("utf-8"))
    return new


# ---------------------------------------------------------------------------
# The builders
# ---------------------------------------------------------------------------

def run_builder(root, script, outdir, extra=()):
    """Run scripts/<script> from the tree at `root`; return its output. ExportError when it fails."""
    rc, out = run([sys.executable, Path(root) / "scripts" / script, "--outdir", outdir] + list(extra), cwd=root)
    if rc != 0:
        raise ExportError("%s failed (exit status %d); see its output above." % (script, rc))
    return out


def only_zip(folder, pattern, what):
    found = sorted(p for p in Path(folder).glob(pattern) if not p.name.endswith(".sha256"))
    if len(found) != 1:
        raise ExportError("Expected one %s in %s, found %d." % (what, folder, len(found)))
    return found[0]


def check_licence_gaps(output, allow, builder):
    for line in output.splitlines():
        if LICENCE_GAP_MARK in line:
            message = "%s: %s" % (builder, line.strip())
            if not allow:
                raise ExportError(message + "\nA package without a licence text cannot ship (--allow-license-gaps is for local work only).")
            say("  [warn] " + message)


def build_windows(root, work, args):
    extra = []
    if args.no_verify:
        extra.append("--skip-verify")
    if args.skip_pgp:
        extra.append("--skip-pgp")
    for flag, value in (("--embed-zip", args.embed_zip), ("--embed-sig", args.embed_sig)):
        if value:
            extra += [flag, value]
    out = run_builder(root, "build_embedded.py", work, extra)
    check_licence_gaps(out, args.allow_license_gaps, "build_embedded.py")
    return only_zip(work, EMBEDDED_GLOB, "Windows package")


def build_linux(root, work, args):
    if not IS_LINUX:
        raise ExportError("The Linux program is a PyInstaller build and can be built only on Linux. Build it there and pass it "
                          "here with --linux-zip.")
    out = run_builder(root, "build_binary.py", work, ["--skip-verify"] if args.no_verify else [])
    check_licence_gaps(out, args.allow_license_gaps, "build_binary.py")
    return only_zip(work, LINUX_GLOB, "Linux program")


def prebuilt(path, what):
    """A zip given on the command line: it must exist, and its .sha256 (when beside it) must match."""
    path = Path(path)
    if not path.is_file():
        raise ExportError("%s %s does not exist." % (what, path))
    checksum = Path(str(path) + ".sha256")
    if checksum.is_file():
        expected = checksum.read_text(encoding="utf-8").split()[0]
        if expected != sha256_file(path):
            raise ExportError("%s does not match %s." % (path.name, checksum.name))
    return path


def combine(root, zips, outdir, label):
    extra = ["--kind", "standalone"] + (["--label", label] if label else [])
    out = run_builder(root, "combine_installers.py", outdir, extra + [str(z) for z in zips])
    return only_zip(outdir, NAME + "-*-hybrid-windows-linux-*.zip", "combined package")


# ---------------------------------------------------------------------------
# Sidecars: SBOM and build record, beside each zip
# ---------------------------------------------------------------------------

def member_text(zip_path, name):
    with zipfile.ZipFile(zip_path) as archive:
        root = sorted({n.split("/", 1)[0] for n in archive.namelist() if n.strip("/")})[0]
        return archive.read("%s/%s" % (root, name)).decode("utf-8")


def parse_requirements(text):
    """[(name, version, sha256)] from a hash-pinned requirements.txt."""
    rows = []
    for block in re.split(r"\n(?=\S)", text.strip()):
        head = re.match(r"([A-Za-z0-9_.-]+)==(\S+)", block)
        digest = re.search(r"--hash=sha256:([0-9a-f]{64})", block)
        if head and digest:
            rows.append((head.group(1), head.group(2), digest.group(1)))
    return rows


def licence_fields(text):
    """{normalised name: licence field} from THIRD_PARTY_LICENSES.txt lines `name version  [licence]`."""
    fields = {}
    for match in re.finditer(r"^([A-Za-z0-9_.-]+) (\S+)  \[([^\]]*)\]$", text, re.M):
        if "not stated" not in match.group(3):
            fields[re.sub(r"[-_.]+", "-", match.group(1)).lower()] = match.group(3)
    return fields


def embedded_components(zip_path):
    """CycloneDX components of a Windows package: the interpreter and every package, from its own records."""
    info = json.loads(member_text(zip_path, "BUILDINFO"))
    licences = licence_fields(member_text(zip_path, "THIRD_PARTY_LICENSES.txt"))
    components = [{
        "type": "library", "bom-ref": "pkg:generic/cpython@%s?platform=windows" % info["python"], "name": "cpython",
        "version": info["python"], "purl": "pkg:generic/cpython@%s?download_url=%s" % (info["python"], info["python_source"]),
        "hashes": [{"alg": "SHA-256", "content": info["python_sha256"]}], "licenses": [{"license": {"id": "Python-2.0"}}],
        "description": "python.org's embeddable CPython, unmodified (the zip as published, SHA-256 above)",
        "properties": [{"name": "lattice:platform", "value": "windows"},
                       {"name": "lattice:openpgp-fingerprint", "value": str(info.get("python_pgp_fingerprint"))},
                       {"name": "lattice:openpgp-verified", "value": str(bool(info.get("python_pgp_verified"))).lower()}]}]
    for name, version, digest in parse_requirements(member_text(zip_path, "requirements.txt")):
        key = re.sub(r"[-_.]+", "-", name).lower()
        purl = "pkg:pypi/%s@%s" % (key, version)
        component = {"type": "library", "bom-ref": purl + "?platform=windows", "name": key, "version": version, "purl": purl,
                     "hashes": [{"alg": "SHA-256", "content": digest}],
                     "properties": [{"name": "lattice:platform", "value": "windows"}]}
        if key in licences:
            component["licenses"] = [{"license": {"name": licences[key]}}]
        components.append(component)
    return components


def linux_sbom(zip_path):
    """The SBOM build_binary.py wrote into the Linux program's zip."""
    return json.loads(member_text(zip_path, "sbom.cdx.json"))


def write_sbom(zip_path, digest, version, built_at, commit, parts):
    """<zip>.sbom.cdx.json for a final zip. `parts` is a list of (kind, per-OS zip) the zip holds."""
    components, dependencies = [], []
    for kind, part in parts:
        if kind == "linux":
            base = linux_sbom(part)
            for component in base.get("components", []):
                component = json.loads(json.dumps(component))
                component.setdefault("properties", []).append({"name": "lattice:platform", "value": "linux"})
                component["bom-ref"] = component.get("bom-ref", component.get("name", "?")) + "?platform=linux"
                components.append(component)
        else:
            components.extend(embedded_components(part))
    seen, unique = set(), []
    for component in components:
        if component["bom-ref"] not in seen:
            seen.add(component["bom-ref"])
            unique.append(component)
    bom = {
        "bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
        "serialNumber": "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, "%s:%s" % (zip_path.name, digest))),
        "metadata": {
            "timestamp": built_at,
            "tools": {"components": [{"type": "application", "name": "create_export.py"}]},
            "component": {"type": "application", "name": zip_path.stem, "version": version,
                          "hashes": [{"alg": "SHA-256", "content": digest}],
                          "properties": [{"name": "lattice:source", "value": commit}]},
        },
        "components": unique,
    }
    out = zip_path.parent / (zip_path.name + ".sbom.cdx.json")
    out.write_text(json.dumps(bom, indent=2) + "\n", encoding="utf-8", newline="\n")
    return out


def write_sidecars(zip_path, version, built_at, commit, ref, parts, args, repo):
    digest = sha256_file(zip_path)
    (zip_path.parent / (zip_path.name + ".sha256")).write_bytes(("%s  %s\n" % (digest, zip_path.name)).encode("utf-8"))
    sbom = write_sbom(zip_path, digest, version, built_at, commit, parts)
    dirty = git_dirty(repo)
    lines = ["# Internal build record -- NOT shipped. Do not hand this to a customer.",
             "artifact: %s" % zip_path.name,
             "sha256:   %s" % digest,
             "version:  %s" % version,
             "source:   %s (%s)" % (ref, commit)]
    lines += ["part:     %s %s sha256 %s" % (kind, part.name, sha256_file(part)) for kind, part in parts]
    lines += ["built:    %s" % built_at,
              "built-by: Python %s (%s)" % (sys.version.split()[0], sys.executable)]
    if dirty:
        lines.append("working tree has uncommitted changes" + (" (INCLUDED in the zip)" if ref == WORKTREE else " (not in the zip)"))
    if args.skip_pgp:
        lines.append("OpenPGP check of the Windows interpreter was SKIPPED (--skip-pgp)")
    (zip_path.parent / (zip_path.name + ".buildinfo")).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return digest, sbom


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def platforms_in(bundle):
    """The platforms of the builds in an unpacked zip: its own BUILDINFO, or one per folder (a combined zip)."""
    found = []
    for folder in [bundle] + sorted(p for p in bundle.iterdir() if p.is_dir()):
        info = folder / "BUILDINFO"
        if info.is_file():
            try:
                found.append(json.loads(info.read_text(encoding="utf-8")).get("platform", ""))
            except ValueError:
                continue
    return found


def verify_zip(zip_path):
    """Install the finished zip with its own install.py, as a customer would, and start what was installed.
    Only what this system can run is run; the rest is said to be unchecked. Returns True if it was run."""
    say("Verifying %s ..." % zip_path.name)
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(zip_path) as archive:
            for name in archive.namelist():           # the zip was just built here, but never extract an unchecked path
                if name.startswith("/") or ".." in Path(name).parts:
                    raise ExportError("%s holds an unsafe path: %s" % (zip_path.name, name))
            archive.extractall(tmp)  # nosec B202 - every member name was checked above
            if not IS_WINDOWS:
                for info in archive.infolist():       # extractall drops permission bits; restore them, as `unzip` does
                    mode = (info.external_attr >> 16) & 0o777
                    if mode and not info.is_dir():
                        os.chmod(Path(tmp) / info.filename, mode)
        bundle = next(p for p in Path(tmp).iterdir() if p.is_dir())
        here = "windows" if IS_WINDOWS else "linux"
        platforms = platforms_in(bundle)
        if not any(platform.startswith(here) for platform in platforms):
            say("  NOT run: this zip holds only %s, and this is a %s machine. Its manifest and layout were checked when it was built; "
                "install-test it on the right system." % (", ".join(platforms) or "nothing recognisable", here))
            return False
        others = [platform for platform in platforms if not platform.startswith(here)]
        if others:
            say("  NOT run: the %s build in this zip (it needs that operating system)." % ", ".join(others))
        target = Path(tmp) / "installed"
        env = dict(os.environ, HOME=tmp, USERPROFILE=tmp, LOCALAPPDATA=str(Path(tmp) / "local"), XDG_DATA_HOME=str(Path(tmp) / "share"))
        rc, out = run([sys.executable, bundle / "install.py", "--no-register", "--install-dir", target], env=env)
        if rc != 0 or "Integrity:" not in out:
            raise ExportError("The finished zip did not install (exit status %d)." % rc)
        if "Integrity: all" not in out:
            say("  [warn] the zip carries no MANIFEST.sha256, so its files were not checked")
        exe = target / ("python.exe" if (target / "python.exe").exists() else ("diamond-mcp.exe" if IS_WINDOWS else "diamond-mcp"))
        extra = ["--smoke-test-args=-I -m diamond_mcp"] if exe.name == "python.exe" else []
        rc, out = run([sys.executable, SCRIPTS_DIR / "build_binary.py", "--smoke-test", exe] + extra, env=env)
        if rc != 0:
            raise ExportError("The installed server did not answer over MCP.")
        say("  installed with install.py, manifest checked, and the server answered over MCP")
        return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_all(args):
    repo = Path(args.repo).resolve()
    outdir = Path(args.outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    built_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    commit = commit_of(repo, args.ref)
    with tempfile.TemporaryDirectory() as tmp:
        root = export_repo(repo, args.ref, Path(tmp) / "source")
        version = project_version(root)
        say("Building %s %s from %s (%s)" % (NAME, version, "the working tree" if args.ref == WORKTREE else args.ref, commit))
        if args.ref == WORKTREE and "uncommitted" in commit:
            say("  [warn] the working tree has uncommitted changes and they are in the zips; use --ref for a release")
        work = Path(tmp) / "parts"
        work.mkdir()
        windows = prebuilt(args.windows_zip, "--windows-zip") if args.windows_zip else None
        linux = prebuilt(args.linux_zip, "--linux-zip") if args.linux_zip else None
        if args.which in ("windows", "both") and windows is None:
            windows = build_windows(root, work, args)
        if args.which in ("linux", "both") and linux is None:
            linux = build_linux(root, work, args)

        finals = []
        if args.which == "both":
            final = combine(root, [windows, linux], outdir, args.label)
            finals.append((final, [("windows", windows), ("linux", linux)]))
        else:
            kind, source = ("windows", windows) if args.which == "windows" else ("linux", linux)
            target = outdir / source.name
            if source.resolve() != target.resolve():
                shutil.copyfile(source, target)
            finals.append((labelled(target, version, args.label), [(kind, source)]))

        results = []
        for final, parts in finals:
            if not args.no_verify:
                verify_zip(final)
            digest, sbom = write_sidecars(final, version, built_at, commit, args.ref, parts, args, repo)
            results.append((final, digest, sbom))
    return results


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(
        prog="create_export.py",
        description="Assemble the lattice-diamond-mcp installers for Windows and Linux, in cortex-bridge's convention: "
                    "--windows (python.org's Python with the packages), --linux (the standalone program) or --both "
                    "(the hybrid package).",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG)
    p.add_argument("--outdir", default=str(REPO_ROOT / "dist"), metavar="DIR", help="where to write the zips (default: dist)")
    p.add_argument("--repo", default=str(REPO_ROOT), metavar="DIR", help="this repository (default: the one holding this script)")
    p.add_argument("--ref", default=WORKTREE, metavar="REF",
                   help="ref to build (default: WORKTREE, the working tree, so uncommitted changes are included; pass a branch, "
                        "tag or SHA for a reproducible build)")
    p.add_argument("--windows-zip", metavar="FILE", help="use this Windows package instead of building one (with --both)")
    p.add_argument("--linux-zip", metavar="FILE", help="use this Linux program zip instead of building one (with --both); "
                                                       "needed on Windows, where it cannot be built")
    p.add_argument("--embed-zip", metavar="FILE", help="Windows: use this already downloaded python.org embeddable zip "
                                                       "(still checked against its pinned SHA-256)")
    p.add_argument("--embed-sig", metavar="FILE", help="Windows: the .asc that belongs to --embed-zip")
    p.add_argument("--skip-pgp", action="store_true",
                   help="Windows: do not check the interpreter's OpenPGP signature (local experiments; recorded in the .buildinfo)")
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument("--windows", dest="which", action="store_const", const="windows",
                       help="build the Windows package: python.org's signed Python with the packages, no executable of ours")
    which.add_argument("--linux", dest="which", action="store_const", const="linux",
                       help="build the Linux standalone program (Linux only)")
    which.add_argument("--both", dest="which", action="store_const", const="both",
                       help="build both and combine them into the hybrid package")
    p.add_argument("--label", default="", metavar="TEXT",
                   type=lambda s: s if re.fullmatch(r"[A-Za-z0-9_.]*", s) else p.error("--label may contain only letters, digits, _ and ."),
                   help="append -TEXT to the zip names, after the version")
    p.add_argument("--no-verify", action="store_true",
                   help="skip the install-and-run check of the finished zips (faster; not for releases)")
    p.add_argument("--allow-license-gaps", action="store_true",
                   help="warn instead of failing when a package has no licence text (local builds only; never for a release)")
    args = p.parse_args()

    if args.which != "both" and (args.windows_zip or args.linux_zip):
        p.error("--windows-zip and --linux-zip are for --both, which combines them")
    if args.which == "both" and not args.linux_zip and not IS_LINUX:
        p.error("the Linux program can be built only on Linux; pass the one built there with --linux-zip")
    if args.which == "linux" and not IS_LINUX:
        p.error("--linux needs a Linux machine (the program is a PyInstaller build)")
    if (args.embed_zip or args.embed_sig or args.skip_pgp) and args.which == "linux":
        p.error("--embed-zip, --embed-sig and --skip-pgp are for the Windows package")
    try:
        results = build_all(args)
    except ExportError as exc:
        print("\nEXPORT FAILED: %s" % exc, file=sys.stderr)
        return 1
    say()
    for final, digest, sbom in results:
        say("Built %s" % final)
        say("  size:    %.1f MB" % (final.stat().st_size / 1e6))
        say("  sha256:  %s" % digest)
        say("  beside it: %s.sha256, %s, %s.buildinfo" % (final.name, sbom.name, final.name))
    say()
    say("FOR LEGAL: LEGAL_REVIEW.txt in each folder of the zip lists the items that need a decision.")
    if not IS_WINDOWS and args.which != "linux":
        say("NOTE: the Windows package was not installed or started here (that needs Windows).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
