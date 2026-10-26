#!/usr/bin/env python3
# Copyright(c) 2026 Lattice Semiconductor Corporation. All rights reserved. DO NOT DISTRIBUTE.
# Subject to the terms of the AI Solutions Software License Agreement
"""
Look for secrets in the files of the working tree.

    python security_scan/secrets_scan.py [--out secrets_scan.json] [PATH ...]

Nine regular expressions: private key blocks, cloud and token formats, URLs with credentials, and
key/password/token assignments of a string literal. It reads the working tree (the files git tracks plus new
ones that are not ignored, or every file below PATH when git is not available), not git history. It is a
stand-in for detect-secrets or gitleaks, not a replacement.

A line that carries the text `secrets-scan: allow` is skipped; use it with a reason, for a test
fixture. Exit status: 0 no hits, 1 at least one hit.
"""

import argparse
import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALLOW = "secrets-scan: allow"
MAX_BYTES = 2_000_000
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", "out", "results"}
SKIP_SUFFIXES = (".pdf", ".zip", ".png", ".jpg", ".gif", ".whl", ".exe", ".dll", ".so", ".pyc", ".lock")

PATTERNS = {
    "private-key-block": re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----"),
    "aws-access-key-id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "github-token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{50,}\b"),
    "slack-token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    "google-api-key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "anthropic-or-openai-key": re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{32,}\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    "url-with-credentials": re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s:@]+@[^/\s]+", re.IGNORECASE),
    "secret-assignment": re.compile(
        r"""(?ix)\b(?:password|passwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token|token)\b\s*[:=]\s*
            (?P<q>['"])(?!\s*(?P=q))(?![^'"]{0,3}(?:\$\{|%\(|\{\{|<))(?P<value>[^'"\n]{8,})(?P=q)"""),
}


def tracked_files(roots):
    if not roots:
        try:
            out = subprocess.run(["git", "-C", REPO, "ls-files", "-z", "--cached", "--others", "--exclude-standard"], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, check=True).stdout.decode("utf-8", "replace")
            return [os.path.join(REPO, name) for name in out.split("\0") if name]
        except (OSError, subprocess.CalledProcessError):
            roots = [REPO]
    files = []
    for root in roots:
        if os.path.isfile(root):
            files.append(root)
            continue
        for base, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            files.extend(os.path.join(base, n) for n in names)
    return files


def scan(files):
    hits = []
    scanned = 0
    for path in files:
        if path.lower().endswith(SKIP_SUFFIXES) or not os.path.isfile(path) or os.path.getsize(path) > MAX_BYTES:
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        scanned += 1
        for number, line in enumerate(lines, 1):
            if ALLOW in line:
                continue
            for name, pattern in PATTERNS.items():
                match = pattern.search(line)
                if match:
                    text = match.group(0)
                    hits.append({"file": os.path.relpath(path, REPO).replace(os.sep, "/"), "line": number,
                                 "pattern": name, "match": text[:4] + "..." + text[-2:] if len(text) > 10 else "..."})
    return hits, scanned


def main(argv=None):
    parser = argparse.ArgumentParser(description="Scan tracked files for secrets.")
    parser.add_argument("paths", nargs="*", help="files or folders to scan (default: the files git tracks, plus new unignored ones)")
    parser.add_argument("--out", help="write the result as JSON to this file")
    args = parser.parse_args(argv)
    hits, scanned = scan(tracked_files(args.paths))
    result = {"tool": "secrets_scan.py", "patterns": sorted(PATTERNS), "files_scanned": scanned, "hits": hits}
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)
    print("secrets scan: %d file(s), %d hit(s)" % (scanned, len(hits)))
    for hit in hits:
        print("  %(file)s:%(line)s  %(pattern)s  %(match)s" % hit)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
