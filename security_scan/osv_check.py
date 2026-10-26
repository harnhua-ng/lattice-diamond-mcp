#!/usr/bin/env python3
# Copyright(c) 2026 Lattice Semiconductor Corporation. All rights reserved. DO NOT DISTRIBUTE.
# Subject to the terms of the AI Solutions Software License Agreement
"""
Ask OSV.dev whether any package pinned in uv.lock has a known advisory.

    python security_scan/osv_check.py [--lock uv.lock] [--out osv_check.json] [--strict]

Every locked package is sent to https://api.osv.dev/v1/querybatch (ecosystem PyPI, exact version).
Packages are split into two groups: RUNTIME (reachable from the project's own dependencies, so they
end up in the installers) and BUILD (only needed to build, for example PyInstaller). An advisory on
a runtime package fails the check; one on a build-only package is listed and fails only with
--strict. Uses the standard library and needs network access to api.osv.dev.

Exit status: 0 nothing to report, 1 an advisory was found, 2 OSV could not be reached.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

try:
    import tomllib
except ImportError:  # Python 3.10 and older
    sys.exit("osv_check.py needs Python 3.11 or later (it reads uv.lock with tomllib).")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = "https://api.osv.dev/v1/querybatch"
CHUNK = 500


def normalise(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def read_lock(path):
    """Return ({(name, version)}: runtime, {(name, version)}: build only, project name)."""
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    packages = {normalise(p["name"]): p for p in data["package"]}
    project = next(p for p in data["package"] if "editable" in p.get("source", {}) or "virtual" in p.get("source", {}))
    runtime, todo = set(), [normalise(d["name"]) for d in project.get("dependencies", [])]
    while todo:
        name = todo.pop()
        if name in runtime or name not in packages:
            continue
        runtime.add(name)
        todo.extend(normalise(d["name"]) for d in packages[name].get("dependencies", []))
    every = {n: p for n, p in packages.items() if n != normalise(project["name"])}
    return ({(n, p["version"]) for n, p in every.items() if n in runtime},
            {(n, p["version"]) for n, p in every.items() if n not in runtime}, project["name"])


def query(pairs):
    results = []
    for start in range(0, len(pairs), CHUNK):
        batch = pairs[start:start + CHUNK]
        body = json.dumps({"queries": [{"package": {"name": n, "ecosystem": "PyPI"}, "version": v} for n, v in batch]}).encode()
        request = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
        try:
            # URL is the fixed https address above, never user input.
            with urllib.request.urlopen(request, timeout=60) as response:  # nosec B310
                answer = json.load(response)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ConnectionError("Could not query OSV: %s" % exc)
        results.extend(answer.get("results", []))
    if len(results) != len(pairs):
        raise ConnectionError("OSV answered %d results for %d packages." % (len(results), len(pairs)))
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check uv.lock against OSV.dev.")
    parser.add_argument("--lock", default=os.path.join(REPO, "uv.lock"))
    parser.add_argument("--out", help="write the result as JSON to this file")
    parser.add_argument("--strict", action="store_true", help="also fail on advisories in build-only packages")
    args = parser.parse_args(argv)

    runtime, build, project = read_lock(args.lock)
    pairs = sorted(runtime) + sorted(build)
    try:
        results = query(pairs)
    except ConnectionError as exc:
        print("osv check FAILED to run: %s" % exc, file=sys.stderr)
        return 2
    findings = []
    for (name, version), result in zip(pairs, results):
        ids = [v["id"] for v in result.get("vulns", [])]
        if ids:
            findings.append({"package": name, "version": version, "group": "runtime" if (name, version) in runtime else "build",
                             "advisories": ids})
    report = {"tool": "OSV.dev /v1/querybatch", "lock": os.path.basename(args.lock), "project": project,
              "runtime_packages": len(runtime), "build_only_packages": len(build), "findings": findings}
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
    print("osv check: %d runtime and %d build-only packages, %d with advisories" % (len(runtime), len(build), len(findings)))
    for f in findings:
        print("  [%(group)s] %(package)s %(version)s: %(advisories)s" % {**f, "advisories": ", ".join(f["advisories"])})
    failing = [f for f in findings if f["group"] == "runtime" or args.strict]
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main())
