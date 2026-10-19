"""Fault controls for live journey code; these substitutions never emit qualification."""
from copy import deepcopy
import json
from pathlib import Path
import sys
from unittest import mock

import pytest
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dotnet_import_fidelity as gate
import import_fidelity_journeys as journeys
import produce_import_fidelity as producer


@pytest.fixture
def score():
    manifest = yaml.safe_load((HERE.parents[1] / "platform-manifest.yaml").read_text())
    return producer.Scorecard(manifest, gate.load_requirements(), "https://github.com/honua-io/honua-release/actions/runs/1")


def snapshot():
    return {"objectIdField": "OBJECTID", "count": 2, "ids": [1, 2],
            "metadata": {"fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID"}],
                         "geometryType": "esriGeometryPoint"},
            "features": [{"attributes": {"OBJECTID": i}, "geometry": {"x": i, "y": i}} for i in (1, 2)]}


def test_missing_authored_input_keeps_every_requirement_and_specific_blocker(score):
    lane = journeys.Journeys(producer, score, mock.Mock(), [], "http://localhost", "unit")
    lane.run()
    assert score.receipt()["summary"] == {"denominator": 142, "passed": 0, "failed": 142, "skipped": 0, "notApplicable": 10}
    for role in journeys.ROLES:
        assert score.cells["fixture." + role]["verdict"] == "blocked"
        assert "authored source manifest" in score.cells["fixture." + role]["reason"]
    for key in ("journey.cancellation-recovery", "dependency.server-4420", "evidence.rollback-repoint"):
        assert score.cells[key]["verdict"] == "blocked"
        assert score.cells[key]["executed"] is False


@pytest.mark.parametrize("role", journeys.ROLES)
def test_plain_point_data_cannot_claim_richer_authored_coverage(role):
    value = snapshot()
    if role in {"points", "auth"}:
        journeys.validate_role(role, value)
    else:
        with pytest.raises(ValueError):
            journeys.validate_role(role, value)


def test_null_and_int64_role_requires_actual_edge_values():
    value = snapshot()
    value["metadata"]["fields"].append({"name": "observed", "type": "esriFieldTypeDate"})
    value["features"][0]["attributes"].update(empty="", nullable=None, precise=9007199254740993, observed=1700000000000)
    journeys.validate_role("nulls-int64-time", value)
    del value["features"][0]["attributes"]["precise"]
    with pytest.raises(ValueError, match="64-bit"):
        journeys.validate_role("nulls-int64-time", value)


def test_geometry_role_refuses_missing_null_and_wrong_type():
    value = snapshot()
    journeys.validate_role("points", value)
    value["features"][0].pop("geometry")
    with pytest.raises(ValueError, match="missing shapes"):
        journeys.validate_role("points", value)


def test_transfer_limit_role_requires_crossing_real_source_cap():
    value = snapshot()
    value["metadata"]["maxRecordCount"] = 2
    with pytest.raises(ValueError, match="transfer cap"):
        journeys.validate_role("pagination-limits", value)
    value["metadata"]["maxRecordCount"] = 1
    journeys.validate_role("pagination-limits", value)
    with pytest.raises(ValueError, match="2001"):
        journeys.validate_role("bounded-large-data", value)


def test_source_manifest_refuses_insecure_or_unpinned_inputs(tmp_path, monkeypatch):
    spec = {"schema": journeys.FIXTURE_SCHEMA, "fixtureRevision": journeys.fixture_revision(), "fixtures": {role: {
        "serviceUrl": "https://source.example/rest/services/authored/FeatureServer", "layerId": 1,
        "expectedSnapshotDigest": "sha256:" + "a" * 64} for role in journeys.ROLES}}
    monkeypatch.setenv("IMPORT_FIDELITY_AUTH_SOURCE_URL", spec["fixtures"]["auth"]["serviceUrl"])
    path = tmp_path / "source.json"
    path.write_text(json.dumps(spec))
    assert journeys.source_spec(path) == spec
    for fault in ({"serviceUrl": "http://source.example/FeatureServer"},
                  {"serviceUrl": "https://source.example/FeatureServer?token=secret"},
                  {"expectedSnapshotDigest": ""}, {"layerId": True}):
        broken = deepcopy(spec)
        broken["fixtures"]["points"].update(fault)
        path.write_text(json.dumps(broken))
        with pytest.raises(ValueError):
            journeys.source_spec(path)


def test_source_credentials_are_scoped_to_exact_configured_service():
    raw = mock.Mock()
    scoped = journeys.SourceSdk(raw, "https://source.example/auth/FeatureServer")
    scoped.call("query", baseUrl="https://source.example/auth/FeatureServer")
    assert raw.call.call_args.kwargs["sourceAuth"] is True
    scoped.call("query", baseUrl="https://source.example/other/FeatureServer")
    assert "sourceAuth" not in raw.call.call_args.kwargs
    scoped.call("query", baseUrl="https://source.example/auth/FeatureServer", candidate=True)
    assert "sourceAuth" not in raw.call.call_args.kwargs


def lane_with_points(score):
    lane = journeys.Journeys(producer, score, mock.Mock(), [], "http://localhost", "unit")
    lane.states["points"] = {"source": snapshot(), "target": snapshot(), "spec": {
        "serviceUrl": "https://source.example/FeatureServer", "layerId": 1},
        "request": {"tableName": "authored_points_unit", "serviceName": "points-unit"},
        "targetUrl": "http://localhost/rest/services/points-unit/FeatureServer", "targetLayer": 1}
    score.record("fixture.points", lambda: "unit input")
    return lane


@pytest.mark.parametrize("terminal", ["completed", "needsreview", "failed"])
def test_cancel_requested_is_not_enough_must_observe_cancelled_before_commit(score, terminal):
    lane = lane_with_points(score)
    lane.states["bounded-large-data"] = {"source": snapshot(), "spec": {"serviceUrl": "https://source.example/FeatureServer", "layerId": 2}}
    score.record("fixture.bounded-large-data", lambda: "unit input")
    with mock.patch.object(lane, "start", return_value="real-shaped-job"), mock.patch.object(lane, "fingerprint", return_value="2:before"), mock.patch.object(
            lane, "wait", return_value={"status": terminal}), mock.patch.object(lane, "lease_free", side_effect=[True, False]):
        lane.sdk.call.return_value = {"jobId": "real-shaped-job", "status": "InsertingFeatures", "featuresProcessed": 1}
        score.record("journey.cancellation-recovery", lane.cancellation)
    assert score.cells["journey.cancellation-recovery"]["verdict"] == "fail"


def test_missing_wire_commit_field_requires_worker_cessation_and_measured_prior_data(score):
    lane = lane_with_points(score)
    observations = []
    def fingerprint(table):
        observations.append("fingerprint")
        return "2:before"
    def release(table):
        observations.append("worker-lease-ended")
    with mock.patch.object(lane, "start", return_value="job"), mock.patch.object(lane, "fingerprint", side_effect=fingerprint), mock.patch.object(
            lane, "wait", return_value={"status": "Failed"}), mock.patch.object(lane, "wait_for_lease_release", side_effect=release), mock.patch.object(
            producer, "read_snapshot", return_value=lane.states["points"]["source"]):
        score.record("fixture.injected-failure", lane.injected_failure)
    assert score.cells["fixture.injected-failure"]["verdict"] == "pass"
    assert observations == ["fingerprint", "worker-lease-ended", "fingerprint"]


def test_failed_overwrite_must_preserve_every_prior_row(score):
    lane = lane_with_points(score)
    with mock.patch.object(lane, "start", return_value="job"), mock.patch.object(lane, "fingerprint", side_effect=["2:before", "1:lost"]), mock.patch.object(
            lane, "wait", return_value={"status": "Failed"}), mock.patch.object(lane, "wait_for_lease_release"):
        score.record("fixture.injected-failure", lane.injected_failure)
    assert score.cells["fixture.injected-failure"]["verdict"] == "fail"
    assert "retained every prior row" in score.cells["fixture.injected-failure"]["reason"]


def test_independent_vector_reader_measures_real_rows_and_rejects_loss(score):
    lane = lane_with_points(score)
    physical = [{"id": i, "geometry": {"type": "Point", "coordinates": [i, i]}} for i in (1, 2)]
    with mock.patch.object(producer, "sql", return_value=json.dumps(physical)) as sql:
        lane.independent_geometry()
        assert "ST_AsGeoJSON(ST_Transform" in sql.call_args.args[1]
        assert score.provenance["independentGeometry"]["measuredCount"] == 2
    physical[0]["geometry"]["coordinates"][0] += 1
    with mock.patch.object(producer, "sql", return_value=json.dumps(physical)):
        with pytest.raises(producer.ProducerError, match="independent PostGIS vector"):
            lane.independent_geometry()


def test_attachment_metadata_cannot_substitute_for_content_bytes(score):
    lane = lane_with_points(score)
    lane.sdk.call.side_effect = [[{"attachmentId": 9, "size": 8, "name": "actual.txt", "contentType": "text/plain"}],
                                 {"size": 7, "sha256": "a" * 64}]
    with pytest.raises(ValueError, match="content length"):
        lane.attachments("https://source.example/FeatureServer", 1, {"ids": [1]})


def test_vector_geometry_tolerance_never_applies_to_attributes(score):
    lane = lane_with_points(score)
    expected = [{"id": 1, "properties": {"value": 1.0}, "geometry": {"value": {"x": 1.0, "y": 2.0}}}]
    observed = deepcopy(expected)
    observed[0]["geometry"]["value"]["x"] += 1e-10
    lane.geojson_equal(expected, observed, "vector readback")
    observed[0]["properties"]["value"] += 1e-10
    with pytest.raises(producer.ProducerError, match="exact attributes"):
        lane.geojson_equal(expected, observed, "vector readback")


def test_missing_authored_fixture_cannot_qualify_client_repoint(score):
    lane = journeys.Journeys(producer, score, mock.Mock(), [], "http://localhost", "unit")
    score.record("evidence.rollback-repoint", lane.rollback)
    assert score.cells["evidence.rollback-repoint"]["verdict"] == "blocked"
    assert "rollbackRepoint" not in score.provenance
    assert score.receipt()["summary"]["denominator"] == 142


def test_unavailable_gdal_never_substitutes_baseline(score):
    lane = lane_with_points(score)
    with mock.patch.object(lane, "independent_geometry"), mock.patch.object(journeys.shutil, "which", return_value=None):
        score.record("dependency.server-4420", lane.dependency_evidence)
    assert score.cells["dependency.server-4420"]["verdict"] == "blocked"
    assert "ogr2ogr" in score.cells["dependency.server-4420"]["reason"]


@pytest.mark.parametrize("role", journeys.ROLES)
def test_authored_data_is_fixed_and_rejects_a_seeded_source_value_fault(role):
    features = journeys.fixture_features(role)
    value = {"features": deepcopy(features), "metadata": {"fields": json.loads(journeys.FIXTURES.read_text())["fieldDefinitions"]}}
    if role in {"domains-subtypes", "renderer-time-metadata", "pagination-limits", "bounded-large-data"}:
        # These additional metadata preconditions are independently covered by role checks.
        # Stop at the first known attribute fault, before any later metadata access.
        value["features"][0]["attributes"]["label"] = "changed-source-data"
        with pytest.raises(producer.ProducerError, match="fixed authored values"):
            journeys.validate_authored_data(role, value, producer.require_equal)
    else:
        journeys.validate_authored_data(role, value, producer.require_equal)
        value["features"][0]["attributes"]["label"] = "changed-source-data"
        with pytest.raises(producer.ProducerError, match="fixed authored values"):
            journeys.validate_authored_data(role, value, producer.require_equal)


def test_fixture_export_writes_real_bytes_and_full_bounded_dataset_without_a_verdict(tmp_path):
    import export_import_fixtures
    destination = tmp_path / "authored"
    export_import_fixtures.export(destination)
    document = json.loads((destination / "bounded-large-data.json").read_text())
    assert len(document["features"]) == 2501
    assert document["features"][0]["attributes"]["OBJECTID"] == 1
    assert document["features"][-1]["attributes"]["OBJECTID"] == 2501
    assert document["fixtureRevision"] == journeys.fixture_revision()
    assert "verdict" not in document
    definitions = json.loads(journeys.FIXTURES.read_text())["roles"]["attachments"]["attachments"]
    import hashlib
    for item in definitions:
        raw = (destination / "attachment-bytes" / item["name"]).read_bytes()
        assert len(raw) == item["size"] and hashlib.sha256(raw).hexdigest() == item["sha256"]
    with pytest.raises(ValueError, match="already exist"):
        export_import_fixtures.export(destination)


@pytest.mark.parametrize("fault", ["missing", "empty", "truncated"])
def test_gdal_success_exit_cannot_hide_missing_or_truncated_measured_output(score, tmp_path, fault):
    lane = lane_with_points(score)
    score.capture_directory = tmp_path
    features = [{"id": i, "properties": {"objectid": i}, "geometry": {"present": True, "value": {"type": "Point", "coordinates": [i, i]}}} for i in (1, 2)]
    def reader(command, **kwargs):
        output = tmp_path / "gdal-readback.geojson"
        if fault == "empty":
            output.write_text("")
        elif fault == "truncated":
            output.write_text(json.dumps({"type": "FeatureCollection", "features": []}))
        return mock.Mock(returncode=0)
    with mock.patch.object(lane, "independent_geometry"), mock.patch.object(journeys.shutil, "which", return_value="/usr/bin/ogr2ogr"), mock.patch.object(
            producer, "geojson_snapshot", return_value=features), mock.patch.object(journeys.subprocess, "run", side_effect=reader):
        score.record("dependency.server-4420", lane.dependency_evidence)
    cell = score.cells["dependency.server-4420"]
    assert cell["verdict"] == "fail" and cell["executed"] is True
    assert "absent, empty or failed" in cell["reason"] or "every imported feature" in cell["reason"]


def cancellation_lane(score):
    lane = lane_with_points(score)
    lane.states["bounded-large-data"] = {"source": snapshot(), "spec": {"serviceUrl": "https://source.example/FeatureServer", "layerId": 2}}
    score.record("fixture.bounded-large-data", lambda: "unit input")
    lane.sdk.call.side_effect = [
        {"jobId": "job", "status": "InsertingFeatures", "featuresProcessed": 1},
        {"jobId": "job", "message": "Cancellation requested"},
        {"jobId": "job", "status": "Cancelled"}]
    return lane


def test_cancel_endpoint_acknowledgement_cannot_pass_before_worker_lease_ends(score):
    lane = cancellation_lane(score)
    with mock.patch.object(lane, "start", return_value="job"), mock.patch.object(lane, "fingerprint", return_value="2:prior") as fingerprint, mock.patch.object(
            lane, "lease_free", side_effect=[True, False]), mock.patch.object(lane, "wait", return_value={"status": "Cancelled"}), mock.patch.object(
            lane, "wait_for_lease_release", side_effect=ValueError("cancel acknowledgement arrived before worker lease release")), mock.patch.object(lane, "complete") as recover:
        score.record("journey.cancellation-recovery", lane.cancellation)
    assert score.cells["journey.cancellation-recovery"]["verdict"] == "fail"
    assert fingerprint.call_count == 1
    recover.assert_not_called()


def test_postcommit_cancel_mutation_fails_even_after_worker_lease_ends(score):
    lane = cancellation_lane(score)
    with mock.patch.object(lane, "start", return_value="job"), mock.patch.object(lane, "fingerprint", side_effect=["2:prior", "2501:committed"]), mock.patch.object(
            lane, "lease_free", side_effect=[True, False]), mock.patch.object(lane, "wait", return_value={"status": "Cancelled"}), mock.patch.object(
            lane, "wait_for_lease_release"), mock.patch.object(lane, "complete") as recover:
        score.record("journey.cancellation-recovery", lane.cancellation)
    cell = score.cells["journey.cancellation-recovery"]
    assert cell["verdict"] == "fail" and "after worker cessation" in cell["reason"]
    recover.assert_not_called()


def test_wrong_import_lease_key_cannot_qualify_from_processing_status_alone(score):
    lane = cancellation_lane(score)
    with mock.patch.object(lane, "start", return_value="job"), mock.patch.object(lane, "fingerprint", return_value="2:prior"), mock.patch.object(
            lane, "lease_free", return_value=True), mock.patch.object(journeys.time, "monotonic", side_effect=[0, 31]):
        score.record("journey.cancellation-recovery", lane.cancellation)
    cell = score.cells["journey.cancellation-recovery"]
    assert cell["verdict"] == "fail" and "held its lease" in cell["reason"]
    assert lane.sdk.call.call_count == 1


def test_worker_lease_probe_is_bounded_and_unknown_results_fail(score):
    lane = lane_with_points(score)
    with mock.patch.object(lane, "lease_free", return_value=False), mock.patch.object(journeys.time, "monotonic", side_effect=[0, 31]):
        with pytest.raises(ValueError, match="before worker lease release"):
            lane.wait_for_lease_release("authored_points_unit")
    with mock.patch.object(producer, "sql", return_value="unknown"):
        with pytest.raises(ValueError, match="unknown result"):
            lane.lease_free("authored_points_unit")


class ConfiguredConsumerControl:
    """Independent command-boundary fault double; never release evidence."""
    def __init__(self, fault=None):
        self.fault, self.commands = fault, []
        self.configuration = None

    def call(self, operation, **arguments):
        self.commands.append((operation, deepcopy(arguments)))
        if operation == "configure-readback":
            self.configuration = {**{key: arguments[key] for key in ("baseUrl", "layerId", "candidate", "sourceAuth")},
                                  "configurationId": "a" * 32, "revision": 1}
            self.original = deepcopy(self.configuration)
            return deepcopy(self.configuration)
        if operation == "readback-status":
            if self.fault == "cleanup-unavailable":
                raise producer.ProducerError("configuration state unavailable")
            return deepcopy(self.configuration)
        if operation == "repoint-readback":
            if self.fault != "ignored-switch":
                self.configuration.update({key: arguments[key] for key in ("baseUrl", "layerId", "candidate", "sourceAuth")})
                self.configuration["revision"] += 1
            if self.fault == "lost-repoint-ack":
                raise producer.ProducerError("configuration acknowledgement lost")
            return {**self.configuration, "configurationId": "bad"} if self.fault == "corrupt-repoint-ack" else deepcopy(self.configuration)
        if operation == "rollback-readback":
            self.configuration = {**self.original, "revision": 3}
            return {**self.configuration, "revision": 2} if self.fault == "corrupt-restore-ack" else deepcopy(self.configuration)
        assert operation == "configured-read"
        path = self.configuration["baseUrl"] + "/" + str(self.configuration["layerId"])
        if arguments["readOperation"] != "layer":
            path += "/query"
        return {"configuration": deepcopy(self.configuration), "observedRequests": [] if self.fault == "no-request" else [
                    "https://other.example/FeatureServer/1/query" if self.fault == "wrong-route" else path], "value": {"actual": True}}


@pytest.mark.parametrize("fault", ["ignored-switch", "no-request", "wrong-route"])
def test_configured_sdk_refuses_noop_switch_or_unobserved_actual_route(fault):
    child = ConfiguredConsumerControl(fault)
    client = journeys.ConfiguredSdk(child, "https://source.example/FeatureServer", 1)
    with pytest.raises(ValueError, match="application configuration|actual SDK request"):
        client.repoint("http://localhost/rest/services/imported/FeatureServer", 2)
        client.call("query", baseUrl="http://localhost/rest/services/imported/FeatureServer", layerId=2, candidate=True)


def test_configured_reads_never_send_an_endpoint_override_and_restore_same_identity():
    child = ConfiguredConsumerControl()
    client = journeys.ConfiguredSdk(child, "https://source.example/FeatureServer", 1)
    client.call("layer", baseUrl="https://source.example/FeatureServer", layerId=1)
    client.repoint("http://localhost/rest/services/imported/FeatureServer", 2)
    client.call("query", baseUrl="http://localhost/rest/services/imported/FeatureServer", layerId=2, candidate=True, query={"where": "1=1"})
    client.restore()
    client.call("layer", baseUrl="https://source.example/FeatureServer", layerId=1)
    assert client.configuration_id == "a" * 32 and client.configuration["revision"] == 3
    reads = [arguments for operation, arguments in child.commands if operation == "configured-read"]
    assert len(reads) == 3 and all(not {"baseUrl", "layerId", "candidate", "sourceAuth"} & arguments.keys() for arguments in reads)
    rollback = next(arguments for operation, arguments in child.commands if operation == "rollback-readback")
    assert set(rollback) == {"configurationId", "revision"}


@pytest.mark.parametrize("fault", [None, "cached-source", "wrong-restored-values", "fixture-restore-failure", "corrupt-repoint-ack", "lost-repoint-ack", "corrupt-restore-ack", "cleanup-unavailable"])
def test_real_rollback_orchestration_requires_discriminating_readback_and_cleanup(score, fault):
    lane = lane_with_points(score)
    for feature in lane.states["points"]["source"]["features"]:
        feature["attributes"]["label"] = "original_" + str(feature["attributes"]["OBJECTID"])
    lane.states["points"]["target"] = deepcopy(lane.states["points"]["source"])
    original = deepcopy(lane.states["points"]["source"])
    marked = deepcopy(original)
    for feature in marked["features"]:
        feature["attributes"]["label"] = "repoint_unit"
    reads = [deepcopy(original), deepcopy(original if fault == "cached-source" else marked),
             deepcopy(marked if fault == "wrong-restored-values" else original), deepcopy(original)]
    child = ConfiguredConsumerControl(fault)
    lane.sdk = child
    with mock.patch.object(producer, "read_snapshot", side_effect=reads), mock.patch.object(producer, "sql") as mutate, mock.patch.object(
            lane, "complete", return_value=deepcopy(lane.states["points"])) as restore, mock.patch.object(lane, "parity") as parity:
        if fault == "fixture-restore-failure":
            restore.side_effect = producer.ProducerError("fixture restoration failed")
        score.record("evidence.rollback-repoint", lane.rollback)
    assert mutate.call_count == 1 and restore.call_count == 1
    if fault is None:
        assert score.cells["evidence.rollback-repoint"]["verdict"] == "pass"
        assert score.provenance["rollbackRepoint"]["originalDigest"] == score.provenance["rollbackRepoint"]["restoredDigest"]
        assert score.provenance["rollbackRepoint"]["switchedDigest"] != score.provenance["rollbackRepoint"]["originalDigest"]
        parity.assert_called_once()
    else:
        assert score.cells["evidence.rollback-repoint"]["verdict"] == "fail"
        assert "rollbackRepoint" not in score.provenance
    if fault == "cleanup-unavailable":
        assert score.provenance["rollbackRepointCleanup"]["configurationStatus"] == "unconfirmed"
    else:
        assert score.provenance["rollbackRepointCleanup"]["configurationStatus"] == "confirmed-original"
    assert score.provenance["rollbackRepointCleanup"]["fixtureRestoration"] == ("unconfirmed" if fault == "fixture-restore-failure" else "confirmed")
    assert sum(operation == "rollback-readback" for operation, _ in child.commands) == 1
    assert child.configuration["revision"] == 3


def test_public_process_configuration_controls_are_in_the_existing_pr_lane():
    text = (HERE.parents[1] / ".github/workflows/installed-client-certification.yml").read_text()
    command = "python certification/installed-clients/check_import_client_readback.py"
    assert command in text
    assert text.index(command) < text.index("# Qualification is explicit")
    controls = (HERE / "check_import_client_readback.py").read_text()
    assert "producer.install_consumer" in controls and "producer.Sdk" in controls
    assert '"qualification": False' in controls
    assert "score.receipt" not in controls and "sourceBuilt" not in controls
