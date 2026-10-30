"""Tests for the Linux embedded package (build_embedded.py --platform linux), its install on Linux, scripts/smoke_test.py,
and create_export.py's --linux and --both modes."""

import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
REPO = SCRIPTS.parent
posix_only = pytest.mark.skipif(os.name == "nt", reason="uses shell-script test doubles and POSIX file modes")


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


embedded = load("build_embedded")
install = load("install")
export = load("create_export")
smoke = load("smoke_test")
MINOR = embedded.MINOR


# ------------------------------------------------------------------------ the pinned interpreter

def test_the_pinned_linux_interpreter_is_the_release_the_notices_were_fetched_for():
    assert embedded.PBS_RELEASE == (embedded.NOTICES_DIR / "RELEASE").read_text().strip()
    assert embedded.PBS_URL.startswith("https://github.com/astral-sh/python-build-standalone/releases/download/%s/" % embedded.PBS_RELEASE)
    assert "%2B" in embedded.PBS_URL and embedded.EMBED_VERSION in embedded.PBS_ARCHIVE and len(embedded.PBS_SHA256) == 64
    assert embedded.LINUX_PROGRAM == "bin/python" + MINOR


def test_the_linux_archive_must_match_the_pinned_hash(tmp_path, monkeypatch):
    archive = tmp_path / "py.tar.gz"
    archive.write_bytes(b"not the interpreter")
    with pytest.raises(embedded.BuildError, match="expected"):
        embedded.fetch_pbs(tmp_path, archive)
    monkeypatch.setattr(embedded, "PBS_SHA256", hashlib.sha256(archive.read_bytes()).hexdigest())
    assert embedded.fetch_pbs(tmp_path, archive) == archive


# ----------------------------------------------------------------------------- unpacking and trimming

def make_pbs_archive(path, extra=(), with_program=True):
    """A stand-in for a python-build-standalone install_only archive."""
    files = {
        "python/bin/python%s" % MINOR: (b"ELF-interpreter", 0o755),
        "python/bin/pip": (b"#!/bin/sh", 0o755), "python/bin/idle3": (b"#!/bin/sh", 0o755),
        "python/include/Python.h": (b"h", 0o644), "python/share/man/man1/python3.1": (b"m", 0o644),
        "python/lib/libpython%s.so.1.0" % MINOR: (b"lib", 0o755), "python/lib/libtcl9.0.so": (b"tcl", 0o755),
        "python/lib/tcl9.0/init.tcl": (b"t", 0o644), "python/lib/pkgconfig/python3.pc": (b"p", 0o644),
        "python/lib/python%s/os.py" % MINOR: (b"import sys", 0o644), "python/lib/python%s/LICENSE.txt" % MINOR: (b"PSF", 0o644),
        "python/lib/python%s/ensurepip/__init__.py" % MINOR: (b"e", 0o644), "python/lib/python%s/idlelib/x.py" % MINOR: (b"i", 0o644),
        "python/lib/python%s/test/test_x.py" % MINOR: (b"t", 0o644), "python/lib/python%s/tkinter/__init__.py" % MINOR: (b"k", 0o644),
        "python/lib/python%s/lib-dynload/_tkinter.so" % MINOR: (b"so", 0o755),
        "python/lib/python%s/lib-dynload/.empty" % MINOR: (b"", 0o644),
        "python/lib/python%s/site-packages/pip/__init__.py" % MINOR: (b"p", 0o644),
        "python/lib/python%s/site-packages/README.txt" % MINOR: (b"r", 0o644),
    }
    if not with_program:
        files.pop("python/bin/python%s" % MINOR)
    for name, data in extra:
        files[name] = (data, 0o644)
    with tarfile.open(path, "w:gz") as tar:
        for name, (data, mode) in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            tar.addfile(info, io.BytesIO(data))
        for link, target in (("python/bin/python3", "python%s" % MINOR), ("python/bin/python", "python%s" % MINOR)):
            info = tarfile.TarInfo(link)
            info.type, info.linkname = tarfile.SYMTYPE, target
            tar.addfile(info)
    return path


@posix_only
def test_unpacking_keeps_the_interpreter_and_the_standard_library_and_drops_the_rest(tmp_path):
    dest = tmp_path / "python"
    removed = embedded.unpack_pbs(make_pbs_archive(tmp_path / "py.tar.gz"), dest)
    kept = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    assert kept == ["bin/python" + MINOR, "lib/python%s/LICENSE.txt" % MINOR, "lib/python%s/lib-dynload/.empty" % MINOR,
                    "lib/python%s/os.py" % MINOR]
    assert not any(p.is_symlink() for p in dest.rglob("*"))
    assert os.access(dest / "bin" / ("python" + MINOR), os.X_OK)
    for gone in ("bin/pip", "bin/idle3", "include", "share", "lib/tcl9.0", "lib/libtcl9.0.so", "lib/libpython%s.so.1.0" % MINOR,
                 "lib/python%s/ensurepip" % MINOR, "lib/python%s/idlelib" % MINOR, "lib/python%s/test" % MINOR,
                 "lib/python%s/tkinter" % MINOR, "lib/python%s/site-packages/pip" % MINOR):
        assert gone in removed or not (dest / gone).exists(), gone
        assert not (dest / gone).exists(), gone


@posix_only
@pytest.mark.parametrize("member", ["../escape.txt", "/etc/escape.txt", "other/top-level.txt"])
def test_an_archive_with_an_unsafe_or_unexpected_path_is_refused(tmp_path, member):
    archive = make_pbs_archive(tmp_path / "py.tar.gz", extra=[(member, b"x")])
    with pytest.raises(embedded.BuildError, match="unexpected path"):
        embedded.unpack_pbs(archive, tmp_path / "python")
    assert not (tmp_path / "escape.txt").exists()


@posix_only
def test_an_archive_without_the_interpreter_is_refused_as_a_changed_layout(tmp_path):
    with pytest.raises(embedded.BuildError, match="layout has changed"):
        embedded.unpack_pbs(make_pbs_archive(tmp_path / "py.tar.gz", with_program=False), tmp_path / "python")


# ------------------------------------------------------------------------------------ glibc

OBJDUMP = """
0000000000000000      DF *UND*  0000000000000000  GLIBC_2.2.5 memcpy
0000000000000000      DF *UND*  0000000000000000  (GLIBC_2.17) clock_gettime
"""


def test_the_highest_glibc_symbol_version_is_read_from_objdump_output():
    assert embedded.parse_glibc_requirement(OBJDUMP) == (2, 17)
    assert embedded.parse_glibc_requirement(OBJDUMP + "GLIBC_2.34 pthread_create") == (2, 34)
    assert embedded.parse_glibc_requirement("nothing here") is None


@posix_only
def test_a_file_that_needs_a_newer_glibc_than_the_floor_fails_the_build(tmp_path, monkeypatch):
    (tmp_path / "ok.so").write_bytes(b"\x7fELF" + b"1")
    (tmp_path / "new.so").write_bytes(b"\x7fELF" + b"2")
    (tmp_path / "data.txt").write_text("not elf")
    monkeypatch.setattr(embedded.shutil, "which", lambda name: "/usr/bin/objdump")

    def fake_run(command, **kwargs):
        text = OBJDUMP + ("GLIBC_2.34 pthread_create\n" if command[-1].endswith("new.so") else "")
        return subprocess.CompletedProcess(command, 0, stdout=text)
    monkeypatch.setattr(embedded.subprocess, "run", fake_run)
    with pytest.raises(embedded.BuildError, match="glibc 2.34  new.so"):
        embedded.glibc_floor(tmp_path)
    (tmp_path / "new.so").unlink()
    assert embedded.glibc_floor(tmp_path) == (2, 17)


# ----------------------------------------------------------------------------------- layout

def linux_stage(tmp_path):
    stage = tmp_path / "stage"
    site = stage / "python" / "lib" / ("python" + MINOR) / "site-packages"
    (site / "mcp").mkdir(parents=True)
    (site / "lattice_diamond_mcp-0.1.0.dist-info").mkdir()
    (stage / "python" / "bin").mkdir()
    program = stage / "python" / "bin" / ("python" + MINOR)
    program.write_text("x")
    program.chmod(0o755)
    return stage


@posix_only
def test_a_correct_linux_layout_passes(tmp_path):
    embedded.verify_layout_linux(linux_stage(tmp_path), ["mcp"])


@posix_only
@pytest.mark.parametrize("damage, message", [
    ("extra_program", "no program but bin/python"),
    ("not_executable", "is not executable"),
    ("symlink", "no symbolic links"),
    ("pip", "pip must not be in the package"),
    ("missing_package", "Expected mcp in site-packages"),
])
def test_a_linux_layout_with_a_stray_program_link_pip_or_missing_package_fails(tmp_path, damage, message):
    stage = linux_stage(tmp_path)
    python = stage / "python"
    site = python / "lib" / ("python" + MINOR) / "site-packages"
    if damage == "extra_program":
        (python / "bin" / "pip").write_text("x")
    elif damage == "not_executable":
        (python / "bin" / ("python" + MINOR)).chmod(0o644)
    elif damage == "symlink":
        os.symlink("python" + MINOR, python / "bin" / "python3")
        (python / "bin" / "python3").unlink()
        os.symlink("os.py", python / "lib" / "link")
    elif damage == "pip":
        (site / "pip").mkdir()
    elif damage == "missing_package":
        (site / "mcp").rmdir()
    with pytest.raises(embedded.BuildError, match=message):
        embedded.verify_layout_linux(stage, ["mcp"])


@posix_only
def test_the_native_files_of_the_linux_package_are_the_elf_files_with_no_signer(tmp_path):
    stage = linux_stage(tmp_path)
    (stage / "python" / "bin" / ("python" + MINOR)).write_bytes(b"\x7fELF" + b"0" * 10)
    (stage / "python" / "lib" / "mod.so").write_bytes(b"\x7fELF" + b"1" * 10)
    (stage / "python" / "lib" / "notes.txt").write_text("text")
    rows = embedded.native_inventory(stage, "linux-x86_64")
    assert sorted(r[0] for r in rows) == ["python/bin/python" + MINOR, "python/lib/mod.so"]
    assert all(r[1].startswith("not applicable") and len(r[2]) == 64 for r in rows)
    text = embedded.inventory_text(rows)
    assert "Linux has no code-signing check" in text and "Totals: 2 not applicable (Linux)" in text


def test_the_linux_legal_review_states_the_facts_and_the_open_questions():
    text = embedded.linux_legal_review_text(version="0.1.0", embedded_notices=["LICENSE.openssl-3.txt"], native_files=["rpds.so"],
                                            removed=["include", "share"], omitted_packages=["pyjwt"], floor=(2, 17))
    for needle in ("python-build-standalone", embedded.PBS_SHA256, embedded.PBS_RELEASE, "publishes no signature", "no OpenPGP check",
                   "LICENSE.openssl-3.txt", "glibc", "2.17", "rpds.so", "pyjwt", "Question for Legal", "executable of ours"):
        assert needle in text, needle
    assert "WARNING" not in text and "WARNING" in embedded.linux_legal_review_text(
        version="0.1.0", embedded_notices=[], native_files=[], removed=[], omitted_packages=[], floor=None)


# ----------------------------------------------------------------------------- the installer

def linux_source(folder, mode=0o755, script="#!/bin/sh\necho diamond-mcp usage\n"):
    source = folder / "python"
    exe = source / "bin" / ("python" + MINOR)
    exe.parent.mkdir(parents=True)
    exe.write_text(script)
    exe.chmod(mode)
    return source


@posix_only
def test_the_installer_finds_the_bundled_interpreter_by_the_recorded_program_or_by_looking(tmp_path):
    root = tmp_path / "inst"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "python3.12").write_text("x")
    os.symlink("python3.12", root / "bin" / "python3")
    (root / "bin" / "python3.12-config").write_text("x")
    assert install.embedded_python(str(root), "bin/python3.12") == str(root / "bin" / "python3.12")
    assert install.embedded_python(str(root)) == str(root / "bin" / "python3.12")                 # no link, no -config
    assert install.embedded_python(str(tmp_path / "empty")).endswith(os.path.join("bin", "python3"))


@posix_only
def test_a_linux_install_repairs_a_lost_execute_bit_and_registers_the_bundled_interpreter(tmp_path):
    linux_source(tmp_path / "zip", mode=0o644)                      # as some unzip tools leave it
    args = install.parse_args(["--install-dir", str(tmp_path / "target")])
    python = install.install_embedded_bundle(args, {"version": "0.1.0", "python": "3.12.10", "program": "bin/python3.12"}, str(tmp_path / "zip"))
    assert python == str(tmp_path / "target" / "bin" / "python3.12") and os.access(python, os.X_OK)
    assert (tmp_path / "target" / install.MARKER).exists()


@posix_only
def test_a_linux_folder_given_with_install_dir_is_recognised_as_embedded_for_uninstall(tmp_path):
    folder = linux_source(tmp_path / "x")
    assert install.uninstall_targets(install.parse_args(["--uninstall", "--install-dir", str(folder)]))[0][0] == "embedded"


# ------------------------------------------------------------------------------ smoke test

def run_smoke(*args):
    return subprocess.run([sys.executable, str(SCRIPTS / "smoke_test.py")] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


@posix_only
def test_the_smoke_test_passes_against_the_real_server(tmp_path):
    wrapper = tmp_path / "diamond-mcp"
    wrapper.write_text('#!/bin/sh\nPYTHONPATH="%s" exec "%s" -m diamond_mcp "$@"\n' % (REPO / "src", sys.executable))
    wrapper.chmod(0o755)
    names = smoke.mcp_smoke_test(wrapper)
    assert len(names) >= smoke.MIN_TOOLS and "compile_project" in names


@posix_only
@pytest.mark.parametrize("script, fragment", [
    ('if [ "$1" = "--help" ]; then echo "usage: diamond-mcp"; fi', "closed its output"),
    ('echo "something unrelated"', "did not describe"),
    ('echo "usage: diamond-mcp"', "not JSON-RPC"),
    ('if [ "$1" = "--help" ]; then echo "usage: diamond-mcp"; else read l; echo \'{"jsonrpc":"2.0","id":1,"result":{}}\'; fi', None),
])
def test_the_smoke_test_rejects_programs_that_do_not_speak_mcp(tmp_path, script, fragment):
    fake = tmp_path / "diamond-mcp"
    fake.write_text("#!/bin/sh\n" + script + "\n")
    fake.chmod(0o755)
    with pytest.raises(smoke.SmokeError) as excinfo:
        smoke.mcp_smoke_test(fake)
    if fragment:
        assert fragment in str(excinfo.value)


@posix_only
def test_the_smoke_test_command_line_takes_the_program_and_its_arguments(tmp_path):
    good = tmp_path / "python3.12"
    good.write_text('#!/bin/sh\nPYTHONPATH="%s" exec "%s" "$@"\n' % (REPO / "src", sys.executable))
    good.chmod(0o755)
    bad = tmp_path / "broken"
    bad.write_text("#!/bin/sh\nexit 3\n")
    bad.chmod(0o755)
    ok = run_smoke(str(good), "--args=-I -m diamond_mcp")
    assert ok.returncode == 0 and "smoke test passed" in ok.stdout
    failed = run_smoke(str(bad))
    assert failed.returncode == 1 and "SMOKE TEST FAILED" in failed.stderr and "Traceback" not in failed.stderr
    missing = run_smoke(str(tmp_path / "does-not-exist"))
    assert missing.returncode == 1 and "Traceback" not in missing.stderr


# ----------------------------------------------------------------------------------- create_export

def fake_embedded_zip(folder, platform, version="0.1.0", git="abc1234"):
    info = {"name": "lattice-diamond-mcp", "version": version, "kind": "embedded", "platform": platform, "git": git, "python": "3.12.10",
            "python_tag": "cp312", "python_source": "https://example.invalid/python", "python_sha256": "c" * 64,
            "python_pgp_fingerprint": None, "python_pgp_verified": False}
    path = folder / ("lattice-diamond-mcp-%s-embedded-%s.zip" % (version, platform))
    root = "lattice-diamond-mcp-" + version
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(root + "/BUILDINFO", json.dumps(info))
        archive.writestr(root + "/install.py", b"# installer\n")
        archive.writestr(root + "/requirements.txt", "pydantic==2.14.1 \\\n    --hash=sha256:" + "a" * 64 + "\n")
        archive.writestr(root + "/THIRD_PARTY_LICENSES.txt", "pydantic 2.14.1  [MIT]\n")
        archive.writestr(root + "/python/" + ("python.exe" if platform.startswith("windows") else "bin/python3.12"), b"x")
    return path


def test_the_new_modes_are_listed_in_the_help_and_need_the_right_options():
    help_text = subprocess.run([sys.executable, str(SCRIPTS / "create_export.py"), "--help"], stdout=subprocess.PIPE, text=True).stdout
    assert "--both" in help_text and "--linux" in help_text and "--linux-python" in help_text
    assert "--combined-embedded" not in help_text and "PyInstaller" not in help_text and "hybrid" not in help_text
    for argv in (["--windows", "--linux-python", "x.tar.gz"], ["--linux", "--embed-zip", "x.zip"], ["--linux", "--skip-pgp"],
                 ["--windows", "--both"], ["--linux", "--windows-zip", "w.zip"]):
        assert subprocess.run([sys.executable, str(SCRIPTS / "create_export.py")] + argv, capture_output=True, text=True).returncode == 2, argv


def test_combined_embedded_builds_both_halves_and_one_zip_with_sidecars_from_any_system(tmp_path, monkeypatch):
    windows, linux = fake_embedded_zip(tmp_path, "windows-x86_64"), fake_embedded_zip(tmp_path, "linux-x86_64")
    calls = []
    monkeypatch.setattr(export, "build_windows", lambda root, work, args: calls.append("windows") or windows)
    monkeypatch.setattr(export, "build_linux", lambda root, work, args: calls.append("linux") or linux)
    monkeypatch.setattr(export, "project_version", lambda root: "0.1.0")
    args = export.argparse.Namespace(
        outdir=str(tmp_path / "out"), repo=str(REPO), ref=export.WORKTREE, windows_zip=None, linux_zip=None, linux_python=None,
        embed_zip=None, embed_sig=None, skip_pgp=False, which="both", label="rc1", no_verify=True, allow_license_gaps=False)
    (final, digest, sbom), = export.build_all(args)
    assert calls == ["windows", "linux"]
    assert final.name == "lattice-diamond-mcp-0.1.0-rc1-embedded-windows-linux-x86_64.zip"
    assert Path(str(final) + ".sha256").read_text() == "%s  %s\n" % (digest, final.name)
    names = zipfile.ZipFile(final).namelist()
    assert any("/windows-x86_64/python/python.exe" in n for n in names) and any("/linux-x86_64/python/bin/python3.12" in n for n in names)
    bom = json.loads(sbom.read_text())
    platforms = {p["value"] for c in bom["components"] for p in c.get("properties", []) if p["name"] == "lattice:platform"}
    refs = [c["bom-ref"] for c in bom["components"]]
    assert platforms == {"windows", "linux"} and len(refs) == len(set(refs))
    record = Path(str(final) + ".buildinfo").read_text()
    assert "part:     windows" in record and "part:     linux" in record


def test_prebuilt_halves_are_combined_without_building(tmp_path, monkeypatch):
    windows, linux = fake_embedded_zip(tmp_path, "windows-x86_64"), fake_embedded_zip(tmp_path, "linux-x86_64")
    monkeypatch.setattr(export, "build_windows", lambda *a: pytest.fail("must not build"))
    monkeypatch.setattr(export, "build_linux", lambda *a: pytest.fail("must not build"))
    monkeypatch.setattr(export, "project_version", lambda root: "0.1.0")
    args = export.argparse.Namespace(
        outdir=str(tmp_path / "out"), repo=str(REPO), ref=export.WORKTREE, windows_zip=str(windows), linux_zip=str(linux), linux_python=None,
        embed_zip=None, embed_sig=None, skip_pgp=False, which="both", label="", no_verify=True, allow_license_gaps=False)
    (final, _, _), = export.build_all(args)
    assert final.name == "lattice-diamond-mcp-0.1.0-embedded-windows-linux-x86_64.zip"


def test_the_linux_half_alone_keeps_its_name_and_gets_sidecars(tmp_path, monkeypatch):
    (tmp_path / "w").mkdir()
    linux = fake_embedded_zip(tmp_path / "w", "linux-x86_64")
    monkeypatch.setattr(export, "build_linux", lambda root, work, args: linux)
    monkeypatch.setattr(export, "project_version", lambda root: "0.1.0")
    args = export.argparse.Namespace(
        outdir=str(tmp_path / "out"), repo=str(REPO), ref=export.WORKTREE, windows_zip=None, linux_zip=None, linux_python=None,
        embed_zip=None, embed_sig=None, skip_pgp=False, which="linux", label="", no_verify=True, allow_license_gaps=False)
    (final, _, sbom), = export.build_all(args)
    assert final.name == linux.name and sbom.exists() and "part:     linux" in Path(str(final) + ".buildinfo").read_text()
