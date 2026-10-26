"""Synthetic EKS receipts exercise the real canonical journey validator, never cloud proof."""
import hashlib
import json
from pathlib import Path
import sys

import pytest
from jsonschema import ValidationError
import yaml

import deployment_qualification as dq
from test_deployment_qualification import stage_fixture

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'e2e'))
import cloud_journey as cj


@pytest.fixture
def cell():
    manifest_text = (Path(__file__).resolve().parents[1] / 'platform-manifest.yaml').read_text()
    manifest = yaml.safe_load(manifest_text)
    configuration, actuation = stage_fixture()
    configuration['stages']['terraform']['configuration'] = {
        'arguments': ['-var=region=us-west-2'], 'iacSource': manifest['components']['honua-iac']['sha'],
        'secretVersionObservations': [], 'tenantFixture': {'enabled': False, 'signingSecret': None}}
    configuration['stages']['helm']['configuration'] = {
        'arguments': ['helm', 'upgrade', '--install', 'synthetic'], 'values': {'replicas': 1},
        'helmSource': manifest['components']['honua-helm']['sha'],
        'runtimeSecretCommitmentAlgorithm': 'hmac-sha256',
        'runtimeSecretValueCommitments': {'HONUA_ADMIN_PASSWORD': 'hmac-sha256:' + 'a' * 64},
        'immutableSecretVersionIds': [], 'tenantSigningSecret': None}
    configuration['stages']['helm']['previousStageDigest'] = dq.digest(configuration['stages']['terraform'])
    producer = {'repository': 'honua-io/honua-release', 'sourceRevision': 'a' * 40,
                'workflow': '.github/workflows/e2e-cloud-aws.yml'}
    deployment = {'target': 'awsEks', 'component': 'honua-helm', 'architecture': 'x86_64',
                  'producer': producer, 'configuration': configuration}
    inputs = dq.execution_inputs(manifest, deployment)
    producer = {**producer, 'runId': '123', 'runAttempt': 1,
                'runUrl': 'https://github.com/honua-io/honua-release/actions/runs/123'}
    driver, _ = cj.drivers()
    name = 'aws-eks/redis-off'
    receipt = driver.build_receipt(manifest=manifest,
        journey=driver.load(cj.HERE / 'journey.v1.json'), roster={'status': 'pass'},
        evidence_uri='urn:synthetic:cloud#honua-run=123/1', mode='live',
        target={'id': name, 'kind': 'aws-eks'}, target_path=None,
        target_base_url='https://synthetic.invalid',
        workspace=driver.pins.ClientWorkspace(status='blocked', root=None, reason='synthetic control'),
        stage_results=None, notices=[])
    receipt['status'] = 'pass'
    receipt['clientWorkspace'].update(status='pass', resolved=[{
        'name': 'honua-sdk-js', 'package': '@honua/sdk-js', 'version': '0.0.0',
        'ecosystem': 'npm', 'integrityVerified': True, 'tarballSha256': 'f' * 64, 'bin': {}}])
    receipt['server'] = {'sourceSha': manifest['components']['honua-server']['sha'],
                         'image': cj.candidate_image(name, manifest)}
    for stage in receipt['stages']:
        stage.update(status='pass', blockedBy=[], checks=[{'id': 'synthetic', 'kind': 'http',
            'invocation': 'GET /fixture', 'status': 'pass', 'detail': 'synthetic control'}])
        for key in ('operationId', 'operationInstanceId', 'correlationId', 'auditId', 'proposalId',
                    'jobId', 'resourceUri', 'jobStatus', 'jobCreatedAt'):
            stage[key] = 'synthetic-' + key
        stage['evidence'].update(source='live-aws-eks', freshness='verified-current',
                                completeness='complete', observedAt=receipt['generatedAt'])
    driver.validate_receipt(receipt, cj.HERE / 'receipt.schema.json')
    manifest_digest = hashlib.sha256(manifest_text.encode()).hexdigest()
    path = 'cloud-evidence/123/1/aws-eks/redis-off/receipt-1.json'
    text = json.dumps(receipt)
    attempt = {'number': 1, 'cell': name, 'runId': '123', 'runAttempt': '1',
               'candidateDigest': manifest_digest, 'receipt': path,
               'receiptSha256': hashlib.sha256(text.encode()).hexdigest(), 'failureAttribution': None}
    report = {'cell': name, 'target': 'aws-eks', 'redis': 'redis-off', 'status': 'pass',
              'require_real': True, 'teardown': {'attempted': True, 'completed': True},
              'readiness': {'ready': True}, 'checks': [{'name': 'synthetic', 'status': 'pass'}],
              'journeyAttempts': [attempt], 'cost': {'status': 'pass', 'scope': 'run',
                  'runId': '123', 'runAttempt': '1', 'candidateDigest': manifest_digest,
                  'amountUsd': '1', 'ceilingUsd': '20', 'measuredAt': cj.now()}}
    raw = {'schema': 'honua.eks-deployment-cell/v1', 'result': 'pass',
           'producer': producer, 'runUrl': producer['runUrl'],
           'executionInputs': {'schema': dq.INPUT_SCHEMA, 'digest': dq.digest(inputs)},
           'actuation': actuation, 'report': report, 'executedManifestYaml': manifest_text,
           'journeyReceiptBytes': {path: text}}
    artifact = {'id': 1234, 'sha256': 'sha256:' + 'c' * 64,
                'inputsPath': 'cloud-evidence/123/1/aws-eks/redis-off/deployment-execution-inputs.json',
                'rawReceiptPath': 'cloud-evidence/123/1/aws-eks/redis-off/deployment-cell-receipt.json'}
    return manifest, inputs, raw, producer, artifact


def test_complete_eks_cell_uses_real_journey_validation(cell):
    _manifest, inputs, raw, producer, artifact = cell
    envelope = dq.record(inputs, raw, producer, artifact)
    assert envelope['phase'] == 'full-deployment-cell'
    assert envelope['scope']['fullGaQualified'] is False


@pytest.mark.parametrize('change', ['missing-stage8', 'blocked-stage8', 'wrong-server', 'wrong-client',
                                   'changed-bytes', 'teardown', 'cost', 'wrong-manifest', 'pending-journey'])
def test_eks_full_cell_does_not_phase_away_real_gates(cell, change):
    _manifest, inputs, raw, producer, artifact = cell
    path, text = next(iter(raw['journeyReceiptBytes'].items()))
    receipt = json.loads(text)
    if change == 'missing-stage8':
        receipt['stages'].pop(7)
    elif change == 'blocked-stage8':
        receipt['stages'][7]['status'] = 'blocked'
    elif change == 'wrong-server':
        receipt['server']['sourceSha'] = 'b' * 40
    elif change == 'wrong-client':
        receipt['clientArtifacts']['honua-sdk-js']['version'] = '99.0.0'
    elif change == 'teardown':
        raw['report']['teardown']['completed'] = False
    elif change == 'cost':
        raw['report']['cost']['amountUsd'] = '21'
    elif change == 'wrong-manifest':
        raw['executedManifestYaml'] += '\nfutureRuntimePolicy: changed\n'
    elif change == 'pending-journey':
        raw['report']['journeyAttempts'] = []
    if change == 'changed-bytes':
        raw['journeyReceiptBytes'][path] += ' '
    else:
        raw['journeyReceiptBytes'][path] = json.dumps(receipt)
        if raw['report']['journeyAttempts']:
            raw['report']['journeyAttempts'][0]['receiptSha256'] = hashlib.sha256(json.dumps(receipt).encode()).hexdigest()
    with pytest.raises((ValueError, KeyError, ValidationError)):
        dq.record(inputs, raw, producer, artifact)


def test_captured_manifest_override_is_paired_and_does_not_change_default(cell):
    manifest, _inputs, raw, _producer, _artifact = cell
    attempt = raw['report']['journeyAttempts'][0]
    receipt = json.loads(next(iter(raw['journeyReceiptBytes'].values())))
    kwargs = {'run_id': '123', 'run_attempt': '1'}
    assert cj.validate_attempt(attempt, receipt, 'aws-eks/redis-off', **kwargs)
    assert cj.validate_attempt(attempt, receipt, 'aws-eks/redis-off', **kwargs,
                               pinned_manifest=manifest, pinned_manifest_digest=attempt['candidateDigest'])
    for override in ({'pinned_manifest': manifest}, {'pinned_manifest_digest': attempt['candidateDigest']}):
        with pytest.raises(ValueError, match='supplied together'):
            cj.validate_attempt(attempt, receipt, 'aws-eks/redis-off', **kwargs, **override)


def test_full_cell_capture_must_belong_to_exact_run_attempt(cell):
    manifest, inputs, raw, producer, artifact = cell
    envelope = dq.record(inputs, raw, producer, artifact)
    from test_deployment_qualification import run_metadata
    run = {**run_metadata(producer), 'run_started_at': '2026-10-13T00:00:00Z',
           'updated_at': '2026-10-13T00:11:00Z'}
    dq.verify_receipt(envelope, manifest, inputs['deployment'], run, lambda *_: (inputs, raw))
    run['run_started_at'] = '2026-10-13T00:00:01Z'
    with pytest.raises(dq.QualificationError, match='another producer attempt'):
        dq.verify_receipt(envelope, manifest, inputs['deployment'], run, lambda *_: (inputs, raw))


def test_supported_projection_requires_both_complete_cells(cell):
    import validate_platform as vp
    _manifest, inputs, raw, producer, artifact = cell
    proof = dq.VerifiedQualification(inputs, dq.record(inputs, raw, producer, artifact))
    matrix = vp._load_yaml(vp.MATRIX_PATH)
    with pytest.raises(dq.QualificationError, match='full verified EKS'):
        dq.project_verified_matrix(matrix, {'awsEks': dq.VerifiedEksQualification({'redis-off': proof})})
    assert matrix['deploy']['honua-helm']['awsEks']['architectures']['x86_64']['status'] == 'preview'


@pytest.mark.parametrize('change', ['iac-source', 'helm-source', 'missing-values', 'floating-reference',
                                   'wrong-version', 'missing-runtime-commitments', 'unkeyed-runtime-hash', 'wrong-commitment-algorithm'])
def test_configuration_cannot_borrow_source_or_unobserved_secret_version(cell, change):
    import copy
    manifest, inputs, _raw, _producer, _artifact = cell
    configuration = copy.deepcopy(inputs['deployment']['configuration'])
    terraform = configuration['stages']['terraform']['configuration']
    helm = configuration['stages']['helm']['configuration']
    arn = 'arn:aws:secretsmanager:us-west-2:123456789012:secret:synthetic-control'
    version = 'a' * 32
    helm['immutableSecretVersionIds'] = [{'arn': arn, 'versionId': version}]
    helm['values']['config'] = {'env': {'Example': 'aws:secretsmanager:' + arn + '?versionId=' + version}}
    dq.verify_eks_configuration(configuration, manifest)
    if change == 'iac-source':
        terraform['iacSource'] = 'b' * 40
    elif change == 'helm-source':
        helm['helmSource'] = 'b' * 40
    elif change == 'missing-values':
        del helm['values']
    elif change == 'floating-reference':
        helm['values']['config']['env']['Example'] = 'aws:secretsmanager:' + arn
    elif change == 'wrong-version':
        helm['values']['config']['env']['Example'] = 'aws:secretsmanager:' + arn + '?versionId=' + 'b' * 32
    elif change == 'unkeyed-runtime-hash':
        helm['runtimeSecretValueCommitments'] = {'HONUA_ADMIN_PASSWORD': 'sha256:' + 'a' * 64}
    elif change == 'wrong-commitment-algorithm':
        helm['runtimeSecretCommitmentAlgorithm'] = 'sha256'
    else:
        helm['runtimeSecretValueCommitments'] = {}
    with pytest.raises(dq.QualificationError):
        dq.verify_eks_configuration(configuration, manifest)


def complete_pair(cell):
    import copy
    import validate_platform as vp
    from test_deployment_qualification import run_metadata
    manifest, original_inputs, original_raw, producer, original_artifact = cell
    envelope = vp.LAMBDA_ENVELOPE_PATH.read_bytes()
    proofs = {}
    for mode in ('off', 'on'):
        inputs, raw, artifact = copy.deepcopy((original_inputs, original_raw, original_artifact))
        deployment = inputs['deployment']
        deployment['operatingEnvelope'] = {'path': dq.ENVELOPE_PATH,
                                          'sha256': 'sha256:' + hashlib.sha256(envelope).hexdigest()}
        deployment['configuration']['redisMode'] = mode
        raw['executionInputs']['digest'] = dq.digest(inputs)
        name = f'aws-eks/redis-{mode}'
        raw['report'].update(cell=name, redis=f'redis-{mode}')
        attempt = raw['report']['journeyAttempts'][0]
        journey = json.loads(next(iter(raw['journeyReceiptBytes'].values())))
        journey['target']['id'] = name
        text = json.dumps(journey)
        attempt.update(cell=name, receipt=f'cloud-evidence/123/1/{name}/receipt-1.json',
                       receiptSha256=hashlib.sha256(text.encode()).hexdigest())
        raw['journeyReceiptBytes'] = {attempt['receipt']: text}
        artifact['inputsPath'] = f'cloud-evidence/123/1/{name}/deployment-execution-inputs.json'
        artifact['rawReceiptPath'] = f'cloud-evidence/123/1/{name}/deployment-cell-receipt.json'
        receipt = dq.record(inputs, raw, producer, artifact)
        run = {**run_metadata(producer), 'run_started_at': '2026-10-13T00:00:00Z',
               'updated_at': '2026-10-13T00:11:00Z'}
        dq.verify_receipt(receipt, manifest, deployment, run, lambda *_: (inputs, raw))
        proofs[f'redis-{mode}'] = dq.VerifiedQualification(inputs, receipt)
    result = dq.VerifiedEksQualification(proofs)
    result.require_current(manifest, envelope)
    return result


def test_complete_verified_pair_promotes_only_reviewed_eks_architecture(cell):
    import copy
    import validate_platform as vp
    proof = complete_pair(cell)
    manifest = cell[0]
    matrix = vp._load_yaml(vp.MATRIX_PATH)
    # A real proof cannot silently promote the current Preview policy.
    with pytest.raises(dq.QualificationError, match='outside the reviewed GA scope'):
        dq.project_verified_matrix(matrix, {'awsEks': proof})
    # Retain validation coverage for a future explicitly reviewed GA scope.
    matrix['deploy']['honua-helm']['awsEks']['architectures']['x86_64'].update(
        status='ga-target', releaseScope='ga-target')
    original = copy.deepcopy(matrix)
    updated = dq.project_verified_matrix(matrix, {'awsEks': proof})
    assert matrix == original
    lambda_row = updated['deploy']['honua-server']['awsLambda']['architectures']['x86_64']
    assert lambda_row == original['deploy']['honua-server']['awsLambda']['architectures']['x86_64']
    assert lambda_row['status'] == 'preview' and lambda_row['qualification'] == 'pending'
    rows = updated['deploy']['honua-helm']['awsEks']['architectures']
    assert rows['x86_64']['qualificationReceipt'] == proof.reference()
    findings = vp.Findings()
    vp.check_deploy_qualification(manifest, updated, findings, exact_candidate=False,
                                 deployment_qualification={'awsEks': proof})
    assert findings.ok, findings.errors
    rows['arm64'] = copy.deepcopy(rows['x86_64'])
    findings = vp.Findings()
    vp.check_deploy_qualification(manifest, updated, findings, exact_candidate=False,
                                 deployment_qualification={'awsEks': proof})
    assert len(findings.errors) == 1 and 'arm64' in findings.errors[0]
