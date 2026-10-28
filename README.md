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
    config.py            executable, license, timeout and audit-log resolution; config file
    audit.py             command audit log (JSON lines, rotated, owner-only)
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
scripts/
    build_binary.py      builds the standalone (Python-free) installer zip, SBOM and checks
    diamond-mcp.spec     PyInstaller spec (drops host libgcc_s and the build tools)
    pyinstaller_entry.py the program PyInstaller freezes
    build_installer.py   builds the wheels installer zip (verifies it before writing)
    combine_installers.py  joins the per-OS zips of one kind into one Windows + Linux package
    install.py           the installer that ships in both zips (standard library only)
security_scan/
    run_scans.py         runs every scan below; README.md lists the results and findings
    secrets_scan.py      regular-expression secrets scan of the working tree
    osv_check.py         checks the packages pinned in uv.lock against OSV.dev
    dast_parsers.py      seeded fuzzing of the parsers and the Tcl quoting
    dast_stdio.py        probes of the running server over stdio
    results/             the scan output behind the README, before and after the fixes
.github/workflows/
    build-dist.yml       builds and verifies both installers on Linux and Windows
    security-scan.yml    unit tests and security_scan/run_scans.py on every push and weekly
uv.lock                  exact, hashed versions of every dependency (and of PyInstaller)
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

The editable install provides `python -m diamond_mcp`. There is no `diamond-mcp` command: the project
declares no console script, because pip would build a launcher program (`diamond-mcp.exe` on Windows)
from it, and the wheels installer installs no executable of its own.

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

The security scans are separate: `python security_scan/run_scans.py` (see `security_scan/README.md`).

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

### Audit log

`audit.py` writes one JSON object per line for session events and for every command the
session sends to Diamond. `DiamondSession.run_tcl` is the single choke point, so every
tool is covered. A `command` record is written before the command is sent and a `result`
record after it, so a command that hangs or crashes Diamond is still on record. The `_tool`
decorator stores the MCP tool name in a context variable (`audit.current_tool`) so records
say which tool issued each command, including commands issued by a tool on behalf of
another. Writing never raises: if the file cannot be written, the log disables itself with
one warning. The location comes from `DIAMOND_MCP_AUDIT_LOG` or `audit_log` in the config
file (see `config.resolve_audit_log_path`). The log is detective only and the account that
runs the server can alter it; the user guide explains the limits.

`tests/conftest.py` redirects the log into a temporary directory for every test, so the
suite never writes to a real home directory.

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
- **`run_tcl` refuses commands containing `prj_project close`** (`_CLOSE_COMMAND_RE` in
  `server.py`), so the one remaining route to the crash is closed.
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

There are two installers. Both are offline zips that `scripts/install.py` installs and registers
with Claude Code; `BUILDINFO` inside each says which kind it is.

| | Standalone (recommended) | Wheels |
|---|---|---|
| Built by | `scripts/build_binary.py` | `scripts/build_installer.py` |
| Built in CI | yes, `build-dist.yml` | yes, `build-dist.yml` |
| Result | PyInstaller program with its own CPython | wheels installed into a virtual environment |
| Python needed to **run** it | none | the exact version it was built for (3.11) |
| Python needed to **install** it | any 3.8 or later | the exact version it was built for |
| Minimum Linux | glibc 2.17 (Red Hat 7) | glibc 2.28 (Red Hat 8) |
| Size | about 17 MB zip, 34 MB installed | about 10 MB zip, 65 MB installed |
| SBOM | yes (CycloneDX, see below) | no |

### The standalone installer

```bash
pip install uv==0.13.5                 # the pinned version; the script refuses another
python scripts/build_binary.py         # writes dist/lattice-diamond-mcp-<version>-<platform>.zip (+ .sha256, .cdx.json)
```

Run it with any Python 3.11 or later. The Python that goes **into** the program is a separate,
uv-managed CPython (`PYTHON_VERSION` in the script), so the result does not depend on the build
machine's Python or system libraries. Build one zip per operating system, each on that system.

What the build does; any failure stops it and writes nothing:

1. Checks `uv lock --check` (the lock matches `pyproject.toml`) and the pinned uv version.
2. Installs the pinned CPython with `uv python install --no-bin --no-registry` (so uv creates no
   shortcuts or registry entries) and builds with `uv run --frozen --no-editable --group build
   pyinstaller scripts/diamond-mcp.spec`. Every package is the exact, hash-checked version in
   `uv.lock`. PyInstaller is a locked **build** dependency (`[dependency-groups] build` in
   `pyproject.toml`), not a runtime dependency.
3. **On Linux, fails if any bundled native file needs glibc newer than 2.17.** This gate is what
   keeps the program runnable on Red Hat 7, and it matters: PyInstaller copies `libgcc_s.so.1`
   from the build machine, and Ubuntu 22.04's copy needs glibc 2.35, which would stop the program
   starting on Red Hat 7 and Red Hat 8. The spec leaves that library out so the target's own copy
   is used (`HOST_LIBRARIES` in `scripts/diamond-mcp.spec`). It also leaves out the packaging
   tools PyInstaller's hooks would otherwise freeze in (`BUILD_TOOLS`).
4. Starts the built program and speaks MCP to it: `initialize`, `tools/list` (at least 30 tools)
   and a tool call.
5. Writes the SBOM, collects licence texts, writes `BUILDINFO` and `LEGAL_REVIEW.txt`, zips the result with `install.py`,
   then **installs that zip into a temporary folder and runs it again.**

`python scripts/build_binary.py --smoke-test PATH_TO_PROGRAM` runs only step 4 against an existing
program; the workflow uses it on the installed result.

The program is a folder (PyInstaller "onedir"), not a single file. A one-file build unpacks itself
into `/tmp` on every start, which is slower and needs a writable, executable `/tmp`; the sandbox
example in the user guide makes `/tmp` a private tmpfs. `scripts/pyinstaller_entry.py` also gives
the PyInstaller bootloader a flushable `sys.stdout` at exit: the MCP stdio transport closes
`sys.stdout` when the client disconnects, and without this the bootloader's final flush printed a
harmless but alarming `ValueError: I/O operation on closed file` traceback to stderr.

#### The SBOM

`uv export --format cyclonedx1.5` describes the locked Python packages only, and it is marked
experimental by uv. It does not know what is inside the finished program, so `build_binary.py`
corrects it:

- **Packages that are locked but not in the program are removed.** `cffi`, `cryptography`,
  `pycparser` and `pyjwt` (pulled in by `mcp`'s authentication code, which the stdio server never
  loads) and the Windows-only `pywin32` are listed in the SBOM's
  `diamond-mcp:locked-but-not-bundled` property instead, so scanners do not report vulnerabilities
  in code that does not ship.
- **The build fails if the program contains a package the SBOM would not list.** This was found
  by measurement: PyInstaller was freezing `setuptools` and `packaging` from the build
  environment into the program although they are not dependencies. They are now excluded, and
  `check_sbom_matches_bundle` keeps it that way.
- **The embedded interpreter and every bundled native file are added**, each native file with its
  SHA-256 (the program, `libpython`, and the compiled `pydantic_core` and `rpds`).
- Each package component carries the download location and SHA-256 of its files from the lock.
- PyInstaller and uv are recorded as build tools.

It does not contain licence data for the packages (uv does not export it); the licence texts are
in `THIRD_PARTY_LICENSES/` in the zip. The interpreter is a python-build-standalone build; the
SBOM names its version, not that project's own dependencies.

#### PyInstaller's licence (for Legal)

The PyInstaller *tool* is not shipped, but part of it is inside the program: the `diamond-mcp`
executable is PyInstaller's compiled bootloader, and three of its loader modules are embedded.
The licence file that ships with it (`COPYING.txt`, copied unmodified into `THIRD_PARTY_LICENSES/`)
says PyInstaller is GPL-2.0-or-later with a "Bootloader Exception" that gives unlimited permission
to link or embed the bootloader in other programs and distribute the result without restriction
from those files; its run-time hooks are Apache-2.0. The licence file is shipped as a notice, as a
precaution, and not because the exception is known to require one.

Each build writes `LEGAL_REVIEW.txt` into the zip and prints a pointer to it. It lists, for Legal,
the PyInstaller item (what is embedded, what the shipped licence says, the open question) and the
product `LICENSE`, which is a copy of the cortex-bridge text. It is generated from the licence file that ships, and says
`WARNING` instead of summarising the terms if the expected wording is no longer there, so a future
PyInstaller release with different terms cannot pass unnoticed. It states facts, not conclusions.
The wheels zip contains no PyInstaller code.

#### Reproducibility

The package set, the interpreter and the compiled libraries are identical from build to build,
and `SOURCE_DATE_EPOCH` (set from the commit time) makes the `diamond-mcp` program itself
identical too. The zip as a whole is **not** bit-for-bit reproducible: PyInstaller's
`base_library.zip`, two packages' `RECORD` files, `BUILDINFO` and the SBOM's serial number and
timestamp differ between builds. This was measured with two builds.

#### CI: building and testing both installers

`.github/workflows/build-dist.yml` builds **both** installers. It is started by hand: Actions tab,
**Build dist**, **Run workflow**, with three inputs:

| Input | Values | Default |
|---|---|---|
| `ref` | branch, tag or commit to build | `main` |
| `installers` | `both`, `standalone`, `wheels` | `both` |
| `wheels_python` | `3.11`, `3.12`, `3.13`, `all` (only for the wheels zips) | `3.11` |

| Job | What it does | Artifacts |
|---|---|---|
| `build-standalone` | `build_binary.py` on `ubuntu-22.04` and `windows-2022` | `lattice-diamond-mcp-standalone-<os>` (zip, `.sha256`, SBOM) |
| `verify-standalone` | installs each zip on a fresh runner with an installer Python that is not the build one (3.8 on Ubuntu 22.04 and Windows, 3.13 on Ubuntu 24.04), checks the checksum, speaks MCP to the installed program, runs it with an empty environment and checks `ldd` (Linux), uninstalls | none |
| `plan` | turns `wheels_python` into the list of Pythons to build | none |
| `build-wheels` | `build_installer.py` on each OS under each chosen Python | `lattice-diamond-mcp-wheels-<os>-py<version>` (zip, `.sha256`) |
| `verify-wheels` | installs each wheels zip under the Python it was built for, speaks MCP to the installed server, uninstalls | none |

The wheels zip is tied to one Python version, so `wheels_python` picks which. Each zip is built on the
OS it is for, because pip and uv resolve dependencies for the machine they run on. The two verify jobs
run even if another OS's build failed (`!cancelled()`), so a first-time Windows failure does not hide the
Linux result; the leg whose artifact is missing fails at the download step.

To test both kinds on both platforms, run it with the defaults. To build only one kind, set
`installers`. Two things must be in the ref you build: `.github/workflows/build-dist.yml` (GitHub's
documentation says a `workflow_dispatch` workflow must be on the default branch to be started) and
`uv.lock` (the standalone build fails without it, and fails if it is out of date).

**Status of the workflow:** its YAML validates against GitHub's workflow schema, and the shell steps of the
standalone and wheels jobs were extracted from the file and executed on a Linux host, in order, in
separate workspaces, starting from a bare Python. The workflow itself has **never run on GitHub**, and the
Windows legs have never run anywhere. The Ubuntu runner is also untested; the glibc gate exists so that
a surprise there fails the build instead of shipping. Install-test the first artifacts before relying on
them. Not covered: the wheels zip is not tested on a machine whose default `python3` is older than the
Python it needs (the standalone installer is, with 3.8), and neither route has a test job that runs the
repository's own unit tests.

### The wheels installer

```bash
pip install build                      # once; "pip install -e .[dist]" also works
python scripts/build_installer.py      # writes dist/lattice-diamond-mcp-<version>-<pythontag>-<platform>.zip
```

The zip holds `install.py`, our wheel and every dependency wheel (about 30 wheels, 9.5 MB),
`requirements.txt` (every wheel by exact version and SHA-256), `BUILDINFO`, the licence texts
taken from the dependency wheels, and the user guide. A `.sha256` file is written beside it.
Its installer path uses only the standard library, runs on Python 3.8 or later (a test enforces
this), and installs with `pip install --no-index --find-links wheels --require-hashes -r requirements.txt`,
so no network is used and a wheel that was changed after the build is refused.

What the build does:

1. Builds the wheel with `python -m build`.
2. Downloads dependency wheels with `pip download --only-binary=:all:` for the Python and
   operating system that run the build, at the versions recorded in `uv.lock` (given to pip as
   constraints). pip honours `PIP_INDEX_URL`, `PIP_FIND_LINKS`, proxy variables and `pip.conf`.
3. Checks every downloaded wheel against the SHA-256 that `uv.lock` records for it, and stops
   without writing anything if one differs or is not listed.
4. Writes `requirements.txt` with the hash of every wheel, extracts licence files, and
   records the glibc floor (the highest `manylinux` requirement among the wheels) and the
   hash of `uv.lock` in `BUILDINFO`.
5. **Installs the bundle into a throwaway environment by running its own `install.py`**,
   and fails if that does not work. `--skip-verify` bypasses this and must not be used for a
   release.
6. Writes the zip and its checksum.

One zip is built per Python version and per operating system, each on that system. Do not
build for another platform from this one: pip evaluates dependency markers for the machine
it runs on, so a Windows zip built on Linux would silently lack the Windows-only dependency
(`pywin32`).

### Combined packages (Windows and Linux in one zip)

The **Build dist** workflow builds each kind on each operating system, then joins the zips of one kind
into the packages to hand out, with `scripts/combine_installers.py`:

```
lattice-diamond-mcp-<version>-standalone-windows-linux-x86_64.zip     artifact lattice-diamond-mcp-combined-standalone
lattice-diamond-mcp-<version>-wheels-windows-linux-<cpNN>[-<cpNN>].zip  artifact lattice-diamond-mcp-combined-wheels
```

```
lattice-diamond-mcp-<version>/
    install.py              one installer for every build
    PACKAGES.txt            which folder is which build
    windows-x86_64/         a complete per-platform bundle, as it was built   (standalone)
    linux-x86_64/
    windows-x86_64-cp311/   one folder per system and Python version         (wheels)
    linux-x86_64-cp311/
```

Nothing inside a build is changed, so each folder's `BUILDINFO`, licences and SBOM still describe the
files next to them, and Unix permissions are kept. The script refuses to write anything unless both
operating systems are present, every input is the same kind, version and git commit, each system (and
Python version) appears once, and every `install.py` is identical. By hand:

```bash
python scripts/combine_installers.py --kind standalone --outdir dist  <windows zip> <linux zip>
python scripts/combine_installers.py --kind wheels     --outdir dist  <every wheels zip of both systems>
```

The combine jobs run only after every per-OS zip of their kind has built and been installed on a fresh
runner, and each combined zip is itself installed on fresh Linux and Windows runners (and, for wheels,
under every Python version it holds) before the workflow succeeds. The per-OS artifacts remain as
intermediates. `tests/test_combine.py` covers the combining and the choice of folder; the Windows legs
have never run (see "Things to know").

### What the installer does on the target machine

`install.py` reads `BUILDINFO` and refuses a wrong operating system or a too-old glibc. In a combined
zip it first chooses the build: the folder for this operating system and, for wheels, the one whose
Python it can find (the running interpreter's version first, then newer to older; `--python` picks the
folder for that interpreter's version). It prints which folder it chose.

- **Standalone zip:** copies the `diamond-mcp` folder into `--install-dir` (default
  `~/diamond-mcp`) through a staging folder, so a failed copy never leaves a half-installed
  program; restores the execute bit (some unzip tools drop it); runs `diamond-mcp --help`; and
  registers it.
- **Wheels zip:** finds the Python it was built for (searching `python3.11`, `python3`, `python`,
  and `py -3.11` on Windows, so an older default `python3` is fine), creates a virtual
  environment in `--venv-dir` (default `%LOCALAPPDATA%\lattice-diamond-mcp\venv` on Windows, `~/.local/share/lattice-diamond-mcp/venv` on Linux; earlier versions used `~/diamond-mcp-venv`, which `--uninstall` still removes), installs (with `--force-reinstall`, so a
  rebuild with an unchanged version number still replaces the old files), checks, and registers
  `<environment>/python -I -m diamond_mcp`. No launcher program is installed. `-I` (isolated mode) makes
  Python ignore `PYTHONPATH` and the current folder: with a plain `-m`, a stray `mcp.py` in the folder the
  AI client runs in is imported in place of the real package. An earlier install's registration of
  `bin/diamond-mcp` (or `Scripts\diamond-mcp.exe`) is replaced, and removed again by `--uninstall`.

**A running server.** Both first look for processes running from the folder they are about to replace (every
open Claude Code or VS Code session starts one). On Windows the test is whether the folder's `.exe`, `.pyd`
and `.dll` files are held open (a read-only file is not mistaken for a locked one); on Linux it is a scan of
`/proc/*/cmdline` for a program under the folder. If anything runs, the installer lists it and asks: stop
them, wait while you stop them, or exit with nothing changed. Nothing is stopped without that answer. With
no one to answer (stdin closed, a script, an AI assistant) it exits with status 1; `--if-locked kill`
(or `DIAMOND_MCP_IF_LOCKED=kill`) approves stopping in advance and `--if-locked exit` refuses. Stopping uses
SIGTERM, then SIGKILL after five seconds, on Linux and `taskkill /F /T` (the whole process tree, because the
environment's `python.exe` is a launcher and its child holds the modules) on Windows. The check runs before
pip or the copy touches anything, and `--uninstall` makes it before removing the registration, so exiting
leaves both in place. A folder the installer did not create is never checked or touched. The mechanism is
the one in `scripts/install.py` of cortex-bridge (`--if-locked`).

Both mark the folder they create with `.diamond-mcp-installer` and never reuse, replace or
delete a folder that lacks it (an existing empty folder is accepted). Registration is
`claude mcp add --scope user diamond -- <program>`; it will not overwrite a registration of the
same name that points elsewhere. `--uninstall` reverses it with the same protections, and finds
the right kind of installation from `BUILDINFO`. Note the argument order: `claude mcp add` treats
`-e` as taking several values, so the server name must come before it.

### Tests for the installers

`tests/test_installer.py` covers the wheels route; `tests/test_binary_build.py` covers the
standalone route. Between them: wheel and licence helpers, the glibc gate (and that the spec
excludes `libgcc_s` and the build tools), the pins staying consistent (PyInstaller in `pyproject.toml`
equals the lock), SBOM augmentation, pruning and validation, the bundle-versus-SBOM check, licence
collection, `LEGAL_REVIEW.txt` (including its warning when the licence wording changes), the smoke test against the real server and against programs that misbehave (always a
clean build failure, never a traceback), zip permissions, and the binary install, reinstall,
refusal, dry-run, uninstall and failed-start cases against a fake `claude`. Two more tests build
and install the real bundles; they need network access and are skipped unless
`DIAMOND_TEST_INSTALLER=1` (wheels) or `DIAMOND_TEST_BINARY=1` (standalone, also needs uv).

### Updating pinned actions and dependencies

Nothing updates these automatically; the pins are in git and change only when someone changes them.

- **Actions** are pinned to commit SHAs in `.github/workflows/*.yml`, with the version in a comment.
  To update one, look up the commit of the new tag and replace the SHA and the comment in every place the
  action is used (`grep -rn "actions/checkout@" .github`):

  ```bash
  git ls-remote https://github.com/actions/checkout refs/tags/v7.0.1 'refs/tags/v7.0.1^{}'
  ```

  Use the `^{}` line when there is one (an annotated tag); otherwise the first. Then run the workflow.
- **Python dependencies** are pinned with hashes in `uv.lock`: `uv lock --upgrade-package <name>` (or
  `uv lock --upgrade`), then the tests, the scans and a build. The weekly **Security scan** workflow reports an advisory against
  a locked package, so a vulnerable pin is flagged without anyone looking; a merely old one is not.
  Nothing flags an action pin that has gone stale, so check them now and then, and before a release.

### Releasing

1. Update `version` in `pyproject.toml`; if dependencies changed, run `uv lock` and commit
   `uv.lock` (CI fails on a stale lock).
2. Run `pytest`, `pytest -m integration` against a real Diamond, and the two opt-in tests above.
3. Run the **Build dist** workflow (its combined artifacts are the packages to release), or
   `python scripts/build_binary.py` and `python scripts/build_installer.py` on each supported system
   and then `scripts/combine_installers.py`.
4. Check for newer pinned actions and dependencies (see above), add the approved `LICENSE` text (see below), and review `THIRD_PARTY_LICENSES.txt` and any
   build warning about packages without a licence file.
5. Update the revision history in the user guide.

### Things to know

- **The dependency set is dominated by `mcp`.** Its transitive dependencies include
  `cryptography` and `cffi` (through `pyjwt[crypto]`), which this server never loads, and
  `pydantic-core` and `rpds-py`, which it does. They make the wheels bundle platform specific,
  and `cryptography` raises its glibc floor to 2.28. The standalone zip does not contain the two
  that are not loaded, but `uv.lock` still pins them, so they still need tracking for security
  updates until `mcp` stops requiring them.
- **The standalone program needs `libz.so.1` and `libgcc_s.so.1` from the system**, plus `libc`,
  `libdl` and `libpthread`. `libgcc_s` is deliberately not bundled (see above); `libz` is linked
  by PyInstaller's bootloader. Both were present on the Red Hat 7 and 8 hosts tested; the CI `ldd`
  step only checks that none is missing on its own runner, so a very minimal Linux image could
  still lack one.
- **Verified:** standalone zip built on Red Hat 8.10, installed and run on Red Hat 7.9 (glibc 2.17)
  with a real MCP handshake of all 32 tools, installed with that host's own Python 3.8.5, and a
  full real Diamond 3.14.0.75.0 compile with bitstream through it inside the `bwrap` sandbox from
  the user guide. **Not verified:** Windows, GitHub's Ubuntu runners, any non-x86-64 machine.
  Only the Linux build host's behaviour has been observed.
- **PyInstaller built with the host Python was rejected, and that finding stands:** a build made
  with the Red Hat 8 system Python needs glibc 2.28 (it bundles that Python's `libpython`, `libssl`,
  `libreadline` and others) and fails on Red Hat 7 with `GLIBC_2.28 not found`. Using uv's managed
  Python (a python-build-standalone build against glibc 2.17) is what makes the standalone zip
  work there.
- **`LICENSE` is a byte-for-byte copy of cortex-bridge's AI Solutions Software License Agreement**
  (its own notice allows free distribution of the text if the copyright line is kept, and
  forbids changing the terms), and both zips contain it. A test checks that it is the unmodified
  text, and the standalone `BUILDINFO` records its SHA-256. Whether it is the approved text for
  this product is for Legal (`LEGAL_REVIEW.txt` says so). Section 4 of the agreement points at
  `THIRD_PARTY_LICENSES.txt`, which both zips ship. cortex-bridge also keeps a third-party licence
  review file; it has not been reproduced here, and these builds do not check dependency licences
  against a review list.
- `python -m build` prints a deprecation warning that `project.license` as a TOML table is
  deprecated (removal announced for 2027-02-18). The Radiant server has the same field.
  When both move to setuptools 77 or later, use an SPDX expression such as
  `LicenseRef-Lattice-Proprietary`.

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

- Preventive controls for `run_tcl` (an allowlist or a mode that disables it). The user guide
  describes sandboxing and the audit log instead; software checks inside the server cannot
  be made airtight.
- Remote execution (SSH, LSF `bsub`) and background compilation jobs. Flows run on the
  host that runs the server; long flows hold the tool call until they finish or time out.
- Running a simulator. The server can generate post-Map and post-PAR simulation netlists
  (`run_flow` with the tasks listed in the user guide), but Diamond's Tcl console has no
  command that runs a simulation. A tool would have to launch the bundled QuestaSim (or
  another simulator) directly, as the Radiant server does; this needs a design for library
  compilation (`cmpl_libs`), testbench handling and licensing.
- Documentation search. Use the `radiant-code-kb` server for knowledge-base queries.
- Listing available strategy options (`prj_strgy list_value` and `list_option`): neither
  returned results for the patterns tried. Read and write individual options with
  `get_strategy_option` and `set_strategy_option`, or use `run_tcl`.
