# Automating Lattice Diamond with the Diamond MCP Server User Guide

## Copyright

Copyright(c) 2026 Lattice Semiconductor Corporation. All rights reserved. DO NOT DISTRIBUTE.
Subject to the terms of the AI Solutions Software License Agreement.

## Disclaimers

Lattice makes no warranty, representation, or guarantee regarding the accuracy of
information in this document or the suitability of its products. Lattice Diamond itself
remains subject to its own license terms. An AI assistant can make mistakes: review the
results of any flow, in particular timing and utilisation figures, before relying on them.

## Contents

- [Overview](#overview)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configure Diamond's location and license](#configure-diamonds-location-and-license)
- [Register the server with your AI client](#register-the-server-with-your-ai-client)
- [Verify](#verify)
- [Using the server](#using-the-server)
- [Tools reference](#tools-reference)
- [Behaviour you should know about](#behaviour-you-should-know-about)
- [Troubleshooting](#troubleshooting)
- [Configuration reference](#configuration-reference)
- [Tcl commands used](#tcl-commands-used)
- [Revision history](#revision-history)

## Overview

The Diamond MCP server lets an AI assistant drive Lattice Diamond. It starts one Diamond
Tcl console and keeps it running, so project state is kept between requests and the
console start-up time is paid once.

With it, an assistant can:

- open or create a Diamond project (`.ldf`) and inspect its device, sources and strategy;
- change the target device, synthesis tool, sources and strategy options;
- run Synthesis, Translate, Map, Place and Route, timing analysis, and bitstream
  generation, individually or as one end-to-end compile;
- read back timing, resource utilisation and the full report files;
- run any other Diamond Tcl command through an escape hatch.

The server checks a project before the flow starts. In particular it detects source
files that are missing or that differ from the project's reference only by letter case,
which is a common problem when a project written on Windows is compiled on Linux.

## Requirements

- Python 3.11 or later on the machine that runs the server.
- Lattice Diamond installed on the same machine, with a valid license.
  Diamond 3.14.0.75.0 on Linux is the validated combination. Windows is supported in the
  code (it uses `pnmainc.exe`) but has not been tested.
- An MCP-capable AI client, for example Claude Code or Claude Desktop.

The server and Diamond run on the same host. Remote execution (SSH or LSF) is not
supported in this release.

## Installation

Install the wheel supplied with the release:

```bash
python3.11 -m venv ~/diamond-mcp-venv
~/diamond-mcp-venv/bin/pip install lattice_diamond_mcp-0.1.0-py3-none-any.whl
```

On Windows use `python -m venv %USERPROFILE%\diamond-mcp-venv` and
`%USERPROFILE%\diamond-mcp-venv\Scripts\pip.exe`.

This installs the `diamond-mcp` command. Check it:

```bash
~/diamond-mcp-venv/bin/diamond-mcp --help
```

Developers who work from the source repository should read `README.md` instead.

## Configure Diamond's location and license

### Diamond executable (first match wins)

1. The `DIAMOND_EXE` environment variable.
2. `diamond_exe` or `diamond_install_dir` in the config file.
3. The newest installation found automatically.

The automatic search looks in `/tools/dist/lattice/DIAMOND`, `/usr/local/diamond`,
`/opt/lattice/diamond`, `/opt/lscc/diamond` and `/usr/local/lscc/diamond` on Linux, and
`C:\lscc\diamond` on Windows. Add more locations with `DIAMOND_SEARCH_ROOTS` (a list of
directories separated by `:` on Linux or `;` on Windows). Temporary, test and backup
directories are ignored.

On Linux the executable is `diamondc`, found under `bin/lin64` in the installation.
Do not point the server at the `diamond` GUI launcher or at a site wrapper that opens a
GUI window: those need a display and cannot be driven by a program.

### License (first match wins)

1. The `LM_LICENSE_FILE` environment variable.
2. `lm_license_file` in the config file.

Diamond also reads the `license/license.dat` file inside its own installation. If your
site distributes licenses through a wrapper script rather than a fixed value, ask your
administrator for the `port@host` value to use.

### Setting it up through your assistant

Ask the assistant to run these tools:

1. `detect_diamond_installations` lists what it can find, newest first.
2. `configure_diamond` with the chosen `diamond_exe` (and `lm_license_file` if needed)
   saves the choice to the config file.
3. `check_environment` confirms that Diamond can be started.

### Setting it up through the config file

Create `diamond-mcp.json` in the working directory of the server, or
`~/.config/diamond-mcp/config.json` (`%APPDATA%\diamond-mcp\config.json` on Windows), or
point `DIAMOND_MCP_CONFIG` at any file:

```json
{
    "diamond_exe": "/opt/lattice/diamond/3.14.0.75.0/bin/lin64/diamondc",
    "lm_license_file": "1234@license-server",
    "timeout_seconds": 3600
}
```

Every field is optional. Do not commit a license value to a shared repository.

## Register the server with your AI client

### Claude Code

```bash
claude mcp add --scope user diamond -- ~/diamond-mcp-venv/bin/diamond-mcp
```

To pass settings at registration time, add environment variables with `-e`:

```bash
claude mcp add --scope user diamond \
    -e DIAMOND_EXE=/opt/lattice/diamond/3.14.0.75.0/bin/lin64/diamondc \
    -e LM_LICENSE_FILE=1234@license-server \
    -- ~/diamond-mcp-venv/bin/diamond-mcp
```

`--scope user` makes the server available in every project and stores the entry outside
any repository. Use `--scope project` to write a shared `.mcp.json` file instead, and
then do not put a license value in it.

To avoid a permission prompt on every call, allow the server's tools in
`~/.claude/settings.json`:

```json
{
    "permissions": {
        "allow": ["mcp__diamond__*"]
    }
}
```

Merge this into the existing `permissions` block rather than replacing the file. The
`run_tcl` tool can run any Diamond command, so allow the whole server only if you accept
that.

### Other clients

Any MCP client that can launch a stdio server works. A typical entry is:

```json
{
    "mcpServers": {
        "diamond": {
            "command": "/home/you/diamond-mcp-venv/bin/diamond-mcp",
            "env": {
                "DIAMOND_EXE": "/opt/lattice/diamond/3.14.0.75.0/bin/lin64/diamondc"
            }
        }
    }
}
```

## Verify

1. Restart the client so it loads the new server.
2. In Claude Code, run `/mcp` and confirm that `diamond` shows as connected.
3. Ask: "Run check_environment for the Diamond server." The reply should say the
   environment is ready.
4. Ask: "Open /path/to/project.ldf and show me the device." A successful reply names the
   device and lists any `preflight` problems.

## Using the server

The Diamond console starts automatically when the first tool needs it.

### Typical workflow

1. `open_project` (or `create_project`, then `add_sources`).
2. `compile_project` for the whole flow, or `run_synthesis`, `run_map`, `run_par` one at a
   time to inspect each stage.
3. `get_timing_summary` and `get_resource_usage` for the results.
4. `generate_bitstream` if a programming file is needed and `compile_project` was run with
   `bitstream=false`.

### Prompt examples

- "Compile /designs/uart/uart.ldf and tell me whether timing is met."
- "Open /designs/uart/uart.ldf, retarget it to LCMXO3LF-6900C-5BG256C, rebuild, and compare
  the LUT count with before."
- "What is the worst setup slack in the last PAR run, and which constraint fails?"
- "The synthesis step fails on this project. Find out why."
- "Turn on register retiming in the Map strategy, rerun Map and PAR, and report the change
  in timing errors."

### What a result looks like

Every tool returns a structure with these fields:

- `success`: true or false.
- `output`: the console output and Tcl result, shortened if very long.
- `warnings`: lines of the output that look like errors or warnings.
- `elapsed_ms` and `timestamp`.
- `hints`: present on some failures, with suggestions for known problems.
- `session_note`: present when the Diamond console was restarted (see below).

`compile_project` adds `stages` (success and time for each stage), `timing`, `resources`
and `bitstream_files`. When a stage fails it adds `failed_stage`.

## Tools reference

Optional parameters are shown in square brackets.

### Session management

| Tool | Parameters | Purpose |
|---|---|---|
| `start_diamond` | [exe_path], [timeout] | Start the console. Optional: tools start it automatically. |
| `stop_diamond` | none | Stop the console. An open project is closed without saving. |
| `get_session_status` | [log_lines] | Running state, Diamond version, open project, command counts, why the last console ended, and the console log tail. |
| `detect_diamond_installations` | none | List installations found on this machine. |
| `get_diamond_config` | none | Show the resolved executable and where it came from. Never shows the license value. |
| `configure_diamond` | [diamond_exe], [lm_license_file], [timeout_seconds] | Save settings to the config file. Applies at the next console start. |
| `check_environment` | none | Check that Diamond can be started. |

### Project

| Tool | Parameters | Purpose |
|---|---|---|
| `open_project` | project_path, [implementation] | Open an `.ldf`. Closes any open project first. Reports missing sources under `preflight`. |
| `close_project` | none | Close the open project without saving. |
| `create_project` | name, directory, device, [implementation], [synthesis_tool], [lpf] | Create a project and leave it open. `synthesis_tool` is `lse` (default), `synplify` or `precision`. |
| `save_project` | none | Save the open project. |
| `get_project_info` | [project_path] | Describe a project from its file: device, implementations, top module, sources, strategies, existing reports, missing files. Works without Diamond running. |
| `add_sources` | files, [implementation], [file_format], [include_paths], [library], [synthesis_only] | Add source files. Relative paths are relative to the project directory. |
| `remove_sources` | [files], [implementation], [remove_all] | Remove sources. |
| `set_target_device` | part | Change the device. Use a full part name such as `LCMXO3LF-6900C-5BG256C`. |
| `set_synthesis_tool` | tool, [implementation] | Set `lse`, `synplify` or `precision`. |
| `get_strategy_option` | name, [strategy] | Read one strategy option, for example `PROP_MAP_RegRetiming`. |
| `set_strategy_option` | name, value, [strategy] | Set one strategy option and return the new value. |

### Flow

| Tool | Parameters | Purpose |
|---|---|---|
| `compile_project` | project_path, [implementation], [force], [bitstream], [timeout] | Open, then Synthesis, Translate, Map, PAR and (by default) Bitgen, stopping at the first failure. Returns timing and resource summaries. Leaves the project open. |
| `run_flow` | milestone, [task], [implementation], [force], [timeout] | Run any milestone or task, for example `PAR` with task `PARTrace`. |
| `run_synthesis` | [implementation], [force] | Synthesis (Synplify, LSE or Precision per the project). |
| `run_translate` | [implementation], [force] | Translate. |
| `run_map` | [implementation], [force] | Map. |
| `run_par` | [implementation], [force] | Place and route. |
| `run_timing_analysis` | [implementation], [force] | Run PAR Trace and return the parsed timing summary. |
| `run_export` | [implementation], [force] | Run the Export milestone. With default strategy settings this writes no bitstream. |
| `generate_bitstream` | [implementation], [force], [timeout] | Generate the `.bit` file and verify that it was written. |

`force` is `none` (default), `one` (rerun this stage) or `all` (rerun all stages).
Stages that are already up to date are not rerun unless forced.

`timeout` is in seconds. The default is one hour (`timeout_seconds` in the config).

### Reports

| Tool | Parameters | Purpose |
|---|---|---|
| `list_reports` | [project_path], [implementation] | List the report and output files that exist. |
| `get_timing_summary` | [project_path], [implementation] | Setup and hold errors and scores, worst slack, and each constraint with its target and achieved value. |
| `get_resource_usage` | [project_path], [implementation] | Registers, SLICEs, LUT4s, I/O, block RAM and other resources, each with used, total and percent. |
| `read_report` | file_path, [offset], [max_chars], [tail] | Read a report or log file in pieces (up to 20000 characters per call). |

`read_report` reads only report and log file types (`.par`, `.twr`, `.mrp`, `.srr`,
`.srf`, `.log`, `.rpt`, `.pad`, `.drc`, `.txt`, `.htm`, `.html`, `.prf`, `.lpf`, `.sdc`,
`.tcl`, `.ldf`).

Timing units: PAR scores are in nanoseconds (`*_ns` keys) and timing-report scores are
in picoseconds (`*_ps` keys).

### Escape hatch

| Tool | Parameters | Purpose |
|---|---|---|
| `run_tcl` | command, [timeout] | Run any Tcl in the live console. Multi-line scripts work. Prefer the dedicated tools. |

## Behaviour you should know about

- **Closing a project restarts the console.** Diamond's own close command intermittently
  crashes it, so `close_project`, and `open_project` or `create_project` when a project is
  already open, restart the console instead. This takes a few seconds, discards unsaved
  changes, and clears any Tcl variables you set with `run_tcl`. Call `save_project`
  first if you changed the project.
- **Only one project is open at a time.** The console is a single session shared by all
  tools.
- **Calls are serialised.** If a long flow is running, other tool calls wait for it.
- **A timeout restarts the console.** When a command exceeds its timeout the console is
  terminated, because its state is unknown. The next call starts a fresh one and the
  response carries a `session_note`. Reopen the project afterwards.
- **A crash is detected and reported.** If Diamond ends unexpectedly, the failed call
  reports how (for example `terminated by signal SIGSEGV`) and the next call restarts it.
- **`run_export` does not write a bitstream** unless the strategy enables it. Use
  `generate_bitstream`, which also confirms the file exists.
- **Flows run on the host that runs the server.** There is no remote execution or job
  queue. A long flow holds its tool call open until it finishes or times out.
- **Success means Diamond returned success.** For flows that promise a file (a bitstream)
  the server checks the file. Always read `warnings` as well, because Diamond can succeed
  with warnings worth reviewing.

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| `check_environment` says no executable found | Diamond is not in a searched location | Run `detect_diamond_installations`, then `configure_diamond`, or set `DIAMOND_EXE` or `DIAMOND_SEARCH_ROOTS`. |
| The console does not start and the output mentions a display | A GUI launcher or site wrapper was configured | Point `diamond_exe` at `diamondc` (Linux) or `pnmainc.exe` (Windows), not the `diamond` launcher. |
| Output mentions a license error | No license value reached Diamond | Set `LM_LICENSE_FILE` or `lm_license_file`, and confirm with your administrator. |
| `open_project` fails with "Error in reading project file" | The project's device is not supported by this Diamond version, or the file is from a newer release | Compare the `preflight.device` value with the version in `get_session_status`. Use a Diamond version that supports the device, or edit the device in the `.ldf` to a valid part name. |
| `preflight.missing_files` lists files with a "different letter case" hint | The project was written on Windows and Linux file names are case sensitive | Rename the files to match the project, or fix the references. The flow will fail until you do. |
| Synthesis fails with "This software has expired" | The Synplify bundled with that Diamond installation has expired | Use a newer installation, or switch the project to the Lattice Synthesis Engine with `set_synthesis_tool` (`lse`). Re-run the flow with `force` set to `one`. |
| A stage fails with only `Running milestone "..." failed` | The cause is in the console output | Read `output` and `warnings` in the response, and the stage's log with `list_reports` and `read_report` (`.srr` for synthesis, `.par`, `.twr`). |
| `NoSuchStage is not a valid milestone name` | Wrong milestone | Use Synthesis, Translate, Map, PAR or Export. |
| The response has a `session_note` about a restart | The console ended or timed out | Reopen the project with `open_project`. |
| `generate_bitstream` reports no `.bit` file | The device or strategy produces no bitstream, or the file is up to date | Retry with `force` set to `one` and check the Export logs. |
| Timing summary shows errors but PAR reported success | PAR success means the design routed, not that timing was met | Read `get_timing_summary`: unmet constraints are listed with target and achieved values. |
| A flow is slow | The design is large, or the host is busy | Raise `timeout` for the call, or run the stages one at a time. |

If a problem persists, call `get_session_status` with a large `log_lines` value and
include the `console_log_tail` and `last_exit` in your report.

## Configuration reference

### Environment variables

| Variable | Purpose |
|---|---|
| `DIAMOND_EXE` | Path to the Diamond console executable. |
| `LM_LICENSE_FILE` | License value passed to Diamond. |
| `DIAMOND_MCP_TIMEOUT` | Default command timeout in seconds. |
| `DIAMOND_MCP_CONFIG` | Path to a config file. |
| `DIAMOND_SEARCH_ROOTS` | Extra directories to search for installations. |

### Config file fields

| Field | Purpose |
|---|---|
| `diamond_exe` | Full path to the console executable. |
| `diamond_install_dir` | Installation directory; the executable is located inside it. |
| `lm_license_file` | License value. |
| `timeout_seconds` | Default command timeout. |

Config file search order: `DIAMOND_MCP_CONFIG`, then `diamond-mcp.json` in the working
directory, then `~/.config/diamond-mcp/config.json` (`%APPDATA%\diamond-mcp\config.json`
on Windows).

### Command-line options

| Option | Purpose |
|---|---|
| `--diamond-exe PATH` | Override the executable. |
| `--timeout SECONDS` | Override the default timeout. |
| `--transport {stdio,sse,streamable-http}` | MCP transport. Default: stdio. |
| `--log-level LEVEL` | Server log level. Default: WARNING. |

## Tcl commands used

All verified against Diamond 3.14.0.75.0. The commands were also run on 3.10.0.82.0.

| Purpose | Command |
|---|---|
| Open a project | `prj_project open <file>` |
| Create a project | `prj_project new -name <n> -dev <part> -impl <i> -synthesis <tool> [-lpf <f>]` |
| Save a project | `prj_project save` |
| Activate an implementation | `prj_impl active <name>` |
| Query or set the device | `prj_dev`, `prj_dev set -part <part>` |
| Query or set the synthesis tool | `prj_syn`, `prj_syn set <tool> [-impl <i>]` |
| Add or remove sources | `prj_src add ...`, `prj_src remove ...` |
| Read or write a strategy option | `prj_strgy get_value <name>`, `prj_strgy set_value <name>=<value>` |
| Run a milestone or task | `prj_run <milestone> [-impl <i>] [-task <t>] [-forceOne\|-forceAll]` |

The server does not use `prj_project close`; see "Behaviour you should know about".

## Licensing and compliance

- This server carries the notice "Copyright(c) 2026 Lattice Semiconductor Corporation. All
  rights reserved. DO NOT DISTRIBUTE. Subject to the terms of the AI Solutions Software
  License Agreement". Distribute it only as that agreement allows.
- Running flows requires a valid Lattice Diamond license for the device and tools used.
- The server depends on the open-source `mcp` Python package. Follow your organisation's
  third-party software review process before distributing a build that includes it. No
  internal policy number is cited here; consult your compliance team for the applicable
  reference.
- Project files and reports you pass through an AI assistant leave your machine if the
  assistant is a cloud service. Apply your organisation's rules for sharing design data.

## Revision history

| Version | Notes |
|---|---|
| 0.1.0 | First release. Local execution only. Validated on Diamond 3.14.0.75.0 (Linux). |
