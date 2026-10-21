# Copyright(c) 2026 Lattice Semiconductor Corporation. All rights reserved. DO NOT DISTRIBUTE.
# Subject to the terms of the AI Solutions Software License Agreement
"""
Lattice Diamond MCP Server

Exposes Diamond FPGA design operations as MCP tools via a persistent
Diamond Tcl console session. Import main() to start the server.
"""

from .server import main

__all__ = ["main"]
