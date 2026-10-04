"""R27: the booted candidate's advertised contract versions must equal its source declaration."""
import copy
import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_contract_versions_live as live  # noqa: E402

SERVER_SHA = "87966c3f7b6c840ffc4d4da0b451714ab717b18a"
IMAGE_REF = ("ghcr.io/honua-io/honua-server@sha256:"
             "0d4c9f3b5e2a7c1d6f8e9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d")

# honua-server release/component-versions.json as committed by honua-server#5369.
DECLARATION = {
    "format": "honua.component-versions/v1",
    "component": "honua-server",
    "contractVersions": {"admin": "v1", "metadata": "metadata.honua.io/v2alpha1", "grpc": "v1",
                         "geoservices": "1.0.0", "ogc": "1.0.0", "stac": "1.0.0"},
    "schemaVersions": {},
}

# The anonymous /api/v1/admin/capabilities envelope as the server serializes it today
# (AdminInfoEndpoints.HandleGetCapabilities): no contractVersions map.
TODAY = {
    "success": True,
    "data": {
        "metadataApiVersion": "metadata.honua.io/v2alpha1",
        "metadataSchemaVersion": "2.0.0-alpha.1",
        "serverVersion": "1.0.0",
        "compatibility": {
            "serverVersion": "1.0.0",
            "releaseChannel": "stable",
            "controlPlaneApi": {"major": 1, "basePath": "/api/v1/admin", "deprecated": False},
            "metadataSchemas": [{"version": "metadata.honua.io/v2alpha1", "deprecated": False}],
            "features": {"metadataResources": True, "manifestExport": False, "manifestApply": False,
                         "manifestDryRun": False, "manifestPrune": False},
            "adminApiMajor": "v1",
            "metadataApiVersion": "metadata.honua.io/v2alpha1",
            "metadataSchemaVersion": "2.0.0-alpha.1",
        },
    },
    "message": None,
    "timestamp": "2026-10-03T06:00:00+00:00",
}


def advertising(contract_versions):
    response = copy.deepcopy(TODAY)
    response["data"]["compatibility"]["contractVersions"] = contract_versions
    return response


IDENTICAL = advertising({"admin": "v1", "metadata": "metadata.honua.io/v2alpha1", "grpc": "v1",
                         "geoservices": "1.0.0", "ogc": "1.0.0", "stac": "1.0.0"})
# One drifted key: the image advertises stac 1.1.0 while its source still declares 1.0.0.
DRIFTED = advertising({"admin": "v1", "metadata": "metadata.honua.io/v2alpha1", "grpc": "v1",
                       "geoservices": "1.0.0", "ogc": "1.0.0", "stac": "1.1.0"})


@pytest.fixture
def inputs(tmp_path):
    manifest = tmp_path / "platform-manifest.yaml"
    manifest.write_text(yaml.safe_dump({"components": {"honua-server": {
        "repository": "https://github.com/honua-io/honua-server", "sha": SERVER_SHA,
        "image": "ghcr.io/honua-io/honua-server:nightly-87966c3",
        "contractVersions": copy.deepcopy(DECLARATION["contractVersions"])}}}))
    declaration = tmp_path / "component-versions.json"
    declaration.write_text(json.dumps(DECLARATION))
    capabilities = tmp_path / "capabilities.json"
    return manifest, declaration, capabilities


def run(inputs, response, declaration=None):
    manifest, default_declaration, capabilities = inputs
    if response is not None:
        capabilities.write_text(response if isinstance(response, str) else json.dumps(response))
    out = capabilities.parent / "report.json"
    code = live.main(["--manifest", str(manifest), "--capabilities", str(capabilities),
                      "--image-ref", IMAGE_REF, "--declaration", str(declaration or default_declaration),
                      "--out", str(out)])
    return code, json.loads(out.read_text())


def test_identical_maps_pass(inputs):
    code, report = run(inputs, IDENTICAL)
    assert code == 0
    assert report == {
        "gate": "contract-live",
        "status": "pass",
        "why": "all 6 declared contract versions are advertised unchanged",
        "declared": {"admin": "v1", "geoservices": "1.0.0", "grpc": "v1",
                     "metadata": "metadata.honua.io/v2alpha1", "ogc": "1.0.0", "stac": "1.0.0"},
        "advertised": {"admin": "v1", "geoservices": "1.0.0", "grpc": "v1",
                       "metadata": "metadata.honua.io/v2alpha1", "ogc": "1.0.0", "stac": "1.0.0"},
        "findings": [],
        "manifest": DECLARATION["contractVersions"],
        "manifestFindings": [],
        "image": IMAGE_REF,
        "declaration": {"repository": "honua-io/honua-server", "sha": SERVER_SHA,
                        "path": "release/component-versions.json"},
        "capabilities": "/api/v1/admin/capabilities",
    }


def test_one_drifted_key_refuses_and_the_row_carries_both_maps(inputs):
    code, report = run(inputs, DRIFTED)
    assert code == 1
    assert report["status"] == "fail"
    assert report["findings"] == [
        {"key": "stac", "kind": "mismatch", "declared": "1.0.0", "advertised": "1.1.0"}]
    assert report["why"] == ("advertised contract versions differ from the declaration: "
                             "stac mismatch (declared '1.0.0', advertised '1.1.0')")
    assert report["declared"] == {"admin": "v1", "geoservices": "1.0.0", "grpc": "v1",
                                  "metadata": "metadata.honua.io/v2alpha1", "ogc": "1.0.0", "stac": "1.0.0"}
    assert report["advertised"] == {"admin": "v1", "geoservices": "1.0.0", "grpc": "v1",
                                    "metadata": "metadata.honua.io/v2alpha1", "ogc": "1.0.0", "stac": "1.1.0"}


def test_missing_and_extra_advertised_keys_refuse(inputs):
    code, report = run(inputs, advertising({
        "admin": "v1", "metadata": "metadata.honua.io/v2alpha1", "geoservices": "1.0.0",
        "ogc": "1.0.0", "stac": "1.0.0", "odata": "4.0"}))
    assert code == 1
    assert report["findings"] == [
        {"key": "grpc", "kind": "missing", "declared": "v1", "advertised": None},
        {"key": "odata", "kind": "extra", "declared": None, "advertised": "4.0"},
    ]


def test_todays_envelope_advertises_only_admin_and_metadata(inputs):
    """Without a contractVersions map the envelope names two contracts; the rest are missing."""
    code, report = run(inputs, TODAY)
    assert code == 1
    assert report["advertised"] == {"admin": "v1", "metadata": "metadata.honua.io/v2alpha1"}
    assert report["findings"] == [
        {"key": "geoservices", "kind": "missing", "declared": "1.0.0", "advertised": None},
        {"key": "grpc", "kind": "missing", "declared": "v1", "advertised": None},
        {"key": "ogc", "kind": "missing", "declared": "1.0.0", "advertised": None},
        {"key": "stac", "kind": "missing", "declared": "1.0.0", "advertised": None},
    ]


def test_an_envelope_field_that_drifts_refuses(inputs):
    response = copy.deepcopy(TODAY)
    response["data"]["compatibility"]["adminApiMajor"] = "v2"
    assert live.advertised_contract_versions(response) == {
        "admin": "v2", "metadata": "metadata.honua.io/v2alpha1"}
    assert live.compare({"admin": "v1", "metadata": "metadata.honua.io/v2alpha1"},
                        live.advertised_contract_versions(response)) == [
        {"key": "admin", "kind": "mismatch", "declared": "v1", "advertised": "v2"}]


def test_the_explicit_map_is_the_whole_advertised_set(inputs):
    """When the server publishes contractVersions, the flat fields do not add keys to it."""
    assert live.advertised_contract_versions(advertising({"admin": "v1"})) == {"admin": "v1"}
    assert live.advertised_contract_versions(advertising({})) == {}


@pytest.mark.parametrize("response, detail", [
    ("not json", "/api/v1/admin/capabilities unreadable: Expecting value"),
    ({"success": False, "data": None}, "success must be true"),
    ({"success": True, "data": None}, "has no data.compatibility object"),
    (advertising(["admin", "v1"]), "contractVersions must map names to version strings"),
    (advertising({"admin": 1}), "contractVersions must map names to version strings"),
])
def test_an_unreadable_response_is_blocked_never_a_pass(inputs, response, detail):
    code, report = run(inputs, response)
    assert code == 3
    assert report["status"] == "blocked"
    assert detail in report["why"]
    assert report["declared"] == {"admin": "v1", "geoservices": "1.0.0", "grpc": "v1",
                                  "metadata": "metadata.honua.io/v2alpha1", "ogc": "1.0.0", "stac": "1.0.0"}
    assert report["advertised"] is None


def test_a_candidate_that_never_served_a_response_is_blocked(inputs):
    code, report = run(inputs, None)
    assert code == 3
    assert report["status"] == "blocked"
    assert "No such file" in report["why"]


@pytest.mark.parametrize("edit, detail", [
    (lambda d: d.update(component="honua-console"), "declares component 'honua-console', not 'honua-server'"),
    (lambda d: d["contractVersions"].update(stac="latest"), "not an exact version"),
    (lambda d: d.update(contractVersions={}), "an empty map is permitted only for a sourcePinnedOnly"),
    (lambda d: d.pop("contractVersions"), "does not match component-versions.v1.schema.json"),
])
def test_the_declaration_is_validated_as_the_resolver_reads_it(inputs, edit, detail):
    declaration = copy.deepcopy(DECLARATION)
    edit(declaration)
    path = inputs[0].parent / "edited.json"
    path.write_text(json.dumps(declaration))
    code, report = run(inputs, IDENTICAL, declaration=path)
    assert code == 3
    assert report["status"] == "blocked"
    assert report["why"].startswith("declaration unreadable: honua-server: ")
    assert detail in report["why"]
    assert report["declaration"] is None


def test_the_declaration_not_the_manifest_map_is_the_input(inputs):
    """A partial manifest cannot certify a matching image and six-key declaration."""
    manifest = yaml.safe_load(inputs[0].read_text())
    manifest["components"]["honua-server"]["contractVersions"] = {"admin": "v1"}
    inputs[0].write_text(yaml.safe_dump(manifest))
    code, report = run(inputs, IDENTICAL)
    assert code == 1
    assert len(report["declared"]) == 6
    assert {f["key"] for f in report["manifestFindings"]} == set(DECLARATION["contractVersions"]) - {"admin"}
    assert all(f["kind"] == "missing" for f in report["manifestFindings"])


@pytest.mark.parametrize("success", [False, None, "true", 1])
def test_unsuccessful_envelope_with_matching_versions_is_blocked(inputs, success):
    response = copy.deepcopy(IDENTICAL)
    response["success"] = success
    code, report = run(inputs, response)
    assert code == 3
    assert report["status"] == "blocked"
    assert "success must be true" in report["why"]


@pytest.mark.parametrize("pinned, kind", [
    ({**DECLARATION["contractVersions"], "stac": "1.1.0"}, "mismatch"),
    ({**DECLARATION["contractVersions"], "odata": "4.0"}, "extra"),
])
def test_manifest_drift_refuses_matching_source_and_image(inputs, pinned, kind):
    manifest = yaml.safe_load(inputs[0].read_text())
    manifest["components"]["honua-server"]["contractVersions"] = pinned
    inputs[0].write_text(yaml.safe_dump(manifest))
    code, report = run(inputs, IDENTICAL)
    assert code == 1
    assert report["declared"] == report["advertised"] == DECLARATION["contractVersions"]
    assert report["manifest"] == pinned
    assert len(report["manifestFindings"]) == 1
    assert report["manifestFindings"][0]["kind"] == kind


@pytest.mark.parametrize("pinned", [None, [], {"admin": 1}])
def test_malformed_manifest_versions_are_blocked(inputs, pinned):
    manifest = yaml.safe_load(inputs[0].read_text())
    manifest["components"]["honua-server"]["contractVersions"] = pinned
    inputs[0].write_text(yaml.safe_dump(manifest))
    code, report = run(inputs, IDENTICAL)
    assert code == 3
    assert "manifest components.honua-server.contractVersions" in report["why"]
