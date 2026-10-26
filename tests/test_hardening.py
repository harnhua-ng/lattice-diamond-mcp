"""Tests for the security hardening: report confinement, console path, stdio only, DTD refusal."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from diamond_mcp import config, project as prj, server

SRC = Path(__file__).resolve().parents[1] / "src"


# ------------------------------------------------------------------ read_report

@pytest.fixture
def reports_dir(ldf_project):
    folder = ldf_project.parent / "impl1"
    folder.mkdir(exist_ok=True)
    return folder


def test_read_report_refuses_before_any_project_is_known(ldf_project, reports_dir):
    (reports_dir / "a.par").write_text("report")
    result = server.read_report(str(reports_dir / "a.par"))
    assert not result["success"] and "inside a project" in result["output"]


@pytest.mark.parametrize("registering_call", ["open", "info", "list", "timing"])
def test_read_report_allows_files_of_a_project_once_it_is_known(ldf_project, reports_dir, registering_call, server_session):
    (reports_dir / "a.par").write_text("report text")
    path = str(ldf_project)
    {
        "open": lambda: server.open_project(path),
        "info": lambda: server.get_project_info(path),
        "list": lambda: server.list_reports(path),
        "timing": lambda: server.get_timing_summary(path),
    }[registering_call]()
    result = server.read_report(str(reports_dir / "a.par"))
    assert result["success"] and result["output"] == "report text"


def test_read_report_refuses_a_file_outside_the_project(ldf_project, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "secret.log"
    outside.write_text("not a report of this project")
    server.get_project_info(str(ldf_project))
    result = server.read_report(str(outside))
    assert not result["success"] and "inside a project" in result["output"]


@pytest.mark.skipif(os.name == "nt", reason="needs symlink privilege")
def test_read_report_does_not_follow_a_link_out_of_the_project(ldf_project, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "secret.log"
    outside.write_text("secret")
    (ldf_project.parent / "link.log").symlink_to(outside)
    server.get_project_info(str(ldf_project))
    assert not server.read_report(str(ldf_project.parent / "link.log"))["success"]


def test_a_project_file_cannot_widen_the_readable_area(ldf_project, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere")
    (outside / "secret.log").write_text("secret")
    ldf_project.write_text(ldf_project.read_text().replace('dir="impl1"', 'dir="%s"' % outside.as_posix()))
    server.get_project_info(str(ldf_project))
    assert not server.read_report(str(outside / "secret.log"))["success"]
    assert Path(ldf_project.parent).resolve() in server._REPORT_ROOTS
    assert outside.resolve() not in server._REPORT_ROOTS


def test_read_report_refuses_a_file_over_the_size_cap(ldf_project, monkeypatch):
    big = ldf_project.parent / "big.log"
    big.write_text("x" * 100)
    server.get_project_info(str(ldf_project))
    monkeypatch.setattr(server, "_MAX_REPORT_FILE_BYTES", 50)
    result = server.read_report(str(big))
    assert not result["success"] and "larger than" in result["output"]


@pytest.mark.parametrize("chunk", [1, 7, 64, 1 << 20])
@pytest.mark.parametrize("offset, limit, tail", [(0, 10, False), (95, 10, False), (13, 40, False), (1000, 5, False), (0, 30, True), (0, 500, True)])
def test_the_window_matches_slicing_the_whole_file_whatever_the_chunk_size(tmp_path, monkeypatch, chunk, offset, limit, tail):
    text = "".join(chr(97 + i % 26) for i in range(130)) + "é中" * 5
    path = tmp_path / "t.log"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(server, "_READ_CHUNK_CHARS", chunk)
    got, total, start = server._read_window(path, offset, limit, tail)
    expected_start = max(0, len(text) - limit) if tail else offset
    assert (got, total, start) == (text[expected_start:expected_start + limit], len(text), expected_start)


def test_read_report_pages_through_a_file_larger_than_one_chunk(ldf_project, monkeypatch):
    log = ldf_project.parent / "big.log"
    log.write_text("0123456789" * 50)
    server.get_project_info(str(ldf_project))
    monkeypatch.setattr(server, "_READ_CHUNK_CHARS", 64)
    seen = ""
    offset = 0
    while True:
        page = server.read_report(str(log), offset=offset, max_chars=100)
        seen += page["output"]
        if not page["has_more"]:
            break
        offset += page["returned_chars"]
    assert seen == "0123456789" * 50


# --------------------------------------------------------------- console path

@pytest.fixture
def console_file(tmp_path):
    name = "pnmainc.exe" if os.name == "nt" else "diamondc"
    path = tmp_path / name
    path.write_text("#!/bin/sh\n")
    return path


@pytest.mark.parametrize("name", ["sh", "evil", "diamondc.sh", "not_diamondc", "bash"])
def test_start_diamond_refuses_a_program_that_is_not_the_console(tmp_path, name):
    program = tmp_path / name
    program.write_text("#!/bin/sh\ntouch %s\n" % (tmp_path / "ran"))
    program.chmod(0o755)
    result = server.start_diamond(exe_path=str(program))
    assert not result["success"] and "must be a file named" in result["output"]
    assert not (tmp_path / "ran").exists()


def test_start_diamond_refuses_a_missing_console(tmp_path):
    name = "pnmainc.exe" if os.name == "nt" else "diamondc"
    assert "not found" in server.start_diamond(exe_path=str(tmp_path / name))["output"]


def test_configure_diamond_refuses_other_programs_and_writes_nothing(tmp_path, monkeypatch, isolated_home):
    monkeypatch.setenv("DIAMOND_MCP_CONFIG", str(tmp_path / "config.json"))
    program = tmp_path / "evil"
    program.write_text("#!/bin/sh\n")
    result = server.configure_diamond(diamond_exe=str(program), timeout_seconds=5)
    assert not result["success"] and "must be a file named" in result["output"]
    assert not (tmp_path / "config.json").exists()
    assert not any(isolated_home.rglob("config.json"))


def test_configure_diamond_accepts_the_console_file(tmp_path, monkeypatch, console_file):
    (tmp_path / "config.json").write_text("{}")
    monkeypatch.setenv("DIAMOND_MCP_CONFIG", str(tmp_path / "config.json"))
    assert server.configure_diamond(diamond_exe=str(console_file))["success"]
    assert config.load_config_data(tmp_path / "config.json")["diamond_exe"] == str(console_file)


def test_the_operator_can_still_name_any_program(monkeypatch, fake_exe):
    """DIAMOND_EXE, --diamond-exe and the config file are not agent input, so they are not restricted."""
    monkeypatch.setenv("DIAMOND_EXE", fake_exe)
    assert config.resolve_diamond_path().path == fake_exe


# ------------------------------------------------------------------ stdio only

def _run_cli(*args):
    env = dict(os.environ, PYTHONPATH=str(SRC))
    return subprocess.run([sys.executable, "-m", "diamond_mcp", *args], stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, timeout=60)


@pytest.mark.parametrize("transport", ["sse", "streamable-http", "stdio"])
def test_there_is_no_transport_option(transport):
    done = _run_cli("--transport", transport)
    assert done.returncode == 2 and "unrecognized arguments" in done.stderr


def test_help_does_not_offer_a_network_transport():
    done = _run_cli("--help")
    assert done.returncode == 0
    assert "transport" not in done.stdout and "streamable" not in done.stdout


def test_the_server_module_never_selects_a_network_transport():
    text = (SRC / "diamond_mcp" / "server.py").read_text()
    assert 'mcp.run(transport="stdio")' in text
    assert "streamable" not in text and "transport=args" not in text and '"sse"' not in text


# ------------------------------------------------------------------- DTD refusal

BOMB = (
    '<?xml version="1.0"?>\n<!DOCTYPE BaliProject [\n<!ENTITY a "aaaaaaaaaa">\n'
    '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">\n<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">\n]>\n'
    '<BaliProject title="&c;" device="X"/>\n'
)


@pytest.mark.parametrize("text", [
    BOMB,
    '<!DOCTYPE BaliProject>\n<BaliProject/>',
    '<?xml version="1.0"?><!DOCTYPE BaliProject SYSTEM "file:///etc/passwd"><BaliProject/>',
    '<!DOCTYPE BaliProject [<!ENTITY x SYSTEM "file:///etc/passwd">]><BaliProject t="&x;"/>',
])
def test_a_project_file_with_a_dtd_or_entity_is_refused(tmp_path, text):
    ldf = tmp_path / "x.ldf"
    ldf.write_text(text, encoding="utf-8")
    with pytest.raises(prj.ProjectFileError, match="DTD or an entity"):
        prj.parse_ldf(str(ldf))


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-8-sig"])
def test_the_dtd_check_also_covers_other_encodings(tmp_path, encoding):
    ldf = tmp_path / "x.ldf"
    ldf.write_bytes(('<?xml version="1.0"?><!DOCTYPE BaliProject [<!ENTITY a "b">]><BaliProject/>').encode(encoding))
    with pytest.raises(prj.ProjectFileError):
        prj.parse_ldf(str(ldf))


def test_the_bomb_is_refused_before_it_is_expanded(tmp_path):
    ldf = tmp_path / "x.ldf"
    ldf.write_text(BOMB)
    with pytest.raises(prj.ProjectFileError, match="DTD or an entity"):
        prj._load(str(ldf))


def test_a_malformed_file_is_still_reported_as_invalid_xml(tmp_path):
    ldf = tmp_path / "x.ldf"
    ldf.write_text("<BaliProject><unclosed>")
    with pytest.raises(prj.ProjectFileError, match="not valid XML"):
        prj.parse_ldf(str(ldf))


def test_deeply_nested_project_xml_does_not_crash(tmp_path):
    ldf = tmp_path / "x.ldf"
    ldf.write_text("<BaliProject>" + "<a>" * 20000 + "</a>" * 20000 + "</BaliProject>")
    try:
        prj.parse_ldf(str(ldf))
    except prj.ProjectFileError:
        pass
