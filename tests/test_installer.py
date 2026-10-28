"""Tests for scripts/install.py and scripts/build_installer.py."""

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import time
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


def test_lock_constraints_are_sorted_normalised_and_exact():
    locked = {("pydantic-core", "2.46.3"): {}, ("anyio", "4.15.1"): {}}
    assert build.lock_constraints_text(locked) == "anyio==4.15.1\npydantic-core==2.46.3\n"


def make_wheel(folder, filename, files):
    path = folder / filename
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return path


def lock_for(tmp_path, wheel):
    return {("dep", "1.0"): {wheel.name: build.sha256_file(wheel)}}


def test_the_real_lock_file_is_read_with_a_hash_for_every_wheel():
    locked = build.load_lock(SCRIPTS.parent / "uv.lock")
    assert any(name == "mcp" for name, _version in locked)
    assert all(hashes and all(len(x) == 64 for x in hashes.values()) for hashes in locked.values())
    assert ("lattice-diamond-mcp", "0.1.0") not in locked  # the project itself is built, not locked


def test_wheels_that_match_the_lock_pass(tmp_path):
    wheel = make_wheel(tmp_path, "dep-1.0-py3-none-any.whl", {"a": "1"})
    make_wheel(tmp_path, "lattice_diamond_mcp-0.1.0-py3-none-any.whl", {"b": "2"})  # own wheel is not in the lock
    build.check_wheels_against_lock(tmp_path, lock_for(tmp_path, wheel), "lattice_diamond_mcp-")


def test_a_wheel_changed_after_locking_stops_the_build(tmp_path):
    wheel = make_wheel(tmp_path, "dep-1.0-py3-none-any.whl", {"a": "1"})
    locked = lock_for(tmp_path, wheel)
    make_wheel(tmp_path, "dep-1.0-py3-none-any.whl", {"a": "tampered"})
    with pytest.raises(build.BuildError, match="SHA-256 differs"):
        build.check_wheels_against_lock(tmp_path, locked, "lattice_diamond_mcp-")


@pytest.mark.parametrize("filename, message", [
    ("other-2.0-py3-none-any.whl", "not in uv.lock"),
    ("dep-1.0-cp311-cp311-win_amd64.whl", "lists no wheel of that name"),
])
def test_a_wheel_the_lock_does_not_list_stops_the_build(tmp_path, filename, message):
    good = make_wheel(tmp_path, "dep-1.0-py3-none-any.whl", {"a": "1"})
    locked = lock_for(tmp_path, good)
    good.unlink()
    make_wheel(tmp_path, filename, {"a": "1"})
    with pytest.raises(build.BuildError, match=message):
        build.check_wheels_against_lock(tmp_path, locked, "lattice_diamond_mcp-")


def test_requirements_pin_every_wheel_by_hash(tmp_path):
    first = make_wheel(tmp_path, "Pydantic_Core-2.46.3-cp311-cp311-manylinux_2_17_x86_64.whl", {"a": "1"})
    second = make_wheel(tmp_path, "anyio-4.15.1-py3-none-any.whl", {"b": "2"})
    text = build.requirements_text(tmp_path)
    assert text == (
        "anyio==4.15.1 \\\n    --hash=sha256:%s\n" % build.sha256_file(second)
        + "pydantic-core==2.46.3 \\\n    --hash=sha256:%s\n" % build.sha256_file(first)
    )


def test_an_unreadable_lock_stops_the_build(tmp_path):
    (tmp_path / "uv.lock").write_text("not [toml")
    with pytest.raises(build.BuildError, match="Cannot read"):
        build.load_lock(tmp_path / "uv.lock")


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
def test_register_passes_the_server_arguments_after_the_program(fake_claude):
    claude, state = fake_claude
    install.register(claude, "diamond", "user", "/venv/bin/python", None, False, server_args=install.WHEELS_SERVER_ARGS)
    call = Path(str(state) + ".calls").read_text().splitlines()[-1].split()
    assert call[call.index("--") + 1:] == ["/venv/bin/python", "-I", "-m", "diamond_mcp"]


@posix_only
def test_register_replaces_the_launcher_an_earlier_install_registered(fake_claude, capsys):
    claude, state = fake_claude
    state.write_text("diamond\t/venv/bin/diamond-mcp\nother\t/x\n")
    install.register(claude, "diamond", "user", "/venv/bin/python", None, False,
                     server_args=install.WHEELS_SERVER_ARGS, replaces=["/venv/bin/diamond-mcp"])
    assert state.read_text() == "other\t/x\ndiamond\t/venv/bin/python\n"
    assert "Replacing" in capsys.readouterr().out


@posix_only
def test_register_still_refuses_an_entry_that_is_not_ours_when_replacing_is_allowed(fake_claude):
    claude, state = fake_claude
    state.write_text("diamond\t/some/other/server\n")
    with pytest.raises(install.InstallError, match="already registered"):
        install.register(claude, "diamond", "user", "/venv/bin/python", None, False, replaces=["/venv/bin/diamond-mcp"])
    assert state.read_text() == "diamond\t/some/other/server\n"


@posix_only
def test_unregister_also_removes_an_old_launcher_registration(fake_claude):
    claude, state = fake_claude
    state.write_text("diamond\t/venv/bin/diamond-mcp\n")
    install.unregister(claude, "diamond", "user", "/venv/bin/python", False, replaces=["/venv/bin/diamond-mcp"])
    assert state.read_text() == ""


def test_the_wheels_install_registers_python_in_isolated_mode_not_a_launcher():
    assert install.WHEELS_SERVER_ARGS == ["-I", "-m", "diamond_mcp"]
    import tomllib
    project = tomllib.loads((SCRIPTS.parent / "pyproject.toml").read_text())["project"]
    assert "scripts" not in project and "gui-scripts" not in project, "pip would build a launcher program from these"


@posix_only
def test_a_dry_run_shows_the_python_command_to_register(tmp_path, monkeypatch, capsys, fake_claude):
    claude, _ = fake_claude
    monkeypatch.setattr(install, "load_buildinfo", lambda folder=None: info())
    install.main(["--dry-run", "--venv-dir", str(tmp_path / "v"), "--claude-path", claude])
    out = capsys.readouterr().out
    assert "-I -m diamond_mcp" in out and "diamond-mcp --help" not in out


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
    state.write_text("diamond\t%s\n" % install.venv_python(str(venv)))
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
    state.write_text("diamond\t%s\n" % install.venv_python(str(venv)))
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
    assert "--require-hashes" in out and "requirements.txt" in out and "constraints" not in out
    assert not venv.exists()


def test_install_refuses_a_bundle_without_requirements(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "HERE", str(tmp_path))
    (tmp_path / "wheels").mkdir()
    with pytest.raises(install.InstallError, match="requirements.txt is missing"):
        install.install_packages(str(tmp_path / "venv"), info(), dry_run=False)


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
    server = install.venv_python(str(venv))
    assert "diamond-mcp" in subprocess.run([server] + install.WHEELS_SERVER_ARGS + ["--help"], stdout=subprocess.PIPE, text=True).stdout
    assert not os.path.exists(install.legacy_launcher(str(venv))), "the wheels install must not create a launcher"
    again = subprocess.run([sys.executable, str(bundle / "install.py"), "--uninstall", "--venv-dir", str(venv)],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert again.returncode == 0 and not venv.exists(), again.stdout


# ------------------------------------------------- a server that is running from the folder

def ns(**kw):
    import argparse
    return argparse.Namespace(**kw)


@pytest.fixture
def running_server(tmp_path, monkeypatch):
    """A folder with a fake 'running' state: detection and listing are replaced, the choreography is real."""
    folder = tmp_path / "venv"
    folder.mkdir()
    state = {"running": True}
    monkeypatch.setattr(install, "server_running_from", lambda p: state["running"])
    monkeypatch.setattr(install, "pids_running_from", lambda p: ["7 python.exe"])
    return folder, state


def test_the_lock_policy_comes_from_the_flag_then_the_environment(monkeypatch):
    monkeypatch.delenv("DIAMOND_MCP_IF_LOCKED", raising=False)
    assert install.if_locked_policy(ns(if_locked="")) == "ask"
    assert install.if_locked_policy(ns(if_locked="kill")) == "kill"
    monkeypatch.setenv("DIAMOND_MCP_IF_LOCKED", "exit")
    assert install.if_locked_policy(ns(if_locked="")) == "exit"
    assert install.if_locked_policy(ns(if_locked="kill")) == "kill"  # the flag wins
    monkeypatch.setenv("DIAMOND_MCP_IF_LOCKED", "bogus")
    assert install.if_locked_policy(ns(if_locked="")) == "ask"


def test_pids_are_read_from_the_listing_without_the_installers_own():
    own = os.getpid()
    assert install.process_ids(["12 python.exe", "%d python.exe" % own, "x y", "", "34 python.exe"]) == [12, 34]


def test_nothing_is_asked_when_nothing_runs_from_the_folder(running_server, monkeypatch):
    folder, state = running_server
    state["running"] = False
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("must not ask"))
    install.free_directory(ns(if_locked=""), folder)


def test_a_missing_folder_is_not_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "server_running_from", lambda p: pytest.fail("must not look"))
    install.free_directory(ns(if_locked=""), tmp_path / "absent")


def test_kill_policy_stops_the_processes_without_asking(running_server, monkeypatch):
    folder, state = running_server
    killed = []
    monkeypatch.setattr(install, "kill_processes", lambda procs: killed.append(procs) or state.update(running=False))
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("must not ask"))
    install.free_directory(ns(if_locked="kill"), folder)
    assert killed == [["7 python.exe"]]


def test_exit_policy_changes_nothing(running_server, monkeypatch):
    folder, _ = running_server
    monkeypatch.setattr(install, "kill_processes", lambda procs: pytest.fail("must not kill"))
    with pytest.raises(install.InstallError, match="Nothing has been changed"):
        install.free_directory(ns(if_locked="exit"), folder)


def test_nobody_to_ask_exits_and_names_the_flag_without_killing(running_server, monkeypatch):
    folder, _ = running_server
    monkeypatch.setattr(install, "kill_processes", lambda procs: pytest.fail("must not kill"))

    def no_stdin(*a):
        raise EOFError
    monkeypatch.setattr("builtins.input", no_stdin)
    with pytest.raises(install.InstallError) as stop:
        install.free_directory(ns(if_locked=""), folder)
    message = str(stop.value)
    assert "--if-locked kill" in message and "DIAMOND_MCP_IF_LOCKED" in message and "7 python.exe" in message
    assert "do not choose for them" in message


@pytest.mark.parametrize("answer, kills", [("a", True), ("c", False), ("﻿a", True), (" C \n", False)])
def test_the_prompt_answers_kill_or_exit(running_server, monkeypatch, answer, kills):
    folder, state = running_server
    killed = []
    monkeypatch.setattr(install, "kill_processes", lambda procs: killed.append(1) or state.update(running=False))
    monkeypatch.setattr("builtins.input", lambda *a: answer)
    if kills:
        install.free_directory(ns(if_locked=""), folder)
    else:
        with pytest.raises(install.InstallError):
            install.free_directory(ns(if_locked=""), folder)
    assert bool(killed) is kills


def test_a_wrong_answer_is_asked_again(running_server, monkeypatch):
    folder, state = running_server
    answers = iter(["x", "", "a"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr(install, "kill_processes", lambda procs: state.update(running=False))
    install.free_directory(ns(if_locked=""), folder)
    assert not list(answers)


def test_waiting_rechecks_after_the_user_stops_the_processes(running_server, monkeypatch):
    folder, state = running_server

    def fake_input(prompt=""):
        if "Choice" in prompt:
            return "b"
        state["running"] = False  # the user stopped the server, then pressed Enter
        return ""
    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(install, "kill_processes", lambda procs: pytest.fail("must not kill"))
    install.free_directory(ns(if_locked=""), folder)


def test_a_kill_that_does_not_free_it_falls_back_to_asking(running_server, monkeypatch):
    folder, _ = running_server
    monkeypatch.setattr(install, "kill_processes", lambda procs: None)
    monkeypatch.setattr(install, "wait_until_free", lambda p, seconds=15.0: False)
    monkeypatch.setattr("builtins.input", lambda *a: "c")
    with pytest.raises(install.InstallError, match="still in use"):
        install.free_directory(ns(if_locked="kill"), folder)


def test_helper_processes_never_read_the_installers_stdin(tmp_path, monkeypatch):
    """A child that inherits stdin swallows the answer typed at the prompt."""
    seen = []

    class Done:
        returncode, stdout = 0, "5 python.exe"
    monkeypatch.setattr(install, "IS_WINDOWS", True)
    monkeypatch.setattr(install.subprocess, "run", lambda cmd, **k: seen.append((cmd[0], k)) or Done())
    install.pids_running_from(str(tmp_path))
    install.kill_processes(["5 python.exe"])
    assert [name for name, _ in seen] == ["powershell", "taskkill"]
    assert all(k.get("stdin") is install.subprocess.DEVNULL for _, k in seen)


def test_windows_kills_the_whole_process_tree(monkeypatch):
    seen = []
    monkeypatch.setattr(install, "IS_WINDOWS", True)
    monkeypatch.setattr(install.subprocess, "run", lambda cmd, **k: seen.append(cmd))
    install.kill_processes(["5 python.exe"])
    assert seen == [["taskkill", "/F", "/T", "/PID", "5"]]


def test_a_running_server_is_detected_from_locked_files_on_windows_and_processes_elsewhere(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "IS_WINDOWS", True)
    monkeypatch.setattr(install, "locked_files", lambda p: ["python.exe"])
    assert install.server_running_from(str(tmp_path))
    monkeypatch.setattr(install, "locked_files", lambda p: [])
    assert not install.server_running_from(str(tmp_path))
    monkeypatch.setattr(install, "IS_WINDOWS", False)
    monkeypatch.setattr(install, "pids_running_from", lambda p: ["9 python"])
    assert install.server_running_from(str(tmp_path))


@posix_only
def test_a_real_process_started_from_the_folder_is_found_and_stopped(tmp_path):
    folder = tmp_path / "bin-holder"
    (folder / "bin").mkdir(parents=True)
    program = folder / "bin" / "diamond-mcp"
    program.symlink_to(shutil.which("sleep"))
    other = tmp_path / "elsewhere"
    other.mkdir()
    bystander = subprocess.Popen([shutil.which("sleep"), "60"])
    process = subprocess.Popen([str(program), "60"])  # argv[0] is the path inside the folder, as a client launches it
    try:
        deadline = time.monotonic() + 5
        while not install.pids_running_from(str(folder)) and time.monotonic() < deadline:
            time.sleep(0.05)
        found = install.pids_running_from(str(folder))
        assert found == ["%d diamond-mcp" % process.pid]  # not the bystander, not this test
        assert install.pids_running_from(str(other)) == []
        install.free_directory(ns(if_locked="kill"), folder)
        assert process.wait(timeout=10) is not None
        assert install.pids_running_from(str(folder)) == []
        assert bystander.poll() is None, "a process from outside the folder must be left alone"
    finally:
        for p in (process, bystander):
            if p.poll() is None:
                p.kill()
            p.wait()


@posix_only
def test_the_installer_stops_before_changing_anything_when_a_server_runs_and_nobody_can_answer(tmp_path, monkeypatch, capsys):
    """End to end through main(): the environment must be untouched and the exit status 1."""
    venv = tmp_path / "venv"
    venv.mkdir()
    (venv / install.MARKER).write_text("x")
    (venv / "precious.txt").write_text("keep")
    monkeypatch.setattr(install, "load_buildinfo", lambda folder=None: info())
    monkeypatch.setattr(install, "find_python", lambda wanted, explicit=None: [sys.executable])
    monkeypatch.setattr(install, "pids_running_from", lambda p: ["7 python"])
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(EOFError()))
    monkeypatch.setattr(install, "run", lambda *a, **k: pytest.fail("must not run pip or anything else"))
    assert install.main(["--no-register", "--venv-dir", str(venv)]) == 1
    err = capsys.readouterr().err
    assert "in use by running MCP server" in err and "--if-locked kill" in err
    assert (venv / "precious.txt").read_text() == "keep"


@posix_only
def test_uninstall_frees_the_folder_before_removing_the_registration(fake_claude, tmp_path, monkeypatch):
    claude, state = fake_claude
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / install.MARKER).write_text("x")
    state.write_text("diamond\t%s\n" % install.venv_python(str(venv)))
    monkeypatch.setattr(install, "pids_running_from", lambda p: ["7 python"])
    monkeypatch.setattr("builtins.input", lambda *a: "c")
    with pytest.raises(install.InstallError, match="still in use"):
        install.do_uninstall(install.parse_args(["--uninstall", "--venv-dir", str(venv), "--claude-path", claude]))
    assert venv.exists() and "diamond" in state.read_text(), "exiting must leave the registration and the files alone"


def test_a_folder_that_is_not_ours_is_never_checked_for_a_running_server(tmp_path, monkeypatch):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    monkeypatch.setattr(install, "server_running_from", lambda p: pytest.fail("must not look"))
    monkeypatch.setattr(install, "load_buildinfo", lambda folder=None: info())
    monkeypatch.setattr(install, "find_python", lambda wanted, explicit=None: [sys.executable])
    assert install.main(["--no-register", "--venv-dir", str(foreign)]) == 1


def test_the_dry_run_does_not_look_for_running_servers(tmp_path, monkeypatch):
    venv = tmp_path / "venv"
    venv.mkdir()
    (venv / install.MARKER).write_text("x")
    monkeypatch.setattr(install, "server_running_from", lambda p: pytest.fail("must not look"))
    monkeypatch.setattr(install, "load_buildinfo", lambda folder=None: info())
    monkeypatch.setattr(install, "find_python", lambda wanted, explicit=None: [sys.executable])
    assert install.main(["--dry-run", "--no-register", "--venv-dir", str(venv)]) == 0


def test_a_read_only_file_is_not_mistaken_for_a_locked_one(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("checks the Windows branch with a stand-in")
    monkeypatch.setattr(install, "IS_WINDOWS", True)
    readonly = tmp_path / "ro.dll"
    readonly.write_bytes(b"x")
    readonly.chmod(0o444)
    real_open = open

    def refuse(path, mode="r", *a, **k):
        if str(path).endswith(".dll") and "+" in mode:
            raise PermissionError(13, "denied")
        return real_open(path, mode, *a, **k)
    monkeypatch.setattr("builtins.open", refuse)
    if os.access(str(readonly), os.W_OK):
        pytest.skip("running as a user who can write read-only files (root)")
    assert install.locked_files(str(tmp_path)) == []
    readonly.chmod(0o644)
    assert install.locked_files(str(tmp_path)) == [str(readonly)]


def test_the_default_registration_name_is_lattice_diamond_mcp():
    assert install.DEFAULT_NAME == "lattice-diamond-mcp"
    assert install.parse_args([]).name == "lattice-diamond-mcp"


@posix_only
def test_an_old_diamond_registration_of_this_program_is_retired(fake_claude):
    claude, state = fake_claude
    state.write_text("diamond\t/venv/bin/python -I -m diamond_mcp\n")
    install.retire_old_name(claude, "lattice-diamond-mcp", "user", "/venv/bin/python", False)
    assert state.read_text() == ""


@posix_only
def test_an_old_registration_that_points_elsewhere_or_was_chosen_is_kept(fake_claude):
    claude, state = fake_claude
    state.write_text("diamond\t/some/other/server\n")
    install.retire_old_name(claude, "lattice-diamond-mcp", "user", "/venv/bin/python", False)
    assert state.read_text() == "diamond\t/some/other/server\n"
    state.write_text("diamond\t/venv/bin/python\n")
    install.retire_old_name(claude, "diamond", "user", "/venv/bin/python", False)  # the user asked for that name
    install.retire_old_name(claude, "lattice-diamond-mcp", "user", "/venv/bin/python", True)  # dry run
    assert state.read_text() == "diamond\t/venv/bin/python\n"
