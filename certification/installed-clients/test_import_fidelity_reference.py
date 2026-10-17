"""Reference authentication controls; fixture data is never a live native receipt."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from unittest import mock

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import import_fidelity_reference as reference


def attestation():
    return [{"verificationResult": {"statement": {"subject": [{"digest": {"sha256": "a" * 64}}],
        "predicateType": reference.PREDICATE, "predicate": {"buildDefinition": {
            "resolvedDependencies": [{"digest": {"gitCommit": "b" * 40}}]}}},
        "signature": {"certificate": {
            "sourceRepositoryURI": "https://github.com/" + reference.REPOSITORY,
            "sourceRepositoryDigest": "b" * 40, "sourceRepositoryRef": "refs/heads/trunk",
            "buildSignerURI": f"https://github.com/{reference.REPOSITORY}/{reference.WORKFLOW}@refs/heads/trunk",
            "runInvocationURI": f"https://github.com/{reference.REPOSITORY}/actions/runs/123/attempts/2"}}}}]


def test_verified_native_content_is_bound_to_exact_bytes_source_workflow_and_run():
    reference.bind_verified(attestation(), "a" * 64, "b" * 40, 123, 2)


@pytest.mark.parametrize("fault", ["subject", "predicate", "repository", "source", "ref", "signer", "run", "attempt", "resolved-source", "missing"])
def test_wrong_native_identity_never_qualifies(fault):
    verified = attestation()
    result = verified[0]["verificationResult"]
    certificate = result["signature"]["certificate"]
    if fault == "subject":
        result["statement"]["subject"][0]["digest"]["sha256"] = "0" * 64
    elif fault == "predicate":
        result["statement"]["predicateType"] = "other"
    elif fault == "resolved-source":
        result["statement"]["predicate"]["buildDefinition"]["resolvedDependencies"][0]["digest"]["gitCommit"] = "0" * 40
    elif fault == "missing":
        verified = []
    else:
        key = {"repository": "sourceRepositoryURI", "source": "sourceRepositoryDigest", "ref": "sourceRepositoryRef",
               "signer": "buildSignerURI", "run": "runInvocationURI", "attempt": "runInvocationURI"}[fault]
        certificate[key] = "untrusted"
    with pytest.raises(reference.ReferenceError):
        reference.bind_verified(verified, "a" * 64, "b" * 40, 123, 2)


def response_file(tmp_path):
    request = {"challenge": "fresh-not-an-old-receipt"}
    response = {"schema": reference.RESPONSE_SCHEMA, "requestDigest": reference.journeys.digest(request),
                "producer": {"sourceSha": "b" * 40, "runId": 123, "runAttempt": 2}}
    path = tmp_path / "native.json"
    path.write_text(json.dumps(response))
    return path, request


def api_values():
    return [{"full_name": reference.REPOSITORY, "default_branch": "trunk"},
            {"protected": True, "commit": {"sha": "c" * 40}},
            {"status": "ahead", "base_commit": {"sha": "b" * 40}, "merge_base_commit": {"sha": "b" * 40}},
            {"id": 123, "run_attempt": 2, "head_sha": "b" * 40, "head_branch": "trunk", "path": reference.WORKFLOW,
             "event": "workflow_dispatch", "status": "completed", "conclusion": "success",
             "repository": {"full_name": reference.REPOSITORY}}]


@pytest.mark.parametrize("index,change", [(0, {"full_name": "external/repository"}), (1, {"protected": False}),
    (2, {"status": "diverged"}), (2, {"merge_base_commit": {"sha": "0" * 40}}),
    (3, {"head_sha": "0" * 40}), (3, {"head_branch": "feature"}), (3, {"conclusion": "failure"}),
    (3, {"event": "pull_request"}), (3, {"status": "in_progress"}), (3, {"run_attempt": 1})])
def test_live_api_metadata_refuses_untrusted_or_incomplete_native_run(tmp_path, index, change):
    path, request = response_file(tmp_path)
    values = api_values()
    values[index].update(change)
    with mock.patch.object(reference, "api", side_effect=values), mock.patch.object(reference, "command_json") as verifier:
        with pytest.raises(reference.ReferenceError):
            reference.authenticate(path, request)
        verifier.assert_not_called()


def test_successful_api_metadata_still_requires_actual_cryptographic_verification(tmp_path):
    path, request = response_file(tmp_path)
    verified = attestation()
    verified[0]["verificationResult"]["statement"]["subject"][0]["digest"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with mock.patch.object(reference, "api", side_effect=api_values()), mock.patch.object(reference, "command_json", return_value=verified) as verifier:
        result = reference.authenticate(path, request)
        assert result["authentication"]["runAttempt"] == 2
        command = verifier.call_args.args[0]
        assert command[:3] == ["gh", "attestation", "verify"]
        assert "--source-ref" in command and "--source-digest" in command


def test_stale_request_refuses_before_any_network_or_attestation(tmp_path):
    path, request = response_file(tmp_path)
    with mock.patch.object(reference, "api") as api:
        with pytest.raises(reference.ReferenceError, match="stale"):
            reference.authenticate(path, {"challenge": "a-different-run"})
        api.assert_not_called()


def test_native_http_auth_failure_is_not_a_verdict():
    with mock.patch.object(reference.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="secret response", stderr="token")):
        with pytest.raises(reference.ReferenceError) as error:
            reference.api("repos/private/inaccessible")
    assert "secret" not in str(error.value) and "token" not in str(error.value)


def test_raw_pass_verdict_cannot_replace_native_observations():
    with pytest.raises(reference.ReferenceError, match="omits"):
        reference.crosscheck("honua-and-esri-clients", {"status": "pass"}, {"fixtures": {"points": {}}}, {}, lambda *args: None)


def crosscheck_fixture():
    snapshot = {"objectIdField": "oid", "count": 2, "metadata": {"fields": [{"name": "oid", "type": "esriFieldTypeOID"}]},
                "features": [{"attributes": {"oid": 1, "value": 9007199254740993}, "geometry": {"x": 1.0, "y": 2.0}},
                             {"attributes": {"oid": 2, "value": None}, "geometry": {"x": 3.0, "y": 4.0}}]}
    state = {"source": deepcopy(snapshot), "target": deepcopy(snapshot)}
    identity = {"sourceDigest": "sha256:" + "a" * 64, "targetDigest": "sha256:" + "b" * 64}
    observation = {"runtimeVersion": "actual-runtime-version-recorded-by-owner", "methods": ["native-reader"], "live": True}
    for side in ("source", "target"):
        observation[side] = {"count": 2, "rows": reference.journeys.rows(snapshot),
                             "shapes": reference.journeys.shapes(snapshot), "schema": reference.journeys.schema(snapshot)}
    return ({"fixtures": {"points": {"identity": identity, "observations": {"licensed-desktop": deepcopy(observation), "open-desktop": deepcopy(observation)}}}},
            {"fixtures": {"points": identity}}, {"points": state})


@pytest.mark.parametrize("check,fault", [("counts-values", "int64"), ("counts-values", "null"),
    ("geometry-tolerances", "coordinate"), ("schema-metadata", "field"), ("honua-and-esri-clients", "int64")])
def test_authenticated_native_identity_does_not_hide_measured_data_loss(check, fault):
    import produce_import_fidelity as producer
    response, request, states = crosscheck_fixture()
    reference.crosscheck(check, response, request, states, producer.require_equal)
    target = response["fixtures"]["points"]["observations"]["licensed-desktop"]["target"]
    if fault == "int64":
        target["rows"][0]["value"] = 9007199254740992
    elif fault == "null":
        target["rows"][1]["value"] = ""
    elif fault == "coordinate":
        target["shapes"][0][1]["value"]["x"] += 0.1
    elif fault == "field":
        target["schema"]["fields"] = []
    with pytest.raises(producer.ProducerError):
        reference.crosscheck(check, response, request, states, producer.require_equal)
