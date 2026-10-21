# lattice-diamond-mcp

MCP server for Lattice Diamond. It keeps one Diamond Tcl console alive and exposes
project, flow and report operations as MCP tools, so an AI assistant can open a
project, run synthesis, map, place and route, generate a bitstream, and read back
timing and utilisation without paying Diamond's start-up cost on every command.

This README is for developers: how the code is organised, how to run the tests,
and how to build and package a release. End users should read
[docs/Lattice_Diamond_MCP_Server_UserGuide.md](docs/Lattice_Diamond_MCP_Server_UserGuide.md).

## Table of Contents

- [Repository layout](#repository-layout)
- [Requirements](#requirements)
- [Development setup](#development-setup)
- [Running the tests](#running-the-tests)
- [Architecture](#architecture)
- [Diamond behaviour you must know about](#diamond-behaviour-you-must-know-about)
- [Adding a tool](#adding-a-tool)
- [How to build and package](#how-to-build-and-package)
- [Validation status](#validation-status)
- [Not yet supported](#not-yet-supported)

## Repository layout

```
src/diamond_mcp/
    __init__.py          exports main()
    __main__.py          python -m diamond_mcp
    server.py            FastMCP tool definitions (32 tools) and the entry point
    diamond_session.py   persistent Tcl console: protocol, timeouts, crash handling
    config.py            executable, license and timeout resolution; config file
    discovery.py         finds Diamond installations on disk
    project.py           reads .ldf files; checks for missing sources; locates reports
    reports.py           parsers for .par, .twr and .mrp reports
docs/
    Lattice_Diamond_MCP_Server_UserGuide.md    user-facing guide
tests/
    conftest.py          shared fixtures
    fake_diamond/        test double for diamondc, run by the real tclsh
    data/                trimmed excerpts of real Diamond 3.14 reports
    test_*.py            unit tests
    test_integration.py  tests against a real Diamond (skipped by default)
pyproject.toml           packaging and pytest configuration
```

## Requirements

- Python 3.11 or later.
- The `mcp` package, version 1.29 or later and below 2 (installed automatically).
- To run the server for real: Lattice Diamond 3.10 or later and a license. 3.14.0.75.0 is
  the version it has been validated against.
- To run the unit tests: `pytest` and `tclsh` (Tcl 8.5 or later). Tests that need `tclsh`
  are skipped when it is not installed.

Platforms: developed and validated on Linux. The Windows console is `pnmainc.exe`;
discovery and launching are written for it but it has not been exercised.

## Development setup

```bash
git clone <repository-url> lattice-diamond-mcp
cd lattice-diamond-mcp
python3.11 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[test]"
```

The editable install provides the `diamond-mcp` command and `python -m diamond_mcp`.

## Running the tests

Unit tests need no Diamond installation or license:

```bash
pytest
```

They exercise the session and server against `tests/fake_diamond`, a small Tcl program
that behaves like the Diamond console, run by the real `tclsh`. This covers process
handling, the command protocol, timeouts, crash detection and every tool. The report
parsers are tested on real Diamond output kept in `tests/data`.

`tests/test_stdio_server.py` launches `python -m diamond_mcp` as a subprocess and talks
MCP to it over stdio, so the entry point, transport and tool schemas are covered.

Integration tests run against a real Diamond. They are skipped unless
`DIAMOND_TEST_LDF` is set:

```bash
export DIAMOND_EXE=/path/to/diamondc               # or rely on auto-detection
export LM_LICENSE_FILE=port@license-server          # if your site needs it
export DIAMOND_TEST_LDF=/path/to/small_design/project.ldf
export DIAMOND_TEST_ROOT=/path/to/small_design      # optional, see below
pytest -m integration
```

The project is copied to a temporary directory first, so the original is never
modified. `DIAMOND_TEST_ROOT` is the directory that is copied; it defaults to the
folder containing the `.ldf`. Set it higher when the project references sources
outside its own folder. Choose a design that compiles in about a minute.

## Architecture

### Session protocol

`DiamondSession` starts the console with piped stdin and stdout and a background reader
thread. Each command is written to a temporary Tcl file and sent as one line:

```
set ::__dmcp_rc [catch {source {<file>}} ::__dmcp_res]; puts <BEGIN>; puts $::__dmcp_res; puts <DONE_rc>; flush stdout
```

This gives three properties:

1. Any multi-line Tcl works without quoting problems.
2. An uncaught Tcl error never reaches the console.
3. The Tcl return code and result are exact, delimited by unique sentinel lines. Success
   is the return code, not a guess from the log text.

A command that exceeds its timeout, or a console that dies mid-command, marks the
session down and clears the open-project record. The server restarts the console on the
next tool call and tells the caller (`session_note`) that the project must be reopened.
Process-group kill is used so Diamond's child engines do not linger.

### Tool layer

`server.py` registers each tool through the `_tool` decorator. Tools are plain
synchronous functions; the decorator registers an async wrapper that runs them in a
worker thread, so a multi-minute flow does not block the MCP event loop. The decorator
returns the plain function, so tests call tools directly.

Every tool returns a dict with `success`, `output`, `warnings`, `elapsed_ms` and
`timestamp`. Failed tools may add `hints`: suggestions matched from known failure text
(see `_HINT_RULES` in `server.py`). Flow tools may add `session_note`.

### Why closing a project restarts the console

See the next section. The short version: `prj_project close` is unreliable, so the
server never sends it.

### Offline helpers

`project.py` and `reports.py` do not need a running Diamond. `open_project` and
`compile_project` use `project.py` to detect missing source files before Diamond sees
them, including files that differ only in letter case (a Windows-authored project
opened on Linux). Report paths follow `<implementation dir>/<project title>_<implementation title>.<ext>`.

## Diamond behaviour you must know about

These were found by running real Diamond and are encoded in the code and tests.

- **`prj_project close` intermittently crashes Diamond 3.14.0.75.0 with SIGSEGV.** It is
  most likely when no flow stage has run since the project was opened (open, query,
  close). It reproduces whether commands arrive directly, through `catch`, `eval` or
  `source`, and it is intermittent, so a few passing runs prove nothing. The server
  therefore closes a project by restarting the console (about 3 seconds). An ad hoc loop
  of 8 open, query, close cycles passed with this design, and the integration test
  repeats the pattern 5 times.
- **`prj_run Export` does not write a bitstream** with default strategy settings. It
  returns success and produces nothing. `generate_bitstream` runs
  `prj_run Export -task Bitgen` and then checks that a `.bit` file was written.
- **Failures are in the return code, details are in the log.** A failed `prj_run`
  returns only `Running milestone "Synthesis" failed`. The cause (an expired tool, a
  missing file) is in the console output, which the server returns as `output` and
  scans for `warnings`.
- **A bad device is reported only as "Error in reading project file".** The device name
  is checked by `prj_project new -dev` with a clearer message. Parts valid in 3.14
  (for example LCMXO3LF-1300C-5TG100C) are rejected by 3.10.
- **`hlp` is not defined** in the piped console. Use the usage text that Diamond prints
  when a command is called with wrong arguments.
- **The bundled Synplify in some installations has expired.** Diamond 3.10.0.82.0
  reports `This software has expired` from Synplify. LSE-based projects are unaffected.
- **No display is needed** for the console, and a personal `FOUNDRY` environment variable
  is overridden by `diamond_env`.
- **The site `diamond` wrapper cannot be used headless.** It always requests a GUI window.
  The server starts the installation's own `diamondc` directly.
- **PAR reports score in nanoseconds, timing reports in picoseconds.** The parsers name
  the keys `*_ns` and `*_ps` accordingly.

## Adding a tool

1. Write a plain function in `server.py` with type hints and a docstring. The docstring
   becomes the tool description the model reads, so state what it does, what the
   parameters mean, and any caveat that changes how it should be used.
2. Decorate it with `@_tool`.
3. Build Tcl with `tcl_quote()` for every user-supplied value. Never interpolate raw
   strings into a command.
4. Return through `_run()`, `_ok_dict()` or `_error_dict()`.
5. Add the name to `EXPECTED_TOOLS` in `tests/test_server.py` and write tests against the
   fake console. Extend `tests/fake_diamond/fake_console.tcl` if the command is new.
6. If the command's real behaviour is not documented, run it against real Diamond and
   add what you learn to the list above.

## How to build and package

Install the build front end and build both distributions:

```bash
pip install build
python -m build
```

This writes `dist/lattice_diamond_mcp-<version>-py3-none-any.whl` and a source
distribution. The wheel contains the `diamond_mcp` package and the `diamond-mcp` console
script; tests and test data are not included.

Check a build in a clean environment:

```bash
python3.11 -m venv /tmp/verify && /tmp/verify/bin/pip install dist/*.whl
/tmp/verify/bin/diamond-mcp --help
```

To release:

1. Update `version` in `pyproject.toml`.
2. Run `pytest`, and `pytest -m integration` against a real Diamond.
3. Run `python -m build` and verify the wheel as above.
4. Update the revision history in the user guide.

Notes:

- `pyproject.toml` lists `wheel` in the build requirements so that builds also work with
  older setuptools releases.
- `python -m build` prints a deprecation warning that `project.license` as a TOML table
  is deprecated (removal is announced for 2027-02-18). The Radiant server's
  `pyproject.toml` has the same field. When both move to setuptools 77 or later, replace
  it with an SPDX expression such as `LicenseRef-Lattice-Proprietary`.
- Dependencies are not vendored. For an environment without network access, download the
  wheels with `pip download .` on a connected machine and install with
  `pip install --no-index --find-links <dir> lattice-diamond-mcp`.

## Validation status

Validated against Diamond 3.14.0.75.0 on Linux:

- 4 integration tests pass (environment check, full compile with bitstream, five
  open/query/close cycles, failure handling with session survival).
- Five projects (three MachXO3LF, two MachXO3D) were compiled through the MCP server over
  stdio: Synthesis, Translate, Map, PAR and Bitgen all succeeded, a bitstream was written
  for each, and the resource and timing figures matched manual runs of the same designs.
  Run times were 36 to 62 seconds per project.

Diamond 3.10.0.82.0 starts and runs the command set, but it rejects some newer parts and
its bundled Synplify has expired, so five MachXO-family benchmarks failed there for
reasons unrelated to the server. No Diamond 3.15 installation was available for testing.

## Not yet supported

These exist in the sibling Radiant server and have not been built here:

- Remote execution (SSH, LSF `bsub`) and background compilation jobs. Flows run on the
  host that runs the server; long flows hold the tool call until they finish or time out.
- RTL simulation.
- Documentation search. Use the `radiant-code-kb` server for knowledge-base queries.
- Listing available strategy options (`prj_strgy list_value` and `list_option`): neither
  returned results for the patterns tried. Read and write individual options with
  `get_strategy_option` and `set_strategy_option`, or use `run_tcl`.
