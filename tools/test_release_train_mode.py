"""Preparation authenticates inputs without starting gates or certifying an RC."""
import json

import pytest

from release_train_mode import preparation_receipt, validate
from test_nightly_certification import evaluate, step, workflow


def preparation():
    return {'FREEZE_ONLY': 'true', 'DRY_RUN': 'false', 'NIGHTLY': 'false', 'CANDIDATE_REF': '',
            'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REF': 'refs/heads/trunk',
            'GITHUB_REPOSITORY': 'honua-io/honua-release',
            'GITHUB_WORKFLOW_REF': 'honua-io/honua-release/.github/workflows/release-train.yml@refs/heads/trunk'}


def test_preparation_needs_no_fabricated_capacity_or_dr_receipts():
    assert validate(preparation()) is True


@pytest.mark.parametrize('key,value', [
    ('DRY_RUN', 'true'), ('NIGHTLY', 'true'), ('CANDIDATE_REF', 'candidate/x'),
    ('GITHUB_EVENT_NAME', 'pull_request'), ('GITHUB_REF', 'refs/heads/fix'),
    ('GITHUB_REPOSITORY', 'another/repository'),
    ('GITHUB_WORKFLOW_REF', 'honua-io/honua-release/.github/workflows/other.yml@refs/heads/trunk'),
])
def test_preparation_refuses_ambiguous_or_untrusted_invocations(key, value):
    with pytest.raises(ValueError):
        validate({**preparation(), key: value})


@pytest.mark.parametrize('dry_run', ['true', 'false'])
def test_full_train_still_requires_both_real_receipt_inputs(dry_run):
    env = {'FREEZE_ONLY': 'false', 'DRY_RUN': dry_run,
           'CAPACITY_RECEIPT_URL': 'https://example.test/capacity.zip',
           'DR_RECEIPT_URL': 'https://example.test/dr.json'}
    assert validate(env) is False
    for key in ('CAPACITY_RECEIPT_URL', 'DR_RECEIPT_URL'):
        with pytest.raises(ValueError):
            validate({**env, key: ''})


@pytest.mark.parametrize('needs_ok,cancelled', [(True, False), (False, False), (True, True)])
def test_preparation_cannot_start_any_gate_or_report_even_after_failed_freeze(needs_ok, cancelled):
    jobs = workflow('release-train.yml')['jobs']
    assert set(jobs) == {'freeze', 'report'} | {name for name in jobs if name.startswith('gate_')}
    context = {'inputs.freeze_only': True, 'inputs.dry_run': False, 'needs.freeze.result': 'success'}
    for name, job in jobs.items():
        if name != 'freeze':
            assert not evaluate(job['if'], context, needs_ok=needs_ok, cancelled=cancelled), name


def test_normal_train_keeps_every_gate_and_its_failed_run_report():
    jobs = workflow('release-train.yml')['jobs']
    context = {'inputs.freeze_only': False, 'needs.freeze.result': 'success'}
    for name, job in jobs.items():
        if name != 'freeze':
            assert evaluate(job['if'], context), name
    assert evaluate(jobs['report']['if'], context, needs_ok=False)


def test_preparation_attests_source_inputs_and_never_signs_release_lock():
    freeze = workflow('release-train.yml')['jobs']['freeze']
    context = {'inputs.freeze_only': True, 'inputs.dry_run': False, 'inputs.nightly': False}
    selected = [s for s in freeze['steps'] if evaluate(s.get('if', 'true'), context)]
    assert any('--qualification' in s.get('run', '') for s in selected)
    attestations = [s['with']['subject-path'].split() for s in selected if 'attest-build-provenance@' in s.get('uses', '')]
    assert attestations == [['platform-manifest.yaml', 'compatibility-matrix.yaml']]
    assert not any('platform-lock.sigstore.json' in s.get('run', '') for s in selected)
    assert not evaluate(step(freeze, 'Bind the complete atomic')['if'], context)
    assert step(freeze, 'Validate preparation')['env']['FREEZE_ONLY'] == '${{ inputs.freeze_only }}'


def test_preparation_receipt_cannot_be_confused_with_release_evidence(tmp_path):
    lock = tmp_path/'platform-lock.json'
    lock.write_text(json.dumps({'platform': {'status': 'rc'}}))
    result = preparation_receipt(lock)
    assert result['qualification'] is False and result['releasePublished'] is False
    assert result['qualificationGates'] == 'not-run'
    (tmp_path/'platform-lock.sigstore.json').write_text('unexpected release signature')
    with pytest.raises(ValueError, match='release lock signature'):
        preparation_receipt(lock)
