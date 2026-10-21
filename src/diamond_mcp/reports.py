# Copyright(c) 2026 Lattice Semiconductor Corporation. All rights reserved. DO NOT DISTRIBUTE.
# Subject to the terms of the AI Solutions Software License Agreement
"""
Parsers for Diamond implementation reports.

    .par  place-and-route report  -> parse_par()  (PAR_SUMMARY:: block)
    .twr  timing report           -> parse_twr()  (summary and constraint table)
    .mrp  map report              -> parse_mrp()  (Design Summary resource usage)

Formats were taken from Diamond 3.14.0.75.0 output. Parsers return only what
they can find, so a missing section yields a missing key rather than a guess.
"""

from __future__ import annotations

import re
from typing import Optional

_NA = {"<n/a>", "n/a", ""}


def _number(text: str) -> Optional[float]:
    text = text.strip()
    if text in _NA:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_par(text: str) -> dict:
    """Parse the machine-readable PAR_SUMMARY block and the CPU/real time lines."""
    raw: dict[str, str] = {}
    for match in re.finditer(r"^PAR_SUMMARY::(.+?)\s*=\s*(.*?)\s*$", text, re.MULTILINE):
        raw[re.sub(r"\s+", " ", match.group(1)).strip()] = match.group(2)
    if not raw:
        return {}

    def field(*needles: str) -> Optional[float]:
        for key, value in raw.items():
            if all(n in key for n in needles):
                return _number(value)
        return None

    def count(*needles: str) -> Optional[int]:
        value = field(*needles)
        return None if value is None else int(value)

    result: dict = {
        "run_status": raw.get("Run status"),
        "unrouted_connections": count("unrouted"),
        "setup_worst_slack_ns": field("slack", "setup"),
        "setup_timing_score_ns": field("Timing score", "setup"),
        "hold_worst_slack_ns": field("slack", "hold"),
        "hold_timing_score_ns": field("Timing score", "hold"),
        "errors": count("Number of errors"),
    }
    real = re.search(r"Total REAL time to completion:\s*(\d+)\s*secs", text)
    if real:
        result["real_time_s"] = int(real.group(1))
    return result


def parse_twr(text: str) -> dict:
    """Parse the timing summary and the setup constraint table of a .twr report."""
    result: dict = {}

    both = re.search(
        r"Timing errors:\s*(\d+)\s*\(setup\),\s*(\d+)\s*\(hold\)\s*\n\s*Score:\s*(\d+)\s*\(setup\),\s*(\d+)\s*\(hold\)",
        text,
    )
    if both:
        result.update(
            setup_errors=int(both.group(1)),
            hold_errors=int(both.group(2)),
            setup_score_ps=int(both.group(3)),
            hold_score_ps=int(both.group(4)),
        )
    else:
        setup_only = re.search(
            r"Timing summary \(Setup\):\s*-+\s*\n\s*Timing errors:\s*(\d+)\s+Score:\s*(\d+)", text
        )
        if setup_only:
            result.update(setup_errors=int(setup_only.group(1)), setup_score_ps=int(setup_only.group(2)))

    # The report prints one verdict per analysis, setup first, then hold.
    verdicts = list(re.finditer(r"All preferences were met\.|(\d+) preferences?\(marked by \"\*\" above\) not met", text))
    for label, match in zip(("setup", "hold"), verdicts):
        result[f"{label}_preferences_met"] = match.group(1) is None
        if match.group(1) is not None:
            result[f"{label}_preferences_not_met"] = int(match.group(1))

    constraints = []
    pending = ""
    table = re.search(
        r"^Preference\s*\|\s*Constraint\|\s*Actual\|Levels\s*\n-+\n(.*?)\n-{20,}",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if table:
        for line in table.group(1).splitlines():
            cells = [c.strip() for c in line.split("|")]
            if len(cells) != 4 or not cells[0]:
                continue
            if not any(cells[1:]):
                # A long preference name wraps; its numbers are on the next row.
                pending = f"{pending} {cells[0]}".strip()
                continue
            constraints.append(
                {
                    "preference": f"{pending} {cells[0]}".strip(),
                    "constraint": cells[1],
                    "actual": cells[2],
                    "levels": cells[3].rstrip("* ").strip(),
                    "met": not cells[3].rstrip().endswith("*"),
                }
            )
            pending = ""
    if constraints:
        result["constraints"] = constraints
    cover = re.search(r"Constraints cover (\d+) paths.*?\(([\d.]+)% coverage\)", text)
    if cover:
        result["constraint_coverage_percent"] = float(cover.group(2))
    return result


_MRP_ROW = re.compile(
    r"^\s*(?P<name>[A-Za-z][^:]*?):\s*(?P<used>\d+)(?:\s*\+\s*\d+\(\w+\))?\s+out of\s+(?P<total>\d+)\s+\((?P<pct>\d+)%\)\s*$"
)
_MRP_SUB = re.compile(r"^\s*(?P<name>Number used as [^:]+?):\s*(?P<used>\d+)\s*$")


def parse_mrp(text: str) -> dict:
    """Parse the Design Summary resource table of a .mrp map report."""
    start = text.find("Design Summary")
    if start < 0:
        return {}
    section = text[start:]
    end = section.find("Number of Signals")
    if end > 0:
        section = section[:end]

    resources: dict[str, dict] = {}
    for line in section.splitlines():
        row = _MRP_ROW.match(line)
        if row:
            resources[row.group("name").strip()] = {
                "used": int(row.group("used")),
                "total": int(row.group("total")),
                "percent": int(row.group("pct")),
            }
            continue
        sub = _MRP_SUB.match(line)
        if sub:
            resources[sub.group("name").strip()] = {"used": int(sub.group("used"))}
    return {"resources": resources} if resources else {}
