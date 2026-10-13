"""Execute the installed-client report steps when the producer emits no artifact."""
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/gate-installed-clients.yml'


@pytest.mark.parametrize('status', ['pass', 'blocked', 'fail'])
@pytest.mark.parametrize('receipt_present', [False, True])
def test_report_preserves_producer_failure_and_existing_verdicts(tmp_path, status, receipt_present):
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow['jobs']['report']['steps']
    assemble = next(step['run'] for step in steps if step.get('id') == 'assemble')
    enforce = next(step['run'] for step in steps if step.get('name') == 'Enforce')
    expected = status if receipt_present else 'fail'
    if receipt_present:
        directory = tmp_path / 'receipt'
        directory.mkdir()
        (directory / 'installed-client-certification.json').write_text(json.dumps({
            'server': {'sha': 'a' * 40},
            'results': [{
                'cell': 'js-feature-query', 'status': status,
                'package': '@honua/sdk-js', 'version': '0.2.0',
                'operationId': 'query', 'detail': 'observed producer result',
            }],
        }))
    env = {
        **os.environ, 'SUITE_STATUS': status, 'PLATFORM_LABEL': 'test-candidate',
        'RUN_URL': 'https://github.com/honua-io/honua-release/actions/runs/1',
        'GITHUB_OUTPUT': str(tmp_path / 'outputs'),
        'GITHUB_STEP_SUMMARY': str(tmp_path / 'summary'),
    }
    result = subprocess.run(['bash', '-c', assemble], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / 'out/gate-report.json').read_text())
    assert report['overallStatus'] == expected
    assert f'overall_status={expected}' in (tmp_path / 'outputs').read_text()
    assert f'SDK regression: {expected}' in (tmp_path / 'summary').read_text()
    if receipt_present:
        assert report['gates'][0]['status'] == status
        assert report['gates'][0]['why'] == 'observed producer result'
        assert report['server'] == {'sha': 'a' * 40}
    else:
        assert 'produced no installed-client receipt' in result.stdout
        assert report['gates'] == []
        assert report['server'] is None
        assert report['summary'] == {'pass': 0, 'blocked': 0, 'fail': 0}
    verdict = subprocess.run(['bash', '-c', enforce], cwd=tmp_path,
                             env={**env, 'OVERALL': report['overallStatus']},
                             capture_output=True, text=True, timeout=20)
    assert verdict.returncode == (1 if expected == 'fail' else 0)
