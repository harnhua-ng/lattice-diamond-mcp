"""
Shared pytest fixtures for the lattice-diamond-mcp test suite.

Most tests run against a fake Diamond console (tests/fake_diamond) executed by
the real `tclsh`, so the process handling and Tcl protocol are exercised
without a Diamond installation or licence. Tests that need tclsh are skipped
when it is not installed. Tests marked `integration` need a real Diamond.

Fixtures provided:
    isolated_audit_log  (autouse) redirects the audit log into a temporary directory
    fake_exe        path to the fake diamondc launcher
    session         a started DiamondSession on the fake console
    server_session  a started session installed into the server module
    ldf_project     a small valid project on disk (sources, strategy, .ldf)
"""

import shutil
import stat
from pathlib import Path

import pytest

from diamond_mcp import server
from diamond_mcp.diamond_session import DiamondSession

FAKE_DIR = Path(__file__).parent / "fake_diamond"
DATA_DIR = Path(__file__).parent / "data"

needs_tclsh = pytest.mark.skipif(shutil.which("tclsh") is None, reason="tclsh is not installed")


@pytest.fixture(autouse=True)
def isolated_audit_log(tmp_path_factory, monkeypatch):
    """Keep every test's audit log out of the real home directory."""
    path = tmp_path_factory.mktemp("audit") / "audit.log"
    monkeypatch.setenv("DIAMOND_MCP_AUDIT_LOG", str(path))
    return path


@pytest.fixture(scope="session")
def fake_exe() -> str:
    exe = FAKE_DIR / "fake_diamondc"
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return str(exe)


@pytest.fixture
def session(fake_exe):
    sess = DiamondSession(diamond_exe=fake_exe, timeout=30, start_timeout=30)
    sess.start()
    yield sess
    sess.stop()


@pytest.fixture
def server_session(fake_exe):
    sess = DiamondSession(diamond_exe=fake_exe, timeout=30, start_timeout=30)
    server._set_session(sess)
    yield sess
    sess.stop()
    server._set_session(None)


LDF_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<BaliProject version="3.2" title="proj" device="FAKE-DEV-1PKG" default_implementation="impl1">
    <Options/>
    <Implementation title="impl1" dir="impl1" description="impl1" synthesis="lse" default_strategy="Strategy1">
        <Options>
            <Option name="def_top" value="top"/>
        </Options>
        <Source name="src/top.v" type="Verilog" type_short="Verilog">
            <Options top_module="top"/>
        </Source>
        <Source name="proj.lpf" type="Logic Preference" type_short="LPF">
            <Options/>
        </Source>
    </Implementation>
    <Strategy name="Strategy1" file="proj.sty"/>
</BaliProject>
"""


@pytest.fixture
def ldf_project(tmp_path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "top.v").write_text("module top; endmodule\n")
    (tmp_path / "proj.lpf").write_text("")
    (tmp_path / "proj.sty").write_text("<Strategy/>\n")
    ldf = tmp_path / "proj.ldf"
    ldf.write_text(LDF_TEMPLATE)
    return ldf
