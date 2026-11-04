"""Unit tests for the conformance-evidence federation gate (honua-release#133).

The point of this gate is that it can FAIL. Most of these tests therefore assert failure paths:
diverged lineage, a stale snapshot, a regressed suite. A gate that only ever reports pass or
blocked is the defect this module was written to remove.
"""
from datetime import datetime, timezone

from check_conformance_evidence import (
    ConformanceEvidenceError,
    ConformanceInputs,
    evaluate_conformance,
    non_implemented_cite_suites,
    parse_cite_status,
)

import pytest

NOW = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)
CANDIDATE = "6b6d3b898f4abb6b34833d953b50d44f3d38c6c1"
CITE_SHA = "eee76952f08d68e78b93b11b3acedac655625d62"

# Trimmed to the shape the real docs/cite-status.md carries.
CITE_STATUS_MD = f"""# CITE Status — Authoritative Snapshot

Last reviewed: 2026-08-12
Owner: Honua Server platform

Snapshot copied from
[CITE Evidence Report run 31609659377](https://github.com/honua-io/honua-server/actions/runs/31609659377)
on `trunk@{CITE_SHA}`, completed
2026-08-12T15:29:44Z. The fully green run's bundle reported `allPassed=true`: 1117 passed, 0
failed, 0 skipped, 0 CantTell.

| Suite | Profile | Passed / Total | Pass Rate | Last Evidence Run |
|---|---|---:|---:|---|
| OGC API Features 1.0 | `default` | 137 / 137 | 100% | 2026-08-12 |
| OGC API Tiles 1.0 | `default` | 16 / 16 | 100% | 2026-08-12 |
| WFS 2.0 | `basic` | 167 / 167 | 100% | 2026-08-12 |
| WMS 1.3 | `default` | 213 / 213 | 100% | 2026-08-12 |
| WMTS 1.0 | `default` | 60 / 60 | 100% | 2026-08-12 |
| WCS 2.0 | `core` | 82 / 82 | 100% | 2026-08-12 |
"""

CONFIG = {
    "cite": {
        "maxAgeDays": 14,
        "requiredSuites": [
            "OGC API Features 1.0",
            "OGC API Tiles 1.0",
            "WFS 2.0",
            "WMS 1.3",
            "WMTS 1.0",
            "WCS 2.0",
        ],
    },
    "stac": {
        "requiredConformanceClasses": [
            "https://api.stacspec.org/v1.0.0/core",
            "https://api.stacspec.org/v1.0.0/collections",
        ]
    },
}

GOOD_STAC = {
    "conformsTo": [
        "https://api.stacspec.org/v1.0.0/core",
        "https://api.stacspec.org/v1.0.0/collections",
    ],
    "errors": [],
}


def _status(rows, check):
    return next(r["status"] for r in rows if r["check"] == check)


def test_parses_the_real_cite_status_shape():
    cite = parse_cite_status(CITE_STATUS_MD)
    assert cite["sha"] == CITE_SHA
    assert cite["lastReviewed"] == "2026-08-12"
    assert cite["allPassed"] is True
    assert cite["runId"] == "31609659377"
    assert len(cite["suites"]) == 6
    assert {"suite": "WCS 2.0", "profile": "core", "passed": 82, "total": 82} in cite["suites"]


def test_absent_snapshot_is_none_not_an_error():
    # None means "not fetched" -> the gate reports BLOCKED. It must not raise, and must not invent.
    assert parse_cite_status(None) is None


def test_snapshot_without_a_sha_is_rejected_loudly():
    # A snapshot that cannot be bound to a candidate is worse than a missing one: it looks like
    # evidence. Malformed must never degrade into a silent pass.
    with pytest.raises(ConformanceEvidenceError):
        parse_cite_status("Last reviewed: 2026-08-12\n\nno sha here at all\n")


def test_snapshot_without_a_review_date_is_rejected_loudly():
    with pytest.raises(ConformanceEvidenceError):
        parse_cite_status(f"on `trunk@{CITE_SHA}`, completed\n")


# The happy path — everything genuinely proven
def test_fully_federated_candidate_passes():
    rows, overall = evaluate_conformance(ConformanceInputs(
        CANDIDATE, parse_cite_status(CITE_STATUS_MD), "ancestor", GOOD_STAC, CONFIG), now=NOW
    )
    assert overall == "pass", [r for r in rows if r["status"] != "pass"]
    assert _status(rows, "cite:lineage") == "pass"
    assert _status(rows, "cite:suites") == "pass"
    assert _status(rows, "stac:validator") == "pass"


def test_ancestor_lineage_is_accepted_deliberately():
    # CITE runs on its own weekly cadence, so the sha it certified is normally BEHIND a manifest
    # pinned later. That is lineage continuity, not a break -- the same rule the freeze-phase
    # evidence gate already proved out.
    for relation in ("identical", "ancestor", "descendant"):
        rows, _ = evaluate_conformance(ConformanceInputs(
            CANDIDATE, parse_cite_status(CITE_STATUS_MD), relation, GOOD_STAC, CONFIG), now=NOW
        )
        assert _status(rows, "cite:lineage") == "pass", relation


# Failure paths — the reason this gate exists
def test_diverged_lineage_fails():
    rows, overall = evaluate_conformance(ConformanceInputs(
        CANDIDATE, parse_cite_status(CITE_STATUS_MD), "diverged", GOOD_STAC, CONFIG), now=NOW
    )
    assert overall == "fail"
    assert _status(rows, "cite:lineage") == "fail"


def test_stale_snapshot_fails_rather_than_blocking():
    # Obtained-but-too-old is a decided failure, not a bootstrap gap.
    late = datetime(2026, 9, 30, tzinfo=timezone.utc)
    rows, overall = evaluate_conformance(ConformanceInputs(
        CANDIDATE, parse_cite_status(CITE_STATUS_MD), "ancestor", GOOD_STAC, CONFIG), now=late
    )
    assert overall == "fail"
    assert _status(rows, "cite:freshness") == "fail"


def test_a_regressed_suite_fails():
    regressed = CITE_STATUS_MD.replace("| WFS 2.0 | `basic` | 167 / 167 |", "| WFS 2.0 | `basic` | 160 / 167 |")
    rows, overall = evaluate_conformance(ConformanceInputs(
        CANDIDATE, parse_cite_status(regressed), "ancestor", GOOD_STAC, CONFIG), now=NOW
    )
    assert overall == "fail"
    assert "WFS 2.0 160/167" in next(r["why"] for r in rows if r["check"] == "cite:suites")


def test_a_vanished_required_suite_fails():
    # Dropping a row must not read as "nothing regressed" -- that shrinks the denominator silently.
    shrunk = "\n".join(line for line in CITE_STATUS_MD.splitlines() if not line.startswith("| WMTS 1.0"))
    rows, overall = evaluate_conformance(ConformanceInputs(
        CANDIDATE, parse_cite_status(shrunk), "ancestor", GOOD_STAC, CONFIG), now=NOW
    )
    assert overall == "fail"
    assert _status(rows, "cite:coverage") == "fail"


def test_all_passed_false_fails_even_when_the_table_looks_clean():
    doctored = CITE_STATUS_MD.replace("allPassed=true", "allPassed=false")
    rows, overall = evaluate_conformance(ConformanceInputs(
        CANDIDATE, parse_cite_status(doctored), "ancestor", GOOD_STAC, CONFIG), now=NOW
    )
    assert overall == "fail"
    assert _status(rows, "cite:allPassed") == "fail"


def test_missing_stac_conformance_class_fails():
    stripped = {"conformsTo": ["https://api.stacspec.org/v1.0.0/core"], "errors": []}
    rows, overall = evaluate_conformance(ConformanceInputs(
        CANDIDATE, parse_cite_status(CITE_STATUS_MD), "ancestor", stripped, CONFIG), now=NOW
    )
    assert overall == "fail"
    assert _status(rows, "stac:conformance-classes") == "fail"


def test_stac_validator_errors_fail():
    broken = dict(GOOD_STAC, errors=["item-search: sortby not honoured"])
    rows, overall = evaluate_conformance(ConformanceInputs(
        CANDIDATE, parse_cite_status(CITE_STATUS_MD), "ancestor", broken, CONFIG), now=NOW
    )
    assert overall == "fail"
    assert _status(rows, "stac:validator") == "fail"


# Blocked paths — honest bootstrap, never a fake pass
def test_unfetchable_snapshot_blocks_and_never_passes():
    rows, overall = evaluate_conformance(ConformanceInputs(CANDIDATE, None, None, GOOD_STAC, CONFIG), now=NOW)
    assert overall == "blocked"
    assert _status(rows, "cite:snapshot") == "blocked"


def test_undecidable_lineage_blocks():
    rows, overall = evaluate_conformance(ConformanceInputs(
        CANDIDATE, parse_cite_status(CITE_STATUS_MD), None, GOOD_STAC, CONFIG), now=NOW
    )
    assert overall == "blocked"
    assert _status(rows, "cite:lineage") == "blocked"


def test_missing_stac_blocks_rather_than_passing():
    rows, overall = evaluate_conformance(ConformanceInputs(
        CANDIDATE, parse_cite_status(CITE_STATUS_MD), "ancestor", None, CONFIG), now=NOW
    )
    assert overall == "blocked"
    assert _status(rows, "stac:validator") == "blocked"


def test_fail_outranks_blocked():
    # A real regression must not be masked by an unrelated bootstrap gap in the same run.
    rows, overall = evaluate_conformance(ConformanceInputs(
        CANDIDATE, parse_cite_status(CITE_STATUS_MD), "diverged", None, CONFIG), now=NOW
    )
    assert overall == "fail"


# Maturity scope (operator ruling 2026-10-22): non-implemented capabilities do not decide
MATRIX = {
    "capabilities": [
        {"key": "serve.wfs", "maturity": {"implemented": 2}, "cite": ["WFS 2.0"]},
        {"key": "serve.wmts", "maturity": {"preview": 1}, "cite": ["WMTS 1.0"]},
        {"key": "serve.mixed", "maturity": {"implemented": 3, "preview": 1}, "cite": ["WMS 1.3"]},
        {"key": "serve.empty", "maturity": {}, "cite": ["WCS 2.0"]},
    ]
}


def _scoped_config():
    cite = dict(CONFIG["cite"], nonImplementedSuites=non_implemented_cite_suites(MATRIX))
    return dict(CONFIG, cite=cite)


def test_only_wholly_non_implemented_capabilities_leave_the_verdict():
    assert non_implemented_cite_suites(MATRIX) == {"WMTS 1.0": "serve.wmts preview"}
    assert non_implemented_cite_suites(None) == {}


def test_a_preview_suite_regression_is_reported_but_does_not_fail():
    regressed = CITE_STATUS_MD.replace("| WMTS 1.0 | `default` | 60 / 60 |", "| WMTS 1.0 | `default` | 50 / 60 |")
    rows, overall = evaluate_conformance(ConformanceInputs(
        CANDIDATE, parse_cite_status(regressed), "ancestor", None, _scoped_config()), now=NOW, sections=("cite",)
    )
    assert overall == "pass"
    assert _status(rows, "cite:not-certified") == "advisory"
    assert "WMTS 1.0 50/60" in next(r["why"] for r in rows if r["check"] == "cite:not-certified")


def test_a_preview_suite_may_be_absent_but_an_implemented_one_may_not():
    no_wmts = "\n".join(line for line in CITE_STATUS_MD.splitlines() if not line.startswith("| WMTS 1.0"))
    _, overall = evaluate_conformance(ConformanceInputs(
        CANDIDATE, parse_cite_status(no_wmts), "ancestor", None, _scoped_config()), now=NOW, sections=("cite",)
    )
    assert overall == "pass"
    no_wfs = "\n".join(line for line in CITE_STATUS_MD.splitlines() if not line.startswith("| WFS 2.0"))
    rows, overall = evaluate_conformance(ConformanceInputs(
        CANDIDATE, parse_cite_status(no_wfs), "ancestor", None, _scoped_config()), now=NOW, sections=("cite",)
    )
    assert overall == "fail"
    assert _status(rows, "cite:coverage") == "fail"


def test_an_implemented_suite_regression_still_fails_under_scope():
    regressed = CITE_STATUS_MD.replace("| WFS 2.0 | `basic` | 167 / 167 |", "| WFS 2.0 | `basic` | 160 / 167 |")
    _, overall = evaluate_conformance(ConformanceInputs(
        CANDIDATE, parse_cite_status(regressed), "ancestor", None, _scoped_config()), now=NOW, sections=("cite",)
    )
    assert overall == "fail"


def test_cli_on_the_committed_config_writes_a_report(tmp_path):
    """No supplied evidence is blocked, never a pass, and the report is written."""
    import json
    from pathlib import Path
    import check_conformance_evidence as cce
    root = Path(cce.__file__).resolve().parents[1]
    out = tmp_path / "report.json"
    code = cce.main(["--manifest", str(root / "platform-manifest.yaml"), "--sections", "cite,stac",
                     "--json-out", str(out)])
    report = json.loads(out.read_text())
    assert report["overallStatus"] != "pass" and code == 1
    assert report["rows"]


def test_a_null_section_is_normalized_not_a_crash(tmp_path):
    import json
    import yaml
    from pathlib import Path
    import check_conformance_evidence as cce
    root = Path(cce.__file__).resolve().parents[1]
    config = yaml.safe_load((root / "certification/conformance-evidence.yaml").read_text())
    config["cite"] = None
    path = tmp_path / "conformance-evidence.yaml"
    path.write_text(yaml.safe_dump(config))
    out = tmp_path / "report.json"
    cce.main(["--manifest", str(root / "platform-manifest.yaml"), "--config", str(path),
              "--sections", "cite", "--json-out", str(out)])
    assert json.loads(out.read_text())["overallStatus"] != "pass"
