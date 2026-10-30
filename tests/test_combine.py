"""Tests for scripts/combine_installers.py and for install.py choosing a build inside a combined zip."""

import hashlib
import importlib.util
import json
import stat
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


combine = load("combine_installers")
install = load("install")

INSTALLER = b"# the installer\n"


def make_zip(folder, platform, kind="embedded", python_tag=None, python="3.11", version="0.1.0", git="abc1234",
             installer=INSTALLER, root=None):
    """A stand-in for one per-platform zip written by build_embedded.py or build_installer.py."""
    info = {"name": "lattice-diamond-mcp", "version": version, "kind": kind, "platform": platform, "git": git}
    if kind == "embedded":
        info.update(python="3.12.10", python_tag="cp312")
        name = "lattice-diamond-mcp-%s-embedded-%s.zip" % (version, platform)
    else:
        info.update(python=python, python_tag=python_tag or "cp" + python.replace(".", ""), min_glibc="2.28" if platform.startswith("linux") else None)
        name = "lattice-diamond-mcp-%s-%s-%s.zip" % (version, info["python_tag"], platform)
    root = root or "lattice-diamond-mcp-" + version
    path = folder / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        def add(member, data, mode=0o644):
            entry = zipfile.ZipInfo("%s/%s" % (root, member), date_time=(2026, 10, 26, 1, 2, 3))
            entry.external_attr = mode << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, data)
        add("BUILDINFO", json.dumps(info) + "\n")
        add("install.py", installer, 0o755)
        add("LICENSE", "licence text\n")
        if kind == "embedded":
            add("python/" + ("python.exe" if platform.startswith("windows") else "bin/python3.12"), b"\x7fELF" + platform.encode(), 0o755)
            add("python/lib/module.so", b"library " + platform.encode())
        else:
            add("requirements.txt", "pkg==1 --hash=sha256:00\n")
            add("wheels/pkg-1-py3-none-any.whl", b"wheel " + platform.encode())
    return path


@pytest.fixture
def inputs(tmp_path):
    folder = tmp_path / "in"
    folder.mkdir()
    return folder


def names(path):
    with zipfile.ZipFile(path) as archive:
        return sorted(archive.namelist())


# ------------------------------------------------------------------ combining

def test_embedded_zips_become_one_zip_with_a_folder_per_platform(inputs, tmp_path):
    zips = [make_zip(inputs, "windows-x86_64"), make_zip(inputs, "linux-x86_64")]
    out = tmp_path / "out"
    assert combine.main(["--kind", "embedded", "--outdir", str(out)] + [str(z) for z in zips]) == 0
    target = out / "lattice-diamond-mcp-0.1.0-embedded-windows-linux-x86_64.zip"
    root = "lattice-diamond-mcp-0.1.0/"
    assert names(target) == sorted(root + n for n in [
        "install.py", "PACKAGES.txt",
        "linux-x86_64/BUILDINFO", "linux-x86_64/LICENSE", "linux-x86_64/python/bin/python3.12",
        "linux-x86_64/python/lib/module.so",
        "windows-x86_64/BUILDINFO", "windows-x86_64/LICENSE", "windows-x86_64/python/python.exe",
        "windows-x86_64/python/lib/module.so"])


def test_the_contents_of_each_build_are_unchanged_and_permissions_survive(inputs, tmp_path):
    zips = [make_zip(inputs, "windows-x86_64"), make_zip(inputs, "linux-x86_64")]
    combine.main(["--kind", "embedded", "--outdir", str(tmp_path), str(zips[0]), str(zips[1])])
    target = next(tmp_path.glob("*.zip"))
    with zipfile.ZipFile(target) as combined, zipfile.ZipFile(zips[1]) as original:
        root = "lattice-diamond-mcp-0.1.0/"
        for member in original.infolist():
            if member.filename.endswith("/install.py"):
                continue
            moved = combined.getinfo(root + "linux-x86_64/" + member.filename.split("/", 1)[1])
            assert combined.read(moved) == original.read(member)
            assert moved.external_attr == member.external_attr and moved.date_time == member.date_time
        program = combined.getinfo(root + "linux-x86_64/python/bin/python3.12")
        assert stat.S_IMODE(program.external_attr >> 16) == 0o755
        assert stat.S_IMODE(combined.getinfo(root + "install.py").external_attr >> 16) == 0o755
        assert combined.read(root + "install.py") == INSTALLER


def test_the_checksum_file_is_in_sha256sum_format_with_unix_line_ends(inputs, tmp_path):
    zips = [make_zip(inputs, "windows-x86_64"), make_zip(inputs, "linux-x86_64")]
    combine.main(["--kind", "embedded", "--outdir", str(tmp_path), str(zips[0]), str(zips[1])])
    target = next(tmp_path.glob("*.zip"))
    raw = (tmp_path / (target.name + ".sha256")).read_bytes()
    assert raw == ("%s  %s\n" % (hashlib.sha256(target.read_bytes()).hexdigest(), target.name)).encode()


def test_wheels_get_a_folder_for_each_system_and_python(inputs, tmp_path):
    zips = [make_zip(inputs, platform, kind="wheels", python=python)
            for platform in ("windows-x86_64", "linux-x86_64") for python in ("3.11", "3.12")]
    assert combine.main(["--kind", "wheels", "--outdir", str(tmp_path)] + [str(z) for z in zips]) == 0
    target = tmp_path / "lattice-diamond-mcp-0.1.0-wheels-windows-linux-cp311-cp312.zip"
    folders = {n.split("/")[1] for n in names(target) if n.count("/") > 1}
    assert folders == {"linux-x86_64-cp311", "linux-x86_64-cp312", "windows-x86_64-cp311", "windows-x86_64-cp312"}
    with zipfile.ZipFile(target) as archive:
        index = archive.read("lattice-diamond-mcp-0.1.0/PACKAGES.txt").decode()
    assert "linux-x86_64-cp312/" in index and "Python 3.12" in index and "glibc 2.28" in index


def refuse(tmp_path, kind, zips, message):
    out = tmp_path / "out"
    assert combine.main(["--kind", kind, "--outdir", str(out)] + [str(z) for z in zips]) == 1
    assert not list(out.glob("*")) if out.exists() else True, "nothing may be written when the inputs do not belong together"


def test_a_missing_operating_system_is_refused(inputs, tmp_path, capsys):
    refuse(tmp_path, "embedded", [make_zip(inputs, "linux-x86_64")], "")
    assert "Nothing for windows" in capsys.readouterr().err


@pytest.mark.parametrize("field, other", [("version", {"version": "0.2.0"}), ("git", {"git": "def5678"})])
def test_builds_of_different_versions_or_commits_are_refused(inputs, tmp_path, capsys, field, other):
    first = make_zip(inputs, "linux-x86_64")
    second = make_zip(inputs, "windows-x86_64", **other)
    refuse(tmp_path, "embedded", [first, second], "")
    assert "differ in " + field in capsys.readouterr().err


def test_different_installers_are_refused(inputs, tmp_path, capsys):
    refuse(tmp_path, "embedded", [make_zip(inputs, "linux-x86_64"), make_zip(inputs, "windows-x86_64", installer=b"# other\n")], "")
    assert "different install.py" in capsys.readouterr().err


def test_installers_that_differ_only_in_line_endings_are_explained(inputs, tmp_path, capsys):
    """What happened in CI: the Windows checkout converted install.py to CRLF."""
    refuse(tmp_path, "embedded", [make_zip(inputs, "linux-x86_64", installer=b"# one\n# two\n"),
                                    make_zip(inputs, "windows-x86_64", installer=b"# one\r\n# two\r\n")], "")
    err = capsys.readouterr().err
    assert "differ only in line endings" in err and ".gitattributes" in err and "SHA-256" in err


def test_the_repository_pins_line_endings_so_every_checkout_has_the_same_bytes():
    attributes = (SCRIPTS.parent / ".gitattributes").read_text()
    assert "* text=auto eol=lf" in attributes
    for binary in ("*.pdf", "*.zip", "*.whl", "*.exe"):
        assert binary + " binary" in attributes


def test_the_same_build_twice_is_refused(inputs, tmp_path, capsys):
    first = make_zip(inputs, "linux-x86_64")
    copy = inputs / "copy.zip"
    copy.write_bytes(first.read_bytes())
    refuse(tmp_path, "embedded", [first, copy, make_zip(inputs, "windows-x86_64")], "")
    assert "both the build for linux-x86_64" in capsys.readouterr().err


def test_the_wrong_kind_is_refused(inputs, tmp_path, capsys):
    refuse(tmp_path, "wheels", [make_zip(inputs, "linux-x86_64"), make_zip(inputs, "windows-x86_64")], "")
    assert "is an embedded zip, not a wheels zip" in capsys.readouterr().err


def test_things_that_are_not_installer_zips_are_refused(inputs, tmp_path, capsys):
    broken = inputs / "broken.zip"
    broken.write_bytes(b"not a zip")
    refuse(tmp_path, "embedded", [broken], "")
    assert "not a readable zip" in capsys.readouterr().err
    other = inputs / "other.zip"
    with zipfile.ZipFile(other, "w") as archive:
        archive.writestr("root/readme.txt", "x")
    refuse(tmp_path, "embedded", [other], "")
    assert "no BUILDINFO" in capsys.readouterr().err
    two_roots = inputs / "two.zip"
    with zipfile.ZipFile(two_roots, "w") as archive:
        archive.writestr("a/BUILDINFO", "{}")
        archive.writestr("b/BUILDINFO", "{}")
    refuse(tmp_path, "embedded", [two_roots], "")
    assert "exactly one top-level folder" in capsys.readouterr().err


# ------------------------------------------- install.py choosing inside a combined zip

def lay_out(tmp_path, builds):
    """Write a combined-zip folder: install.py's directory with one sub-folder (holding BUILDINFO) per build."""
    for folder, info in builds.items():
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "BUILDINFO").write_text(json.dumps(info))
    return tmp_path


def embedded(platform):
    return {"kind": "embedded", "platform": platform, "version": "0.1.0", "python": "3.12.10",
            "program": None if platform.startswith("windows") else "bin/python3.12"}


def wheels(platform, python):
    return {"kind": "wheels", "platform": platform, "python": python, "python_tag": "cp" + python.replace(".", ""), "version": "0.1.0"}


@pytest.fixture
def here(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "HERE", str(tmp_path))
    return tmp_path


def test_a_single_platform_zip_is_used_as_it_always_was(here):
    (here / "BUILDINFO").write_text(json.dumps(embedded("linux-x86_64")))
    folder, info = install.select_bundle()
    assert folder == str(here) and info["platform"] == "linux-x86_64"


@pytest.mark.parametrize("on_windows, expected", [(False, "linux-x86_64"), (True, "windows-x86_64")])
def test_a_combined_embedded_zip_gives_the_build_for_this_system(here, monkeypatch, on_windows, expected):
    lay_out(here, {"windows-x86_64": embedded("windows-x86_64"), "linux-x86_64": embedded("linux-x86_64")})
    monkeypatch.setattr(install, "IS_WINDOWS", on_windows)
    folder, info = install.select_bundle()
    assert Path(folder).name == expected and info["platform"] == expected


def test_a_zip_with_only_other_systems_is_refused_clearly(here, monkeypatch):
    lay_out(here, {"windows-x86_64": embedded("windows-x86_64")})
    monkeypatch.setattr(install, "IS_WINDOWS", False)
    with pytest.raises(install.InstallError, match="holds builds for windows-x86_64 but this machine is linux"):
        install.select_bundle()


def test_no_build_anywhere_reports_the_original_problem(here):
    with pytest.raises(install.InstallError, match="Cannot read BUILDINFO"):
        install.select_bundle()


def test_the_python_that_is_running_is_preferred(here, monkeypatch):
    lay_out(here, {"linux-x86_64-cp311": wheels("linux-x86_64", "3.11"), "linux-x86_64-cp312": wheels("linux-x86_64", "3.12"),
                   "linux-x86_64-cp313": wheels("linux-x86_64", "3.13")})
    monkeypatch.setattr(install, "IS_WINDOWS", False)
    monkeypatch.setattr(install, "find_python", lambda version, explicit=None: [sys.executable])
    running = "%d.%d" % sys.version_info[:2]
    folder, info = install.select_bundle()
    assert info["python"] == (running if running in ("3.11", "3.12", "3.13") else "3.13")


def test_the_newest_available_python_is_used_when_the_running_one_does_not_match(here, monkeypatch):
    lay_out(here, {"linux-x86_64-cp311": wheels("linux-x86_64", "3.11"), "linux-x86_64-cp312": wheels("linux-x86_64", "3.12")})
    monkeypatch.setattr(install, "IS_WINDOWS", False)
    monkeypatch.setattr(sys, "version_info", (3, 8, 5, "final", 0))
    available = {"3.11", "3.12"}

    def fake_find(version, explicit=None):
        if version not in available:
            raise install.InstallError("no python " + version)
        return ["python" + version]

    monkeypatch.setattr(install, "find_python", fake_find)
    assert install.select_bundle()[1]["python"] == "3.12"
    available.discard("3.12")
    assert install.select_bundle()[1]["python"] == "3.11"


def test_no_matching_python_lists_what_the_zip_holds(here, monkeypatch):
    lay_out(here, {"linux-x86_64-cp311": wheels("linux-x86_64", "3.11"), "linux-x86_64-cp312": wheels("linux-x86_64", "3.12")})
    monkeypatch.setattr(install, "IS_WINDOWS", False)
    monkeypatch.setattr(install, "find_python", lambda version, explicit=None: (_ for _ in ()).throw(install.InstallError("none")))
    with pytest.raises(install.InstallError, match=r"wheels for Python 3\.\d+, 3\.\d+, and none of those was found"):
        install.select_bundle()


def test_an_explicit_python_picks_the_matching_build(here, monkeypatch):
    lay_out(here, {"linux-x86_64-cp311": wheels("linux-x86_64", "3.11"), "linux-x86_64-cp312": wheels("linux-x86_64", "3.12")})
    monkeypatch.setattr(install, "IS_WINDOWS", False)
    monkeypatch.setattr(install, "python_version_of", lambda command: "3.12")
    assert install.select_bundle(python="/opt/py/bin/python")[1]["python"] == "3.12"
    monkeypatch.setattr(install, "python_version_of", lambda command: "3.9")
    with pytest.raises(install.InstallError, match="given with --python is not one of them"):
        install.select_bundle(python="/opt/py/bin/python")


def test_a_dry_run_from_a_combined_zip_names_the_build_it_chose(here, monkeypatch, capsys, tmp_path):
    lay_out(here, {"windows-x86_64": embedded("windows-x86_64"), "linux-x86_64": embedded("linux-x86_64")})
    monkeypatch.setattr(install, "IS_WINDOWS", False)
    target = tmp_path / "installed"
    assert install.main(["--dry-run", "--no-register", "--install-dir", str(target)]) == 0
    out = capsys.readouterr().out
    assert "several builds; using the one for linux-x86_64" in out and "[dry run] copy" in out
    assert str(here / "linux-x86_64" / "python") in out and not target.exists()
