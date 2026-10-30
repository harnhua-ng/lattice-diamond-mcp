"""Tests that span the packaging scripts: the product licence text, the checked-in interpreter notices, checksum files,
the default install folders, and the installer's handling of the retired standalone (binary) kind."""

import hashlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
REPO = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


embedded = load("build_embedded")
install = load("install")
posix_only = pytest.mark.skipif(os.name == "nt", reason="uses shell-script test doubles")


def test_the_repository_licence_is_the_unmodified_agreement_text():
    text = (REPO / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("AI SOLUTIONS SOFTWARE LICENSE AGREEMENT")
    # Its notice allows free distribution only with this line, and forbids changing the terms.
    assert "(c)2026 Lattice Semiconductor Corporation. You may freely distribute the text of this Agreement" in text
    assert "THIRD_PARTY_LICENSES.txt" in text  # section 4 points at the file both zips ship



def test_the_checked_in_notices_cover_every_library_the_manifest_names():
    for platform in ("linux-x86_64", "windows-x86_64"):
        have = {p.name for p in (embedded.NOTICES_DIR / platform).glob("LICENSE.*.txt")}
        assert have and all(p.stat().st_size > 20 for p in (embedded.NOTICES_DIR / platform).glob("LICENSE.*.txt"))   # liblzma's is one line: public domain
        assert "LICENSE.openssl-3.txt" in have and "LICENSE.zlib.txt" in have and "LICENSE.expat.txt" in have
    assert (embedded.NOTICES_DIR / "RELEASE").read_text().strip().isdigit()
    sources = (embedded.NOTICES_DIR / "SOURCES.txt").read_text()
    for platform in ("linux-x86_64", "windows-x86_64"):
        for path in (embedded.NOTICES_DIR / platform).glob("LICENSE.*.txt"):
            assert hashlib.sha256(path.read_bytes()).hexdigest() in sources, path.name



def windows_text_mode(monkeypatch):
    """Make Path.write_text behave as on Windows, where text mode turns \\n into \\r\\n."""
    original = Path.write_text

    def write_text(self, data, encoding=None, errors=None, newline=None):
        if newline is None:
            data = data.replace("\n", "\r\n")
        return original(self, data, encoding=encoding, errors=errors, newline="")

    monkeypatch.setattr(Path, "write_text", write_text)


@pytest.mark.parametrize("module_name", ["build_embedded", "build_installer"])
def test_checksum_file_has_unix_line_endings_even_where_text_mode_adds_carriage_returns(tmp_path, monkeypatch, module_name):
    # Regression: on Windows the .sha256 file was written as "<hash>  <name>\\r\\n", and `sha256sum -c`
    # (run by the CI verify jobs under Git Bash) then looked for a file whose name ends in a carriage return.
    windows_text_mode(monkeypatch)
    module = load(module_name)
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "BUILDINFO").write_text("{}")
    out = tmp_path / "out"
    out.mkdir()
    info = {"version": "0.1.0", "python": "3.11", "python_tag": "cp311", "platform": "linux-x86_64"}
    target, digest = module.write_zip(stage, out, info)
    checksum = out / (target.name + ".sha256")
    raw = checksum.read_bytes()
    assert b"\r" not in raw
    assert raw == ("%s  %s\n" % (digest, target.name)).encode()


def test_the_simulation_really_would_have_broken_the_old_behaviour(tmp_path, monkeypatch):
    windows_text_mode(monkeypatch)
    probe = tmp_path / "probe"
    probe.write_text("a\n", encoding="utf-8")
    assert probe.read_bytes() == b"a\r\n"



# ------------------------------------------- installer: the retired standalone (binary) kind

def test_a_zip_of_the_retired_standalone_kind_is_refused_with_a_pointer_to_the_embedded_package(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "IS_WINDOWS", False)
    args = install.parse_args(["--dry-run", "--no-register", "--install-dir", str(tmp_path / "t")])
    with pytest.raises(install.InstallError, match="no longer installs.*embedded package"):
        install.do_install(args, {"kind": "binary", "platform": "linux-x86_64", "version": "0.1.0"}, str(tmp_path))
    assert not (tmp_path / "t").exists()


def legacy_standalone(folder):
    """A folder as an earlier version left it: the standalone program and the installer's marker."""
    folder.mkdir(parents=True)
    program = folder / ("diamond-mcp.exe" if install.IS_WINDOWS else "diamond-mcp")
    program.write_text("#!/bin/sh\n")
    (folder / install.MARKER).write_text("Created by the lattice-diamond-mcp installer.\n")
    return program


@posix_only
def test_uninstall_removes_an_earlier_standalone_installation_and_its_registration(tmp_path, fake_claude):
    claude, state = fake_claude
    target = tmp_path / "diamond-mcp"
    program = legacy_standalone(target)
    state.write_text("lattice-diamond-mcp\t%s\nother\t/x\n" % program)
    install.do_uninstall(install.parse_args(["--uninstall", "--install-dir", str(target), "--claude-path", claude]))
    assert not target.exists() and state.read_text() == "other\t/x\n"


def test_uninstall_targets_follow_the_kind_of_installation(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "HERE", str(tmp_path))
    args = install.parse_args(["--uninstall"])
    (tmp_path / "BUILDINFO").write_text(json.dumps({"kind": "embedded"}))
    assert install.uninstall_targets(args) == [("embedded", install.default_embedded_dir()), ("binary", install.default_install_dir())]
    (tmp_path / "BUILDINFO").write_text(json.dumps({"kind": "wheels"}))
    assert install.uninstall_targets(args) == [("wheels", install.default_venv_dir()), ("wheels", install.previous_venv_dir())]
    (tmp_path / "BUILDINFO").unlink()
    assert [k for k, _ in install.uninstall_targets(args)] == ["embedded", "binary", "wheels", "wheels"]  # unknown: look everywhere
    legacy_standalone(tmp_path / "old")
    assert install.uninstall_targets(install.parse_args(["--uninstall", "--install-dir", str(tmp_path / "old")])) == [("binary", str(tmp_path / "old"))]


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).parent))
    import test_installer
    script = tmp_path / "claude"
    script.write_text(test_installer.FAKE_CLAUDE)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    state = tmp_path / "state"
    state.write_text("")
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(state))
    return str(script), state


def test_default_environment_is_in_the_per_user_data_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(install, "IS_WINDOWS", True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert install.default_venv_dir() == str(tmp_path / "Local" / "lattice-diamond-mcp" / "venv")
    monkeypatch.setattr(install, "IS_WINDOWS", False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    assert install.default_venv_dir() == str(tmp_path / "share" / "lattice-diamond-mcp" / "venv")
    monkeypatch.delenv("XDG_DATA_HOME")
    assert install.default_venv_dir().endswith(os.path.join(".local", "share", "lattice-diamond-mcp", "venv"))
