"""No-cloud controls for persisted EKS raw proof; never qualification evidence."""
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deployment_cell as cell


@pytest.fixture
def captured(tmp_path):
    directory = tmp_path / 'cloud-evidence/123/1/aws-eks/redis-off'
    directory.mkdir(parents=True)
    env = {'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': 'a' * 40,
           'GITHUB_REPOSITORY': 'honua-io/honua-release'}
    inputs = {'schema': 'honua.deployment-execution-inputs/v1', 'manifest': {'synthetic': True},
              'deployment': {'target': 'awsEks', 'producer': {'repository': env['GITHUB_REPOSITORY'],
                  'sourceRevision': env['GITHUB_SHA'], 'workflow': '.github/workflows/e2e-cloud-aws.yml'},
                  'configuration': {'redisMode': 'off'}}}
    (directory / 'deployment-execution-inputs.json').write_text(json.dumps(inputs))
    (directory / 'deployment-actuation.json').write_text('{}')
    (directory / 'executed-manifest.yaml').write_text('synthetic: true\n')
    receipt = '{"status":"fail","synthetic":true}\n'
    (directory / 'receipt-1.json').write_text(receipt)
    report = {'cell': 'aws-eks/redis-off', 'status': 'fail', 'journeyAttempts': [{
        'number': 1, 'receipt': (directory / 'receipt-1.json').relative_to(tmp_path).as_posix(),
        'receiptSha256': hashlib.sha256(receipt.encode()).hexdigest()}]}
    return tmp_path, directory, env, inputs, report


def test_fresh_teardown_process_reads_original_persisted_bytes(captured):
    root, directory, env, inputs, report = captured
    path = cell.write_receipt(report, directory, root, environ=env)
    raw = json.loads(path.read_text())
    assert raw['result'] == 'fail'  # writer never promotes a failed cell
    assert raw['executedManifestYaml'] == 'synthetic: true\n'
    assert raw['executionInputs']['digest'] == 'sha256:' + hashlib.sha256(cell.canonical_bytes(inputs)).hexdigest()
    assert raw['journeyReceiptBytes'][report['journeyAttempts'][0]['receipt']].endswith('\n')
    with pytest.raises(FileExistsError):
        cell.write_receipt(report, directory, root, environ=env)


@pytest.mark.parametrize('change', ['run', 'source', 'cell', 'path', 'bytes', 'symlink'])
def test_wrong_identity_or_changed_receipt_refused(captured, change):
    root, directory, env, _inputs, report = captured
    if change == 'run':
        env['GITHUB_RUN_ID'] = '124'
    elif change == 'source':
        env['GITHUB_SHA'] = 'b' * 40
    elif change == 'cell':
        report['cell'] = 'aws-eks/redis-on'
    elif change == 'path':
        report['journeyAttempts'][0]['receipt'] = '../foreign/receipt-1.json'
    elif change == 'bytes':
        (directory / 'receipt-1.json').write_text('{"status":"pass"}')
    else:
        original = directory / 'receipt-1.json'
        foreign = root / 'foreign.json'
        foreign.write_bytes(original.read_bytes())
        original.unlink()
        original.symlink_to(foreign)
    with pytest.raises(ValueError):
        cell.write_receipt(report, directory, root, environ=env)
    assert not (directory / 'deployment-cell-receipt.json').exists()
