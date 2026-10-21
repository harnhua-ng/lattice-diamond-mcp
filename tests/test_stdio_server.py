"""End-to-end test: launch the server as a subprocess and talk MCP over stdio."""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from conftest import needs_tclsh

pytestmark = needs_tclsh

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _params(fake_exe: str, tmp_path: Path) -> StdioServerParameters:
    env = {
        **os.environ,
        "DIAMOND_EXE": fake_exe,
        "PYTHONPATH": SRC,
        "DIAMOND_MCP_CONFIG": str(tmp_path / "none.json"),
        "DIAMOND_MCP_AUDIT_LOG": str(tmp_path / "stdio-audit.log"),
    }
    return StdioServerParameters(command=sys.executable, args=["-m", "diamond_mcp"], env=env)


def _payload(result) -> dict:
    assert not result.isError, result
    return json.loads(result.content[0].text)


def test_server_lists_tools_and_runs_a_flow_over_stdio(fake_exe, ldf_project, tmp_path):
    async def scenario():
        async with stdio_client(_params(fake_exe, tmp_path)) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = {t.name for t in (await client.list_tools()).tools}
                assert {"open_project", "compile_project", "run_tcl", "generate_bitstream"} <= tools

                answer = _payload(await client.call_tool("run_tcl", {"command": "expr {6*7}"}))
                assert answer["success"] and answer["output"] == "42"

                compiled = _payload(
                    await client.call_tool("compile_project", {"project_path": str(ldf_project)})
                )
                assert compiled["success"], compiled
                assert compiled["bitstream_files"]
                status = _payload(await client.call_tool("get_session_status", {"log_lines": 0}))
                assert status["is_running"]

    asyncio.run(asyncio.wait_for(scenario(), timeout=120))


def test_stdio_server_writes_an_audit_log(fake_exe, ldf_project, tmp_path):
    async def scenario():
        async with stdio_client(_params(fake_exe, tmp_path)) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                await client.call_tool("run_tcl", {"command": "exec echo audited"})
                await client.call_tool("run_tcl", {"command": "prj_project close"})
                await client.call_tool("compile_project", {"project_path": str(ldf_project)})
                await client.call_tool("stop_diamond", {})

    asyncio.run(asyncio.wait_for(scenario(), timeout=120))
    entries = [json.loads(line) for line in (tmp_path / "stdio-audit.log").read_text().splitlines()]
    by_event = {}
    for entry in entries:
        by_event.setdefault(entry["event"], []).append(entry)
    assert by_event["session_start"] and by_event["session_end"]
    commands = by_event["command"]
    assert commands[0]["command"] == "exec echo audited" and commands[0]["tool"] == "run_tcl"
    assert any(c["tool"] == "compile_project" and "prj_run PAR" in c["command"] for c in commands)
    assert by_event["refused"][0]["command"] == "prj_project close"
    assert all(e["user"] and e["pid"] for e in entries)
