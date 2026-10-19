from copy import deepcopy
import json
from unittest.mock import Mock

import pytest

import deployment_qualification as qualification
from nightly_deployments import dispatch_lambda, final_lambda_manifest, lambda_artifact, lambda_inputs


def manifest():
    return {'platformRelease': '2026.1-rc.3', 'components': {'honua-server': {
        'sha': 'a' * 40, 'awsLambdaImage': 'ghcr.io/honua-io/honua-server:exact',
        'awsLambdaDigest': 'sha256:' + 'b' * 64, 'awsLambdaArchitecture': 'x86_64',
        'awsLambdaEcrDigest': 'pending-ecr-mirror'}}}


def test_dispatch_uses_exact_response_run_not_a_racy_listing():
    api = Mock(side_effect=[{'workflow_run_id': 42}, {
        'id': 42, 'event': 'workflow_dispatch', 'path': '.github/workflows/lambda-preview-certification.yml',
        'head_branch': 'trunk'}])
    assert dispatch_lambda(api, manifest(), b'policy') == 42
    body = api.call_args_list[0].args[2]
    assert body['return_run_details'] is True
    assert body['ref'] == 'refs/heads/trunk'
    assert body['inputs']['server_revision'] == 'a' * 40
    assert api.call_count == 2


def test_payload_is_data_and_preserves_unknown_execution_fields():
    source = manifest()
    source['unknownExecutionInput'] = {'new': 'retained'}
    prepared = json.loads(lambda_inputs(source, b'policy')['prepared_manifest'])
    assert prepared['manifest']['unknownExecutionInput'] == {'new': 'retained'}
    assert 'platformRelease' not in prepared['manifest']
    assert prepared['operatingEnvelope']['sha256'].startswith('sha256:')
    assert source['platformRelease'] == '2026.1-rc.3'


@pytest.mark.parametrize('artifacts', [[], [{'id': 1, 'name': 'lambda-preview-certification-receipt', 'expired': True}],
    [{'id': number, 'name': 'lambda-preview-certification-receipt', 'expired': False} for number in (1, 2)]])
def test_ambiguous_or_missing_producer_artifacts_fail(artifacts):
    with pytest.raises(ValueError):
        lambda_artifact(Mock(return_value={'total_count': len(artifacts), 'artifacts': artifacts}), 42)


def test_bootstrap_accepts_only_measured_ecr_change(monkeypatch):
    # The projection comparison is the boundary under test; the full producer/config
    # schema and raw live assertions are independently required by canonical record().
    monkeypatch.setattr(qualification, 'execution_inputs', lambda m, d: {'manifest': qualification.execution_manifest(m), 'deployment': d})
    requested = manifest()
    actual = 'sha256:' + 'c' * 64
    expected = deepcopy(requested)
    expected['components']['honua-server']['awsLambdaEcrDigest'] = actual
    captured = {'manifest': qualification.execution_manifest(expected), 'deployment': {'actual': 'configuration'}}
    raw = {'artifact': {'ecrDigest': actual}}
    assert final_lambda_manifest(requested, captured, raw) == expected
    assert requested['components']['honua-server']['awsLambdaEcrDigest'] == 'pending-ecr-mirror'
    captured['manifest']['components']['honua-server']['sha'] = 'd' * 40
    with pytest.raises(ValueError, match='different prepared inputs'):
        final_lambda_manifest(requested, captured, raw)
    with pytest.raises(ValueError, match='already bound'):
        final_lambda_manifest(expected, captured, {'artifact': {'ecrDigest': 'sha256:' + 'e' * 64}})


@pytest.mark.parametrize('include_lambda', [True, False])
def test_preview_assembly_retains_pending_eks_and_validates_actual_lambda(tmp_path, monkeypatch, include_lambda):
    import nightly_deployments as nightly
    import yaml
    import validate_platform as vp
    # Actual cryptographic/provenance verification has dedicated negative tests. At this seam,
    # require verification of precisely the supplied Lambda record before any output is created.
    paths = {name: tmp_path / name for name in ('manifest', 'matrix', 'envelope', 'lambda', 'out')}
    paths['manifest'].write_text(yaml.safe_dump(manifest()))
    matrix = vp._load_yaml(vp.MATRIX_PATH)
    paths['matrix'].write_text(yaml.safe_dump(matrix))
    paths['envelope'].write_bytes(b'policy')
    record = {'actual': 'lambda producer bytes'}
    paths['lambda'].write_text(json.dumps(record))
    def verify(bundle, candidate, envelope):
        assert bundle['records'] == {'awsLambda': record}
        assert candidate == manifest() and envelope == b'policy'
        if not include_lambda:
            raise ValueError('missing or invalid actual Lambda proof')
        return {'awsLambda': object()}
    monkeypatch.setattr(qualification, 'verify_bundle', verify)
    if not include_lambda:
        with pytest.raises(ValueError, match='actual Lambda proof'):
            nightly.assemble(paths['manifest'], paths['matrix'], paths['envelope'], paths['lambda'], None, paths['out'])
        assert not paths['out'].exists()
    else:
        nightly.assemble(paths['manifest'], paths['matrix'], paths['envelope'], paths['lambda'], None, paths['out'])
        assert yaml.safe_load((tmp_path / 'compatibility-matrix.yaml').read_text()) == matrix
        assert json.loads(paths['out'].read_text())['records'] == {'awsLambda': record}


def test_assembly_refuses_missing_eks_when_scope_is_ga(tmp_path):
    import nightly_deployments as nightly
    import yaml
    import validate_platform as vp
    matrix = vp._load_yaml(vp.MATRIX_PATH)
    matrix['deploy']['honua-helm']['awsEks']['architectures']['x86_64']['releaseScope'] = 'ga-target'
    source = tmp_path / 'matrix.yaml'
    source.write_text(yaml.safe_dump(matrix))
    record = tmp_path / 'lambda.json'
    record.write_text('{}')
    with pytest.raises(ValueError, match='requires both complete'):
        nightly.assemble(tmp_path / 'unused', source, tmp_path / 'unused', record, None, tmp_path / 'out')
