# Copyright(c) 2026 Lattice Semiconductor Corporation. All rights reserved. DO NOT DISTRIBUTE.
# Subject to the terms of the AI Solutions Software License Agreement
"""
Diamond MCP Server - Tool Definitions

Exposes Lattice Diamond FPGA design operations as MCP tools via FastMCP. All
tools communicate through one persistent DiamondSession (a Diamond Tcl console
process). Tools are organised into these groups:

    Session management:  start_diamond, stop_diamond, get_session_status,
                         detect_diamond_installations, get_diamond_config,
                         configure_diamond, check_environment
    Project:             open_project, close_project, create_project,
                         save_project, get_project_info, add_sources,
                         remove_sources, set_target_device, set_synthesis_tool,
                         get_strategy_option, set_strategy_option
    Flow:                compile_project, run_flow, run_synthesis, run_translate,
                         run_map, run_par, run_timing_analysis, run_export,
                         generate_bitstream
    Reports:             list_reports, get_timing_summary, get_resource_usage,
                         read_report
    Escape hatch:        run_tcl

All tools return a JSON-serialisable dict:
    { "success": bool, "output": str, "warnings": list[str], "elapsed_ms": float,
      "timestamp": str, ... }
Failed flow tools also include "hints": suggestions for known failure patterns.

Tcl commands (prj_project, prj_run, prj_dev, prj_syn, prj_src, prj_strgy)
verified against Lattice Diamond 3.14.0.75.0 and 3.10.0.82.0.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import logging
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from . import project as prj
from . import reports
from .config import (
    get_config_info,
    resolve_diamond_path,
    resolve_lm_license_file,
    save_config,
)
from .diamond_session import CommandResult, DiamondSession, SessionError, make_session, tcl_quote
from .discovery import discover_diamond_installations, installations_to_dicts

logger = logging.getLogger(__name__)

_MAX_OUTPUT_CHARS = 20_000
_MAX_REPORT_CHARS = 20_000
_READABLE_REPORT_SUFFIXES = (
    ".par", ".twr", ".mrp", ".srr", ".srf", ".log", ".rpt", ".pad", ".drc", ".txt", ".htm", ".html",
    ".prf", ".lpf", ".sdc", ".tcl", ".ldf",
)
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SYNTHESIS_TOOLS = ("lse", "synplify", "precision")

# Flow stages run in order by compile_project.
_COMPILE_STAGES = ("Synthesis", "Translate", "Map", "PAR")


# =============================================================================
# SESSION HOLDER
# =============================================================================

_SESSION: Optional[DiamondSession] = None


def _get_session() -> DiamondSession:
    """Return the process-wide session, creating it (not starting it) on first use."""
    global _SESSION
    if _SESSION is None:
        _SESSION = make_session()
    return _SESSION


def _set_session(session: Optional[DiamondSession]) -> None:
    """Replace the process-wide session (used by tests)."""
    global _SESSION
    _SESSION = session


def _ensure_started() -> tuple[DiamondSession, Optional[str]]:
    """
    Return a running session, starting or restarting it if needed.

    The second value is a note for the caller when the previous console had died
    (so project state was lost), or None.
    """
    session = _get_session()
    if session.is_running:
        return session, None
    previous = session.last_exit
    session.start()
    if previous:
        return session, (
            f"The previous Diamond console ended ({previous}); a new session was started. "
            "Any open project was closed - reopen it with open_project."
        )
    return session, None


def _project_is_loaded(session: DiamondSession) -> bool:
    """Ask Diamond whether a project is loaded (prj_dev fails with 'Project is not loaded.')."""
    return session.run_tcl("prj_dev").success


def _ensure_no_project() -> tuple[DiamondSession, Optional[str]]:
    """
    Return a running session with no project loaded.

    A loaded project is discarded by restarting the console rather than with
    `prj_project close`, which intermittently crashes Diamond (see
    diamond_session). Returns the session and a restart note, if any.
    """
    session, note = _ensure_started()
    if _project_is_loaded(session):
        session.restart()
        note = (note + " " if note else "") + "The previously open project was closed by restarting the Diamond console."
    return session, note


@asynccontextmanager
async def _lifespan(_server):
    try:
        yield {}
    finally:
        if _SESSION is not None:
            await asyncio.to_thread(_SESSION.stop)


mcp = FastMCP(
    "lattice-diamond-mcp",
    log_level="WARNING",
    lifespan=_lifespan,
    instructions=(
        "Lattice Diamond FPGA flow automation. Typical sequence: check_environment, "
        "open_project (or create_project), compile_project or the individual run_* tools, "
        "then get_timing_summary and get_resource_usage. The Diamond console starts "
        "automatically on first use. After a failed flow stage read 'output', 'warnings' "
        "and 'hints' in the response before retrying."
    ),
)


def _tool(fn):
    """
    Register fn as an MCP tool that runs in a worker thread.

    Flow stages can run for minutes; running them on the event loop would block
    the whole server. The plain function is returned so tests can call it directly.
    """

    @functools.wraps(fn)
    async def _async_tool(*args, **kwargs):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except Exception as exc:  # last-resort guard so a tool never raises into MCP
            logger.exception("Tool %s failed", fn.__name__)
            return _error_dict(f"{fn.__name__} failed: {exc}")

    mcp.tool()(_async_tool)
    return fn


# =============================================================================
# HELPERS
# =============================================================================

def _ts() -> dict:
    return {"elapsed_ms": 0.0, "timestamp": datetime.now().isoformat()}


def _error_dict(message: str, **extra) -> dict:
    return {"success": False, "output": message, "warnings": [], **_ts(), **extra}


def _ok_dict(message: str, **extra) -> dict:
    return {"success": True, "output": message, "warnings": [], **_ts(), **extra}


def _truncate(text: str, limit: int = _MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    omitted = len(text) - limit
    return f"{text[:head]}\n... [{omitted} characters omitted] ...\n{text[-tail:]}"


_HINT_RULES = (
    (
        "Project is not loaded",
        "No project is open in the Diamond session. Call open_project first.",
    ),
    (
        "software has expired",
        "A tool in this Diamond installation has expired (typically the bundled Synplify). "
        "Use a newer installation (detect_diamond_installations, configure_diamond), or switch "
        "the project to the Lattice Synthesis Engine with set_synthesis_tool('lse').",
    ),
    (
        "Error in reading project file",
        "Diamond could not load the project. Common causes: a device or package that this Diamond "
        "version does not support (compare get_project_info 'device' with get_session_status "
        "'diamond_version'), or a project file written by a newer release.",
    ),
    (
        "is invalid device or not supported",
        "The device name is not valid for this Diamond version. Use a full part name such as "
        "LCMXO3LF-6900C-5BG256C and check the version with get_session_status.",
    ),
    (
        "Cannot find",
        "A source file named in the project does not exist. Linux paths are case sensitive; "
        "open_project reports mismatched letter case under 'preflight'.",
    ),
    (
        "not a valid milestone name",
        "Unknown flow milestone. Valid milestones include Synthesis, Translate, Map, PAR and Export.",
    ),
    (
        "not a valid task name",
        "Unknown task for that milestone, for example PAR tasks PARTrace and IOTiming, "
        "Export tasks Bitgen and TimingSimFileVlg.",
    ),
    (
        "Current project is not closed",
        "A project is already open. Use open_project, which closes it first, or close_project.",
    ),
)


def _hints_for(text: str) -> list[str]:
    return [hint for needle, hint in _HINT_RULES if needle in text]


def _result_to_dict(result: CommandResult, note: Optional[str] = None, **extra) -> dict:
    """Convert a CommandResult to a tool response dict."""
    pieces = [p for p in (result.output, result.result) if p]
    text = "\n".join(pieces)
    payload = {
        "success": result.success,
        "output": _truncate(text),
        "warnings": result.warnings,
        "elapsed_ms": round(result.elapsed_ms, 1),
        "timestamp": result.timestamp,
        **extra,
    }
    if result.result and result.output:
        payload["tcl_result"] = _truncate(result.result, 2000)
    if not result.success:
        hints = _hints_for(text)
        if hints:
            payload["hints"] = hints
    if note:
        payload["session_note"] = note
    return payload


def _run(command: str, timeout: Optional[float] = None, **extra) -> dict:
    """Run Tcl in the (auto-started) session and convert the result."""
    try:
        session, note = _ensure_started()
        result = session.run_tcl(command, timeout=timeout)
    except SessionError as exc:
        return _error_dict(str(exc))
    return _result_to_dict(result, note=note, **extra)


def _impl_arg(implementation: Optional[str]) -> str:
    return f" -impl {tcl_quote(implementation)}" if implementation else ""


def _abs_path(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path)).replace("\\", "/")


def _current_project_path(project_path: Optional[str]) -> Optional[str]:
    if project_path:
        return _abs_path(project_path)
    session = _get_session()
    return session.current_project


def _force_flag(force: str) -> str:
    return {"none": "", "": "", "one": " -forceOne", "all": " -forceAll"}.get(force.lower(), None)


# =============================================================================
# SESSION MANAGEMENT TOOLS
# =============================================================================

@_tool
def start_diamond(exe_path: Optional[str] = None, timeout: Optional[float] = None) -> dict:
    """
    Start the Diamond Tcl console. Idempotent - a no-op when already running. Tools start it automatically, so this is only needed to pick a specific installation or timeout.

    Args:
        exe_path: Override path to the Diamond console (diamondc on Linux, pnmainc.exe on Windows); defaults to DIAMOND_EXE, the config file, then auto-detection.
        timeout:  Default command timeout in seconds (default 3600).
    """
    session = _get_session()
    if exe_path and not session.is_running:
        session.diamond_exe = exe_path
    if timeout:
        session.timeout = float(timeout)
    try:
        result = session.start()
    except SessionError as exc:
        return _error_dict(str(exc))
    return _result_to_dict(result, diamond_version=session.diamond_version)


@_tool
def stop_diamond() -> dict:
    """Stop the Diamond Tcl console gracefully (force-kill after 10 seconds). Open projects are closed without saving."""
    return _result_to_dict(_get_session().stop())


@_tool
def get_session_status(log_lines: int = 50) -> dict:
    """
    Return session health: is_running, diamond_version, current_project, commands_run, errors, last_exit (why a previous console died) and the console log tail.

    Args:
        log_lines: Console log lines to include (default 50, 0 to omit).
    """
    stats = _get_session().get_stats(log_lines=max(0, log_lines))
    return _ok_dict("Session status.", **stats)


@_tool
def detect_diamond_installations() -> dict:
    """List Diamond installations found on this machine, newest first. Use the diamond_exe of an entry with configure_diamond."""
    installs = installations_to_dicts(discover_diamond_installations())
    message = f"Found {len(installs)} Diamond installation(s)." if installs else "No Diamond installation found."
    return _ok_dict(message, installations=installs)


@_tool
def get_diamond_config() -> dict:
    """Show the resolved Diamond executable, where it came from, whether a license is configured, and the config file search order."""
    return _ok_dict("Diamond configuration.", **get_config_info())


@_tool
def configure_diamond(
    diamond_exe: Optional[str] = None,
    lm_license_file: Optional[str] = None,
    timeout_seconds: Optional[float] = None,
) -> dict:
    """
    Save Diamond settings to the config file. Takes effect the next time the console starts (call stop_diamond first if it is running).

    Args:
        diamond_exe:     Full path to the Diamond console executable.
        lm_license_file: License value (port@host or a license file path) passed as LM_LICENSE_FILE.
        timeout_seconds: Default command timeout in seconds.
    """
    if diamond_exe is None and lm_license_file is None and timeout_seconds is None:
        return _error_dict("Provide at least one of diamond_exe, lm_license_file or timeout_seconds.")
    if diamond_exe is not None and not Path(diamond_exe).is_file():
        return _error_dict(f"diamond_exe does not exist: {diamond_exe}")
    target = save_config(diamond_exe, lm_license_file, timeout_seconds)
    note = " Stop and restart the session to apply." if _get_session().is_running else ""
    return _ok_dict(f"Saved configuration to {target}.{note}", config_file=str(target))


@_tool
def check_environment() -> dict:
    """Check that Diamond can be started: executable resolved and present, license configured, version. Run this first when something fails to start."""
    resolved = resolve_diamond_path()
    checks = []
    if resolved.path is None:
        checks.append({
            "name": "diamond_exe", "status": "missing",
            "detail": "No Diamond executable configured or detected. Use detect_diamond_installations "
                      "then configure_diamond, or set DIAMOND_EXE.",
        })
    elif not resolved.exists:
        checks.append({
            "name": "diamond_exe", "status": "error",
            "detail": f"{resolved.path} (from {resolved.source}) does not exist.",
        })
    elif not os.access(resolved.path, os.X_OK) and os.name != "nt":
        checks.append({
            "name": "diamond_exe", "status": "error",
            "detail": f"{resolved.path} is not executable.",
        })
    else:
        checks.append({
            "name": "diamond_exe", "status": "ok",
            "detail": f"{resolved.path} (from {resolved.source})",
        })
    if resolve_lm_license_file():
        checks.append({"name": "license", "status": "ok", "detail": "LM_LICENSE_FILE is configured."})
    else:
        checks.append({
            "name": "license", "status": "warning",
            "detail": "No LM_LICENSE_FILE in the environment or config file. Diamond may still find a "
                      "license through its own install (license.dat); if flows fail with license errors, "
                      "run configure_diamond(lm_license_file=...).",
        })
    blocking = [c for c in checks if c["status"] in ("missing", "error")]
    ready = not blocking
    summary = "Environment is ready." if ready else f"Environment is NOT ready - {len(blocking)} issue(s)."
    return _ok_dict(summary, ready=ready, checks=checks)


# =============================================================================
# PROJECT TOOLS
# =============================================================================

@_tool
def open_project(project_path: str, implementation: Optional[str] = None) -> dict:
    """
    Open a Diamond project (.ldf). Any project that is already open is closed first (unsaved changes are lost). Checks the project file first and reports missing source files (including letter-case mismatches) under 'preflight'.

    Args:
        project_path:   Path to the .ldf file.
        implementation: Implementation to make active (default: the project's default).
    """
    path = _abs_path(project_path)
    try:
        info = prj.parse_ldf(path)
        missing = prj.find_missing_sources(path)
    except prj.ProjectFileError as exc:
        return _error_dict(str(exc))

    try:
        _session, note = _ensure_no_project()
    except SessionError as exc:
        return _error_dict(str(exc))
    lines = [f"cd {tcl_quote(str(Path(path).parent))}", f"prj_project open {tcl_quote(path)}"]
    if implementation:
        lines.append(f"prj_impl active {tcl_quote(implementation)}")
    response = _run("\n".join(lines))
    if note:
        response["session_note"] = note

    preflight = {"device": info["device"], "missing_files": missing}
    if response["success"]:
        _get_session().current_project = path
        response["preflight"] = preflight
        if any(m["blocking"] for m in missing):
            response["warnings"] = response["warnings"] + [
                "Some source files named in the project do not exist; the flow will fail. See preflight.missing_files."
            ]
    else:
        response["preflight"] = preflight
        response.setdefault("hints", []).append(
            f"Project device is {info['device']}; Diamond version is "
            f"{_get_session().diamond_version or 'unknown'}."
        )
    return response


@_tool
def close_project() -> dict:
    """Close the currently open project WITHOUT saving (call save_project first to keep changes). Implemented by restarting the Diamond console, because Diamond's own close command intermittently crashes it."""
    session = _get_session()
    if not session.is_running:
        session.current_project = None
        return _ok_dict("No project is open (the Diamond session is not running).")
    if not _project_is_loaded(session):
        session.current_project = None
        return _ok_dict("No project is open.")
    result = session.restart()
    return _ok_dict(
        "Project closed (the Diamond console was restarted).",
        elapsed_ms=round(result.elapsed_ms, 1),
    )


@_tool
def create_project(
    name: str,
    directory: str,
    device: str,
    implementation: str = "impl1",
    synthesis_tool: str = "lse",
    lpf: Optional[str] = None,
) -> dict:
    """
    Create a new project and leave it open. Add sources afterwards with add_sources.

    Args:
        name:           Project name; the file is <directory>/<name>.ldf.
        directory:      Existing directory to create the project in.
        device:         Full part name, for example LCMXO3LF-6900C-5BG256C.
        implementation: Name of the first implementation.
        synthesis_tool: lse, synplify or precision.
        lpf:            Optional logic preference (.lpf) file.
    """
    if synthesis_tool not in _SYNTHESIS_TOOLS:
        return _error_dict(f"synthesis_tool must be one of {', '.join(_SYNTHESIS_TOOLS)}.")
    if not _NAME_RE.match(name):
        return _error_dict("name must start with a letter or underscore and contain only letters, digits and underscores.")
    target_dir = Path(_abs_path(directory))
    if not target_dir.is_dir():
        return _error_dict(f"Directory does not exist: {target_dir}")
    try:
        _session, note = _ensure_no_project()
    except SessionError as exc:
        return _error_dict(str(exc))
    command = (
        f"cd {tcl_quote(str(target_dir))}\n"
        f"prj_project new -name {tcl_quote(name)} -dev {tcl_quote(device)} "
        f"-impl {tcl_quote(implementation)} -synthesis {tcl_quote(synthesis_tool)}"
        + (f" -lpf {tcl_quote(_abs_path(lpf))}" if lpf else "")
    )
    response = _run(command)
    if response["success"]:
        _get_session().current_project = _abs_path(str(target_dir / f"{name}.ldf"))
    if note:
        response["session_note"] = note
    return response


@_tool
def save_project() -> dict:
    """Save the currently open project."""
    return _run("prj_project save")


@_tool
def get_project_info(project_path: Optional[str] = None) -> dict:
    """
    Describe a project from its .ldf file (no Diamond session needed): device, implementations with synthesis tool, top module and sources, strategies, which reports exist, and any missing source files.

    Args:
        project_path: Path to the .ldf file; defaults to the currently open project.
    """
    path = _current_project_path(project_path)
    if not path:
        return _error_dict("No project_path given and no project is open.")
    try:
        info = prj.parse_ldf(path)
        missing = prj.find_missing_sources(path)
        files = prj.find_report_files(path)
    except prj.ProjectFileError as exc:
        return _error_dict(str(exc))
    return _ok_dict(
        f"Project {info['title']} ({info['device']}).",
        project=info,
        project_path=path,
        missing_files=missing,
        existing_reports=files,
    )


def _resolve_files(files: list[str]) -> list[str]:
    base = Path(_get_session().current_project).parent if _get_session().current_project else None
    resolved = []
    for name in files:
        candidate = Path(name)
        if not candidate.is_absolute() and base is not None:
            candidate = base / candidate
        resolved.append(str(candidate).replace("\\", "/"))
    return resolved


@_tool
def add_sources(
    files: list[str],
    implementation: Optional[str] = None,
    file_format: Optional[str] = None,
    include_paths: Optional[list[str]] = None,
    library: Optional[str] = None,
    synthesis_only: bool = False,
) -> dict:
    """
    Add source files to the open project. Relative paths are taken relative to the project directory.

    Args:
        files:          Source files to add.
        implementation: Target implementation (default: the active one).
        file_format:    Force a format, for example VERILOG, VHDL, EDIF.
        include_paths:  Verilog include search paths.
        library:        VHDL library name.
        synthesis_only: Use the files for synthesis only, not simulation.
    """
    if not files:
        return _error_dict("files must not be empty.")
    resolved = _resolve_files(files)
    absent = [f for f in resolved if not Path(f).exists()]
    if absent:
        return _error_dict("These files do not exist: " + ", ".join(absent))
    command = "prj_src add" + _impl_arg(implementation)
    if file_format:
        command += f" -format {tcl_quote(file_format)}"
    if include_paths:
        command += " -include " + tcl_quote(" ".join(include_paths))
    if library:
        command += f" -work {tcl_quote(library)}"
    if synthesis_only:
        command += " -synthesis_only"
    command += " " + " ".join(tcl_quote(f) for f in resolved)
    return _run(command)


@_tool
def remove_sources(
    files: Optional[list[str]] = None,
    implementation: Optional[str] = None,
    remove_all: bool = False,
) -> dict:
    """
    Remove sources from the open project.

    Args:
        files:          Files to remove (as named in the project).
        implementation: Target implementation (default: the active one).
        remove_all:     Remove every source instead of listing files.
    """
    if remove_all == bool(files):
        return _error_dict("Provide either files or remove_all=True, not both or neither.")
    command = "prj_src remove" + _impl_arg(implementation)
    command += " -all" if remove_all else " " + " ".join(tcl_quote(f) for f in files or [])
    return _run(command)


@_tool
def set_target_device(part: str) -> dict:
    """
    Change the target device of the open project and return the resulting device information.

    Args:
        part: Full part name, for example LCMXO3LF-6900C-5BG256C.
    """
    return _run(f"prj_dev set -part {tcl_quote(part)}\nprj_dev")


@_tool
def set_synthesis_tool(tool: str, implementation: Optional[str] = None) -> dict:
    """
    Set the synthesis tool for an implementation of the open project.

    Args:
        tool:           lse, synplify or precision.
        implementation: Target implementation (default: the active one).
    """
    if tool not in _SYNTHESIS_TOOLS:
        return _error_dict(f"tool must be one of {', '.join(_SYNTHESIS_TOOLS)}.")
    return _run(f"prj_syn set {tool}{_impl_arg(implementation)}\nprj_syn")


@_tool
def get_strategy_option(name: str, strategy: Optional[str] = None) -> dict:
    """
    Read one strategy option, for example PROP_MAP_RegRetiming.

    Args:
        name:     Strategy option name (PROP_...).
        strategy: Strategy name (default: the active implementation's strategy).
    """
    option = "prj_strgy get_value"
    if strategy:
        option += f" -strategy {tcl_quote(strategy)}"
    return _run(f"{option} {tcl_quote(name)}")


@_tool
def set_strategy_option(name: str, value: str, strategy: Optional[str] = None) -> dict:
    """
    Set one strategy option and return its new value.

    Args:
        name:     Strategy option name (PROP_...).
        value:    New value, for example True, Off, 5.
        strategy: Strategy name (default: the active implementation's strategy).
    """
    if "=" in name or not name.strip():
        return _error_dict("name must be a non-empty option name without '='.")
    base = "prj_strgy"
    flag = f" -strategy {tcl_quote(strategy)}" if strategy else ""
    return _run(
        f"{base} set_value{flag} {tcl_quote(name + '=' + value)}\n"
        f"{base} get_value{flag} {tcl_quote(name)}"
    )


# =============================================================================
# FLOW TOOLS
# =============================================================================

def _flow_command(milestone: str, task: Optional[str], implementation: Optional[str], force: str) -> Optional[str]:
    flag = _force_flag(force)
    if flag is None:
        return None
    command = f"prj_run {milestone}{_impl_arg(implementation)}"
    if task:
        command += f" -task {task}"
    return command + flag


def _run_flow_impl(
    milestone: str,
    task: Optional[str],
    implementation: Optional[str],
    force: str,
    timeout: Optional[float],
) -> dict:
    if not _NAME_RE.match(milestone) or (task and not _NAME_RE.match(task)):
        return _error_dict("milestone and task must be plain names such as Synthesis or PARTrace.")
    command = _flow_command(milestone, task, implementation, force)
    if command is None:
        return _error_dict("force must be one of: none, one, all.")
    return _run(command, timeout=timeout)


@_tool
def run_flow(
    milestone: str,
    task: Optional[str] = None,
    implementation: Optional[str] = None,
    force: str = "none",
    timeout: Optional[float] = None,
) -> dict:
    """
    Run a flow milestone or task on the open project: prj_run <milestone> [-task <task>]. Upstream stages run automatically when out of date.

    Args:
        milestone:      Synthesis, Translate, Map, PAR or Export.
        task:           Optional task within the milestone, for example PARTrace, IOTiming, Bitgen, TimingSimFileVlg.
        implementation: Implementation to run (default: the active one).
        force:          none, one (re-run this stage) or all (re-run all stages).
        timeout:        Seconds to allow (default: the session timeout). On timeout the session is restarted and the project must be reopened.
    """
    return _run_flow_impl(milestone, task, implementation, force, timeout)


@_tool
def run_synthesis(implementation: Optional[str] = None, force: str = "none") -> dict:
    """
    Run the Synthesis stage (Synplify, LSE or Precision, per the project) on the open project.

    Args:
        implementation: Implementation to run (default: the active one).
        force:          none, one or all.
    """
    return _run_flow_impl("Synthesis", None, implementation, force, None)


@_tool
def run_translate(implementation: Optional[str] = None, force: str = "none") -> dict:
    """
    Run the Translate stage (netlist to NGD) on the open project.

    Args:
        implementation: Implementation to run (default: the active one).
        force:          none, one or all.
    """
    return _run_flow_impl("Translate", None, implementation, force, None)


@_tool
def run_map(implementation: Optional[str] = None, force: str = "none") -> dict:
    """
    Run the Map stage on the open project.

    Args:
        implementation: Implementation to run (default: the active one).
        force:          none, one or all.
    """
    return _run_flow_impl("Map", None, implementation, force, None)


@_tool
def run_par(implementation: Optional[str] = None, force: str = "none") -> dict:
    """
    Run Place and Route on the open project. Produces the .par and .twr reports.

    Args:
        implementation: Implementation to run (default: the active one).
        force:          none, one or all.
    """
    return _run_flow_impl("PAR", None, implementation, force, None)


@_tool
def run_timing_analysis(implementation: Optional[str] = None, force: str = "none") -> dict:
    """
    Run Place and Route Trace (timing analysis) and return the parsed timing summary.

    Args:
        implementation: Implementation to run (default: the active one).
        force:          none, one or all.
    """
    response = _run_flow_impl("PAR", "PARTrace", implementation, force, None)
    if response["success"]:
        response["timing"] = _timing_for_current(implementation)
    return response


@_tool
def run_export(implementation: Optional[str] = None, force: str = "none") -> dict:
    """
    Run the Export milestone. With default strategy settings this does NOT write a bitstream or JEDEC file; use generate_bitstream for that.

    Args:
        implementation: Implementation to run (default: the active one).
        force:          none, one or all.
    """
    return _run_flow_impl("Export", None, implementation, force, None)


@_tool
def generate_bitstream(
    implementation: Optional[str] = None, force: str = "none", timeout: Optional[float] = None
) -> dict:
    """
    Generate the programming bitstream (.bit) with prj_run Export -task Bitgen, and verify that a bitstream file was actually written.

    Args:
        implementation: Implementation to run (default: the active one).
        force:          none, one or all.
        timeout:        Seconds to allow (default: the session timeout).
    """
    started = time.time() - 1.0
    response = _run_flow_impl("Export", "Bitgen", implementation, force, timeout)
    if not response["success"]:
        return response
    path = _get_session().current_project
    if not path:
        return response
    try:
        directory = prj.report_base(path, implementation)["directory"]
    except prj.ProjectFileError:
        return response
    files = prj.newest_artifacts(directory, (".bit",), started)
    if files:
        response["bitstream_files"] = files
    else:
        response["success"] = False
        response["warnings"] = response["warnings"] + [
            "Bitgen returned success but no .bit file was written. A device may not support bitstream "
            "output, or the file is up to date from an earlier run (retry with force='one')."
        ]
    return response


def _timing_for_current(implementation: Optional[str]) -> dict:
    path = _get_session().current_project
    if not path:
        return {}
    return _timing_summary(path, implementation)


def _read_text(file: str) -> str:
    return Path(file).read_text(encoding="utf-8", errors="replace")


def _timing_summary(path: str, implementation: Optional[str]) -> dict:
    files = prj.find_report_files(path, implementation)
    summary: dict = {}
    if "par" in files:
        summary["par"] = reports.parse_par(_read_text(files["par"]))
    if "twr" in files:
        summary["twr"] = reports.parse_twr(_read_text(files["twr"]))
    return summary


@_tool
def compile_project(
    project_path: str,
    implementation: Optional[str] = None,
    force: bool = False,
    bitstream: bool = True,
    timeout: Optional[float] = None,
) -> dict:
    """
    Compile a project end to end: open, Synthesis, Translate, Map, PAR and optionally Bitgen, stopping at the first failing stage. Returns per-stage status and, on success, the timing and resource summaries. The project stays open afterwards.

    Args:
        project_path: Path to the .ldf file.
        implementation: Implementation to run (default: the project's default).
        force:        Re-run every stage even if up to date (-forceOne).
        bitstream:    Also generate the bitstream after PAR.
        timeout:      Seconds allowed per stage (default: the session timeout).
    """
    path = _abs_path(project_path)
    try:
        blocking = [m for m in prj.find_missing_sources(path) if m["blocking"]]
    except prj.ProjectFileError as exc:
        return _error_dict(str(exc))
    if blocking:
        return _error_dict(
            "Cannot compile: source files named in the project do not exist.",
            missing_files=blocking,
        )

    opened = open_project(path, implementation)
    stages = [{"stage": "open", "success": opened["success"], "elapsed_ms": opened["elapsed_ms"]}]
    if not opened["success"]:
        opened["stages"] = stages
        return opened

    force_mode = "one" if force else "none"
    last = opened
    for milestone in _COMPILE_STAGES:
        last = _run_flow_impl(milestone, None, implementation, force_mode, timeout)
        stages.append({"stage": milestone, "success": last["success"], "elapsed_ms": last["elapsed_ms"]})
        if not last["success"]:
            last["stages"] = stages
            last["failed_stage"] = milestone
            return last
    if bitstream:
        last = generate_bitstream(implementation, force_mode, timeout)
        stages.append({"stage": "Bitgen", "success": last["success"], "elapsed_ms": last["elapsed_ms"]})
        if not last["success"]:
            last["stages"] = stages
            last["failed_stage"] = "Bitgen"
            return last

    total_ms = sum(s["elapsed_ms"] for s in stages)
    summary = _ok_dict("Compile completed.", stages=stages)
    summary["elapsed_ms"] = round(total_ms, 1)
    summary["timing"] = _timing_summary(path, implementation)
    summary["resources"] = _resources(path, implementation)
    if last.get("bitstream_files"):
        summary["bitstream_files"] = last["bitstream_files"]
    return summary


# =============================================================================
# REPORT TOOLS
# =============================================================================

def _resources(path: str, implementation: Optional[str]) -> dict:
    files = prj.find_report_files(path, implementation)
    if "mrp" not in files:
        return {}
    return reports.parse_mrp(_read_text(files["mrp"]))


@_tool
def list_reports(project_path: Optional[str] = None, implementation: Optional[str] = None) -> dict:
    """
    List the report and output files that exist for an implementation (srr, mrp, par, twr, pad, bit, ...).

    Args:
        project_path:   Path to the .ldf file; defaults to the currently open project.
        implementation: Implementation name (default: the project's default).
    """
    path = _current_project_path(project_path)
    if not path:
        return _error_dict("No project_path given and no project is open.")
    try:
        files = prj.find_report_files(path, implementation)
    except prj.ProjectFileError as exc:
        return _error_dict(str(exc))
    return _ok_dict(f"{len(files)} report file(s) found.", reports=files)


@_tool
def get_timing_summary(project_path: Optional[str] = None, implementation: Optional[str] = None) -> dict:
    """
    Summarise timing from the PAR and timing reports: setup/hold errors and scores, worst slack, constraint versus achieved frequency, and whether all preferences were met.

    Args:
        project_path:   Path to the .ldf file; defaults to the currently open project.
        implementation: Implementation name (default: the project's default).
    """
    path = _current_project_path(project_path)
    if not path:
        return _error_dict("No project_path given and no project is open.")
    try:
        summary = _timing_summary(path, implementation)
    except prj.ProjectFileError as exc:
        return _error_dict(str(exc))
    if not summary:
        return _error_dict("No PAR or timing report found. Run run_par first.")
    return _ok_dict("Timing summary.", timing=summary)


@_tool
def get_resource_usage(project_path: Optional[str] = None, implementation: Optional[str] = None) -> dict:
    """
    Summarise device utilisation from the map report: registers, SLICEs, LUT4s, I/O, block RAM and so on, each with used, total and percent.

    Args:
        project_path:   Path to the .ldf file; defaults to the currently open project.
        implementation: Implementation name (default: the project's default).
    """
    path = _current_project_path(project_path)
    if not path:
        return _error_dict("No project_path given and no project is open.")
    try:
        resources = _resources(path, implementation)
    except prj.ProjectFileError as exc:
        return _error_dict(str(exc))
    if not resources:
        return _error_dict("No map report found. Run run_map first.")
    return _ok_dict("Resource usage.", **resources)


@_tool
def read_report(file_path: str, offset: int = 0, max_chars: int = _MAX_REPORT_CHARS, tail: bool = False) -> dict:
    """
    Read a text report or log file (.par, .twr, .mrp, .srr, .srf, .log, .rpt, .pad, .htm, ...). Use list_reports to find files.

    Args:
        file_path: Path to the file.
        offset:    Character offset to start from (ignored when tail is true).
        max_chars: Maximum characters to return (default and cap: 20000).
        tail:      Return the last max_chars characters instead.
    """
    path = Path(_abs_path(file_path))
    if path.suffix.lower() not in _READABLE_REPORT_SUFFIXES:
        return _error_dict(
            "Only report and log files can be read (" + ", ".join(_READABLE_REPORT_SUFFIXES) + ")."
        )
    if not path.is_file():
        return _error_dict(f"File does not exist: {path}")
    limit = max(1, min(int(max_chars), _MAX_REPORT_CHARS))
    text = path.read_text(encoding="utf-8", errors="replace")
    start = max(0, len(text) - limit) if tail else max(0, int(offset))
    chunk = text[start:start + limit]
    return _ok_dict(
        chunk,
        total_chars=len(text),
        offset=start,
        returned_chars=len(chunk),
        has_more=start + len(chunk) < len(text),
    )


# =============================================================================
# ESCAPE HATCH
# =============================================================================

@_tool
def run_tcl(command: str, timeout: Optional[float] = None) -> dict:
    """
    Run arbitrary Tcl in the live Diamond console (any prj_* command, or plain Tcl). Multi-line scripts are supported. Prefer the dedicated tools; use this for commands they do not cover.

    Args:
        command: Tcl to run.
        timeout: Seconds to allow (default: the session timeout).
    """
    if not command.strip():
        return _error_dict("command must not be empty.")
    return _run(command, timeout=timeout)


# =============================================================================
# ENTRY POINT
# =============================================================================

def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(prog="diamond-mcp", description="MCP server for Lattice Diamond")
    parser.add_argument("--diamond-exe", help="Path to the Diamond console executable (overrides config).")
    parser.add_argument("--timeout", type=float, help="Default command timeout in seconds.")
    parser.add_argument(
        "--transport", choices=("stdio", "sse", "streamable-http"), default="stdio",
        help="MCP transport (default: stdio).",
    )
    parser.add_argument("--log-level", default="WARNING", help="Python logging level (default: WARNING).")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(), stream=sys.stderr)
    if args.diamond_exe:
        os.environ["DIAMOND_EXE"] = args.diamond_exe
    if args.timeout:
        os.environ["DIAMOND_MCP_TIMEOUT"] = str(args.timeout)
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
