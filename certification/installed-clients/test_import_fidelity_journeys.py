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
            lane, "wait", return_value={"status": terminal, "dataCommitted": False}):
        lane.sdk.call.return_value = {"status": "TransferringFeatures"}
        score.record("journey.cancellation-recovery", lane.cancellation)
    assert score.cells["journey.cancellation-recovery"]["verdict"] == "fail"


def test_missing_published_progress_field_cannot_mean_false(score):
    lane = lane_with_points(score)
    with mock.patch.object(lane, "start", return_value="job"), mock.patch.object(lane, "fingerprint", return_value="2:before"), mock.patch.object(
            lane, "wait", return_value={"status": "Failed"}):
        score.record("fixture.injected-failure", lane.injected_failure)
    assert score.cells["fixture.injected-failure"]["verdict"] == "blocked"
    assert "DataCommitted" in score.cells["fixture.injected-failure"]["reason"]


def test_failed_overwrite_must_preserve_every_prior_row(score):
    lane = lane_with_points(score)
    with mock.patch.object(lane, "start", return_value="job"), mock.patch.object(lane, "fingerprint", side_effect=["2:before", "1:lost"]), mock.patch.object(
            lane, "wait", return_value={"status": "Failed", "dataCommitted": False}):
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


def test_client_repoint_must_read_distinct_forward_data_then_complete_prior_publication(score):
    lane = lane_with_points(score)
    prior = lane.states["points"]["source"]
    subset = deepcopy(prior)
    subset.update(count=1, ids=[1], features=prior["features"][:1])
    target = {"targetUrl": "http://localhost/rest/services/repoint-unit/FeatureServer", "targetLayer": 2}
    # The final read is deliberately still on the one-row forward publication.
    # A count-only/no-op repoint must fail the complete rollback data assertion.
    with mock.patch.object(producer, "read_snapshot", side_effect=[subset, subset, subset]), mock.patch.object(
            lane, "complete", return_value=target), mock.patch.object(lane, "fingerprint", return_value="2:preserved"):
        score.record("evidence.rollback-repoint", lane.rollback)
    assert score.cells["evidence.rollback-repoint"]["verdict"] == "fail"
    assert "rollback attributes" in score.cells["evidence.rollback-repoint"]["reason"]


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
