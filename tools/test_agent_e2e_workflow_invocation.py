"""Execute the gate's invocation boundary; these fixtures do not qualify a campaign."""
import os
import subprocess

import pytest

from test_nightly_certification import evaluate, step, workflow


def gate():
    return workflow('gate-agent-e2e.yml')


def identity(tmp_path, *, current_run, candidate_run='', producer=True, event='workflow_dispatch'):
    fake = tmp_path / 'gh'
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$GH_CALLS"\nexit ' + ('0' if producer else '1') + '\n')
    fake.chmod(0o755)
    output = tmp_path / 'outputs'
    calls = tmp_path / 'gh-calls'
    env = {**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'],
           'RUN_ID': '4241', 'RUN_ATTEMPT': '1', 'SOURCE_SHA': 'a' * 40,
           'CAMPAIGN_DIGEST': 'sha256:' + 'b' * 64, 'CANDIDATE_RUN_ID': candidate_run,
           'CURRENT_RUN_CANDIDATE': 'true' if current_run else '',
           'GITHUB_EVENT_NAME': event, 'GITHUB_REPOSITORY': 'honua-io/honua-release',
           'GITHUB_OUTPUT': str(output), 'GH_CALLS': str(calls)}
    result = subprocess.run(['bash'], input=step(gate()['jobs']['verify'], 'Require configured')['run'],
                            text=True, capture_output=True, env=env, cwd=tmp_path)
    return result, output, calls


@pytest.mark.parametrize('event', ['workflow_dispatch', 'schedule'])
def test_reusable_gate_uses_running_callers_candidate_without_completed_run_lookup(tmp_path, event):
    result, output, calls = identity(tmp_path, current_run=True, event=event, producer=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'run_id=4241' in output.read_text()
    assert not calls.exists()
    context = {'inputs.current_run_candidate': True, 'github.event_name': event,
               'github.ref': 'refs/heads/nightly/candidate'}
    selected = [s for s in gate()['jobs']['verify']['steps'] if evaluate(s.get('if', 'true'), context)]
    candidates = [s for s in selected if s.get('with', {}).get('name') == 'candidate-manifest']
    assert len(candidates) == 1
    assert 'run-id' not in candidates[0]['with']
    assert not any(s.get('name', '').startswith(('Authenticate the candidate', 'Refuse a standalone'))
                   for s in selected)


def test_current_run_mode_is_only_a_reusable_input_and_train_sets_it():
    triggers = gate()['triggers']
    assert 'current_run_candidate' not in triggers['workflow_dispatch']['inputs']
    assert triggers['workflow_call']['inputs']['current_run_candidate'] == {
        'required': False, 'type': 'boolean', 'default': True}
    caller = workflow('release-train.yml')['jobs']['gate_agent_e2e']
    assert caller['with']['current_run_candidate'] is True
    assert step(gate()['jobs']['verify'], 'Require configured')['env']['CURRENT_RUN_CANDIDATE'] == (
        '${{ inputs.current_run_candidate }}')


@pytest.mark.parametrize('candidate_run,producer,success', [
    ('4242', True, True), ('', True, False), ('4242', False, False), ('not-a-run', True, False),
])
def test_standalone_keeps_required_candidate_and_real_producer_check(tmp_path, candidate_run, producer, success):
    result, output, calls = identity(tmp_path, current_run=False, candidate_run=candidate_run, producer=producer)
    assert (result.returncode == 0) is success, result.stdout + result.stderr
    assert output.exists() is success
    assert 'agent-e2e-campaign.yml?ref=trunk' in calls.read_text()


def test_standalone_download_remains_bound_to_selected_completed_trunk_run():
    context = {'inputs.current_run_candidate': '', 'github.event_name': 'workflow_dispatch',
               'github.ref': 'refs/heads/trunk'}
    selected = [s for s in gate()['jobs']['verify']['steps'] if evaluate(s.get('if', 'true'), context)]
    candidates = [s for s in selected if s.get('with', {}).get('name') == 'candidate-manifest']
    assert len(candidates) == 1
    assert candidates[0]['with']['run-id'] == '${{ inputs.candidate_run_id }}'
    assert candidates[0]['with']['repository'] == 'honua-io/honua-release'
    assert any(s.get('name', '').startswith('Authenticate the candidate') for s in selected)


def test_standalone_off_trunk_refuses_before_candidate_download(tmp_path):
    guard = step(gate()['jobs']['verify'], 'Refuse a standalone')
    assert evaluate(guard['if'], {'inputs.current_run_candidate': '',
                                 'github.event_name': 'workflow_dispatch', 'github.ref': 'refs/heads/topic'})
    result = subprocess.run(['bash'], input=guard['run'], text=True, capture_output=True,
                            env={**os.environ, 'GITHUB_REF': 'refs/heads/topic'}, cwd=tmp_path)
    assert result.returncode != 0
    assert 'only from refs/heads/trunk' in result.stdout
