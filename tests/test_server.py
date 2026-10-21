"""Tests for the MCP tool layer, run against the fake Diamond console."""

import asyncio

import pytest

from conftest import needs_tclsh
from diamond_mcp import server

pytestmark = needs_tclsh

EXPECTED_TOOLS = {
    "start_diamond", "stop_diamond", "get_session_status", "detect_diamond_installations",
    "get_diamond_config", "configure_diamond", "check_environment",
    "open_project", "close_project", "create_project", "save_project", "get_project_info",
    "add_sources", "remove_sources", "set_target_device", "set_synthesis_tool",
    "get_strategy_option", "set_strategy_option",
    "compile_project", "run_flow", "run_synthesis", "run_translate", "run_map", "run_par",
    "run_timing_analysis", "run_export", "generate_bitstream",
    "list_reports", "get_timing_summary", "get_resource_usage", "read_report",
    "run_tcl",
}


def test_all_tools_are_registered_with_schemas():
    tools = asyncio.run(server.mcp.list_tools())
    assert {t.name for t in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert tool.description, f"{tool.name} has no description"
        assert tool.inputSchema["type"] == "object"


def test_registered_tool_is_callable_through_mcp(server_session):
    content = asyncio.run(server.mcp.call_tool("run_tcl", {"command": "expr {1+1}"}))
    text = str(content)
    assert "success" in text and "2" in text


# ------------------------------------------------------------------ session

def test_tools_autostart_the_session(server_session):
    assert not server_session.is_running
    result = server.run_tcl("expr {3*3}")
    assert result["success"] and result["output"] == "9"
    assert server_session.is_running


def test_run_tcl_rejects_empty_command(server_session):
    assert not server.run_tcl("   ")["success"]


def test_run_tcl_failure_carries_hints(server_session):
    result = server.run_tcl("prj_dev")
    assert not result["success"]
    assert any("open_project" in h for h in result["hints"])


def test_crash_then_next_call_restarts_with_note(server_session):
    server.run_tcl("expr 1")
    crashed = server.run_tcl("dmcp_crash")
    assert not crashed["success"]
    again = server.run_tcl("expr {2+2}")
    assert again["success"] and again["output"] == "4"
    assert "SIGSEGV" in again["session_note"]
    assert "reopen" in again["session_note"]


def test_get_session_status(server_session):
    server.run_tcl("expr 1")
    status = server.get_session_status(log_lines=5)
    assert status["is_running"] and status["diamond_version"] == "9.9.9.9.9"
    assert "console_log_tail" in status


def test_stop_diamond(server_session):
    server.run_tcl("expr 1")
    assert server.stop_diamond()["success"]
    assert not server_session.is_running


@pytest.mark.parametrize(
    "command",
    [
        "prj_project close",
        "  prj_project   close  ",
        "catch {prj_project close}",
        "puts x; prj_project close",
        "set r [prj_project close]",
        "if {1} {\n  prj_project close\n}",
    ],
)
def test_run_tcl_refuses_prj_project_close(server_session, command):
    result = server.run_tcl(command)
    assert not result["success"]
    assert "close_project" in result["output"]
    assert not server_session.is_running  # refused before the console was even started


@pytest.mark.parametrize("command", ["prj_project closed_form", "puts {prj_project closer}", "set my_prj_project close"])
def test_run_tcl_does_not_refuse_lookalikes(server_session, command):
    # These do not run the close command; they fail or succeed on their own merits.
    assert "Refused" not in server.run_tcl(command)["output"]


# ------------------------------------------------------------------ project

def test_open_project_success_reports_preflight(server_session, ldf_project):
    result = server.open_project(str(ldf_project))
    assert result["success"], result
    assert result["preflight"] == {"device": "FAKE-DEV-1PKG", "missing_files": []}
    assert server_session.current_project == str(ldf_project).replace("\\", "/")


def test_open_project_warns_about_missing_blocking_sources(server_session, ldf_project):
    (ldf_project.parent / "src" / "top.v").unlink()
    result = server.open_project(str(ldf_project))
    assert result["success"]
    assert result["preflight"]["missing_files"][0]["file"] == "src/top.v"
    assert any("do not exist" in w for w in result["warnings"])


def test_open_project_missing_file(server_session, tmp_path):
    result = server.open_project(str(tmp_path / "nope.ldf"))
    assert not result["success"]
    assert "does not exist" in result["output"]


def test_open_project_rejects_non_diamond_file(server_session, tmp_path):
    bad = tmp_path / "bad.ldf"
    bad.write_text("<Other/>")
    assert "Not a Diamond project" in server.open_project(str(bad))["output"]


def test_open_project_replaces_an_open_project_by_restarting(server_session, ldf_project):
    assert server.open_project(str(ldf_project))["success"]
    epoch = server_session.epoch
    second = server.open_project(str(ldf_project))
    assert second["success"], second
    assert server_session.epoch == epoch + 1
    assert "restarting" in second["session_note"]


def test_close_project_restarts_and_clears_state(server_session, ldf_project):
    server.open_project(str(ldf_project))
    result = server.close_project()
    assert result["success"] and "restarted" in result["output"]
    assert server_session.current_project is None
    assert not server.run_tcl("prj_dev")["success"]  # no project loaded after close


def test_close_project_when_nothing_open_does_not_restart(server_session):
    server.run_tcl("expr 1")
    epoch = server_session.epoch
    result = server.close_project()
    assert result["success"] and "No project is open" in result["output"]
    assert server_session.epoch == epoch


def test_server_never_sends_prj_project_close(server_session, ldf_project, tmp_path, monkeypatch):
    # Diamond 3.14 intermittently crashes on prj_project close, so no tool may send it.
    close_log = tmp_path / "close.log"
    monkeypatch.setenv("FAKE_DIAMOND_CLOSE_LOG", str(close_log))
    server.open_project(str(ldf_project))
    server.open_project(str(ldf_project))  # open over open
    server.create_project("other", str(tmp_path), "FAKE-DEV-1PKG")  # create over open
    server.close_project()
    server.stop_diamond()
    assert not close_log.exists(), close_log.read_text()


def test_create_project(server_session, tmp_path):
    result = server.create_project("demo", str(tmp_path), "LCMXO3LF-1300C-5TG100C")
    assert result["success"], result
    assert server_session.current_project.endswith("demo.ldf")


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        ({"name": "1bad"}, "name must start"),
        ({"synthesis_tool": "vivado"}, "synthesis_tool must be one of"),
        ({"directory": "/no/such/dir"}, "Directory does not exist"),
    ],
)
def test_create_project_validation(server_session, tmp_path, kwargs, fragment):
    args = {"name": "demo", "directory": str(tmp_path), "device": "X"}
    args.update(kwargs)
    result = server.create_project(**args)
    assert not result["success"] and fragment in result["output"]


def test_get_project_info_offline(ldf_project):
    result = server.get_project_info(str(ldf_project))
    assert result["success"]
    assert result["project"]["device"] == "FAKE-DEV-1PKG"
    assert result["missing_files"] == []


def test_get_project_info_without_path_or_open_project(server_session):
    assert "no project is open" in server.get_project_info()["output"]


def test_add_sources_checks_files_exist(server_session, ldf_project):
    server.open_project(str(ldf_project))
    result = server.add_sources(["src/missing.v"])
    assert not result["success"] and "do not exist" in result["output"]


def test_add_sources_resolves_relative_to_project(server_session, ldf_project):
    server.open_project(str(ldf_project))
    (ldf_project.parent / "src" / "extra.v").write_text("module e; endmodule\n")
    result = server.add_sources(["src/extra.v"], synthesis_only=True)
    assert result["success"], result
    assert str(ldf_project.parent / "src" / "extra.v") in result["output"]


def test_remove_sources_requires_exactly_one_mode(server_session):
    assert not server.remove_sources()["success"]
    assert not server.remove_sources(files=["a.v"], remove_all=True)["success"]


def test_set_target_device_returns_new_device(server_session, ldf_project):
    server.open_project(str(ldf_project))
    result = server.set_target_device("LCMXO3LF-6900C-5BG256C")
    assert result["success"] and "LCMXO3LF-6900C-5BG256C" in result["output"]


def test_set_synthesis_tool_validates(server_session):
    assert not server.set_synthesis_tool("quartus")["success"]


def test_strategy_option_round_trip(server_session, ldf_project):
    server.open_project(str(ldf_project))
    assert server.get_strategy_option("PROP_MAP_RegRetiming")["output"] == "False"
    result = server.set_strategy_option("PROP_MAP_RegRetiming", "True")
    assert result["success"] and result["output"] == "True"


def test_set_strategy_option_rejects_equals_in_name(server_session):
    assert not server.set_strategy_option("A=B", "1")["success"]


# --------------------------------------------------------------------- flow

def test_run_flow_validates_names_and_force(server_session, ldf_project):
    server.open_project(str(ldf_project))
    assert not server.run_flow("Syn thesis")["success"]
    assert not server.run_flow("Map", task="bad;task")["success"]
    assert "force must be" in server.run_flow("Map", force="sometimes")["output"]


def test_run_flow_unknown_milestone_gives_hint(server_session, ldf_project):
    server.open_project(str(ldf_project))
    result = server.run_flow("NoSuchStage")
    assert not result["success"]
    assert any("Valid milestones" in h for h in result["hints"])


def test_stage_tools_run_and_write_reports(server_session, ldf_project):
    server.open_project(str(ldf_project))
    for tool in (server.run_synthesis, server.run_translate, server.run_map, server.run_par):
        assert tool()["success"]
    impl = ldf_project.parent / "impl1"
    assert (impl / "proj_impl1.par").is_file() and (impl / "proj_impl1.mrp").is_file()


def test_run_flow_without_project_hints_to_open_one(server_session):
    result = server.run_flow("Synthesis")
    assert not result["success"]
    assert any("open_project" in h for h in result["hints"])


def test_run_timing_analysis_includes_summary(server_session, ldf_project):
    server.open_project(str(ldf_project))
    server.run_par()
    result = server.run_timing_analysis()
    assert result["success"]
    assert result["timing"]["twr"]["setup_errors"] == 38


def test_run_export_alone_writes_no_bitstream(server_session, ldf_project):
    server.open_project(str(ldf_project))
    assert server.run_export()["success"]
    assert not list(ldf_project.parent.rglob("*.bit"))


def test_generate_bitstream_verifies_file(server_session, ldf_project):
    server.open_project(str(ldf_project))
    result = server.generate_bitstream()
    assert result["success"], result
    assert result["bitstream_files"][0].endswith("proj_impl1.bit")


def test_generate_bitstream_fails_when_no_file_written(server_session, ldf_project):
    server.open_project(str(ldf_project))
    server.run_tcl("set ::no_bitgen 1")
    result = server.generate_bitstream()
    assert not result["success"]
    assert any("no .bit file" in w for w in result["warnings"])


def test_compile_project_runs_all_stages(server_session, ldf_project):
    result = server.compile_project(str(ldf_project))
    assert result["success"], result
    assert [s["stage"] for s in result["stages"]] == ["open", "Synthesis", "Translate", "Map", "PAR", "Bitgen"]
    assert result["timing"]["par"]["run_status"] == "Completed"
    assert result["resources"]["resources"]["Number of LUT4s"]["used"] == 191
    assert result["bitstream_files"]
    assert server_session.current_project  # left open for follow-up calls


def test_compile_project_without_bitstream(server_session, ldf_project):
    result = server.compile_project(str(ldf_project), bitstream=False)
    assert result["success"]
    assert [s["stage"] for s in result["stages"]][-1] == "PAR"
    assert "bitstream_files" not in result


def test_compile_project_aborts_before_flow_on_missing_sources(server_session, ldf_project):
    (ldf_project.parent / "src" / "top.v").unlink()
    result = server.compile_project(str(ldf_project))
    assert not result["success"]
    assert result["missing_files"][0]["file"] == "src/top.v"
    assert not server_session.is_running  # never even started the console


def test_compile_project_reports_failed_stage(fake_exe, ldf_project, monkeypatch):
    sess = server.DiamondSession(diamond_exe=fake_exe, timeout=30, start_timeout=30)
    server._set_session(sess)
    try:
        original = server.open_project

        def open_then_arm(path, implementation=None):
            result = original(path, implementation)
            sess.run_tcl("set ::fail_stage PAR")
            return result

        monkeypatch.setattr(server, "open_project", open_then_arm)
        result = server.compile_project(str(ldf_project))
        assert not result["success"]
        assert result["failed_stage"] == "PAR"
        assert [s["stage"] for s in result["stages"]] == ["open", "Synthesis", "Translate", "Map", "PAR"]
        assert result["stages"][-1]["success"] is False
    finally:
        sess.stop()
        server._set_session(None)


# ------------------------------------------------------------------ reports

def test_list_reports_and_summaries(server_session, ldf_project):
    server.compile_project(str(ldf_project))
    listed = server.list_reports()
    assert {"par", "twr", "mrp"} <= set(listed["reports"])
    timing = server.get_timing_summary()
    assert timing["timing"]["twr"]["setup_preferences_met"] is False
    usage = server.get_resource_usage()
    assert usage["resources"]["Number of registers"]["used"] == 140


def test_summaries_fail_clearly_before_flow(ldf_project):
    assert "Run run_par first" in server.get_timing_summary(str(ldf_project))["output"]
    assert "Run run_map first" in server.get_resource_usage(str(ldf_project))["output"]


def test_read_report_pagination_and_tail(tmp_path):
    report = tmp_path / "x.par"
    report.write_text("0123456789" * 10)
    first = server.read_report(str(report), max_chars=30)
    assert first["output"] == "0123456789" * 3 and first["has_more"] and first["total_chars"] == 100
    second = server.read_report(str(report), offset=30, max_chars=30)
    assert second["output"] == "0123456789" * 3
    tail = server.read_report(str(report), max_chars=15, tail=True)
    assert tail["output"] == "56789" + "0123456789" and not tail["has_more"]


def test_read_report_refuses_other_file_types_and_missing_files(tmp_path):
    secret = tmp_path / "id_rsa"
    secret.write_text("secret")
    assert not server.read_report(str(secret))["success"]
    assert "does not exist" in server.read_report(str(tmp_path / "gone.par"))["output"]


# -------------------------------------------------------------- environment

def test_check_environment_flags_missing_exe(monkeypatch):
    monkeypatch.setenv("DIAMOND_EXE", "/does/not/exist")
    result = server.check_environment()
    assert result["ready"] is False
    assert result["checks"][0]["status"] == "error"


def test_check_environment_ready_with_fake_exe(monkeypatch, fake_exe):
    monkeypatch.setenv("DIAMOND_EXE", fake_exe)
    result = server.check_environment()
    assert result["ready"] is True
    assert result["checks"][0]["status"] == "ok"


def test_configure_diamond_validates_input(tmp_path):
    assert not server.configure_diamond()["success"]
    assert "does not exist" in server.configure_diamond(diamond_exe=str(tmp_path / "x"))["output"]
