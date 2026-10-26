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
| D-10 | Workflow actions were pinned by tag. | code review | Fixed: pinned to commit SHAs, the version named in a comment; `.github/dependabot.yml` proposes updates for the actions and for `uv.lock`. | I-5 |
| D-11 | No static analysis, advisory check, secrets scan or dynamic test in CI. | n/a | Added: `security-scan.yml` runs the unit tests and `run_scans.py`. | S-2, S-3, S-4 |

## Still open, or not covered

- **`run_tcl` runs arbitrary Tcl, by design** (bridge R-4), and Tcl can run programs and read files. D-3 and D-4
  therefore limit what the other tools can be used for; they do not stop a client that can call `run_tcl`. Containing
  that is the job of the sandbox in the user guide's deployment section.
- **The installer zips are not signed.** A checksum beside the zip does not help against someone who can replace
  both (bridge I-1 1b and 1c). Code signing is also what Windows Defender and SmartScreen respond to.
- **No SBOM for the wheels zip and no provenance attestation** for either zip (the standalone zip has an SBOM).
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
