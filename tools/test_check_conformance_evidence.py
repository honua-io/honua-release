"""Unit tests for the conformance-evidence federation gate (honua-release#133).

The point of this gate is that it can FAIL. Most of these tests therefore assert failure paths:
diverged lineage, a stale snapshot, a regressed suite, a bundle certifying the wrong image. A gate
that only ever reports pass or blocked is the defect this module was written to remove.
"""
from datetime import datetime, timezone

from check_conformance_evidence import (
    ConformanceEvidenceError,
    evaluate_conformance,
    parse_cite_status,
    summarize_esri_bundle,
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
    "esri": {"expectedImage": "ghcr.io/honua-io/honua-server:nightly-aot-6b6d3b8"},
}

GOOD_STAC = {
    "conformsTo": [
        "https://api.stacspec.org/v1.0.0/core",
        "https://api.stacspec.org/v1.0.0/collections",
    ],
    "errors": [],
}

GOOD_ESRI = summarize_esri_bundle(
    [{"image": "ghcr.io/honua-io/honua-server:nightly-aot-6b6d3b8", "checks": [{"id": "FS-OP-QUERY", "status": "pass"}]}]
)


def _status(rows, check):
    return next(r["status"] for r in rows if r["check"] == check)


# ---------------------------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------------------------
# The happy path — everything genuinely proven
# ---------------------------------------------------------------------------------------------
def test_fully_federated_candidate_passes():
    rows, overall = evaluate_conformance(
        CANDIDATE, parse_cite_status(CITE_STATUS_MD), "ancestor", GOOD_STAC, GOOD_ESRI, CONFIG, now=NOW
    )
    assert overall == "pass", [r for r in rows if r["status"] != "pass"]
    assert _status(rows, "cite:lineage") == "pass"
    assert _status(rows, "cite:suites") == "pass"
    assert _status(rows, "stac:validator") == "pass"
    assert _status(rows, "esri:binding") == "pass"


def test_ancestor_lineage_is_accepted_deliberately():
    # CITE runs on its own weekly cadence, so the sha it certified is normally BEHIND a manifest
    # pinned later. That is lineage continuity, not a break -- the same rule the freeze-phase
    # evidence gate already proved out.
    for relation in ("identical", "ancestor", "descendant"):
        rows, _ = evaluate_conformance(
            CANDIDATE, parse_cite_status(CITE_STATUS_MD), relation, GOOD_STAC, GOOD_ESRI, CONFIG, now=NOW
        )
        assert _status(rows, "cite:lineage") == "pass", relation


# ---------------------------------------------------------------------------------------------
# Failure paths — the reason this gate exists
# ---------------------------------------------------------------------------------------------
def test_diverged_lineage_fails():
    rows, overall = evaluate_conformance(
        CANDIDATE, parse_cite_status(CITE_STATUS_MD), "diverged", GOOD_STAC, GOOD_ESRI, CONFIG, now=NOW
    )
    assert overall == "fail"
    assert _status(rows, "cite:lineage") == "fail"


def test_stale_snapshot_fails_rather_than_blocking():
    # Obtained-but-too-old is a decided failure, not a bootstrap gap.
    late = datetime(2026, 9, 30, tzinfo=timezone.utc)
    rows, overall = evaluate_conformance(
        CANDIDATE, parse_cite_status(CITE_STATUS_MD), "ancestor", GOOD_STAC, GOOD_ESRI, CONFIG, now=late
    )
    assert overall == "fail"
    assert _status(rows, "cite:freshness") == "fail"


def test_a_regressed_suite_fails():
    regressed = CITE_STATUS_MD.replace("| WFS 2.0 | `basic` | 167 / 167 |", "| WFS 2.0 | `basic` | 160 / 167 |")
    rows, overall = evaluate_conformance(
        CANDIDATE, parse_cite_status(regressed), "ancestor", GOOD_STAC, GOOD_ESRI, CONFIG, now=NOW
    )
    assert overall == "fail"
    assert "WFS 2.0 160/167" in next(r["why"] for r in rows if r["check"] == "cite:suites")


def test_a_vanished_required_suite_fails():
    # Dropping a row must not read as "nothing regressed" -- that shrinks the denominator silently.
    shrunk = "\n".join(line for line in CITE_STATUS_MD.splitlines() if not line.startswith("| WMTS 1.0"))
    rows, overall = evaluate_conformance(
        CANDIDATE, parse_cite_status(shrunk), "ancestor", GOOD_STAC, GOOD_ESRI, CONFIG, now=NOW
    )
    assert overall == "fail"
    assert _status(rows, "cite:coverage") == "fail"


def test_all_passed_false_fails_even_when_the_table_looks_clean():
    doctored = CITE_STATUS_MD.replace("allPassed=true", "allPassed=false")
    rows, overall = evaluate_conformance(
        CANDIDATE, parse_cite_status(doctored), "ancestor", GOOD_STAC, GOOD_ESRI, CONFIG, now=NOW
    )
    assert overall == "fail"
    assert _status(rows, "cite:allPassed") == "fail"


def test_missing_stac_conformance_class_fails():
    stripped = {"conformsTo": ["https://api.stacspec.org/v1.0.0/core"], "errors": []}
    rows, overall = evaluate_conformance(
        CANDIDATE, parse_cite_status(CITE_STATUS_MD), "ancestor", stripped, GOOD_ESRI, CONFIG, now=NOW
    )
    assert overall == "fail"
    assert _status(rows, "stac:conformance-classes") == "fail"


def test_stac_validator_errors_fail():
    broken = dict(GOOD_STAC, errors=["item-search: sortby not honoured"])
    rows, overall = evaluate_conformance(
        CANDIDATE, parse_cite_status(CITE_STATUS_MD), "ancestor", broken, GOOD_ESRI, CONFIG, now=NOW
    )
    assert overall == "fail"
    assert _status(rows, "stac:validator") == "fail"


def test_esri_bundle_for_a_different_image_fails():
    # Evidence about another build reads as a pass unless the binding is checked. This is the exact
    # failure mode the candidate-binding work exists to prevent, applied to a federated source.
    wrong = summarize_esri_bundle([{"image": "ghcr.io/honua-io/honua-server:nightly-aot-deadbee", "checks": []}])
    rows, overall = evaluate_conformance(
        CANDIDATE, parse_cite_status(CITE_STATUS_MD), "ancestor", GOOD_STAC, wrong, CONFIG, now=NOW
    )
    assert overall == "fail"
    assert _status(rows, "esri:binding") == "fail"


def test_esri_bundle_bound_by_manifest_digest_passes():
    digest = "sha256:78e3088d64d832d3e2752c87d80bfcad201b414f4525989ca5d9a242cd5fee8a"
    config = {**CONFIG, "esri": {**CONFIG["esri"], "expectedDigest": digest}}
    by_digest = summarize_esri_bundle(
        [{"image": f"ghcr.io/honua-io/honua-server@{digest}", "checks": []}]
    )
    rows, overall = evaluate_conformance(
        CANDIDATE, parse_cite_status(CITE_STATUS_MD), "ancestor", GOOD_STAC, by_digest, config, now=NOW
    )
    assert overall == "pass"
    assert _status(rows, "esri:binding") == "pass"


def test_esri_lane_failures_fail():
    failing = summarize_esri_bundle(
        [
            {
                "image": "ghcr.io/honua-io/honua-server:nightly-aot-6b6d3b8",
                "checks": [{"id": "IS-OP-TILE", "status": "fail", "notes": "tile failed (200)"}],
            }
        ]
    )
    rows, overall = evaluate_conformance(
        CANDIDATE, parse_cite_status(CITE_STATUS_MD), "ancestor", GOOD_STAC, failing, CONFIG, now=NOW
    )
    assert overall == "fail"
    assert "IS-OP-TILE" in next(r["why"] for r in rows if r["check"] == "esri:lanes")


def test_summarize_walks_both_cert_and_coverage_shapes():
    # The harness emits CERT records keyed by test_case_id and operation-coverage records keyed by
    # id. A summary blind to either dimension would under-report.
    summary = summarize_esri_bundle(
        [
            {"results": [{"test_case_id": "CERT-CONN-01", "status": "fail", "notes": "no VTS"}]},
            {"coverage": [{"id": "IS-OP-LEGEND", "status": "fail"}]},
        ]
    )
    assert len(summary["failures"]) == 2
    assert {"CERT-CONN-01", "IS-OP-LEGEND"} == {f["id"] for f in summary["failures"]}


# ---------------------------------------------------------------------------------------------
# Blocked paths — honest bootstrap, never a fake pass
# ---------------------------------------------------------------------------------------------
def test_unfetchable_snapshot_blocks_and_never_passes():
    rows, overall = evaluate_conformance(CANDIDATE, None, None, GOOD_STAC, GOOD_ESRI, CONFIG, now=NOW)
    assert overall == "blocked"
    assert _status(rows, "cite:snapshot") == "blocked"


def test_undecidable_lineage_blocks():
    rows, overall = evaluate_conformance(
        CANDIDATE, parse_cite_status(CITE_STATUS_MD), None, GOOD_STAC, GOOD_ESRI, CONFIG, now=NOW
    )
    assert overall == "blocked"
    assert _status(rows, "cite:lineage") == "blocked"


def test_missing_stac_and_esri_block_rather_than_pass():
    rows, overall = evaluate_conformance(
        CANDIDATE, parse_cite_status(CITE_STATUS_MD), "ancestor", None, None, CONFIG, now=NOW
    )
    assert overall == "blocked"
    assert _status(rows, "stac:validator") == "blocked"
    assert _status(rows, "esri:bundle") == "blocked"


def test_fail_outranks_blocked():
    # A real regression must not be masked by an unrelated bootstrap gap in the same run.
    rows, overall = evaluate_conformance(
        CANDIDATE, parse_cite_status(CITE_STATUS_MD), "diverged", None, None, CONFIG, now=NOW
    )
    assert overall == "fail"
