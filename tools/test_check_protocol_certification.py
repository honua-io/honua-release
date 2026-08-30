from __future__ import annotations

import base64
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_protocol_certification as cert  # noqa: E402

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
SHA = "a" * 40
REQUIREMENTS_SOURCE_SHA = "d" * 40
DIGEST = "sha256:" + "b" * 64
CUT = "2026-08-20T09:00:00Z"
SHIPPED_CLIENT_VERSIONS = {
    "sdk-js": "0.1.9-beta.0",
    "sdk-python": "0.1.10",
    "sdk-dotnet": "1.6.0",
}


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
        "entitlement_policy_revision": None,
        "addressable_by_client": True,
        "addressability_reason": None,
        "result": "pass",
        "skip_reason": None,
        "scenario_facets": ["positive", "metadata", "range-efficiency"],
        "contract_revision": "cog-1.0",
        "auth_policy_revision": "anonymous-v1",
        "source_sha": SHA,
        "producer_source_sha": SHA,
        "image_digest": DIGEST,
        "fixture_revision": "fixture-cog-v1",
        "evidence_uri": None,
        "evidence_digest": None,
        "evidence_receipt": None,
        "facet_results": None,
        "started_at": "2026-08-20T10:00:00Z",
        "completed_at": "2026-08-20T10:05:00Z",
        "budget_expectations": None,
        "budget_observations": None,
    }
    value.update(overrides)
    if "evidence_receipt" not in overrides:
        identity = {
            field: value[field] for field in cert.RECEIPT_ID_FIELDS
        }
        if isinstance(value.get("test_ids"), list):
            identity["test_ids"] = value["test_ids"]
        value["evidence_receipt"] = {
            "schema": "honua.certification-evidence-receipt/v1",
            "identity": identity,
            "result": value["result"],
            "facets": {facet: "pass" for facet in value["scenario_facets"]},
            "payload_base64": "dGVzdA==",
        }
    if "evidence_digest" not in overrides:
        value["evidence_digest"] = cert._receipt_digest(value["evidence_receipt"])
    if "evidence_uri" not in overrides:
        value["evidence_uri"] = "https://evidence.honua.io/data/sha256/" + value["evidence_digest"][7:]
    if "facet_results" not in overrides:
        value["facet_results"] = {
            facet: {"result": "pass", "evidence_digest": value["evidence_digest"]}
            for facet in value["scenario_facets"]
        }
    return value


def _bind_format_budget_payload(cell):
    payload = {
        "schema": "honua.format-budget-observations/v1",
        "budget_observations": cell["budget_observations"],
    }
    cell["evidence_receipt"]["payload_base64"] = base64.b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    cell["evidence_digest"] = cert._receipt_digest(cell["evidence_receipt"])
    cell["evidence_uri"] = "https://evidence.honua.io/data/sha256/" + cell["evidence_digest"][7:]
    cell["facet_results"] = {
        facet: {"result": "pass", "evidence_digest": cell["evidence_digest"]}
        for facet in cell["scenario_facets"]
    }
    return cell


def _licensed_cell(
    *,
    policy="honua-pro-feature-subscriptions-v1",
    deployment_target="licensed-release",
    auth_policy_revision="api-key-protected-v1",
    checked_at="2026-08-20T10:02:00Z",
    **overrides,
):
    value = _cell(
        licensed=True,
        entitlement_policy_revision=policy,
        deployment_target=deployment_target,
        auth_policy_revision=auth_policy_revision,
        **overrides,
    )
    value["evidence_receipt"]["identity"]["entitlement_policy_revision"] = policy
    value["evidence_receipt"]["entitlement"] = {
        "policy_revision": policy,
        "capability_key": value["capability_key"],
        "deployment_target": deployment_target,
        "verification": "live-server-capability-probe-v1",
        "status": "active",
        "checked_at": checked_at,
        "license_fingerprint": "sha256:" + "e" * 64,
    }
    value["evidence_digest"] = cert._receipt_digest(value["evidence_receipt"])
    value["evidence_uri"] = "https://evidence.honua.io/data/sha256/" + value["evidence_digest"][7:]
    value["facet_results"] = {
        facet: {"result": "pass", "evidence_digest": value["evidence_digest"]}
        for facet in value["scenario_facets"]
    }
    return value


def _ledger(*cells):
    return {
        "schema": cert.SCHEMA_ID,
        "requirements_revision": "requirements-test-v1",
        "requirements_source_revision": REQUIREMENTS_SOURCE_SHA,
        "requirements_complete": True,
        "generated_at": "2026-08-20T10:06:00Z",
        "candidate": {"source_sha": SHA, "image_digest": DIGEST, "cut_at": "2026-08-20T09:00:00Z"},
        "cells": list(cells or [_cell()]),
    }


def _requirements(*cells, complete=True):
    return {
        "schema": cert.REQUIREMENTS_SCHEMA_ID,
        "revision": "requirements-test-v1",
        "receipt_schema_min": "v1",
        "complete": complete,
        "source_revisions": {
            "server": {"commit": SHA},
            "server-certification": {"commit": SHA},
            "sdk-js": {"commit": SHA},
            "sdk-python": {"commit": SHA},
            "sdk-dotnet": {"commit": SHA},
            "geospatial-grpc": {"commit": SHA},
            "geospatial-mcp": {"commit": SHA},
        },
        "requirements": [
            {field: cell[field] for field in cert.REQUIREMENT_FIELDS if field in cell}
            for cell in (cells or [_cell()])
        ],
    }


def _evaluate(ledger, tier, **kwargs):
    if tier == "release":
        kwargs.setdefault("expected_cut_at", CUT)
        kwargs.setdefault("expected_image_digest", DIGEST)
        kwargs.setdefault(
            "expected_component_source_shas",
            {source: SHA for source in cert.FROZEN_RELEASE_SOURCES},
        )
        kwargs.setdefault("expected_client_versions", SHIPPED_CLIENT_VERSIONS)
    requirements = kwargs.pop("requirements", _requirements(*ledger["cells"]))
    return cert.evaluate(
        ledger,
        tier,
        requirements=requirements,
        **kwargs,
    )


def test_release_shipped_client_version_match_passes():
    cell = _cell(
        canonical_client="Honua SDK .NET",
        client_lane="sdk-dotnet",
        client_version="1.6.0",
    )
    report = _evaluate(_ledger(cell), "release", expected_source_sha=SHA, now=NOW)
    assert report["overall_status"] == "pass"


def test_release_shipped_client_version_mismatch_fails():
    cell = _cell(
        canonical_client="Honua SDK Python",
        client_lane="sdk-python",
        client_version="0.1.11",
    )
    report = _evaluate(_ledger(cell), "release", now=NOW)
    assert report["overall_status"] == "fail"
    assert any("does not match shipped sdk-python artifact version" in finding["why"] for finding in report["findings"])


def test_release_requires_all_shipped_client_versions():
    report = _evaluate(_ledger(), "release", expected_client_versions={}, now=NOW)
    assert report["overall_status"] == "fail"
    assert {
        finding["check"]
        for finding in report["findings"]
        if finding["check"].startswith("expected_client_versions.")
    } == {
        "expected_client_versions.sdk-js",
        "expected_client_versions.sdk-python",
        "expected_client_versions.sdk-dotnet",
    }


def test_non_release_tiers_do_not_bind_shipped_client_versions():
    cell = _cell(
        canonical_client="Honua SDK .NET",
        client_lane="sdk-dotnet",
        client_version="source-preview",
        required_tier="pr",
    )
    for tier in ("pr", "nightly"):
        report = _evaluate(_ledger(cell), tier, expected_client_versions={}, now=NOW)
        assert report["overall_status"] == "pass"


def test_catalog_server_revision_must_match_candidate():
    requirements = _requirements()
    requirements["source_revisions"]["server"]["commit"] = "f" * 40
    report = _evaluate(
        _ledger(),
        "nightly",
        requirements=requirements,
        now=NOW,
    )
    assert report["overall_status"] == "fail"
    assert any(
        finding["check"] == "requirements.source_revisions.server.commit"
        for finding in report["findings"]
    )


def test_producer_source_sha_must_match_owned_client_revision():
    cell = _cell(
        client_lane="sdk-js-certification",
        producer_source_sha="f" * 40,
    )
    requirements = _requirements(cell)
    requirements["source_revisions"]["sdk-js"] = {"commit": "d" * 40}
    report = _evaluate(
        _ledger(cell),
        "nightly",
        requirements=requirements,
        now=NOW,
    )
    assert report["overall_status"] == "fail"
    assert any("owned sdk-js revision" in finding["why"] for finding in report["findings"])


def test_server_harness_pass_binds_test_ids_and_certification_source_revision():
    harness_sha = "c" * 40
    test_ids = ["EdrEndpointsTests.Edr_Cube_ReturnsCoverageJsonGridSubset"]
    cell = _cell(
        capability_key="serve.ogc-api-edr",
        surface="ogc-api-edr",
        operation="GET /edr/collections/{collectionId}/cube",
        canonical_client="Honua server public protocol integration harness",
        client_lane="server-protocol-harness",
        client_version=f"source@{harness_sha}",
        deployment_target="source-test-host",
        producer_source_sha=harness_sha,
        image_digest=None,
        test_ids=test_ids,
    )
    requirements = _requirements(cell)
    requirements["source_revisions"]["server-certification"] = {"commit": harness_sha}
    passing = _evaluate(_ledger(cell), "nightly", requirements=requirements, now=NOW)
    assert passing["overall_status"] == "pass"

    wrong_tests = copy.deepcopy(cell)
    wrong_tests["evidence_receipt"]["identity"]["test_ids"] = ["OtherTests.NotTheGovernedTest"]
    wrong_tests["evidence_digest"] = cert._receipt_digest(wrong_tests["evidence_receipt"])
    wrong_tests["evidence_uri"] = "https://evidence.honua.io/data/sha256/" + wrong_tests["evidence_digest"][7:]
    wrong_tests["facet_results"] = {
        facet: {"result": "pass", "evidence_digest": wrong_tests["evidence_digest"]}
        for facet in wrong_tests["scenario_facets"]
    }
    wrong_test_report = _evaluate(
        _ledger(wrong_tests), "nightly", requirements=requirements, now=NOW,
    )
    assert wrong_test_report["overall_status"] == "fail"
    assert any("semantically bound" in finding["why"] for finding in wrong_test_report["findings"])

    falsely_bound = _cell(
        capability_key="serve.ogc-api-edr",
        surface="ogc-api-edr",
        operation="GET /edr/collections/{collectionId}/cube",
        canonical_client="Honua server public protocol integration harness",
        client_lane="server-protocol-harness",
        client_version=f"source@{harness_sha}",
        deployment_target="source-test-host",
        producer_source_sha=harness_sha,
        image_digest=DIGEST,
        test_ids=test_ids,
    )
    false_report = _evaluate(
        _ledger(falsely_bound), "nightly", requirements=requirements, now=NOW,
    )
    assert false_report["overall_status"] == "fail"
    assert any("must not claim candidate image" in finding["why"] for finding in false_report["findings"])

    deployed_without_digest = _cell(image_digest=None)
    deployed_report = _evaluate(_ledger(deployed_without_digest), "nightly", now=NOW)
    assert deployed_report["overall_status"] == "fail"
    assert any("does not match ledger candidate" in finding["why"] for finding in deployed_report["findings"])

    wrong_source = copy.deepcopy(cell)
    wrong_source["producer_source_sha"] = "f" * 40
    wrong_source["evidence_receipt"]["identity"]["producer_source_sha"] = "f" * 40
    wrong_source["evidence_digest"] = cert._receipt_digest(wrong_source["evidence_receipt"])
    wrong_source["evidence_uri"] = "https://evidence.honua.io/data/sha256/" + wrong_source["evidence_digest"][7:]
    wrong_source["facet_results"] = {
        facet: {"result": "pass", "evidence_digest": wrong_source["evidence_digest"]}
        for facet in wrong_source["scenario_facets"]
    }
    wrong_source_report = _evaluate(
        _ledger(wrong_source), "nightly", requirements=requirements, now=NOW,
    )
    assert wrong_source_report["overall_status"] == "fail"
    assert any("owned server-certification revision" in finding["why"] for finding in wrong_source_report["findings"])


def test_cloud_native_pass_requires_owned_budget_and_observations():
    missing = _cell(capability_key="format.cog")
    missing_report = _evaluate(_ledger(missing), "nightly", now=NOW)
    assert missing_report["overall_status"] == "fail"
    assert any("governed fixture budgets" in finding["why"] for finding in missing_report["findings"])

    expectations = {
        "max_requests": 4,
        "max_transferred_bytes": 1_000_000,
        "max_full_object_downloads": 0,
        "min_range_requests": 1,
        "min_cache_hits": 0,
        "max_coordinate_error": 0.000001,
        "max_geometry_error": 0.000001,
        "required_metadata": ["crs", "nodata"],
        "expected_metadata": {"crs": "EPSG:4326", "nodata": -9999.0},
    }
    observations = {
        "requests": 3,
        "transferred_bytes": 500_000,
        "full_object_downloads": 0,
        "range_requests": 2,
        "cache_hits": 0,
        "coordinate_error": 0.0,
        "geometry_error": 0.0,
        "metadata_assertions": ["crs", "nodata"],
        "metadata_values": {"crs": "EPSG:4326", "nodata": -9999.0},
    }
    passing = _bind_format_budget_payload(_cell(
        capability_key="format.cog",
        budget_expectations=expectations,
        budget_observations=observations,
    ))
    passing_report = _evaluate(_ledger(passing), "nightly", now=NOW)
    assert passing_report["overall_status"] == "pass", passing_report["findings"]

    unbound = copy.deepcopy(passing)
    unbound["budget_observations"]["requests"] = 2
    unbound_report = _evaluate(_ledger(unbound), "nightly", now=NOW)
    assert unbound_report["overall_status"] == "fail"
    assert any("semantically bound" in finding["why"] for finding in unbound_report["findings"])

    exceeding = copy.deepcopy(passing)
    exceeding["budget_observations"]["requests"] = 5
    exceeding_report = _evaluate(_ledger(exceeding), "nightly", now=NOW)
    assert exceeding_report["overall_status"] == "fail"
    assert any("max_requests" in finding["why"] for finding in exceeding_report["findings"])

    for field, invalid in (
        ("requests", -1),
        ("transferred_bytes", 1.5),
        ("range_requests", -1),
        ("coordinate_error", float("nan")),
        ("coordinate_error", 10**400),
        ("geometry_error", -0.1),
    ):
        invalid_cell = copy.deepcopy(passing)
        invalid_cell["budget_observations"][field] = invalid
        invalid_report = _evaluate(_ledger(invalid_cell), "nightly", now=NOW)
        assert invalid_report["overall_status"] == "fail"
        assert any(field in finding["why"] for finding in invalid_report["findings"])

    wrong_metadata = copy.deepcopy(passing)
    wrong_metadata["budget_observations"]["metadata_values"]["crs"] = "EPSG:3857"
    wrong_metadata_report = _evaluate(_ledger(wrong_metadata), "nightly", now=NOW)
    assert wrong_metadata_report["overall_status"] == "fail"
    assert any("metadata value 'crs'" in finding["why"] for finding in wrong_metadata_report["findings"])

    wrong_metadata_type = copy.deepcopy(passing)
    wrong_metadata_type["budget_observations"]["metadata_values"]["nodata"] = -9999
    wrong_metadata_type_report = _evaluate(_ledger(wrong_metadata_type), "nightly", now=NOW)
    assert wrong_metadata_type_report["overall_status"] == "fail"
    assert any("metadata value 'nodata'" in finding["why"] for finding in wrong_metadata_type_report["findings"])

    duplicate_metadata = copy.deepcopy(passing)
    duplicate_metadata["budget_observations"]["metadata_assertions"].append("crs")
    duplicate_metadata_report = _evaluate(_ledger(duplicate_metadata), "nightly", now=NOW)
    assert duplicate_metadata_report["overall_status"] == "fail"
    assert any("metadata assertions" in finding["why"] for finding in duplicate_metadata_report["findings"])

    extra_observation = copy.deepcopy(passing)
    extra_observation["budget_observations"]["untrusted"] = 0
    extra_observation_report = _evaluate(_ledger(extra_observation), "nightly", now=NOW)
    assert extra_observation_report["overall_status"] == "fail"
    assert any("closed governed fields" in finding["why"] for finding in extra_observation_report["findings"])


def test_format_budget_receipt_rejects_ambiguous_or_pathological_json():
    cell = _cell(capability_key="format.cog")
    cell["budget_observations"] = {
        "requests": 3,
        "transferred_bytes": 500_000,
        "full_object_downloads": 0,
        "range_requests": 2,
        "cache_hits": 1,
        "coordinate_error": 0.0,
        "geometry_error": 0.0,
        "metadata_assertions": ["crs"],
        "metadata_values": {"crs": "EPSG:4326"},
    }
    duplicate = (
        b'{"schema":"honua.format-budget-observations/v1",'
        b'"budget_observations":{"metadata_values":{"crs":"EPSG:3857","crs":"EPSG:4326"}}}'
    )
    deeply_nested = (
        b'{"schema":"honua.format-budget-observations/v1","budget_observations":'
        + b"[" * 2000
        + b"0"
        + b"]" * 2000
        + b"}"
    )
    oversized = b"x" * (cert.MAX_FORMAT_RECEIPT_PAYLOAD_BYTES + 1)

    for payload_bytes in (duplicate, deeply_nested, oversized):
        candidate = copy.deepcopy(cell)
        candidate["evidence_receipt"]["payload_base64"] = base64.b64encode(
            payload_bytes
        ).decode("ascii")
        assert not cert._valid_receipt(candidate)

    for nonstandard in (float("inf"), float("-inf")):
        candidate = copy.deepcopy(cell)
        candidate["budget_observations"]["metadata_values"]["untrusted"] = nonstandard
        payload_bytes = json.dumps(
            {
                "schema": "honua.format-budget-observations/v1",
                "budget_observations": candidate["budget_observations"],
            },
            separators=(",", ":"),
        ).encode("utf-8")
        candidate["evidence_receipt"]["payload_base64"] = base64.b64encode(
            payload_bytes
        ).decode("ascii")
        assert not cert._valid_receipt(candidate)


def test_fresh_nightly_required_cell_passes():
    report = _evaluate(_ledger(), "nightly", expected_source_sha=SHA, now=NOW)
    assert report["overall_status"] == "pass"


def test_required_skip_fails_closed():
    report = _evaluate(_ledger(_cell(result="skip", skip_reason="client unavailable")), "nightly", now=NOW)
    assert report["overall_status"] == "fail"


def test_pass_requires_digest_bound_results_for_every_facet_and_trusted_uri():
    missing_facet = _cell()
    missing_facet["facet_results"].pop("metadata")
    untrusted = _cell(evidence_uri="https://example.test/run/1")
    wrong_digest = _cell()
    wrong_digest["facet_results"]["positive"]["evidence_digest"] = "sha256:" + "f" * 64
    failed_facet = _cell()
    failed_facet["facet_results"]["positive"]["result"] = "fail"

    for cell in (missing_facet, untrusted, wrong_digest, failed_facet):
        report = _evaluate(_ledger(cell), "nightly", now=NOW)
        assert report["overall_status"] == "fail"


def test_pass_rejects_digest_valid_but_semantically_empty_receipt():
    cell = _cell(evidence_receipt={})
    report = _evaluate(_ledger(cell), "nightly", now=NOW)
    assert report["overall_status"] == "fail"
    assert any("semantically bound" in finding["why"] for finding in report["findings"])


def test_python_receipt_must_bind_the_ledger_candidate_cut():
    cell = _cell(
        canonical_client="Honua SDK Python",
        client_lane="sdk-python-certification",
        contract_revision="sdk-python-certification@" + "c" * 40,
        producer_source_sha="c" * 40,
    )
    requirements = _requirements(cell)
    requirements["source_revisions"]["sdk-python"] = {"commit": "c" * 40}
    unbound = _evaluate(_ledger(cell), "nightly", requirements=requirements, now=NOW)
    assert unbound["overall_status"] == "fail"
    assert any("semantically bound" in finding["why"] for finding in unbound["findings"])

    cell["evidence_receipt"]["identity"]["candidate_cut_at"] = CUT
    cell["evidence_digest"] = cert._receipt_digest(cell["evidence_receipt"])
    cell["evidence_uri"] = "https://evidence.honua.io/data/sha256/" + cell["evidence_digest"][7:]
    cell["facet_results"] = {
        facet: {"result": "pass", "evidence_digest": cell["evidence_digest"]}
        for facet in cell["scenario_facets"]
    }
    bound = _evaluate(_ledger(cell), "nightly", requirements=requirements, now=NOW)
    assert bound["overall_status"] == "pass"

    cell["evidence_receipt"]["identity"]["candidate_cut_at"] = "2026-08-20T09:00:01Z"
    cell["evidence_digest"] = cert._receipt_digest(cell["evidence_receipt"])
    cell["evidence_uri"] = "https://evidence.honua.io/data/sha256/" + cell["evidence_digest"][7:]
    cell["facet_results"] = {
        facet: {"result": "pass", "evidence_digest": cell["evidence_digest"]}
        for facet in cell["scenario_facets"]
    }
    wrong_cut = _evaluate(_ledger(cell), "nightly", requirements=requirements, now=NOW)
    assert wrong_cut["overall_status"] == "fail"
    assert any("semantically bound" in finding["why"] for finding in wrong_cut["findings"])


def test_every_additional_python_lane_receipt_must_bind_the_ledger_candidate_cut():
    for lane, contract in (
        ("sdk-python", "sdk-python-coverage@" + "c" * 40),
        ("sdk-python-ogc", "ogc-api-features-1.0"),
    ):
        cell = _cell(
            canonical_client="Honua SDK Python",
            client_lane=lane,
            contract_revision=contract,
            producer_source_sha="c" * 40,
        )
        requirements = _requirements(cell)
        requirements["source_revisions"]["sdk-python"] = {"commit": "c" * 40}

        report = _evaluate(_ledger(cell), "nightly", requirements=requirements, now=NOW)

        assert report["overall_status"] == "fail", lane
        assert any("semantically bound" in finding["why"] for finding in report["findings"]), lane


def test_cli_receipt_root_requires_exact_materialized_bytes(tmp_path):
    cell = _cell()
    ledger = _ledger(cell)
    requirements = _requirements(cell)
    missing = cert.evaluate(
        ledger, "nightly", requirements=requirements, now=NOW, receipt_root=tmp_path,
    )
    assert missing["overall_status"] == "fail"
    receipt_path = tmp_path / cell["evidence_digest"][7:]
    receipt_path.write_bytes(json.dumps(
        cell["evidence_receipt"], sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8"))
    present = cert.evaluate(
        ledger, "nightly", requirements=requirements, now=NOW, receipt_root=tmp_path,
    )
    assert present["overall_status"] == "pass"


def test_duplicate_normalized_key_fails():
    cell = _cell()
    report = _evaluate(_ledger(cell, copy.deepcopy(cell)), "nightly", now=NOW)
    assert report["overall_status"] == "fail"
    assert any("duplicate" in finding["why"] for finding in report["findings"])


def test_load_ledger_rejects_duplicate_json_keys(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema":"honua.protocol-certification/v1","schema":"forged"}', encoding="utf-8")

    value, error = cert.load_ledger(path)

    assert value is None
    assert error is not None and "schema" in error


def test_release_evaluation_requires_external_server_source_sha():
    report = _evaluate(_ledger(_cell()), "release", now=NOW)

    assert report["overall_status"] == "fail"
    assert any(finding["check"] == "expected_source_sha" for finding in report["findings"])


def test_non_addressable_requires_reason_and_matching_result():
    report = _evaluate(_ledger(_cell(addressable_by_client=False, result="pass")), "release", now=NOW)
    assert report["overall_status"] == "fail"


def test_supported_operation_needs_an_addressable_client_at_nightly_and_release():
    cell = _cell(addressable_by_client=False, result="not-addressable", addressability_reason="API absent in client")
    for tier in ("nightly", "release"):
        report = _evaluate(_ledger(cell), tier, now=NOW)
        assert report["overall_status"] == "fail"
        assert any(finding["check"] == "addressability" for finding in report["findings"])


def test_candidate_and_cells_require_full_source_shas_at_every_tier():
    for tier in ("pr", "nightly", "release"):
        ledger = _ledger(_cell(source_sha="a" * 7))
        ledger["candidate"]["source_sha"] = "a" * 7
        report = _evaluate(ledger, tier, now=NOW)
        assert report["overall_status"] == "fail"
        assert any("full 40-character" in finding["why"] for finding in report["findings"])


def test_owned_denominator_has_no_unassigned_canonical_clients():
    requirements, error = cert.load_ledger(cert.REQUIREMENTS_PATH)
    assert error is None
    unassigned = [
        row for row in requirements["requirements"]
        if row["canonical_client"] == cert.UNASSIGNED_CANONICAL_CLIENT
    ]
    assert unassigned == []


def test_canonical_client_applicability_decisions_are_complete_and_governed():
    source = json.loads(
        (cert.REQUIREMENTS_PATH.parent / "sources" / "canonical-client-applicability.v1.json")
        .read_text(encoding="utf-8")
    )
    decisions = source["decisions"]
    capability_keys = [decision["capability_key"] for decision in decisions]
    allowed = {
        "official-sdk-required",
        "canonical-external-required",
        "not-client-addressable",
    }

    assert len(decisions) == 41
    assert len(capability_keys) == len(set(capability_keys))
    assert all(decision["classification"] in allowed for decision in decisions)
    assert all(
        decision.get("clients") and decision.get("scenario_facets")
        for decision in decisions
        if decision["classification"] == "canonical-external-required"
    )
    assert all(
        decision.get("reason")
        for decision in decisions
        if decision["classification"] == "not-client-addressable"
    )


def test_unassigned_canonical_client_cannot_be_fabricated_as_a_pass():
    cell = _cell(
        canonical_client=cert.UNASSIGNED_CANONICAL_CLIENT,
        client_lane="canonical-client-unassigned-serve-cog",
        client_version="pending-3387",
    )

    report = _evaluate(_ledger(cell), "nightly", now=NOW)

    assert report["overall_status"] == "fail"
    assert any(
        "canonical client applicability is unassigned" in finding["why"]
        for finding in report["findings"]
    )


def test_unassigned_operation_contract_cannot_be_fabricated_as_a_pass():
    for operation in (
        "UNASSIGNED SDK OPERATION CONTRACT:admin.control-plane",
        "UNASSIGNED PROTOCOL HARNESS CONTRACT:analytics.buffer-aggregate",
    ):
        report = _evaluate(_ledger(_cell(operation=operation)), "nightly", now=NOW)

        assert report["overall_status"] == "fail"
        assert any(
            "client/protocol harness contract is unassigned" in finding["why"]
            for finding in report["findings"]
        )


def test_nightly_older_than_seven_days_fails():
    report = _evaluate(_ledger(_cell(completed_at="2026-08-10T10:00:00Z")), "nightly", now=NOW)
    assert report["overall_status"] == "fail"


def test_licensed_evidence_older_than_72_hours_fails():
    cell = _licensed_cell(
        started_at="2026-08-16T10:00:00Z",
        completed_at="2026-08-16T10:05:00Z",
        checked_at="2026-08-16T10:02:00Z",
    )
    report = _evaluate(_ledger(cell), "nightly", now=NOW)
    assert report["overall_status"] == "fail"
    assert any("licensed evidence is older than 72 hours" in finding["why"] for finding in report["findings"])


def test_arcpy_licensed_target_and_auth_policy_are_governed():
    valid = _licensed_cell(
        policy="esri-arcgis-pro-arcpy-v1",
        deployment_target="windows-licensed",
        auth_policy_revision="anonymous-and-protected-v1",
    )
    assert _evaluate(_ledger(valid), "nightly", now=NOW)["overall_status"] == "pass"

    wrong_target = _licensed_cell(
        policy="esri-arcgis-pro-arcpy-v1",
        deployment_target="windows",
        auth_policy_revision="anonymous-and-protected-v1",
    )
    target_report = _evaluate(_ledger(wrong_target), "nightly", now=NOW)
    assert target_report["overall_status"] == "fail"
    assert any("windows-licensed target" in finding["why"] for finding in target_report["findings"])

    wrong_auth = _licensed_cell(
        policy="esri-arcgis-pro-arcpy-v1",
        deployment_target="windows-licensed",
        auth_policy_revision="api-key-protected-v1",
    )
    auth_report = _evaluate(_ledger(wrong_auth), "nightly", now=NOW)
    assert auth_report["overall_status"] == "fail"
    assert any("anonymous-and-protected auth policy" in finding["why"] for finding in auth_report["findings"])


def test_release_requires_exact_digest_and_post_cut_execution():
    cell = _cell(image_digest="sha256:" + "c" * 64, completed_at="2026-08-20T08:00:00Z")
    report = _evaluate(_ledger(cell), "release", expected_image_digest=DIGEST, now=NOW)
    assert report["overall_status"] == "fail"
    assert len(report["findings"]) >= 2


def test_release_requires_external_image_and_frozen_component_pins():
    ledger = _ledger()
    requirements = _requirements()

    missing = cert.evaluate(
        ledger,
        "release",
        requirements=requirements,
        expected_cut_at=CUT,
        now=NOW,
    )
    assert any(finding["check"] == "expected_image_digest" for finding in missing["findings"])
    assert sum(
        finding["check"].startswith("expected_component_source_shas.")
        for finding in missing["findings"]
    ) == 6

    mismatched = cert.evaluate(
        ledger,
        "release",
        requirements=requirements,
        expected_cut_at=CUT,
        expected_image_digest=DIGEST,
        expected_component_source_shas={
            "sdk-js": "c" * 40,
            "sdk-python": SHA,
            "sdk-dotnet": SHA,
            "geospatial-grpc": SHA,
            "geospatial-mcp": SHA,
        },
        now=NOW,
    )
    assert any(
        finding["check"] == "requirements.source_revisions.sdk-js.commit"
        for finding in mismatched["findings"]
    )


def test_release_server_certification_match_passes():
    report = _evaluate(_ledger(), "release", expected_source_sha=SHA, now=NOW)
    assert report["overall_status"] == "pass"


def test_release_server_certification_mismatch_fails():
    requirements = _requirements()
    requirements["source_revisions"]["server-certification"]["commit"] = "f" * 40
    report = _evaluate(_ledger(), "release", requirements=requirements, now=NOW)
    assert report["overall_status"] == "fail"
    assert any(
        finding["check"] == "requirements.source_revisions.server-certification.commit"
        for finding in report["findings"]
    )


def test_release_server_certification_frozen_pin_must_match_server_candidate():
    divergent_sha = "f" * 40
    cell = _cell(
        client_lane="server-protocol-harness",
        deployment_target="source-test-host",
        producer_source_sha=divergent_sha,
        image_digest=None,
    )
    requirements = _requirements(cell)
    requirements["source_revisions"]["server-certification"]["commit"] = divergent_sha
    expected = {source: SHA for source in cert.FROZEN_RELEASE_SOURCES}
    expected["server-certification"] = divergent_sha

    report = _evaluate(
        _ledger(cell),
        "release",
        requirements=requirements,
        expected_source_sha=SHA,
        expected_component_source_shas=expected,
        now=NOW,
    )

    assert report["overall_status"] == "fail"
    assert any(
        finding["check"] == "expected_component_source_shas.server-certification"
        and "does not match frozen server candidate" in finding["why"]
        for finding in report["findings"]
    )


def test_release_requires_server_certification_sha():
    expected = {source: SHA for source in cert.FROZEN_RELEASE_SOURCES}
    del expected["server-certification"]
    report = _evaluate(
        _ledger(), "release", expected_component_source_shas=expected, now=NOW
    )
    assert report["overall_status"] == "fail"
    assert any(
        finding["check"] == "expected_component_source_shas.server-certification"
        for finding in report["findings"]
    )


def test_non_release_tiers_do_not_bind_server_certification_sha():
    for tier in ("pr", "nightly"):
        cell = _cell(required_tier=tier)
        requirements = _requirements(cell)
        requirements["source_revisions"]["server-certification"]["commit"] = "f" * 40
        report = _evaluate(_ledger(cell), tier, requirements=requirements, now=NOW)
        assert report["overall_status"] == "pass"


def test_preview_failure_does_not_block_release_claim():
    preview = _cell(maturity="preview", result="fail")
    supported = _cell(canonical_client="GDAL", client_lane="gdal", client_version="3.11.4")
    report = _evaluate(_ledger(preview, supported), "release", expected_source_sha=SHA, now=NOW)
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


def test_required_cell_fixture_must_match_owned_revision():
    ledger = _ledger(_cell(fixture_revision="stale-fixture"))
    requirements = _requirements(_cell(fixture_revision="docker/cng/seed.sql@{source_sha}"))

    report = cert.evaluate(ledger, "nightly", requirements=requirements, now=NOW)

    assert report["overall_status"] == "fail"
    assert any("fixture_revision" in finding["why"] for finding in report["findings"])


def test_required_cell_needs_valid_producer_source_sha():
    report = _evaluate(_ledger(_cell(producer_source_sha="not-a-sha")), "nightly", now=NOW)

    assert report["overall_status"] == "fail"
    assert any("producer_source_sha" in finding["why"] for finding in report["findings"])


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


def test_nightly_honors_external_cut_and_rejects_pre_cut_execution():
    ledger = _ledger(_cell(started_at="2026-08-20T08:59:00Z"))
    report = _evaluate(ledger, "nightly", expected_cut_at=CUT, now=NOW)

    assert report["overall_status"] == "fail"
    assert any(
        "nightly evidence started before independently frozen candidate cut" in finding["why"]
        for finding in report["findings"]
    )

    ledger["candidate"]["cut_at"] = "2026-08-20T08:00:00Z"
    mismatch = _evaluate(ledger, "nightly", expected_cut_at=CUT, now=NOW)
    assert any("does not match" in finding["why"] for finding in mismatch["findings"])


def test_out_of_scope_rows_still_require_truthful_image_provenance():
    roadmap = _cell(
        maturity="roadmap",
        required_tier="release",
        result="skip",
        skip_reason="not implemented",
        source_sha=None,
        producer_source_sha=None,
        fixture_revision=None,
        evidence_uri=None,
        evidence_digest=None,
        evidence_receipt=None,
        facet_results=None,
        started_at=None,
        completed_at=None,
        image_digest=None,
    )
    report = _evaluate(_ledger(roadmap), "pr", now=NOW)

    assert report["overall_status"] == "fail"
    assert any("image_digest" in finding["why"] for finding in report["findings"])


def test_roadmap_rows_cannot_report_passing_certification():
    roadmap = _cell(
        capability_key="serve.copc",
        canonical_client="PDAL",
        client_lane="pdal",
        maturity="roadmap",
        result="pass",
    )
    supported = _cell(canonical_client="GDAL", client_lane="gdal", client_version="3.11.4")

    report = _evaluate(_ledger(roadmap, supported), "pr", now=NOW)

    assert report["overall_status"] == "fail"
    assert any(
        "roadmap capability cannot report a passing" in finding["why"]
        for finding in report["findings"]
    )


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


def test_schema_conditionally_requires_nonnull_licensed_entitlement():
    schema = json.loads(
        (Path(__file__).parents[1] / "certification" / "protocol-certification.v1.schema.json")
        .read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema)

    licensed = _cell(
        licensed=True,
        entitlement_policy_revision="honua-pro-feature-subscriptions-v1",
        deployment_target="licensed-release",
        auth_policy_revision="api-key-protected-v1",
    )
    licensed["evidence_receipt"]["identity"]["entitlement_policy_revision"] = None
    licensed["evidence_receipt"]["entitlement"] = None
    assert list(validator.iter_errors(_ledger(licensed)))

    licensed["evidence_receipt"]["identity"]["entitlement_policy_revision"] = (
        "honua-pro-feature-subscriptions-v1"
    )
    licensed["evidence_receipt"]["entitlement"] = {
        "policy_revision": "honua-pro-feature-subscriptions-v1",
        "capability_key": licensed["capability_key"],
        "deployment_target": "licensed-release",
        "verification": "live-server-capability-probe-v1",
        "status": "active",
        "checked_at": "2026-08-20T10:02:00Z",
        "license_fingerprint": "sha256:" + "e" * 64,
    }
    assert not list(validator.iter_errors(_ledger(licensed)))
    assert not list(validator.iter_errors(_ledger(_cell())))


def test_schema_binds_execution_image_digest_to_deployment_target():
    schema = json.loads(
        (Path(__file__).parents[1] / "certification" / "protocol-certification.v1.schema.json")
        .read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema)

    source_host = _cell(deployment_target="source-test-host", image_digest=None)
    assert not list(validator.iter_errors(_ledger(source_host)))
    assert list(validator.iter_errors(_ledger(_cell(deployment_target="source-test-host"))))
    assert list(validator.iter_errors(_ledger(_cell(image_digest=None))))

    deployed_skip = _cell(
        result="skip",
        skip_reason="no producer evidence",
        source_sha=None,
        producer_source_sha=None,
        fixture_revision=None,
        evidence_uri=None,
        evidence_digest=None,
        evidence_receipt=None,
        facet_results=None,
        started_at=None,
        completed_at=None,
    )
    assert not list(validator.iter_errors(_ledger(deployed_skip)))
    deployed_skip["image_digest"] = None
    assert list(validator.iter_errors(_ledger(deployed_skip)))

    source_skip = copy.deepcopy(deployed_skip)
    source_skip["deployment_target"] = "source-test-host"
    assert not list(validator.iter_errors(_ledger(source_skip)))
    source_skip["image_digest"] = DIGEST
    assert list(validator.iter_errors(_ledger(source_skip)))

    missing_candidate = _ledger(source_host)
    missing_candidate["candidate"]["image_digest"] = None
    assert list(validator.iter_errors(missing_candidate))


def test_licensed_receipt_requires_bound_live_entitlement_assertion():
    value = _cell(
        licensed=True,
        entitlement_policy_revision="honua-pro-feature-subscriptions-v1",
        deployment_target="licensed-release",
        auth_policy_revision="api-key-protected-v1",
    )
    assert not cert._valid_receipt(value)

    value["evidence_receipt"]["identity"]["entitlement_policy_revision"] = (
        "honua-pro-feature-subscriptions-v1"
    )
    value["evidence_receipt"]["entitlement"] = {
        "policy_revision": "honua-pro-feature-subscriptions-v1",
        "capability_key": value["capability_key"],
        "deployment_target": "licensed-release",
        "verification": "live-server-capability-probe-v1",
        "status": "active",
        "checked_at": "2026-08-20T10:02:00Z",
        "license_fingerprint": "sha256:" + "a" * 64,
    }
    assert cert._valid_receipt(value)

    value["evidence_receipt"]["entitlement"]["deployment_target"] = "local-docker"
    assert not cert._valid_receipt(value)


def test_receipt_that_binds_requirement_context_must_bind_it_truthfully():
    # contract_revision carries only the PRODUCER revision, so a policy-side
    # denominator change (preview -> supported, or a tier promotion) moves the
    # governed requirement without moving any SHA. A receipt that binds that
    # context must bind it to the cell it is certifying.
    cell = _cell()
    assert cert._valid_receipt(cell)

    for field, wrong in (("maturity", "preview"), ("required_tier", "release")):
        bound = copy.deepcopy(cell)
        bound["evidence_receipt"]["identity"][field] = bound[field]
        assert cert._valid_receipt(bound), field

        stale = copy.deepcopy(bound)
        stale["evidence_receipt"]["identity"][field] = wrong
        assert not cert._valid_receipt(stale), field


def test_receipt_that_binds_a_requirements_revision_must_match_the_owned_one():
    cell = _cell()
    bound = copy.deepcopy(cell)
    bound["evidence_receipt"]["identity"]["requirements_revision"] = "2026-08-21-complete.10"

    assert cert._valid_receipt(bound, None, "2026-08-21-complete.10")
    # Reusing evidence under a different owned denominator must not validate.
    assert not cert._valid_receipt(bound, None, "2026-08-22-complete.11")
    assert not cert._valid_receipt(bound, None, None)
    # An unbound receipt is unaffected (producers have not migrated yet).
    assert cert._valid_receipt(cell, None, "2026-08-22-complete.11")


def test_v2_receipt_with_full_requirement_context_passes():
    cell = _cell()
    receipt = cell["evidence_receipt"]
    receipt["schema"] = "honua.certification-evidence-receipt/v2"
    receipt["identity"].update({
        "maturity": cell["maturity"],
        "required_tier": cell["required_tier"],
        "requirements_revision": "requirements-test-v1",
    })
    assert cert._valid_receipt(cell, None, "requirements-test-v1")


def test_v2_receipt_requires_every_requirement_context_field():
    cell = _cell()
    receipt = cell["evidence_receipt"]
    receipt["schema"] = "honua.certification-evidence-receipt/v2"
    receipt["identity"].update({
        "maturity": cell["maturity"],
        "required_tier": cell["required_tier"],
        "requirements_revision": "requirements-test-v1",
    })
    for field in ("maturity", "required_tier", "requirements_revision"):
        missing = copy.deepcopy(cell)
        del missing["evidence_receipt"]["identity"][field]
        assert not cert._valid_receipt(missing, None, "requirements-test-v1"), field


def test_v1_receipt_passes_when_catalog_minimum_is_v1():
    report = _evaluate(_ledger(), "nightly", now=NOW)
    assert report["overall_status"] == "pass"


def test_v1_receipt_fails_for_passing_required_cell_when_catalog_minimum_is_v2():
    requirements = _requirements()
    requirements["receipt_schema_min"] = "v2"
    report = _evaluate(_ledger(), "nightly", requirements=requirements, now=NOW)
    assert report["overall_status"] == "fail"
    assert any("requires a v2 evidence receipt" in finding["why"] for finding in report["findings"])


def test_catalog_receipt_schema_min_rejects_unknown_values():
    requirements = _requirements()
    requirements["receipt_schema_min"] = "v3"
    report = _evaluate(_ledger(), "nightly", requirements=requirements, now=NOW)
    assert report["overall_status"] == "fail"
    assert any(finding["check"] == "requirements.receipt_schema_min" for finding in report["findings"])
