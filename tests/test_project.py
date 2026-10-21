"""Tests for the read-only project file helpers."""

import pytest

from diamond_mcp import project as prj


def test_parse_ldf_summary(ldf_project):
    info = prj.parse_ldf(str(ldf_project))
    assert info["title"] == "proj"
    assert info["device"] == "FAKE-DEV-1PKG"
    assert info["default_implementation"] == "impl1"
    impl = info["implementations"][0]
    assert impl["synthesis"] == "lse"
    assert impl["top"] == "top"
    assert [s["name"] for s in impl["sources"]] == ["src/top.v", "proj.lpf"]
    assert info["strategies"] == [{"name": "Strategy1", "file": "proj.sty"}]


def test_parse_ldf_top_falls_back_to_source_top_module(tmp_path):
    ldf = tmp_path / "p.ldf"
    ldf.write_text(
        '<BaliProject title="p" device="D" default_implementation="i"><Implementation title="i" dir="i">'
        '<Source name="a.v" type="Verilog"><Options top_module="my_top"/></Source>'
        "</Implementation></BaliProject>"
    )
    assert prj.parse_ldf(str(ldf))["implementations"][0]["top"] == "my_top"


@pytest.mark.parametrize(
    "content, message",
    [("not xml at all", "not valid XML"), ("<Other/>", "Not a Diamond project")],
)
def test_parse_ldf_rejects_bad_files(tmp_path, content, message):
    bad = tmp_path / "bad.ldf"
    bad.write_text(content)
    with pytest.raises(prj.ProjectFileError, match=message):
        prj.parse_ldf(str(bad))


def test_parse_ldf_missing_file(tmp_path):
    with pytest.raises(prj.ProjectFileError, match="does not exist"):
        prj.parse_ldf(str(tmp_path / "missing.ldf"))


def test_find_missing_sources_none_when_complete(ldf_project):
    assert prj.find_missing_sources(str(ldf_project)) == []


def test_find_missing_sources_reports_absent_file(ldf_project):
    (ldf_project.parent / "src" / "top.v").unlink()
    missing = prj.find_missing_sources(str(ldf_project))
    assert [m["file"] for m in missing] == ["src/top.v"]
    assert missing[0]["blocking"] is True
    assert "hint" not in missing[0]


def test_find_missing_sources_detects_letter_case_mismatch(ldf_project):
    (ldf_project.parent / "src" / "top.v").rename(ldf_project.parent / "src" / "TOP.V")
    missing = prj.find_missing_sources(str(ldf_project))
    assert len(missing) == 1
    assert "different letter case" in missing[0]["hint"]
    assert "TOP.V" in missing[0]["hint"]


def test_find_missing_sources_case_mismatch_in_directory_name(ldf_project):
    (ldf_project.parent / "src").rename(ldf_project.parent / "SRC")
    missing = prj.find_missing_sources(str(ldf_project))
    assert "different letter case" in missing[0]["hint"]


def test_find_missing_sources_simulation_file_is_not_blocking(ldf_project):
    text = ldf_project.read_text().replace(
        '    </Implementation>',
        '        <Source name="test/test.spf" type="Simulation Project File" type_short="SPF"><Options/></Source>\n'
        "    </Implementation>",
    )
    ldf_project.write_text(text)
    missing = prj.find_missing_sources(str(ldf_project))
    assert [(m["file"], m["blocking"]) for m in missing] == [("test/test.spf", False)]


def test_find_missing_sources_checks_strategy_file(ldf_project):
    (ldf_project.parent / "proj.sty").unlink()
    missing = prj.find_missing_sources(str(ldf_project))
    assert [(m["file"], m["kind"]) for m in missing] == [("proj.sty", "Strategy")]


def test_report_base_uses_project_and_implementation_titles(ldf_project):
    base = prj.report_base(str(ldf_project))
    assert base["prefix"] == "proj_impl1"
    assert base["implementation"] == "impl1"
    assert base["directory"] == str(ldf_project.parent.resolve() / "impl1")


def test_report_base_unknown_implementation(ldf_project):
    with pytest.raises(prj.ProjectFileError, match="Available: impl1"):
        prj.report_base(str(ldf_project), "nope")


def test_find_report_files_lists_only_existing(ldf_project):
    impl = ldf_project.parent / "impl1"
    impl.mkdir()
    (impl / "proj_impl1.par").write_text("x")
    (impl / "proj_impl1.twr").write_text("x")
    found = prj.find_report_files(str(ldf_project))
    assert sorted(found) == ["par", "twr"]


def test_newest_artifacts_filters_by_time_and_suffix(tmp_path):
    old = tmp_path / "old.bit"
    old.write_text("x")
    import os, time
    os.utime(old, (time.time() - 1000, time.time() - 1000))
    new = tmp_path / "sub" / "new.bit"
    new.parent.mkdir()
    new.write_text("x")
    (tmp_path / "ignored.txt").write_text("x")
    found = prj.newest_artifacts(str(tmp_path), (".bit",), time.time() - 60)
    assert found == [str(new)]
