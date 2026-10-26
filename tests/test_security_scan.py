"""Tests that the scanners in security_scan/ detect what they claim to, and fail when they should."""

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

SCAN = Path(__file__).resolve().parents[1] / "security_scan"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCAN / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


secrets_scan = load("secrets_scan")
osv_check = load("osv_check")
dast_parsers = load("dast_parsers")

# Built from pieces so that this file does not itself look like a leaked secret.
FAKE_SECRETS = {
    "private-key-block": "-----BEGIN RSA " + "PRIVATE KEY-----",
    "aws-access-key-id": "key = AK" + "IAABCDEFGHIJKLMNOP",
    "github-token": "t = gh" + "p_" + "a" * 36,
    "slack-token": "x" + "oxb-" + "1234567890-abcdefghij",
    "url-with-credentials": "postgres://admin:" + "hunter22@db.example.com/x",
    "secret-assignment": 'api_key = "' + "abcd1234efgh5678" + '"',
    "jwt": "eyJ" + "a" * 12 + ".eyJ" + "b" * 12 + "." + "c" * 12,
}


@pytest.mark.parametrize("pattern, line", sorted(FAKE_SECRETS.items()))
def test_each_secret_format_is_detected_and_redacted(tmp_path, pattern, line):
    target = tmp_path / "leak.txt"
    target.write_text("harmless\n" + line + "\n")
    hits, scanned = secrets_scan.scan([str(target)])
    assert scanned == 1
    assert [h["pattern"] for h in hits if h["pattern"] == pattern] == [pattern]
    assert all(h["line"] == 2 for h in hits)
    assert all(len(h["match"]) <= 9 for h in hits)  # never the whole secret


def test_clean_files_and_marked_lines_are_not_reported(tmp_path):
    clean = tmp_path / "clean.py"
    clean.write_text('password = os.environ["PW"]\ntoken = "${TOKEN}"\nname = "plain text here"\n')
    marked = tmp_path / "fixture.py"
    marked.write_text(FAKE_SECRETS["secret-assignment"] + "  # secrets-scan: allow (test fixture)\n")
    hits, _ = secrets_scan.scan([str(clean), str(marked)])
    assert hits == []


def test_the_secrets_scan_exits_1_on_a_hit_and_0_otherwise(tmp_path):
    leak = tmp_path / "leak.txt"
    leak.write_text(FAKE_SECRETS["aws-access-key-id"] + "\n")
    assert secrets_scan.main([str(leak), "--out", str(tmp_path / "r.json")]) == 1
    assert json.loads((tmp_path / "r.json").read_text())["hits"][0]["pattern"] == "aws-access-key-id"
    leak.write_text("nothing here\n")
    assert secrets_scan.main([str(leak)]) == 0


def test_the_repository_itself_has_no_secrets():
    assert secrets_scan.main([]) == 0


# ----------------------------------------------------------------------- osv

LOCK = """
version = 1
requires-python = ">=3.11"

[[package]]
name = "app"
version = "0.1.0"
source = { editable = "." }
dependencies = [{ name = "runtime-a" }]

[package.dev-dependencies]
build = [{ name = "buildtool" }]

[[package]]
name = "runtime-a"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "runtime-b" }]

[[package]]
name = "runtime-b"
version = "2.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "buildtool"
version = "3.0"
source = { registry = "https://pypi.org/simple" }
"""


@pytest.fixture
def lock(tmp_path):
    path = tmp_path / "uv.lock"
    path.write_text(LOCK)
    return path


def test_the_lock_is_split_into_runtime_and_build_only_packages(lock):
    runtime, build, project = osv_check.read_lock(lock)
    assert runtime == {("runtime-a", "1.0"), ("runtime-b", "2.0")}
    assert build == {("buildtool", "3.0")} and project == "app"


def fake_osv(monkeypatch, vulnerable):
    def query(pairs):
        return [{"vulns": [{"id": "GHSA-test-0001"}]} if name in vulnerable else {} for name, _version in pairs]
    monkeypatch.setattr(osv_check, "query", query)


def test_an_advisory_on_a_runtime_package_fails(lock, monkeypatch, tmp_path, capsys):
    fake_osv(monkeypatch, {"runtime-b"})
    assert osv_check.main(["--lock", str(lock), "--out", str(tmp_path / "o.json")]) == 1
    finding = json.loads((tmp_path / "o.json").read_text())["findings"][0]
    assert (finding["package"], finding["group"], finding["advisories"]) == ("runtime-b", "runtime", ["GHSA-test-0001"])


def test_an_advisory_on_a_build_only_package_fails_only_when_strict(lock, monkeypatch):
    fake_osv(monkeypatch, {"buildtool"})
    assert osv_check.main(["--lock", str(lock)]) == 0
    assert osv_check.main(["--lock", str(lock), "--strict"]) == 1


def test_no_advisories_passes(lock, monkeypatch):
    fake_osv(monkeypatch, set())
    assert osv_check.main(["--lock", str(lock)]) == 0


def test_an_unreachable_osv_is_reported_as_an_error_not_as_clean(lock, monkeypatch, capsys):
    def unreachable(pairs):
        raise ConnectionError("no route")
    monkeypatch.setattr(osv_check, "query", unreachable)
    assert osv_check.main(["--lock", str(lock)]) == 2
    assert "FAILED to run" in capsys.readouterr().err


def test_the_real_lock_file_can_be_read():
    runtime, build, project = osv_check.read_lock(SCAN.parent / "uv.lock")
    assert project == "lattice-diamond-mcp" and any(n == "mcp" for n, _ in runtime) and any(n == "pyinstaller" for n, _ in build)


# -------------------------------------------------------------------- fuzzing

def test_mutations_are_repeatable_for_a_seed():
    import random
    text = "line one\nline two\nline three\n"
    one, two = random.Random(5), random.Random(5)
    first = [dast_parsers.mutate(one, text) for _ in range(20)]
    assert first == [dast_parsers.mutate(two, text) for _ in range(20)]
    assert len(set(first)) > 1


@pytest.mark.skipif(shutil.which("tclsh") is None, reason="tclsh is not installed")
def test_the_tcl_quote_check_catches_a_quoter_that_does_not_quote():
    import random
    from diamond_mcp.diamond_session import tcl_quote
    assert dast_parsers.tcl_quote_check(tcl_quote, random.Random(1), 200)[0] == []
    bad, _ = dast_parsers.tcl_quote_check(lambda value: value, random.Random(1), 200)
    assert bad, "a quoter that returns its input must be reported"
    bad, _ = dast_parsers.tcl_quote_check(lambda value: "{" + value + "}", random.Random(1), 200)
    assert bad, "brace quoting without escaping must be reported"


def test_a_slow_target_is_reported(monkeypatch):
    monkeypatch.setattr(dast_parsers, "SLOW_SECONDS", 0.0)
    target = dast_parsers.Target("x", lambda value: sum(range(10000)))
    target.feed("one", "a")
    assert target.summary()["finding"] and target.summary()["slow_calls"]


def test_an_unexpected_exception_is_reported_and_an_allowed_one_is_not():
    target = dast_parsers.Target("x", lambda value: 1 / 0, allowed=(ZeroDivisionError,))
    target.feed("one", "a")
    assert not target.summary()["finding"]
    target = dast_parsers.Target("x", lambda value: {}["missing"])
    target.feed("one", "a")
    assert target.summary()["unexpected_exceptions"] == {"KeyError": 1}
