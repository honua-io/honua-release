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
    if str(value.get("client_lane", "")).startswith("grpc-"):
        value.setdefault("client_package_digest", "sha512-" + base64.b64encode(b"x" * 64).decode()
                         if value["client_lane"] == "grpc-typescript" else "sha256:" + "f" * 64)
    if value.get("client_lane") == "sdk-python-certification":
        value.setdefault("client_package", {"filename": f"honua_sdk-{value['client_version']}-py3-none-any.whl",
                                            "sha256": "e" * 64, "source": "pypi"})
    if "evidence_receipt" not in overrides:
        identity = {
            field: value[field] for field in cert.RECEIPT_ID_FIELDS
        }
        if "client_package" in value:
            identity["client_package"] = copy.deepcopy(value["client_package"])
        if "client_package_digest" in value:
            identity["client_package_digest"] = value["client_package_digest"]
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


def test_server_harness_pass_binds_test_ids_and_certification_source_revision():
    harness_sha = SHA
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
        contract_revision=f"server-protocol-harness@fixture-v1+{harness_sha}",
    )
    execution = {
        "client_id": cell["canonical_client"], "performed_by": cell["canonical_client"],
        "runner_lane": cell["client_lane"], "protocol_version": "fixture-v1",
        "protocol_profile": "source-test-host-http-adapter", "request_url": None,
        "exercised_capabilities": cell["scenario_facets"],
    }
    cell.update(execution)
    cell["evidence_receipt"]["identity"]["candidate_cut_at"] = CUT
    cell["evidence_receipt"]["identity"]["producer_run"] = {
        "repository": "honua-io/honua-server", "workflow": "protocol-harness-certification.yml",
        "run_id": 123, "run_attempt": 1, "dispatch_id": "nightly-certification-123-1",
    }
    cell["evidence_receipt"]["payload_base64"] = base64.b64encode(json.dumps({
        "contract_revision": cell["contract_revision"], "execution": execution,
        "test_outcomes": {test_id: "pass" for test_id in test_ids},
    }, sort_keys=True, separators=(",", ":")).encode()).decode()
    cell["evidence_digest"] = cert._receipt_digest(cell["evidence_receipt"])
    cell["evidence_uri"] = "https://evidence.honua.io/data/sha256/" + cell["evidence_digest"][7:]
    for facet in cell["facet_results"].values():
        facet["evidence_digest"] = cell["evidence_digest"]
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
    assert any("execution metadata" in finding["why"] for finding in wrong_test_report["findings"])

    falsely_bound = copy.deepcopy(cell)
    falsely_bound["image_digest"] = DIGEST
    falsely_bound["evidence_receipt"]["identity"]["image_digest"] = DIGEST
    falsely_bound["evidence_digest"] = cert._receipt_digest(falsely_bound["evidence_receipt"])
    falsely_bound["evidence_uri"] = "https://evidence.honua.io/data/sha256/" + falsely_bound["evidence_digest"][7:]
    for facet in falsely_bound["facet_results"].values():
        facet["evidence_digest"] = falsely_bound["evidence_digest"]
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

    assert len(decisions) == 42
    assert {"capability_key": "analytics.h3", "classification": "official-sdk-required"} in decisions
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


def _pinned_matrix():
    matrix = json.loads(
        (cert.REQUIREMENTS_PATH.parent / "sources" / "server" / "capability-matrix.v1.json").read_text(encoding="utf-8")
    )
    keys = {row["key"] for row in matrix["capabilities"]}
    implemented = {
        row["key"] for row in matrix["capabilities"] if row.get("maturity", {}).get("implemented")
    }
    return keys, implemented


def _ui_must_fix(source, field):
    """Must-fix UI cells for capabilities the pinned matrix has implemented."""
    matrix_keys, implemented = _pinned_matrix()
    ui = source["ui"]
    core = set()
    for surface in ui["surfaces"]:
        key = surface["capability_key"]
        if key in matrix_keys and key not in implemented:
            continue
        core.update((surface["surface"], function) for function in surface[field])
    if ui["auth_capability"] not in matrix_keys or ui["auth_capability"] in implemented:
        core.update((ui["auth_surface"], mode) for mode in ui["auth_modes"])
    return core


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
    unbound["evidence_receipt"]["identity"]["client_driver"] = "qgis-ui"
    report = _evaluate(_ledger(_rebind(unbound)), "nightly", now=NOW, requirements=requirements)
    assert any("semantically bound" in finding["why"] for finding in report["findings"])

    requirements["requirements"][0]["client_driver"] = next(iter(cert.CLIENT_DRIVERS - {"pyqgis"}))
    report = _evaluate(_ledger(cell), "nightly", now=NOW, requirements=requirements)
    assert any("client_driver does not match" in finding["why"] for finding in report["findings"])


def test_pyqgis_is_the_full_surface_function_grid():
    """R40: pyqgis is every implemented server-declared surface crossed with every function, and a
    function PyQGIS has no client for is not-applicable with that reason declared on the row.
    A capability the pinned matrix has not implemented contributes no row."""
    requirements, _ = cert.load_ledger(cert.REQUIREMENTS_PATH)
    grid = json.loads(
        (Path(__file__).parents[1] / "certification" / "sources" / "pyqgis-function-grid.v1.json")
        .read_text(encoding="utf-8")
    )
    matrix_keys, implemented = _pinned_matrix()
    operations = [item["operation"] for item in grid["functions"]]

    def withheld(capability_key: str) -> bool:
        return capability_key in matrix_keys and capability_key not in implemented

    expected = {
        (surface["surface"], operation): surface["not_applicable"].get(operation)
        for surface in grid["surfaces"]
        if not withheld(surface["capability_key"])
        for operation in operations
    }
    for dropped in grid["dropped_functions"]:
        if withheld(dropped["surface"]):
            continue
        expected[(dropped["surface"], dropped["operation"])] = dropped["addressability_reason"]
    withheld_surfaces = {
        surface["surface"] for surface in grid["surfaces"] if withheld(surface["capability_key"])
    }
    assert "sensorthings" in withheld_surfaces
    rows = [
        row for row in requirements["requirements"]
        if row.get("client_driver") == "pyqgis" and row["client_lane"] == "desktop-pyqgis"
        and row["surface"] != "ogc"
    ]
    assert not any(row["surface"] in withheld_surfaces for row in rows)
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


def test_unproduced_wps_cite_required_skip_is_not_canonical_gp_evidence():
    cell = _cell(
        capability_key="serve.wps", surface="wps-2-0", operation="serve.wps",
        canonical_client="OGC CITE", client_lane="cite-wps-2-0", client_version="WPS 2.0",
        result="skip", skip_reason="Normalized candidate-bound WPS CITE producer absent",
        evidence_receipt=None, evidence_uri=None, evidence_digest=None, facet_results=None,
    )
    report = _evaluate(_ledger(cell), "nightly", now=NOW)
    assert report["overall_status"] == "fail"


def test_grpc_package_digest_is_bound_to_receipt_and_requirement():
    for lane in ("grpc-dotnet", "grpc-python", "grpc-typescript"):
        cell = _cell(client_lane=lane)
        assert cert._valid_receipt(cell, requirements_revision="requirements-test-v1")
        for mutation in ("missing-observation", "wrong-observation", "missing-receipt", "wrong-receipt"):
            changed = copy.deepcopy(cell)
            if mutation == "missing-observation":
                changed.pop("client_package_digest")
            elif mutation == "wrong-observation":
                changed["client_package_digest"] = "sha256:" + "0" * 64
            elif mutation == "missing-receipt":
                changed["evidence_receipt"]["identity"].pop("client_package_digest")
            else:
                changed["evidence_receipt"]["identity"]["client_package_digest"] = "sha256:" + "0" * 64
            assert not cert._valid_receipt(changed, requirements_revision="requirements-test-v1"), mutation


def test_grpc_repacked_identity_cannot_replace_the_governed_digest():
    cell = _cell(client_lane="grpc-dotnet")
    owned = _requirements(cell)
    ledger = _ledger(cell)
    changed = ledger["cells"][0]
    changed["client_package_digest"] = "sha256:" + "0" * 64
    changed["evidence_receipt"]["identity"]["client_package_digest"] = changed["client_package_digest"]
    changed["evidence_digest"] = cert._receipt_digest(changed["evidence_receipt"])
    changed["evidence_uri"] = "https://evidence.honua.io/data/sha256/" + changed["evidence_digest"][7:]
    for value in changed["facet_results"].values():
        value["evidence_digest"] = changed["evidence_digest"]
    result = _evaluate(ledger, "nightly", requirements=owned)
    assert result["overall_status"] == "fail"
    assert any(item["check"] == "requirements_denominator" for item in result["findings"])


def test_grpc_digest_algorithm_and_canonical_encoding_are_not_interchangeable():
    sri = "sha512-" + base64.b64encode(b"x" * 64).decode()
    assert cert._valid_client_package_digest("grpc-typescript", sri)
    assert not cert._valid_client_package_digest("grpc-dotnet", sri)
    assert not cert._valid_client_package_digest("grpc-typescript", "sha256:" + "a" * 64)
    assert not cert._valid_client_package_digest("grpc-typescript", sri + "\n")


def test_non_grpc_package_digest_cannot_expand_closed_identity_or_requirements():
    schema = json.loads((Path(__file__).parents[1] / 'certification/protocol-certification.v1.schema.json').read_text())
    validator = Draft202012Validator(schema)
    original = _cell()
    assert cert._valid_receipt(original, requirements_revision='requirements-test-v1')
    assert not list(validator.iter_errors(_ledger(original)))
    for value in (None, 'sha256:' + 'a' * 64):
        for target in ('cell', 'identity', 'both'):
            changed = copy.deepcopy(original)
            if target in ('cell', 'both'):
                changed['client_package_digest'] = value
            if target in ('identity', 'both'):
                changed['evidence_receipt']['identity']['client_package_digest'] = value
            assert not cert._valid_receipt(changed, requirements_revision='requirements-test-v1'), target
            assert list(validator.iter_errors(_ledger(changed))), target
            result = _evaluate(_ledger(changed), 'nightly', now=NOW, requirements=_requirements(original))
            assert result['overall_status'] == 'fail', target
        owned = _requirements(original)
        owned['requirements'][0]['client_package_digest'] = value
        result = _evaluate(_ledger(original), 'nightly', now=NOW, requirements=owned)
        assert any(item['check'] == 'requirements.client_package_digest' for item in result['findings'])


def test_published_schemas_enforce_each_grpc_digest_algorithm_and_encoding():
    root = Path(__file__).parents[1] / 'certification'
    ledger_validator = Draft202012Validator(json.loads((root / 'protocol-certification.v1.schema.json').read_text()))
    requirement_validator = Draft202012Validator(json.loads((root / 'protocol-certification-requirements.v1.schema.json').read_text()))
    governed = json.loads((root / 'protocol-certification-requirements.v1.json').read_text())
    sha256 = 'sha256:' + 'a' * 64
    sri = 'sha512-' + base64.b64encode(b'x' * 64).decode()
    for lane in ('grpc-dotnet', 'grpc-python', 'grpc-typescript'):
        good = sri if lane == 'grpc-typescript' else sha256
        wrong_algorithm = sha256 if lane == 'grpc-typescript' else sri
        cell = _cell(client_lane=lane, client_package_digest=good)
        assert not list(ledger_validator.iter_errors(_ledger(cell))), lane
        requirements = copy.deepcopy(governed)
        requirements['requirements'] = _requirements(cell)['requirements']
        assert not list(requirement_validator.iter_errors(requirements)), lane
        # Noncanonical pad bits decode to the same bytes but must not identify the package.
        bad_values = (wrong_algorithm, good + '\n', 'sha512-' + 'e' * 86 + '==')
        for value in bad_values:
            assert not cert._valid_client_package_digest(lane, value), (lane, value)
            for target in ('cell', 'identity'):
                changed = copy.deepcopy(cell)
                destination = changed if target == 'cell' else changed['evidence_receipt']['identity']
                destination['client_package_digest'] = value
                assert list(ledger_validator.iter_errors(_ledger(changed))), (lane, target, value)
            changed_requirements = copy.deepcopy(requirements)
            changed_requirements['requirements'][0]['client_package_digest'] = value
            assert list(requirement_validator.iter_errors(changed_requirements)), (lane, value)
    ordinary = _cell(client_package_digest=sha256)
    requirements = copy.deepcopy(governed)
    requirements['requirements'] = _requirements(ordinary)['requirements']
    assert list(requirement_validator.iter_errors(requirements))
    unknown = _cell(client_lane='grpc-unknown')
    assert not cert._valid_receipt(unknown, requirements_revision='requirements-test-v1')
    assert list(ledger_validator.iter_errors(_ledger(unknown)))


NUGET_PIN = {"package": "Honua.Sdk", "digest": "sha256:45e63f896a524ae4c620bef4065b632a270a79e4cfa274a349f2ffbccb8f051d"}


def _dotnet_cell(**identity):
    # The receipt is built before the observed package identity is attached, exactly as the
    # .NET producer emits it: the identity rides on the observation, not inside the receipt.
    cell = _cell(canonical_client="Honua SDK .NET", client_lane="sdk-dotnet-certification",
                 client_version="1.6.0", contract_revision="sdk-dotnet-certification@" + SHA,
                 auth_policy_revision="api-key-protected-v1")
    cell.update(identity)
    return cell


def test_dotnet_package_identity_is_accepted_when_it_names_the_pinned_nuget_artifact():
    schema = Draft202012Validator(json.loads(
        (Path(__file__).parents[1] / "certification/protocol-certification.v1.schema.json").read_text()))
    plain = _dotnet_cell()
    owned = _requirements(plain)
    carried = _dotnet_cell(client_package=NUGET_PIN["package"], client_package_digest=NUGET_PIN["digest"])
    # Producers that send no package identity are unchanged.
    assert cert._valid_receipt(plain, requirements_revision="requirements-test-v1")
    assert _evaluate(_ledger(plain), "nightly", requirements=owned, now=NOW)["overall_status"] == "pass"
    # A .NET result carrying the pinned package id and nupkg digest is accepted and verified.
    assert cert._valid_receipt(carried, requirements_revision="requirements-test-v1")
    assert not list(schema.iter_errors(_ledger(carried)))
    for tier, kwargs in (("nightly", {}), ("nightly", {"expected_client_artifacts": {"sdk-dotnet": NUGET_PIN}}),
                         ("release", {"expected_client_artifacts": {"sdk-dotnet": NUGET_PIN}})):
        report = _evaluate(_ledger(carried), tier, requirements=owned, now=NOW, expected_source_sha=SHA, **kwargs)
        assert report["overall_status"] == "pass", (tier, report["findings"])
    # Governed rows still cannot carry observed .NET identity.
    governed = copy.deepcopy(owned)
    governed["requirements"][0]["client_package_digest"] = NUGET_PIN["digest"]
    assert _evaluate(_ledger(plain), "nightly", requirements=governed, now=NOW)["overall_status"] == "fail"


def test_dotnet_package_identity_that_differs_from_the_pin_is_refused():
    schema = Draft202012Validator(json.loads(
        (Path(__file__).parents[1] / "certification/protocol-certification.v1.schema.json").read_text()))
    owned = _requirements(_dotnet_cell())
    pinned = {"expected_client_artifacts": {"sdk-dotnet": NUGET_PIN}}
    other_digest = "sha256:" + "0" * 64
    # Shape errors: refused by the receipt check, the ledger evaluation and the published schema.
    for identity in ({"client_package": "Honua.Sdk"},
                     {"client_package_digest": NUGET_PIN["digest"]},
                     {"client_package": None, "client_package_digest": None},
                     {"client_package": "Honua.Sdk.Core", "client_package_digest": NUGET_PIN["digest"]},
                     {"client_package": "honua.sdk", "client_package_digest": NUGET_PIN["digest"]},
                     {"client_package": "Honua.Sdk", "client_package_digest": NUGET_PIN["digest"].upper()},
                     {"client_package": "Honua.Sdk", "client_package_digest": "sha512-" + base64.b64encode(b"x" * 64).decode()},
                     {"client_package": {"filename": "honua_sdk-1.6.0-py3-none-any.whl", "sha256": "e" * 64, "source": "pypi"},
                      "client_package_digest": NUGET_PIN["digest"]}):
        cell = _dotnet_cell(**identity)
        assert not cert._valid_receipt(cell, requirements_revision="requirements-test-v1"), identity
        assert _evaluate(_ledger(cell), "nightly", requirements=owned, now=NOW)["overall_status"] == "fail", identity
        assert list(schema.iter_errors(_ledger(cell))), identity
    # Well-formed but not the pinned artifact: refused once the pin is supplied.
    repacked = _dotnet_cell(client_package="Honua.Sdk", client_package_digest=other_digest)
    report = _evaluate(_ledger(repacked), "nightly", requirements=owned, now=NOW, **pinned)
    assert report["overall_status"] == "fail"
    assert any("differs from the pinned NuGet artifact" in item["why"] for item in report["findings"])
    # Release never accepts carried identity it cannot verify against the pin.
    carried = _dotnet_cell(client_package="Honua.Sdk", client_package_digest=NUGET_PIN["digest"])
    report = _evaluate(_ledger(carried), "release", requirements=owned, now=NOW, expected_source_sha=SHA)
    assert report["overall_status"] == "fail"
    assert any("needs the pinned NuGet artifact" in item["why"] for item in report["findings"])
    # The receipt identity stays closed: the producer does not bind package fields into it.
    for field, value in (("client_package", "Honua.Sdk"), ("client_package_digest", NUGET_PIN["digest"])):
        changed = copy.deepcopy(carried)
        changed["evidence_receipt"]["identity"][field] = value
        assert not cert._valid_receipt(changed, requirements_revision="requirements-test-v1"), field
    # Other non-gRPC, non-Python lanes keep the #657/#663 refusal.
    other = _cell(canonical_client="Honua SDK .NET", client_lane="sdk-dotnet", client_version="1.6.0")
    other.update(client_package="Honua.Sdk", client_package_digest=NUGET_PIN["digest"])
    assert not cert._valid_receipt(other, requirements_revision="requirements-test-v1")
    assert _evaluate(_ledger(other), "nightly", now=NOW)["overall_status"] == "fail"


def test_dotnet_pin_comes_from_the_manifest_that_governs_the_catalog_rows():
    root = Path(__file__).parents[1]
    pinned = cert.pinned_client_artifacts(root / "platform-manifest.yaml")["sdk-dotnet"]
    rows = [row for row in json.loads((root / "certification/protocol-certification-requirements.v1.json").read_text())
            ["requirements"] if row["client_lane"] == cert.NUGET_SDK_LANE]
    assert pinned["package"] == cert.NUGET_SDK_PACKAGE
    assert rows and {row["client_version"] for row in rows} == {pinned["version"]}
    assert cert.pinned_client_artifacts(root / "no-such-manifest.yaml") is None
    owned = _requirements(_dotnet_cell())
    carried = _dotnet_cell(client_package="Honua.Sdk", client_package_digest=pinned["digest"])
    # The pin binds the version too: the same digest is not the pinned artifact of another version.
    report = _evaluate(_ledger(carried), "nightly", requirements=owned, now=NOW,
                       expected_client_artifacts={"sdk-dotnet": pinned})
    assert report["overall_status"] == "fail"
    report = _evaluate(_ledger(carried), "nightly", requirements=owned, now=NOW,
                       expected_client_artifacts={"sdk-dotnet": {**pinned, "version": "1.6.0"}})
    assert report["overall_status"] == "pass", report["findings"]


def test_dotnet_pin_overrides_layer_over_the_manifest_one_field_at_a_time():
    manifest = Path(__file__).parents[1] / "platform-manifest.yaml"
    pinned = cert.pinned_client_artifacts(manifest)["sdk-dotnet"]
    assert cert.expected_nuget_pin(manifest) == {"sdk-dotnet": pinned}
    assert cert.expected_nuget_pin(manifest, digest="sha256:" + "0" * 64)["sdk-dotnet"] == {
        **pinned, "digest": "sha256:" + "0" * 64}
    assert cert.expected_nuget_pin(manifest, package="Honua.Sdk")["sdk-dotnet"] == pinned
    absent = Path(__file__).parent / "no-such-manifest.yaml"
    assert cert.expected_nuget_pin(absent) is None
    assert cert.expected_nuget_pin(absent, digest=pinned["digest"])["sdk-dotnet"] == {
        "package": None, "digest": pinned["digest"], "version": None}
    # A digest-only override still accepts the observation matching the pinned artifact.
    carried = _dotnet_cell(client_package="Honua.Sdk", client_package_digest=pinned["digest"])
    expected = cert.expected_nuget_pin(manifest, digest=pinned["digest"], version="1.6.0")
    report = _evaluate(_ledger(carried), "nightly", requirements=_requirements(_dotnet_cell()), now=NOW,
                       expected_client_artifacts=expected)
    assert report["overall_status"] == "pass", report["findings"]


def test_python_wheel_identity_is_governed_not_just_self_consistent():
    cell = _cell(canonical_client="Honua SDK Python", client_lane="sdk-python-certification",
                 client_version="0.1.10", contract_revision="sdk-python-certification@" + SHA)
    cell["evidence_receipt"]["identity"]["candidate_cut_at"] = CUT
    _rehash_python_wheel(cell)
    owned = _requirements(cell)
    assert _evaluate(_ledger(cell), "nightly", requirements=owned, now=NOW)["overall_status"] == "pass"
    for field, value in (("filename", "other-0.1.10-py3-none-any.whl"),
                         ("filename", "honua_sdk-0.1.11-py3-none-any.whl"),
                         ("sha256", "f" * 64), ("sha256", "F" * 64),
                         ("source", "local-build"), ("extra", "unowned")):
        changed = copy.deepcopy(cell)
        changed["client_package"][field] = value
        changed["evidence_receipt"]["identity"]["client_package"] = copy.deepcopy(changed["client_package"])
        _rehash_python_wheel(changed)
        assert _evaluate(_ledger(changed), "nightly", requirements=owned, now=NOW)["overall_status"] == "fail"
    for target in ("cell", "identity", "governed"):
        changed, requirements = copy.deepcopy(cell), copy.deepcopy(owned)
        destination = changed if target == "cell" else changed["evidence_receipt"]["identity"]
        if target == "governed":
            requirements["requirements"][0].pop("client_package")
        else:
            destination.pop("client_package")
        _rehash_python_wheel(changed)
        assert _evaluate(_ledger(changed), "nightly", requirements=requirements, now=NOW)["overall_status"] == "fail"


def _rehash_python_wheel(cell):
    cell["evidence_digest"] = cert._receipt_digest(cell["evidence_receipt"])
    cell["evidence_uri"] = "https://evidence.honua.io/data/sha256/" + cell["evidence_digest"][7:]
    for facet in cell["facet_results"].values():
        facet["evidence_digest"] = cell["evidence_digest"]


def test_shared_actual_python_producer_bytes_pass_the_release_consumer_and_reject_rebinding():
    bundle = json.loads((Path(__file__).parent / "fixtures/python-receipt-v2-seam.json").read_text())
    observation = bundle["fragment"]["observations"][0]
    cell = {**bundle["requirements"]["requirements"][0], **observation, "budget_observations": None}
    ledger = _ledger(cell)
    ledger["requirements_revision"] = bundle["requirements"]["revision"]
    assert bundle["fragment"]["operation_scope"]["complete"] is False
    report = _evaluate(ledger, "nightly", requirements=bundle["requirements"], now=NOW)
    assert report["overall_status"] == "pass", report["findings"]
    for field, value in (("client_package", {"filename": "honua_sdk-9.9.9-py3-none-any.whl", "sha256": "e" * 64, "source": "pypi"}),
                         ("producer_source_sha", "e" * 40), ("client_version", "9.9.8")):
        changed = copy.deepcopy(ledger)
        changed["cells"][0][field] = value
        changed["cells"][0]["evidence_receipt"]["identity"][field] = value
        _rehash_python_wheel(changed["cells"][0])
        assert _evaluate(changed, "nightly", requirements=bundle["requirements"], now=NOW)["overall_status"] == "fail"


def test_python_wheel_schema_is_closed_and_other_lanes_cannot_claim_it():
    schema = json.loads((Path(__file__).parents[1] / "certification/protocol-certification.v1.schema.json").read_text())
    cell = _cell(client_package={"filename": "honua_sdk-1.4.3-py3-none-any.whl", "sha256": "e" * 64, "source": "pypi"})
    assert list(Draft202012Validator(schema).iter_errors(_ledger(cell)))
    assert _evaluate(_ledger(cell), "nightly", now=NOW)["overall_status"] == "fail"


def _mcp_package_versions():
    package = json.loads(
        (Path(__file__).resolve().parents[1] / "certification/sources/geospatial-mcp/package.json").read_text()
    )
    dependencies = package["dependencies"]
    return dependencies["@modelcontextprotocol/sdk"], dependencies["@modelcontextprotocol/inspector"]


def _mcp_obs(name, operation, result, mode, session, caller):
    if ":" in operation:
        method, target = operation.split(":", 1)
    else:
        method, target = operation, ""
    request = {"method": method, "target": target}
    outcome = "result" if result == "pass" else "error"
    code = "ok" if result == "pass" else "unauthorized"
    return {
        "result": result,
        "credential_mode": mode,
        "forwarded_credential": False,
        "session_id": session,
        "caller_id": caller,
        "request": request,
        "response": {
            "outcome": outcome,
            "code": code,
            "digest": cert._mcp_response_digest(name, request, outcome, code, session, caller),
        },
    }


def _mcp_protected_observations(operation="tools/call:geocode_address"):
    return {
        "authenticated-success": _mcp_obs(
            "authenticated-success", operation, "pass", "authenticated", "session-auth", "caller-auth"),
        "fresh-anonymous-refusal": _mcp_obs(
            "fresh-anonymous-refusal", operation, "refused", "anonymous", "session-anon", "caller-anon"),
        "no-credential-forwarding": _mcp_obs(
            "no-credential-forwarding", operation, "pass", "authenticated", "session-auth", "caller-auth"),
        "no-authenticated-session-reuse": _mcp_obs(
            "no-authenticated-session-reuse", operation, "refused", "anonymous", "session-reuse", "caller-anon"),
        "caller-session-binding": _mcp_obs(
            "caller-session-binding", operation, "refused", "authenticated", "session-auth", "caller-other"),
    }


def _mcp_anonymous_observations(operation="initialize"):
    return {
        "anonymous-success": _mcp_obs(
            "anonymous-success", operation, "pass", "anonymous", "session-a", "caller-anon"),
        "fresh-anonymous-session": _mcp_obs(
            "fresh-anonymous-session", operation, "pass", "anonymous", "session-b", "caller-anon"),
        "no-credentials": _mcp_obs(
            "no-credentials", operation, "pass", "anonymous", "session-c", "caller-anon"),
    }


def _bind_mcp_payload(cell, payload):
    cell["evidence_receipt"]["payload_base64"] = base64.b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).decode()
    cell["evidence_digest"] = cert._receipt_digest(cell["evidence_receipt"])
    cell["evidence_uri"] = "https://evidence.honua.io/data/sha256/" + cell["evidence_digest"][7:]
    cell["facet_results"] = {
        facet: {"result": "pass", "evidence_digest": cell["evidence_digest"]}
        for facet in cell["scenario_facets"]
    }
    return cell


def _mcp_payload(cell, observations):
    return {
        "schema": "honua.mcp-auth-observations/v1",
        "auth_policy_revision": cell["auth_policy_revision"],
        "policy_revision": "mcp-operation-auth-v1",
        "canonical_client": cell["canonical_client"],
        "client_lane": cell["client_lane"],
        "client_version": cell["client_version"],
        "operation": cell["operation"],
        "producer_source_sha": cell["producer_source_sha"],
        "observations": observations,
    }


def _mcp_cell(client, lane, version, operation, policy, observations):
    cell = _cell(
        capability_key="mcp.tool" if operation.startswith("tools/call:") else "mcp.transport",
        surface="mcp",
        operation=operation,
        canonical_client=client,
        client_lane=lane,
        client_version=version,
        scenario_facets=["positive", "negative", "media-schema", "auth"],
        contract_revision="geospatial-mcp@" + SHA,
        auth_policy_revision=policy,
    )
    return _bind_mcp_payload(cell, _mcp_payload(cell, observations))


def test_passing_mcp_cells_bind_policy_cohort_and_do_not_qualify_a_candidate():
    """A synthetic observation payload satisfies the checker only. It is not candidate proof."""
    sdk, inspector = _mcp_package_versions()
    protected = _mcp_cell(
        "Official MCP TypeScript SDK", "mcp-typescript-sdk", sdk,
        "tools/call:geocode_address", "mcp-protected-operation-v1", _mcp_protected_observations(),
    )
    anonymous = _mcp_cell(
        "MCP Inspector", "mcp-inspector", inspector,
        "initialize", "anonymous-public-v1", _mcp_anonymous_observations(),
    )
    for cell in (protected, anonymous):
        report = _evaluate(_ledger(cell), "nightly", requirements=_requirements(cell), now=NOW)
        assert report["overall_status"] == "pass", report["findings"]


def test_passing_mcp_cells_refuse_mislabeled_sessions_policy_cohort_and_source():
    sdk, _inspector = _mcp_package_versions()
    cell = _mcp_cell(
        "Official MCP TypeScript SDK", "mcp-typescript-sdk", sdk,
        "tools/call:geocode_address", "mcp-protected-operation-v1", _mcp_protected_observations(),
    )

    def mutated(change):
        changed = copy.deepcopy(cell)
        payload = json.loads(base64.b64decode(changed["evidence_receipt"]["payload_base64"]))
        change(payload)
        _bind_mcp_payload(changed, payload)
        report = _evaluate(_ledger(changed), "nightly", requirements=_requirements(changed), now=NOW)
        assert report["overall_status"] == "fail"
        return " ".join(item["why"] for item in report["findings"])

    assert "anonymous label on an authenticated observation" in mutated(
        lambda payload: payload["observations"]["fresh-anonymous-refusal"].__setitem__("credential_mode", "authenticated")
    )
    def reuse_authenticated_session(payload):
        obs = payload["observations"]["no-authenticated-session-reuse"]
        obs["session_id"] = "session-auth"
        obs["response"]["digest"] = cert._mcp_response_digest(
            "no-authenticated-session-reuse", obs["request"], obs["response"]["outcome"],
            obs["response"]["code"], obs["session_id"], obs["caller_id"],
        )

    assert "reused authenticated session" in mutated(reuse_authenticated_session)
    assert "MCP observation policy does not match the cell" in mutated(
        lambda payload: payload.__setitem__("auth_policy_revision", "anonymous-public-v1")
    )
    assert "MCP observation cohort does not match the pinned canonical client" in mutated(
        lambda payload: payload.__setitem__("client_version", "1.30.0")
    )
    assert "MCP observation source does not match the cell producer" in mutated(
        lambda payload: payload.__setitem__("producer_source_sha", "e" * 40)
    )
    assert "MCP authorization observations are incomplete" in mutated(
        lambda payload: payload["observations"].pop("caller-session-binding")
    )


def test_passing_mcp_cells_refuse_an_authenticated_caller_labeled_anonymous():
    """A caller's credential mode is a property of that caller, not of each row.

    The authenticated caller's identity reappearing under an anonymous label is an
    authenticated call recorded as anonymous, and the reverse borrows authority.
    """
    sdk, _inspector = _mcp_package_versions()

    def evaluate(observations):
        cell = _mcp_cell(
            "Official MCP TypeScript SDK", "mcp-typescript-sdk", sdk,
            "tools/call:geocode_address", "mcp-protected-operation-v1", observations,
        )
        report = _evaluate(_ledger(cell), "nightly", requirements=_requirements(cell), now=NOW)
        return report["overall_status"], " ".join(item["why"] for item in report["findings"])

    relabeled = _mcp_protected_observations()
    relabeled["no-authenticated-session-reuse"] = _mcp_obs(
        "no-authenticated-session-reuse", "tools/call:geocode_address",
        "refused", "anonymous", "session-reuse", "caller-auth")
    status, why = evaluate(relabeled)
    assert status == "fail"
    assert "anonymous label on an authenticated caller" in why

    borrowed = _mcp_protected_observations()
    borrowed["caller-session-binding"] = _mcp_obs(
        "caller-session-binding", "tools/call:geocode_address",
        "refused", "authenticated", "session-auth", "caller-anon")
    status, why = evaluate(borrowed)
    assert status == "fail"
    assert "anonymous label on an authenticated caller" in why


def test_nonpassing_mcp_cell_does_not_require_authorization_observations():
    _sdk, inspector = _mcp_package_versions()
    in_scope = _cell(required_tier="pr")
    cell = _cell(
        surface="mcp",
        operation="initialize",
        canonical_client="MCP Inspector",
        client_lane="mcp-inspector",
        client_version=inspector,
        result="fail",
        required_tier="nightly",
        scenario_facets=["positive", "negative", "media-schema", "auth"],
        auth_policy_revision="anonymous-public-v1",
        contract_revision="geospatial-mcp@" + SHA,
    )
    report = _evaluate(
        _ledger(in_scope, cell), "pr", requirements=_requirements(in_scope, cell), now=NOW,
    )
    assert report["overall_status"] == "pass", report["findings"]
    assert not any("MCP " in item["why"] for item in report["findings"])


def test_copied_mcp_probe_does_not_certify_another_operation():
    sdk, _inspector = _mcp_package_versions()
    copied = _mcp_cell(
        "Official MCP TypeScript SDK", "mcp-typescript-sdk", sdk,
        "tools/call:geocode_address", "mcp-protected-operation-v1",
        _mcp_protected_observations("tools/call:list_layers"),
    )
    report = _evaluate(_ledger(copied), "nightly", requirements=_requirements(copied), now=NOW)
    assert report["overall_status"] == "fail"
    assert any("request does not match the cell operation" in item["why"] for item in report["findings"])

    cell = _mcp_cell(
        "Official MCP TypeScript SDK", "mcp-typescript-sdk", sdk,
        "tools/call:geocode_address", "mcp-protected-operation-v1", _mcp_protected_observations(),
    )
    payload = json.loads(base64.b64decode(cell["evidence_receipt"]["payload_base64"]))
    payload["observations"]["authenticated-success"]["response"]["digest"] = (
        payload["observations"]["no-credential-forwarding"]["response"]["digest"]
    )
    _bind_mcp_payload(cell, payload)
    report = _evaluate(_ledger(cell), "nightly", requirements=_requirements(cell), now=NOW)
    assert report["overall_status"] == "fail"
    assert any("not bound to its request" in item["why"] for item in report["findings"])


def _portal_document():
    import portal_group_sharing as portal

    shared = {"member": {name: True for name in portal.SURFACES}, "nonmember": {name: False for name in portal.SURFACES}}
    hidden = {"member": {name: False for name in portal.SURFACES}, "nonmember": {name: False for name in portal.SURFACES}}
    return {
        "schema": portal.SCHEMA,
        "candidateSourceSha": "a" * 40,
        "candidateImage": "ghcr.io/example/server@sha256:" + "b" * 64,
        "identities": {
            "owner": "owner-1", "member": "member-1", "nonmember": "nonmember-1",
            "group": "group-1", "item": "item-1",
        },
        "runtime": {
            "beforeRestart": "process-before", "afterRestart": "process-after",
            "replica": "process-replica", "store": "store-1",
        },
        "notSharedWith": ["everyone", "org"],
        "publicCallerVisibilityUnchanged": True,
        "policyRefusals": {name: "refused" for name in portal.POLICY_REFUSALS},
        "phases": {
            "shared": shared,
            "sharedAfterRestart": copy.deepcopy(shared),
            "sharedOnReplica": copy.deepcopy(shared),
            "unshared": hidden,
            "unsharedAfterRestart": copy.deepcopy(hidden),
        },
    }


def _portal_cell(**overrides):
    cell = _cell(
        surface="portal-sharing",
        operation="group-member-readback",
        canonical_client="Portal group client",
        client_lane="portal-group-sharing",
        required_tier="nightly",
        **overrides,
    )
    return cell


def _bind_portal_payload(cell, document):
    cell["evidence_receipt"]["payload_base64"] = base64.b64encode(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    ).decode()
    cell["evidence_digest"] = cert._receipt_digest(cell["evidence_receipt"])
    cell["evidence_uri"] = "https://evidence.honua.io/data/sha256/" + cell["evidence_digest"][7:]
    cell["facet_results"] = {
        facet: {"result": "pass" if cell["result"] == "pass" else "fail", "evidence_digest": cell["evidence_digest"]}
        for facet in cell["scenario_facets"]
    }
    return cell


def test_nightly_portal_sharing_cells_are_judged_against_the_ledger_candidate(monkeypatch):
    module = cert._portal_sharing_module()
    calls = []
    original = module.qualify

    def spy(document, candidate=None):
        calls.append(document)
        # The checker always binds the ledger candidate; it never asks for an unbound verdict.
        assert candidate == {"sourceSha": SHA, "imageDigest": DIGEST}
        return original(document, candidate=candidate)

    monkeypatch.setattr(module, "qualify", spy)
    absent = _portal_cell()
    report = _evaluate(_ledger(absent), "nightly", requirements=_requirements(absent), now=NOW)
    assert report["overall_status"] == "fail", report["findings"]
    assert any("no-retained-observations" in item["why"] for item in report["findings"])
    assert calls == [None]

    complete = _bind_portal_payload(copy.deepcopy(absent), _portal_document())
    calls.clear()
    report = _evaluate(_ledger(complete), "nightly", requirements=_requirements(complete), now=NOW)
    assert not any("portal group sharing" in item["why"] for item in report["findings"]), report["findings"]
    assert calls and calls[0]["schema"].endswith("portal-group-sharing-observations/v1")

    # Complete observations of another candidate (source or image) never qualify this ledger.
    for field, value in (("candidateSourceSha", "c" * 40),
                         ("candidateImage", "ghcr.io/example/server@sha256:" + "d" * 64)):
        other = _portal_document()
        other[field] = value
        foreign = _bind_portal_payload(copy.deepcopy(absent), other)
        report = _evaluate(_ledger(foreign), "nightly", requirements=_requirements(foreign), now=NOW)
        assert report["overall_status"] == "fail"
        assert any("portal group sharing refused: candidate-mismatch" in item["why"] for item in report["findings"])

    incomplete = _portal_document()
    incomplete["phases"]["shared"]["nonmember"]["feature"] = True
    refused = _bind_portal_payload(_portal_cell(result="fail"), incomplete)
    report = _evaluate(_ledger(refused), "nightly", requirements=_requirements(refused), now=NOW)
    assert any("nonmember-readback" in item["why"] for item in report["findings"])
    assert report["overall_status"] == "fail"

    calls.clear()
    in_scope = _cell(required_tier="pr")
    parked = _portal_cell(result="fail")
    report = _evaluate(
        _ledger(in_scope, parked), "pr", requirements=_requirements(in_scope, parked), now=NOW,
    )
    assert report["overall_status"] == "pass", report["findings"]
    assert calls == []

    claimed = _portal_cell()
    report = _evaluate(
        _ledger(in_scope, claimed), "pr", requirements=_requirements(in_scope, claimed), now=NOW,
    )
    assert report["overall_status"] == "fail"
    assert any("no-retained-observations" in item["why"] for item in report["findings"])


# cng-preview-producer-2026-10-27: honua-server-cng is declared Preview evidence for 2026.1.
def _preview_overrides(tmp_path, producers=("honua-server-cng",), owner="Mike McDougall (founder)"):
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"rulings": {"cng-preview-producer-2026-10-27": {
        "owner": owner, "preview_producers": list(producers)}}}), encoding="utf-8")
    return path


def _unexecuted(**overrides):
    """A required cell no producer observed, as honua-evidence publishes it."""
    return _cell(result="skip", skip_reason="no producer evidence for required certification cell",
                 source_sha=None, producer_source_sha=None, started_at=None, completed_at=None,
                 evidence_uri=None, evidence_digest=None, evidence_receipt=None, facet_results=None, **overrides)


def _cng_skip(**overrides):
    return _unexecuted(canonical_client="GDAL", client_lane="gdal-cog", **overrides)


def _with_producers(ledger):
    requirements = _requirements(*ledger["cells"])
    requirements["production"] = {"producers": [
        {"producer": "honua-server-cng", "client_lanes": ["gdal-cog"]},
        {"producer": "other", "client_lanes": ["rasterio"]},
    ]}
    return requirements


def test_declared_preview_producer_without_receipt_is_reported_not_refused(tmp_path):
    ledger = _ledger(_cell(), _cng_skip())
    report = _evaluate(ledger, "nightly", now=NOW, requirements=_with_producers(ledger),
                       preview_overrides=_preview_overrides(tmp_path))
    assert report["overall_status"] == "pass", report["findings"]
    assert report["declared_preview"] == [{"producer": "honua-server-cng", "cells": 1,
                                           "ruling": "cng-preview-producer-2026-10-27",
                                           "status": "preview: no candidate-bound receipt required"}]


def test_declared_preview_needs_the_founder_ruling(tmp_path):
    ledger = _ledger(_cell(), _cng_skip())
    for overrides in (tmp_path / "absent.json", _preview_overrides(tmp_path, owner="release coordinator"),
                      _preview_overrides(tmp_path, producers=())):
        report = _evaluate(ledger, "nightly", now=NOW, requirements=_with_producers(ledger),
                           preview_overrides=overrides)
        assert report["overall_status"] == "fail"
        assert "declared_preview" not in report


def test_other_producer_missing_its_receipt_still_fails(tmp_path):
    ledger = _ledger(_unexecuted(), _cng_skip())
    report = _evaluate(ledger, "nightly", now=NOW, requirements=_with_producers(ledger),
                       preview_overrides=_preview_overrides(tmp_path))
    assert report["overall_status"] == "fail"
    assert any(finding["check"] == "cells[0]" and "'skip'" in finding["why"] for finding in report["findings"])
    assert report["declared_preview"][0]["cells"] == 1


def test_preview_lapses_once_the_producer_emits_a_receipt(tmp_path):
    receipted = _cell(canonical_client="GDAL", client_lane="gdal-cog", operation="overview-read",
                      evidence_receipt={"schema": "honua.certification-evidence-receipt/v2"})
    ledger = _ledger(_cell(), _cng_skip(), receipted)
    report = _evaluate(ledger, "nightly", now=NOW, requirements=_with_producers(ledger),
                       preview_overrides=_preview_overrides(tmp_path))
    assert report["overall_status"] == "fail"
    assert "declared_preview" not in report
    assert any(finding["check"] == "cells[1]" and "'skip'" in finding["why"] for finding in report["findings"])


def test_preview_ruling_is_recorded_in_the_decision_overrides():
    from protocol_preview import declared_preview_producers
    assert declared_preview_producers() == frozenset({"honua-server-cng"})


# sdk-harness-trunk-pinned-package-2026-10-27: an SDK harness receipt names the trunk harness commit.
def _rebound(cell):
    cell["evidence_digest"] = cert._receipt_digest(cell["evidence_receipt"])
    cell["evidence_uri"] = "https://evidence.honua.io/data/sha256/" + cell["evidence_digest"][7:]
    for facet in cell["facet_results"].values():
        facet["evidence_digest"] = cell["evidence_digest"]
    return cell


def _dotnet_harness_cell(**identity):
    cell = _cell(canonical_client="Honua SDK .NET", client_lane="sdk-dotnet-certification",
                 client_package="Honua.Sdk", client_package_digest="sha256:" + "4" * 64)
    cell["evidence_receipt"]["identity"].update({"harness_commit": "5" * 40, "client_package": "Honua.Sdk",
                                                 "client_package_digest": "sha256:" + "4" * 64, **identity})
    return _rebound(cell)


def _evaluate_dotnet(cell):
    governed = {key: value for key, value in cell.items() if key not in ("client_package", "client_package_digest")}
    return _evaluate(_ledger(cell), "nightly", now=NOW, requirements=_requirements(governed))


def test_dotnet_harness_receipt_binds_the_harness_commit_and_package_digest():
    schema = json.loads((Path(__file__).parents[1] / "certification" / "protocol-certification.v1.schema.json")
                        .read_text(encoding="utf-8"))
    cell = _dotnet_harness_cell()
    assert not list(Draft202012Validator(schema).iter_errors(_ledger(cell)))
    assert _evaluate_dotnet(cell)["overall_status"] == "pass"
    for broken in (_dotnet_harness_cell(harness_commit="HEAD"),
                   _dotnet_harness_cell(client_package_digest="sha256:" + "6" * 64)):
        assert _evaluate_dotnet(broken)["overall_status"] == "fail"
    missing = _dotnet_harness_cell()
    del missing["evidence_receipt"]["identity"]["client_package_digest"]
    assert _evaluate_dotnet(_rebound(missing))["overall_status"] == "fail"
    assert list(Draft202012Validator(schema).iter_errors(_ledger(missing)))


def test_only_an_sdk_harness_lane_may_record_a_harness_commit():
    cell = _cell()
    cell["evidence_receipt"]["identity"]["harness_commit"] = "5" * 40
    assert _evaluate(_ledger(_rebound(cell)), "nightly", now=NOW)["overall_status"] == "fail"
