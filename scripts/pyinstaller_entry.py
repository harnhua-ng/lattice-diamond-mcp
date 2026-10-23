# Copyright(c) 2026 Lattice Semiconductor Corporation. All rights reserved. DO NOT DISTRIBUTE.
# Subject to the terms of the AI Solutions Software License Agreement
"""Entry point that PyInstaller freezes into the diamond-mcp executable."""

import os
import sys

from diamond_mcp import main

if __name__ == "__main__":
    try:
        main()
    finally:
        # When the client disconnects, the MCP stdio transport closes sys.stdout. PyInstaller's
        # bootloader then flushes it once more as the program exits, and prints a harmless but
        # alarming "ValueError: I/O operation on closed file" traceback to stderr. Hand it
        # something that can still be flushed.
        sys.stdout = sys.__stdout__ = open(os.devnull, "w")
