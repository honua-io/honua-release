"""Tests for the freeze-phase evidence lineage/freshness gate (honua-release#60, #84).

Proves the decision core can FAIL (a diverged evidence sha, a stale producer, a stalled honua-evidence
aggregator, an unowned ledger-red producer) and is BLOCKED — never a fake pass — on a missing matrix
or a producer absent from the freshness contract (honua-io/honua-evidence#8 not yet landed).

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
LEDGER = {"maxAgeHours": 36}


def _ack(producer, review_by="2026-12-31", issue="https://github.com/honua-io/honua-release/issues/89"):
    return {producer: ef.AcknowledgedProducer(producer=producer, issue=issue, reason="owned",
                                              since="2026-07-01", review_by=review_by)}


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


# --- ledger liveness (honua-release#84 / honua-evidence#17) ---------------------------------------
#
# These replay the real 2026-08-16 outage: honua-evidence's `aggregate` run parked in `waiting` on the
# github-pages environment and held the aggregate-pages concurrency group for 42h, freezing the whole
# matrix. The pre-#84 gate stayed GREEN through all of it because server-matrix's frozen fetchedAt was
# still inside its 48h window.

def _fresh_server_matrix(ts="2026-07-20T10:00:00Z"):
    return {"sourceVersion": f"{'a' * 40}@{ts}", "fetchedAt": ts, "status": "fresh"}


def test_stalled_aggregator_fails_even_while_every_producer_is_inside_its_window():
    frozen = (NOW - timedelta(hours=42)).strftime("%Y-%m-%dT%H:%M:%SZ")
    matrix = {"generatedAt": frozen,
              "freshness": {"server-matrix": _fresh_server_matrix(frozen),
                            "cite": {"fetchedAt": frozen, "status": "fresh"}}}
    rows, overall = ef.evaluate_evidence_freshness("a" * 40, matrix, "identical", THRESHOLDS,
                                                   now=NOW, ledger_policy=LEDGER)
    ledger = next(r for r in rows if r["check"] == "ledger")
    assert ledger["status"] == "fail"
    assert "aggregator is stalled" in ledger["why"]
    assert overall == "fail"
    # ...and this is the misattribution the check exists to prevent: both producers still PASS.
    assert all(r["status"] == "pass" for r in rows if r["check"].startswith("freshness:"))


def test_live_ledger_passes_the_liveness_check():
    matrix = {"generatedAt": (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "freshness": {"server-matrix": _fresh_server_matrix(),
                            "cite": {"fetchedAt": "2026-07-20T10:00:00Z", "status": "fresh"}}}
    rows, overall = ef.evaluate_evidence_freshness("a" * 40, matrix, "identical", THRESHOLDS,
                                                   now=NOW, ledger_policy=LEDGER)
    assert next(r for r in rows if r["check"] == "ledger")["status"] == "pass"
    assert overall == "pass"


def test_missing_or_unparseable_generated_at_is_blocked_never_pass():
    for generated in (None, "not-a-timestamp"):
        matrix = {"freshness": {"server-matrix": _fresh_server_matrix()}}
        if generated is not None:
            matrix["generatedAt"] = generated
        rows, _ = ef.evaluate_evidence_freshness("a" * 40, matrix, "identical", {}, now=NOW,
                                                 ledger_policy=LEDGER)
        assert next(r for r in rows if r["check"] == "ledger")["status"] == "blocked"


def test_ledger_check_is_skipped_when_no_policy_configured():
    matrix = {"generatedAt": "2020-01-01T00:00:00Z", "freshness": {"server-matrix": _fresh_server_matrix()}}
    rows, _ = ef.evaluate_evidence_freshness("a" * 40, matrix, "identical", {}, now=NOW)
    assert not [r for r in rows if r["check"] == "ledger"]


# --- ledger-declared producers + acknowledgements (honua-release#84 AC-2) --------------------------

def test_unlisted_ledger_red_producer_fails_when_no_issue_owns_it():
    matrix = {"freshness": {"server-matrix": _fresh_server_matrix(),
                            "live-canary": {"status": "stale", "ageDays": 11}}}
    rows, overall = ef.evaluate_evidence_freshness("a" * 40, matrix, "identical",
                                                   {"server-matrix": {"maxAgeHours": 48}}, now=NOW)
    row = next(r for r in rows if r["check"] == "producer:live-canary")
    assert row["status"] == "fail" and "no owning issue" in row["why"]
    assert overall == "fail"


def test_acknowledged_ledger_red_producer_does_not_redden_and_carries_its_issue():
    matrix = {"freshness": {"server-matrix": _fresh_server_matrix(),
                            "dr-drills": {"status": "missing", "detail": "none pushed yet"}}}
    rows, overall = ef.evaluate_evidence_freshness("a" * 40, matrix, "identical",
                                                   {"server-matrix": {"maxAgeHours": 48}}, now=NOW,
                                                   acknowledged=_ack("dr-drills"))
    row = next(r for r in rows if r["check"] == "producer:dr-drills")
    assert row["status"] == ef.ACKNOWLEDGED
    assert "honua-io/honua-release/issues/89" in row["why"]
    assert "none pushed yet" in row["why"]
    assert overall == "pass"


def test_expired_acknowledgement_stops_applying_and_fails_again():
    matrix = {"freshness": {"server-matrix": _fresh_server_matrix(),
                            "dr-drills": {"status": "missing", "detail": "none pushed yet"}}}
    rows, overall = ef.evaluate_evidence_freshness("a" * 40, matrix, "identical",
                                                   {"server-matrix": {"maxAgeHours": 48}}, now=NOW,
                                                   acknowledged=_ack("dr-drills", review_by="2026-07-19"))
    row = next(r for r in rows if r["check"] == "producer:dr-drills")
    assert row["status"] == "fail" and "EXPIRED" in row["why"]
    assert overall == "fail"


def test_acknowledgement_never_overrides_a_real_threshold():
    # A producer WITH a threshold is judged by that threshold. Otherwise an acknowledgement could be
    # used to silence server-matrix itself, which is the one producer the lineage check depends on.
    stale_ts = (NOW - timedelta(hours=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
    matrix = {"freshness": {"server-matrix": {"sourceVersion": f"{'a' * 40}@{stale_ts}",
                                              "fetchedAt": stale_ts, "status": "stale"}}}
    rows, overall = ef.evaluate_evidence_freshness("a" * 40, matrix, "identical",
                                                   {"server-matrix": {"maxAgeHours": 48}}, now=NOW,
                                                   acknowledged=_ack("server-matrix"))
    assert next(r for r in rows if r["check"] == "freshness:server-matrix")["status"] == "fail"
    assert not [r for r in rows if r["check"] == "producer:server-matrix"]
    assert overall == "fail"


def test_recovered_producer_reports_its_acknowledgement_as_rot():
    matrix = {"freshness": {"server-matrix": _fresh_server_matrix(),
                            "dr-drills": {"status": "fresh", "fetchedAt": "2026-07-20T10:00:00Z"}}}
    rows, overall = ef.evaluate_evidence_freshness("a" * 40, matrix, "identical",
                                                   {"server-matrix": {"maxAgeHours": 48}}, now=NOW,
                                                   acknowledged=_ack("dr-drills"))
    note = next(r for r in rows if r["check"] == "acknowledgement:dr-drills")
    assert note["status"] == ef.NOTE and "green again" in note["why"]
    assert overall == "pass"  # rot is loud, not red


def test_acknowledgement_for_a_producer_the_ledger_does_not_carry_is_reported_unknown():
    matrix = {"freshness": {"server-matrix": _fresh_server_matrix()}}
    rows, _ = ef.evaluate_evidence_freshness("a" * 40, matrix, "identical",
                                             {"server-matrix": {"maxAgeHours": 48}}, now=NOW,
                                             acknowledged=_ack("renamed-producer"))
    note = next(r for r in rows if r["check"] == "acknowledgement:renamed-producer")
    assert note["status"] == ef.NOTE and "does not carry" in note["why"]


def test_committed_config_ledger_and_acknowledgements_parse():
    policy = ef.load_ledger_policy()
    assert policy["maxAgeHours"] > 0
    acknowledged = ef.load_acknowledged()
    assert acknowledged, "the committed config should own its known-red producers, not hide them"
    for name, entry in acknowledged.items():
        assert entry.issue.startswith("https://github.com/"), name
        assert not entry.expired(datetime.now(timezone.utc).date()), \
            f"acknowledgement for {name} has expired — re-own it or delete it"
        # An acknowledgement must never shadow a producer that has a real threshold.
        assert name not in ef.load_thresholds(), name


def test_malformed_acknowledgement_raises_rather_than_silently_disabling_the_guard(tmp_path):
    cfg = tmp_path / "evidence-freshness.yaml"
    cfg.write_text("producers: {}\nacknowledged:\n  x:\n    issue: not-a-url\n    reason: r\n"
                   "    since: '2026-01-01'\n    reviewBy: '2026-12-31'\n", encoding="utf-8")
    try:
        ef.load_acknowledged(cfg)
    except ValueError as exc:
        assert "full URL" in str(exc)
    else:
        raise AssertionError("expected a ValueError for a non-URL issue reference")

    cfg.write_text("acknowledged:\n  x:\n    issue: https://example.test/1\n    reason: r\n",
                   encoding="utf-8")
    try:
        ef.load_acknowledged(cfg)
    except ValueError as exc:
        assert "missing required field" in str(exc)
    else:
        raise AssertionError("expected a ValueError for missing required fields")


if __name__ == "__main__":
    import inspect
    import tempfile
    import traceback

    # The pytest-free runner has to supply the one fixture these tests use, or it reports a false
    # FAIL for every tmp_path test and the advertised `python tools/test_check_evidence_freshness.py`
    # entrypoint lies about the suite's health.
    failures = 0
    for name, fn in sorted(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        try:
            params = inspect.signature(fn).parameters
            if "tmp_path" in params:
                with tempfile.TemporaryDirectory() as td:
                    fn(Path(td))
            else:
                fn()
            print(f"PASS {name}")
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{'OK' if not failures else 'FAILED'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)
