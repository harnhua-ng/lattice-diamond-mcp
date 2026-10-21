"""Tests for the report parsers, using trimmed excerpts of real Diamond 3.14 reports."""

from pathlib import Path

from diamond_mcp import reports

DATA = Path(__file__).parent / "data"


def read(name: str) -> str:
    return (DATA / name).read_text()


def test_parse_par_reads_summary_block():
    par = reports.parse_par(read("sample.par"))
    assert par["run_status"] == "Completed"
    assert par["unrouted_connections"] == 0
    assert par["errors"] == 0
    assert par["setup_worst_slack_ns"] == -0.469
    assert par["setup_timing_score_ns"] == 2.516
    assert par["hold_worst_slack_ns"] == 0.303
    assert par["real_time_s"] == 10


def test_parse_par_unconstrained_design_has_none_slacks():
    text = (
        "PAR_SUMMARY::Run status = Completed\n"
        "PAR_SUMMARY::Number of unrouted conns = 0\n"
        "PAR_SUMMARY::Worst  slack<setup/<ns>> = <n/a>\n"
        "PAR_SUMMARY::Timing score<setup/<ns>> = <n/a>\n"
        "PAR_SUMMARY::Number of errors = 0\n"
    )
    par = reports.parse_par(text)
    assert par["setup_worst_slack_ns"] is None
    assert par["unrouted_connections"] == 0


def test_parse_par_without_summary_is_empty():
    assert reports.parse_par("nothing useful here") == {}


def test_parse_twr_setup_and_hold_summary():
    twr = reports.parse_twr(read("sample_setup_hold.twr"))
    assert twr["setup_errors"] == 38
    assert twr["hold_errors"] == 0
    assert twr["setup_score_ps"] == 17644
    assert twr["hold_score_ps"] == 0
    assert twr["constraint_coverage_percent"] == 90.72


def test_parse_twr_verdicts_are_per_analysis_not_first_match_anywhere():
    # Setup misses constraints while hold meets them: the "All preferences were
    # met." line that comes later belongs to hold, not setup.
    twr = reports.parse_twr(read("sample_setup_hold.twr"))
    assert twr["setup_preferences_met"] is False
    assert twr["setup_preferences_not_met"] == 4
    assert twr["hold_preferences_met"] is True


def test_parse_twr_joins_wrapped_preference_names():
    constraints = reports.parse_twr(read("sample_setup_hold.twr"))["constraints"]
    names = [c["preference"] for c in constraints]
    assert 'FREQUENCY PORT "sys_clk" 500.000000 MHz ;' in names
    assert all(c["constraint"] for c in constraints)  # no orphan continuation rows


def test_parse_twr_marks_unmet_constraints():
    constraints = reports.parse_twr(read("sample_setup_hold.twr"))["constraints"]
    unmet = [c for c in constraints if not c["met"]]
    assert len(unmet) == 4
    port = next(c for c in unmet if "sys_clk\"" in c["preference"] and "PORT" in c["preference"])
    assert port["constraint"] == "500.000 MHz"
    assert port["actual"] == "125.000 MHz"


def test_parse_twr_setup_only_report():
    twr = reports.parse_twr(read("sample_setup_only.twr"))
    assert twr["setup_errors"] == 0
    assert twr["setup_score_ps"] == 0
    assert twr["setup_preferences_met"] is True
    assert "hold_preferences_met" not in twr
    assert twr["constraint_coverage_percent"] == 100.0
    assert [c["preference"] for c in twr["constraints"]] == [
        "Default path enumeration",
        "Default net enumeration",
    ]


def test_parse_mrp_resources():
    resources = reports.parse_mrp(read("sample.mrp"))["resources"]
    assert resources["Number of LUT4s"] == {"used": 191, "total": 1280, "percent": 15}
    assert resources["Number of SLICEs"]["used"] == 98
    assert resources["Number used as logic LUTs"] == {"used": 163}


def test_parse_mrp_handles_pio_row_with_jtag_suffix():
    resources = reports.parse_mrp(read("sample.mrp"))["resources"]
    assert resources["Number of PIO sites used"] == {"used": 18, "total": 80, "percent": 28}


def test_parse_mrp_without_design_summary_is_empty():
    assert reports.parse_mrp("no summary") == {}
