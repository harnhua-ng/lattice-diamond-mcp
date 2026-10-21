"""Tests for the command audit log: the writer, session integration and server attribution."""

import asyncio
import json
import os
import stat
import threading
from pathlib import Path

import pytest

from conftest import needs_tclsh
from diamond_mcp import config, server
from diamond_mcp.audit import MAX_COMMAND_CHARS, AuditLog, current_tool
from diamond_mcp.diamond_session import DiamondSession


def read_log(path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


# --------------------------------------------------------------- the writer

def test_record_writes_one_json_object_per_line(tmp_path):
    log = AuditLog(str(tmp_path / "a.log"))
    log.record("command", id="x1", command="puts hello", tool="run_tcl")
    log.record("result", id="x1", success=True)
    entries = read_log(log.path)
    assert [e["event"] for e in entries] == ["command", "result"]
    first = entries[0]
    assert first["command"] == "puts hello" and first["tool"] == "run_tcl"
    assert first["session"] == log.session_id
    assert first["pid"] == os.getpid()
    assert first["user"]
    assert first["ts"].endswith("+00:00")


def test_none_fields_are_omitted(tmp_path):
    log = AuditLog(str(tmp_path / "a.log"))
    log.record("command", id="x", command="c", tool=None)
    assert "tool" not in read_log(log.path)[0]


def test_command_text_is_recorded_exactly_including_newlines_and_unicode(tmp_path):
    log = AuditLog(str(tmp_path / "a.log"))
    command = 'puts "line one"\nexec echo café {braces} $x'
    log.record("command", id="x", command=command)
    assert read_log(log.path)[0]["command"] == command


def test_very_long_command_is_truncated_and_flagged(tmp_path):
    log = AuditLog(str(tmp_path / "a.log"))
    log.record("command", id="x", command="a" * (MAX_COMMAND_CHARS + 500))
    entry = read_log(log.path)[0]
    assert len(entry["command"]) == MAX_COMMAND_CHARS and entry["command_truncated"] is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_log_and_directory_are_private(tmp_path):
    log = AuditLog(str(tmp_path / "state" / "audit.log"))
    log.record("command", id="x", command="c")
    assert stat.S_IMODE(Path(log.path).stat().st_mode) == 0o600
    assert stat.S_IMODE(Path(log.path).parent.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_rotated_files_stay_private(tmp_path):
    log = AuditLog(str(tmp_path / "a.log"), max_bytes=300, backups=2)
    for i in range(20):
        log.record("command", id=str(i), command="x" * 50)
    rotated = sorted(tmp_path.glob("a.log*"))
    assert len(rotated) >= 2
    assert {stat.S_IMODE(p.stat().st_mode) for p in rotated} == {0o600}


def test_rotation_keeps_the_configured_number_of_backups(tmp_path):
    log = AuditLog(str(tmp_path / "a.log"), max_bytes=300, backups=2)
    for i in range(60):
        log.record("command", id=str(i), command="x" * 50)
    assert sorted(p.name for p in tmp_path.glob("a.log*")) == ["a.log", "a.log.1", "a.log.2"]


def test_unwritable_location_disables_logging_without_raising(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    log = AuditLog(str(blocker / "sub" / "audit.log"))
    log.record("command", id="x", command="c")  # must not raise
    log.record("command", id="y", command="c")
    assert log.disabled
    assert capsys.readouterr().err.count("audit log disabled") == 1  # warned once


def test_concurrent_writers_produce_intact_lines(tmp_path):
    log = AuditLog(str(tmp_path / "a.log"))

    def work(n):
        for i in range(50):
            log.record("command", id=f"{n}-{i}", command="p" * 200)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(read_log(log.path)) == 400  # every line parses


# ------------------------------------------------- session integration (fake)

@pytest.fixture
def audited(fake_exe, tmp_path):
    log = AuditLog(str(tmp_path / "session-audit.log"))
    sess = DiamondSession(diamond_exe=fake_exe, timeout=30, start_timeout=30, audit=log)
    yield sess, log
    sess.stop()


@needs_tclsh
def test_session_lifecycle_and_command_are_recorded(audited):
    sess, log = audited
    sess.start()
    sess.run_tcl("expr {6*7}")
    sess.stop()
    events = read_log(log.path)
    assert [e["event"] for e in events] == ["session_start", "command", "result", "session_end"]
    start, command, result, end = events
    assert start["version"] == "9.9.9.9.9" and start["console_pid"] > 0
    assert command["command"] == "expr {6*7}"
    assert result["id"] == command["id"] and result["success"] is True and result["tcl_code"] == 0
    assert end["reason"] == "stopped"


@needs_tclsh
def test_failed_command_is_recorded_as_failure(audited):
    sess, log = audited
    sess.start()
    sess.run_tcl("error {boom}")
    result = [e for e in read_log(log.path) if e["event"] == "result"][0]
    assert result["success"] is False and result["tcl_code"] == 1


@needs_tclsh
def test_command_is_written_before_it_runs(audited):
    # A command that crashes the console must still be on record, with no result.
    sess, log = audited
    sess.start()
    sess.run_tcl("dmcp_crash")
    events = read_log(log.path)
    kinds = [e["event"] for e in events]
    assert kinds.index("command") < kinds.index("result")
    crash_cmd = next(e for e in events if e["event"] == "command")
    assert crash_cmd["command"] == "dmcp_crash"
    assert any(e["event"] == "session_end" and "SIGSEGV" in e["reason"] for e in events)
    result = next(e for e in events if e["event"] == "result")
    assert result["success"] is False and "terminated" in result["failure"]


@needs_tclsh
def test_timeout_is_recorded(audited):
    sess, log = audited
    sess.start()
    sess.run_tcl("dmcp_sleep 10000", timeout=1)
    events = read_log(log.path)
    assert any(e["event"] == "session_end" and "timed out" in e["reason"] for e in events)
    assert next(e for e in events if e["event"] == "result")["success"] is False


@needs_tclsh
def test_restart_is_recorded(audited):
    sess, log = audited
    sess.start()
    sess.restart()
    reasons = [(e["event"], e.get("reason")) for e in read_log(log.path) if e["event"].startswith("session")]
    assert reasons == [("session_start", None), ("session_end", "restart"), ("session_start", None)]


@needs_tclsh
def test_session_without_audit_still_works(fake_exe):
    sess = DiamondSession(diamond_exe=fake_exe, start_timeout=30)
    sess.start()
    assert sess.run_tcl("expr 1").success
    assert sess.get_stats()["audit_log"] is None
    sess.stop()


@needs_tclsh
def test_status_reports_audit_log_path(audited):
    sess, log = audited
    sess.start()
    assert sess.get_stats()["audit_log"] == log.path


# ------------------------------------------------------- server attribution

@pytest.fixture
def audited_server(fake_exe, tmp_path):
    log = AuditLog(str(tmp_path / "server-audit.log"))
    sess = DiamondSession(diamond_exe=fake_exe, timeout=30, start_timeout=30, audit=log)
    server._set_session(sess)
    yield sess, log
    sess.stop()
    server._set_session(None)


@needs_tclsh
def test_commands_carry_the_mcp_tool_name(audited_server):
    sess, log = audited_server
    asyncio.run(server.mcp.call_tool("run_tcl", {"command": "expr {1+1}"}))
    command = next(e for e in read_log(log.path) if e["event"] == "command")
    assert command["tool"] == "run_tcl" and command["command"] == "expr {1+1}"


@needs_tclsh
def test_nested_calls_are_attributed_to_the_outer_tool(audited_server, ldf_project):
    sess, log = audited_server
    asyncio.run(server.mcp.call_tool("compile_project", {"project_path": str(ldf_project)}))
    commands = [e for e in read_log(log.path) if e["event"] == "command"]
    assert len(commands) >= 5
    assert {e["tool"] for e in commands} == {"compile_project"}
    assert any("prj_run Synthesis" in e["command"] for e in commands)


@needs_tclsh
def test_direct_calls_have_no_tool_name_and_context_resets(audited_server):
    sess, log = audited_server
    server.run_tcl("expr 1")
    assert "tool" not in next(e for e in read_log(log.path) if e["event"] == "command")
    asyncio.run(server.mcp.call_tool("run_tcl", {"command": "expr 2"}))
    assert current_tool.get() is None


@needs_tclsh
def test_refused_close_command_is_audited(audited_server):
    sess, log = audited_server
    server.run_tcl("prj_project close")
    refused = next(e for e in read_log(log.path) if e["event"] == "refused")
    assert refused["command"] == "prj_project close" and "close" in refused["reason"]
    assert not any(e["event"] == "command" for e in read_log(log.path))  # never reached Diamond


@needs_tclsh
def test_dedicated_tools_audit_the_exact_tcl_they_send(audited_server, ldf_project):
    sess, log = audited_server
    server.open_project(str(ldf_project))
    server.set_target_device("LCMXO3LF-6900C-5BG256C")
    sent = [e["command"] for e in read_log(log.path) if e["event"] == "command"]
    assert any(c.startswith("prj_dev set -part {LCMXO3LF-6900C-5BG256C}") for c in sent)


# ------------------------------------------------------------ configuration

def test_default_audit_path_is_under_the_user_state_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("DIAMOND_MCP_AUDIT_LOG")
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    resolved = config.resolve_audit_log_path()
    assert resolved.source == "default"
    assert resolved.path == str(tmp_path / "home" / ".local" / "state" / "diamond-mcp" / "audit.log")


def test_audit_path_honours_xdg_state_home(tmp_path, monkeypatch):
    monkeypatch.delenv("DIAMOND_MCP_AUDIT_LOG")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    assert config.resolve_audit_log_path().path == str(tmp_path / "xdg" / "diamond-mcp" / "audit.log")


def test_audit_path_from_env_and_config_priority(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "diamond-mcp.json").write_text(json.dumps({"audit_log": "/from/config.log"}))
    monkeypatch.setenv("DIAMOND_MCP_AUDIT_LOG", "/from/env.log")
    assert config.resolve_audit_log_path().path == "/from/env.log"
    monkeypatch.delenv("DIAMOND_MCP_AUDIT_LOG")
    resolved = config.resolve_audit_log_path()
    assert resolved.path == "/from/config.log" and resolved.source.startswith("config:")


@pytest.mark.parametrize("value", ["", "off", "OFF", "none", "false", "0", "disabled"])
def test_audit_can_be_disabled_by_env(monkeypatch, value):
    monkeypatch.setenv("DIAMOND_MCP_AUDIT_LOG", value)
    resolved = config.resolve_audit_log_path()
    assert resolved.path is None and resolved.source == "disabled-by-env"


@pytest.mark.parametrize("value", [False, "off", "none"])
def test_audit_can_be_disabled_by_config(tmp_path, monkeypatch, value):
    monkeypatch.delenv("DIAMOND_MCP_AUDIT_LOG")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "diamond-mcp.json").write_text(json.dumps({"audit_log": value}))
    assert config.resolve_audit_log_path().source == "disabled-by-config"


def test_make_session_builds_an_audit_log_from_configuration(tmp_path, monkeypatch):
    from diamond_mcp.diamond_session import make_session

    monkeypatch.setenv("DIAMOND_MCP_AUDIT_LOG", str(tmp_path / "x.log"))
    assert make_session(diamond_exe="/x").audit.path == str(tmp_path / "x.log")
    monkeypatch.setenv("DIAMOND_MCP_AUDIT_LOG", "off")
    assert make_session(diamond_exe="/x").audit is None


def test_get_config_info_reports_audit_location(tmp_path, monkeypatch):
    monkeypatch.setenv("DIAMOND_MCP_AUDIT_LOG", str(tmp_path / "x.log"))
    info = config.get_config_info()
    assert info["audit_log"] == str(tmp_path / "x.log") and info["audit_log_source"] == "env"


def test_check_environment_reports_audit_state(tmp_path, monkeypatch, fake_exe):
    monkeypatch.setenv("DIAMOND_EXE", fake_exe)
    monkeypatch.setenv("DIAMOND_MCP_AUDIT_LOG", str(tmp_path / "new" / "x.log"))
    audit_check = next(c for c in server.check_environment()["checks"] if c["name"] == "audit_log")
    assert audit_check["status"] == "ok"
    monkeypatch.setenv("DIAMOND_MCP_AUDIT_LOG", "off")
    audit_check = next(c for c in server.check_environment()["checks"] if c["name"] == "audit_log")
    assert audit_check["status"] == "warning" and "disabled" in audit_check["detail"]
