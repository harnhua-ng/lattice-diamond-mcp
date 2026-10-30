#!/usr/bin/env python3
# Copyright(c) 2026 Lattice Semiconductor Corporation. All rights reserved. DO NOT DISTRIBUTE.
# Subject to the terms of the AI Solutions Software License Agreement
"""
Talk MCP to an installed Lattice Diamond MCP server and check that it answers.

    python scripts/smoke_test.py PROGRAM [--args "-I -m diamond_mcp"]

PROGRAM is what a client would start: for the embedded packages the bundled Python (`python.exe` on Windows,
`bin/python3.12` on Linux) with `--args "-I -m diamond_mcp"`. The script checks that `--help` describes the server,
then speaks JSON-RPC over stdio: initialize, tools/list (at least MIN_TOOLS tools including the core ones) and a
get_session_status call. A hung or misbehaving program fails cleanly (exit status 1, never a traceback).

Standard library only (Python 3.8 or later).
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

MIN_TOOLS = 30
SMOKE_TIMEOUT_S = 180
REQUIRED_TOOLS = {"compile_project", "run_tcl", "open_project"}


class SmokeError(Exception):
    pass


def clean_env(**extra):
    """The environment for the program: nothing from the Python that runs this script leaks in."""
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")}
    env["DIAMOND_MCP_AUDIT_LOG"] = "off"
    env.pop("DIAMOND_EXE", None)
    env.update(extra)
    return env


def mcp_smoke_test(program, args=()):
    """Run the program (with `args`) and speak MCP to it over stdio. Returns the tool names; SmokeError on any failure."""
    env = clean_env()
    command = [str(program)] + list(args)
    helped = subprocess.run(command + ["--help"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
                            stdin=subprocess.DEVNULL, timeout=SMOKE_TIMEOUT_S)
    if helped.returncode != 0 or "diamond-mcp" not in helped.stdout:
        raise SmokeError("--help did not describe diamond-mcp (exit status %d):\n%s" % (helped.returncode, helped.stdout[-400:]))

    errors = tempfile.TemporaryFile("w+")
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True, env=env)
    watchdog = threading.Timer(SMOKE_TIMEOUT_S, proc.kill)  # a hung program must not hang the caller
    watchdog.start()

    def stderr_tail():
        errors.seek(0)
        return errors.read()[-400:]

    def exchange(request_id, method, params=None):
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
        except OSError:
            raise SmokeError("The program exited before it read %s. stderr: %s" % (method, stderr_tail()))
        while True:
            line = proc.stdout.readline()
            if not line:
                raise SmokeError("The program closed its output during %s (or timed out). stderr: %s" % (method, stderr_tail()))
            try:
                reply = json.loads(line)
            except ValueError:
                raise SmokeError("The program wrote something that is not JSON-RPC to stdout during %s: %r" % (method, line[:120]))
            if reply.get("id") == request_id:
                if "error" in reply:
                    raise SmokeError("%s returned an error: %s" % (method, reply["error"]))
                return reply["result"]

    try:
        exchange(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke-test", "version": "1"}})
        try:
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            proc.stdin.flush()
        except OSError:
            raise SmokeError("The program exited right after initialize. stderr: %s" % stderr_tail())
        names = [tool["name"] for tool in exchange(2, "tools/list")["tools"]]
        if len(names) < MIN_TOOLS or not REQUIRED_TOOLS <= set(names):
            raise SmokeError("tools/list returned %d tools (%s ...)" % (len(names), ", ".join(names[:5])))
        status = exchange(3, "tools/call", {"name": "get_session_status", "arguments": {"log_lines": 0}})
        if status.get("isError"):
            raise SmokeError("get_session_status failed: %s" % status)
    finally:
        watchdog.cancel()
        try:
            proc.stdin.close()
            proc.wait(timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
        errors.close()
    return names


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check that a Lattice Diamond MCP server answers over stdio.")
    parser.add_argument("program", help="the program to start, for example the bundled python.exe or bin/python3.12")
    parser.add_argument("--args", default="", help='its arguments as one string, for example --args="-I -m diamond_mcp"')
    args = parser.parse_args(argv)
    try:
        names = mcp_smoke_test(Path(args.program), shlex.split(args.args))
    except (SmokeError, OSError, subprocess.SubprocessError) as exc:
        print("\nSMOKE TEST FAILED: %s" % exc, file=sys.stderr)
        return 1
    print("MCP smoke test passed (%d tools)" % len(names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
