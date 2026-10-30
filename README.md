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
    create_export.py     the entry point: builds the embedded packages, combines them, writes SBOM and records
    build_embedded.py    builds the Windows or Linux embedded-Python zip (checks interpreter, wheels, layout)
    build_installer.py   builds the wheels installer zip (verifies it before writing)
    combine_installers.py  joins the per-OS zips of one kind into one Windows + Linux package
    install.py           the installer that ships in every zip (standard library only)
    smoke_test.py        speaks MCP to an installed server (used by the builds and CI)
    fetch_python_notices.py  refreshes the licence notices of the libraries inside the interpreters
    python_notices/      those notices, with the hashes of where they came from
    keys/                the python.org Windows release-signing key (OpenPGP)
security_scan/
    run_scans.py         runs every scan below; README.md lists the results and findings
    secrets_scan.py      regular-expression secrets scan of the working tree
    osv_check.py         checks the packages pinned in uv.lock against OSV.dev
    dast_parsers.py      seeded fuzzing of the parsers and the Tcl quoting
    dast_stdio.py        probes of the running server over stdio
    results/             the scan output behind the README, before and after the fixes
.github/workflows/
    build-dist.yml       builds and verifies the installers on Linux and Windows
    security-scan.yml    unit tests and security_scan/run_scans.py on every push and weekly
uv.lock                  exact, hashed versions of every dependency
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

There are two installers. Both are offline zips that `scripts/install.py` installs and registers with
Claude Code; `BUILDINFO` inside each says which kind it is. Neither installs a program of ours.

| | Embedded (recommended) | Wheels |
|---|---|---|
| Built by | `scripts/create_export.py` (runs `scripts/build_embedded.py`) | `scripts/build_installer.py` |
| Built in CI | yes, `build-dist.yml` | yes, `build-dist.yml` |
| Result | a bundled Python with the locked packages inside it | wheels installed into a virtual environment |
| Interpreter | Windows: python.org's embeddable 3.12.10 (PSF-signed); Linux: python-build-standalone 3.12.10 | the customer's |
| Python needed to **run** it | none | the exact version it was built for (3.11) |
| Python needed to **install** it | any 3.8 or later | the exact version it was built for |
| Minimum Linux | glibc 2.17 (Red Hat 7) | glibc 2.28 (Red Hat 8) |
| Size | about 16 MB (Windows) and 20 MB (Linux) zipped; 36 MB combined | about 10 MB zip, 65 MB installed |
| SBOM | yes (CycloneDX, beside the zip) | no |

Earlier versions also built a standalone PyInstaller program and a "hybrid" package (the Windows embedded
package with that program for Linux). Both were removed once the embedded package passed CI on both systems:
the PyInstaller launcher was an unsigned `diamond-mcp.exe` that Windows Defender and SmartScreen distrusted.

### `create_export.py`: one command for the customer package

`scripts/create_export.py` is the single entry point, with the same options, file names and sidecar files as
cortex-bridge's script of that name. It runs `build_embedded.py` for each system and `combine_installers.py`.

```
python scripts/create_export.py --both                       # the combined Windows + Linux package
python scripts/create_export.py --windows --outdir out       # Windows half only
python scripts/create_export.py --linux --outdir out         # Linux half only
python scripts/create_export.py --both --ref main --label rc1 --outdir dist/main
python scripts/create_export.py --both --windows-zip W.zip --linux-zip L.zip     # combine halves built elsewhere
```

| | cortex-bridge | here |
|---|---|---|
| Required mode flag | `--online`, `--offline`, `--both` | `--windows`, `--linux`, `--both` |
| `--outdir`, `--label`, `--no-verify`, `--allow-license-gaps` | yes | yes (same meaning) |
| Build from a git ref | `--bridge-ref` (default `WORKTREE`) | `--ref` (default `WORKTREE`: the working tree, uncommitted changes included) |
| `--python`, `--online-python`, `--platform` | per-Python wheel zips | not needed: each package carries its own interpreter |
| Sidecars beside each zip | `.sha256`, `.sbom.cdx.json`, `.buildinfo` | the same three |
| Inside the zip | `BUILDINFO`, `MANIFEST.sha256`, licences | `BUILDINFO`, `MANIFEST.sha256` (per OS folder), licences, `LEGAL_REVIEW.txt`, `NATIVE_FILES.txt` |

Both halves only download and unpack files, so every mode works on any system; the finished zip is installed with its
own `install.py` and the installed server is started over MCP (`scripts/smoke_test.py`) for the system the script runs
on, and the other half is reported as not run. `--embed-zip` (Windows) and `--linux-python` (Linux) use interpreter
archives that were downloaded already. The script fails when a build reports a package without a licence text, unless
`--allow-license-gaps` is given. The `.buildinfo` file is an internal record (commit, parts and their hashes,
uncommitted-change note) and is never shipped.

`MANIFEST.sha256` (`<sha256>  <path>` per file) is written by every build, and `install.py` checks it before it
installs anything: a changed or missing file stops the install and names the file. In a combined zip each OS folder
keeps its manifest, minus the line for `install.py` (the single one at the top).

### The embedded packages

The common design: a bundled Python with the packages from `uv.lock` unpacked into its `site-packages`, no executable of
ours, no pip, started as `<interpreter> -I -m diamond_mcp`. In both:

- **Every wheel** is checked against its SHA-256 in `uv.lock`, downloaded with pip for the *target* system: pip judges a
  dependency's environment markers (`pywin32; sys_platform == 'win32'`) by the machine it runs on, whatever
  `--platform` says, so the build runs pip with the target's marker values (as cortex-bridge's `create_export.py`
  does) and then checks that every package the wheels need on that system is present.
- **Packages the server never imports are left out:** `OMITTED_PACKAGES` in `build_embedded.py` (pyjwt, cryptography,
  cffi, pycparser: only mcp's OAuth code uses pyjwt). A test starts the server and fails if any of them is imported,
  and the verify jobs check the installed server.
- **A wheel's `.data` folder** is unpacked by scheme (purelib and platlib into site-packages); scripts are left out and
  reported; anything else is refused.
- The package carries `BUILDINFO`, `MANIFEST.sha256`, `LEGAL_REVIEW.txt` (facts and open questions for Legal),
  `NATIVE_FILES.txt` (every native file with its SHA-256, and its signer on Windows), `THIRD_PARTY_LICENSES/`
  (each wheel's licence files, CPython's licence and the notices of the libraries built into the interpreter) and a
  copy of the product `LICENSE`.

**Windows** (`build_embedded.py --platform windows`): python.org's embeddable Python 3.12.10, which the Python Software
Foundation has signed. The build checks the zip against the SHA-256 pinned in the script (`EMBED_SHA256`) **and**
against python.org's OpenPGP signature (`.asc`) with the Windows release signer's key, checked in at
`scripts/keys/python-windows-release-signing.asc` and pinned by fingerprint (`PGP_FINGERPRINT`, Steve Dower, Python
Release Signing; needs `gpg`, which Git for Windows has; `--skip-pgp` is for local experiments and marks the build
`python_pgp_verified: false`). It checks that `python.exe` carries the PSF's name in its signature block (the workflow's
verify job has Windows validate it with `Get-AuthenticodeSignature`) and that `python.exe` is the only program
(`pythonw.exe` is removed). The `._pth` file lists the library zip, the folder and `Lib\site-packages` and ends with
`import site`, so that pywin32's `.pth` file is read; with a `._pth` file Python still ignores `PYTHONPATH` and the current
folder. `mcp` imports `pywintypes`, `win32api`, `win32con` and `win32job` on Windows, so pywin32 is needed, but only eight
of its files are kept (`PYWIN32_KEEP`); the build reads the import tables of the remaining native files and stops if one
needs a DLL that was removed, and the verify job imports them for real. Six unsigned third-party native files remain
(`_pydantic_core`, `rpds`, and four from pywin32); pywin32's `Pythonwin.exe` and `pythonservice.exe` are deleted.

python.org's Windows binaries contain Microsoft Distributable Code (`vcruntime140*.dll`), whose terms are in the
`LICENSE.txt` of python.org's zip ("Additional Conditions for this Windows binary build") and require that end users agree
to terms protecting it, so the package ships `THIRD_PARTY_LICENSES/microsoft-distributable-code.txt`, reached through
section 4 of the product licence (which is not edited; its reverse-engineering sentence is unverified wording from the
project owner, and the build warns if python.org's clause ever changes). The notices for the libraries python.org builds
in are its own list for the release (`Doc/license.rst` and the library versions of its Windows build, in
`scripts/python_notices/cpython-3.12.10/`) plus the python-build-standalone texts from `scripts/python_notices/windows-x86_64/`.

**Updating the signing key pin.** When a release is signed by someone else, fetch the new key, confirm its fingerprint
against python.org's published list of release-signing keys (the script cannot do that for you), and replace the key
file and the constant together.

**Linux** (`build_embedded.py --platform linux`): CPython 3.12.10 from python-build-standalone release 20250529, the build
that uv 0.13.5 (`UV_VERSION`) fetches for that version. The build downloads that archive from the pinned URL and checks it
against the SHA-256 pinned in the script (`PBS_SHA256`, taken from the release's `SHA256SUMS`); python-build-standalone
publishes no signature, so there is no OpenPGP check. From the `install_only_stripped` archive it keeps `bin/python3.12`
and the standard library and deletes the rest (pip, Tcl/Tk, IDLE, the test suite, headers, man pages, the shared
`libpython`, which the statically linked interpreter does not use, and every symbolic link). It stops on a path outside
`python/` in the archive, a missing interpreter, any file needing a glibc newer than 2.17 (read with `objdump -T`),
anything in `bin/` but the interpreter, a symbolic link, pip, or notices in `scripts/python_notices/` from another
python-build-standalone release (`PBS_RELEASE` against `scripts/python_notices/RELEASE`). The package holds three ELF
files (the interpreter and the `pydantic_core` and `rpds` modules). It is not free of system dependencies: the interpreter
links the system glibc (libc, libm, libpthread), which is why the floor is glibc 2.17.

**Notices of the libraries inside the interpreters.** OpenSSL, zlib, expat, bzip2, liblzma, mpdecimal and, on Linux, libedit,
ncurses, libffi, libuuid and SQLite are built into the interpreters. `scripts/fetch_python_notices.py` fills
`scripts/python_notices/<platform>/` with the notices python-build-standalone publishes for a release (checked against the
release's `SHA256SUMS`; `SOURCES.txt` records every hash), and with `--cpython-only VERSION` writes python.org's own notices
for that version's Windows build. The files are marked `-text` in `.gitattributes`, so git does not change their line endings.

**Which Python: 3.12.10, on both systems, as a starting point.** python.org builds an embeddable zip only for releases that
have binaries, and for 3.12 the last is 3.12.10 (3.12.11 to 3.12.16 are source-only security releases). The cost: the
interpreter lacks the fixes of those six releases, and the Windows ones cannot be had as python.org binaries. 3.13.17 is
current on both systems and has locked wheels, but python.org already publishes 3.13.18 as source only; 3.14 still has a
Windows zip (3.14.10) and locked wheels but has not been validated here. To move, change `EMBED_VERSION`, `EMBED_SHA256`,
`PBS_RELEASE` and `PBS_SHA256` together, re-run `scripts/fetch_python_notices.py` in both modes, and check the signature
note above.

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

The **Build dist** workflow builds each kind on each operating system, then joins the zips of one kind into the packages
to hand out, with `scripts/combine_installers.py`:

```
lattice-diamond-mcp-<version>-embedded-windows-linux-x86_64.zip       artifact lattice-diamond-mcp-combined-embedded
lattice-diamond-mcp-<version>-wheels-windows-linux-<cpNN>[-<cpNN>].zip  artifact lattice-diamond-mcp-combined-wheels
```

```
lattice-diamond-mcp-<version>/
    install.py              one installer for every build
    PACKAGES.txt            which folder is which build
    windows-x86_64/         a complete per-platform bundle, as it was built   (embedded)
    linux-x86_64/
    windows-x86_64-cp311/   one folder per system and Python version         (wheels)
    linux-x86_64-cp311/
```

Nothing inside a build is changed (except that each folder's `MANIFEST.sha256` loses the line for `install.py`), so each
folder's `BUILDINFO` and licences still describe the files next to them, and Unix permissions are kept. The script refuses
to write anything unless both operating systems are present, every input is the same kind, version and git commit, each
system (and Python version) appears once, and every `install.py` is identical. By hand:

```bash
python scripts/combine_installers.py --kind embedded --outdir dist  <windows zip> <linux zip>
python scripts/combine_installers.py --kind wheels   --outdir dist  <every wheels zip of both systems>
```

### CI: building and testing the installers

`.github/workflows/build-dist.yml` is started by hand: Actions tab, **Build dist**, **Run workflow**, with three inputs:

| Input | Values | Default |
|---|---|---|
| `ref` | branch, tag or commit to build | `main` |
| `installers` | `both` (embedded and wheels), `combined-embedded`, `wheels` | `both` |
| `wheels_python` | `3.11`, `3.12`, `3.13`, `all` (only for the wheels zips) | `3.11` |

| Job | What it does | Artifacts |
|---|---|---|
| `build-embedded` | `create_export.py --windows` on `windows-2022` (builds, install-tests and smoke-tests the Windows half) | `lattice-diamond-mcp-embedded-windows-x86_64` |
| `verify-embedded` | installs it on a fresh Windows runner with installer Python 3.8, checks that `python.exe` is the only program, has Windows validate its signature, imports pywin32 and the server, speaks MCP, uninstalls | none |
| `build-linux-embedded` | `create_export.py --linux` on `ubuntu-22.04` | `lattice-diamond-mcp-embedded-linux-x86_64` |
| `verify-linux-embedded` | installs it on Ubuntu 22.04 (installer Python 3.8) and 24.04 (3.13), checks that `bin/python3.12` is the only program and that there are no links, checks that no omitted package is imported, speaks MCP, uninstalls | none |
| `combine-embedded` | `create_export.py --both` with the two halves | `lattice-diamond-mcp-combined-embedded` (zip, `.sha256`, SBOM, `.buildinfo`) |
| `verify-combined-embedded` | installs the combined zip on Ubuntu 22.04, Ubuntu 24.04 and Windows; `install.py` must pick the right folder and check its manifest; speaks MCP; uninstalls | none |
| `plan`, `build-wheels`, `verify-wheels`, `combine-wheels`, `verify-combined-wheels` | the wheels zips: one per system and Python version, each installed on a fresh runner, then combined and installed again | `lattice-diamond-mcp-combined-wheels` |

The combine jobs run only after every per-OS zip of their kind has built and been installed on a fresh runner. Two things
must be in the ref you build: `.github/workflows/build-dist.yml` (a `workflow_dispatch` workflow must be on the default
branch to be started) and `uv.lock`. The combined-embedded run passed on GitHub's Windows and Ubuntu runners.

### What the installer does on the target machine

`install.py` reads `BUILDINFO` and refuses a wrong operating system or a too-old glibc. In a combined
zip it first chooses the build: the folder for this operating system and, for wheels, the one whose
Python it can find (the running interpreter's version first, then newer to older; `--python` picks the
folder for that interpreter's version). It prints which folder it chose.

- **Embedded zip:** copies the `python` folder into `--install-dir` (default
  `%LOCALAPPDATA%\lattice-diamond-mcp\python` on Windows, `~/.local/share/lattice-diamond-mcp/python` on Linux)
  through a staging folder, so a failed copy never leaves a half-installed interpreter; restores the execute bit of
  `bin/python3.12` on Linux (some unzip tools drop it); runs `<interpreter> -I -m diamond_mcp --help`; and registers
  that command. A zip of the retired standalone kind is refused with a pointer to the embedded package;
  `--uninstall` still removes an earlier standalone installation (`~/diamond-mcp`) and its registration.
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
leaves both in place. The check can miss a holder that appears later, and Windows also refuses to move the freshly
copied folder into place while anything has a file in it open (antivirus and file indexers do, for a moment, even when no
server runs). That last step is therefore retried for about 20 seconds, then done by copying the files instead of renaming
the folder; if a process is found running from the folder at that point the same question is asked again and the copy
redone, and if none is found the error says so and points at antivirus instead of blaming the AI client. In that case it also asks
Windows' Restart Manager (the service behind "this file is in use by ...", through `ctypes`) which programs have files open
in the folder and names them, for example `MsMpEng.exe` for Defender; they are only named, never offered for stopping, and
if the query fails the error simply omits them. A folder the installer did not create is never checked or touched. The mechanism is
the one in `scripts/install.py` of cortex-bridge (`--if-locked`).

Both mark the folder they create with `.diamond-mcp-installer` and never reuse, replace or
delete a folder that lacks it (an existing empty folder is accepted). Registration is
`claude mcp add --scope user lattice-diamond-mcp -- <program>`; it will not overwrite a registration of the
same name that points elsewhere. Earlier versions registered the name `diamond`; the installer removes
such a registration if it points at this program (tool names change from `mcp__diamond__*` to
`mcp__lattice-diamond-mcp__*`). `--uninstall` reverses it with the same protections, and finds
the right kind of installation from `BUILDINFO`. Note the argument order: `claude mcp add` treats
`-e` as taking several values, so the server name must come before it.

### Tests for the installers

`tests/test_installer.py` covers the wheels route and the shared installer logic; `tests/test_embedded.py` and
`tests/test_linux_embedded.py` the embedded builds and their install (signatures, OpenPGP, trimming, import tables, the
`._pth` file, the Microsoft notice, the Linux archive, the glibc gate, layouts, the installer on both layouts, the smoke
test against the real server and against programs that misbehave); `tests/test_create_export.py`, `tests/test_combine.py`
and `tests/test_packaging.py` the entry point, the combining, the manifests and what spans them. One more test builds and
installs the real wheels bundle; it needs network access and is skipped unless `DIAMOND_TEST_INSTALLER=1`.

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
   `python scripts/create_export.py --both --ref <tag>` (and `python scripts/build_installer.py` on each
   supported system, then `scripts/combine_installers.py --kind wheels`, for the wheels package).
4. Check for newer pinned actions and dependencies (see above), add the approved `LICENSE` text (see below), and review `THIRD_PARTY_LICENSES.txt` and any
   build warning about packages without a licence file.
5. Update the revision history in the user guide.

### Things to know

- **The dependency set is dominated by `mcp`.** Its transitive dependencies include
  `cryptography` and `cffi` (through `pyjwt[crypto]`), which this server never loads, and
  `pydantic-core` and `rpds-py`, which it does. They make the wheels bundle platform specific,
  and `cryptography` raises its glibc floor to 2.28. The embedded packages do not contain the ones
  that are not loaded, but `uv.lock` still pins them, so they still need tracking for security
  updates until `mcp` stops requiring them.
- **Verified:** the combined-embedded package passed the Build dist workflow on GitHub's Windows and Ubuntu
  runners (install, signature validation on Windows, pywin32 import, MCP handshake, uninstall). The
  Linux half was also built, installed and run on a Red Hat 8 host. **Not verified:** Red Hat 7 for the
  embedded package (the glibc gate says it needs at most 2.17), any non-x86-64 machine.
- **`LICENSE` is a byte-for-byte copy of cortex-bridge's AI Solutions Software License Agreement**
  (its own notice allows free distribution of the text if the copyright line is kept, and
  forbids changing the terms), and both zips contain it. A test checks that it is the unmodified
  text, and the embedded `BUILDINFO` records its SHA-256. Whether it is the approved text for
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
