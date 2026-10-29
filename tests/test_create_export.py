"""Tests for scripts/create_export.py (the single entry point), and for MANIFEST.sha256 (written by the builders,
checked by install.py, carried through combine_installers.py)."""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


export = load("create_export")
install = load("install")
build_installer = load("build_installer")
combine = load("combine_installers")

INSTALLER = b"# the installer\n"
REQUIREMENTS = "pydantic==2.14.1 \\\n    --hash=sha256:" + "a" * 64 + "\nmcp==1.30.0 \\\n    --hash=sha256:" + "b" * 64 + "\n"
LICENCES = "Third-party packages\n\npydantic 2.14.1  [MIT]\n    licenses/LICENSE\nmcp 1.30.0  [licence field not stated]\n"
LINUX_SBOM = {"bomFormat": "CycloneDX", "specVersion": "1.5",
              "components": [{"type": "library", "bom-ref": "pkg:pypi/anyio@4.15.1", "name": "anyio", "version": "4.15.1"}]}


def fake_zip(folder, kind, platform, version="0.1.0", git="abc1234"):
    """A stand-in for a per-OS zip written by build_embedded.py (embedded) or build_binary.py (binary)."""
    info = {"name": "lattice-diamond-mcp", "version": version, "kind": kind, "platform": platform, "git": git}
    if kind == "embedded":
        info.update(python="3.13.17", python_tag="cp313", python_source="https://www.python.org/x.zip",
                    python_sha256="c" * 64, python_pgp_fingerprint="D" * 40, python_pgp_verified=True)
        name = "lattice-diamond-mcp-%s-embedded-%s.zip" % (version, platform)
    else:
        name = "lattice-diamond-mcp-%s-%s.zip" % (version, platform)
    path = folder / name
    root = "lattice-diamond-mcp-" + version
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(root + "/BUILDINFO", json.dumps(info))
        archive.writestr(root + "/install.py", INSTALLER)
        if kind == "embedded":
            archive.writestr(root + "/python/python.exe", b"x")
            archive.writestr(root + "/requirements.txt", REQUIREMENTS)
            archive.writestr(root + "/THIRD_PARTY_LICENSES.txt", LICENCES)
        else:
            archive.writestr(root + "/diamond-mcp/diamond-mcp", b"x")
            archive.writestr(root + "/sbom.cdx.json", json.dumps(LINUX_SBOM))
    return path


# ------------------------------------------------------------------------- the command line

def run_cli(*args):
    return subprocess.run([sys.executable, str(SCRIPTS / "create_export.py")] + list(args), capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=120)


def test_help_describes_the_modes_the_hybrid_package_and_the_sidecars():
    result = run_cli("--help")
    assert result.returncode == 0
    for needle in ("--windows", "--linux", "--both", "--ref", "--label", "--no-verify", "hybrid", ".sbom.cdx.json", ".buildinfo"):
        assert needle in result.stdout, needle


def test_a_mode_is_required_and_the_modes_exclude_each_other():
    assert run_cli().returncode == 2
    assert run_cli("--windows", "--both").returncode == 2


def test_a_label_may_hold_only_safe_characters():
    result = run_cli("--windows", "--label", "bad/label")
    assert result.returncode == 2 and "--label may contain only" in result.stderr


def test_the_linux_program_cannot_be_built_on_windows_so_both_needs_a_linux_zip(monkeypatch, capsys):
    monkeypatch.setattr(export, "IS_LINUX", False)
    monkeypatch.setattr(sys, "argv", ["create_export.py", "--both"])
    with pytest.raises(SystemExit):
        export.main()
    assert "--linux-zip" in capsys.readouterr().err


def test_prebuilt_zips_are_only_for_both(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["create_export.py", "--windows", "--linux-zip", "x.zip"])
    with pytest.raises(SystemExit):
        export.main()
    assert "for --both" in capsys.readouterr().err


# --------------------------------------------------------------------------------- refs

def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.invalid", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)

    def git(*args):
        subprocess.run(["git", "-C", str(repo)] + list(args), check=True, capture_output=True, env=env)
    git("init", "-q")
    (repo / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.0.0"\n')
    (repo / "scripts").mkdir()
    (repo / "scripts" / "a.txt").write_text("committed")
    os.symlink("/etc/passwd", repo / "link")
    git("add", ".")
    git("commit", "-q", "-m", "one")
    git("tag", "v1")
    (repo / "scripts" / "a.txt").write_text("edited after the tag")
    return repo


@pytest.mark.skipif(os.name == "nt", reason="uses a symbolic link")
def test_a_ref_is_read_from_git_not_from_the_working_tree_and_links_are_not_followed(tmp_path):
    repo = make_repo(tmp_path)
    root = export.export_repo(repo, "v1", tmp_path / "out")
    assert (root / "scripts" / "a.txt").read_text() == "committed"
    assert not (root / "link").exists() and not (root / "link").is_symlink()
    assert export.project_version(root) == "1.0.0"
    assert export.export_repo(repo, export.WORKTREE, tmp_path / "unused") == repo


@pytest.mark.skipif(os.name == "nt", reason="uses a symbolic link")
def test_the_commit_is_recorded_and_uncommitted_changes_are_flagged_for_the_working_tree_only(tmp_path):
    repo = make_repo(tmp_path)
    assert export.commit_of(repo, "v1") == export.commit_of(repo, "v1").split("+")[0]
    assert export.commit_of(repo, export.WORKTREE).endswith("+uncommitted-changes")
    with pytest.raises(export.ExportError):
        export.export_repo(repo, "no-such-ref", tmp_path / "bad")


def test_a_label_goes_after_the_version_and_the_checksum_follows(tmp_path):
    path = fake_zip(tmp_path, "embedded", "windows-x86_64")
    path.with_name(path.name + ".sha256").write_text("old  name\n")
    new = export.labelled(path, "0.1.0", "rc1")
    assert new.name == "lattice-diamond-mcp-0.1.0-rc1-embedded-windows-x86_64.zip" and new.exists() and not path.exists()
    assert Path(str(new) + ".sha256").read_text() == "%s  %s\n" % (hashlib.sha256(new.read_bytes()).hexdigest(), new.name)
    assert export.labelled(new, "0.1.0", "") == new


# --------------------------------------------------------------------------- the checks

def test_a_package_without_a_licence_text_fails_the_build_unless_allowed(capsys):
    output = "built\n" + export.LICENCE_GAP_MARK + " odd 1. Check before release.\n"
    with pytest.raises(export.ExportError, match="cannot ship"):
        export.check_licence_gaps(output, False, "build_embedded.py")
    export.check_licence_gaps(output, True, "build_embedded.py")
    assert "[warn]" in capsys.readouterr().out
    export.check_licence_gaps("all fine\n", False, "build_embedded.py")


def test_a_prebuilt_zip_must_exist_and_match_its_checksum(tmp_path):
    path = fake_zip(tmp_path, "binary", "linux-x86_64")
    assert export.prebuilt(path, "--linux-zip") == path
    Path(str(path) + ".sha256").write_text("%s  x\n" % ("0" * 64))
    with pytest.raises(export.ExportError, match="does not match"):
        export.prebuilt(path, "--linux-zip")
    with pytest.raises(export.ExportError, match="does not exist"):
        export.prebuilt(tmp_path / "missing.zip", "--linux-zip")


# ---------------------------------------------------------------------------------- SBOM

def test_requirements_and_licence_fields_are_read_from_the_package_records():
    assert export.parse_requirements(REQUIREMENTS) == [("pydantic", "2.14.1", "a" * 64), ("mcp", "1.30.0", "b" * 64)]
    assert export.licence_fields(LICENCES) == {"pydantic": "MIT"}


def test_the_windows_components_are_the_interpreter_and_every_package(tmp_path):
    components = export.embedded_components(fake_zip(tmp_path, "embedded", "windows-x86_64"))
    names = {c["name"]: c for c in components}
    assert set(names) == {"cpython", "pydantic", "mcp"}
    assert names["cpython"]["hashes"][0]["content"] == "c" * 64
    assert {"name": "lattice:openpgp-verified", "value": "true"} in names["cpython"]["properties"]
    assert names["pydantic"]["licenses"] == [{"license": {"name": "MIT"}}] and "licenses" not in names["mcp"]
    assert all(c["bom-ref"].endswith("?platform=windows") for c in components)


def test_the_hybrid_sbom_holds_both_operating_systems_with_unique_references(tmp_path):
    windows, linux = fake_zip(tmp_path, "embedded", "windows-x86_64"), fake_zip(tmp_path, "binary", "linux-x86_64")
    zip_path = tmp_path / "lattice-diamond-mcp-0.1.0-hybrid-windows-linux-x86_64.zip"
    zip_path.write_bytes(b"zip")
    out = export.write_sbom(zip_path, "e" * 64, "0.1.0", "2026-10-29T00:00:00Z", "abc1234", [("windows", windows), ("linux", linux)])
    bom = json.loads(out.read_text())
    refs = [c["bom-ref"] for c in bom["components"]]
    assert len(refs) == len(set(refs)) and out.name == zip_path.name + ".sbom.cdx.json"
    platforms = {c["name"]: [p["value"] for p in c["properties"] if p["name"] == "lattice:platform"] for c in bom["components"]}
    assert platforms["anyio"] == ["linux"] and platforms["cpython"] == ["windows"]
    assert bom["metadata"]["component"]["hashes"][0]["content"] == "e" * 64 and bom["bomFormat"] == "CycloneDX"


# ------------------------------------------------------------------ the whole orchestration

def test_both_builds_the_hybrid_package_with_its_sidecars_and_a_label(tmp_path, monkeypatch):
    windows, linux = fake_zip(tmp_path, "embedded", "windows-x86_64"), fake_zip(tmp_path, "binary", "linux-x86_64")
    args = export.argparse.Namespace(
        outdir=str(tmp_path / "out"), repo=str(SCRIPTS.parent), ref=export.WORKTREE, windows_zip=str(windows), linux_zip=str(linux),
        embed_zip=None, embed_sig=None, skip_pgp=False, which="both", label="rc1", no_verify=True, allow_license_gaps=False)
    monkeypatch.setattr(export, "project_version", lambda root: "0.1.0")
    (final, digest, sbom), = export.build_all(args)
    assert final.name == "lattice-diamond-mcp-0.1.0-rc1-hybrid-windows-linux-x86_64.zip"
    assert Path(str(final) + ".sha256").read_text() == "%s  %s\n" % (digest, final.name) == "%s  %s\n" % (hashlib.sha256(final.read_bytes()).hexdigest(), final.name)
    assert sbom.exists() and "windows-x86_64/" in "".join(zipfile.ZipFile(final).namelist())
    record = Path(str(final) + ".buildinfo").read_text()
    assert "NOT shipped" in record and "part:     windows lattice-diamond-mcp-0.1.0-embedded-windows-x86_64.zip" in record
    assert "part:     linux" in record and "source:   WORKTREE" in record


def test_one_half_alone_is_copied_labelled_and_given_sidecars(tmp_path, monkeypatch):
    (tmp_path / "w").mkdir()
    windows = fake_zip(tmp_path / "w", "embedded", "windows-x86_64")
    monkeypatch.setattr(export, "build_windows", lambda root, work, args: windows)
    monkeypatch.setattr(export, "project_version", lambda root: "0.1.0")
    args = export.argparse.Namespace(
        outdir=str(tmp_path / "out"), repo=str(SCRIPTS.parent), ref=export.WORKTREE, windows_zip=None, linux_zip=None,
        embed_zip=None, embed_sig=None, skip_pgp=True, which="windows", label="", no_verify=True, allow_license_gaps=False)
    (final, digest, sbom), = export.build_all(args)
    assert final.name == windows.name and final.parent == (tmp_path / "out")
    assert "SKIPPED" in Path(str(final) + ".buildinfo").read_text()


def test_verifying_on_the_wrong_system_says_so_instead_of_pretending(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(export, "IS_WINDOWS", False)
    assert export.verify_zip(fake_zip(tmp_path, "embedded", "windows-x86_64")) is False
    assert "NOT run" in capsys.readouterr().out


# ----------------------------------------------------------------------------- MANIFEST.sha256

def test_the_builders_write_a_manifest_of_every_other_file(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_bytes(b"alpha")
    (tmp_path / "sub" / "b.txt").write_bytes(b"beta")
    assert build_installer.write_manifest(tmp_path) == 2
    lines = (tmp_path / "MANIFEST.sha256").read_bytes().decode().splitlines()
    assert lines == ["%s  a.txt" % hashlib.sha256(b"alpha").hexdigest(), "%s  sub/b.txt" % hashlib.sha256(b"beta").hexdigest()]
    assert b"\r" not in (tmp_path / "MANIFEST.sha256").read_bytes()


def manifest_folder(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_bytes(b"alpha")
    (tmp_path / "sub" / "b.txt").write_bytes(b"beta")
    build_installer.write_manifest(tmp_path)
    return tmp_path


def test_install_checks_the_manifest_and_reports_no_manifest_as_none(tmp_path):
    assert install.verify_manifest(str(tmp_path)) is None
    assert install.verify_manifest(str(manifest_folder(tmp_path))) == 2


def test_a_changed_or_missing_file_stops_the_install_before_anything_is_copied(tmp_path):
    folder = manifest_folder(tmp_path)
    (folder / "a.txt").write_bytes(b"tampered")
    (folder / "sub" / "b.txt").unlink()
    with pytest.raises(install.InstallError, match="2 of 2 files do not match") as caught:
        install.verify_manifest(str(folder))
    assert "changed: a.txt" in str(caught.value) and "missing or unreadable: sub/b.txt" in str(caught.value)
    assert "nothing was installed" in str(caught.value)


def test_a_manifest_line_that_climbs_out_of_the_folder_is_refused(tmp_path):
    (tmp_path / "MANIFEST.sha256").write_text("%s  ../outside.txt\n" % ("0" * 64))
    with pytest.raises(install.InstallError, match="unreadable line"):
        install.verify_manifest(str(tmp_path))


def test_do_install_checks_the_manifest_first(tmp_path, monkeypatch):
    folder = manifest_folder(tmp_path)
    (folder / "a.txt").write_bytes(b"tampered")
    monkeypatch.setattr(install, "IS_WINDOWS", False)
    args = install.parse_args(["--dry-run", "--no-register", "--install-dir", str(tmp_path / "t")])
    with pytest.raises(install.InstallError, match="damaged or has been changed"):
        install.do_install(args, {"kind": "binary", "platform": "linux-x86_64", "version": "0.1.0"}, str(folder))


def test_combining_drops_the_install_py_line_from_each_folders_manifest(tmp_path):
    windows, linux = fake_zip(tmp_path, "embedded", "windows-x86_64"), fake_zip(tmp_path, "binary", "linux-x86_64")
    for path in (windows, linux):                       # give each stand-in a manifest that lists install.py, as the builders' do
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr("lattice-diamond-mcp-0.1.0/MANIFEST.sha256", "%s  install.py\n%s  BUILDINFO\n" % ("1" * 64, "2" * 64))
    out = tmp_path / "out"
    assert combine.main(["--kind", "standalone", "--outdir", str(out), str(windows), str(linux)]) == 0
    with zipfile.ZipFile(next(out.glob("*.zip"))) as archive:
        for folder in ("windows-x86_64", "linux-x86_64"):
            text = archive.read("lattice-diamond-mcp-0.1.0/%s/MANIFEST.sha256" % folder).decode()
            assert "install.py" not in text and "BUILDINFO" in text
