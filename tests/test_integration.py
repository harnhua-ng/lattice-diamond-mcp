"""
Integration test against a real Lattice Diamond installation.

Skipped unless DIAMOND_TEST_LDF points at a small, compilable .ldf project.
The project is copied to a temporary directory first, so the original is left
untouched. By default the .ldf's own directory is copied; set DIAMOND_TEST_ROOT
to a higher directory when the project references sources outside its folder.
A licence must be available (LM_LICENSE_FILE or the config file), and
DIAMOND_EXE (or a discoverable installation) must resolve.

    DIAMOND_TEST_LDF=/path/to/design/project.ldf pytest -m integration
"""

import os
import shutil
from pathlib import Path

import pytest

from diamond_mcp import server
from diamond_mcp.config import resolve_diamond_path

LDF = os.environ.get("DIAMOND_TEST_LDF")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not LDF, reason="set DIAMOND_TEST_LDF to run against real Diamond"),
]


@pytest.fixture
def real_project(tmp_path) -> Path:
    source = Path(LDF).resolve()
    root = Path(os.environ.get("DIAMOND_TEST_ROOT") or source.parent).resolve()
    target = tmp_path / root.name
    shutil.copytree(root, target)
    return target / source.relative_to(root)


@pytest.fixture(autouse=True)
def fresh_session():
    server._set_session(None)
    yield
    session = server._SESSION
    if session is not None:
        session.stop()
    server._set_session(None)


def test_environment_is_ready():
    assert resolve_diamond_path().exists, "no Diamond executable resolved"
    assert server.check_environment()["ready"]


def test_compile_project_with_real_diamond(real_project):
    result = server.compile_project(str(real_project), bitstream=True, timeout=1200)
    assert result["success"], result
    assert [s["stage"] for s in result["stages"]][:5] == ["open", "Synthesis", "Translate", "Map", "PAR"]
    assert result["timing"]["par"]["run_status"] == "Completed"
    assert result["resources"]["resources"]
    assert any(f.endswith(".bit") for f in result.get("bitstream_files", []))


def test_repeated_open_and_close_does_not_crash_diamond(real_project):
    # Regression: Diamond 3.14 crashes intermittently on prj_project close.
    for _ in range(5):
        assert server.open_project(str(real_project))["success"]
        assert server.run_tcl("prj_dev")["success"]
        assert server.close_project()["success"]
    assert server.get_session_status(log_lines=0)["last_exit"] is None


def test_failure_reports_hints_and_session_survives(real_project):
    assert server.open_project(str(real_project))["success"]
    result = server.run_flow("NoSuchStage")
    assert not result["success"] and result["hints"]
    assert server.run_tcl("prj_dev")["success"]
