#!/usr/bin/env python3
# Copyright(c) 2026 Lattice Semiconductor Corporation. All rights reserved. DO NOT DISTRIBUTE.
# Subject to the terms of the AI Solutions Software License Agreement
"""
Installer for the Lattice Diamond MCP server (lattice-diamond-mcp).

Run it from the unpacked installer zip:

    python install.py                      install, then register with Claude Code
    python install.py --no-register        install only
    python install.py --uninstall          remove the registration and the installation

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

This file uses only the standard library and runs on Python 3.8 or later.
"""

import argparse
import glob
import json
import os
import shlex
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
IS_WINDOWS = os.name == "nt"
MARKER = ".diamond-mcp-installer"
DEFAULT_NAME = "diamond"
# How the wheels install starts the server, after the environment's python.
WHEELS_SERVER_ARGS = ["-I", "-m", "diamond_mcp"]


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
    if all(info.get("kind") == "binary" for _, info in fitting):
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


def install_binary_bundle(args, info, folder=None):
    """Copy the self-contained program folder into place and check that it starts. Returns its path."""
    install_dir = os.path.abspath(args.install_dir or default_install_dir())
    source = os.path.join(folder or HERE, "diamond-mcp")
    server = binary_server(install_dir)
    say("Installing lattice-diamond-mcp %s (self-contained; no Python is needed to run it)." % info["version"])
    if not args.dry_run and not os.path.isfile(binary_server(source)):
        raise InstallError("The diamond-mcp folder is missing next to install.py. Run the installer from the unpacked zip.")

    marker = os.path.join(install_dir, MARKER)
    if os.path.isdir(install_dir) and not os.path.exists(marker) and os.listdir(install_dir):
        raise InstallError("%s already exists and was not created by this installer. Choose another "
                           "folder with --install-dir, or remove it yourself." % install_dir)
    say("Copying the program to %s" % install_dir)
    if args.dry_run:
        say("  [dry run] copy %s -> %s" % (source, install_dir))
        return server

    staging = install_dir + ".installing"
    try:
        if os.path.isdir(staging):
            shutil.rmtree(staging)
        shutil.copytree(source, staging, symlinks=True)
        with open(os.path.join(staging, MARKER), "w", encoding="utf-8") as handle:
            handle.write("Created by the lattice-diamond-mcp installer. Safe to delete with --uninstall.\n")
        if os.path.isdir(install_dir):
            shutil.rmtree(install_dir)
        os.rename(staging, install_dir)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise InstallError("Could not install into %s (%s). If the server is running, close your AI client "
                           "and try again." % (install_dir, exc))
    if not IS_WINDOWS:
        os.chmod(server, os.stat(server).st_mode | 0o755)  # some unzip tools drop the execute bit

    say("Checking the installation")
    help_run = run([server, "--help"], quiet=True)
    if "diamond-mcp" not in help_run.stdout:
        raise InstallError("The installed diamond-mcp program did not behave as expected.")
    say("  OK: diamond-mcp %s" % info["version"])
    return server


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
    return os.path.join(os.path.expanduser("~"), "diamond-mcp-venv")


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="install.py",
        description="Install or remove the Lattice Diamond MCP server.",
    )
    parser.add_argument("--venv-dir",
                        help="Wheels zip: where to create the environment (default: %s)." % default_venv_dir())
    parser.add_argument("--install-dir",
                        help="Binary zip: where to install the program (default: %s)." % default_install_dir())
    parser.add_argument("--python", help="Wheels zip: path to the Python interpreter to use (must match the version it was built for).")
    parser.add_argument("--no-register", action="store_true", help="Install only; do not register with Claude Code.")
    parser.add_argument("--claude-path", help="Path to the claude executable (default: found on PATH or in the VS Code extension).")
    parser.add_argument("--name", default=DEFAULT_NAME, help="Name to register the server under (default: %(default)s).")
    parser.add_argument("--scope", choices=("user", "local", "project"), default="user",
                        help="Claude Code configuration scope (default: %(default)s).")
    parser.add_argument("--diamond-exe", help="Diamond console executable to store in the registration (DIAMOND_EXE).")
    parser.add_argument("--uninstall", action="store_true", help="Remove the registration and the installation.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without doing it.")
    return parser.parse_args(argv)


def install_wheel_bundle(args, info, folder=None):
    wanted = info["python"]
    python_cmd = find_python(wanted, args.python)
    say("Installing lattice-diamond-mcp %s with Python %s (%s)." % (info["version"], wanted, " ".join(python_cmd)))
    venv_dir = os.path.abspath(args.venv_dir or default_venv_dir())
    prepare_venv(venv_dir, python_cmd, args.dry_run)
    install_packages(venv_dir, info, args.dry_run, folder)
    verify(venv_dir, info, args.dry_run)
    return venv_python(venv_dir)


def do_install(args, info, folder=None):
    check_platform(info)
    if folder and os.path.abspath(folder) != os.path.abspath(HERE):
        say("This zip has several builds; using the one for %s%s (%s)." % (
            info.get("platform", "this machine"), ", Python " + info["python"] if info.get("kind") != "binary" and info.get("python") else "",
            os.path.basename(folder)))
    if info.get("kind") == "binary":
        server, server_args, replaces = install_binary_bundle(args, info, folder), [], []
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
        return [("binary", os.path.abspath(args.install_dir))]
    if args.venv_dir:
        return [("wheels", os.path.abspath(args.venv_dir))]
    try:
        kind = load_buildinfo().get("kind", "wheels")
    except InstallError:  # run from somewhere other than the unpacked zip: look at both default places
        return [("binary", default_install_dir()), ("wheels", default_venv_dir())]
    return [("binary", default_install_dir())] if kind == "binary" else [("wheels", default_venv_dir())]


def do_uninstall(args):
    claude = find_claude(args.claude_path)
    if claude is None:
        say("The claude command was not found; skipping the Claude Code registration.")
    explicit = bool(args.install_dir or args.venv_dir)
    removed_any = False
    for kind, directory in uninstall_targets(args):
        if not explicit and not os.path.isdir(directory):
            continue
        server = binary_server(directory) if kind == "binary" else venv_python(directory)
        if claude is not None:
            unregister(claude, args.name, args.scope, server, args.dry_run,
                       replaces=[] if kind == "binary" else [legacy_launcher(directory)])
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
