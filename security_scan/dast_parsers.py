#!/usr/bin/env python3
# Copyright(c) 2026 Lattice Semiconductor Corporation. All rights reserved. DO NOT DISTRIBUTE.
# Subject to the terms of the AI Solutions Software License Agreement
"""
Seeded fuzzing of everything that reads a file or builds Tcl, to find hangs and unexpected errors.

    python security_scan/dast_parsers.py [--src PATH/TO/src] [--cases 300] [--seed 20261026] [--out FILE]

Targets and what counts as a finding
  reports.parse_par / parse_twr / parse_mrp   must return a dict and never raise
  project.parse_ldf / find_missing_sources    may raise only ProjectFileError
  config.load_config_data                     must return a dict and never raise
  diamond_session.tcl_quote                   when tclsh is installed: the result must be exactly one
                                              Tcl word whose value is the original string

Every target is also fed nesting 100,000 levels deep and runs of one character 1 MB long. A call
that takes more than SLOW_SECONDS is reported as slow, and on POSIX one that takes more than
HANG_SECONDS is interrupted and reported as a hang. Inputs are mutations of the sample reports in
tests/data, and the same seed gives the same inputs.

Exit status: 0 no finding, 1 at least one.
"""

import argparse
import binascii
import json
import os
import platform
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(REPO, "tests", "data")
SLOW_SECONDS = 2.0
HANG_SECONDS = int(os.environ.get("DAST_HANG_SECONDS", "20"))

LDF_SEED = """<?xml version="1.0" encoding="UTF-8"?>
<BaliProject version="3.2" title="proj" device="FAKE-DEV-1PKG" default_implementation="impl1">
    <Options/>
    <Implementation title="impl1" dir="impl1" description="impl1" synthesis="lse" default_strategy="Strategy1">
        <Options><Option name="def_top" value="top"/></Options>
        <Source name="src/top.v" type="Verilog" type_short="Verilog"><Options top_module="top"/></Source>
    </Implementation>
    <Strategy name="Strategy1" file="proj.sty"/>
</BaliProject>
"""
CONFIG_SEED = '{"diamond_exe": "/opt/diamondc", "lm_license_file": "1234@host", "timeout_seconds": 3600, "audit_log": "off"}'


class Hang(Exception):
    pass


def _alarm(_signum, _frame):
    raise Hang()


def mutate(rng, text):
    """One random change to a text: cut, flip, delete or repeat lines, insert noise, repeat a chunk."""
    if not text:
        return text
    kind = rng.randrange(7)
    if kind == 0:
        return text[:rng.randrange(len(text))]
    if kind == 1:
        chars = list(text)
        for _ in range(rng.randrange(1, 20)):
            chars[rng.randrange(len(chars))] = chr(rng.choice([0, 1, 9, 10, 13, 34, 38, 39, 60, 62, 92, 123, 125, 0x2028, 0xFFFD, 0x10348]))
        return "".join(chars)
    lines = text.split("\n")
    if kind == 2:
        del lines[rng.randrange(len(lines))]
        return "\n".join(lines)
    if kind == 3:
        rng.shuffle(lines)
        return "\n".join(lines)
    if kind == 4:
        i = rng.randrange(len(lines))
        return "\n".join(lines[:i] + [lines[i]] * rng.randrange(2, 200) + lines[i:])
    if kind == 5:
        at = rng.randrange(len(text))
        return text[:at] + "".join(chr(rng.randrange(1, 0x2FF)) for _ in range(rng.randrange(1, 300))) + text[at:]
    at = rng.randrange(len(text))
    return text[:at] + text[at:at + rng.randrange(1, 400)] * rng.randrange(2, 400) + text[at:]


def extreme_inputs():
    return {
        "empty": "", "nul bytes": "\0" * 1000, "1 MB of a": "a" * 1_000_000, "1 MB of spaces": " " * 1_000_000,
        "1 MB of newlines": "\n" * 1_000_000, "1 MB of =": "=" * 1_000_000, "1 MB of tabs and spaces": "\t " * 500_000,
        "100,000 '[' ": "[" * 100_000, "100,000 '<a>'": "<a>" * 100_000, "100,000 '{'": "{" * 100_000,
        "marker repeated": "PAR_SUMMARY::" * 100_000, "table rows": ("| a | 1 | 2 | 3 |\n" * 100_000),
        "one 1 MB number": "9" * 1_000_000, "non-UTF-8": "\udcff\udcfe" * 1000,
    }


class Target:
    def __init__(self, name, run, allowed=()):
        self.name, self.run, self.allowed = name, run, allowed
        self.cases = 0
        self.exceptions = Counter()
        self.examples = {}
        self.slow = []
        self.hangs = []

    def feed(self, label, value):
        self.cases += 1
        started = time.monotonic()
        if hasattr(signal, "SIGALRM"):
            signal.alarm(HANG_SECONDS)
        try:
            self.run(value)
        except Hang:
            self.hangs.append(label)
        except self.allowed:
            pass
        except BaseException as exc:  # noqa: BLE001 - the point is to catch whatever escapes
            if isinstance(exc, KeyboardInterrupt):
                raise
            name = type(exc).__name__
            self.exceptions[name] += 1
            self.examples.setdefault(name, "%s: %s" % (label, str(exc)[:120]))
        finally:
            if hasattr(signal, "SIGALRM"):
                signal.alarm(0)
        elapsed = time.monotonic() - started
        if elapsed > SLOW_SECONDS:
            self.slow.append({"input": label, "seconds": round(elapsed, 2)})

    def summary(self):
        return {"target": self.name, "cases": self.cases, "unexpected_exceptions": dict(self.exceptions),
                "examples": self.examples, "slow_calls": self.slow[:10], "hangs": self.hangs[:10],
                "finding": bool(self.exceptions or self.hangs or self.slow)}


def tcl_quote_check(tcl_quote, rng, cases):
    """Return a list of (original, problem) for strings whose quoted form is not one Tcl word equal to them."""
    tclsh = shutil.which("tclsh")
    if not tclsh:
        return None, "tclsh is not installed"
    alphabet = list('\\{}[]$";&|<>()#\'`~ \t\n\r\x0b\x0c\x00\x01\x1b\x7f') + list("abcXYZ019_-./:=@") + ["é", "中", "\u2028", "\U0001F600"]
    strings = ["", "{", "}", "\\", "a\\", "{}", "}{", "$x", "[exit]", "a b", "\n", "\\\n", "[puts hi];", '"', "# c", "a\\{b", "}\\{",
               "\\u0041", "\\x41", "\\0", "%s" % ("{" * 200)]
    strings += ["".join(rng.choice(alphabet) for _ in range(rng.randrange(1, 40))) for _ in range(cases)]
    work = tempfile.mkdtemp(prefix="tclq_")
    try:
        lines = ["set bad 0", "proc check {n hex args} {",
                 "  global bad",
                 "  set want [encoding convertfrom utf-8 [binary decode hex $hex]]",
                 "  if {[llength $args] != 3 || [lindex $args 1] ne $want} {puts \"BAD $n\"; incr bad}",
                 "}"]
        for n, original in enumerate(strings):
            hex_value = binascii.hexlify(original.encode("utf-8")).decode() or "00"
            if not original:
                hex_value = ""
            lines.append("check %d {%s} a %s b" % (n, hex_value, tcl_quote(original)))
        lines.append("puts \"DONE $bad\"")
        path = os.path.join(work, "t.tcl")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")
        done = subprocess.run([tclsh, path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    bad = [int(line.split()[1]) for line in done.stdout.splitlines() if line.startswith("BAD ")]
    if "DONE" not in done.stdout:
        return [("(script)", "tclsh did not finish: %s" % (done.stderr.strip()[:200] or done.stdout[-200:]))], None
    return [(strings[n], "not a single word equal to the original") for n in bad], None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Seeded fuzzing of the parsers and the Tcl quoting.")
    parser.add_argument("--src", default=os.path.join(REPO, "src"))
    parser.add_argument("--cases", type=int, default=300, help="mutated inputs per target")
    parser.add_argument("--seed", type=int, default=20261026)
    parser.add_argument("--out")
    args = parser.parse_args(argv)

    sys.path.insert(0, os.path.abspath(args.src))
    sys.setrecursionlimit(1000)
    from diamond_mcp import config, project, reports  # noqa: E402
    from diamond_mcp.diamond_session import tcl_quote  # noqa: E402
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _alarm)

    rng = random.Random(args.seed)
    seeds = {n: open(os.path.join(DATA, n), encoding="utf-8", errors="replace").read()
             for n in ("sample.par", "sample_setup_hold.twr", "sample.mrp")}
    work = tempfile.mkdtemp(prefix="dast_parsers_")
    ldf_path = os.path.join(work, "p.ldf")
    config_path = os.path.join(work, "c.json")

    def run_ldf(text):
        with open(ldf_path, "w", encoding="utf-8", errors="surrogatepass", newline="") as handle:
            handle.write(text)
        project.parse_ldf(ldf_path)
        project.find_missing_sources(ldf_path)

    def run_config(text):
        with open(config_path, "w", encoding="utf-8", errors="surrogatepass", newline="") as handle:
            handle.write(text)
        result = config.load_config_data(__import__("pathlib").Path(config_path))
        assert isinstance(result, dict)

    targets = [
        (Target("reports.parse_par", lambda t: reports.parse_par(t)), seeds["sample.par"]),
        (Target("reports.parse_twr", lambda t: reports.parse_twr(t)), seeds["sample_setup_hold.twr"]),
        (Target("reports.parse_mrp", lambda t: reports.parse_mrp(t)), seeds["sample.mrp"]),
        (Target("project.parse_ldf + find_missing_sources", run_ldf, allowed=(project.ProjectFileError,)), LDF_SEED),
        (Target("config.load_config_data", run_config), CONFIG_SEED),
    ]
    extras = {
        "reports.parse_par": {
            "marker then 100,000 spaces": "PAR_SUMMARY::" + " " * 100_000,
            "marker, key, spaces, no '='": "PAR_SUMMARY::a" + " " * 100_000 + "x",
            "100,000 marker lines without '='": "PAR_SUMMARY::abc\n" * 100_000,
        },
        "reports.parse_twr": {
            "'Constraints cover' repeated on one line": "Constraints cover 1 paths " * 40_000,
            "table header repeated, no end": "Preference | Constraint| Actual|Levels\n-\n" * 25_000,
            "'Timing errors:' then spaces": "Timing errors:" + " " * 100_000 + "9",
            "'preferences' lookalikes": "9 preferences(marked by \"*\" abov " * 30_000,
            "digits then 'Timing summary (Setup):'": "9" * 100_000 + "Timing summary (Setup):" + "-" * 100_000,
        },
        "reports.parse_mrp": {
            "row with 100,000 digits": "Design Summary\nA: " + "9" * 100_000 + " out of",
            "row with 100,000 spaces": "Design Summary\nA: 1 " + " " * 100_000 + "x",
            "100,000 'Number used as' lines": "Design Summary\n" + "Number used as x: 1 \n" * 100_000,
            "'Number used as' + colon-free run": "Design Summary\nNumber used as " + "x" * 100_000,
        },
        "project.parse_ldf + find_missing_sources": {
        "entity bomb": ('<?xml version="1.0"?><!DOCTYPE BaliProject [<!ENTITY a "' + "a" * 1000 + '"><!ENTITY b "' + "&a;" * 1000 + '">'
                        '<!ENTITY c "' + "&b;" * 1000 + '">]><BaliProject title="&c;"/>'),
        "50,000 deep elements": "<BaliProject>" + "<a>" * 50_000 + "</a>" * 50_000 + "</BaliProject>"}}

    try:
        for target, seed_text in targets:
            for label, value in list(extreme_inputs().items()) + list(extras.get(target.name, {}).items()):
                target.feed(label, value)
            for n in range(args.cases):
                target.feed("mutation %d" % n, mutate(rng, seed_text))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    summaries = [t.summary() for t, _ in targets]
    bad, skipped_reason = tcl_quote_check(tcl_quote, rng, args.cases)
    quote = {"target": "diamond_session.tcl_quote (checked with tclsh)", "cases": None if bad is None else args.cases + 20,
             "skipped": skipped_reason, "not_one_word": [{"input": repr(o), "problem": p} for o, p in (bad or [])][:20],
             "finding": bool(bad)}
    summaries.append(quote)

    findings = [s for s in summaries if s["finding"]]
    result = {"tool": "dast_parsers.py", "src": os.path.abspath(args.src), "seed": args.seed, "cases_per_target": args.cases,
              "python": platform.python_version(), "targets": summaries, "findings": len(findings)}
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)
    for s in summaries:
        detail = ("skipped: %s" % s["skipped"]) if s.get("skipped") else "%s cases" % s["cases"]
        print("  %-5s %-52s %s" % ("FIND" if s["finding"] else "ok", s["target"], detail))
        for name, count in s.get("unexpected_exceptions", {}).items():
            print("         %d x %s, e.g. %s" % (count, name, s["examples"][name]))
        for item in s.get("slow_calls", []):
            print("         slow: %(input)s %(seconds)s s" % item)
        for item in s.get("hangs", []):
            print("         HANG: %s" % item)
        for item in s.get("not_one_word", [])[:5]:
            print("         %(input)s: %(problem)s" % item)
    print("dast_parsers: %d finding(s)" % len(findings))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
