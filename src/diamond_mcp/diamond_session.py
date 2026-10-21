# Copyright(c) 2026 Lattice Semiconductor Corporation. All rights reserved. DO NOT DISTRIBUTE.
# Subject to the terms of the AI Solutions Software License Agreement
"""
Persistent Lattice Diamond Tcl console session.

Keeps one Diamond console process (diamondc on Linux, pnmainc.exe on Windows)
alive with piped stdin/stdout so project state survives between MCP tool calls.

Protocol
--------
Every command is written to a temporary Tcl file and run as

    set ::__dmcp_rc [catch {source {<file>}} ::__dmcp_res]; puts <BEGIN>; ...

so that:
  * the command can be any multi-line Tcl without quoting problems;
  * an uncaught Tcl error never reaches the console;
  * the Tcl return code and result are reported exactly, delimited by unique
    sentinel lines, instead of being guessed from the log text.

Known Diamond defect
--------------------
`prj_project close` intermittently crashes Diamond 3.14.0.75.0 with SIGSEGV,
mostly when no flow stage ran since the project was opened (for example open,
prj_dev, close). The failure does not depend on how the command is delivered
(direct console input, catch, eval and source all reproduce it). The server
therefore never sends `prj_project close`: it restarts the console instead
(see DiamondSession.restart), which is deterministic.

Failure handling
----------------
A command that exceeds its timeout, or a console that exits mid-command,
marks the session as down (the Tcl interpreter state is unknown) and clears
the open-project record. The server restarts the session on the next call.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .audit import AuditLog, current_tool

logger = logging.getLogger(__name__)

DEFAULT_COMMAND_TIMEOUT_S = 3600.0
DEFAULT_START_TIMEOUT_S = 180.0
STOP_GRACE_S = 10.0
LOG_RING_LINES = 2000
MAX_FLAGGED_LINES = 50

_BANNER_RE = re.compile(r"Lattice Diamond\s+Version\s+(\d+(?:\.\d+)+)")
_FLAGGED_RE = re.compile(
    r"^\s*(ERROR|WARNING|FATAL)\b|software has expired|Cannot find|invalid command name|failed",
    re.IGNORECASE,
)
_UNSAFE_PATH_CHARS = set("{}\\")


class SessionError(RuntimeError):
    """The session could not be started or is not usable."""


@dataclass
class CommandResult:
    """
    Result from executing a Tcl command in the Diamond console.

    Attributes:
        command:    The Tcl command(s) that were executed.
        output:     Console/log text produced while the command ran.
        success:    True when the Tcl return code was 0. Diamond reports flow
                    and project failures through the Tcl return code, so this
                    is reliable for failures. It does NOT prove that an
                    expected output file was written (for example a plain
                    `prj_run Export` succeeds without producing a bitstream),
                    so tools that promise a file verify it separately.
        elapsed_ms: Wall-clock execution time in milliseconds.
        result:     The Tcl result string (return value, or error message).
        tcl_code:   Tcl return code (0 ok, 1 error), or None if no code was
                    received (timeout, crash).
        warnings:   Output lines that look like errors/warnings (capped).
        timestamp:  ISO-8601 completion time.
    """

    command: str
    output: str
    success: bool
    elapsed_ms: float
    result: str = ""
    tcl_code: Optional[int] = None
    warnings: list = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


def tcl_quote(value: str) -> str:
    """Quote a string as a single Tcl word."""
    if value and not any(c in value for c in "{}\\") and not value.endswith("\\"):
        return "{" + value + "}"
    out = []
    for ch in value:
        if ch in '\\{}[]$";&|<>()#' or ch.isspace():
            if ch == "\n":
                out.append("\\n")
            elif ch == "\t":
                out.append("\\t")
            else:
                out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out) or "{}"


def extract_flagged_lines(lines: list[str]) -> list[str]:
    """Return output lines that look like errors or warnings, capped at MAX_FLAGGED_LINES."""
    flagged = [ln.strip() for ln in lines if _FLAGGED_RE.search(ln)]
    if len(flagged) > MAX_FLAGGED_LINES:
        extra = len(flagged) - MAX_FLAGGED_LINES
        flagged = flagged[:MAX_FLAGGED_LINES] + [f"... {extra} more flagged line(s) omitted"]
    return flagged


def _describe_exit(returncode: Optional[int]) -> str:
    if returncode is None:
        return "still running"
    if returncode < 0:
        try:
            return f"terminated by signal {signal.Signals(-returncode).name}"
        except ValueError:
            return f"terminated by signal {-returncode}"
    if returncode > 128 and os.name != "nt":
        try:
            return f"exit code {returncode} (signal {signal.Signals(returncode - 128).name})"
        except ValueError:
            pass
    return f"exit code {returncode}"


class DiamondSession:
    """
    Manages a persistent Diamond Tcl console.

    Thread safety: _lock serialises command execution, so concurrent MCP calls
    queue behind each other.
    """

    def __init__(
        self,
        diamond_exe: Optional[str] = None,
        timeout: float = DEFAULT_COMMAND_TIMEOUT_S,
        lm_license_file: Optional[str] = None,
        start_timeout: float = DEFAULT_START_TIMEOUT_S,
        audit: Optional[AuditLog] = None,
    ):
        self.audit = audit
        self.diamond_exe = diamond_exe
        self.timeout = float(timeout)
        self.lm_license_file = lm_license_file
        self.start_timeout = float(start_timeout)

        self.process: Optional[subprocess.Popen] = None
        self.diamond_version: Optional[str] = None
        self.current_project: Optional[str] = None
        self.epoch = 0  # increments on every successful start
        self.last_exit: Optional[str] = None  # why the previous console went away

        self._lock = threading.Lock()
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._reader: Optional[threading.Thread] = None
        self._workdir: Optional[Path] = None
        self._log: deque[str] = deque(maxlen=LOG_RING_LINES)
        self._session_start: Optional[str] = None
        self._stats = {"commands_run": 0, "errors": 0, "total_ms": 0.0}

    # ------------------------------------------------------------------ state

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _audit(self, event: str, **fields) -> None:
        if self.audit is not None:
            self.audit.record(event, **fields)

    def _record_down(self, reason: str) -> None:
        self.last_exit = reason
        self.current_project = None
        logger.error("Diamond session down: %s", reason)
        self._audit("session_end", reason=reason)

    # ---------------------------------------------------------------- process

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.lm_license_file:
            env["LM_LICENSE_FILE"] = self.lm_license_file
        # diamond_env only re-points FOUNDRY at the installation when this is unset.
        env.pop("BALI_USE_FOUNDRY_OUTSIDE", None)
        return env

    def _read_output(self, process: subprocess.Popen, out_queue: "queue.Queue[Optional[str]]") -> None:
        try:
            for line in process.stdout:
                out_queue.put(line.rstrip("\n\r"))
        except Exception:
            pass
        finally:
            out_queue.put(None)

    def _drain_to_log(self) -> None:
        while True:
            try:
                line = self._queue.get_nowait()
            except queue.Empty:
                return
            if line is None:
                return
            self._note_line(line)

    def _note_line(self, line: str) -> None:
        self._log.append(line)
        match = _BANNER_RE.search(line)
        if match:
            self.diamond_version = match.group(1)

    def _wait_for(self, pattern: "re.Pattern[str]", timeout: float, label: str):
        """Collect lines until one matches pattern. Returns (lines_before, match)."""
        deadline = time.monotonic() + timeout
        collected: list[str] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Timed out after {timeout:.0f}s waiting for {label}")
            try:
                line = self._queue.get(timeout=min(1.0, remaining))
            except queue.Empty:
                continue
            if line is None:
                raise EOFError("Diamond console exited unexpectedly")
            self._note_line(line)
            match = pattern.search(line)
            if match:
                prefix = line[: match.start()]
                if prefix.strip():
                    collected.append(prefix)
                return collected, match
            collected.append(line)

    def _kill(self) -> None:
        proc = self.process
        if proc is None:
            return
        try:
            if proc.poll() is None:
                if os.name != "nt":
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                else:
                    proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            pass

    def _cleanup_workdir(self) -> None:
        if self._workdir is not None:
            shutil.rmtree(self._workdir, ignore_errors=True)
            self._workdir = None

    # -------------------------------------------------------------- lifecycle

    def start(self) -> CommandResult:
        """Start the console. Idempotent: a no-op when it is already running."""
        with self._lock:
            return self._start_locked()

    def _start_locked(self) -> CommandResult:
        started = time.monotonic()
        if self.is_running:
            return CommandResult("start", "Session already running.", True, 0.0)
        if not self.diamond_exe:
            raise SessionError(
                "No Diamond executable configured. Set DIAMOND_EXE, run configure_diamond, "
                "or install Diamond in a standard location (see detect_diamond_installations)."
            )
        if not Path(self.diamond_exe).is_file():
            raise SessionError(f"Diamond executable not found: {self.diamond_exe}")

        self._cleanup_workdir()
        self._workdir = Path(tempfile.mkdtemp(prefix="diamond_mcp_"))
        self._queue = queue.Queue()
        self._log.clear()
        self.diamond_version = self._version_from_path(self.diamond_exe)

        popen_kwargs: dict = {}
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True
        try:
            self.process = subprocess.Popen(
                [self.diamond_exe],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=str(self._workdir),
                env=self._build_env(),
                **popen_kwargs,
            )
        except OSError as exc:
            self._cleanup_workdir()
            raise SessionError(f"Failed to launch {self.diamond_exe}: {exc}") from exc

        self._reader = threading.Thread(
            target=self._read_output, args=(self.process, self._queue), daemon=True
        )
        self._reader.start()

        token = uuid.uuid4().hex[:8]
        ready = re.compile(rf"<<DMCP_READY_{token}>>")
        try:
            self._send(f'puts "<<DMCP_READY_{token}>>"; flush stdout')
            lines, _ = self._wait_for(ready, self.start_timeout, "the Diamond console to respond")
        except (TimeoutError, EOFError, OSError) as exc:
            detail = "\n".join(self._log).strip()
            code = self.process.poll()
            self._kill()
            self._cleanup_workdir()
            self._record_down(f"failed to start ({exc}; {_describe_exit(code)})")
            raise SessionError(
                f"Diamond console did not become ready: {exc}"
                + (f"\nConsole output:\n{detail}" if detail else "")
            ) from exc

        self.epoch += 1
        self.last_exit = None
        self._session_start = datetime.now().isoformat()
        self._audit(
            "session_start",
            exe=self.diamond_exe,
            version=self.diamond_version,
            console_pid=self.process.pid,
        )
        elapsed = (time.monotonic() - started) * 1000
        return CommandResult(
            "start",
            f"Diamond console started (version {self.diamond_version or 'unknown'}).",
            True,
            elapsed,
            warnings=extract_flagged_lines(lines),
        )

    @staticmethod
    def _version_from_path(exe: str) -> Optional[str]:
        for part in reversed(Path(exe).parts):
            if re.match(r"^\d+\.\d+", part):
                return re.sub(r"x64$", "", part, flags=re.IGNORECASE)
        return None

    def restart(self) -> CommandResult:
        """
        Discard the console and start a fresh one.

        This is how a project is closed (see the module docstring). Unsaved
        project changes are lost.
        """
        with self._lock:
            if self.process is not None:
                self._kill()
                self.process = None
                self._audit("session_end", reason="restart")
            self.current_project = None
            self.last_exit = None
            self._cleanup_workdir()
            return self._start_locked()

    def stop(self) -> CommandResult:
        """Stop the console gracefully, force-killing after STOP_GRACE_S."""
        with self._lock:
            started = time.monotonic()
            if self.process is None or not self.is_running:
                self.process = None
                self._cleanup_workdir()
                self.current_project = None
                return CommandResult("stop", "Session was not running.", True, 0.0)
            try:
                self._send("exit")
            except OSError:
                pass
            try:
                self.process.wait(timeout=STOP_GRACE_S)
                message = "Diamond console stopped."
            except subprocess.TimeoutExpired:
                self._kill()
                message = "Diamond console did not exit in time and was killed."
            self.process = None
            self.current_project = None
            self._cleanup_workdir()
            self._audit("session_end", reason="stopped")
            return CommandResult("stop", message, True, (time.monotonic() - started) * 1000)

    # --------------------------------------------------------------- commands

    def _send(self, line: str) -> None:
        assert self.process is not None and self.process.stdin is not None
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()

    def _write_script(self, command: str, cmd_id: str) -> str:
        assert self._workdir is not None
        script = self._workdir / f"cmd_{cmd_id}.tcl"
        script.write_text(command + "\n", encoding="utf-8")
        path = script.as_posix()
        if any(c in _UNSAFE_PATH_CHARS for c in path):
            raise SessionError(f"Temporary directory path contains characters Tcl cannot quote: {path}")
        return path

    def run_tcl(self, command: str, timeout: Optional[float] = None) -> CommandResult:
        """
        Run Tcl in the live console and return its result.

        Never raises for command failures; a failed, timed-out or crashed command
        is reported through CommandResult.success. Raises SessionError only when
        the session is not running.
        """
        limit = float(timeout) if timeout else self.timeout
        with self._lock:
            if not self.is_running:
                detail = f" ({self.last_exit})" if self.last_exit else ""
                raise SessionError(f"Diamond session is not running{detail}.")
            return self._run_locked(command, limit)

    def _run_locked(self, command: str, limit: float) -> CommandResult:
        self._drain_to_log()
        started = time.monotonic()
        cmd_id = uuid.uuid4().hex[:8]
        script_path = self._write_script(command, cmd_id)

        begin = re.compile(rf"<<DMCP_BEGIN_{cmd_id}>>")
        done = re.compile(rf"<<DMCP_DONE_{cmd_id}_(\d+)>>")
        wrapper = (
            "set ::__dmcp_rc [catch {source {" + script_path + "}} ::__dmcp_res]; "
            f'puts "<<DMCP_BEGIN_{cmd_id}>>"; puts $::__dmcp_res; '
            f'puts "<<DMCP_DONE_{cmd_id}_$::__dmcp_rc>>"; flush stdout'
        )

        log_lines: list[str] = []
        result_lines: list[str] = []
        tcl_code: Optional[int] = None
        failure: Optional[str] = None
        self._audit("command", id=cmd_id, tool=current_tool.get(), command=command)
        try:
            self._send(wrapper)
            before, _ = self._wait_for(begin, limit, "the command to finish")
            log_lines.extend(before)
            remaining = max(1.0, limit - (time.monotonic() - started))
            between, match = self._wait_for(done, remaining, "the command to finish")
            result_lines.extend(between)
            tcl_code = int(match.group(1))
        except TimeoutError:
            failure = (
                f"Command timed out after {limit:.0f}s. The Diamond session was terminated "
                "because its state is unknown; it will be restarted on the next call and the "
                "open project must be reopened."
            )
            self._kill()
            self.process = None
            self._record_down(f"command timed out after {limit:.0f}s")
        except (EOFError, OSError):
            code = self.process.poll() if self.process else None
            if self.process is not None and code is None:
                try:
                    self.process.wait(timeout=2)
                    code = self.process.poll()
                except subprocess.TimeoutExpired:
                    pass
            reason = _describe_exit(code)
            failure = (
                f"Diamond console terminated during the command ({reason}). The session will be "
                "restarted on the next call and the open project must be reopened."
            )
            self._record_down(reason)
        finally:
            try:
                Path(script_path).unlink()
            except OSError:
                pass

        elapsed_ms = (time.monotonic() - started) * 1000
        self._stats["commands_run"] += 1
        self._stats["total_ms"] += elapsed_ms
        self._audit(
            "result",
            id=cmd_id,
            success=failure is None and tcl_code == 0,
            tcl_code=tcl_code,
            elapsed_ms=round(elapsed_ms, 1),
            failure=failure,
        )

        # Lines up to the result marker belong to the console log; collect any
        # that were recorded before a failure for context.
        if failure is not None:
            tail = [ln for ln in list(self._log)[-40:]]
            self._stats["errors"] += 1
            return CommandResult(
                command=command,
                output="\n".join(tail).strip(),
                success=False,
                elapsed_ms=elapsed_ms,
                result=failure,
                tcl_code=None,
                warnings=extract_flagged_lines(tail) + [failure],
            )

        success = tcl_code == 0
        if not success:
            self._stats["errors"] += 1
        return CommandResult(
            command=command,
            output="\n".join(log_lines).strip(),
            success=success,
            elapsed_ms=elapsed_ms,
            result="\n".join(result_lines).strip(),
            tcl_code=tcl_code,
            warnings=extract_flagged_lines(log_lines + result_lines),
        )

    # ------------------------------------------------------------------ stats

    def get_stats(self, log_lines: int = 50) -> dict:
        commands = self._stats["commands_run"]
        stats = {
            "is_running": self.is_running,
            "diamond_exe": self.diamond_exe,
            "diamond_version": self.diamond_version,
            "current_project": self.current_project,
            "session_start": self._session_start,
            "session_epoch": self.epoch,
            "last_exit": self.last_exit,
            "commands_run": commands,
            "errors": self._stats["errors"],
            "avg_command_time_ms": round(self._stats["total_ms"] / commands, 1) if commands else 0.0,
            "timeout_seconds": self.timeout,
            "audit_log": self.audit.path if self.audit is not None and not self.audit.disabled else None,
        }
        if log_lines > 0:
            stats["console_log_tail"] = list(self._log)[-log_lines:]
        return stats


def make_session(
    diamond_exe: Optional[str] = None,
    timeout: Optional[float] = None,
    lm_license_file: Optional[str] = None,
) -> DiamondSession:
    """Build a session from explicit values, falling back to the resolved configuration."""
    from .config import (
        resolve_audit_log_path,
        resolve_diamond_path,
        resolve_lm_license_file,
        resolve_timeout,
    )

    audit_path = resolve_audit_log_path().path
    return DiamondSession(
        diamond_exe=diamond_exe or resolve_diamond_path().path,
        timeout=timeout or resolve_timeout(),
        lm_license_file=lm_license_file or resolve_lm_license_file(),
        audit=AuditLog(audit_path) if audit_path else None,
    )
