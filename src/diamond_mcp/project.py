# Copyright(c) 2026 Lattice Semiconductor Corporation. All rights reserved. DO NOT DISTRIBUTE.
# Subject to the terms of the AI Solutions Software License Agreement
"""
Read-only helpers for Diamond project files (.ldf).

These work without a running Diamond session. They exist because Diamond's own
diagnostics for a broken project are poor: a device it does not recognise is
reported only as "Error in reading project file", and a source file whose name
differs from the project's reference only by letter case (a Windows-authored
project opened on Linux) is reported as "Cannot find ... File does not exist"
only after synthesis has started.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

# Source types that are not inputs to synthesis, map or PAR.
_NON_FLOW_TYPE_SHORT = {"SPF"}


class ProjectFileError(ValueError):
    """The .ldf file is missing or is not a readable Diamond project."""


def _load(path: str) -> ET.Element:
    ldf = Path(path)
    if not ldf.is_file():
        raise ProjectFileError(f"Project file does not exist: {path}")
    try:
        root = ET.parse(ldf).getroot()
    except ET.ParseError as exc:
        raise ProjectFileError(f"Project file is not valid XML: {exc}") from exc
    if root.tag != "BaliProject":
        raise ProjectFileError(f"Not a Diamond project (root element is <{root.tag}>): {path}")
    return root


def _options(element: Optional[ET.Element]) -> dict[str, str]:
    if element is None:
        return {}
    return {o.get("name", ""): o.get("value", "") for o in element.findall("Option")}


def parse_ldf(path: str) -> dict:
    """Return a summary of a project: device, implementations, sources and strategies."""
    root = _load(path)
    implementations = []
    for impl in root.findall("Implementation"):
        options = _options(impl.find("Options"))
        sources = []
        top = options.get("top") or options.get("def_top") or ""
        for src in impl.findall("Source"):
            sources.append({"name": src.get("name", ""), "type": src.get("type", "")})
            src_opts = src.find("Options")
            if not top and src_opts is not None and src_opts.get("top_module"):
                top = src_opts.get("top_module", "")
        implementations.append(
            {
                "title": impl.get("title", ""),
                "dir": impl.get("dir", ""),
                "synthesis": impl.get("synthesis", ""),
                "strategy": impl.get("default_strategy", ""),
                "top": top,
                "sources": sources,
            }
        )
    return {
        "title": root.get("title", ""),
        "device": root.get("device", ""),
        "default_implementation": root.get("default_implementation", ""),
        "implementations": implementations,
        "strategies": [
            {"name": s.get("name", ""), "file": s.get("file", "")} for s in root.findall("Strategy")
        ],
    }


def _resolve_exact_or_case_insensitive(base: Path, relative: str) -> tuple[Optional[Path], bool]:
    """
    Resolve `relative` below `base`. Returns (path, exact) where exact is False
    when the match required ignoring letter case; path is None when not found.
    """
    parts = [p for p in relative.replace("\\", "/").split("/") if p not in ("", ".")]
    current = base
    exact = True
    for part in parts:
        if part == "..":
            current = current.parent
            continue
        candidate = current / part
        if candidate.exists():
            current = candidate
            continue
        try:
            siblings = list(current.iterdir())
        except OSError:
            return None, False
        match = next((s for s in siblings if s.name.lower() == part.lower()), None)
        if match is None:
            return None, False
        current = match
        exact = False
    return current, exact


def find_missing_sources(path: str) -> list[dict]:
    """
    Check that every source and strategy file referenced by the project exists.

    Each returned entry has: file, kind, blocking, and (when the file exists
    under a different letter case) a hint naming the real path. `blocking` is
    False for items that are not flow inputs, such as Simulation Project Files.
    """
    summary = parse_ldf(path)
    ldf_dir = Path(path).resolve().parent
    problems: list[dict] = []
    seen: set[str] = set()

    def check(name: str, kind: str, blocking: bool) -> None:
        if not name or name in seen:
            return
        seen.add(name)
        target = Path(name)
        if target.is_absolute():
            resolved, exact = (target, True) if target.exists() else (None, False)
        else:
            resolved, exact = _resolve_exact_or_case_insensitive(ldf_dir, name)
        if resolved is not None and exact:
            return
        entry = {"file": name, "kind": kind, "blocking": blocking}
        if resolved is not None:
            entry["hint"] = (
                f"File exists with different letter case: {resolved}. "
                "Linux paths are case sensitive; rename the file or fix the project reference."
            )
        problems.append(entry)

    for src in _load(path).iter("Source"):
        type_short = src.get("type_short", "")
        blocking = type_short not in _NON_FLOW_TYPE_SHORT and not src.get("type", "").startswith(
            "Simulation"
        )
        check(src.get("name", ""), src.get("type", "source") or "source", blocking)
    for strategy in summary["strategies"]:
        check(strategy["file"], "Strategy", True)
    return problems


def report_base(path: str, implementation: Optional[str] = None) -> dict:
    """
    Locate the report directory and file prefix for an implementation.

    Reports are named <project title>_<implementation title>.<ext> inside the
    implementation directory, for example impl_1/phase_shift_impl_1.par.
    """
    summary = parse_ldf(path)
    wanted = implementation or summary["default_implementation"]
    for impl in summary["implementations"]:
        if impl["title"] == wanted:
            impl_dir = Path(path).resolve().parent / (impl["dir"] or impl["title"])
            return {
                "implementation": impl["title"],
                "directory": str(impl_dir),
                "prefix": f"{summary['title']}_{impl['title']}",
            }
    available = ", ".join(i["title"] for i in summary["implementations"]) or "(none)"
    raise ProjectFileError(f"Implementation '{wanted}' not found. Available: {available}")


def find_report_files(path: str, implementation: Optional[str] = None) -> dict:
    """Map report extension to absolute path for the reports that exist."""
    base = report_base(path, implementation)
    directory = Path(base["directory"])
    found = {}
    for ext in ("srr", "srf", "mrp", "par", "twr", "pad", "log", "bit", "jed"):
        candidate = directory / f"{base['prefix']}.{ext}"
        if candidate.is_file():
            found[ext] = str(candidate)
    return found


def newest_artifacts(directory: str, suffixes: tuple[str, ...], since: float) -> list[str]:
    """Files below `directory` with one of `suffixes`, modified at or after `since` (epoch seconds)."""
    found = []
    for root, _dirs, files in os.walk(directory):
        for name in files:
            if name.lower().endswith(suffixes):
                full = os.path.join(root, name)
                try:
                    if os.path.getmtime(full) >= since:
                        found.append(full)
                except OSError:
                    continue
    return sorted(found)
