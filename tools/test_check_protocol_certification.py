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
UNBOUND_RECEIPT_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "unbound-certification-receipt-v1.json"


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
            "schema": "honua.certification-evidence-receipt/v2",
            "identity": identity,
            "result": value["result"],
            "facets": {facet: "pass" for facet in value["scenario_facets"]},
            "payload_base64": "dGVzdA==",
        }
        value["evidence_receipt"]["identity"].update({
            "maturity": value["maturity"],
            "required_tier": value["required_tier"],
            "requirements_revision": "requirements-test-v1",
        })
        if value.get("client_driver") is not None:
            value["evidence_receipt"]["identity"]["client_driver"] = value["client_driver"]
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
            cert._owned_source_name({"deployment_target": "windows-licensed"}): {"commit": SHA},
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


def _licensed_desktop_cell(**overrides):
    return _licensed_cell(**{
        "policy": "licensed-desktop-client-v1",
        "deployment_target": "windows-licensed",
        "auth_policy_revision": "anonymous-and-protected-v1",
        "client_lane": "desktop-pro",
        **overrides,
    })


def test_licensed_desktop_target_binds_its_production_map_producer_revision():
    production = json.loads(
        (cert.REQUIREMENTS_PATH.parent / "sources" / "protocol-certification-production.v1.json").read_text(encoding="utf-8")
    )
    [owner] = [
        producer["source_revision_key"] for producer in production["producers"]
        if "windows-licensed" in producer.get("deployment_targets", [])
    ]
    producer_sha = "e" * 40
    cell = _licensed_desktop_cell(producer_source_sha=producer_sha)
    requirements = _requirements(cell)
    requirements["source_revisions"][owner] = {"commit": producer_sha}
    assert _evaluate(_ledger(cell), "nightly", requirements=requirements, now=NOW)["overall_status"] == "pass"

    cell = _licensed_desktop_cell(producer_source_sha=SHA)
    requirements = _requirements(cell)
    requirements["source_revisions"][owner] = {"commit": producer_sha}
    report = _evaluate(_ledger(cell), "nightly", requirements=requirements, now=NOW)
    assert report["overall_status"] == "fail"
    assert any(f"owned {owner} revision" in finding["why"] for finding in report["findings"])


def test_licensed_desktop_policy_is_governed_and_fresh():
    wrong_target = _licensed_desktop_cell(deployment_target="windows")
    report = _evaluate(_ledger(wrong_target), "nightly", now=NOW)
    assert any("windows-licensed target" in finding["why"] for finding in report["findings"])

    stale = _licensed_desktop_cell(
        started_at="2026-08-16T10:00:00Z",
        completed_at="2026-08-16T10:05:00Z",
        checked_at="2026-08-16T10:02:00Z",
    )
    report = _evaluate(_ledger(stale), "nightly", now=NOW)
    assert any("licensed evidence is older than 72 hours" in finding["why"] for finding in report["findings"])


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


def _with_producer_run(**fields):
    cell = _cell()
    cell["evidence_receipt"]["identity"]["producer_run"] = {
        "repository": "honua-io/honua-server",
        "workflow": "protocol-harness-certification.yml",
        "run_id": 37200001,
        "run_attempt": 1,
        "dispatch_id": "nightly-certification-37199990-1",
        **fields,
    }
    cell["evidence_digest"] = cert._receipt_digest(cell["evidence_receipt"])
    cell["evidence_uri"] = "https://evidence.honua.io/data/sha256/" + cell["evidence_digest"][7:]
    for facet in cell["facet_results"].values():
        facet["evidence_digest"] = cell["evidence_digest"]
    return cell


def test_receipt_may_bind_the_producer_run_that_observed_it():
    # honua-release#386: the nightly binds each cell to the producer run it dispatched, so the
    # receipt (and therefore evidence_digest) carries that run's identity.
    cell = _with_producer_run()
    schema = json.loads(
        (Path(__file__).parents[1] / "certification" / "protocol-certification.v1.schema.json")
        .read_text(encoding="utf-8")
    )
    assert not list(Draft202012Validator(schema).iter_errors(_ledger(cell)))
    assert _evaluate(_ledger(cell), "nightly", now=NOW)["overall_status"] == "pass"

    for malformed in (
        _with_producer_run(run_id="37200001"),
        _with_producer_run(run_attempt=0),
        _with_producer_run(repository="honua-server"),
        _with_producer_run(workflow="../protocol-harness-certification.yml"),
        _with_producer_run(dispatch_id=""),
        _with_producer_run(extra="field"),
    ):
        assert list(Draft202012Validator(schema).iter_errors(_ledger(malformed)))
        assert _evaluate(_ledger(malformed), "nightly", now=NOW)["overall_status"] == "fail"


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


def test_licensed_policy_target_and_auth_are_governed():
    valid = _licensed_cell(
        policy="licensed-desktop-client-v1",
        deployment_target="windows-licensed",
        auth_policy_revision="anonymous-and-protected-v1",
    )
    assert _evaluate(_ledger(valid), "nightly", now=NOW)["overall_status"] == "pass"

    wrong_target = _licensed_cell(
        policy="licensed-desktop-client-v1",
        deployment_target="windows",
        auth_policy_revision="anonymous-and-protected-v1",
    )
    target_report = _evaluate(_ledger(wrong_target), "nightly", now=NOW)
    assert target_report["overall_status"] == "fail"
    assert any("windows-licensed target" in finding["why"] for finding in target_report["findings"])

    wrong_auth = _licensed_cell(
        policy="licensed-desktop-client-v1",
        deployment_target="windows-licensed",
        auth_policy_revision="api-key-protected-v1",
    )
    auth_report = _evaluate(_ledger(wrong_auth), "nightly", now=NOW)
    assert auth_report["overall_status"] == "fail"
    assert any("anonymous-and-protected auth policy" in finding["why"] for finding in auth_report["findings"])

    unknown = _licensed_cell(policy="unknown-policy-v1")
    report = _evaluate(_ledger(unknown), "nightly", now=NOW)
    assert report["overall_status"] == "fail"
    assert any("governed entitlement policy" in finding["why"] for finding in report["findings"])


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
    assert cert._valid_receipt(value, None, "requirements-test-v1")

    value["evidence_receipt"]["entitlement"]["deployment_target"] = "local-docker"
    assert not cert._valid_receipt(value, None, "requirements-test-v1")


def test_receipt_requirement_context_must_bind_truthfully():
    # contract_revision carries only the PRODUCER revision, so a policy-side
    # denominator change (preview -> supported, or a tier promotion) moves the
    # governed requirement without moving any SHA. A receipt that binds that
    # context must bind it to the cell it is certifying.
    cell = _cell()
    assert cert._valid_receipt(cell, None, "requirements-test-v1")

    for field, wrong in (("maturity", "preview"), ("required_tier", "release")):
        bound = copy.deepcopy(cell)
        bound["evidence_receipt"]["identity"][field] = bound[field]
        assert cert._valid_receipt(bound, None, "requirements-test-v1"), field

        stale = copy.deepcopy(bound)
        stale["evidence_receipt"]["identity"][field] = wrong
        assert not cert._valid_receipt(stale, None, "requirements-test-v1"), field


def test_receipt_that_binds_a_requirements_revision_must_match_the_owned_one():
    cell = _cell()
    bound = copy.deepcopy(cell)
    bound["evidence_receipt"]["identity"]["requirements_revision"] = "2026-08-21-complete.10"

    assert cert._valid_receipt(bound, None, "2026-08-21-complete.10")
    # Reusing evidence under a different owned denominator must not validate.
    assert not cert._valid_receipt(bound, None, "2026-08-22-complete.11")
    assert not cert._valid_receipt(bound, None, None)
    # The consumer never supplies or substitutes producer-owned context.
    context_less = copy.deepcopy(cell)
    del context_less["evidence_receipt"]["identity"]["requirements_revision"]
    assert not cert._valid_receipt(context_less, None, "requirements-test-v1")


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


def test_v1_receipt_fails_even_when_catalog_minimum_is_v1():
    cell = _cell()
    cell["evidence_receipt"]["schema"] = "honua.certification-evidence-receipt/v1"
    for field in ("maturity", "required_tier", "requirements_revision"):
        del cell["evidence_receipt"]["identity"][field]
    cell["evidence_digest"] = cert._receipt_digest(cell["evidence_receipt"])
    cell["evidence_uri"] = "https://evidence.honua.io/data/sha256/" + cell["evidence_digest"][7:]
    cell["facet_results"] = {
        facet: {"result": "pass", "evidence_digest": cell["evidence_digest"]}
        for facet in cell["scenario_facets"]
    }

    report = _evaluate(_ledger(cell), "nightly", now=NOW)

    assert report["overall_status"] == "fail"
    assert any("semantically bound" in finding["why"] for finding in report["findings"])


def test_v1_receipt_fails_for_passing_required_cell_when_catalog_minimum_is_v2():
    cell = _cell()
    cell["evidence_receipt"]["schema"] = "honua.certification-evidence-receipt/v1"
    for field in ("maturity", "required_tier", "requirements_revision"):
        del cell["evidence_receipt"]["identity"][field]
    cell["evidence_digest"] = cert._receipt_digest(cell["evidence_receipt"])
    cell["evidence_uri"] = "https://evidence.honua.io/data/sha256/" + cell["evidence_digest"][7:]
    cell["facet_results"] = {
        facet: {"result": "pass", "evidence_digest": cell["evidence_digest"]}
        for facet in cell["scenario_facets"]
    }
    requirements = _requirements()
    requirements["receipt_schema_min"] = "v2"
    report = _evaluate(_ledger(cell), "nightly", requirements=requirements, now=NOW)
    assert report["overall_status"] == "fail"
    assert any("requires a v2 evidence receipt" in finding["why"] for finding in report["findings"])


def test_release_rejects_unbound_receipt_fixture_when_v2_is_required():
    cell = _cell(evidence_receipt=json.loads(UNBOUND_RECEIPT_FIXTURE.read_text(encoding="utf-8")))
    cell["evidence_digest"] = cert._receipt_digest(cell["evidence_receipt"])
    cell["evidence_uri"] = "https://evidence.honua.io/data/sha256/" + cell["evidence_digest"][7:]
    cell["facet_results"] = {
        facet: {"result": "pass", "evidence_digest": cell["evidence_digest"]}
        for facet in cell["scenario_facets"]
    }
    requirements = _requirements(cell)
    requirements["receipt_schema_min"] = "v2"

    report = _evaluate(_ledger(cell), "release", requirements=requirements, now=NOW)

    assert report["overall_status"] == "fail"
    assert any("requires a v2 evidence receipt" in finding["why"] for finding in report["findings"])


def test_catalog_receipt_schema_min_rejects_unknown_values():
    requirements = _requirements()
    requirements["receipt_schema_min"] = "v3"
    report = _evaluate(_ledger(), "nightly", requirements=requirements, now=NOW)
    assert report["overall_status"] == "fail"
    assert any(finding["check"] == "requirements.receipt_schema_min" for finding in report["findings"])


# ------------------------------------------------------------------ R38 desktop release lines and buckets


def _rebind(cell):
    cell["evidence_digest"] = cert._receipt_digest(cell["evidence_receipt"])
    cell["evidence_uri"] = "https://evidence.honua.io/data/sha256/" + cell["evidence_digest"][7:]
    for facet in cell["facet_results"].values():
        facet["evidence_digest"] = cell["evidence_digest"]
    return cell


def _release_line_cell(observed="3.44.14-Solothurn", *, receipt_observed=None, **overrides):
    """A desktop cell on the 3.44 line whose receipt records the exact patch it observed."""
    cell = _cell(
        capability_key="serve.wfs", surface="wfs", operation="serve.wfs", canonical_client="QGIS",
        client_lane="desktop-qgis", client_version="3.44.x", **overrides,
    )
    if observed is not None:
        cell["client_version_observed"] = observed
        cell["evidence_receipt"]["identity"]["client_version_observed"] = (
            observed if receipt_observed is None else receipt_observed
        )
    return _rebind(cell)


def test_release_line_accepts_any_patch_of_its_line_only():
    for observed in ("3.44.0", "3.44.14", "3.44.14-Solothurn", "3.44.15-rc.1"):
        assert cert.client_version_satisfies("3.44.x", observed), observed
    for observed in ("3.40.5", "3.45.0", "3.4.14", "3.440.1", "3.44", "3.44.x", "03.44.1", "", None):
        assert not cert.client_version_satisfies("3.44.x", observed), observed
    # an exact requirement still joins exactly
    assert cert.client_version_satisfies("1.4.3", "1.4.3")
    assert not cert.client_version_satisfies("1.4.3", "1.4.4")
    assert cert.is_version_line("3.7.x") and not cert.is_version_line("3.7.1") and not cert.is_version_line("3.x")


def test_release_line_pass_records_the_exact_observed_version():
    cell = _release_line_cell()
    schema = json.loads(
        (Path(__file__).parents[1] / "certification" / "protocol-certification.v1.schema.json")
        .read_text(encoding="utf-8")
    )
    assert not list(Draft202012Validator(schema).iter_errors(_ledger(cell)))
    assert _evaluate(_ledger(cell), "nightly", now=NOW)["overall_status"] == "pass"
    # the requirement and the ledger keep the line; only the receipt names the patch
    assert _requirements(cell)["requirements"][0]["client_version"] == "3.44.x"


def test_release_line_rejects_missing_off_line_or_unbound_observed_version():
    cases = {
        "missing": _release_line_cell(observed=None),
        "off-line": _release_line_cell(observed="3.40.5"),
        "unbound": _release_line_cell(receipt_observed="3.44.13-Solothurn"),
    }
    for name, cell in cases.items():
        report = _evaluate(_ledger(cell), "nightly", now=NOW)
        assert report["overall_status"] == "fail", name
    missing = _evaluate(_ledger(cases["missing"]), "nightly", now=NOW)
    assert any("client_version_observed" in finding["why"] for finding in missing["findings"])
    # an exact requirement takes no observed version: the join already names it
    exact = _rebind(_cell())
    exact["client_version_observed"] = "1.4.3"
    exact["evidence_receipt"]["identity"]["client_version_observed"] = "1.4.3"
    assert _evaluate(_ledger(_rebind(exact)), "nightly", now=NOW)["overall_status"] == "fail"


def test_ledger_release_bucket_must_match_the_owned_requirement():
    cell = _release_line_cell(release_bucket="prove-against-candidate", client_driver="qgis-ui")
    requirements = _requirements(cell)
    requirements["requirements"][0]["release_bucket"] = "prove-against-candidate"
    requirements["requirements"][0]["client_driver"] = "qgis-ui"
    assert _evaluate(_ledger(cell), "nightly", now=NOW, requirements=requirements)["overall_status"] == "pass"
    requirements["requirements"][0]["release_bucket"] = "must-fix-before-cut"
    report = _evaluate(_ledger(cell), "nightly", now=NOW, requirements=requirements)
    assert any("release_bucket" in finding["why"] for finding in report["findings"])


def test_ledger_cell_must_carry_the_owned_requirement_release_bucket():
    cell = _release_line_cell()
    cell.pop("release_bucket", None)
    requirements = _requirements(cell)
    requirements["requirements"][0]["release_bucket"] = "prove-against-candidate"
    report = _evaluate(_ledger(cell), "nightly", now=NOW, requirements=requirements)
    assert report["overall_status"] == "fail"
    assert any("release_bucket is required" in finding["why"] for finding in report["findings"])

def _desktop_source():
    return json.loads(
        (cert.REQUIREMENTS_PATH.parent / "sources" / "desktop-client-certification.v1.json").read_text(encoding="utf-8")
    )


def test_ledger_cell_must_carry_and_bind_the_client_driver():
    """R40: the ledger cell carries the requirement's driver, and the receipt binds that driver."""
    cell = _release_line_cell(client_driver="pyqgis", release_bucket="must-fix-before-cut")
    schema = json.loads(
        (Path(__file__).parents[1] / "certification" / "protocol-certification.v1.schema.json")
        .read_text(encoding="utf-8")
    )
    assert not list(Draft202012Validator(schema).iter_errors(_ledger(cell)))
    requirements = _requirements(cell)
    requirements["requirements"][0]["client_driver"] = "pyqgis"
    requirements["requirements"][0]["release_bucket"] = "must-fix-before-cut"
    assert _evaluate(_ledger(cell), "nightly", now=NOW, requirements=requirements)["overall_status"] == "pass"

    missing = copy.deepcopy(cell)
    missing.pop("client_driver")
    missing["evidence_receipt"]["identity"].pop("client_driver")
    report = _evaluate(_ledger(_rebind(missing)), "nightly", now=NOW, requirements=requirements)
    assert any("client_driver is required" in finding["why"] for finding in report["findings"])

    unbound = copy.deepcopy(cell)
    unbound["evidence_receipt"]["identity"]["client_driver"] = "pro-ui"
    report = _evaluate(_ledger(_rebind(unbound)), "nightly", now=NOW, requirements=requirements)
    assert any("semantically bound" in finding["why"] for finding in report["findings"])

    requirements["requirements"][0]["client_driver"] = next(iter(cert.CLIENT_DRIVERS - {"pyqgis", "pro-ui", "qgis-ui"}))
    report = _evaluate(_ledger(cell), "nightly", now=NOW, requirements=requirements)
    assert any("client_driver does not match" in finding["why"] for finding in report["findings"])


def test_owned_denominator_certifies_desktop_clients_on_release_lines():
    requirements, error = cert.load_ledger(cert.REQUIREMENTS_PATH)
    assert error is None
    source = _desktop_source()
    qgis, desktop = source["clients"]["qgis"], source["clients"]["pro"]
    assert (qgis["version"], desktop["version"]) == ("3.44.x", "3.7.x")
    assert requirements["trademarkNotice"] == source["trademarkNotice"]
    rows = requirements["requirements"]
    qgis_rows = [row for row in rows if row["canonical_client"] == "QGIS" or row["canonical_client"].startswith("QGIS/")]
    assert qgis_rows and {row["client_version"] for row in qgis_rows} == {"3.44.x"}
    desktop_rows = [
        row for row in rows
        if row["canonical_client"] == desktop["name"] or row["canonical_client"].startswith(desktop["name"] + "/")
    ]
    assert desktop_rows and {row["client_version"] for row in desktop_rows} == {"3.7.x"}
    desktop_clients = {row["canonical_client"] for row in qgis_rows + desktop_rows}
    driven = [row for row in rows if "client_driver" in row or "release_bucket" in row]
    assert driven and {row["canonical_client"] for row in driven} <= desktop_clients
    assert all("client_driver" in row and "release_bucket" in row for row in driven)


def test_owned_denominator_splits_desktop_rows_by_driver():
    requirements, _ = cert.load_ledger(cert.REQUIREMENTS_PATH)
    source = _desktop_source()
    must_fix, prove = source["buckets"]["must_fix"], source["buckets"]["prove"]
    rows = requirements["requirements"]
    summary = requirements["desktop_driver_summary"]
    assert summary["ruling"] == "R40"
    scripting = next(iter(set(summary["drivers"]) - {"pyqgis", "pro-ui", "qgis-ui"}))
    counts = {
        driver: {
            must_fix: sum(row.get("client_driver") == driver and row.get("release_bucket") == must_fix for row in rows),
            prove: sum(row.get("client_driver") == driver and row.get("release_bucket") == prove for row in rows),
        }
        for driver in (scripting, "pyqgis", "pro-ui", "qgis-ui")
    }
    assert summary["drivers"] == counts
    for driver in (scripting, "pyqgis"):
        assert counts[driver][must_fix] > 0 and counts[driver][prove] == 0
        driver_rows = [row for row in rows if row.get("client_driver") == driver]
        assert any(
            row["surface"] in {"feature-server", "map-server", "image-server", "featureserver", "mapserver"}
            or "geoservices" in row["capability_key"]
            for row in driver_rows
        )
        assert any(row["surface"] == "ogc" or row["capability_key"].startswith("serve.w") or row["capability_key"].startswith("serve.ogc") for row in driver_rows)
    pro_core = {
        (row["surface"], row["operation"])
        for row in rows
        if row.get("client_driver") == "pro-ui" and row["release_bucket"] == must_fix
    }
    assert pro_core == {
        ("feature-server", "connect"), ("feature-server", "add-layer"), ("feature-server", "render"),
        ("feature-server", "identify"), ("feature-server", "query"), ("feature-server", "edit"),
        ("map-server", "connect"), ("map-server", "add-layer"), ("map-server", "render"),
        ("map-server", "identify"), ("map-server", "query"),
        ("image-server", "render"),
        ("auth", "anonymous"), ("auth", "token-api-key"), ("auth", "portal-sign-in"),
    }
    qgis_core = {
        (row["surface"], row["operation"])
        for row in rows
        if row.get("client_driver") == "qgis-ui" and row["release_bucket"] == must_fix
    }
    assert pro_core < qgis_core
    for surface, functions in {
        "wms": {"connect", "add-layer", "render", "identify", "query"},
        "wfs": {"connect", "add-layer", "render", "identify", "query", "wfs-t-edit"},
        "wcs": {"connect", "add-layer", "render", "identify", "query"},
    }.items():
        assert {(surface, function) for function in functions} <= qgis_core
        assert not {(surface, function) for function in functions} & pro_core
    assert counts["pro-ui"][prove] > 0 and counts["qgis-ui"][prove] > 0
    assert all(
        row["release_bucket"] == prove
        for row in rows
        if row.get("client_driver") in ("pro-ui", "qgis-ui")
        and (row["surface"], row["operation"]) not in (pro_core if row["client_driver"] == "pro-ui" else qgis_core)
    )


def test_owned_denominator_runs_licensed_desktop_drivers_on_their_governed_target():
    requirements, _ = cert.load_ledger(cert.REQUIREMENTS_PATH)
    source = _desktop_source()
    desktop = source["clients"]["pro"]
    rows = requirements["requirements"]
    scripting_driver = next(iter(set(requirements["desktop_driver_summary"]["drivers"]) - {"pyqgis", "pro-ui", "qgis-ui"}))
    scripting = [row for row in rows if row.get("client_driver") == scripting_driver]
    pro_ui = [row for row in rows if row.get("client_driver") == "pro-ui"]
    assert scripting and all(
        row["licensed"] and row["deployment_target"] == "windows-licensed"
        and row["entitlement_policy_revision"] != desktop["entitlement_policy_revision"]
        and row["canonical_client"] == desktop["name"]
        and scripting_driver in row["client_lane"]
        for row in scripting
    )
    assert len({row["entitlement_policy_revision"] for row in scripting}) == 1
    assert pro_ui and all(
        row["licensed"] and row["deployment_target"] == desktop["deployment_target"]
        and row["entitlement_policy_revision"] == desktop["entitlement_policy_revision"]
        and row["canonical_client"] == desktop["name"] and row["client_lane"] == "desktop-pro-ui"
        for row in pro_ui
    )
    qgis_drivers = [row for row in rows if row.get("client_driver") in ("pyqgis", "qgis-ui")]
    assert qgis_drivers and all(
        not row["licensed"] and row["deployment_target"] == "local-docker" and row["client_version"] == "3.44.x"
        for row in qgis_drivers
    )


def test_ledger_schema_governs_the_licensed_desktop_entitlement():
    # Every pro-ui row binds licensed-desktop-client-v1, so the ledger schema must admit it, on the
    # same governed windows-licensed target and auth policy as the scripting entitlement.
    schema = json.loads(
        (Path(__file__).parents[1] / "certification" / "protocol-certification.v1.schema.json")
        .read_text(encoding="utf-8")
    )
    cell = _licensed_desktop_cell()
    assert not list(Draft202012Validator(schema).iter_errors(_ledger(cell)))
    for overrides in ({"deployment_target": "windows"}, {"auth_policy_revision": "anonymous-public-v1"}):
        assert list(Draft202012Validator(schema).iter_errors(_ledger({**cell, **overrides})))


def test_pro_ui_lane_has_its_own_unproduced_disposition():
    # The licensed producer selects windows-licensed by target; it must not claim the UI driver,
    # whose cells no pinned workflow emits.
    requirements, _ = cert.load_ledger(cert.REQUIREMENTS_PATH)
    production = requirements["production"]
    [licensed] = [entry for entry in production["producers"] if "windows-licensed" in entry.get("deployment_targets", [])]
    assert "desktop-pro-ui" in licensed["except_client_lanes"]
    [pro_ui] = [entry for entry in production["unproduced"] if "desktop-pro-ui" in entry.get("client_lanes", [])]
    assert pro_ui["cells"] == sum(row.get("client_driver") == "pro-ui" for row in requirements["requirements"])
    assert pro_ui["cells"] > 0


def test_licensed_scripting_driver_keeps_the_metadata_facet():
    # The coarse desktop interop rows were the licensed client's only metadata coverage; their
    # FeatureServer and MapServer replacement rows must still declare the metadata facet.
    requirements, _ = cert.load_ledger(cert.REQUIREMENTS_PATH)
    scripting_driver = next(iter(set(requirements["desktop_driver_summary"]["drivers"]) - {"pyqgis", "pro-ui", "qgis-ui"}))
    covered = {
        row["surface"] for row in requirements["requirements"]
        if row.get("client_driver") == scripting_driver and "metadata" in row["scenario_facets"]
        and row["release_bucket"] == "must-fix-before-cut"
    }
    assert {"feature-server", "map-server"} <= covered, covered


def test_pyqgis_is_the_full_surface_function_grid():
    """R40: pyqgis is every server-declared surface crossed with every function, and a function
    PyQGIS has no client for is not-applicable with that reason declared on the row."""
    requirements, _ = cert.load_ledger(cert.REQUIREMENTS_PATH)
    grid = json.loads(
        (Path(__file__).parents[1] / "certification" / "sources" / "pyqgis-function-grid.v1.json")
        .read_text(encoding="utf-8")
    )
    operations = [item["operation"] for item in grid["functions"]]
    expected = {
        (surface["surface"], operation): surface["not_applicable"].get(operation)
        for surface in grid["surfaces"]
        for operation in operations
    }
    for dropped in grid["dropped_functions"]:
        expected[(dropped["surface"], dropped["operation"])] = dropped["addressability_reason"]
    rows = [
        row for row in requirements["requirements"]
        if row.get("client_driver") == "pyqgis" and row["client_lane"] == "desktop-pyqgis"
        and row["surface"] != "ogc"
    ]
    present = {(row["surface"], row["operation"]): row for row in rows}
    assert set(present) == set(expected)
    assert all(row["release_bucket"] == "must-fix-before-cut" for row in rows)
    for key, reason in expected.items():
        row = present[key]
        if reason is None:
            assert row["addressable_by_client"] and row["addressability_reason"] is None
        else:
            assert not row["addressable_by_client"] and row["addressability_reason"] == reason
            assert reason.startswith("not-applicable: ")
    # The coarse one-row-per-surface fill is gone; matrix cases the roster does not cover remain.
    assert all(row["surface"] == "ogc" for row in requirements["requirements"]
               if row.get("client_driver") == "pyqgis" and row["client_lane"] == "desktop-pyqgis"
               and (row["surface"], row["operation"]) not in expected)
    families = {item["operation"]: item["family"] for item in grid["functions"]}
    assert set(families.values()) == {
        "connect/discover", "add", "render", "identify", "query/filter", "edit",
        "raster read", "save/reopen", "auth",
    }
