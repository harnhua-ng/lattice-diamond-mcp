#!/usr/bin/env python3
# Copyright(c) 2026 Lattice Semiconductor Corporation. All rights reserved. DO NOT DISTRIBUTE.
# Subject to the terms of the AI Solutions Software License Agreement
"""
Dynamic probes of the running server: the real program, started as an AI client would start it
and spoken to over stdio, plus one check that it opens no network port.

    python security_scan/dast_stdio.py [--src PATH/TO/src] [--out dast_stdio.json]

--src is the folder that contains the diamond_mcp package; the default is this repository's src.
Point it at another checkout (for example `git worktree add ../before <commit>`) to run the same
probes against older code. The probes run in a scratch folder with its own HOME, so nothing on
the real account is read or written.

Groups of probes
  robustness  malformed, mistyped and oversized JSON-RPC frames; the server must keep answering.
  files       read_report must stay inside a project and not follow links out of it; a slice of a
              30 MB log must come back correctly and quickly (memory use is not measured).
  program     start_diamond and configure_diamond must not run or save an arbitrary program.
  config      a diamond-mcp.json in the working directory must be ignored; a hostile config file
              must not crash the server.
  xml         a project file that declares a DTD or entity must be refused.
  network     --transport sse and streamable-http must not start a listener.

Exit status: 0 every probe passed, 1 at least one failed, 2 the harness itself failed.
"""

import argparse
import importlib.metadata
import json
import os
import platform
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IS_WINDOWS = os.name == "nt"

LDF = """<?xml version="1.0" encoding="UTF-8"?>
<BaliProject version="3.2" title="proj" device="FAKE-DEV-1PKG" default_implementation="impl1">
    <Options/>
    <Implementation title="impl1" dir="impl1" description="impl1" synthesis="lse" default_strategy="Strategy1">
        <Options><Option name="def_top" value="top"/></Options>
        <Source name="top.v" type="Verilog" type_short="Verilog"><Options top_module="top"/></Source>
    </Implementation>
    <Strategy name="Strategy1" file="proj.sty"/>
</BaliProject>
"""
BOMB = ('<?xml version="1.0"?>\n<!DOCTYPE BaliProject [\n<!ENTITY a "aaaaaaaaaa">\n'
        '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">\n<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">\n]>\n'
        '<BaliProject title="&c;" device="X"/>\n')


class Harness(Exception):
    pass


class Server:
    """The server as a child process, speaking newline-delimited JSON-RPC on stdio."""

    def __init__(self, src, env, cwd, args=()):
        self.env = dict(env, PYTHONPATH=src)
        self.stderr = tempfile.TemporaryFile()
        self.process = subprocess.Popen([sys.executable, "-m", "diamond_mcp", *args], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=self.stderr, env=self.env, cwd=cwd)
        self.lines = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self.next_id = 1

    def _read(self):
        for line in self.process.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def send_raw(self, data):
        try:
            self.process.stdin.write(data)
            self.process.stdin.flush()
        except (OSError, ValueError):
            pass

    def send(self, message):
        self.send_raw((json.dumps(message) + "\n").encode())

    def wait_for(self, request_id, timeout):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                return None
            if line is None:
                return None
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("id") == request_id:
                return message

    def request(self, method, params=None, timeout=30):
        request_id = self.next_id
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        return self.wait_for(request_id, timeout)

    def initialise(self):
        answer = self.request("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                             "clientInfo": {"name": "dast", "version": "1"}}, timeout=60)
        if not answer or "result" not in answer:
            raise Harness("the server did not answer initialize: %r" % (answer,))
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return answer["result"]

    def tool(self, name, arguments, timeout=60):
        """Call a tool. Returns (payload dict or None, seconds, raw answer)."""
        started = time.monotonic()
        answer = self.request("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)
        elapsed = time.monotonic() - started
        payload = None
        try:
            payload = json.loads(answer["result"]["content"][0]["text"])
        except (TypeError, KeyError, IndexError, ValueError):
            pass
        return payload, elapsed, answer

    def responsive(self, timeout=15):
        answer = self.request("tools/list", timeout=timeout)
        return bool(answer and "result" in answer)

    def alive(self):
        return self.process.poll() is None

    def stop(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.stderr.close()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def build_scratch(work):
    """Create the scratch tree: an account home, a project, a file outside it, and a hostile program."""
    layout = {name: os.path.join(work, name) for name in ("home", "outside", "proj", "cwd", "bin")}
    for path in layout.values():
        os.makedirs(path)
    os.makedirs(os.path.join(layout["proj"], "impl1"))
    with open(os.path.join(layout["proj"], "proj.ldf"), "w") as handle:
        handle.write(LDF)
    with open(os.path.join(layout["proj"], "impl1", "proj_impl1.par"), "w") as handle:
        handle.write("PAR report of the project\n")
    with open(os.path.join(layout["outside"], "secret.log"), "w") as handle:
        handle.write("SECRET outside any project\n")
    with open(os.path.join(layout["outside"], "bomb.ldf"), "w") as handle:
        handle.write(BOMB)
    big = os.path.join(layout["proj"], "impl1", "big.log")
    with open(big, "w") as handle:
        line = "x" * 1023 + "\n"
        for _ in range(30 * 1024):
            handle.write(line)
    if not IS_WINDOWS:
        os.symlink(os.path.join(layout["outside"], "secret.log"), os.path.join(layout["proj"], "link.log"))
    layout["marker"] = os.path.join(work, "evil-ran")
    layout["evil"] = os.path.join(layout["bin"], "evil" + (".bat" if IS_WINDOWS else ""))
    with open(layout["evil"], "w") as handle:
        handle.write(("@echo off\r\necho x> \"%s\"\r\n" if IS_WINDOWS else "#!/bin/sh\ntouch '%s'\n") % layout["marker"])
    # A scratch script the probes try to get the server to run; it has to be executable to prove that it did not run.
    os.chmod(layout["evil"], 0o755)  # nosec B103
    with open(os.path.join(layout["cwd"], "diamond-mcp.json"), "w") as handle:
        json.dump({"diamond_exe": layout["evil"], "audit_log": "off", "lm_license_file": "evil@host"}, handle)
    with open(os.path.join(work, "deep.json"), "w") as handle:
        handle.write("[" * 200_000 + "]" * 200_000)
    return layout


def base_env(layout):
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL")}
    home = layout["home"]
    env.update(HOME=home, USERPROFILE=home, APPDATA=home, LOCALAPPDATA=home, XDG_STATE_HOME=os.path.join(home, "state"),
               PYTHONDONTWRITEBYTECODE="1")
    return env


class Report:
    def __init__(self):
        self.results = []

    def add(self, group, name, expected, passed, evidence, skipped=False):
        self.results.append({"group": group, "probe": name, "expected": expected, "passed": bool(passed) or skipped,
                             "skipped": skipped, "evidence": evidence})
        print("  %-4s %-9s %s" % ("SKIP" if skipped else "PASS" if passed else "FAIL", group, name))
        if not passed and not skipped:
            print("         expected: %s\n         observed: %s" % (expected, evidence))


def robustness(report, server):
    deep = "[" * 50_000 + "]" * 50_000
    frames = [
        ("invalid JSON", b"{not json\n"),
        ("empty line", b"\n"),
        ("array instead of object", b"[1,2,3]\n"),
        ("unknown method", json.dumps({"jsonrpc": "2.0", "id": 900, "method": "no/such", "params": {}}).encode() + b"\n"),
        ("params of the wrong type", json.dumps({"jsonrpc": "2.0", "id": 901, "method": "tools/call", "params": [1]}).encode() + b"\n"),
        ("tool name of the wrong type", json.dumps({"jsonrpc": "2.0", "id": 902, "method": "tools/call", "params": {"name": 5, "arguments": {}}}).encode() + b"\n"),
        ("object as request id", b'{"jsonrpc":"2.0","id":{"a":1},"method":"tools/list"}\n'),
        ("50,000-deep nesting", ('{"jsonrpc":"2.0","id":903,"method":"tools/call","params":{"name":"get_diamond_config","arguments":{"x":%s}}}\n' % deep).encode()),
        ("5,000-digit integer", ('{"jsonrpc":"2.0","id":904,"method":"tools/list","params":{"n":%s}}\n' % ("9" * 5000)).encode()),
        ("1 MB string argument", json.dumps({"jsonrpc": "2.0", "id": 905, "method": "tools/call",
                                             "params": {"name": "read_report", "arguments": {"file_path": "a" * 1_000_000}}}).encode() + b"\n"),
        ("100,000-element array", json.dumps({"jsonrpc": "2.0", "id": 906, "method": "tools/call",
                                              "params": {"name": "add_sources", "arguments": {"files": ["f"] * 100_000}}}).encode() + b"\n"),
        ("binary garbage", bytes(range(256)) * 20 + b"\n"),
    ]
    for name, raw in frames:
        started = time.monotonic()
        server.send_raw(raw)
        alive = server.alive() and server.responsive(timeout=30)
        report.add("robustness", "frame: " + name, "the server keeps answering", alive,
                   "answered tools/list afterwards in %.2f s" % (time.monotonic() - started) if alive else "no answer, or the server exited")
        if not server.alive():
            raise Harness("the server died on: " + name)


def files(report, server, layout):
    proj = os.path.join(layout["proj"], "proj.ldf")
    secret = os.path.join(layout["outside"], "secret.log")
    par = os.path.join(layout["proj"], "impl1", "proj_impl1.par")

    payload, _, _ = server.tool("read_report", {"file_path": par})
    report.add("files", "read_report before any project is known", "refused",
               payload and not payload.get("success"), (payload or {}).get("output", "")[:120])

    server.tool("get_project_info", {"project_path": proj})
    payload, _, _ = server.tool("read_report", {"file_path": par})
    report.add("files", "read_report of the project's own report (control)", "returned",
               payload and payload.get("success") and "PAR report" in payload.get("output", ""), (payload or {}).get("output", "")[:80])

    payload, _, _ = server.tool("read_report", {"file_path": secret})
    report.add("files", "read_report of a .log outside the project", "refused",
               payload and not payload.get("success"), "returned: %r" % payload.get("output", "")[:60] if payload and payload.get("success") else (payload or {}).get("output", "")[:120])

    if IS_WINDOWS:
        report.add("files", "read_report through a link that leads out of the project", "refused", True, "skipped on Windows (no symlink privilege)")
    else:
        payload, _, _ = server.tool("read_report", {"file_path": os.path.join(layout["proj"], "link.log")})
        report.add("files", "read_report through a link that leads out of the project", "refused",
                   payload and not payload.get("success"), "returned: %r" % payload.get("output", "")[:60] if payload and payload.get("success") else (payload or {}).get("output", "")[:120])

    payload, elapsed, _ = server.tool("read_report", {"file_path": os.path.join(layout["proj"], "impl1", "big.log"),
                                                      "max_chars": 100, "tail": True}, timeout=120)
    report.add("files", "tail of a 30 MB log returns only the slice", "100 characters, in reasonable time",
               payload and payload.get("success") and payload.get("returned_chars") == 100 and elapsed < 60,
               "returned %s characters of %s in %.2f s" % ((payload or {}).get("returned_chars"), (payload or {}).get("total_chars"), elapsed))


def program(report, server, layout):
    marker = layout["marker"]
    payload, _, _ = server.tool("start_diamond", {"exe_path": layout["evil"]}, timeout=30)
    time.sleep(1.0)
    ran = os.path.exists(marker)
    report.add("program", "start_diamond with an arbitrary program", "refused, and the program does not run",
               payload and not payload.get("success") and not ran,
               "the program ran (marker file written)" if ran else (payload or {}).get("output", "")[:120])
    server.tool("stop_diamond", {})
    if os.path.exists(marker):
        os.remove(marker)

    saved = [p for p in (os.path.join(layout["home"], ".config", "diamond-mcp", "config.json"),
                         os.path.join(layout["home"], "diamond-mcp", "config.json")) if os.path.exists(p)]
    payload, _, _ = server.tool("configure_diamond", {"diamond_exe": layout["evil"]})
    saved_after = [p for p in (os.path.join(layout["home"], ".config", "diamond-mcp", "config.json"),
                               os.path.join(layout["home"], "diamond-mcp", "config.json")) if os.path.exists(p)]
    wrote = bool(saved_after) and not saved
    report.add("program", "configure_diamond saving an arbitrary program", "refused, and nothing saved",
               payload and not payload.get("success") and not wrote,
               "saved to " + saved_after[0] if wrote else (payload or {}).get("output", "")[:120])


def config(report, server, layout, deep_server):
    payload, _, _ = server.tool("get_diamond_config", {})
    honoured = payload and (payload.get("diamond_exe") == layout["evil"] or payload.get("audit_log") is None
                            or payload.get("lm_license_file_set"))
    report.add("config", "diamond-mcp.json in the working directory", "ignored",
               payload and payload.get("success") and not honoured,
               "diamond_exe=%s audit_log=%s license_set=%s config_file=%s" % (
                   (payload or {}).get("diamond_exe"), (payload or {}).get("audit_log"),
                   (payload or {}).get("lm_license_file_set"), (payload or {}).get("config_file")))

    payload, _, answer = deep_server.tool("get_diamond_config", {})
    report.add("config", "config file nested 200,000 levels deep", "ignored; the tool still answers",
               payload and payload.get("success"),
               "success=%s output=%r" % (payload.get("success"), payload.get("output", "")[:100]) if payload else json.dumps(answer)[:160])


def xml(report, server, layout):
    payload, elapsed, _ = server.tool("get_project_info", {"project_path": os.path.join(layout["outside"], "bomb.ldf")})
    report.add("xml", "project file with a DTD and nested entities", "refused as a DTD",
               payload and not payload.get("success") and "DTD" in payload.get("output", ""),
               "success=%s output=%r (%.2f s)" % ((payload or {}).get("success"), (payload or {}).get("output", "")[:80], elapsed))


def port_in_use(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def network(report, src, layout):
    # The MCP SDK's HTTP transports listen on 127.0.0.1:8000 and, in the version tested, ignore FASTMCP_PORT.
    port = 8000
    env = dict(base_env(layout), PYTHONPATH=src, FASTMCP_PORT=str(port), FASTMCP_HOST="127.0.0.1")
    for transport in ("streamable-http", "sse"):
        if port_in_use(port):
            report.add("network", "--transport %s" % transport, "refused: no listener, non-zero exit", True,
                       "something already listens on 127.0.0.1:%d, so a listener of ours could not be told apart" % port, skipped=True)
            continue
        process = subprocess.Popen([sys.executable, "-m", "diamond_mcp", "--transport", transport], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, cwd=layout["cwd"])
        listening, status = False, None
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and process.poll() is None and not listening:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                        listening = True
                except OSError:
                    time.sleep(0.25)
            if listening:
                body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "dast", "version": "1"}}}).encode()
                path = "/mcp" if transport == "streamable-http" else "/sse"
                request = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=body if transport == "streamable-http" else None,
                                                 headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
                try:
                    # Loopback address and fixed http scheme: this probes the server's own listener.
                    with urllib.request.urlopen(request, timeout=5) as response:  # nosec B310
                        status = response.status
                except urllib.error.HTTPError as exc:
                    status = exc.code
                except OSError as exc:
                    status = "error: %s" % exc
        finally:
            exit_code = process.poll()
            process.kill()
            process.wait()
        report.add("network", "--transport %s" % transport, "refused: no listener, non-zero exit",
                   not listening and exit_code not in (None, 0),
                   "listening on 127.0.0.1:%d; an unauthenticated request got HTTP %s" % (port, status) if listening
                   else "exit status %s, nothing listening" % exit_code)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Dynamic probes of the running diamond-mcp server.")
    parser.add_argument("--src", default=os.path.join(REPO, "src"), help="folder that contains the diamond_mcp package")
    parser.add_argument("--out", help="write the result as JSON to this file")
    args = parser.parse_args(argv)
    src = os.path.abspath(args.src)
    work = tempfile.mkdtemp(prefix="dast_stdio_")
    report = Report()
    servers = []
    try:
        layout = build_scratch(work)
        env = base_env(layout)
        located = subprocess.run([sys.executable, "-c", "import diamond_mcp; print(diamond_mcp.__file__)"],
                                 env=dict(env, PYTHONPATH=src), stdout=subprocess.PIPE, universal_newlines=True).stdout.strip()
        if os.path.commonpath([os.path.dirname(located), src]) != src:
            raise Harness("diamond_mcp is imported from %s, not from --src %s" % (located, src))
        server = Server(src, env, layout["cwd"])
        servers.append(server)
        info = server.initialise()
        print("dast_stdio: %s %s, python %s, imported from %s" % (
            info["serverInfo"]["name"], info["serverInfo"].get("version", ""), platform.python_version(), located))
        robustness(report, server)
        files(report, server, layout)
        program(report, server, layout)
        deep_env = dict(env, DIAMOND_MCP_CONFIG=os.path.join(work, "deep.json"))
        deep_server = Server(src, deep_env, layout["proj"])
        servers.append(deep_server)
        deep_server.initialise()
        config(report, server, layout, deep_server)
        xml(report, server, layout)
        network(report, src, layout)
    except Harness as exc:
        print("harness failure: %s" % exc, file=sys.stderr)
        return 2
    finally:
        for server in servers:
            server.stop()
        shutil.rmtree(work, ignore_errors=True)

    failed = [r for r in report.results if not r["passed"]]
    result = {"tool": "dast_stdio.py", "src": src, "python": platform.python_version(), "platform": platform.platform(),
              "mcp": importlib.metadata.version("mcp"), "diamond_mcp_imported_from": located,
              "probes": len(report.results), "passed": len(report.results) - len(failed), "failed": len(failed),
              "results": report.results}
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)
    skipped = sum(1 for r in report.results if r["skipped"])
    result["skipped"] = skipped
    print("dast_stdio: %d probes, %d passed (%d skipped), %d failed" % (result["probes"], result["passed"], skipped, result["failed"]))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
