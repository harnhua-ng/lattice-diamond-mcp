# Copyright(c) 2026 Lattice Semiconductor Corporation. All rights reserved. DO NOT DISTRIBUTE.
# Subject to the terms of the AI Solutions Software License Agreement
"""
Discover Lattice Diamond installations on the local machine.

Used by the detect_diamond_installations MCP tool so an LLM can locate Diamond
when no config file or environment variable is set.

Recognised layouts below a search root (one directory per release):

    <root>/<release>/bin/lin64/diamondc                    standard Linux install
    <root>/<release>/diamond/sub/bin/lin64/diamondc        site-managed Linux install
    <root>/<release>/diamond/<x>/bin/lin64/diamondc        site-managed (versioned subdir)
    <root>/<release>/bin/nt64/pnmainc.exe                  Windows install

Extra search roots can be added with the DIAMOND_SEARCH_ROOTS environment
variable (os.pathsep separated).
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    EXE_RELATIVE_PATHS = (Path("bin") / "nt64" / "pnmainc.exe",)
    DEFAULT_ROOTS = (
        Path(r"C:\lscc\diamond"),
        Path(r"C:\Program Files\Lattice Semiconductor\Diamond"),
    )
else:
    EXE_RELATIVE_PATHS = (
        Path("bin") / "lin64" / "diamondc",
        Path("diamond") / "sub" / "bin" / "lin64" / "diamondc",
    )
    DEFAULT_ROOTS = (
        Path("/tools/dist/lattice/DIAMOND"),
        Path("/usr/local/diamond"),
        Path("/opt/lattice/diamond"),
        Path("/opt/lscc/diamond"),
        Path("/usr/local/lscc/diamond"),
    )

_SKIP_MARKERS = ("tmp", "test", "backup", "old")
_VERSIONED_SUBDIR_EXE = (
    Path("bin") / ("nt64" if IS_WINDOWS else "lin64") / ("pnmainc.exe" if IS_WINDOWS else "diamondc")
)


@dataclass(frozen=True)
class DiamondInstallation:
    """A discovered Diamond installation."""

    version: str
    install_dir: str
    exe: str
    source: str  # e.g. "filesystem:/tools/dist/lattice/DIAMOND"


def version_key(version: str) -> tuple[int, ...]:
    """Sortable key for a release directory name such as '3.14.0.75.0' or '3.10.p.082x64'."""
    cleaned = re.sub(r"x64", "", version, flags=re.IGNORECASE)
    return tuple(int(n) for n in re.findall(r"\d+", cleaned))


def _find_exe(release_dir: Path) -> Optional[Path]:
    for rel in EXE_RELATIVE_PATHS:
        candidate = release_dir / rel
        if candidate.is_file():
            return candidate
    versioned = release_dir / "diamond"
    if versioned.is_dir():
        try:
            for child in sorted(versioned.iterdir()):
                candidate = child / _VERSIONED_SUBDIR_EXE
                if candidate.is_file():
                    return candidate
        except OSError:
            return None
    return None


def _scan_release_dir(release_dir: Path, source: str) -> Optional[DiamondInstallation]:
    exe = _find_exe(release_dir)
    if exe is None:
        return None
    return DiamondInstallation(
        version=release_dir.name,
        install_dir=str(release_dir.resolve()),
        exe=str(exe.resolve()),
        source=source,
    )


def _scan_root(root: Path) -> list[DiamondInstallation]:
    if not root.is_dir():
        return []
    source = f"filesystem:{root}"
    found: list[DiamondInstallation] = []
    # The root itself may be a single install directory.
    direct = _scan_release_dir(root, source)
    if direct is not None:
        found.append(direct)
    try:
        children = sorted(root.iterdir())
    except OSError:
        return found
    for child in children:
        name = child.name.lower()
        if not child.is_dir() or not name[:1].isdigit():
            continue
        if any(marker in name for marker in _SKIP_MARKERS):
            continue
        install = _scan_release_dir(child, source)
        if install is not None:
            found.append(install)
    return found


def _search_roots(extra_roots: Optional[Iterable[Path]]) -> list[Path]:
    roots: list[Path] = list(extra_roots or [])
    env_roots = os.environ.get("DIAMOND_SEARCH_ROOTS", "")
    roots.extend(Path(p) for p in env_roots.split(os.pathsep) if p.strip())
    roots.extend(DEFAULT_ROOTS)
    return roots


def discover_diamond_installations(
    extra_roots: Optional[Iterable[Path]] = None,
) -> list[DiamondInstallation]:
    """
    Find Diamond installations on this machine.

    Returns:
        Installations sorted newest release first. Duplicates (same resolved
        executable) are removed.
    """
    seen: set[str] = set()
    installations: list[DiamondInstallation] = []
    for root in _search_roots(extra_roots):
        for install in _scan_root(root):
            if install.exe in seen:
                continue
            seen.add(install.exe)
            installations.append(install)
    installations.sort(key=lambda i: version_key(i.version), reverse=True)
    return installations


def installations_to_dicts(installations: list[DiamondInstallation]) -> list[dict]:
    return [
        {
            "version": i.version,
            "install_dir": i.install_dir,
            "diamond_exe": i.exe,
            "source": i.source,
        }
        for i in installations
    ]
