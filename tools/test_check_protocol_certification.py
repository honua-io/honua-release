from __future__ import annotations

import copy
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_protocol_certification as cert  # noqa: E402

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
CUT = "2026-08-20T09:00:00Z"


def _cell(**overrides):
    value = {
        "capability_key": "serve.cog",
        "surface": "cog",
        "operation": "window-read",
        "maturity": "supported",
        "canonical_client": "Rasterio",
        "client_lane": "rasterio",
        "client_version": "1.4.3",
        "deployment_target": "local-docker",
        "required_tier": "nightly",
        "licensed": False,
        "addressable_by_client": True,
        "addressability_reason": None,
        "result": "pass",
        "skip_reason": None,
        "scenario_facets": ["positive", "metadata", "range-efficiency"],
        "contract_revision": "cog-1.0",
        "auth_policy_revision": "anonymous-v1",
        "source_sha": SHA,
        "image_digest": DIGEST,
        "fixture_revision": "fixture-cog-v1",
        "evidence_uri": "https://evidence.honua.io/runs/1",
        "started_at": "2026-08-20T10:00:00Z",
        "completed_at": "2026-08-20T10:05:00Z",
    }
    value.update(overrides)
    return value


def _ledger(*cells):
    return {
        "schema": cert.SCHEMA_ID,
        "requirements_revision": "requirements-test-v1",
        "requirements_complete": True,
        "generated_at": "2026-08-20T10:06:00Z",
        "candidate": {"source_sha": SHA, "image_digest": DIGEST, "cut_at": "2026-08-20T09:00:00Z"},
        "cells": list(cells or [_cell()]),
    }


def _requirements(*cells, complete=True):
    return {
        "schema": cert.REQUIREMENTS_SCHEMA_ID,
        "revision": "requirements-test-v1",
        "complete": complete,
        "requirements": [
            {field: cell[field] for field in cert.REQUIREMENT_FIELDS}
            for cell in (cells or [_cell()])
        ],
    }


def _evaluate(ledger, tier, **kwargs):
    if tier == "release":
        kwargs.setdefault("expected_cut_at", CUT)
    return cert.evaluate(
        ledger,
        tier,
        requirements=_requirements(*ledger["cells"]),
        **kwargs,
    )


def test_fresh_nightly_required_cell_passes():
    report = _evaluate(_ledger(), "nightly", expected_source_sha=SHA, now=NOW)
    assert report["overall_status"] == "pass"


def test_required_skip_fails_closed():
    report = _evaluate(_ledger(_cell(result="skip", skip_reason="client unavailable")), "nightly", now=NOW)
    assert report["overall_status"] == "fail"


def test_duplicate_normalized_key_fails():
    cell = _cell()
    report = _evaluate(_ledger(cell, copy.deepcopy(cell)), "nightly", now=NOW)
    assert report["overall_status"] == "fail"
    assert any("duplicate" in finding["why"] for finding in report["findings"])


def test_non_addressable_requires_reason_and_matching_result():
    report = _evaluate(_ledger(_cell(addressable_by_client=False, result="pass")), "release", now=NOW)
    assert report["overall_status"] == "fail"


def test_supported_operation_needs_an_addressable_client_at_release():
    cell = _cell(addressable_by_client=False, result="not-addressable", addressability_reason="API absent in client")
    report = _evaluate(_ledger(cell), "release", now=NOW)
    assert report["overall_status"] == "fail"
    assert any(finding["check"] == "addressability" for finding in report["findings"])


def test_nightly_older_than_seven_days_fails():
    report = _evaluate(_ledger(_cell(completed_at="2026-08-10T10:00:00Z")), "nightly", now=NOW)
    assert report["overall_status"] == "fail"


def test_licensed_evidence_older_than_72_hours_fails():
    report = _evaluate(_ledger(_cell(licensed=True, completed_at="2026-08-16T10:00:00Z")), "nightly", now=NOW)
    assert report["overall_status"] == "fail"


def test_release_requires_exact_digest_and_post_cut_execution():
    cell = _cell(image_digest="sha256:" + "c" * 64, completed_at="2026-08-20T08:00:00Z")
    report = _evaluate(_ledger(cell), "release", expected_image_digest=DIGEST, now=NOW)
    assert report["overall_status"] == "fail"
    assert len(report["findings"]) >= 2


def test_preview_failure_does_not_block_release_claim():
    preview = _cell(maturity="preview", result="fail")
    supported = _cell(canonical_client="GDAL", client_lane="gdal", client_version="3.11.4")
    report = _evaluate(_ledger(preview, supported), "release", now=NOW)
    assert report["overall_status"] == "pass"


def test_incomplete_denominator_can_never_certify_any_tier():
    for tier in cert.TIERS:
        ledger = _ledger()
        ledger["requirements_complete"] = False
        report = cert.evaluate(
            ledger,
            tier,
            requirements=_requirements(complete=False),
            expected_cut_at=CUT if tier == "release" else None,
            now=NOW,
        )
        assert report["overall_status"] == "fail"
        assert any(finding["check"] == "requirements_complete" for finding in report["findings"])


def test_ledger_cannot_invent_or_omit_owned_requirements():
    ledger = _ledger()
    owned = _requirements(_cell(), _cell(canonical_client="GDAL", client_lane="gdal", client_version="3.11.4"))

    report = cert.evaluate(ledger, "nightly", requirements=owned, now=NOW)

    assert report["overall_status"] == "fail"
    assert any(finding["check"] == "requirements_denominator" for finding in report["findings"])


def test_scoped_cells_must_match_ledger_candidate_without_cli_pins():
    report = _evaluate(_ledger(_cell(source_sha="c" * 40, image_digest="sha256:" + "d" * 64)), "nightly", now=NOW)
    assert report["overall_status"] == "fail"
    assert any("ledger candidate" in finding["why"] for finding in report["findings"])


def test_future_candidate_and_evidence_timestamps_fail():
    ledger = _ledger(_cell(started_at="2099-01-01T00:00:00Z", completed_at="2099-01-01T00:01:00Z"))
    ledger["generated_at"] = "2099-01-01T00:02:00Z"
    ledger["candidate"]["cut_at"] = "2099-01-01T00:00:00Z"

    report = _evaluate(ledger, "nightly", now=NOW)

    assert report["overall_status"] == "fail"
    assert sum("future" in finding["why"] for finding in report["findings"]) >= 4


def test_release_execution_must_start_after_cut():
    ledger = _ledger(_cell(started_at="2026-08-20T08:59:00Z", completed_at="2026-08-20T09:01:00Z"))

    report = _evaluate(ledger, "release", now=NOW)

    assert report["overall_status"] == "fail"
    assert any("started before independently frozen candidate cut" in finding["why"] for finding in report["findings"])


def test_release_requires_external_cut_and_rejects_backdated_ledger_cut():
    ledger = _ledger(_cell(started_at="2026-08-20T08:30:00Z"))
    ledger["candidate"]["cut_at"] = "2026-08-20T08:00:00Z"

    missing = cert.evaluate(ledger, "release", requirements=_requirements(*ledger["cells"]), now=NOW)
    mismatched = cert.evaluate(
        ledger,
        "release",
        requirements=_requirements(*ledger["cells"]),
        expected_cut_at=CUT,
        now=NOW,
    )

    assert any(finding["check"] == "expected_cut_at" for finding in missing["findings"])
    assert any("does not match" in finding["why"] for finding in mismatched["findings"])
    assert any("started before independently frozen" in finding["why"] for finding in mismatched["findings"])


def test_release_rejects_naive_external_cut_without_throwing():
    report = cert.evaluate(
        _ledger(),
        "release",
        requirements=_requirements(),
        expected_cut_at=datetime(2026, 8, 20, 9, 0),
        now=NOW,
    )

    assert report["overall_status"] == "fail"
    assert any(finding["check"] == "expected_cut_at" for finding in report["findings"])
