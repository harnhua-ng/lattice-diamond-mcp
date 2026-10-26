#!/usr/bin/env python3
# Copyright(c) 2026 Lattice Semiconductor Corporation. All rights reserved. DO NOT DISTRIBUTE.
# Subject to the terms of the AI Solutions Software License Agreement
"""
Run every scan in this folder, write each result as JSON, and fail if any step fails.

    python security_scan/run_scans.py [--out security_scan/out] [--skip-osv] [--skip-dast]

Needs Python 3.11 or later, `bandit` (pip install bandit) and the project's own dependencies
importable (the dynamic probes start the real server). The OSV step needs network access.

Steps
  bandit        static analysis of src, scripts and security_scan; fails on any Medium or High finding
                that is not suppressed (a suppression carries its reason in the source); a second run
                with --ignore-nosec records what the suppressions hide
  secrets       secrets_scan.py over the tracked files
  osv           osv_check.py: advisories for the packages pinned in uv.lock (runtime ones fail)
  dast_parsers  dast_parsers.py: seeded fuzzing of the report, project and config parsers and Tcl quoting
  dast_stdio    dast_stdio.py: the running server probed over stdio, plus the network transport check

Exit status: 0 every step passed, 1 a step found something, 2 a step could not run.
"""

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def run(command, **kwargs):
    return subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, cwd=REPO, **kwargs)


def bandit_step(out):
    path = os.path.join(out, "bandit.json")
    done = run([sys.executable, "-m", "bandit", "-r", "src", "scripts", "security_scan", "-f", "json", "-o", path, "-q"])
    if not os.path.exists(path):
        return 2, "bandit did not run (pip install bandit): " + done.stdout.strip()[-200:]
    run([sys.executable, "-m", "bandit", "-r", "src", "scripts", "security_scan", "--ignore-nosec", "-f", "json",
         "-o", os.path.join(out, "bandit_ignore_nosec.json"), "-q"])
    with open(path, encoding="utf-8") as handle:
        totals = json.load(handle)["metrics"]["_totals"]
    high, medium, low = totals["SEVERITY.HIGH"], totals["SEVERITY.MEDIUM"], totals["SEVERITY.LOW"]
    return (1 if high or medium else 0), "%d high, %d medium, %d low (low are informational)" % (high, medium, low)


def script_step(name, args, out):
    path = os.path.join(out, name + ".json")
    done = run([sys.executable, os.path.join(HERE, name + ".py"), "--out", path] + args)
    lines = [line for line in done.stdout.strip().splitlines() if line.strip()]
    return done.returncode, (lines[-1] if lines else "(no output)"), done.stdout


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run every security scan in this folder.")
    parser.add_argument("--out", default=os.path.join(HERE, "out"), help="folder for the JSON results (default: security_scan/out)")
    parser.add_argument("--skip-osv", action="store_true", help="do not query OSV.dev (no network)")
    parser.add_argument("--skip-dast", action="store_true", help="do not run the dynamic probes")
    args = parser.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)

    steps = [("bandit", lambda: bandit_step(args.out) + ("",)),
             ("secrets", lambda: script_step("secrets_scan", [], args.out))]
    if not args.skip_osv:
        steps.append(("osv", lambda: script_step("osv_check", [], args.out)))
    if not args.skip_dast:
        steps.append(("dast_parsers", lambda: script_step("dast_parsers", [], args.out)))
        steps.append(("dast_stdio", lambda: script_step("dast_stdio", [], args.out)))

    summary, worst = [], 0
    for name, step in steps:
        started = time.monotonic()
        status, detail, log = step()
        seconds = round(time.monotonic() - started, 1)
        if log:
            with open(os.path.join(args.out, name + ".log"), "w", encoding="utf-8") as handle:
                handle.write(log)
        summary.append({"step": name, "exit": status, "seconds": seconds, "detail": detail})
        worst = max(worst, status)
        print("%-13s %-6s %6.1f s  %s" % (name, "ok" if status == 0 else "FAIL" if status == 1 else "ERROR", seconds, detail), flush=True)
    with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)
    return worst


if __name__ == "__main__":
    sys.exit(main())
