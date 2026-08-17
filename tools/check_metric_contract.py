#!/usr/bin/env python3
"""Cross-repo SLO metric-name guard (honua-release#5).

Every layer of Honua's SLO alerting was individually reviewed and individually correct, and the
platform was still blind, because nothing checked that the metric names matched ACROSS the repo
seam:

  * honua-server emits   honua_request_error_total / honua_serving_request_duration_ms_count
  * honua-helm divided by honua_http_requests_total          <- never emitted
  * honua-devops divided by honua_http_requests_total        <- never emitted
  * honua-release's gate divided by honua_geoservices_requests_total  <- never emitted

The failure mode is SILENCE. In PromQL `rate()` over an absent series is an empty vector, `sum()`
of an empty vector is empty, `clamp_min(empty, 0.001)` is empty, and `real / empty` is empty — so
the rule produces no series, the alert never fires, and every dashboard panel is simply blank.
Nothing goes red. A unit test in any single repo cannot see this.

This checker closes the seam from the consumer side. honua-server owns the contract
(`observability/slo-metric-contract.json`) and proves the PRODUCER end with an integration test
that scrapes its real /metrics exposition (SloMetricContractTests). Here we parse the actual alert
rules / gate configuration and require that every Honua series they reference is declared in that
contract — either as `emitted` (the server exports it) or as an explicit, justified `not_emitted`
waiver. A typo, a rename, or a newly-invented metric name fails the gate.

Label names are checked the same way, because they fail identically: honua-devops grouped by
`protocol`, but the exporter sanitizes the instrument's dotted tag key to `honua_protocol`, so
`sum by (protocol)` collapses every protocol into one unlabelled series without erroring.

  python tools/check_metric_contract.py --repos-root <dir> [--self <honua-release checkout>]

Exit codes mirror the other gate checkers: 0 pass, 1 fail, 2 blocked (a checkout was unavailable,
so coverage is incomplete — never reported as a pass).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

CONTRACT_RELPATH = os.path.join("observability", "slo-metric-contract.json")

# Consumer surfaces that reference honua-server metric names from another repository. Add a file
# here whenever new alerting/gating starts naming Honua series, or it is unguarded.
CONSUMER_GLOBS: dict[str, list[str]] = {
    "honua-helm": [
        "honua/templates/prometheusrule.yaml",
        "honua/values.yaml",
        # The chart's own CI asserted the rendered denominator by name. That is a consumer of these
        # names like any other, and while it pinned honua_http_requests_total it did not merely miss
        # the drift, it ENFORCED it: the chart could not adopt a real metric without turning
        # lint-chart red. A guard that skips the file which locks the bug in is not a guard.
        ".github/workflows/ci.yml",
        # Operator-facing docs describing which counters the alerts are built on.
        "honua/README.md",
    ],
    "honua-devops": [
        "observability/prometheus/*.yml",
        "observability/prometheus/*.yaml",
        # Dashboard panels reference the same series and labels, and a blank panel is exactly as
        # silent as an alert that never fires.
        "observability/grafana/*.json",
    ],
}

# honua-release's own observability gate names the metrics it scrapes.
SELF_GLOBS: list[str] = [".github/workflows/gate-observability.yml"]

# Template actions are NOT stripped. It is tempting to remove Helm's {{ ... }} and GitHub Actions'
# ${{ ... }} before parsing, but almost every metric name in these files lives INSIDE one:
# honua-helm builds its PromQL with `{{ printf "sum(rate(honua_request_error_total{%s}...)" }}` and
# gate-observability defaults its metric with `${{ vars.X || 'honua_..._count' }}`. Stripping
# template actions would make the checker silently blind to exactly the files it exists to guard —
# the same class of failure it is looking for. Instead, label matchers are only recognised when
# attached to a honua_ series name, which is what keeps YAML flow mappings (`labels: {}`,
# `platform_label: { required: false }`) from being misread as PromQL label blocks.
# Go-template comment actions — Helm's only comment form, so they must be classified as comments
# rather than as expression text.
_GO_TEMPLATE_COMMENT = re.compile(r"\{\{-?\s*/\*.*?\*/\s*-?\}\}", re.DOTALL)
_SERIES_WITH_MATCHER = re.compile(r"(honua_[a-z0-9_]+)\s*\{([^}]*)\}")
# PromQL aggregation label lists.
_GROUPING = re.compile(r"\b(?:by|without)\s*\(([^)]*)\)")
_SERIES_TOKEN = re.compile(r"\bhonua_[a-z0-9_]+\b")
# Grafana legendFormat label interpolation: a bare {{label}} and nothing else inside the braces.
# Helm actions ({{ .Release.Namespace }}, {{ printf ... }}, {{- $sel -}}) do not match this shape.
_TEMPLATE_LABEL = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_LABEL_NAME = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:=~|!~|!=|=)")
# A label matcher term standing on its own, i.e. not written directly after a series name — Helm
# assigns selector fragments to template variables and splices them in later.
# Only the REGEX operators are recognised. `ident="value"` is ubiquitous in YAML, shell and Markdown
# (ERR="$x", HONUA_ADMIN_PASSWORD="...") and treating those as PromQL matchers floods the report with
# nonsense; `=~` and `!~` are effectively PromQL-only, and the Honua-prefixed labels that would
# otherwise be misread as series names (honua_protocol, honua_operation) are always written with one.
_BARE_MATCHER_TERM = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*(=~|!~)\s*\\?"([^"]*)')


def load_contract(path: str) -> dict:
    """Read the contract honua-server owns. Raises FileNotFoundError when the checkout is absent."""
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def contract_series(contract: dict) -> tuple[set[str], set[str], set[str]]:
    """(emitted series, explicitly-waived series, retired series)."""
    emitted = {entry["series"] for entry in contract.get("emitted", [])}
    waived = {entry["series"] for entry in contract.get("not_emitted", [])}
    retired = {entry["series"] for entry in contract.get("retired", [])}
    return emitted, waived, retired


def contract_labels(contract: dict) -> dict[str, set[str]]:
    """series -> label names the server actually attaches to it."""
    return {entry["series"]: set(entry.get("labels", [])) for entry in contract.get("emitted", [])}


def split_comments(text: str) -> tuple[str, str]:
    """Split a rule file into (expression text, comment text).

    The two are checked against different rule sets. An EXPRESSION naming a series the server
    does not emit is a live outage. A COMMENT naming one is usually the opposite: these files
    document why an old name was wrong, and that history is worth keeping. Comments are still
    checked — a stale explanation is how the next person reintroduces the bug — but they may
    additionally name anything in the contract's `retired` list.
    """
    # Helm comments are Go-template comment actions, not '#' lines.
    go_comments = _GO_TEMPLATE_COMMENT.findall(text)
    text = _GO_TEMPLATE_COMMENT.sub(" ", text)

    code: list[str] = []
    comments: list[str] = list(go_comments)
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            comments.append(stripped)
        else:
            code.append(line)
    return "\n".join(code), "\n".join(comments)


def extract_references(text: str) -> tuple[set[str], dict[str, set[str]], set[str]]:
    """Return (series names, labels attached per series, unattributed label names).

    A label used in a matcher is attributed to the SERIES it is written on, so it can be checked
    against that series' own labels — `honua_serving_request_duration_ms_count{status_code=~"5.."}`
    must fail even though `status_code` is a perfectly real label on some other metric. Labels from
    `by (...)`/`without (...)` groupings and from Grafana `{{legend}}` fields cannot be attributed
    without a full PromQL/dashboard parse, so they are returned separately and checked against the
    union of the file's series.

    Pure and side-effect free so the extraction rules are unit-testable without any checkout.
    """
    attached: dict[str, set[str]] = {}
    for series_name, matcher in _SERIES_WITH_MATCHER.findall(text):
        attached.setdefault(series_name, set()).update(_LABEL_NAME.findall(matcher))

    # Remove the attributed matchers first so their terms are not re-read as free-standing ones.
    stripped = _SERIES_WITH_MATCHER.sub(lambda m: m.group(1) + " ", text)

    unattributed: set[str] = set()
    # Selector fragments that are not written against a series name: Helm assigns them to template
    # variables ($geoServicesSel := "honua_protocol=~\"...\"") and splices them in later. Their label
    # names must be recognised as labels, or `honua_protocol` reads as a metric that does not exist.
    unattributed.update(label for label, _, _ in _BARE_MATCHER_TERM.findall(stripped))
    stripped = _BARE_MATCHER_TERM.sub(" ", stripped)

    for grouping in _GROUPING.findall(stripped):
        unattributed.update(
            part.strip() for part in grouping.split(",") if part.strip() and part.strip().isidentifier()
        )
    # Grafana legendFormat interpolates label names as bare {{label}}. A legend naming a label the
    # rules no longer group by renders blank — the same silent failure, one layer up.
    unattributed.update(_TEMPLATE_LABEL.findall(stripped))

    # Strip the grouping/legend blocks too, so label names are never mistaken for series names — a
    # legend that correctly renders {{honua_protocol}} must not be reported as a missing metric.
    stripped = _GROUPING.sub(" ", stripped)
    stripped = _TEMPLATE_LABEL.sub(" ", stripped)
    series = set(_SERIES_TOKEN.findall(stripped))

    return series, attached, unattributed


def check_sources(contract: dict, sources: dict[str, str]) -> tuple[list[str], list[str]]:
    """Return (problems, notes) for a mapping of source label -> file contents."""
    emitted, waived, retired = contract_series(contract)
    labels_by_series = contract_labels(contract)
    external = set(contract.get("external_labels", {}).get("names", []))

    problems: list[str] = []
    notes: list[str] = []

    for label, text in sorted(sources.items()):
        code, comments = split_comments(text)
        series, attached_labels, unattributed_labels = extract_references(code)
        commented, _, _ = extract_references(comments)

        for name in sorted(series):
            if name in emitted:
                continue
            if name in waived:
                notes.append(f"{label}: '{name}' is a declared not_emitted waiver (no server instrument)")
                continue
            hint = (
                " It is a RETIRED name the contract explicitly records as never-emitted — naming it in "
                "a comment is fine, using it in an expression is not."
                if name in retired
                else ""
            )
            problems.append(
                f"{label}: references '{name}', which honua-server does not emit and the contract "
                f"does not waive. A PromQL expression over an absent series yields an EMPTY vector, "
                f"so this rule silently never fires.{hint}"
            )

        # Label names share the honua_ prefix (honua_protocol, honua_operation) and legitimately
        # appear in prose, so they are not "undocumented metrics".
        known_labels = set(external).union(*labels_by_series.values()) if labels_by_series else set(external)
        for name in sorted(commented - series):
            if name in emitted or name in waived or name in retired or name in known_labels:
                continue
            problems.append(
                f"{label}: a comment documents '{name}', which is not in the contract at all. "
                f"Stale metric documentation is how the next author reintroduces the drift."
            )

        # A matcher label is checked against the series it is WRITTEN ON. Pooling every label in a
        # file against the union of its series lets `duration_count{status_code=~"5.."}` pass purely
        # because some other metric in the same file has a status_code label.
        for series_name in sorted(attached_labels):
            series_allowed = set(external) | labels_by_series.get(series_name, set())
            for name in sorted(attached_labels[series_name] - series_allowed):
                problems.append(
                    f"{label}: selects '{series_name}' on label '{name}', which that series does not "
                    f"carry (its labels are "
                    f"{sorted(labels_by_series.get(series_name, set())) or 'unknown — series not in contract'}). "
                    f"Matching on a label a series does not have yields an EMPTY vector, so the rule "
                    f"silently never fires."
                )

        # Grouping, legend and free-standing-selector labels cannot be attributed to one series
        # without a full PromQL parse — and a dashboard querying `honua:slo:latency:p95_ms:5m`
        # references no raw series at all, because the labels it groups by were inherited from a
        # recording rule in another file. They are therefore checked against every label the
        # contract knows about. That still catches the real class of bug (a label no Honua series
        # carries, e.g. `protocol` instead of `honua_protocol`); attributing them precisely would
        # need cross-file recording-rule resolution.
        pooled = set(external) | (
            set().union(*labels_by_series.values()) if labels_by_series else set()
        )
        for name in sorted(unattributed_labels - pooled):
            problems.append(
                f"{label}: groups or renders by label '{name}', which is neither a contract label of "
                f"the series it references nor a known target label. Grouping by a label the server "
                f"does not attach silently collapses the result instead of erroring."
            )

    return problems, notes


def collect_sources(repos_root: str, self_root: str | None) -> tuple[dict[str, str], list[str]]:
    """Read every consumer file. Returns (sources, missing repo names)."""
    sources: dict[str, str] = {}
    missing: list[str] = []

    for repo, patterns in CONSUMER_GLOBS.items():
        repo_root = os.path.join(repos_root, repo)
        if not os.path.isdir(repo_root):
            missing.append(repo)
            continue
        found = False
        for pattern in patterns:
            for path in sorted(glob.glob(os.path.join(repo_root, pattern))):
                with open(path, encoding="utf-8") as handle:
                    sources[f"{repo}/{os.path.relpath(path, repo_root)}"] = handle.read()
                found = True
        if not found:
            missing.append(repo)

    if self_root:
        for pattern in SELF_GLOBS:
            for path in sorted(glob.glob(os.path.join(self_root, pattern))):
                with open(path, encoding="utf-8") as handle:
                    sources[f"honua-release/{os.path.relpath(path, self_root)}"] = handle.read()

    return sources, missing


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--repos-root", required=True, help="directory holding the cloned components")
    ap.add_argument("--self", dest="self_root", default=".", help="this honua-release checkout")
    args = ap.parse_args(argv)

    contract_path = os.path.join(args.repos_root, "honua-server", CONTRACT_RELPATH)
    try:
        contract = load_contract(contract_path)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"metric-contract: blocked — cannot read {contract_path}: {exc}")
        return 2

    sources, missing = collect_sources(args.repos_root, args.self_root)
    if not sources:
        print("metric-contract: blocked — no consumer alert rules were available to check")
        return 2

    problems, notes = check_sources(contract, sources)

    for note in notes:
        print(f"note: {note}")
    for problem in problems:
        print(f"DRIFT: {problem}")

    checked = ", ".join(sorted(sources))
    if problems:
        print(f"metric-contract: fail — {len(problems)} metric-name/label drift(s) across {checked}")
        return 1
    if missing:
        print(
            "metric-contract: blocked — checked "
            f"{len(sources)} file(s) with no drift, but {', '.join(sorted(missing))} was unavailable "
            "so its alert rules are UNVERIFIED"
        )
        return 2
    print(f"metric-contract: pass — every metric name and label in {checked} is declared by the contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
