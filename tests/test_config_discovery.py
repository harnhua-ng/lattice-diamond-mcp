"""Tests for configuration resolution and installation discovery."""

import json
import os
import stat

import pytest

from diamond_mcp import config, discovery


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for var in ("DIAMOND_EXE", "LM_LICENSE_FILE", "DIAMOND_MCP_CONFIG", "DIAMOND_MCP_TIMEOUT", "DIAMOND_SEARCH_ROOTS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(discovery, "DEFAULT_ROOTS", ())
    monkeypatch.setattr(config, "discover_diamond_installations", discovery.discover_diamond_installations)


def make_install(root, release, layout="standard"):
    if layout == "standard":
        bin_dir = root / release / "bin" / "lin64"
    elif layout == "site":
        bin_dir = root / release / "diamond" / "sub" / "bin" / "lin64"
    else:  # versioned subdirectory
        bin_dir = root / release / "diamond" / "3.0_x64" / "bin" / "lin64"
    bin_dir.mkdir(parents=True)
    exe = bin_dir / "diamondc"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


# ---------------------------------------------------------------- discovery

@pytest.mark.skipif(os.name == "nt", reason="Linux layouts")
@pytest.mark.parametrize("layout", ["standard", "site", "versioned"])
def test_discovery_finds_each_layout(tmp_path, layout):
    exe = make_install(tmp_path / "root", "3.14.0.75.0", layout)
    found = discovery.discover_diamond_installations([tmp_path / "root"])
    assert [i.exe for i in found] == [str(exe.resolve())]
    assert found[0].version == "3.14.0.75.0"


@pytest.mark.skipif(os.name == "nt", reason="Linux layouts")
def test_discovery_sorts_newest_first_and_ignores_noise(tmp_path):
    root = tmp_path / "root"
    for release in ("3.9.2.099x64", "3.10.p.082x64", "3.14.0.75.0", "3.11.0.088x64_tmp", "latest", "3.11.0.5_test"):
        make_install(root, release, "site")
    found = discovery.discover_diamond_installations([root])
    assert [i.version for i in found] == ["3.14.0.75.0", "3.10.p.082x64", "3.9.2.099x64"]


@pytest.mark.skipif(os.name == "nt", reason="Linux layouts")
def test_discovery_deduplicates_by_resolved_exe(tmp_path):
    root = tmp_path / "root"
    make_install(root, "3.14.0.75.0")
    found = discovery.discover_diamond_installations([root, root])
    assert len(found) == 1


@pytest.mark.skipif(os.name == "nt", reason="Linux layouts")
def test_discovery_uses_search_roots_env(tmp_path, monkeypatch):
    make_install(tmp_path / "custom", "3.12.0.1")
    monkeypatch.setenv("DIAMOND_SEARCH_ROOTS", str(tmp_path / "custom"))
    assert [i.version for i in discovery.discover_diamond_installations()] == ["3.12.0.1"]


def test_discovery_with_no_roots_returns_empty():
    assert discovery.discover_diamond_installations() == []


def test_version_key_ordering():
    keys = {v: discovery.version_key(v) for v in ("3.9.2.099x64", "3.10.p.082x64", "3.14.0.75.0", "3.13.72")}
    assert sorted(keys, key=keys.get) == ["3.9.2.099x64", "3.10.p.082x64", "3.13.72", "3.14.0.75.0"]


# ------------------------------------------------------------------- config

def test_diamond_exe_env_wins_over_config(tmp_path, monkeypatch):
    cfg = tmp_path / "diamond-mcp.json"
    cfg.write_text(json.dumps({"diamond_exe": "/from/config"}))
    monkeypatch.setenv("DIAMOND_EXE", "/from/env")
    resolved = config.resolve_diamond_path()
    assert (resolved.path, resolved.source) == ("/from/env", "env")


def test_diamond_exe_from_config_file_in_cwd(tmp_path):
    (tmp_path / "diamond-mcp.json").write_text(json.dumps({"diamond_exe": "/from/config"}))
    resolved = config.resolve_diamond_path()
    assert resolved.path == "/from/config"
    assert resolved.source.startswith("config:")


@pytest.mark.skipif(os.name == "nt", reason="Linux layouts")
def test_diamond_install_dir_in_config_is_resolved(tmp_path):
    exe = make_install(tmp_path / "inst", "3.14.0.75.0")
    install_dir = exe.parents[2]
    (tmp_path / "diamond-mcp.json").write_text(json.dumps({"diamond_install_dir": str(install_dir)}))
    assert config.resolve_diamond_path().path == str(exe)


@pytest.mark.skipif(os.name == "nt", reason="Linux layouts")
def test_diamond_exe_auto_detected_when_nothing_configured(tmp_path, monkeypatch):
    exe = make_install(tmp_path / "root", "3.14.0.75.0")
    monkeypatch.setenv("DIAMOND_SEARCH_ROOTS", str(tmp_path / "root"))
    resolved = config.resolve_diamond_path()
    assert resolved.source == "auto-detected"
    assert resolved.path == str(exe.resolve())
    assert resolved.exists


def test_nothing_configured_or_found_gives_none():
    resolved = config.resolve_diamond_path()
    assert resolved.path is None and resolved.source == "none" and not resolved.exists


def test_explicit_config_path_env(tmp_path, monkeypatch):
    explicit = tmp_path / "elsewhere.json"
    explicit.write_text(json.dumps({"lm_license_file": "1234@host"}))
    monkeypatch.setenv("DIAMOND_MCP_CONFIG", str(explicit))
    assert config.resolve_lm_license_file() == "1234@host"


def test_license_env_beats_config(tmp_path, monkeypatch):
    (tmp_path / "diamond-mcp.json").write_text(json.dumps({"lm_license_file": "from-config"}))
    monkeypatch.setenv("LM_LICENSE_FILE", "from-env")
    assert config.resolve_lm_license_file() == "from-env"


@pytest.mark.parametrize(
    "env, cfg, expected",
    [
        ("120", 5, 120.0),
        ("", 5, 5.0),
        ("not-a-number", 5, 5.0),
        ("-3", None, config.DEFAULT_TIMEOUT_SECONDS),
        ("", None, config.DEFAULT_TIMEOUT_SECONDS),
    ],
)
def test_timeout_resolution(tmp_path, monkeypatch, env, cfg, expected):
    if env:
        monkeypatch.setenv("DIAMOND_MCP_TIMEOUT", env)
    if cfg is not None:
        (tmp_path / "diamond-mcp.json").write_text(json.dumps({"timeout_seconds": cfg}))
    assert config.resolve_timeout() == expected


def test_malformed_config_file_is_ignored(tmp_path):
    (tmp_path / "diamond-mcp.json").write_text("{ not json")
    assert config.load_config_data() == {}
    assert config.resolve_diamond_path().path is None


def test_save_config_merges_and_round_trips(tmp_path):
    target = tmp_path / "diamond-mcp.json"
    target.write_text(json.dumps({"diamond_install_dir": "/old", "timeout_seconds": 10}))
    written = config.save_config(diamond_exe="/new/diamondc", lm_license_file="1@h")
    assert written == target
    data = json.loads(target.read_text())
    assert data == {"diamond_exe": "/new/diamondc", "lm_license_file": "1@h", "timeout_seconds": 10}


def test_save_config_creates_user_file_when_none_exists(tmp_path):
    written = config.save_config(timeout_seconds=99)
    assert written == tmp_path / "home" / ".config" / "diamond-mcp" / "config.json"
    assert json.loads(written.read_text()) == {"timeout_seconds": 99.0}


def test_get_config_info_does_not_leak_license_value(tmp_path, monkeypatch):
    monkeypatch.setenv("LM_LICENSE_FILE", "1234@secret-host")
    info = config.get_config_info()
    assert info["lm_license_file_set"] is True
    assert "secret-host" not in json.dumps(info)
