#!/usr/bin/env python3
# Copyright(c) 2026 Lattice Semiconductor Corporation. All rights reserved. DO NOT DISTRIBUTE.
# Subject to the terms of the AI Solutions Software License Agreement
"""
Installer for the Lattice Diamond MCP server (lattice-diamond-mcp).

Run it from the unpacked installer zip:

    python install.py                      install, then register with Claude Code
    python install.py --no-register        install only
    python install.py --uninstall          remove the registration and the installation
    python install.py --if-locked kill     stop a running server without asking (see below)

Two kinds of zip use this one script; BUILDINFO says which:

  wheels   (built by build_installer.py) installs the packages in wheels/ with pip, with no
           network, into its own virtual environment. It needs the Python version the zip was
           built for; the running interpreter may be older, in which case a suitable one is
           looked for. No program of ours is installed: the server is started as
           `<environment>/python -I -m diamond_mcp` (-I so that a folder the client happens to
           run in cannot shadow a module).
  binary   (built by build_binary.py) copies the self-contained diamond-mcp folder. No Python
           is needed to RUN the result; this script only needs any Python 3.8 or later.

Either kind also comes as a COMBINED zip with a build for each operating system (and, for wheels, each
Python version) in its own folder next to this script; the installer picks the folder that fits this
machine and says which it chose.

A server that is running from the folder being replaced (every open Claude Code or VS Code session
starts one) holds its files open: Windows cannot replace them, and on Linux the running program would
lose the files it still needs. So before anything is changed the installer looks for such processes and,
if there are any, asks what to do: stop them, wait while you stop them, or exit with nothing changed.
It never stops a process without your say-so. With nobody to answer (a script, or an AI assistant
running the installer) it exits and says so; --if-locked kill (or DIAMOND_MCP_IF_LOCKED=kill) is the
way to approve stopping them in advance, --if-locked exit to refuse.

This file uses only the standard library and runs on Python 3.8 or later.
"""

import argparse
import glob
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
IS_WINDOWS = os.name == "nt"
MARKER = ".diamond-mcp-installer"
DEFAULT_NAME = "lattice-diamond-mcp"
OLD_DEFAULT_NAME = "diamond"  # the name earlier versions registered
# How the wheels install starts the server, after the environment's python.
WHEELS_SERVER_ARGS = ["-I", "-m", "diamond_mcp"]
MANIFEST_NAME = "MANIFEST.sha256"
SELF_CONTAINED = ("binary", "embedded")  # builds that carry their own program or interpreter; no Python is needed


class InstallError(Exception):
    """A problem the user can act on; printed without a traceback."""


def say(message=""):
    print(message, flush=True)


# ------------------------------------------------------------------ build info

def load_buildinfo(folder=None):
    path = os.path.join(folder or HERE, "BUILDINFO")
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise InstallError("Cannot read BUILDINFO next to install.py (%s). Run the installer "
                           "from the unpacked zip." % exc)


def verify_manifest(folder):
    """Check every file of the build against its MANIFEST.sha256 (`<sha256>  <relative/path>` per line), before anything
    is installed. Returns the number of files checked, or None when the build has no manifest (older zips)."""
    path = os.path.join(folder, MANIFEST_NAME)
    if not os.path.isfile(path):
        return None
    problems, count = [], 0
    with open(path, encoding="utf-8") as handle:
        entries = [line.rstrip("\r\n") for line in handle if line.strip()]
    for entry in entries:
        digest, _, relative = entry.partition("  ")
        parts = relative.replace("\\", "/").split("/")
        if len(digest) != 64 or not relative or relative.startswith("/") or ".." in parts:
            problems.append("an unreadable line in %s: %r" % (MANIFEST_NAME, entry[:80]))
            continue
        target = os.path.join(folder, *parts)
        count += 1
        try:
            actual = hashlib.sha256()
            with open(target, "rb") as handle:
                for block in iter(lambda: handle.read(1 << 20), b""):
                    actual.update(block)
        except OSError:
            problems.append("missing or unreadable: " + relative)
            continue
        if actual.hexdigest() != digest:
            problems.append("changed: " + relative)
    if problems:
        shown = "\n  ".join(problems[:8]) + ("\n  ... and %d more" % (len(problems) - 8) if len(problems) > 8 else "")
        raise InstallError("The package is damaged or has been changed; nothing was installed. %d of %d files do not match "
                           "%s:\n  %s\nDownload the zip again and compare its SHA-256 with the .sha256 file beside it. "
                           "Antivirus software that quarantines a file also causes this." % (len(problems), len(entries), MANIFEST_NAME, shown))
    return count


def platform_matches(info):
    wanted = info.get("platform", "")
    return not wanted or wanted.startswith("windows" if IS_WINDOWS else "linux")


def select_bundle(python=None):
    """
    Return (folder, BUILDINFO) of the build to install.

    A single-platform zip has BUILDINFO next to this script. A combined zip has one folder per build, each
    with its own BUILDINFO; the one for this operating system is used and, for wheels, the one whose Python
    can be found (the running interpreter's version first, then newer to older), or the one that matches
    the interpreter given with --python.
    """
    try:
        return HERE, load_buildinfo()
    except InstallError as missing:
        found = []
        for name in sorted(os.listdir(HERE)):
            folder = os.path.join(HERE, name)
            if os.path.isfile(os.path.join(folder, "BUILDINFO")):
                try:
                    found.append((folder, load_buildinfo(folder)))
                except InstallError:
                    continue
        if not found:
            raise missing
    here = "windows" if IS_WINDOWS else "linux"
    fitting = [(folder, info) for folder, info in found if platform_matches(info)]
    if not fitting:
        raise InstallError("This zip holds builds for %s but this machine is %s. Use the zip built for this operating system."
                           % (", ".join(sorted({info.get("platform", "?") for _, info in found})), here))
    if all(info.get("kind") in SELF_CONTAINED for _, info in fitting):
        return fitting[0]
    running = "%d.%d" % sys.version_info[:2]
    ranked = sorted(fitting, key=lambda item: (item[1].get("python") != running,
                                                [-int(part) for part in item[1].get("python", "0.0").split(".")]))
    explicit = python
    wanted_versions = []
    for folder, info in ranked:
        version = info.get("python", "")
        wanted_versions.append(version)
        if explicit:
            if python_version_of([explicit]) == version:
                return folder, info
            continue
        try:
            find_python(version)
            return folder, info
        except InstallError:
            continue
    raise InstallError("This zip holds wheels for Python %s, and none of those was found on this machine. Install one of them%s."
                       % (", ".join(wanted_versions), ", or pass the path of a matching interpreter with --python" if not explicit else
                          "; the interpreter given with --python is not one of them"))


# -------------------------------------------------------------- python lookup

def python_version_of(command):
    """Return 'X.Y' for an interpreter command (a list), or None if it does not run."""
    try:
        out = subprocess.run(
            list(command) + ["-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = out.stdout.strip()
    return text if out.returncode == 0 and text.count(".") == 1 else None


def candidate_pythons(wanted):
    """Interpreter commands to try, most specific first. `wanted` is like '3.11'."""
    candidates = [[sys.executable]]
    candidates.append(["python" + wanted])
    if IS_WINDOWS:
        candidates.append(["py", "-" + wanted])
    candidates += [["python3"], ["python"]]
    return candidates


def find_python(wanted, explicit=None):
    """Return a command (list) for a Python whose version is exactly `wanted`."""
    if explicit:
        version = python_version_of([explicit])
        if version != wanted:
            raise InstallError("--python %s is Python %s, but this installer needs Python %s."
                               % (explicit, version or "(not runnable)", wanted))
        return [explicit]
    seen = []
    for command in candidate_pythons(wanted):
        if shutil.which(command[0]) is None:
            continue
        version = python_version_of(command)
        seen.append("%s=%s" % (" ".join(command), version))
        if version == wanted:
            return command
    raise InstallError(
        "This installer needs Python %s and none was found (looked at: %s). Install Python %s, "
        "or pass its path with --python. A zip for another Python version needs a different build."
        % (wanted, ", ".join(seen) or "nothing on PATH", wanted)
    )


def check_platform(info):
    wanted = info.get("platform", "")
    here = "windows" if IS_WINDOWS else "linux"
    if not platform_matches(info):
        raise InstallError("This installer was built for %s but this machine is %s. Use the zip "
                           "built for this operating system." % (wanted, here))
    floor = info.get("min_glibc")
    if floor and not IS_WINDOWS:
        try:
            current = os.confstr("CS_GNU_LIBC_VERSION").split()[-1]
            if _version_tuple(current) < _version_tuple(floor):
                raise InstallError("This build needs glibc %s or later; this machine has %s. Use a newer "
                                   "operating system, or a build made for an older one." % (floor, current))
        except (ValueError, OSError, AttributeError, IndexError):
            pass  # cannot tell; the program or pip will report an incompatibility itself


def _version_tuple(text):
    return tuple(int(part) for part in text.split(".") if part.isdigit())


# ----------------------------------------------------------------- environment

def venv_python(venv_dir):
    return os.path.join(venv_dir, "Scripts" if IS_WINDOWS else "bin", "python.exe" if IS_WINDOWS else "python")


def legacy_launcher(venv_dir):
    """The launcher earlier wheels installs registered; it is no longer installed."""
    return os.path.join(venv_dir, "Scripts" if IS_WINDOWS else "bin", "diamond-mcp.exe" if IS_WINDOWS else "diamond-mcp")


def run(command, dry_run=False, check=True, quiet=False):
    shown = " ".join(shlex.quote(part) for part in command)
    if dry_run:
        say("  [dry run] " + shown)
        return subprocess.CompletedProcess(command, 0, "", "")
    if not quiet:
        say("  $ " + shown)
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    if check and result.returncode != 0:
        raise InstallError("Command failed (exit %d): %s\n%s" % (result.returncode, shown, result.stdout.strip()))
    return result


# ------------------------------------------------------------ a running server

# What to do when a server runs from the folder (--if-locked, or DIAMOND_MCP_IF_LOCKED).
IF_LOCKED_CHOICES = ("ask", "kill", "exit")


def locked_files(path):
    """Executables and extension modules under `path` that a running process holds open.
    Windows refuses to replace these; elsewhere nothing is locked."""
    if not IS_WINDOWS:
        return []
    locked = []
    for pattern in ("*.exe", "*.pyd", "*.dll"):
        for found in glob.glob(os.path.join(glob.escape(path), "**", pattern), recursive=True):
            try:
                with open(found, "r+b"):
                    pass
            except PermissionError:
                if os.access(found, os.W_OK):  # not merely read-only: something has it open
                    locked.append(found)
            except OSError:
                pass
    return locked


def pids_running_from(path):
    """Best effort: 'PID name' for each process whose program lives under `path`."""
    if not IS_WINDOWS:
        found = []
        prefix = os.path.abspath(path) + os.sep
        try:
            entries = os.listdir("/proc")
        except OSError:
            return found
        for entry in entries:
            if not entry.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % entry, "rb") as handle:
                    argv0 = handle.read().split(b"\0", 1)[0].decode("utf-8", "replace")
            except OSError:
                continue
            if argv0.startswith(prefix):
                found.append("%s %s" % (entry, os.path.basename(argv0)))
        return found
    script = ("Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like '"
              + os.path.abspath(path).replace("'", "''") + "\\*' } | ForEach-Object { \"$($_.ProcessId) $($_.Name)\" }")
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-Command", script], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, universal_newlines=True, timeout=30,
                                stdin=subprocess.DEVNULL)  # it would otherwise swallow a typed answer
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def server_running_from(path):
    """Whether a process is running from `path`. On Windows the held-open files say so; elsewhere
    nothing is locked, but replacing the files under a running program breaks its next import, so
    the processes are looked for instead."""
    if IS_WINDOWS:
        return bool(locked_files(path))
    return bool(pids_running_from(path))


def if_locked_policy(args):
    value = (getattr(args, "if_locked", "") or os.environ.get("DIAMOND_MCP_IF_LOCKED", "")).strip().lower()
    return value if value in IF_LOCKED_CHOICES else "ask"


def process_ids(procs):
    """The PIDs in `pids_running_from` output ('1234 python.exe'), without this installer's own."""
    pids = []
    for line in procs:
        head = line.split(None, 1)[0] if line.strip() else ""
        if head.isdigit() and int(head) != os.getpid():
            pids.append(int(head))
    return pids


def kill_processes(procs):
    """Stop the listed processes. On Windows the whole tree goes: the environment's python.exe is a
    launcher, and the process that holds the compiled modules open is its child, which is not itself
    found under the folder."""
    for pid in process_ids(procs):
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue
        for _ in range(20):
            time.sleep(0.25)
            try:
                os.kill(pid, 0)
            except OSError:
                break
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def wait_until_free(path, seconds=15.0):
    """Whether nothing runs from `path` within `seconds`."""
    deadline = time.monotonic() + seconds
    while True:
        if not server_running_from(path):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def describe_lock(path, procs):
    listing = "\n".join("    " + p for p in procs) if procs else "    (the processes could not be listed)"
    return ("%s is in use by running MCP server process(es):\n%s\n"
            "It cannot be replaced while they run. Every open Claude Code or VS Code session starts one." % (path, listing))


def ask_lock_choice(path, procs):
    """'kill', 'wait' or 'exit' from the user, or 'noprompt' when nobody can answer (a script, or an AI
    assistant running the installer). EOFError, not isatty(), decides that."""
    say()
    say(describe_lock(path, procs))
    say()
    say("  [a] Stop those processes now and continue. Open sessions lose the Diamond MCP server\n"
        "      until they are restarted.")
    say("  [b] I will stop them myself: wait here, then check again.")
    say("  [c] Exit without changing anything.")
    while True:
        try:
            answer = input("Choice [a/b/c]: ").lstrip("\ufeff").strip().lower()  # PowerShell 5.1 pipes a BOM
        except (EOFError, KeyboardInterrupt):
            say()
            return "noprompt"
        if answer in ("a", "b", "c"):
            return {"a": "kill", "b": "wait", "c": "exit"}[answer]
        say("  Please answer a, b or c.")


def locked_exit_message(path, procs, asked):
    if asked:
        return "Nothing has been changed. %s is still in use." % path
    return (describe_lock(path, procs) + "\n"
            "There is nobody to ask, so nothing has been changed. Choose one and run the installer again:\n"
            "  - stop the listed processes yourself (or close Claude Code and VS Code), then re-run;\n"
            "  - re-run with --if-locked kill (or DIAMOND_MCP_IF_LOCKED=kill) to let the installer stop them.\n"
            "If an AI assistant is running this installer, ask the user which, and do not choose for them.")


def free_directory(args, path):
    """Return once nothing runs from `path`, or raise InstallError. Stopping the server processes needs
    the user's say-so: --if-locked kill, or answer [a] at the prompt."""
    if not os.path.isdir(path):
        return
    policy = if_locked_policy(args)
    while server_running_from(path):
        procs = pids_running_from(path)
        choice = policy if policy in ("kill", "exit") else ask_lock_choice(path, procs)
        if choice in ("exit", "noprompt"):
            raise InstallError(locked_exit_message(path, procs, choice == "exit"))
        if choice == "kill":
            say("  Stopping %d process(es) running from %s ..." % (len(process_ids(procs)), path))
            kill_processes(procs)
            if wait_until_free(path):
                say("  OK  Nothing runs from it now.")
                return
            say("  Something still holds files in it.")
            policy = "ask"  # do not loop on a kill that did not work
        elif choice == "wait":
            try:
                input("Stop the processes, then press Enter to check again ... ")
            except (EOFError, KeyboardInterrupt):
                raise InstallError(locked_exit_message(path, procs, False))


def prepare_venv(venv_dir, python_cmd, dry_run):
    marker = os.path.join(venv_dir, MARKER)
    if os.path.isdir(venv_dir) and not os.path.exists(marker):
        raise InstallError("%s already exists and was not created by this installer. Choose another "
                           "folder with --venv-dir, or remove it yourself." % venv_dir)
    if os.path.exists(marker):
        say("Reusing the existing environment (it will be updated).")
        return
    say("Creating the environment %s" % venv_dir)
    run(python_cmd + ["-m", "venv", venv_dir], dry_run)
    if not dry_run:
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("Created by the lattice-diamond-mcp installer. Safe to delete with --uninstall.\n")


def install_packages(venv_dir, info, dry_run, folder=None):
    folder = folder or HERE
    wheels = os.path.join(folder, "wheels")
    requirements = os.path.join(folder, "requirements.txt")
    if not dry_run and not os.path.isdir(wheels):
        raise InstallError("The wheels folder is missing next to install.py.")
    if not dry_run and not os.path.isfile(requirements):
        raise InstallError("requirements.txt is missing next to install.py. Run the installer from the unpacked zip.")
    say("Installing packages from %s (no network access is used; every wheel is checked against its SHA-256)" % wheels)
    run([venv_python(venv_dir), "-m", "pip", "install", "--no-index", "--find-links", wheels,
         "--disable-pip-version-check", "--require-hashes", "--upgrade", "--force-reinstall", "-r", requirements],
        dry_run)


def verify(venv_dir, info, dry_run):
    say("Checking the installation")
    if dry_run:
        return
    python = venv_python(venv_dir)
    run([python, "-m", "pip", "check"])
    run([python, "-c", "import diamond_mcp, mcp"], quiet=True)
    help_run = run([python] + WHEELS_SERVER_ARGS + ["--help"], quiet=True)
    if "diamond-mcp" not in help_run.stdout:
        raise InstallError("The installed server did not behave as expected.")
    if os.path.exists(legacy_launcher(venv_dir)):
        raise InstallError("%s is still there; this installer installs no launcher program. Remove the environment "
                           "and run the installer again." % legacy_launcher(venv_dir))
    version = run([python, "-c", "from importlib.metadata import version; print(version('lattice-diamond-mcp'))"],
                  quiet=True).stdout.strip()
    if version != info["version"]:
        raise InstallError("Installed version %s but expected %s." % (version, info["version"]))
    say("  OK: lattice-diamond-mcp %s" % version)


def default_install_dir():
    return os.path.join(os.path.expanduser("~"), "diamond-mcp")


def binary_server(install_dir):
    return os.path.join(install_dir, "diamond-mcp.exe" if IS_WINDOWS else "diamond-mcp")


SWAP_ATTEMPTS = 8       # tries to move the new folder into place on Windows
SWAP_PAUSE = 1.0        # seconds before the second try; each further try waits one second longer, up to four


def swap_into_place(staging, install_dir):
    """Make `staging` the folder `install_dir`, replacing what is there.

    Windows refuses to rename a folder while anything inside it is open, and refuses to reuse a name whose old folder is
    still "delete pending". Antivirus and file indexers open freshly written files, so this fails for a moment even when
    no program of ours is running. It is retried; as a last resort the files are copied into place instead of the folder
    being renamed. The first error is raised if nothing works."""
    first = None
    for attempt in range(SWAP_ATTEMPTS if IS_WINDOWS else 1):
        try:
            if os.path.isdir(install_dir):
                shutil.rmtree(install_dir)
            os.rename(staging, install_dir)
            return
        except OSError as exc:
            first = first or exc
            if not IS_WINDOWS:
                raise
            time.sleep(min(SWAP_PAUSE * (1 + attempt), 4 * SWAP_PAUSE))
    try:
        if os.path.isdir(install_dir):
            shutil.rmtree(install_dir)
        shutil.copytree(staging, install_dir, symlinks=True)
        shutil.rmtree(staging, ignore_errors=True)
    except OSError:
        raise first


def files_under(folders, limit=4000):
    """Paths of the files below the folders that exist, at most `limit` of them."""
    found = []
    for folder in folders:
        for base, _, names in os.walk(folder):
            for name in names:
                found.append(os.path.join(base, name))
                if len(found) >= limit:
                    return found
    return found


def open_file_holders(folders):
    """'PID name' of every program that has a file below the folders open, Python or not, from Windows' Restart Manager
    (the service behind "this file is in use by ..."). For diagnosis only: these are as likely to be antivirus or a file
    indexer as anything of ours, so they are named in an error and never offered for stopping. Windows only; returns []
    where it cannot answer and never raises."""
    if not IS_WINDOWS:
        return []
    try:
        import ctypes
        from ctypes import wintypes
        rstrtmgr = ctypes.WinDLL("rstrtmgr")

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        class RM_UNIQUE_PROCESS(ctypes.Structure):
            _fields_ = [("dwProcessId", wintypes.DWORD), ("ProcessStartTime", FILETIME)]

        class RM_PROCESS_INFO(ctypes.Structure):
            _fields_ = [("Process", RM_UNIQUE_PROCESS), ("strAppName", ctypes.c_wchar * 256),
                        ("strServiceShortName", ctypes.c_wchar * 64), ("ApplicationType", ctypes.c_int),
                        ("AppStatus", ctypes.c_ulong), ("TSSessionId", wintypes.DWORD), ("bRestartable", wintypes.BOOL)]

        files = files_under(folders)
        if not files:
            return []
        session, key = wintypes.DWORD(0), ctypes.create_unicode_buffer(33)
        if rstrtmgr.RmStartSession(ctypes.byref(session), 0, key) != 0:
            return []
        try:
            names = (ctypes.c_wchar_p * len(files))(*files)
            if rstrtmgr.RmRegisterResources(session, len(files), names, 0, None, 0, None) != 0:
                return []
            needed, count, reasons = wintypes.UINT(0), wintypes.UINT(0), wintypes.DWORD(0)
            result = rstrtmgr.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), None, ctypes.byref(reasons))
            if result != 234 or not needed.value:      # 234 = ERROR_MORE_DATA: there are holders, and this many
                return []
            count = wintypes.UINT(needed.value)
            holders = (RM_PROCESS_INFO * needed.value)()
            if rstrtmgr.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), holders, ctypes.byref(reasons)) != 0:
                return []
            return ["%d %s" % (holders[i].Process.dwProcessId, holders[i].strAppName) for i in range(count.value)]
        finally:
            rstrtmgr.RmEndSession(session)
    except Exception:  # nosec B110 - a diagnostic must never make an install fail
        return []


def holders_of(*folders):
    """'PID name' of every process running from any of the folders that exist."""
    found = []
    for folder in folders:
        if os.path.isdir(folder):
            found += pids_running_from(folder)
    return found


def place_folder(args, source, install_dir, what="program", retried=False):
    """Copy `source` to `install_dir` (replacing a folder this installer made, refusing any other), marked as ours.
    Returns False on a dry run, when nothing was copied."""
    marker = os.path.join(install_dir, MARKER)
    if os.path.isdir(install_dir) and not os.path.exists(marker) and os.listdir(install_dir):
        raise InstallError("%s already exists and was not created by this installer. Choose another "
                           "folder with --install-dir, or remove it yourself." % install_dir)
    if not args.dry_run and os.path.exists(marker):
        free_directory(args, install_dir)
    say("Copying the %s to %s" % (what, install_dir))
    if args.dry_run:
        say("  [dry run] copy %s -> %s" % (source, install_dir))
        return False

    staging = install_dir + ".installing"
    try:
        if os.path.isdir(staging):
            shutil.rmtree(staging)
        shutil.copytree(source, staging, symlinks=True)
        with open(os.path.join(staging, MARKER), "w", encoding="utf-8") as handle:
            handle.write("Created by the lattice-diamond-mcp installer. Safe to delete with --uninstall.\n")
        swap_into_place(staging, install_dir)
    except OSError as exc:
        holders = holders_of(install_dir, staging)
        others = [h for h in open_file_holders([install_dir, staging]) if h.split()[0] not in {x.split()[0] for x in holders}]
        shutil.rmtree(staging, ignore_errors=True)
        if holders and not retried:
            # A server started, or began holding files, after the check before the copy: ask the same question again
            # (stop them, wait while the user does, or exit), then do the copy once more.
            say("Something is running from the folder now: %s" % ", ".join(holders))
            free_directory(args, install_dir)
            return place_folder(args, source, install_dir, what, True)
        if holders:
            raise InstallError("Could not install into %s (%s). Still running from it: %s. Close your AI client or stop "
                               "those processes and run the installer again." % (install_dir, exc, ", ".join(holders)))
        if others:
            raise InstallError(
                "Could not install into %s (%s).\nNo MCP server was found running from it, but Windows reports that these "
                "programs have files in it open: %s.\nIf one is a security product (Windows Defender is MsMpEng.exe) or the "
                "Windows Search indexer, it is scanning the new files: wait a minute and run the installer again, or exclude "
                "%s from real-time scanning. If it is a shell, Explorer window or editor, close it." %
                (install_dir, exc, ", ".join(others[:8]), os.path.dirname(install_dir)))
        raise InstallError(
            "Could not install into %s (%s).\nNo MCP server was found running from it, and Windows did not say which program "
            "has files open there. This is usually antivirus (Windows Defender) or a file indexer still scanning the new "
            "files. Wait a minute and run the installer again; if it keeps happening, exclude %s from real-time scanning. If "
            "your AI client has the server running from an earlier install, close it too." %
            (install_dir, exc, os.path.dirname(install_dir)))
    return True


def install_binary_bundle(args, info, folder=None):
    """Copy the self-contained program folder into place and check that it starts. Returns its path."""
    install_dir = os.path.abspath(args.install_dir or default_install_dir())
    source = os.path.join(folder or HERE, "diamond-mcp")
    server = binary_server(install_dir)
    say("Installing lattice-diamond-mcp %s (self-contained; no Python is needed to run it)." % info["version"])
    if not args.dry_run and not os.path.isfile(binary_server(source)):
        raise InstallError("The diamond-mcp folder is missing next to install.py. Run the installer from the unpacked zip.")
    if not place_folder(args, source, install_dir):
        return server
    if not IS_WINDOWS:
        os.chmod(server, os.stat(server).st_mode | 0o755)  # some unzip tools drop the execute bit

    say("Checking the installation")
    help_run = run([server, "--help"], quiet=True)
    if "diamond-mcp" not in help_run.stdout:
        raise InstallError("The installed diamond-mcp program did not behave as expected.")
    say("  OK: diamond-mcp %s" % info["version"])
    return server


def embedded_python(install_dir):
    return os.path.join(install_dir, "python.exe")


def default_embedded_dir():
    return os.path.join(os.path.dirname(default_venv_dir()), "python")


def install_embedded_bundle(args, info, folder=None):
    """Windows: copy python.org's embeddable Python, with the packages inside it, into place and check that the
    server starts. There is no program of ours in it. Returns the path of python.exe."""
    install_dir = os.path.abspath(args.install_dir or default_embedded_dir())
    source = os.path.join(folder or HERE, "python")
    python = embedded_python(install_dir)
    say("Installing lattice-diamond-mcp %s with the bundled Python %s (nothing else needs to be installed)."
        % (info["version"], info.get("python", "?")))
    if not args.dry_run and not os.path.isfile(embedded_python(source)):
        raise InstallError("The python folder is missing next to install.py. Run the installer from the unpacked zip.")
    if not place_folder(args, source, install_dir, "bundled Python"):
        return python
    say("Checking the installation")
    check = run([python] + WHEELS_SERVER_ARGS + ["--help"], quiet=True)
    if "diamond-mcp" not in check.stdout:
        raise InstallError("The installed server did not behave as expected.")
    say("  OK: lattice-diamond-mcp %s" % info["version"])
    return python


# -------------------------------------------------------------- claude client

def find_claude(explicit=None):
    if explicit:
        return explicit if os.path.exists(explicit) else None
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    pattern = os.path.expanduser(
        os.path.join("~", ".vscode-server", "extensions", "anthropic.claude-code-*", "resources", "native-binary", "claude")
    )
    found = sorted(glob.glob(pattern))
    return found[-1] if found else None


def registered_command(claude, name):
    """Return the text of `claude mcp get`, or None if the server is not registered."""
    result = run([claude, "mcp", "get", name], check=False, quiet=True)
    return result.stdout if result.returncode == 0 else None


def register(claude, name, scope, server, diamond_exe, dry_run, server_args=(), replaces=()):
    """Register `server` (plus `server_args`) with Claude Code. `replaces` are program paths an earlier
    version of this installer registered for the same installation; such a registration is replaced."""
    existing = None if dry_run else registered_command(claude, name)
    if existing is not None:
        if server in existing:
            say("Already registered with Claude Code as '%s'." % name)
            return True
        if any(old in existing for old in replaces):
            say("Replacing the registration '%s' made by an earlier install of this program." % name)
            run([claude, "mcp", "remove", name, "--scope", scope], check=False)
        else:
            raise InstallError(
                "A server named '%s' is already registered with Claude Code and does not point to this "
                "installation. Remove it yourself (claude mcp remove %s) or choose another name with --name."
                % (name, name))
    # The name must precede -e: -e accepts several values and would swallow it.
    command = [claude, "mcp", "add", "--scope", scope, name]
    if diamond_exe:
        command += ["-e", "DIAMOND_EXE=" + diamond_exe]
    command += ["--", server] + list(server_args)
    run(command, dry_run)
    say("Registered with Claude Code as '%s' (scope: %s)." % (name, scope))
    return True


def retire_old_name(claude, name, scope, server, dry_run, replaces=()):
    """Remove a registration under the old default name, but only if it points at this program, so that
    one installation is not registered twice. Not done when the user chose the old name themselves."""
    if name == OLD_DEFAULT_NAME or dry_run:
        return
    existing = registered_command(claude, OLD_DEFAULT_NAME)
    if existing is None:
        return
    ours = [server, binary_server(default_install_dir()), venv_python(previous_venv_dir()),
            legacy_launcher(previous_venv_dir())] + list(replaces)
    if any(path in existing for path in ours):
        run([claude, "mcp", "remove", OLD_DEFAULT_NAME, "--scope", scope], check=False)
        say("Removed the earlier registration '%s'; the server is now registered as '%s'." % (OLD_DEFAULT_NAME, name))


def unregister(claude, name, scope, server, dry_run, replaces=()):
    existing = None if dry_run else registered_command(claude, name)
    if existing is None and not dry_run:
        say("Not registered with Claude Code under the name '%s'." % name)
        return
    if not dry_run and server not in existing and not any(old in existing for old in replaces):
        say("The server '%s' registered with Claude Code is not from this installation; leaving it alone." % name)
        return
    run([claude, "mcp", "remove", name, "--scope", scope], dry_run, check=False)
    say("Removed the Claude Code registration '%s'." % name)


# ----------------------------------------------------------------------- main

def default_venv_dir():
    """The per-user data folder, as for the other Lattice MCP installers (no spaces-prone or roaming location)."""
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "lattice-diamond-mcp", "venv")


def previous_venv_dir():
    """Where earlier versions of this installer created the environment."""
    return os.path.join(os.path.expanduser("~"), "diamond-mcp-venv")


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="install.py",
        description="Install or remove the Lattice Diamond MCP server.",
    )
    parser.add_argument("--venv-dir",
                        help="Wheels zip: where to create the environment (default: %s)." % default_venv_dir())
    parser.add_argument("--install-dir",
                        help="Standalone zip: where to install the program (default: %s; on Windows with the bundled Python: %s)."
                             % (default_install_dir(), default_embedded_dir()))
    parser.add_argument("--python", help="Wheels zip: path to the Python interpreter to use (must match the version it was built for).")
    parser.add_argument("--no-register", action="store_true", help="Install only; do not register with Claude Code.")
    parser.add_argument("--claude-path", help="Path to the claude executable (default: found on PATH or in the VS Code extension).")
    parser.add_argument("--name", default=DEFAULT_NAME, help="Name to register the server under (default: %(default)s).")
    parser.add_argument("--scope", choices=("user", "local", "project"), default="user",
                        help="Claude Code configuration scope (default: %(default)s).")
    parser.add_argument("--diamond-exe", help="Diamond console executable to store in the registration (DIAMOND_EXE).")
    parser.add_argument("--if-locked", choices=IF_LOCKED_CHOICES, default="",
                        help="When a running MCP server holds the folder: ask (default; prompts, and exits if nobody can "
                             "answer), kill (stop those processes and continue) or exit (change nothing). "
                             "Also DIAMOND_MCP_IF_LOCKED.")
    parser.add_argument("--uninstall", action="store_true", help="Remove the registration and the installation.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without doing it.")
    return parser.parse_args(argv)


def install_wheel_bundle(args, info, folder=None):
    wanted = info["python"]
    python_cmd = find_python(wanted, args.python)
    say("Installing lattice-diamond-mcp %s with Python %s (%s)." % (info["version"], wanted, " ".join(python_cmd)))
    venv_dir = os.path.abspath(args.venv_dir or default_venv_dir())
    if not args.dry_run and os.path.exists(os.path.join(venv_dir, MARKER)):
        free_directory(args, venv_dir)  # before pip touches it; a folder that is not ours is refused below
    prepare_venv(venv_dir, python_cmd, args.dry_run)
    install_packages(venv_dir, info, args.dry_run, folder)
    verify(venv_dir, info, args.dry_run)
    return venv_python(venv_dir)


def do_install(args, info, folder=None):
    check_platform(info)
    checked = verify_manifest(folder or HERE)
    say("Integrity: all %d files match %s." % (checked, MANIFEST_NAME) if checked is not None
        else "Integrity: this package has no %s, so its files were not checked." % MANIFEST_NAME)
    if folder and os.path.abspath(folder) != os.path.abspath(HERE):
        say("This zip has several builds; using the one for %s%s (%s)." % (
            info.get("platform", "this machine"), ", Python " + info["python"] if info.get("kind") not in SELF_CONTAINED and info.get("python") else "",
            os.path.basename(folder)))
    if info.get("kind") == "binary":
        server, server_args, replaces = install_binary_bundle(args, info, folder), [], []
    elif info.get("kind") == "embedded":
        server, server_args, replaces = install_embedded_bundle(args, info, folder), WHEELS_SERVER_ARGS, []
    else:
        server, server_args = install_wheel_bundle(args, info, folder), WHEELS_SERVER_ARGS
        replaces = [legacy_launcher(os.path.dirname(os.path.dirname(server)))]

    say()
    registered = False
    if args.no_register:
        say("Skipping registration (--no-register).")
    else:
        claude = find_claude(args.claude_path)
        if claude is None:
            say("The claude command was not found, so the server was not registered.")
        else:
            registered = register(claude, args.name, args.scope, server, args.diamond_exe, args.dry_run, server_args, replaces)
            if registered:
                retire_old_name(claude, args.name, args.scope, server, args.dry_run, replaces)

    say()
    say("Done. The server command is:")
    say("  " + " ".join([server] + server_args))
    if not registered:
        say()
        say("To register it with Claude Code later:")
        say("  claude mcp add --scope user %s -- %s" % (args.name, " ".join(shlex.quote(part) for part in [server] + server_args)))
        say("For another MCP client, use that command as a stdio server.")
    say()
    say("Next: restart your AI client, then ask it to run check_environment for the Diamond server.")
    say("If Diamond is not found automatically, set DIAMOND_EXE (see the user guide).")


def uninstall_targets(args):
    """(kind, directory) pairs to remove. A folder given on the command line is always the target."""
    if args.install_dir:
        folder = os.path.abspath(args.install_dir)
        return [("embedded" if os.path.isfile(embedded_python(folder)) and not os.path.isfile(binary_server(folder)) else "binary", folder)]
    if args.venv_dir:
        return [("wheels", os.path.abspath(args.venv_dir))]
    try:
        kind = load_buildinfo().get("kind", "wheels")
    except InstallError:  # run from somewhere other than the unpacked zip: look at both default places
        return [("binary", default_install_dir()), ("embedded", default_embedded_dir()),
                ("wheels", default_venv_dir()), ("wheels", previous_venv_dir())]
    if kind == "binary":
        return [("binary", default_install_dir())]
    if kind == "embedded":
        return [("embedded", default_embedded_dir())]
    return [("wheels", default_venv_dir()), ("wheels", previous_venv_dir())]


def do_uninstall(args):
    claude = find_claude(args.claude_path)
    if claude is None:
        say("The claude command was not found; skipping the Claude Code registration.")
    explicit = bool(args.install_dir or args.venv_dir)
    removed_any = False
    for kind, directory in uninstall_targets(args):
        if not explicit and not os.path.isdir(directory):
            continue
        server = {"binary": binary_server, "embedded": embedded_python}.get(kind, venv_python)(directory)
        if not args.dry_run and os.path.exists(os.path.join(directory, MARKER)):
            free_directory(args, directory)  # first: leaving the registration removed but the files in place helps nobody
        if claude is not None:
            unregister(claude, args.name, args.scope, server, args.dry_run,
                       replaces=[legacy_launcher(directory)] if kind == "wheels" else [])
            retire_old_name(claude, args.name, args.scope, server, args.dry_run,
                            replaces=[legacy_launcher(directory)] if kind == "wheels" else [])
        marker = os.path.join(directory, MARKER)
        if not os.path.isdir(directory):
            say("Nothing installed at %s." % directory)
        elif not os.path.exists(marker):
            raise InstallError("%s was not created by this installer, so it was left alone." % directory)
        elif args.dry_run:
            say("  [dry run] remove %s" % directory)
            removed_any = True
        else:
            try:
                shutil.rmtree(directory)
            except OSError as exc:
                raise InstallError("Could not remove %s (%s). If the server is running, close your AI client "
                                   "and try again." % (directory, exc))
            say("Removed %s." % directory)
            removed_any = True
    if not removed_any and not explicit:
        say("No installation was found in the default locations.")
    say("Not removed: your audit log, config file and projects (see the user guide to delete them).")


def main(argv=None):
    args = parse_args(argv)
    try:
        if args.uninstall:
            do_uninstall(args)
        else:
            folder, info = select_bundle(args.python)
            do_install(args, info, folder)
    except InstallError as exc:
        print("\nERROR: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
