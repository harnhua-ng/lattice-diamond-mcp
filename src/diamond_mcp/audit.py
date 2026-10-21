# Copyright(c) 2026 Lattice Semiconductor Corporation. All rights reserved. DO NOT DISTRIBUTE.
# Subject to the terms of the AI Solutions Software License Agreement
"""
Command audit log.

Records everything this server sends to Diamond, as one JSON object per line, so
that a reviewer can later answer "what ran, when, on whose behalf, and with what
result". This is a detective control: it does not stop a command from running,
and a user who can edit their own log file can also alter it. For a record that
the user cannot alter, forward the file to a central log collector or write it
to a location the user cannot modify (see the deployment guidance in the user
guide).

Events
------
    session_start   console started (executable, version, console pid)
    session_end     console stopped, restarted, killed, or died (reason)
    command         a command is about to be sent to Diamond (full text, tool)
    result          that command finished (success, Tcl code, elapsed time)
    refused         a command was rejected before reaching Diamond (reason)

`command` is written before the command runs, so a command that hangs or
crashes Diamond is still on record. `command` and `result` share an `id`.

Every record carries: ts (UTC, ISO 8601), event, session, pid (server process),
user (operating-system account). `command` and `refused` records also carry
`tool`, the MCP tool that issued the command, when known.

Writing is best effort: if the file cannot be written, a warning is logged once
and the server keeps working.
"""

from __future__ import annotations

import contextvars
import getpass
import json
import logging
import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

MAX_COMMAND_CHARS = 20_000
DEFAULT_MAX_BYTES = 10_000_000
DEFAULT_BACKUPS = 5

# Set by the server for the duration of each MCP tool call.
current_tool: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "diamond_mcp_current_tool", default=None
)


class _PrivateRotatingHandler(RotatingFileHandler):
    """Rotating file handler that creates files readable only by their owner."""

    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(fd, self.mode, encoding=self.encoding)


def _default_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


class AuditLog:
    """Thread-safe, size-rotated JSON-lines audit log."""

    def __init__(
        self,
        path: str,
        max_bytes: int = DEFAULT_MAX_BYTES,
        backups: int = DEFAULT_BACKUPS,
    ):
        self.path = str(path)
        self.session_id = uuid.uuid4().hex[:8]
        self.disabled = False
        self._user = _default_user()
        self._lock = threading.Lock()
        self._logger: Optional[logging.Logger] = None
        self._max_bytes = max_bytes
        self._backups = backups

    def _ensure_logger(self) -> bool:
        if self._logger is not None:
            return True
        try:
            target = Path(self.path)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            handler = _PrivateRotatingHandler(
                str(target), maxBytes=self._max_bytes, backupCount=self._backups, encoding="utf-8"
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            log = logging.Logger(f"diamond_mcp.audit.{self.session_id}", level=logging.INFO)
            log.addHandler(handler)
            self._logger = log
            return True
        except OSError as exc:
            self.disabled = True
            print(
                f"diamond-mcp: audit log disabled, cannot write {self.path}: {exc}",
                file=sys.stderr,
            )
            return False

    def record(self, event: str, **fields: Any) -> None:
        """Append one record. Never raises."""
        if self.disabled:
            return
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": event,
            "session": self.session_id,
            "pid": os.getpid(),
            "user": self._user,
        }
        command = fields.pop("command", None)
        if command is not None:
            if len(command) > MAX_COMMAND_CHARS:
                entry["command_truncated"] = True
                command = command[:MAX_COMMAND_CHARS]
            fields["command"] = command
        entry.update({k: v for k, v in fields.items() if v is not None})
        try:
            line = json.dumps(entry, ensure_ascii=False)
            with self._lock:
                if self._ensure_logger():
                    self._logger.info(line)
        except Exception as exc:  # the audit log must never break a tool call
            logger.warning("audit log write failed: %s", exc)

    def close(self) -> None:
        with self._lock:
            if self._logger is not None:
                for handler in list(self._logger.handlers):
                    handler.close()
                    self._logger.removeHandler(handler)
                self._logger = None
