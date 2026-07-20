"""Tests for the freeze-phase evidence lineage/freshness gate (honua-release#60).

Proves the decision core can FAIL (a diverged evidence sha, a stale producer) and is BLOCKED — never
a fake pass — on a missing matrix or a producer absent from the freshness contract (honua-io/
honua-evidence#8 not yet landed).

Run: python -m pytest tools/test_check_evidence_freshness.py    (or: python tools/test_check_evidence_freshness.py)
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_evidence_freshness as ef  # noqa: E402

NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
THRESHOLDS = {"server-matrix": {"maxAgeHours": 48}, "cite": {"maxAgeHours": 336}}


def _matrix(server_matrix_entry=None, extra_freshness=None):
    freshness = {}
    if server_matrix_entry is not None:
        freshness["server-matrix"] = server_matrix_entry
    if extra_freshness:
        freshness.update(extra_freshness)
    return {"freshness": freshness}


def test_missing_matrix_is_blocked_never_pass():
    rows, overall = ef.evaluate_evidence_freshness("a" * 40, None, "identical", THRESHOLDS, now=NOW)
    assert overall == "blocked"
    assert rows[0]["check"] == "matrix"


def test_identical_lineage_and_fresh_producer_passes():
    matrix = _matrix({
        "sourceVersion": f"{'a' * 40}@2026-07-20T10:00:00Z",
        "fetchedAt": "2026-07-20T10:00:00Z",
        "status": "fresh",
    })
    rows, overall = ef.evaluate_evidence_freshness("a" * 40, matrix, "identical", THRESHOLDS, now=NOW)
    assert overall == "blocked"  # cite producer absent -> blocked overall, but lineage+server-matrix pass
    lineage = next(r for r in rows if r["check"] == "lineage")
    server = next(r for r in rows if r["check"] == "freshness:server-matrix")
    assert lineage["status"] == "pass"
    assert server["status"] == "pass"


def test_ancestor_and_descendant_lineage_both_pass():
    matrix = _matrix({
        "sourceVersion": f"{'b' * 40}@2026-07-20T10:00:00Z",
        "fetchedAt": "2026-07-20T10:00:00Z",
        "status": "fresh",
    })
    for status in ("ancestor", "descendant"):
        rows, _ = ef.evaluate_evidence_freshness("a" * 40, matrix, status, THRESHOLDS, now=NOW)
        lineage = next(r for r in rows if r["check"] == "lineage")
        assert lineage["status"] == "pass"


def test_diverged_lineage_fails():
    matrix = _matrix({
        "sourceVersion": f"{'b' * 40}@2026-07-20T10:00:00Z",
        "fetchedAt": "2026-07-20T10:00:00Z",
        "status": "fresh",
    })
    rows, overall = ef.evaluate_evidence_freshness("a" * 40, matrix, "diverged", THRESHOLDS, now=NOW)
    assert overall == "fail"
    lineage = next(r for r in rows if r["check"] == "lineage")
    assert lineage["status"] == "fail" and "DIVERGED" in lineage["why"]


def test_undecidable_lineage_is_blocked():
    matrix = _matrix({
        "sourceVersion": f"{'b' * 40}@2026-07-20T10:00:00Z",
        "fetchedAt": "2026-07-20T10:00:00Z",
        "status": "fresh",
    })
    rows, overall = ef.evaluate_evidence_freshness("a" * 40, matrix, None, THRESHOLDS, now=NOW)
    lineage = next(r for r in rows if r["check"] == "lineage")
    assert lineage["status"] == "blocked"
    assert overall in ("blocked",)  # nothing else fails


def test_no_candidate_sha_is_blocked():
    matrix = _matrix({
        "sourceVersion": f"{'b' * 40}@2026-07-20T10:00:00Z",
        "fetchedAt": "2026-07-20T10:00:00Z",
        "status": "fresh",
    })
    rows, overall = ef.evaluate_evidence_freshness(None, matrix, "identical", THRESHOLDS, now=NOW)
    lineage = next(r for r in rows if r["check"] == "lineage")
    assert lineage["status"] == "blocked" and "no honua-server sha pinned" in lineage["why"]


def test_stale_producer_fails():
    stale_ts = (NOW - timedelta(hours=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
    matrix = _matrix({
        "sourceVersion": f"{'a' * 40}@{stale_ts}",
        "fetchedAt": stale_ts,
        "status": "stale",
    })
    rows, overall = ef.evaluate_evidence_freshness("a" * 40, matrix, "identical", THRESHOLDS, now=NOW)
    assert overall == "fail"
    server = next(r for r in rows if r["check"] == "freshness:server-matrix")
    assert server["status"] == "fail" and "exceeds threshold" in server["why"]


def test_producer_absent_from_freshness_block_is_blocked_pointing_at_evidence_8():
    # cite is configured in THRESHOLDS but the matrix's freshness block (schemaVersion 2.0.0 today)
    # has no cite entry at all -- this is the honua-io/honua-evidence#8 pending state.
    matrix = _matrix({
        "sourceVersion": f"{'a' * 40}@2026-07-20T10:00:00Z",
        "fetchedAt": "2026-07-20T10:00:00Z",
        "status": "fresh",
    })
    rows, overall = ef.evaluate_evidence_freshness("a" * 40, matrix, "identical", THRESHOLDS, now=NOW)
    cite_row = next(r for r in rows if r["check"] == "freshness:cite")
    assert cite_row["status"] == "blocked"
    assert "honua-io/honua-evidence#8" in cite_row["why"]
    assert overall == "blocked"


def test_producer_status_missing_is_blocked():
    matrix = _matrix({
        "sourceVersion": f"{'a' * 40}@2026-07-20T10:00:00Z",
        "fetchedAt": "2026-07-20T10:00:00Z",
        "status": "fresh",
    }, extra_freshness={"cite": {"status": "missing", "detail": "no CITE run yet"}})
    rows, overall = ef.evaluate_evidence_freshness("a" * 40, matrix, "identical", THRESHOLDS, now=NOW)
    cite_row = next(r for r in rows if r["check"] == "freshness:cite")
    assert cite_row["status"] == "blocked" and "no CITE run yet" in cite_row["why"]


def test_source_sha_parsing():
    assert ef._source_sha({"sourceVersion": "3f47c4765a22@2026-07-17T17:50:20Z"}) == "3f47c4765a22"
    assert ef._source_sha({"sourceVersion": None}) is None
    assert ef._source_sha({}) is None


def test_load_matrix_missing_freshness_block_returns_none(tmp_path):
    import json
    bad = tmp_path / "matrix.json"
    bad.write_text(json.dumps({"capabilities": []}), encoding="utf-8")
    assert ef.load_matrix(str(bad)) is None


def test_load_thresholds_reads_committed_config():
    thresholds = ef.load_thresholds()
    assert "server-matrix" in thresholds
    assert thresholds["server-matrix"]["maxAgeHours"] > 0
    assert "cite" in thresholds


if __name__ == "__main__":
    import traceback

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"\n{'OK' if not failures else 'FAILED'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)
