# -*- mode: python ; coding: utf-8 -*-
# Copyright(c) 2026 Lattice Semiconductor Corporation. All rights reserved. DO NOT DISTRIBUTE.
# Subject to the terms of the AI Solutions Software License Agreement
#
# PyInstaller spec for the standalone diamond-mcp executable. Run by scripts/build_binary.py,
# not by hand: it expects to be started through `uv run` so that the locked environment is used.
#
# One folder ("onedir"), not one file: a one-file build unpacks itself into /tmp on every start,
# which is slower and needs a writable, executable /tmp (the sandbox example in the user guide
# makes /tmp a private tmpfs).
import os

# Libraries PyInstaller would copy from the BUILD machine although every target already has
# one. They carry the build machine's glibc requirement: Ubuntu 22.04's libgcc_s.so.1 needs
# glibc 2.35, so bundling it makes the program refuse to start on Red Hat 7 (glibc 2.17) and
# Red Hat 8 (2.28). scripts/build_binary.py fails the build if any bundled file still needs
# a newer glibc than the supported floor.
HOST_LIBRARIES = ("libgcc_s.so",)

# Packaging tools that live in the build environment and that PyInstaller's hooks would otherwise
# freeze into the program. They are not dependencies of the server, so they are not in the SBOM;
# scripts/build_binary.py fails the build if any package is bundled that the SBOM does not list.
BUILD_TOOLS = ["setuptools", "pkg_resources", "_distutils_hack", "packaging"]

a = Analysis(
    [os.path.join(SPECPATH, "pyinstaller_entry.py")],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=BUILD_TOOLS,
    noarchive=False,
)
a.binaries = [entry for entry in a.binaries if not os.path.basename(entry[0]).startswith(HOST_LIBRARIES)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="diamond-mcp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="diamond-mcp",
)
