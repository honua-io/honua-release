"""Paired reports must verify both receipts before comparing stage outcomes."""
import copy

import pytest

import ai_arc_instruction_compare as C


def inputs():
    base = {'runId': 'baseline', 'instructionVariant': 'guide-only', 'variant': 'A',
            'target': 'local-docker', 'arm': 'codex', 'modelId': 'gpt-6.1-sol', 'cliVersion': '1.0', 'family': 'codex',
            'temperature': None, 'temperaturePolicy': 'CLI default',
            'candidate': {'imageDigest': 'same'}, 'arcSha256': 'same', 'harnessDigests': {},
            'preregDigest': 'same', 'exclusions': [], 'opsScope': {},
            'slotFacts': {'quickstart': {'packageSha256': {'AGENTS.md': 'same'}}},
            'stages': [{'stage': stage, 'outcome': 'pass'} for stage in C.R.STAGES]}
    skills = copy.deepcopy(base)
    skills.update(runId='skills', instructionVariant='guide-plus-skills')
    return [base, skills]


def test_report_verifies_both_runs_and_reports_each_stage():
    seen = []
    def verify(receipt):
        seen.append(receipt['runId'])
        return {'stagePass': {s: receipt['runId'] == 'skills' for s in C.R.STAGES}, 'unsafe': False}
    report = C.compare(inputs(), verify)
    assert seen == ['baseline', 'skills']
    assert [stage['delta'] for stage in report['stages']] == [1, 1, 1, 1]
    assert report['nativeSkillLoadingProven'] is False
    assert report['certifying'] is False


@pytest.mark.parametrize('damage', ['candidate', 'modelId', 'cliVersion', 'family', 'temperature', 'temperaturePolicy',
                                    'variant', 'target', 'runId', 'stage', 'duplicate'])
def test_report_rejects_unpaired_or_incomplete_runs(damage):
    rows = inputs()
    if damage == 'stage':
        rows[1]['stages'][2]['outcome'] = 'blocked'
    elif damage == 'duplicate':
        rows[1]['stages'].append(rows[1]['stages'][0])
    else:
        rows[1][damage] = rows[0][damage] if damage == 'runId' else 'other'
    with pytest.raises(ValueError):
        C.compare(rows, lambda _: {'stagePass': {s: True for s in C.R.STAGES}})


def test_report_propagates_evidence_verification_failure():
    def fail(_):
        raise ValueError('evidence differs')
    with pytest.raises(ValueError, match='evidence differs'):
        C.compare(inputs(), fail)


def test_verifier_binds_each_receipt_to_its_evidence_and_rebuilt_package(monkeypatch, tmp_path):
    calls = []
    package = {'sha256': {'AGENTS.md': 'rebuilt'}}
    evidence = {'baseline': tmp_path / 'baseline', 'skills': tmp_path / 'skills'}
    monkeypatch.setattr(C.R, 'harness_digests', lambda: {'runner': 'current'})
    def verify_run(arc, receipt, **options):
        calls.append((arc, receipt, options))
        return {'stagePass': {s: True for s in C.R.STAGES}}
    monkeypatch.setattr(C.R, 'verify_run', verify_run)
    C.compare(inputs(), C.verifier(({'arc': 'pinned'}, {}, {'candidate': 'bound'}, package), evidence))
    assert len(calls) == 2
    for arc, receipt, options in calls:
        assert arc == {'arc': 'pinned'}
        assert options['candidate'] == {'candidate': 'bound'}
        assert options['package'] is package
        assert options['digests'] == {'runner': 'current'}
        assert options['evidence'] == evidence[receipt['runId']]
        assert options['row']['runId'] == receipt['runId']
        assert options['goal_sets'] == {}
