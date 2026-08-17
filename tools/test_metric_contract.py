#!/usr/bin/env python3
"""Self-tests for the cross-repo SLO metric-name guard (tools/check_metric_contract.py).

A guard that cannot fail is worse than no guard, so the central case here is the regression the
guard exists to catch: a consumer dividing by a metric honua-server does not emit. That is exactly
the state honua-release#5 found the platform in, and it produced NO error anywhere — the PromQL
just evaluated to an empty vector and the alerts stopped existing.

Run: python -m pytest tools/test_metric_contract.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_metric_contract as mc  # noqa: E402

CONTRACT = {
    "emitted": [
        {
            "series": "honua_request_error_total",
            "labels": ["service_type", "operation", "error_code", "in_band"],
        },
        {
            "series": "honua_serving_request_duration_ms_count",
            "labels": ["honua_protocol", "honua_operation", "status_class"],
        },
    ],
    "not_emitted": [{"series": "honua_backup_success_ratio", "reason": "future exporter"}],
    "retired": [{"series": "honua_http_requests_total", "reason": "never emitted; replaced"}],
    "external_labels": {"names": ["job", "namespace", "service", "environment", "le"]},
}


# ---- the regression this guard exists for --------------------------------------------------------
def test_absent_denominator_is_a_hard_failure():
    """The pre-#5 state of honua-helm and honua-devops: dividing by a metric nobody emits."""
    rule = (
        'sum(rate(honua_request_error_total{in_band="true"}[5m]))'
        " / clamp_min(sum(rate(honua_http_requests_total[5m])), 0.001)"
    )
    problems, _ = mc.check_sources(CONTRACT, {"honua-helm/prometheusrule.yaml": rule})
    assert any("honua_http_requests_total" in p for p in problems), problems


def test_the_fixed_rule_passes():
    rule = (
        'sum(rate(honua_request_error_total{in_band="true"}[5m]))'
        " / clamp_min(sum(rate(honua_serving_request_duration_ms_count[5m])), 0.001)"
    )
    problems, _ = mc.check_sources(CONTRACT, {"honua-helm/prometheusrule.yaml": rule})
    assert problems == []


def test_a_single_character_typo_in_a_real_metric_name_fails():
    rule = "sum(rate(honua_serving_request_duration_ms_counts[5m]))"
    problems, _ = mc.check_sources(CONTRACT, {"x": rule})
    assert any("honua_serving_request_duration_ms_counts" in p for p in problems), problems


def test_gate_config_metric_names_are_checked_too():
    """The honua-release gate's REQUEST_METRIC default was itself an invented name."""
    workflow = "      REQUEST_METRIC: 'honua_geoservices_requests_total'\n"
    problems, _ = mc.check_sources(CONTRACT, {"honua-release/gate-observability.yml": workflow})
    assert any("honua_geoservices_requests_total" in p for p in problems), problems


# ---- label drift ---------------------------------------------------------------------------------
def test_grouping_by_an_unsanitized_label_name_fails():
    """honua-devops grouped by `protocol`; the exporter sanitizes the tag to `honua_protocol`."""
    rule = "sum by (service, environment, protocol) (rate(honua_serving_request_duration_ms_count[5m]))"
    problems, _ = mc.check_sources(CONTRACT, {"honua-devops/rules.yml": rule})
    assert any("'protocol'" in p for p in problems), problems


def test_grouping_by_the_sanitized_label_name_passes():
    rule = (
        "sum by (service, environment, honua_protocol) "
        "(rate(honua_serving_request_duration_ms_count[5m]))"
    )
    problems, _ = mc.check_sources(CONTRACT, {"honua-devops/rules.yml": rule})
    assert problems == []


def test_matcher_label_is_checked_against_the_series_it_is_written_on():
    """A label that is real on ANOTHER metric in the same file must still fail here.

    Pooling labels per file made this pass: `in_band` is a genuine label of the error counter, so a
    file containing both metrics accepted it on the duration histogram too — where it matches
    nothing and yields an empty vector.
    """
    ok = 'sum(rate(honua_serving_request_duration_ms_count{status_class="5xx"}[5m]))'
    assert mc.check_sources(CONTRACT, {"a": ok})[0] == []

    # in_band lives on honua_request_error_total, never on the histogram — and both appear here.
    mixed = (
        'sum(rate(honua_request_error_total{in_band="true"}[5m]))'
        ' / sum(rate(honua_serving_request_duration_ms_count{in_band="true"}[5m]))'
    )
    problems = mc.check_sources(CONTRACT, {"a": mixed})[0]
    assert any(
        "honua_serving_request_duration_ms_count" in p and "in_band" in p for p in problems
    ), problems
    assert not any("honua_request_error_total' on label 'in_band" in p for p in problems), problems


def test_matcher_label_present_on_no_series_fails():
    bad = 'sum(rate(honua_serving_request_duration_ms_count{status_code=~"5.."}[5m]))'
    assert any("status_code" in p for p in mc.check_sources(CONTRACT, {"a": bad})[0])


def test_grafana_legend_label_drift_fails():
    """A legend naming a label the rules no longer group by renders blank — silent, like the rest."""
    panel = (
        '{"expr": "sum by (honua_protocol) (rate(honua_serving_request_duration_ms_count[5m]))",'
        ' "legendFormat": "p95 {{service}} {{protocol}}"}'
    )
    problems = mc.check_sources(CONTRACT, {"honua-devops/dash.json": panel})[0]
    assert any("'protocol'" in p for p in problems), problems

    fixed = panel.replace("{{protocol}}", "{{honua_protocol}}")
    assert mc.check_sources(CONTRACT, {"honua-devops/dash.json": fixed})[0] == []


def test_target_labels_applied_by_prometheus_are_allowed():
    rule = 'sum(rate(honua_request_error_total{namespace="prod",job="honua"}[5m]))'
    assert mc.check_sources(CONTRACT, {"a": rule})[0] == []


# ---- waivers -------------------------------------------------------------------------------------
def test_declared_waiver_passes_but_is_reported():
    problems, notes = mc.check_sources(CONTRACT, {"a": "avg_over_time(honua_backup_success_ratio[24h])"})
    assert problems == []
    assert any("honua_backup_success_ratio" in n for n in notes)


# ---- extraction rules ----------------------------------------------------------------------------
def test_helm_template_braces_are_not_read_as_label_matchers():
    """Helm templates interleave {{ ... }} with PromQL; template braces are not label matchers."""
    tpl = (
        "{{- $sel := trim (default \"\" (get $slo \"metricSelector\")) }}\n"
        'expr: \'sum(rate(honua_request_error_total{ {{- $inbandSel -}} }[{{ .window }}]))\''
    )
    series, attached, unattributed = mc.extract_references(tpl)
    labels = set().union(*attached.values()) if attached else set()
    labels |= unattributed
    assert series == {"honua_request_error_total"}
    assert labels == set()


def test_metric_names_inside_helm_printf_actions_are_still_checked():
    """honua-helm builds its PromQL inside {{ printf ... }}; those names must NOT be skipped.

    Stripping template actions before parsing would make this checker blind to the entire
    honua-helm PrometheusRule — the same silent-blindness failure it exists to catch.
    """
    tpl = (
        '{{- $errRatio := printf "sum(rate(honua_request_error_total{%s}[%%s]))'
        ' / clamp_min(sum(rate(honua_http_requests_total{%s}[%%s])), 0.001)" $sel $sel }}'
    )
    series, _, _ = mc.extract_references(tpl)
    assert "honua_http_requests_total" in series
    problems, _ = mc.check_sources(CONTRACT, {"honua-helm/prometheusrule.yaml": tpl})
    assert any("honua_http_requests_total" in p for p in problems), problems


def test_github_actions_expressions_are_not_stripped_like_helm_actions():
    """The gate's own bad default lived inside ${{ ... }} — stripping it would hide the drift."""
    workflow = "      REQUEST_METRIC: ${{ vars.HONUA_REQUEST_METRIC || 'honua_geoservices_requests_total' }}\n"
    series, _, _ = mc.extract_references(workflow)
    assert series == {"honua_geoservices_requests_total"}


def test_yaml_flow_mappings_are_not_read_as_label_matchers():
    yaml = "      platform_label: { required: false, type: string, default: 'dev-local' }\n"
    _, attached, unattributed = mc.extract_references(yaml)
    labels = (set().union(*attached.values()) if attached else set()) | unattributed
    assert labels == set()


def test_recording_rule_names_are_not_series_references():
    # `honua:slo:...` recording-rule names are colon-separated and must not be checked as metrics.
    series, _, _ = mc.extract_references("- record: honua:slo:error_rate:ratio_5m")
    assert series == set()


def test_extraction_finds_names_in_prose_comments():
    # Stale explanatory comments are drift too — values.yaml documents the rules to operators.
    series, _, _ = mc.extract_references("# the server increments honua_request_error_total for them")
    assert series == {"honua_request_error_total"}


# ---- comments vs expressions ---------------------------------------------------------------------
def test_a_comment_may_name_a_retired_metric_but_an_expression_may_not():
    """Recording WHY honua_http_requests_total was wrong must stay possible; using it must not."""
    documented = "# these rules used to divide by honua_http_requests_total, which nobody emits\n"
    assert mc.check_sources(CONTRACT, {"a": documented})[0] == []

    used = documented + "        expr: sum(rate(honua_http_requests_total[5m]))\n"
    problems = mc.check_sources(CONTRACT, {"a": used})[0]
    assert any("honua_http_requests_total" in p and "RETIRED" in p for p in problems), problems


def test_helm_go_template_comments_count_as_comments():
    """Helm has no '#' comments; its explanatory blocks are {{- /* ... */ -}} actions."""
    doc = "{{- /* we used to divide by honua_http_requests_total, which nobody emits */ -}}\n"
    assert mc.check_sources(CONTRACT, {"honua-helm/prometheusrule.yaml": doc})[0] == []


def test_a_comment_naming_an_unknown_metric_still_fails():
    stale = "# the server increments honua_totally_made_up_total for them\n"
    problems = mc.check_sources(CONTRACT, {"a": stale})[0]
    assert any("honua_totally_made_up_total" in p for p in problems), problems


def test_a_comment_may_name_contract_label_names():
    doc = "# the histogram is dimensioned by honua_protocol and honua_operation\n"
    assert mc.check_sources(CONTRACT, {"a": doc})[0] == []


# ---- CLI ------------------------------------------------------------------------------------------
def test_cli_blocks_when_the_server_checkout_is_missing(tmp_path, capsys):
    rc = mc.main(["--repos-root", str(tmp_path), "--self", str(tmp_path)])
    assert rc == 2
    assert "blocked" in capsys.readouterr().out


def test_cli_fails_on_real_drift(tmp_path):
    import json

    server = tmp_path / "honua-server" / "observability"
    server.mkdir(parents=True)
    (server / "slo-metric-contract.json").write_text(json.dumps(CONTRACT), encoding="utf-8")

    helm = tmp_path / "honua-helm" / "honua" / "templates"
    helm.mkdir(parents=True)
    (helm / "prometheusrule.yaml").write_text(
        "expr: sum(rate(honua_http_requests_total[5m]))", encoding="utf-8"
    )
    (tmp_path / "honua-helm" / "honua" / "values.yaml").write_text("slo: {}", encoding="utf-8")

    devops = tmp_path / "honua-devops" / "observability" / "prometheus"
    devops.mkdir(parents=True)
    (devops / "rules.yml").write_text("expr: sum(rate(honua_request_error_total[5m]))", encoding="utf-8")

    assert mc.main(["--repos-root", str(tmp_path), "--self", str(tmp_path / "nonexistent")]) == 1
