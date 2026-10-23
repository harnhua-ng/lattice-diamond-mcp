"""Tests for scripts/install.py and scripts/build_installer.py."""

import importlib.util
import json
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


install = load("install")
build = load("build_installer")

posix_only = pytest.mark.skipif(os.name == "nt", reason="uses shell-script test doubles")


# ------------------------------------------------------------------ build side

def test_parse_wheel_name():
    assert build.parse_wheel_name("pydantic_core-2.46.3-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl") == (
        "pydantic_core", "2.46.3", ["manylinux_2_17_x86_64", "manylinux2014_x86_64"])
    assert build.parse_wheel_name("idna-3.10-py3-none-any.whl")[2] == ["any"]
    with pytest.raises(build.BuildError):
        build.parse_wheel_name("broken.whl")


@pytest.mark.parametrize(
    "tags, expected",
    [
        (["manylinux_2_28_x86_64"], (2, 28)),
        (["manylinux2014_x86_64"], (2, 17)),
        (["manylinux_2_17_x86_64", "manylinux2014_x86_64"], (2, 17)),
        (["manylinux2010_x86_64", "manylinux_2_28_x86_64"], (2, 12)),  # either tag works: lowest wins
        (["any"], None),
        (["win_amd64"], None),
    ],
)
def test_glibc_floor_of_one_wheel(tags, expected):
    assert build.glibc_floor(tags) == expected


def test_bundle_glibc_floor_is_the_highest_requirement():
    names = [
        "a-1-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
        "b-1-cp311-abi3-manylinux_2_28_x86_64.whl",
        "c-1-py3-none-any.whl",
    ]
    assert build.bundle_glibc_floor(names) == (2, 28)
    assert build.bundle_glibc_floor(["c-1-py3-none-any.whl"]) is None


def test_constraints_are_sorted_normalised_and_exact():
    text = build.constraints_text([
        "Pydantic_Core-2.46.3-cp311-cp311-manylinux_2_17_x86_64.whl",
        "anyio-4.15.1-py3-none-any.whl",
    ])
    assert text == "anyio==4.15.1\npydantic-core==2.46.3\n"


def make_wheel(folder, filename, files):
    path = folder / filename
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return path


def test_extract_licences_copies_files_and_flags_wheels_without_any(tmp_path):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    make_wheel(wheels, "lattice_diamond_mcp-0.1.0-py3-none-any.whl", {"lattice_diamond_mcp-0.1.0.dist-info/METADATA": "Name: x\n"})
    make_wheel(wheels, "with_licence-1.2-py3-none-any.whl", {
        "with_licence-1.2.dist-info/METADATA": "Name: with-licence\nLicense-Expression: MIT\n\nbody",
        "with_licence-1.2.dist-info/licenses/LICENSE": "MIT text",
        "with_licence-1.2.dist-info/licenses/NOTICE.txt": "notice",
        "with_licence/__init__.py": "",
    })
    make_wheel(wheels, "no_licence-9-py3-none-any.whl", {"no_licence-9.dist-info/METADATA": "Name: no-licence\n"})
    out = tmp_path / "out"
    index, missing = build.extract_licences(wheels, out, "lattice_diamond_mcp-")
    assert missing == ["no_licence 9"]
    assert (out / "with-licence-1.2" / "licenses" / "LICENSE").read_text() == "MIT text"
    assert (out / "with-licence-1.2" / "licenses" / "NOTICE.txt").exists()
    assert "with_licence 1.2  [MIT]" in index and "(no licence file in the wheel)" in index
    assert "lattice_diamond_mcp" not in index  # our own package is not listed


def test_project_version_matches_pyproject():
    assert build.project_version() == "0.1.0"


# ----------------------------------------------------------------- install side

def info(**overrides):
    base = {"version": "0.1.0", "python": "%d.%d" % sys.version_info[:2], "platform": "linux-x86_64", "min_glibc": None}
    base.update(overrides)
    return base


@pytest.mark.skipif(os.name == "nt", reason="Linux platform check")
def test_check_platform_rejects_the_wrong_operating_system():
    with pytest.raises(install.InstallError, match="built for windows"):
        install.check_platform(info(platform="windows-x86_64"))


@pytest.mark.skipif(os.name == "nt", reason="glibc check is Linux only")
def test_check_platform_rejects_an_old_glibc():
    with pytest.raises(install.InstallError, match="glibc 99.0"):
        install.check_platform(info(min_glibc="99.0"))
    install.check_platform(info(min_glibc="2.17"))  # any real host is at least this new
    install.check_platform(info(min_glibc=None))


def test_find_python_accepts_an_explicit_matching_interpreter():
    wanted = "%d.%d" % sys.version_info[:2]
    assert install.find_python(wanted, sys.executable) == [sys.executable]


def test_find_python_rejects_an_explicit_interpreter_of_the_wrong_version():
    with pytest.raises(install.InstallError, match="needs Python 2.7"):
        install.find_python("2.7", sys.executable)
    with pytest.raises(install.InstallError, match="not runnable"):
        install.find_python("3.11", "/no/such/python")


def test_find_python_searches_and_explains_when_nothing_matches():
    with pytest.raises(install.InstallError) as excinfo:
        install.find_python("2.7")
    assert "needs Python 2.7" in str(excinfo.value) and "looked at" in str(excinfo.value)


def test_load_buildinfo_reports_a_missing_file_clearly(tmp_path):
    with pytest.raises(install.InstallError, match="BUILDINFO"):
        install.load_buildinfo(str(tmp_path))
    (tmp_path / "BUILDINFO").write_text(json.dumps({"version": "1"}))
    assert install.load_buildinfo(str(tmp_path))["version"] == "1"


def test_prepare_venv_refuses_a_folder_the_installer_did_not_create(tmp_path):
    foreign = tmp_path / "somebody-elses"
    foreign.mkdir()
    (foreign / "precious.txt").write_text("keep")
    with pytest.raises(install.InstallError, match="not created by this installer"):
        install.prepare_venv(str(foreign), [sys.executable], dry_run=False)
    assert (foreign / "precious.txt").exists()


def test_prepare_venv_reuses_a_marked_environment(tmp_path, capsys):
    venv = tmp_path / "venv"
    venv.mkdir()
    (venv / install.MARKER).write_text("x")
    install.prepare_venv(str(venv), [sys.executable], dry_run=False)
    assert "Reusing" in capsys.readouterr().out


def test_prepare_venv_dry_run_creates_nothing(tmp_path):
    venv = tmp_path / "venv"
    install.prepare_venv(str(venv), [sys.executable], dry_run=True)
    assert not venv.exists()


# ----------------------------------------------------- claude registration (fake)

FAKE_CLAUDE = r"""#!/bin/bash
# Test double for `claude mcp`: keeps registered servers in $FAKE_CLAUDE_STATE (one "name<TAB>command" per line).
echo "$@" >> "$FAKE_CLAUDE_STATE.calls"
[ "$1" = "mcp" ] || exit 2
shift
sub="$1"; shift
case "$sub" in
  get)
    line=$(grep -P "^$1\t" "$FAKE_CLAUDE_STATE" 2>/dev/null) || exit 1
    echo "$line"; exit 0 ;;
  add)
    args=("$@"); name=""; i=0
    while [ $i -lt ${#args[@]} ]; do
      case "${args[$i]}" in
        --scope|-s|-e|--env) i=$((i+2)) ;;     # -e swallows the next value, like the real CLI
        --) cmd="${args[$((i+1))]}"; break ;;
        *) [ -z "$name" ] && name="${args[$i]}"; i=$((i+1)) ;;
      esac
    done
    [ -z "$cmd" ] && { echo "error: missing required argument 'commandOrUrl'" >&2; exit 1; }
    printf '%s\t%s\n' "$name" "$cmd" >> "$FAKE_CLAUDE_STATE"; exit 0 ;;
  remove)
    grep -vP "^$1\t" "$FAKE_CLAUDE_STATE" > "$FAKE_CLAUDE_STATE.tmp"; mv "$FAKE_CLAUDE_STATE.tmp" "$FAKE_CLAUDE_STATE"; exit 0 ;;
esac
exit 2
"""


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    script = tmp_path / "claude"
    script.write_text(FAKE_CLAUDE)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    state = tmp_path / "state"
    state.write_text("")
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(state))
    return str(script), state


@posix_only
def test_register_puts_the_name_before_the_env_option(fake_claude):
    claude, state = fake_claude
    install.register(claude, "diamond", "user", "/venv/bin/diamond-mcp", "/opt/dc/diamondc", dry_run=False)
    assert state.read_text() == "diamond\t/venv/bin/diamond-mcp\n"
    call = Path(str(state) + ".calls").read_text().splitlines()[-1].split()
    assert call.index("diamond") < call.index("-e") < call.index("--")  # name, then -e, then --


@posix_only
def test_register_is_idempotent(fake_claude, capsys):
    claude, state = fake_claude
    install.register(claude, "diamond", "user", "/venv/bin/diamond-mcp", None, False)
    install.register(claude, "diamond", "user", "/venv/bin/diamond-mcp", None, False)
    assert state.read_text().count("diamond\t") == 1
    assert "Already registered" in capsys.readouterr().out


@posix_only
def test_register_will_not_overwrite_someone_elses_entry(fake_claude):
    claude, state = fake_claude
    state.write_text("diamond\t/some/other/server\n")
    with pytest.raises(install.InstallError, match="already registered"):
        install.register(claude, "diamond", "user", "/venv/bin/diamond-mcp", None, False)
    assert state.read_text() == "diamond\t/some/other/server\n"


@posix_only
def test_unregister_removes_only_our_entry(fake_claude, capsys):
    claude, state = fake_claude
    state.write_text("diamond\t/venv/bin/diamond-mcp\nother\t/x\n")
    install.unregister(claude, "diamond", "user", "/venv/bin/diamond-mcp", False)
    assert state.read_text() == "other\t/x\n"
    state.write_text("diamond\t/not/ours\n")
    install.unregister(claude, "diamond", "user", "/venv/bin/diamond-mcp", False)
    assert state.read_text() == "diamond\t/not/ours\n"
    assert "not from this installation" in capsys.readouterr().out


@posix_only
def test_uninstall_removes_registration_and_marked_environment(fake_claude, tmp_path):
    claude, state = fake_claude
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / install.MARKER).write_text("x")
    state.write_text("diamond\t%s\n" % install.venv_server(str(venv)))
    args = install.parse_args(["--uninstall", "--venv-dir", str(venv), "--claude-path", claude])
    install.do_uninstall(args)
    assert not venv.exists() and state.read_text() == ""


@posix_only
def test_uninstall_leaves_a_foreign_folder_alone(fake_claude, tmp_path):
    claude, _ = fake_claude
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    args = install.parse_args(["--uninstall", "--venv-dir", str(foreign), "--claude-path", claude])
    with pytest.raises(install.InstallError, match="left alone"):
        install.do_uninstall(args)
    assert foreign.exists()


@posix_only
def test_uninstall_dry_run_changes_nothing(fake_claude, tmp_path):
    claude, state = fake_claude
    venv = tmp_path / "venv"
    venv.mkdir()
    (venv / install.MARKER).write_text("x")
    state.write_text("diamond\t%s\n" % install.venv_server(str(venv)))
    install.do_uninstall(install.parse_args(["--uninstall", "--dry-run", "--venv-dir", str(venv), "--claude-path", claude]))
    assert venv.exists() and "diamond" in state.read_text()


def test_find_claude_prefers_the_explicit_path(tmp_path):
    exe = tmp_path / "claude"
    exe.write_text("")
    assert install.find_claude(str(exe)) == str(exe)
    assert install.find_claude(str(tmp_path / "missing")) is None


def test_main_reports_errors_without_a_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(install, "HERE", str(tmp_path))
    monkeypatch.setattr(install, "load_buildinfo", lambda folder=None: (_ for _ in ()).throw(install.InstallError("no info")))
    assert install.main(["--venv-dir", str(tmp_path / "v")]) == 1
    err = capsys.readouterr().err
    assert err.startswith("\nERROR: no info") and "Traceback" not in err


def test_dry_run_install_touches_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(install, "load_buildinfo", lambda folder=None: info())
    venv = tmp_path / "venv"
    assert install.main(["--dry-run", "--no-register", "--venv-dir", str(venv)]) == 0
    out = capsys.readouterr().out
    assert "[dry run]" in out and "pip install --no-index" in out
    assert not venv.exists()


def test_installer_source_runs_on_old_pythons():
    # install.py must stay importable by the oldest interpreter that may launch it (3.8).
    import ast
    source = (SCRIPTS / "install.py").read_text()
    ast.parse(source, feature_version=(3, 8))


# ------------------------------------------------- end to end (needs a network)

@pytest.mark.skipif(not os.environ.get("DIAMOND_TEST_INSTALLER"), reason="set DIAMOND_TEST_INSTALLER=1 (downloads wheels)")
def test_build_and_install_the_real_bundle(tmp_path):
    out = tmp_path / "dist"
    done = subprocess.run([sys.executable, str(SCRIPTS / "build_installer.py"), "--outdir", str(out)],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert done.returncode == 0, done.stdout
    zips = list(out.glob("lattice-diamond-mcp-*.zip"))
    assert len(zips) == 1 and (out / (zips[0].name + ".sha256")).exists()
    with zipfile.ZipFile(zips[0]) as archive:
        archive.extractall(tmp_path / "unpacked")
    bundle = next((tmp_path / "unpacked").iterdir())
    assert (bundle / "install.py").exists() and (bundle / "THIRD_PARTY_LICENSES.txt").exists()
    assert (bundle / "LICENSE").read_bytes() == (SCRIPTS.parent / "LICENSE").read_bytes()
    venv = tmp_path / "venv"
    result = subprocess.run([sys.executable, str(bundle / "install.py"), "--no-register", "--venv-dir", str(venv)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env={k: v for k, v in os.environ.items() if k not in ("PIP_FIND_LINKS", "PIP_INDEX_URL")})
    assert result.returncode == 0, result.stdout
    server = install.venv_server(str(venv))
    assert "diamond-mcp" in subprocess.run([server, "--help"], stdout=subprocess.PIPE, text=True).stdout
    again = subprocess.run([sys.executable, str(bundle / "install.py"), "--uninstall", "--venv-dir", str(venv)],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert again.returncode == 0 and not venv.exists(), again.stdout
