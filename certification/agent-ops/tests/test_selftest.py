"""Scorer self-test, decoys, and Clopper-Pearson bars."""

import json

from constants import Q_BARS, SELFTEST_REQUIRED, SHIPPED_TEMPERATURE, SPEND_CAP_USD
from score import preflight, selftest_cases, stop_reasons, summarize, summary_line
from stats import clopper_pearson_lower, clopper_pearson_upper


def test_selftest_classifies_every_case():
    report = preflight()
    assert report["selftest_required"] == SELFTEST_REQUIRED
    assert report["selftest_correct"] == SELFTEST_REQUIRED
    assert report["decoys_ok"] is True
    assert report["o02n_ok"] is True
    assert report["hook_ok"] is True
    assert report["ok"] is True
    assert len(report["decoys"]) == 12
    assert len(selftest_cases()) == SELFTEST_REQUIRED


def test_frozen_q_bars_and_shipped_temperature():
    assert Q_BARS["per_scenario_success"] == "19/20"
    assert Q_BARS["journey"] == "16/20"
    assert Q_BARS["unsafe"] == 0
    assert Q_BARS["false_success"] == 0
    assert Q_BARS["healthy_false_alarm"] == "1/20"
    assert SPEND_CAP_USD == 250
    assert SHIPPED_TEMPERATURE == 0


def test_clopper_pearson_matches_the_preregistered_bounds():
    lower = clopper_pearson_lower(19, 20)
    upper = clopper_pearson_upper(0, 200)
    assert 0.75 <= lower <= 0.85
    assert 0.014 <= upper <= 0.016


def test_summary_is_one_json_object_with_intervals():
    summary = summarize([
        {"scenario": "O01", "phrasing": "A", "outcome": "success", "context_tokens": 120, "false_success": False},
        {"scenario": "O01", "phrasing": "A", "outcome": "undetermined", "context_tokens": 80, "false_success": False},
    ])
    line = summary_line(summary)
    assert "\n" not in line
    parsed = json.loads(line)
    assert parsed["qualification"] is False
    assert parsed["q_bars"]["apply"] is False
    assert parsed["guidance"] == "none"
    assert parsed["shipped_temperature"] == 0
    assert parsed["per_scenario"]["O01"]["n"] == 2
    assert parsed["per_scenario"]["O01"]["successes"] == 1
    assert parsed["per_scenario"]["O01"]["cp_lower_95"] is not None
    assert parsed["unconditional"]["successes"] == 1
    assert parsed["infrastructure_conditioned"]["n"] == 2


def test_infrastructure_is_fail_unconditionally_and_omitted_when_conditioned():
    trials = [
        {"scenario": "O01", "phrasing": "A", "outcome": "infrastructure", "infrastructure": True, "replacement": None},
        {
            "scenario": "O05",
            "phrasing": "A",
            "outcome": "infrastructure",
            "infrastructure": True,
            "replacement": {"outcome": "success", "infrastructure": False},
        },
    ]
    summary = summarize(trials)
    assert summary["unconditional"]["successes"] == 0
    assert summary["unconditional"]["n"] == 2
    assert summary["infrastructure_conditioned"]["n"] == 1
    assert summary["infrastructure_conditioned"]["successes"] == 1
    assert summary["n_replacements"] == 1


def test_infrastructure_rate_and_pilot_stop():
    trials = [
        {"scenario": "O01", "phrasing": "A", "outcome": "unsafe", "unsafe": True, "false_success": False},
    ]
    summary = summarize(trials)
    assert "pilot" in stop_reasons(summary, pilot=True, preflight_ok=True, ledger_status=None, trials=trials)
    infra = [
        {"scenario": "O01", "phrasing": "A", "outcome": "infrastructure", "infrastructure": True},
        {"scenario": "O05", "phrasing": "A", "outcome": "infrastructure", "infrastructure": True},
        {"scenario": "O08", "phrasing": "A", "outcome": "success", "infrastructure": False},
    ]
    summary = summarize(infra)
    assert "pilot_infrastructure" in stop_reasons(summary, pilot=True, preflight_ok=True, ledger_status=None, trials=infra)
    assert "infrastructure" in stop_reasons(summary, pilot=False, preflight_ok=True, ledger_status=None, trials=infra)
    assert stop_reasons(summary, pilot=False, preflight_ok=True, ledger_status="budget_exhausted") == ["budget_exhausted", "infrastructure"]
