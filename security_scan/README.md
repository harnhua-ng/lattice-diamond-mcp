# Security scans

Scans of lattice-diamond-mcp, modelled on the cortex-bridge scan set of 2026-10-23 (SAST and DAST
README, Coverity README, and the remediation registers). Run them all with one command:

    pip install bandit                       # once; the CI job pins bandit==1.9.4
    python security_scan/run_scans.py        # writes security_scan/out/ (ignored by git)

Needs Python 3.11 or later, the project's dependencies importable, and network access for the OSV
step (`--skip-osv` without it, `--skip-dast` to leave out the dynamic probes). `tclsh` is needed by
the Tcl quoting check and is skipped, with a note in the result, when it is missing. The same
command runs in CI: `.github/workflows/security-scan.yml`, on every push to main, every pull request
and weekly.

Nothing here has been triaged by an SDLC security team, and none of it replaces one.

## What each scan does

| File | What it does |
|---|---|
| `run_scans.py` | Runs the steps below, writes one JSON per step plus `summary.json`, exits non-zero if a step finds something or cannot run. |
| (Bandit, run by `run_scans.py`) | Static analysis of `src`, `scripts` and `security_scan`. Fails on any Medium or High finding that is not suppressed. A suppression sits next to its reason in the source; a second run with `--ignore-nosec` records what the suppressions hide. Low findings are informational (almost all are "subprocess is used"). |
| `secrets_scan.py` | Nine regular expressions over the files git tracks plus new unignored ones: private keys, AWS, GitHub, Slack, Google and API-key formats, JWTs, URLs with credentials, and key/password/token string assignments. Does not read git history. |
| `osv_check.py` | Sends every package pinned in `uv.lock` to OSV.dev. Runtime packages (reachable from the project's own dependencies, so shipped in the installers) fail the check; build-only packages such as PyInstaller are listed and fail only with `--strict`. An unreachable OSV is an error, never a clean result. |
| `dast_parsers.py` | Seeded fuzzing (300 mutated inputs per target, plus 1 MB runs, 100,000-deep nesting and crafted worst cases) of the report parsers, the project (.ldf) reader and the config loader. A call over 2 s is "slow", over 20 s a "hang" (POSIX), any unexpected exception type a finding. With `tclsh`, also checks that `tcl_quote` always yields exactly one Tcl word equal to the original, for 320 awkward strings. |
| `dast_stdio.py` | Starts the real server as an AI client would and speaks MCP to it over stdio: 12 malformed, mistyped and oversized frames (the server must keep answering), file-read confinement, program-path restrictions, configuration handling, DTD refusal, and a check that `--transport sse` and `streamable-http` start no listener. Runs in a scratch folder with its own HOME. `--src` points it at older code. |

`tests/test_security_scan.py` tests the scanners themselves: each secret format is detected, a
vulnerable package fails the OSV check, an unreachable OSV is an error, and a quoter that does not
quote is caught.

## Results, 2026-10-26

Run on Linux (kernel 4.18, el8), Python 3.11.7, mcp 1.30.0. "Before" is commit 9de65fe exported
with `git archive`; "after" is the working tree with the changes described below. The JSON is in
`results/`; local paths in it are replaced by `<repo>` and `<scratch>`.

| Scan | Before | After |
|---|---|---|
| Bandit, `src` and `scripts` (`bandit_*.json`) | 0 High, 1 Medium (B314 XML parse), 16 Low | 0 High, 0 Medium, 15 Low. With suppressions ignored the Medium returns: it is suppressed with its reason, because the file is refused if it declares a DTD or entity. |
| Dynamic probes over stdio (`dast_stdio_*.json`) | 24 probes: 14 passed, **10 failed** | 24 probes: **24 passed** |
| Parser and Tcl-quoting fuzzing (`dast_parsers_*.json`) | **4 findings** (D-5, D-7 twice, D-8) | 0 findings |
| Secrets scan (`secrets_scan.json`) | not run | 0 hits in 51 files |
| OSV (`osv_check.json`) | not run | 0 advisories: 27 runtime and 18 build-only packages |
| Unit tests | 252 passed, 6 skipped | 357 passed, 6 skipped |
| Tampered wheel in the wheels bundle (`wheels_hash_check.txt`) | no hash check existed (not tested) | refused: pip reports the hash mismatch and install.py exits 1 |

The probes that failed before the changes were chosen to fail on the old code and were run against
it to confirm that: each failure below is the observed behaviour of commit 9de65fe.

## Re-run, 2026-10-29

All five steps run again with `run_scans.py` on commit 59a32ae plus the licence-notice change described below, which adds, since the results above: the
running-server prompt and its process killing in `scripts/install.py`, the wheels installer without a
launcher program (`python -I -m diamond_mcp`), the environment in the per-user data folder, and the
registration name `lattice-diamond-mcp`. Same machine and Python as above (Linux, Python 3.11.7, mcp 1.30.0).
The JSON is in `results/rerun_2026-10-29/`, with local paths replaced as before.

| Scan | Result |
|---|---|
| Bandit, `src`, `scripts` and `security_scan` | 0 High, 0 Medium, 35 Low (informational, almost all "subprocess is used"). The Low count is higher than the 15 above because `security_scan` is now included, the installer gained process-handling code, and `fetch_python_notices.py` runs `zstd` and downloads over https. With suppressions ignored, four Medium findings appear; each has its reason beside it: B314 `project.py:73` (the file is refused if it declares a DTD or entity), B103 `dast_stdio.py:190` (a deliberately executable test file in a scratch folder), B310 `dast_stdio.py:360` and `osv_check.py:66` (`urlopen` of a fixed `http://127.0.0.1` or `https://api.osv.dev` URL, never a user-supplied scheme). |
| Secrets scan | 0 hits in 88 files (the vendored licence notices included) |
| OSV | 0 advisories: 27 runtime and 18 build-only packages (the `uv.lock` pins are unchanged) |
| Parser and Tcl-quoting fuzzing | 0 findings |
| Dynamic probes over stdio | 24 probes, 24 passed, 0 skipped, 0 failed |
| Unit tests | 418 passed, 6 skipped |

New code that was not in the earlier scan, and what that scan says about it:

- **Killing a running server (`install.py`)**: processes are stopped only after the user answers `a` or passes
  `--if-locked kill` (or `DIAMOND_MCP_IF_LOCKED=kill`). With nobody to ask, the installer exits with status 1 and changes
  nothing. Only processes whose program lies inside the installer's own marked folder are listed. Every helper
  subprocess runs with `stdin` closed, and none uses a shell. Bandit reports the subprocess calls as Low (B404, B603,
  B607 family) and nothing else. The Linux path was exercised against a real process; the Windows path (`taskkill`,
  detection by locked files) is covered only by unit tests with stand-ins.
- **Removing the launcher program**: the wheels zip now installs no program of ours, and the build and the CI
  verify step fail if `bin/diamond-mcp` or `Scripts/diamond-mcp.exe` appears. The registered command is the
  environment's Python with `-I`, which ignores `PYTHONPATH`, the working directory and user site-packages, so a stray
  `mcp.py` in the folder the client starts from is not imported.
- **Environment folder**: the default moved into the per-user data folder (`%LOCALAPPDATA%` or `$XDG_DATA_HOME`),
  which the user owns and which is not roaming. The installer still refuses to reuse or delete a folder that lacks its
  marker file.
- **Old registration name**: the installer removes a registration named `diamond` only if it points at this program;
  one that points elsewhere, or one the user chose with `--name diamond`, is left alone.

## Findings and what was done

Numbering is local to this scan. The last column names the corresponding cortex-bridge item.

| Ref | Finding | Evidence | Status | Bridge item |
|---|---|---|---|---|
| D-1 | `--transport sse` and `streamable-http` opened an unauthenticated HTTP server on 127.0.0.1:8000 (the SDK's port; `FASTMCP_PORT` was ignored) that answered HTTP 200 to a request with no credentials, and every tool, including `run_tcl`, sat behind it. | `dast_stdio_before.json`, network probes | Fixed: the options were removed; the server speaks stdio only. A test, a probe and the user guide say so. | F-4 |
| D-2 | A `diamond-mcp.json` in the working directory was read before the user's own config. It could name the program to run, set a licence server, and switch the audit log off (`"audit_log": "off"`). | probe "diamond-mcp.json in the working directory": `diamond_exe` was the planted program, `audit_log` was null | Fixed: the working directory is not searched. Config comes from `DIAMOND_MCP_CONFIG` or the account's own file. | Radiant R-1 |
| D-3 | `start_diamond(exe_path)` ran any program the client named, and `configure_diamond(diamond_exe)` saved any existing file as the program for every later session. | probes: the planted script ran (marker file written) and was saved to the config | Fixed: the file must be named `diamondc` (`pnmainc.exe` on Windows). `DIAMOND_EXE`, `--diamond-exe` and the config file are operator input and are not restricted. | R-3, F-1 family |
| D-4 | `read_report` read any file with one of 17 suffixes anywhere the account could read, including through a link, and loaded the whole file before slicing it. | probes: a `.log` outside the project and through a symlink were returned | Fixed: only inside a project opened or queried in the session (its folder and implementation folders; an implementation folder outside the project folder is not accepted); links are resolved first; files over 256 MB are refused; the file is read in chunks. | F-3, R-6 |
| D-5 | A config file nested thousands of levels deep raised `RecursionError`, which the loader did not catch. | `dast_parsers_before.json`; probe: `get_diamond_config failed: maximum recursion depth exceeded` | Fixed: caught, treated as an empty config. | F-5 |
| D-6 | Project files were parsed with `xml.etree` with no check for a DTD or entity declaration (Bandit B314). | `bandit_before.json`; probe: an entity-bearing file was parsed (the error came later, from the missing implementation) | Fixed: refused before parsing if it declares either. | R-8 |
| D-7 | Report parsing was quadratic on crafted input: `parse_twr` did not finish in 10 s on a 1 MB run of digits, on 40,000 repeats of "Constraints cover", and on a repeated table header; `parse_par` took about 8 s on a line of 100,000 spaces after `PAR_SUMMARY::`. Python's regular-expression engine holds the interpreter lock while it matches, so one such report stalls the whole server, not just that call. Not in the bridge's findings; found by this fuzzer. | `dast_parsers_before.json` | Fixed: three regular expressions replaced or bounded. Seven timing tests: on the old code six took 8 s (`parse_par`) or over 15 s (`parse_twr`), and the seventh (`parse_mrp`) was already fast and is a guard; on the new code all take under 0.2 s. A differential run of old against new parsers on 12,000 mutated reports differed only where a mutation put a line break between a `PAR_SUMMARY::` name and its `=`; real reports do not. | R-16 family |
| D-8 | `tcl_quote` did not escape a carriage return. Tcl reads a bare CR in a script file as a line break, so a backslash followed by CR became a line continuation and split one argument into two (argument injection into a Diamond command). Needs a value containing both a backslash or brace and a CR. Not in the bridge's findings; found by the quoting check. | `dast_parsers_before.json` | Fixed: CR is always escaped; seven tests that run a real Tcl on a script file (they fail on the old code); the fuzzer finds 0 of 3,020 strings misquoted. | R-5 family |
| D-9 | The wheels installer took dependency versions from whatever the package index offered and installed without checking hashes. | code review of `build_installer.py` | Fixed: versions come from `uv.lock`; every downloaded wheel must have the SHA-256 recorded there or the build stops; the zip carries `requirements.txt` with every hash and installs with `--require-hashes`. Verified by a real build, install and uninstall, and by a tampered wheel being refused. The standalone build already used `uv run --frozen`. | I-1 (1a) |
| D-10 | Workflow actions were pinned by tag. | code review | Fixed: pinned to commit SHAs, the version named in a comment; nothing updates them automatically, so the README describes how to update a pin and says that stale ones are not flagged. | I-5 |
| D-11 | No static analysis, advisory check, secrets scan or dynamic test in CI. | n/a | Added: `security-scan.yml` runs the unit tests and `run_scans.py`. | S-2, S-3, S-4 |

## Still open, or not covered

- **`run_tcl` runs arbitrary Tcl, by design** (bridge R-4), and Tcl can run programs and read files. D-3 and D-4
  therefore limit what the other tools can be used for; they do not stop a client that can call `run_tcl`. Containing
  that is the job of the sandbox in the user guide's deployment section.
- **The installer zips are not signed.** A checksum beside the zip does not help against someone who can replace
  both (bridge I-1 1b and 1c). Code signing is also what Windows Defender and SmartScreen respond to.
- **No SBOM for the wheels zip and no provenance attestation** for either zip (the standalone zip has an SBOM).
- **Licence notices for libraries inside the embedded Python (standalone zip): fixed 2026-10-29.** The CPython from
  python-build-standalone has OpenSSL (3.5.9 by its version string), zlib 1.3.2, expat 2.9.0, bzip2, liblzma, mpdecimal,
  libedit, ncurses, libffi, libuuid and SQLite compiled into `libpython3.12.so.1.0` (on Windows, separate DLLs beside
  `python312.dll`), and the zip carried only CPython's own `LICENSE.txt`. The notices are now vendored in
  `scripts/python_notices/` from the project's release 20250529 (the archives' SHA-256 checked against the release's
  `SHA256SUMS`, each file's hash in `SOURCES.txt`), copied into the zip, and listed in `LEGAL_REVIEW.txt` as item 3.
  The build stops if the interpreter is from a different release. Still open: the Windows set was chosen from the
  DLLs in the release archive, not from a Windows build of this program, and Legal has not reviewed any of it.
- **Windows package with python.org's Python (new, 2026-10-29).** `scripts/build_embedded.py` builds a Windows package
  with no executable of ours: python.org's embeddable CPython 3.12.10 (SHA-256 pinned in the script; python.exe
  carries the Python Software Foundation's signature, found in the signature block but **not validated** on Linux) plus the
  locked wheels (each checked against `uv.lock`). Built and layout-checked on Linux, **never run on Windows**. Open:
  six third-party native files are unsigned (`_pydantic_core`, `rpds` and four from pywin32; the list is in NATIVE_FILES.txt in
  the package; the package was trimmed from 59 by keeping only what the server imports, with a static import-table check, a
  test, and a real import on the Windows verify job as evidence), and pywin32's two programs are removed from the package; python.org's Windows binaries carry Microsoft Distributable Code (`vcruntime140*.dll`) under conditions in the LICENSE.txt of
  its zip that require end users to be bound by protecting terms; the package ships a notice for that (wording needs Legal's approval); the zip is verified against python.org's OpenPGP signature (key 7ED10B65...34E5, checked in and
  pinned by fingerprint) as well as a pinned SHA-256, but the fingerprint's provenance (python.org's published key list
  could not be reached when it was chosen) needs one confirmation by a person; the library notices are python.org's own
  `Doc/license.rst` for the release plus the python-build-standalone texts; the interpreter is deliberately 3.12.10, the last 3.12 with a python.org embeddable zip, so it lacks the fixes of the six later
  source-only 3.12 releases (3.12.11 to 3.12.16), which cannot be had as Windows binaries from python.org; a dated move to 3.13/3.14 is
  proposed (python.org already publishes 3.13.18 as source only).
- **Linux package with python-build-standalone's Python (new, 2026-10-30).** `scripts/build_embedded.py --platform linux` builds the Linux
  twin of the Windows package: CPython 3.12.10 from python-build-standalone release 20250529 (SHA-256 pinned from the release's
  `SHA256SUMS`; **no signature is published, so there is no OpenPGP check**) with the locked wheels, no executable of ours and no symbolic
  links. Three ELF files; the highest glibc symbol is 2.17. Built here on Linux and install-tested with the real server over MCP; **never run
  on the GitHub runners**. The interpreter links the system glibc, so "no system dependency" is not true; the floor is glibc 2.17.
- **The wheels zip carries the `cryptography` wheel** (via the `mcp` dependency), which has OpenSSL linked in, and
  `cffi`, which carries libffi. Their licence files are in `THIRD_PARTY_LICENSES`; the standalone program does not
  contain either package. No wheel for `pip`, `setuptools` or an interpreter is in either zip.
- **Linux only.** Every scan ran on Linux with Python 3.11. The Windows legs of the CI workflows have never run
  anywhere; the symlink probe is skipped on Windows. `security-scan.yml` has never run on GitHub.
- **Fuzzing is seeded, not coverage-guided**: 300 mutated inputs per target. A clean result is absence of evidence.
- **The probes ran without a real Diamond.** They cover the server's own handling, not what Diamond does with a command.
- `read_report`'s memory use is not measured; only that a slice of a 30 MB log is correct and fast.
- OSV knows published advisories only. Git history is not scanned for secrets. Coverity, Semgrep, CodeQL and
  OWASP ZAP were not run.

## Behaviour changes to put in the release notes

- `diamond-mcp.json` in the working directory is no longer read; move it to `~/.config/diamond-mcp/config.json`
  (`%APPDATA%\diamond-mcp\config.json` on Windows) or set `DIAMOND_MCP_CONFIG`.
- `--transport` no longer exists. A registration that passed `--transport stdio` must drop it.
- `start_diamond` and `configure_diamond` accept only a file named `diamondc` / `pnmainc.exe`. A site wrapper script
  has to be set through `DIAMOND_EXE` or the config file.
- `read_report` needs the project to have been opened or queried first, and refuses an implementation folder outside
  the project folder.
- A `.ldf` that declares a DTD or entity is refused.
- The wheels installer installs from `requirements.txt` (hashes) instead of `constraints.txt`.
- The server registers as `lattice-diamond-mcp` (it was `diamond`); tool names change from `mcp__diamond__*` to
  `mcp__lattice-diamond-mcp__*`, so saved permission rules need the new prefix.
- The wheels environment is created in `%LOCALAPPDATA%\lattice-diamond-mcp\venv` (`~/.local/share/lattice-diamond-mcp/venv`
  on Linux) instead of `~/diamond-mcp-venv`, and the wheels zip installs no `diamond-mcp` program.
- If a server is running from the folder being replaced, the installer asks before stopping it
  (`--if-locked ask|kill|exit`).
