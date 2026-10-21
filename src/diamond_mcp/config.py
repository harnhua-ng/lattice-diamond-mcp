# Copyright(c) 2026 Lattice Semiconductor Corporation. All rights reserved. DO NOT DISTRIBUTE.
# Subject to the terms of the AI Solutions Software License Agreement
"""
Diamond installation, license and timeout configuration.

Resolution priority for the Diamond console executable (first match wins):
    1. DIAMOND_EXE environment variable
    2. diamond_exe / diamond_install_dir in the config file
    3. The newest installation found by discovery (source "auto-detected")

Resolution priority for the license (LM_LICENSE_FILE):
    1. LM_LICENSE_FILE environment variable
    2. lm_license_file in the config file

Resolution priority for the default command timeout in seconds:
    1. DIAMOND_MCP_TIMEOUT environment variable
    2. timeout_seconds in the config file
    3. DEFAULT_TIMEOUT_SECONDS

Audit log location (see audit.py), first match wins:
    1. DIAMOND_MCP_AUDIT_LOG environment variable
    2. audit_log in the config file
    3. <state dir>/diamond-mcp/audit.log, where <state dir> is $XDG_STATE_HOME or
       ~/.local/state (Linux) and %LOCALAPPDATA% (Windows)
    A value of "off", "none", "false", "0" or an empty string disables the log.

Config file search order:
    1. DIAMOND_MCP_CONFIG environment variable (explicit path)
    2. ./diamond-mcp.json in the current working directory
    3. %APPDATA%/diamond-mcp/config.json  (Windows)
       ~/.config/diamond-mcp/config.json   (other platforms)

Config file schema (JSON, every field optional):
    {
        "diamond_exe": "/tools/dist/lattice/DIAMOND/3.14.0.75.0/diamond/sub/bin/lin64/diamondc",
        "lm_license_file": "1234@license-server",
        "timeout_seconds": 3600,
        "audit_log": "/var/log/diamond-mcp/audit.log"
    }

    "diamond_install_dir" may be given instead of "diamond_exe"; the console
    executable is then located inside it.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .discovery import EXE_RELATIVE_PATHS, discover_diamond_installations

CONFIG_FILENAME = "diamond-mcp.json"
USER_CONFIG_DIR = "diamond-mcp"
USER_CONFIG_FILENAME = "config.json"
DEFAULT_TIMEOUT_SECONDS = 3600.0


@dataclass(frozen=True)
class ResolvedDiamondPath:
    """Result of resolving the Diamond console executable."""

    path: Optional[str]
    source: str  # "env" | "config:<file>" | "auto-detected" | "none"

    @property
    def exists(self) -> bool:
        return bool(self.path) and Path(self.path).is_file()


def _user_config_path() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / USER_CONFIG_DIR / USER_CONFIG_FILENAME
    return Path.home() / ".config" / USER_CONFIG_DIR / USER_CONFIG_FILENAME


def config_search_paths() -> list[Path]:
    paths: list[Path] = []
    explicit = os.environ.get("DIAMOND_MCP_CONFIG", "").strip()
    if explicit:
        paths.append(Path(explicit))
    paths.append(Path.cwd() / CONFIG_FILENAME)
    paths.append(_user_config_path())
    return paths


def find_config_file() -> Optional[Path]:
    for path in config_search_paths():
        if path.is_file():
            return path
    return None


def load_config_data(config_file: Optional[Path] = None) -> dict[str, Any]:
    """Return the parsed config file, or {} when there is none or it is unreadable."""
    target = config_file or find_config_file()
    if target is None:
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _exe_from_install_dir(install_dir: str) -> str:
    root = Path(install_dir)
    for rel in EXE_RELATIVE_PATHS:
        candidate = root / rel
        if candidate.is_file():
            return str(candidate)
    return str(root / EXE_RELATIVE_PATHS[0])


def resolve_diamond_path() -> ResolvedDiamondPath:
    env_exe = os.environ.get("DIAMOND_EXE", "").strip()
    if env_exe:
        return ResolvedDiamondPath(env_exe, "env")

    config_file = find_config_file()
    data = load_config_data(config_file)
    if data.get("diamond_exe"):
        return ResolvedDiamondPath(str(data["diamond_exe"]), f"config:{config_file}")
    if data.get("diamond_install_dir"):
        return ResolvedDiamondPath(
            _exe_from_install_dir(str(data["diamond_install_dir"])), f"config:{config_file}"
        )

    installations = discover_diamond_installations()
    if installations:
        return ResolvedDiamondPath(installations[0].exe, "auto-detected")
    return ResolvedDiamondPath(None, "none")


def resolve_lm_license_file() -> Optional[str]:
    env_value = os.environ.get("LM_LICENSE_FILE", "").strip()
    if env_value:
        return env_value
    value = load_config_data().get("lm_license_file")
    return str(value) if value else None


_AUDIT_OFF_VALUES = {"", "off", "none", "false", "0", "disabled"}


@dataclass(frozen=True)
class ResolvedAuditLog:
    """Where the audit log is written; path is None when logging is disabled."""

    path: Optional[str]
    source: str  # "env" | "config:<file>" | "default" | "disabled-by-env" | "disabled-by-config"


def _default_audit_log_path() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / USER_CONFIG_DIR / "audit.log"
    state = os.environ.get("XDG_STATE_HOME", "").strip() or str(Path.home() / ".local" / "state")
    return Path(state) / USER_CONFIG_DIR / "audit.log"


def resolve_audit_log_path() -> ResolvedAuditLog:
    if "DIAMOND_MCP_AUDIT_LOG" in os.environ:
        value = os.environ["DIAMOND_MCP_AUDIT_LOG"].strip()
        if value.lower() in _AUDIT_OFF_VALUES:
            return ResolvedAuditLog(None, "disabled-by-env")
        return ResolvedAuditLog(value, "env")
    config_file = find_config_file()
    data = load_config_data(config_file)
    if "audit_log" in data:
        value = data["audit_log"]
        if value is False or str(value).strip().lower() in _AUDIT_OFF_VALUES:
            return ResolvedAuditLog(None, "disabled-by-config")
        return ResolvedAuditLog(str(value), f"config:{config_file}")
    return ResolvedAuditLog(str(_default_audit_log_path()), "default")


def resolve_timeout() -> float:
    env_value = os.environ.get("DIAMOND_MCP_TIMEOUT", "").strip()
    for candidate in (env_value, load_config_data().get("timeout_seconds")):
        try:
            seconds = float(candidate)
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            return seconds
    return DEFAULT_TIMEOUT_SECONDS


def save_config(
    diamond_exe: Optional[str] = None,
    lm_license_file: Optional[str] = None,
    timeout_seconds: Optional[float] = None,
) -> Path:
    """Merge the given fields into the active config file (created if absent)."""
    target = find_config_file() or _user_config_path()
    data = load_config_data(target) if target.is_file() else {}
    if diamond_exe is not None:
        data["diamond_exe"] = diamond_exe
        data.pop("diamond_install_dir", None)
    if lm_license_file is not None:
        data["lm_license_file"] = lm_license_file
    if timeout_seconds is not None:
        data["timeout_seconds"] = float(timeout_seconds)

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".diamond-mcp-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


def get_config_info() -> dict:
    resolved = resolve_diamond_path()
    license_value = resolve_lm_license_file()
    audit = resolve_audit_log_path()
    config_file = find_config_file()
    return {
        "diamond_exe": resolved.path,
        "diamond_exe_source": resolved.source,
        "diamond_exe_exists": resolved.exists,
        "lm_license_file_set": license_value is not None,
        "timeout_seconds": resolve_timeout(),
        "audit_log": audit.path,
        "audit_log_source": audit.source,
        "config_file": str(config_file) if config_file else None,
        "config_search_paths": [str(p) for p in config_search_paths()],
    }
