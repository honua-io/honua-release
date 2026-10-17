"""Synthetic controls only; these fixtures are never deployment evidence."""
from __future__ import annotations

import copy
import hashlib

import pytest
import yaml

import deployment_qualification as dq
from validate_platform import MANIFEST_PATH, qualification_candidate_digest
import validate_platform as vp

ARTIFACT = {"id": 1234, "sha256": "sha256:" + "c" * 64,
            "inputsPath": "deployment-execution-inputs.json",
            "rawReceiptPath": "lambda-preview-receipt.json"}


@pytest.fixture
def manifest():
    return yaml.safe_load(MANIFEST_PATH.read_text())


@pytest.fixture
def deployment():
    return {"target": "awsLambda", "component": "honua-server", "architecture": "x86_64",
            "producer": {"repository": "honua-io/honua-server", "sourceRevision": "a" * 40,
                         "workflow": ".github/workflows/lambda-preview-certification.yml"},
            "configuration": {"memoryMb": 2048, "timeoutSeconds": 30,
                              "fixtureSha256": "sha256:" + "f" * 64,
                              "databaseSecretVersion": "synthetic-immutable-secret-version"}}


@pytest.mark.parametrize("path", dq.MANIFEST_OUTPUT_PATHS)
def test_bookkeeping_output_does_not_invalidate_execution_inputs(manifest, deployment, path):
    before = dq.input_binding(manifest, deployment)
    legacy_before = qualification_candidate_digest(manifest)
    parent = manifest
    for key in path[:-1]:
        parent = parent.setdefault(key, {})
    parent[path[-1]] = "changed-bookkeeping-output"
    assert dq.input_binding(manifest, deployment) == before
    # The original contract is not redefined by the new versioned binding.
    assert qualification_candidate_digest(manifest) != legacy_before


@pytest.mark.parametrize("path", [
    ("components", "honua-server", "sha"),
    ("components", "honua-server", "digest"),
    ("components", "honua-server", "awsLambdaDigest"),
    ("components", "honua-server", "awsLambdaEcrDigest"),
    ("components", "honua-server", "platformDigests", "linux/amd64"),
    ("components", "honua-iac", "sha"),
    ("components", "honua-helm", "sha"),
    ("clientArtifacts", "new-runtime", "integrity"),
    ("protocolCertification", "serverCertificationProducerSha"),
    ("protocolCertification", "ledger", "repository"),
    ("protocolCertification", "ledger", "path"),
    ("protocolCertification", "ledger", "newExecutionField"),
    ("platformLockEvidence", "fixtures"),
    ("futureUnknownConfiguration",),
])
def test_every_execution_input_including_unknown_fields_invalidates_binding(manifest, deployment, path):
    before = dq.input_binding(manifest, deployment)
    parent = manifest
    for key in path[:-1]:
        parent = parent.setdefault(key, {})
    parent[path[-1]] = "changed-execution-input"
    assert dq.input_binding(manifest, deployment) != before


def test_full_producer_configuration_bound_without_mutation(manifest, deployment):
    original = copy.deepcopy((manifest, deployment))
    before = dq.input_binding(manifest, deployment)
    assert (manifest, deployment) == original
    deployment["configuration"]["futureRuntimeSetting"] = {"enabled": True, "values": [1, 2]}
    assert dq.input_binding(manifest, deployment) != before
    deployment = copy.deepcopy(original[1])
    deployment["producer"]["sourceRevision"] = "b" * 40
    assert dq.input_binding(manifest, deployment) != before


@pytest.mark.parametrize("field,value", [("configuration", {}), ("configuration", None),
                                        ("producer", {}), ("architecture", "arm64"),
                                        ("target", "awsEcs"), ("component", "honua-helm")])
def test_missing_or_wrong_execution_profile_refused(manifest, deployment, field, value):
    deployment[field] = value
    with pytest.raises(dq.QualificationError):
        dq.input_binding(manifest, deployment)


def test_nonfinite_configuration_cannot_be_hashed(manifest, deployment):
    deployment["configuration"]["bad"] = float("nan")
    with pytest.raises(ValueError):
        dq.input_binding(manifest, deployment)


@pytest.fixture
def raw_lambda(manifest):
    server = manifest["components"]["honua-server"]
    return {"schema": "honua.lambda-preview-certification/v1", "result": "pass",
            "serverRevision": server["sha"],
            "artifact": {"sourceDigest": server["awsLambdaDigest"],
                         "sourcePlatformDigest": server["awsLambdaDigest"],
                         "sourceConfigDigest": "sha256:" + "a" * 64,
                         "sourceRootfsFingerprint": "sha256:" + "b" * 64,
                         "ecrDigest": server["awsLambdaEcrDigest"],
                         "configDigestPreserved": True, "rootfsPreserved": True,
                         "runtimeAdapterVerified": True},
            "deployment": {"architecture": "x86_64"},
            "serving": {"result": "pass", "candidateDigest": server["awsLambdaDigest"],
                        "deployed": {"result": "pass",
                                     "migrations": {"status": "succeeded", "pendingScripts": 0,
                                                    "upgradeRequired": False},
                                     "fixture": {"name": "client-compat-v1", "expectedRows": 10,
                                                 "sha256": "sha256:" + "f" * 64,
                                                 "actualRows": 10, "namesVerified": True},
                                     "write": {"createdRows": 1, "readBackRows": 1, "deletedRows": 1,
                                               "remainingRows": 0, "distinctWriteUrl": True},
                                     "authorization": {"anonymousStatus": 401, "actualStatus": 403,
                                                       "expectedStatus": 403, "records": 0}}},
            "verification": {"coldStartInitDurationMs": 12.5, "coldStartEnvironmentForced": True,
                             "httpStatus": 200, "responseVerified": True, "cloudWatchLogsVerified": True},
            "teardown": {"functionDeleted": True, "logGroupDeleted": True},
            "runUrl": "https://github.com/honua-io/honua-server/actions/runs/123"}


@pytest.fixture
def producer_run(deployment):
    return {**deployment["producer"], "runId": "123", "runAttempt": 1,
            "runUrl": "https://github.com/honua-io/honua-server/actions/runs/123"}


@pytest.fixture
def bound_raw(manifest, deployment, raw_lambda, producer_run):
    return {**raw_lambda, "executionInputs": dq.input_binding(manifest, deployment),
            "producer": producer_run}


def run_metadata(producer):
    return {"repository": {"full_name": producer["repository"]}, "id": int(producer["runId"]),
            "run_attempt": producer["runAttempt"], "head_sha": producer["sourceRevision"],
            "path": producer["workflow"], "html_url": producer["runUrl"],
            "status": "completed", "conclusion": "success",
            "event": "workflow_dispatch", "head_branch": "trunk"}


def test_actual_precut_fact_shape_is_scoped_not_ga(manifest, deployment, bound_raw, producer_run):
    receipt = dq.record(dq.execution_inputs(manifest, deployment), bound_raw, producer_run, ARTIFACT)
    dq.verify_receipt(receipt, manifest, deployment, run_metadata(producer_run),
                      lambda *_: (dq.execution_inputs(manifest, deployment), bound_raw))
    assert receipt["scope"]["fullGaQualified"] is False
    assert receipt["scope"]["completedBillItems"] == [1, 2]
    assert receipt["phase"] == "candidate-precut"


def test_historical_receipt_cannot_be_rewrapped(manifest, deployment, raw_lambda, producer_run):
    with pytest.raises(dq.QualificationError, match="producer identity"):
        dq.record(dq.execution_inputs(manifest, deployment), raw_lambda, producer_run, ARTIFACT)
    raw_lambda["producer"] = producer_run
    with pytest.raises(dq.QualificationError, match="historical receipts"):
        dq.record(dq.execution_inputs(manifest, deployment), raw_lambda, producer_run, ARTIFACT)


@pytest.mark.parametrize("path,value", [
    (("serverRevision",), "b" * 40),
    (("result",), "failed"),
    (("artifact", "sourceDigest"), "sha256:" + "b" * 64),
    (("artifact", "ecrDigest"), "sha256:" + "b" * 64),
    (("artifact", "configDigestPreserved"), False),
    (("deployment", "architecture"), "arm64"),
    (("serving", "deployed", "fixture", "actualRows"), 9),
    (("serving", "deployed", "write", "readBackRows"), True),
    (("serving", "deployed", "authorization", "actualStatus"), 200),
    (("verification", "coldStartInitDurationMs"), float("nan")),
    (("verification", "coldStartInitDurationMs"), 0),
    (("teardown", "functionDeleted"), False),
])
def test_actual_precut_failures_are_not_qualified(manifest, deployment, bound_raw, producer_run, path, value):
    parent = bound_raw
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    with pytest.raises(dq.QualificationError):
        dq.record(dq.execution_inputs(manifest, deployment), bound_raw, producer_run, ARTIFACT)


@pytest.mark.parametrize("field,value", [("head_sha", "b" * 40), ("run_attempt", 2),
                                        ("path", ".github/workflows/other.yml"),
                                        ("conclusion", "failure"), ("status", "in_progress"),
                                        ("event", "pull_request"), ("head_branch", "unreviewed")])
def test_wrong_producer_api_metadata_refused(manifest, deployment, bound_raw, producer_run, field, value):
    receipt = dq.record(dq.execution_inputs(manifest, deployment), bound_raw, producer_run, ARTIFACT)
    metadata = run_metadata(producer_run)
    metadata[field] = value
    with pytest.raises(dq.QualificationError, match="Actions provenance"):
        dq.verify_receipt(receipt, manifest, deployment, metadata)


def test_after_run_configuration_or_unknown_manifest_drift_refused(manifest, deployment, bound_raw, producer_run):
    receipt = dq.record(dq.execution_inputs(manifest, deployment), bound_raw, producer_run, ARTIFACT)
    deployment["configuration"]["memoryMb"] = 4096
    with pytest.raises(dq.QualificationError, match="inputs before execution"):
        dq.verify_receipt(receipt, manifest, deployment, run_metadata(producer_run))


def qualified_bundle(manifest, deployment, raw_lambda, producer_run):
    envelope = vp.LAMBDA_ENVELOPE_PATH.read_bytes()
    deployment["operatingEnvelope"] = {"path": dq.ENVELOPE_PATH,
                                      "sha256": "sha256:" + hashlib.sha256(envelope).hexdigest()}
    inputs = dq.execution_inputs(manifest, deployment)
    raw = {**raw_lambda, "executionInputs": dq.input_binding(manifest, deployment), "producer": producer_run}
    receipt = dq.record(inputs, raw, producer_run, ARTIFACT)
    bundle = {"schema": dq.BUNDLE_SCHEMA, "records": {"awsLambda": {"inputs": inputs, "receipt": receipt}}}
    return bundle, envelope


def test_candidate_phase_preserves_pending_and_does_not_qualify_ga_or_eks(manifest, deployment, raw_lambda, producer_run):
    bundle, envelope = qualified_bundle(manifest, deployment, raw_lambda, producer_run)
    proof = dq.verify_bundle(bundle, manifest, envelope, lambda _: run_metadata(producer_run),
                             lambda *_: (bundle["records"]["awsLambda"]["inputs"],
                                         bundle["records"]["awsLambda"]["receipt"]["rawReceipt"]))
    matrix = vp._load_yaml(vp.MATRIX_PATH)
    original = copy.deepcopy(matrix)
    candidate = vp.Findings()
    vp.check_deploy_qualification(manifest, matrix, candidate, exact_candidate=True,
                                 deployment_phase="candidate-precut", deployment_qualification=proof)
    assert candidate.ok, candidate.errors
    eks = matrix["deploy"]["honua-helm"]["awsEks"]["architectures"]["x86_64"]
    assert eks["status"] == "preview" and eks["qualification"] == "pending"
    assert "qualificationReceipt" not in eks
    ga = vp.Findings()
    vp.check_deploy_qualification(manifest, matrix, ga, exact_candidate=True,
                                 deployment_qualification=proof)
    assert len(ga.errors) == 1 and "awsLambda" in ga.errors[0]
    assert matrix == original


def test_pre_cut_phase_without_actual_proof_is_not_a_skip(manifest):
    findings = vp.Findings()
    vp.check_deploy_qualification(manifest, vp._load_yaml(vp.MATRIX_PATH), findings,
                                 exact_candidate=True, deployment_phase="candidate-precut")
    assert any("before-execution Lambda proof is missing" in error for error in findings.errors)


def test_changed_operating_envelope_refuses_prior_qualification(manifest, deployment, raw_lambda, producer_run):
    bundle, envelope = qualified_bundle(manifest, deployment, raw_lambda, producer_run)
    with pytest.raises(dq.QualificationError, match="operating envelope"):
        dq.verify_bundle(bundle, manifest, envelope + b"changed limits", lambda _: run_metadata(producer_run),
                         lambda *_: (bundle["records"]["awsLambda"]["inputs"],
                                     bundle["records"]["awsLambda"]["receipt"]["rawReceipt"]))


def test_changed_unknown_input_refuses_even_verified_bundle(manifest, deployment, raw_lambda, producer_run):
    bundle, envelope = qualified_bundle(manifest, deployment, raw_lambda, producer_run)
    manifest["unknownRuntimePolicy"] = {"new": True}
    with pytest.raises(dq.QualificationError, match="current candidate"):
        dq.verify_bundle(bundle, manifest, envelope, lambda _: run_metadata(producer_run),
                             lambda *_: (bundle["records"]["awsLambda"]["inputs"],
                                         bundle["records"]["awsLambda"]["receipt"]["rawReceipt"]))


def archive_fixture(inputs, raw):
    import io
    import zipfile
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr(ARTIFACT['inputsPath'], dq.canonical_bytes(inputs))
        archive.writestr(ARTIFACT['rawReceiptPath'], dq.canonical_bytes(raw))
    data = output.getvalue()
    return data, {**ARTIFACT, 'sha256': 'sha256:' + hashlib.sha256(data).hexdigest()}


def test_actual_archive_bytes_are_required(manifest, deployment, bound_raw):
    inputs = dq.execution_inputs(manifest, deployment)
    data, artifact = archive_fixture(inputs, bound_raw)
    assert dq.artifact_documents(data, artifact) == (inputs, bound_raw)
    with pytest.raises(dq.QualificationError, match='archive digest'):
        dq.artifact_documents(data + b'changed', artifact)
    with pytest.raises(dq.QualificationError, match='missing a required'):
        dq.artifact_documents(data, {**artifact, 'inputsPath': 'other.json'})


def test_duplicate_archive_entries_refused():
    import io
    import zipfile
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('a.json', '{}')
        with pytest.warns(UserWarning):
            archive.writestr('a.json', '{"forged":true}')
    data = output.getvalue()
    with pytest.raises(dq.QualificationError, match='duplicate'):
        dq.artifact_documents(data, {**ARTIFACT, 'sha256': 'sha256:' + hashlib.sha256(data).hexdigest()})


@pytest.mark.parametrize('text', ['{"a":1,"a":2}', '{"a":NaN}', '[]'])
def test_ambiguous_raw_json_refused(text):
    with pytest.raises(dq.QualificationError):
        dq.parse_json(text)


@pytest.mark.parametrize('change', ['run', 'sha', 'attempt-time', 'digest', 'expired'])
def test_artifact_api_identity_and_attempt_checked_before_download(monkeypatch, manifest, deployment, bound_raw, producer_run, change):
    import json
    inputs = dq.execution_inputs(manifest, deployment)
    archive, artifact = archive_fixture(inputs, bound_raw)
    run = {**run_metadata(producer_run), 'run_started_at': '2026-10-13T00:00:00Z',
           'updated_at': '2026-10-13T00:10:00Z'}
    metadata = {'id': artifact['id'], 'expired': False, 'digest': artifact['sha256'],
                'size_in_bytes': len(archive), 'created_at': '2026-10-13T00:09:00Z',
                'workflow_run': {'id': 123, 'head_sha': producer_run['sourceRevision']}}
    if change == 'run':
        metadata['workflow_run']['id'] = 124
    elif change == 'sha':
        metadata['workflow_run']['head_sha'] = 'b' * 40
    elif change == 'attempt-time':
        metadata['created_at'] = '2026-10-12T23:59:59Z'
    elif change == 'digest':
        metadata['digest'] = 'sha256:' + '0' * 64
    else:
        metadata['expired'] = True
    calls = []
    def api(command, **_kwargs):
        calls.append(command)
        assert not command[-1].endswith('/zip'), 'invalid provenance must not download archive'
        return json.dumps(metadata)
    monkeypatch.setattr(dq.subprocess, 'check_output', api)
    with pytest.raises(dq.QualificationError):
        dq.fetch_producer_artifact(producer_run, artifact, run)
    assert len(calls) == 1


def stage_fixture():
    terraform = {'schema': 'honua.deployment-stage-inputs/v1', 'stage': 'terraform',
                 'capturedAt': '2026-10-13T00:00:00Z',
                 'configuration': {'effectiveVarsSha256': 'sha256:' + '1' * 64},
                 'previousStageDigest': None}
    helm = {'schema': terraform['schema'], 'stage': 'helm',
            'capturedAt': '2026-10-13T00:10:00Z',
            'configuration': {'actualResolvedValuesSha256': 'sha256:' + '2' * 64},
            'previousStageDigest': dq.digest(terraform)}
    configuration = {'redisMode': 'off', 'stages': {'terraform': terraform, 'helm': helm}}
    actuation = {'terraformStartedAt': '2026-10-13T00:00:01Z', 'helmStartedAt': '2026-10-13T00:10:01Z'}
    return configuration, actuation


def test_staged_capture_binds_actual_configuration_before_each_actuation():
    configuration, actuation = stage_fixture()
    dq.verify_eks_stages(configuration, actuation)
    configuration['stages']['terraform']['configuration']['unknownFutureInput'] = True
    with pytest.raises(dq.QualificationError, match='hash chain'):
        dq.verify_eks_stages(configuration, actuation)


@pytest.mark.parametrize('field,value', [('terraformStartedAt', '2026-10-12T23:59:59Z'),
                                         ('helmStartedAt', '2026-10-13T00:09:59Z'),
                                         ('terraformStartedAt', '2026-10-13T00:11:00Z')])
def test_late_capture_or_reversed_stage_order_refused(field, value):
    configuration, actuation = stage_fixture()
    actuation[field] = value
    with pytest.raises(dq.QualificationError, match='before its own actuation'):
        dq.verify_eks_stages(configuration, actuation)


def test_preparation_still_runs_intrinsic_exact_pin_checks(monkeypatch):
    import resolve_trunk_candidate as resolver
    calls = []
    findings = vp.Findings()
    monkeypatch.setattr(vp, 'validate', lambda *a, **kw: calls.append(('structure', kw)) or findings)
    def exact(*args):
        calls.append(('exact', args))
        args[1].errors.append('actual immutable source is missing')
    monkeypatch.setattr(vp, 'check_exact_candidate', exact)
    result = resolver.validate_resolved_inputs({}, {}, prepare_deployments=True, ledger_produced_later=True)
    assert result.errors == ['actual immutable source is missing']
    assert calls[0][1]['exact_candidate'] is False
    assert calls[1][0] == 'exact'


def test_successful_run_cannot_authenticate_fabricated_artifact_bytes(manifest, deployment, bound_raw, producer_run):
    inputs = dq.execution_inputs(manifest, deployment)
    receipt = dq.record(inputs, bound_raw, producer_run, ARTIFACT)
    changed = copy.deepcopy(bound_raw)
    changed['serving']['deployed']['write']['remainingRows'] = 1
    with pytest.raises(dq.QualificationError, match='immutable producer artifact'):
        dq.verify_receipt(receipt, manifest, deployment, run_metadata(producer_run),
                          lambda *_: (inputs, changed))


def test_actual_artifact_archive_is_downloaded_only_after_provenance_pass(monkeypatch, manifest, deployment, bound_raw, producer_run):
    import json
    inputs = dq.execution_inputs(manifest, deployment)
    archive, artifact = archive_fixture(inputs, bound_raw)
    run = {**run_metadata(producer_run), 'run_started_at': '2026-10-13T00:00:00Z',
           'updated_at': '2026-10-13T00:10:00Z'}
    metadata = {'id': artifact['id'], 'expired': False, 'digest': artifact['sha256'],
                'size_in_bytes': len(archive), 'created_at': '2026-10-13T00:09:00Z',
                'workflow_run': {'id': 123, 'head_sha': producer_run['sourceRevision']}}
    commands = []
    def api(command, **_kwargs):
        commands.append(command)
        return archive if command[-1].endswith('/zip') else json.dumps(metadata)
    monkeypatch.setattr(dq.subprocess, 'check_output', api)
    assert dq.fetch_producer_artifact(producer_run, artifact, run) == (inputs, bound_raw)
    assert len(commands) == 2 and commands[1][-1].endswith('/1234/zip')
