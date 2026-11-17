"""Retired Terraform producers cannot provision, sweep or report a live pass."""
import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('name', ['e2e-cloud-aws.yml', 'e2e-cloud-aws-cell.yml',
                                 'agent-e2e-aws-install.yml'])
def test_retired_cloud_entry_points_fail_without_running_cloud_jobs(tmp_path, name):
    workflow = yaml.safe_load((ROOT / '.github/workflows' / name).read_text())
    triggers = workflow.get('on', workflow.get(True))
    assert 'schedule' not in triggers
    jobs = workflow['jobs']
    for job_id, job in jobs.items():
        if job_id == 'protocol':
            assert job['if'] == "github.event_name == 'pull_request'"
            continue
        assert job_id == 'retired', job_id
    retired = jobs['retired']
    assert not retired.get('needs')
    assert retired['permissions'] == {}
    assert len(retired['steps']) == 1
    step = retired['steps'][0]
    output = tmp_path / 'output'
    result = subprocess.run(['bash', '-c', step['run']], capture_output=True, text=True,
                            env={**os.environ, 'GITHUB_OUTPUT': str(output)})
    assert result.returncode != 0
    assert 'Terraform cloud cells are retired' in result.stdout
    assert output.read_text() == 'status=blocked\n'
    if 'workflow_call' in triggers and name == 'e2e-cloud-aws.yml':
        assert triggers['workflow_call']['outputs']['overall_status']['value'] == '${{ jobs.retired.outputs.status }}'


def test_reaper_is_removed_and_state_bound_manual_recovery_remains():
    assert not (ROOT / '.github/workflows/release-cell-reaper.yml').exists()
    recovery = yaml.safe_load((ROOT / '.github/workflows/cloud-cell-retirement.yml').read_text())
    assert set(recovery.get('on', recovery.get(True))) == {'workflow_dispatch'}
    assert recovery['concurrency']['cancel-in-progress'] is False
