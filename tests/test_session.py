"""Tests for DiamondSession against the fake console (real tclsh)."""

import shutil
import subprocess

import pytest

from conftest import needs_tclsh
from diamond_mcp.diamond_session import (
    DiamondSession,
    SessionError,
    extract_flagged_lines,
    tcl_quote,
)

pytestmark = needs_tclsh


# ---------------------------------------------------------------- tcl_quote

QUOTE_CASES = [
    "simple",
    "with space",
    "/path/with spaces/and{brace}.ldf",
    "unbalanced { brace",
    "trailing backslash\\",
    'quote " and $dollar [bracket]',
    "semi;colon",
    "newline\nand\ttab",
    "",
    "a=b",
    "C:\\dir\\file.ldf",
]


@pytest.mark.parametrize("value", QUOTE_CASES)
def test_tcl_quote_round_trips_through_real_tcl(value):
    script = f"puts -nonewline [lindex [list {tcl_quote(value)}] 0]"
    out = subprocess.run(
        [shutil.which("tclsh")], input=script, capture_output=True, text=True, check=True
    ).stdout
    assert out == value


CARRIAGE_RETURN_CASES = ["a\rb", "\r", "a\r\nb", "back\\slash\rand cr", "{\r}", "x\\\r", "\\\r-impl evil"]


@pytest.mark.parametrize("value", CARRIAGE_RETURN_CASES)
def test_tcl_quote_keeps_a_carriage_return_inside_one_word_when_read_from_a_file(value, tmp_path):
    """The server hands Diamond a script FILE, and Tcl reads a bare CR in a file as a line break."""
    script = tmp_path / "t.tcl"
    script.write_bytes(
        ("set words [list a %s b]\nputs [llength $words]\nputs -nonewline [lindex $words 1]\n" % tcl_quote(value)).encode("utf-8")
    )
    out = subprocess.run([shutil.which("tclsh"), str(script)], capture_output=True, check=True).stdout.decode("utf-8")
    assert out == "3\n" + value


def test_extract_flagged_lines_caps_output():
    lines = [f"ERROR - number {i}" for i in range(80)] + ["info line"]
    flagged = extract_flagged_lines(lines)
    assert len(flagged) == 51
    assert flagged[-1].startswith("... 30 more")


# ---------------------------------------------------------------- lifecycle

def test_start_reports_version_from_banner(session):
    assert session.is_running
    session.run_tcl("puts hello")
    assert session.diamond_version == "9.9.9.9.9"


def test_start_is_idempotent(session):
    epoch = session.epoch
    result = session.start()
    assert result.success
    assert session.epoch == epoch


def test_start_without_executable_raises():
    with pytest.raises(SessionError, match="No Diamond executable"):
        DiamondSession(diamond_exe=None).start()


def test_start_with_missing_executable_raises(tmp_path):
    with pytest.raises(SessionError, match="not found"):
        DiamondSession(diamond_exe=str(tmp_path / "nope")).start()


def test_stop_is_idempotent(fake_exe):
    sess = DiamondSession(diamond_exe=fake_exe, start_timeout=30)
    assert sess.stop().success
    sess.start()
    assert sess.stop().success
    assert not sess.is_running
    assert sess.stop().success


def test_run_tcl_when_not_running_raises(fake_exe):
    with pytest.raises(SessionError, match="not running"):
        DiamondSession(diamond_exe=fake_exe).run_tcl("puts x")


# ----------------------------------------------------------------- commands

def test_success_result_and_return_value(session):
    result = session.run_tcl("expr {6 * 7}")
    assert result.success
    assert result.tcl_code == 0
    assert result.result == "42"


def test_output_and_result_are_separated(session):
    result = session.run_tcl('puts "log line"\nset x done')
    assert result.output == "log line"
    assert result.result == "done"


def test_failure_has_return_code_and_message(session):
    result = session.run_tcl("error {boom}")
    assert not result.success
    assert result.tcl_code == 1
    assert result.result == "boom"


def test_unknown_command_is_a_caught_failure_not_a_console_error(session):
    result = session.run_tcl("prj_nosuchcmd")
    assert not result.success
    assert "invalid command name" in result.result
    assert any("invalid command name" in w for w in result.warnings)
    assert session.run_tcl("expr {1+1}").result == "2"  # session still healthy


def test_state_persists_between_commands(session):
    session.run_tcl("set counter 5")
    assert session.run_tcl("incr counter").result == "6"


def test_multiline_command_with_braces_and_quotes(session):
    script = 'set s "a {b} c"\nforeach w {1 2 3} {\n  append s $w\n}\nset s'
    assert session.run_tcl(script).result == "a {b} c123"


def test_output_without_trailing_newline_does_not_hide_markers(session):
    result = session.run_tcl("puts -nonewline partial; set r ok")
    assert result.success
    assert result.output == "partial"
    assert result.result == "ok"


def test_stats_track_commands_and_errors(session):
    session.run_tcl("set a 1")
    session.run_tcl("error x")
    stats = session.get_stats()
    assert stats["commands_run"] == 2
    assert stats["errors"] == 1
    assert stats["is_running"] is True
    assert stats["diamond_version"] == "9.9.9.9.9"


# ------------------------------------------------------------ failure modes

def test_timeout_kills_session_and_reports_it(session):
    result = session.run_tcl("dmcp_sleep 10000", timeout=1)
    assert not result.success
    assert "timed out" in result.result
    assert not session.is_running
    assert "timed out" in session.last_exit
    with pytest.raises(SessionError, match="not running"):
        session.run_tcl("puts x")


def test_crash_is_detected_and_described(session):
    result = session.run_tcl("dmcp_crash")
    assert not result.success
    assert "terminated" in result.result
    assert "SIGSEGV" in session.last_exit
    assert not session.is_running


def test_crash_clears_current_project(session):
    session.current_project = "/some/project.ldf"
    session.run_tcl("dmcp_crash")
    assert session.current_project is None


def test_start_after_crash_works(session):
    session.run_tcl("dmcp_crash")
    session.start()
    assert session.is_running
    assert session.last_exit is None
    assert session.run_tcl("expr {2+2}").result == "4"


def test_restart_gives_fresh_interpreter(session):
    session.run_tcl("set marker 1")
    session.current_project = "/some/project.ldf"
    epoch = session.epoch
    result = session.restart()
    assert result.success
    assert session.epoch == epoch + 1
    assert session.current_project is None
    assert not session.run_tcl("info exists marker").result == "1"
