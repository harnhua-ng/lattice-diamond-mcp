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
- [Understanding the Diamond flow](#understanding-the-diamond-flow)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configure Diamond's location and license](#configure-diamonds-location-and-license)
- [Register the server with your AI client](#register-the-server-with-your-ai-client)
- [Verify](#verify)
- [Using the server](#using-the-server)
- [Tools reference](#tools-reference)
- [Behaviour you should know about](#behaviour-you-should-know-about)
- [Command audit log](#command-audit-log)
- [Deploying in a restricted environment](#deploying-in-a-restricted-environment)
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

## Understanding the Diamond flow

This section is for readers who are new to FPGA design tools. It explains what Diamond
does, what each stage produces, and which parts of that the MCP server can drive.

### What Diamond does

An FPGA is a chip whose logic can be reprogrammed. You describe the logic you want in a
hardware description language (Verilog or VHDL). Diamond turns that description into a
file that configures a specific FPGA part. It does this in stages, and each stage reads
the result of the previous one.

Three kinds of input define a project:

- **Design sources**: your Verilog or VHDL files (and sometimes ready-made netlists).
- **Constraints**: a logic preference file (`.lpf`) that tells Diamond your goals, most
  importantly the clock speeds the design must reach, and which chip pin each signal uses.
  Without timing constraints Diamond has no speed target, so "timing met" means little.
- **Settings**: the target device (a part name such as `LCMXO3LF-6900C-5BG256C`), the
  synthesis tool, and a *strategy* (a named set of tool options).

All three are recorded in the project file (`.ldf`). Diamond keeps its results for each
*implementation* of the project in a folder named after the implementation, next to the
project file.

### The stages

```
  HDL sources      Constraints (.lpf)      Strategy (.sty)
       \                  |                     /
        +----------  Project file (.ldf)  ----+
                          |
                          v
   Synthesis --> Translate --> Map --> Place & Route --> Export
   (netlist)     (database)   (fit to   (placement,      (bitstream,
                               chip)     wiring, timing)  simulation files)
```

| Stage | What it means | Main results | Server tools |
|---|---|---|---|
| Synthesis | Converts your HDL into a netlist of basic logic: gates, registers, memories. Done by Synplify Pro, the Lattice Synthesis Engine (LSE) or Precision, as the project selects. | Netlist, synthesis log (`.srr`, `.srf`) | `run_synthesis` |
| Translate | Merges the netlist with the device library and your constraints into Diamond's internal design database. | `.ngd` | `run_translate` |
| Map | Fits the logic onto the chip's real building blocks: lookup tables (LUT4s), registers, block RAM, I/O. | Utilisation report (`.mrp`) | `run_map`, `get_resource_usage` |
| Place and Route (PAR) | Decides where each block sits on the chip and how wires connect them, then measures the speed that results. | Placed design (`.ncd`), PAR report (`.par`), timing report (`.twr`), pin report (`.pad`) | `run_par`, `get_timing_summary` |
| Timing analysis | Checks every path against your constraints. | `.twr` | `run_timing_analysis`, `get_timing_summary` |
| Export | Writes the files you use outside Diamond: the bitstream that configures the chip, simulation netlists, and similar. | `.bit`, `.jed`, `.vo`, `.vho`, `.sdf` | `generate_bitstream`, `run_flow` |

`compile_project` runs Synthesis, Translate, Map, PAR and the bitstream in order and stops
at the first failure. Diamond skips stages whose inputs have not changed, which is why a
repeated run can finish almost at once. Use `force` to rerun.

### Reading the results

- **Timing errors**: the number of paths that are too slow for your constraints. Zero
  means every constraint was met. The worst slack is the margin on the worst path; a
  negative value means that path misses its target by that many nanoseconds.
- **Setup and hold**: setup errors mean a signal arrives too late; hold errors mean it
  changes too early. Setup problems are the common case when a clock target is too fast.
- **Preferences met**: whether each constraint was met, with its target and the value
  actually achieved (for example a 200 MHz target that reached 150 MHz).
- **Utilisation**: how much of the chip the design uses. A design that uses most of a
  resource is likely to have difficulty meeting timing.
- **PAR success is not timing success.** PAR succeeds when everything is routed. Whether
  timing is met is a separate question, answered by the timing summary.

### File types you will meet

| Extension | Meaning |
|---|---|
| `.ldf` | Diamond project file |
| `.lpf` | Constraints: clocks, pin locations, timing goals |
| `.sty` | Strategy: tool option settings |
| `.srr`, `.srf` | Synthesis report and log |
| `.ngd` | Translated design database |
| `.mrp` | Map report: resource utilisation |
| `.ncd` | Placed and routed design |
| `.par` | Place and route report |
| `.twr` | Timing report |
| `.pad` | Pin assignment report |
| `.bit` | Bitstream that configures the chip |
| `.jed` | JEDEC programming file, for devices that use one |
| `.vo`, `.vho`, `.sdf` | Simulation netlists (Verilog, VHDL) and their timing delays |
| `.ibs` | IBIS model of the chip's I/O |

### Simulation

Simulation checks that a design behaves correctly by running it in software, before or
instead of using real hardware. It comes in two kinds:

- **Functional (RTL) simulation** runs your HDL source with a testbench. It checks the
  logic, not the speed.
- **Timing simulation** runs the netlist that Diamond writes after Map or PAR, together
  with an `.sdf` file of the real delays. It checks behaviour including timing.

**Does Diamond support simulation?** Yes. Diamond's documentation covers simulating Lattice
designs with ModelSim and QuestaSim, Aldec Riviera-PRO and Active-HDL, Cadence NC, and
Synopsys VCS, and the Diamond GUI includes a Simulation Wizard. Simulating a Lattice design
needs the compiled Lattice device and PMI simulation libraries, which Diamond includes or
which can be built with the `cmpl_libs` script that Diamond provides. The Diamond
installation used for testing contains QuestaSim programs and these libraries. Whether you
may run them depends on your license, which was not checked.

**What the server does for simulation:** it can generate the simulation netlists, which
the GUI would otherwise produce. These tasks were run and checked on Diamond 3.14.0.75.0:

| Netlist | Call |
|---|---|
| Verilog, after Map (with `.sdf` timing) | `run_flow` with milestone `Map`, task `MapVerilogSimFile` |
| Verilog, after PAR (with `.sdf` timing) | `run_flow` with milestone `Export`, task `TimingSimFileVlg` |
| VHDL, after PAR (with `.sdf` timing) | `run_flow` with milestone `Export`, task `TimingSimFileVHD` |

**What the server does not do:** it does not run a simulator, start the Simulation
Wizard, compile the simulation libraries, or write testbenches. No Diamond Tcl command that
runs a simulation was found in the console (only the netlist tasks above), so a simulator
has to be launched separately, for example from its own command line. Running a simulator
from the server is a candidate for a later release.

### What the server can do

- Start Diamond and keep it running, so projects stay open between requests.
- Open, create and save projects; read a project's device, sources and strategy.
- Check a project before running it, and report missing source files, including files
  whose names differ only by letter case.
- Change the target device, synthesis tool, source list and strategy options.
- Run each stage, or the whole flow, and report the outcome of each stage.
- Read back timing and utilisation as structured data, and read any report or log file.
- Generate a bitstream and confirm that the file was written.
- Generate the simulation netlists listed above, and a few other export files. These
  tasks were verified to run: `PARTrace` and `IOTiming` (under `PAR`), `MapTrace` (under
  `Map`), and `Jedecgen` and `IBIS` (under `Export`). Which tasks apply depends on the
  device and settings; Diamond answers "not a valid task name" for those that do not.
- Run any other Diamond Tcl command through `run_tcl`.
- Detect that Diamond crashed or hung, say why, and recover on the next request.

### What the server cannot do

- **Run a simulation**, as described above.
- **Program a device.** The server produces the bitstream or JEDEC file. Loading it into a
  chip needs Diamond Programmer and a cable, which are outside this server.
- **Use Diamond's graphical tools**: the Simulation Wizard, Reveal (on-chip logic
  analyzer), the spreadsheet and floorplan views, IP Express and Clarity Designer. The
  Diamond Tcl console does list commands for some of these areas (for example `rvl_*` and
  `rva_*` for Reveal, `pwc_*` for the power calculator, `eco_*` for engineering changes),
  and they can be tried through `run_tcl`, but the server has no tools for them and they
  have not been tested.
- **Edit your HDL or constraint files.** The server registers files with a project but
  does not write them. The AI assistant edits files with its own file tools, and then asks
  the server to run the flow again. Use `force` if a rerun does not pick up a change.
- **Decide your constraints.** It reports whether constraints were met. Choosing realistic
  clocks and pin assignments is an engineering decision.
- **Guarantee a correct or working design.** It runs Diamond's tools and reports what
  they say. A successful compile means the tools finished, not that the design is right.
- **Run on another machine or in a queue.** Flows run on the machine that runs the
  server. There is no remote execution, and no background jobs.
- **Work on more than one project at once.** There is one Diamond session and one open
  project.

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

Tasks that `run_flow` has been verified to run on Diamond 3.14.0.75.0 (milestone, then
task): `PAR` with `PARTrace` or `IOTiming`; `Map` with `MapTrace` or `MapVerilogSimFile`;
`Export` with `Bitgen`, `Jedecgen`, `IBIS`, `TimingSimFileVlg` or `TimingSimFileVHD`.
Other tasks named in Diamond's flow definition (for example `SSO`, `MapEqu`, `PowerCal`,
`Promgen`) were rejected as "not a valid task name" for the test project; they may apply
to other devices or settings.

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
| `run_tcl` | command, [timeout] | Run any Tcl in the live console. Multi-line scripts work. Prefer the dedicated tools. Commands containing `prj_project close` are refused (see below). |

## Behaviour you should know about

- **Closing a project restarts the console.** Diamond's own close command intermittently
  crashes it, so `close_project`, and `open_project` or `create_project` when a project is
  already open, restart the console instead. This takes a few seconds, discards unsaved
  changes, and clears any Tcl variables you set with `run_tcl`. Call `save_project`
  first if you changed the project.
- **`run_tcl` refuses `prj_project close`.** If a command or script sent through `run_tcl`
  contains it, the call is rejected with a pointer to `close_project`. The check is a
  text match, so it is a safeguard and not a guarantee, and it can reject a harmless
  string that happens to contain the command.
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

## Command audit log

The server writes a record of everything it sends to Diamond, so a reviewer can later see
what ran, when, on which account, and with what result.

### What is recorded

Each line of the log is one JSON object. Every record has `ts` (UTC time), `event`,
`session`, `pid` (the server process), and `user` (the operating-system account).

| Event | Meaning | Extra fields |
|---|---|---|
| `session_start` | The Diamond console started | `exe`, `version`, `console_pid` |
| `session_end` | The console stopped, was restarted, timed out, or died | `reason` |
| `command` | A command is about to be sent to Diamond | `id`, `command` (full text), `tool` |
| `result` | That command finished | `id`, `success`, `tcl_code`, `elapsed_ms`, `failure` |
| `refused` | A command was rejected before reaching Diamond | `command`, `tool`, `reason` |

- A `command` record is written **before** the command runs, so a command that hangs or
  crashes Diamond is still on record. It shares its `id` with its `result`.
- `tool` names the MCP tool that issued the command (for example `compile_project` or
  `run_tcl`). Commands sent by one tool on behalf of another are attributed to the tool
  that was called. It is absent when the caller is unknown.
- Commands longer than 20000 characters are truncated and marked `command_truncated`.
- The log holds the command text, not Diamond's output.

Example, with the account and process numbers replaced:

```json
{"ts": "2026-10-21T00:54:02.311+00:00", "event": "command", "session": "9f3c1a2e", "pid": 4120, "user": "<account>", "id": "641632c2", "tool": "run_tcl", "command": "exec echo audited-exec"}
{"ts": "2026-10-21T00:54:02.315+00:00", "event": "result", "session": "9f3c1a2e", "pid": 4120, "user": "<account>", "id": "641632c2", "success": true, "tcl_code": 0, "elapsed_ms": 4.1}
{"ts": "2026-10-21T00:54:02.402+00:00", "event": "refused", "session": "9f3c1a2e", "pid": 4120, "user": "<account>", "reason": "prj_project close", "command": "prj_project close"}
```

### Where it is written

| Setting | Effect |
|---|---|
| (default) | `~/.local/state/diamond-mcp/audit.log` on Linux (`$XDG_STATE_HOME` is honoured); `%LOCALAPPDATA%\diamond-mcp\audit.log` on Windows |
| `DIAMOND_MCP_AUDIT_LOG=<path>` | Write to this file |
| `"audit_log": "<path>"` in the config file | Same, when the environment variable is not set |
| `off`, `none`, `false`, `0` or empty | Disable logging |

The environment variable takes priority over the config file. `get_session_status` shows
the active path in `audit_log`, and `check_environment` warns when logging is disabled or
the location cannot be written.

The file and its directory are created readable only by their owner. The log rotates at
10 MB and keeps five older files (about 60 MB in total). If the file cannot be written, a
warning is printed once and the server keeps working without auditing.

### Reading the log

```bash
# Every command, with the tool that sent it
jq -r 'select(.event=="command") | [.ts, (.tool // "-"), .command] | @tsv' audit.log

# Commands that failed (join with the command records by id)
jq -r 'select(.event=="result" and .success==false) | .id' audit.log

# Console crashes, timeouts and restarts
jq -r 'select(.event=="session_end") | [.ts, .reason] | @tsv' audit.log
```

### Handling and limits

- **Treat the log as sensitive.** It contains the account name and the full text of every
  command, which includes file paths and anything an assistant typed into `run_tcl`.
- **It records what the server sent to Diamond, not what happened afterwards.** For
  example, `exec echo hello` is recorded as that command, but the log does not show what
  other programs a script started or what they did.
- **It does not cover use outside the server.** Someone who runs Diamond directly is not
  logged.
- **It is a detective control, not a preventive one.** Logging does not stop a command.
- **It is not tamper-proof.** The account that runs the server owns the log and the
  settings, so that account can edit or delete the file, or switch logging off. For a
  record that the user cannot alter, send it to a central log collector or to a location
  that the account can append to but not modify, and set the location in a launcher that
  the user cannot change. Your administrator decides how.

## Deploying in a restricted environment

The server and Diamond run with the privileges of the account that starts them, and
`run_tcl` can start any program that account can run. Software checks inside the server
cannot make that perfectly safe: Tcl offers several routes to run programs, and Diamond
itself can run user scripts named in project options. The reliable control is to limit
what the account can reach, so that a misused command can do little harm.

### What a sandbox can and cannot do

Tested on Linux with Diamond 3.14.0.75.0 and bubblewrap (`bwrap`), with the server started
by an MCP client over stdio:

- **Works:** a full compile including the bitstream; writes outside the project folder
  are refused; the home directory can be hidden; other processes are not visible; the
  audit log is still written.
- **Does not limit the network.** Everything reachable over the network stays reachable,
  so a command that can read a file could also send it elsewhere. Limiting network access
  must be done with firewall or egress rules set by your administrator.
- **Cutting the network off breaks Diamond.** On the test host, a sandbox with no network
  made the console exit at start-up (a directory-service lookup failed). Diamond may also
  need a license server. Allow only what your site needs, for example the license server
  and directory services.
- **Everything not hidden stays readable.** A read-only view of the root file system still
  lets the account read other files it has access to, including its own home directory.
  In the test, a home directory listing showed files that looked like credentials, which
  is why the example below hides it.

### Example (bubblewrap)

Create the project and log directories first, because a read-only root cannot create them.
Replace the paths with yours.

```bash
PROJECT=/work/my_design               # the only place flows may write results
STATE=/var/tmp/diamond-mcp-state      # audit log location

bwrap \
  --ro-bind / / --dev /dev --proc /proc \
  --tmpfs /tmp \
  --tmpfs "$HOME" \
  --ro-bind "$HOME/.flexlmrc" "$HOME/.flexlmrc" \
  --ro-bind "$HOME/diamond-mcp-venv" "$HOME/diamond-mcp-venv" \
  --bind "$PROJECT" "$PROJECT" \
  --bind "$STATE" "$STATE" \
  --setenv DIAMOND_MCP_AUDIT_LOG "$STATE/audit.log" \
  --unshare-pid --unshare-ipc --unshare-uts --die-with-parent --new-session \
  "$HOME/diamond-mcp-venv/bin/diamond-mcp"
```

What each part does:

- `--ro-bind / /` makes the whole file system read-only, including the Diamond installation.
- `--tmpfs /tmp` gives a private, writable `/tmp`. Tcl's `exec` needs one; without it every
  `exec` fails, and the sandbox is not testing what you intend.
- `--tmpfs "$HOME"` hides the real home directory. Bind back only what is needed: the
  server's own environment, and any license settings. At the test site Diamond reads its
  license from `~/.flexlmrc`, and without that file the console refused to start with
  "License checkout failed". Remove that line if your site does not use it, and add other
  files your site needs.
- `--bind "$PROJECT" "$PROJECT"` and `--bind "$STATE" "$STATE"` are the only writable
  locations, apart from the private `/tmp`.
- `--unshare-pid` and the other `--unshare` options isolate processes and IPC. Do not add
  `--unshare-net` unless you have confirmed that Diamond works without the network at your
  site.

To use it from Claude Code, register the whole command:

```bash
claude mcp add --scope user diamond -- bwrap <options as above> "$HOME/diamond-mcp-venv/bin/diamond-mcp"
```

Anything written to the private `/tmp` or the hidden home disappears when the sandbox ends.

### Other measures

These were not tested here, and are listed for your administrator to consider:

- Run the server as a dedicated low-privilege account that has no access to other users'
  files or to credentials.
- Use a container runtime (for example Podman) or a systemd unit with file system
  protection options instead of `bwrap`.
- Install the server as a read-only, root-owned package so that users cannot change it.
- Forward the audit log to a central collector.
- Apply network egress rules that allow only the license server and the services your site
  requires.

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
| `DIAMOND_MCP_AUDIT_LOG` | Audit log file, or `off` to disable. See "Command audit log". |

### Config file fields

| Field | Purpose |
|---|---|
| `diamond_exe` | Full path to the console executable. |
| `diamond_install_dir` | Installation directory; the executable is located inside it. |
| `lm_license_file` | License value. |
| `timeout_seconds` | Default command timeout. |
| `audit_log` | Audit log file, or `"off"` to disable. |

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
