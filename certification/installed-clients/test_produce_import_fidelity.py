"""Producer negative controls are unit tests, never live qualification receipts."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
from unittest import mock

import pytest
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dotnet_import_fidelity as gate
import produce_import_fidelity as producer

EVIDENCE = "https://github.com/honua-io/honua-release/actions/runs/1"


@pytest.fixture
def manifest():
    return yaml.safe_load((HERE.parents[1] / "platform-manifest.yaml").read_text())


def test_diagnostic_package_install_cannot_become_complete_fidelity(manifest):
    score = producer.Scorecard(manifest, gate.load_requirements(), EVIDENCE)
    score.record("journey.published-package-install", lambda: "installed")
    receipt = score.receipt()
    assert receipt["summary"] == {"denominator": 142, "passed": 1, "failed": 141, "skipped": 0, "notApplicable": 10}
    assert len(receipt["cells"]) == 152
    verdict = gate.evaluate(manifest, receipt)
    assert verdict["status"] == "fail"
    assert any(item["check"] == "rollback-repoint" for item in verdict["findings"])
    assert all(cell["executed"] is False for cell in receipt["cells"] if cell["verdict"] == "not-applicable")


@pytest.mark.parametrize("expected,actual", [
    (9007199254740993, 9007199254740992),
    (9007199254740993, 9007199254740992.0),
    (None, ""), (False, 0), ({"defaultValue": None}, {}),
    ({"geometry": None}, {"geometry": {}}),
    ({"domain": {"codedValues": [{"code": 1, "name": "open"}]}}, {"domain": None}),
    ({"alias": "Site name"}, {"alias": "name"}),
])
def test_seeded_data_and_metadata_loss_is_rejected(expected, actual):
    with pytest.raises(producer.ProducerError):
        producer.require_equal(expected, actual, "seeded-fidelity-fault")


def test_geometry_tolerance_is_bounded_and_checks_zm():
    expected = [[1, {"paths": [[[1.0, 2.0, 30.0, 40.0], [3.0, 4.0, 50.0, 60.0]]]}]]
    within = copy.deepcopy(expected)
    within[0][1]["paths"][0][0][0] += 1e-10
    assert producer.equal(expected, within, tolerance=1e-8)
    for ordinate in range(4):
        fault = copy.deepcopy(expected)
        fault[0][1]["paths"][0][0][ordinate] += 0.1
        assert not producer.equal(expected, fault)


def test_wrong_artifact_identity_fails_before_restore(manifest, tmp_path):
    for change in ({"registry": "private.example"}, {"version": "1.*"}, {"publicationState": "built"}, {"digest": ""}):
        invalid = copy.deepcopy(manifest)
        invalid["clientArtifacts"][gate.PIN_ARTIFACT].update(change)
        with mock.patch.object(producer, "installed_module") as installer:
            with pytest.raises(producer.ProducerError):
                producer.install_consumer(invalid, tmp_path / "consumer")
            installer.assert_not_called()


def test_partial_receipt_and_wrong_package_are_both_rejected(manifest):
    score = producer.Scorecard(manifest, gate.load_requirements(), EVIDENCE)
    score.consumer = {"kind": "published-nuget", "clean": True, "projectReference": False,
        "assemblyMatchesPackage": True, "sourceBuilt": False, "registry": gate.PUBLIC_REGISTRIES[0],
        "packageId": "Honua.Sdk", "version": "0.0.0", "digest": "sha256:" + "0" * 64}
    receipt = score.receipt()
    verdict = gate.evaluate(manifest, receipt)
    assert verdict["status"] == "fail"
    assert any(item["check"] == "wrong-version" for item in verdict["findings"])
    assert verdict["denominator"] == 142


def test_partial_transfer_never_satisfies_value_parity():
    with pytest.raises(producer.ProducerError, match="transfer limit"):
        producer.feature_rows({"features": [], "exceededTransferLimit": True}, ["gid"], "gid")
    with pytest.raises(producer.ProducerError, match="dropped source fields"):
        producer.feature_rows({"features": [{"attributes": {"gid": 1}}]}, ["gid", "name"], "gid")


def test_failed_check_is_executed_and_never_changes_denominator(manifest):
    score = producer.Scorecard(manifest, gate.load_requirements(), EVIDENCE)
    score.record("esri_census_states/all_fields_diff", lambda: producer.require_equal(
        {"alias": "State", "nullable": False}, {"alias": "State", "nullable": True}, "metadata drift"))
    cell = score.cells["esri_census_states/all_fields_diff"]
    assert cell["verdict"] == "fail" and cell["executed"] is True
    assert score.receipt()["summary"]["denominator"] == 142


def test_clean_workspace_must_not_exist_before_install(manifest, tmp_path):
    with pytest.raises(producer.ProducerError, match="must not exist"):
        producer.install_consumer(manifest, tmp_path)


def test_real_service_cases_equal_the_frozen_baseline():
    assert set(producer.CASES) == set(gate.FROZEN_SERVICE_CASES)
    assert all(url.startswith("https://") for url, *_ in producer.CASES.values())


def test_seeded_baseline_metadata_and_data_faults_fail_named_cells(manifest):
    source = {
        "metadata": {"objectIdField": "gid", "geometryType": "esriGeometryPoint", "fields": [
            {"name": "gid", "type": "esriFieldTypeOID", "alias": "Identity", "nullable": False},
            {"name": "value", "type": "esriFieldTypeBigInteger", "alias": "Precise value", "nullable": True},
        ]},
        "objectIdField": "gid", "count": 1, "ids": [1],
        "features": [{"attributes": {"gid": 1, "value": 9007199254740993}, "geometry": {"x": 1.0, "y": 2.0}}],
    }
    class UnitSdk:
        def call(self, operation, **kwargs):
            if operation == "StartGeoservicesImportAsync":
                return {"jobId": "unit-job"}
            if operation == "GetGeoservicesImportJobStatusAsync":
                return {"status": "Completed", "publishedLayerId": 1, "failedFeatures": 0}
            if operation == "extent":
                return {"xmin": 1, "ymin": 2, "xmax": 1, "ymax": 2}
            if operation == "query":
                return {"features": [], "error": {"code": 400, "message": "invalid field"}}
            raise AssertionError(operation)
    def run(target):
        score = producer.Scorecard(manifest, gate.load_requirements(), EVIDENCE)
        # Explicit unit transport/oracle substitution; these results are never written as
        # qualification evidence. Both faults affect actual named baseline code paths.
        with mock.patch.object(producer, "sql", return_value="1"), mock.patch.object(
                producer, "read_snapshot", side_effect=[copy.deepcopy(source), target, copy.deepcopy(source), target]):
            producer.baseline_case(score, UnitSdk(), [], "esri_census_states", "http://localhost", "unit")
        return score
    normal = run(copy.deepcopy(source))
    assert normal.cells["esri_census_states/all_fields_diff"]["verdict"] == "pass"
    assert normal.cells["esri_census_states/core_query_parity"]["verdict"] == "pass"
    fault = copy.deepcopy(source)
    fault["metadata"]["fields"][1]["alias"] = "Lost source alias"
    fault["features"][0]["attributes"]["value"] = 9007199254740992
    corrupted = run(fault)
    assert corrupted.cells["esri_census_states/all_fields_diff"]["verdict"] == "fail"
    assert corrupted.cells["esri_census_states/core_query_parity"]["verdict"] == "fail"
    assert corrupted.receipt()["summary"]["denominator"] == 142


def test_wrong_loaded_artifact_identity_is_rejected():
    consumer = {"assemblies": [{"name": name, "sha256": "a" * 64}
                               for name in ("Honua.Sdk.Admin", "Honua.Sdk.GeoServices")]}
    process = mock.Mock()
    process.poll.return_value = 0
    with mock.patch.object(producer.subprocess, "Popen", return_value=process), mock.patch.object(
            producer.Sdk, "call", return_value=[{"name": "Honua.Sdk.Admin", "sha256": "b" * 64},
                                               {"name": "Honua.Sdk.GeoServices", "sha256": "a" * 64}]):
        with pytest.raises(producer.ProducerError, match="loaded SDK assemblies differ"):
            producer.Sdk(["unit-only"], {}, consumer)
    assert consumer.get("assemblyMatchesPackage") is not True


@pytest.mark.parametrize("fault", ["image", "source", "digest"])
def test_wrong_running_candidate_identity_fails(manifest, fault):
    sha, image = gate.server_image_ref(manifest)
    container = {"Config": {"Image": image}, "Image": "sha256:" + "1" * 64}
    info = {"Config": {"Labels": {"org.opencontainers.image.revision": sha}},
            "RepoDigests": ["ghcr.io/honua-io/honua-server@" + manifest["components"]["honua-server"]["digest"]]}
    if fault == "image":
        container["Config"]["Image"] = "ghcr.io/honua-io/honua-server:latest"
    elif fault == "source":
        info["Config"]["Labels"]["org.opencontainers.image.revision"] = "0" * 40
    else:
        info["RepoDigests"] = ["ghcr.io/honua-io/honua-server@sha256:" + "0" * 64]
    results = [mock.Mock(stdout="container-id"), mock.Mock(stdout=json.dumps([container])), mock.Mock(stdout=json.dumps([info]))]
    with mock.patch.object(producer.subprocess, "run", side_effect=results):
        with pytest.raises(producer.ProducerError, match="candidate image|source revision|registry digest"):
            producer.candidate_identity(manifest, ["unit-only-compose"])


def test_numeric_tolerance_is_absolute_and_cannot_hide_large_value_loss():
    assert not producer.equal(1e12, 1e12 + 1.0)
    assert not producer.equal(float("nan"), float("nan"))
    assert not producer.equal(float("inf"), float("inf"))


def test_expired_live_budget_refuses_new_sdk_mutations():
    sdk = object.__new__(producer.Sdk)
    sdk.process = mock.Mock()
    sdk.deadline = producer.time.monotonic() - 1
    with pytest.raises(producer.ProducerError, match="overall time bound"):
        sdk.call("StartGeoservicesImportAsync", baseUrl="http://localhost", candidate=True, arguments=[])
    sdk.process.stdin.write.assert_not_called()


def multitarget_runtime(tmp_path):
    import zipfile
    packages, output = tmp_path / "packages", tmp_path / "out"
    output.mkdir()
    assets = {"libraries": {}, "targets": {"net10.0": {}}}
    for name in ("Honua.Sdk.Admin", "Honua.Sdk.GeoServices"):
        key = name + "/1.2.3"
        path = packages / name.lower() / "1.2.3"
        path.mkdir(parents=True)
        (path / ".nupkg.metadata").write_text(json.dumps({"source": gate.PUBLIC_REGISTRIES[0]}))
        with zipfile.ZipFile(path / f"{name.lower()}.1.2.3.nupkg", "w") as archive:
            archive.writestr(f"lib/net8.0/{name}.dll", b"different net8 runtime")
            archive.writestr(f"lib/net10.0/{name}.dll", b"selected net10 runtime")
            archive.writestr(f"ref/net10.0/{name}.dll", b"different reference assembly")
        (output / f"{name}.dll").write_bytes(b"selected net10 runtime")
        assets["libraries"][key] = {"path": f"{name.lower()}/1.2.3", "type": "package"}
        assets["targets"]["net10.0"][key] = {"runtime": {f"lib/net10.0/{name}.dll": {}}}
    return assets, packages, output


def test_selected_tfm_runtime_matches_despite_other_tfm_and_reference_bytes(tmp_path):
    assets, packages, output = multitarget_runtime(tmp_path)
    matched = producer.verify_runtime_assets(assets, packages, output, "net10.0")
    assert len(matched) == 2
    assert all(item["asset"].startswith("lib/net10.0/") for item in matched)


def test_wrong_selected_tfm_bytes_are_rejected_even_if_another_tfm_matches(tmp_path):
    assets, packages, output = multitarget_runtime(tmp_path)
    (output / "Honua.Sdk.Admin.dll").write_bytes(b"different net8 runtime")
    with pytest.raises(producer.ProducerError, match="differs from selected runtime asset"):
        producer.verify_runtime_assets(assets, packages, output, "net10.0")


def test_missing_selected_runtime_asset_cannot_be_replaced_by_basename_match(tmp_path):
    assets, packages, output = multitarget_runtime(tmp_path)
    assets["targets"]["net10.0"]["Honua.Sdk.Admin/1.2.3"]["runtime"] = {"lib/net10.0/missing/Honua.Sdk.Admin.dll": {}}
    with pytest.raises(producer.ProducerError, match="selected runtime asset is absent"):
        producer.verify_runtime_assets(assets, packages, output, "net10.0")


def test_attribute_value_parity_does_not_borrow_geometry_tolerance():
    assert not producer.equal({"value": 1.0}, {"value": 1.000000001})
    assert producer.equal({"x": 1.0}, {"x": 1.000000001}, tolerance=1e-8)
    assert producer.equal({"x": 1}, {"x": 1.000000001}, tolerance=1e-8)
    assert not producer.equal(9007199254740993, 9007199254740992.0, tolerance=1e-8)


def test_geojson_pages_all_rows_beyond_source_transfer_cap_and_rejects_repeated_ids():
    snapshot = {"objectIdField": "gid", "ids": list(range(1, 252)), "metadata": {"fields": [
        {"name": "gid", "type": "esriFieldTypeOID"}, {"name": "value", "type": "esriFieldTypeBigInteger"}]}}
    class UnitSdk:
        def __init__(self, corrupt=False):
            self.batches = []
            self.corrupt = corrupt
        def call(self, operation, **kwargs):
            ids = kwargs["query"]["objectIds"]
            self.batches.append(ids)
            features = [{"id": identity, "properties": {"gid": identity, "value": 9007199254740993},
                         "geometry": {"type": "Point", "coordinates": [1.0, 2.0]}} for identity in ids]
            if self.corrupt and len(self.batches) == 2:
                features[-1] = features[0]
            return {"type": "FeatureCollection", "features": features}
    sdk = UnitSdk()
    rows = producer.geojson_snapshot(sdk, "https://source.example/FeatureServer", 0, snapshot)
    assert len(rows) == 251 and len(sdk.batches) == 2
    assert [len(batch) for batch in sdk.batches] == [200, 51]
    assert rows[-1]["properties"]["value"] == 9007199254740993
    with pytest.raises(producer.ProducerError, match="repeated or omitted"):
        producer.geojson_snapshot(UnitSdk(corrupt=True), "https://source.example/FeatureServer", 0, snapshot)
