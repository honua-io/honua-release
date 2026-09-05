"""Decision classifier rejection, coverage, label safety, and retry behavior."""
import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import release_decision_record as decision


def issue(*labels, number=1):
    return {'repo':'honua-server', 'number':number, 'labels':list(labels),
            'body_sha256':'reviewed-body', 'state':'open', 'title':'Example', 'family':None}


def rules(named=True):
    return {'exceptions':{}, 'admission_reviews':{'honua-server#1':{
        'body_sha256':'reviewed-body', 'named_promise':named,
        'reason':'Documented mutation loses committed data.'}}}


def test_no_admission_review_is_unclassified():
    with pytest.raises(ValueError, match='unclassified'):
        decision.classify(issue('priority/P1', 'first-release-gate'), {'exceptions':{},'admission_reviews':{}})
    changed = issue('priority/P1')
    changed['body_sha256'] = 'edited-after-review'
    with pytest.raises(ValueError, match='stale'):
        decision.classify(changed, rules())


def test_priority_zero_is_never_lost_to_feature_or_missing_promise():
    assert decision.classify(issue('priority/P0', 'slice/3d'), rules(False))[0] == 'must-fix-before-cut'


def test_gate_admission_beats_low_priority_but_not_missing_promise():
    row = issue('priority/P2', 'first-release-gate')
    assert decision.classify(row, rules())[0] == 'must-fix-before-cut'
    assert decision.classify(row, rules(False))[0] == 'post-cut-hardening'


def test_explicit_sequencing_exception_requires_reason():
    config = rules()
    config['exceptions']['honua-server#1'] = {'bucket':'prove-against-candidate', 'reason':'Decision 5: SIGKILL proof follows cut.'}
    assert decision.classify(issue('priority/P0'), config)[0] == 'prove-against-candidate'
    config['exceptions']['honua-server#1']['reason'] = ''
    with pytest.raises(ValueError, match='unclassified'):
        decision.classify(issue('priority/P0'), config)


def test_family_priority_one_and_lower_priority_defaults():
    row = issue('priority/P1', 'bug-hunt/ga-vectors-2026-09-04')
    row['family'] = {'status':'queued'}
    assert decision.classify(row, rules(False))[0] == 'must-fix-before-cut'
    row['family']['status'] = 'parked'
    assert decision.classify(row, rules(False))[0] == 'post-cut-hardening'
    for priority in ('priority/P2','priority/P3'):
        assert decision.classify(issue(priority), rules())[0] == 'post-cut-hardening'
    assert decision.classify(issue(), rules())[0] == 'post-cut-hardening'


def test_unknown_or_conflicting_priority_fails():
    for labels in [('priority/P4',), ('priority/P0','priority/P1')]:
        with pytest.raises(ValueError, match='unclassified'):
            decision.classify(issue(*labels), rules())


def test_label_plan_preserves_unrelated_labels_and_removes_release_for_later():
    row = {**issue('priority/P1','security','release/2026.1','bucket/must-fix-before-cut'), 'bucket':'2026.2'}
    add, remove, rejected = decision.label_plan(row, rules())
    assert add == ['bucket/2026.2','release/2026.2']
    assert remove == ['bucket/must-fix-before-cut','release/2026.1']
    assert not rejected
    row['labels'] = sorted((set(row['labels']) | set(add)) - set(remove))
    assert decision.label_plan(row, rules())[:2] == ([], [])
    assert 'security' in row['labels']


def test_unadmitted_gate_is_removed_with_comment_signal():
    row = {**issue('first-release-gate'), 'bucket':'post-cut-hardening'}
    assert decision.label_plan(row, rules(False))[1:] == (['first-release-gate'], True)


def test_closed_implementation_never_proves_candidate():
    row = issue('priority/P0')
    row['state'] = 'closed'
    data = {'issues':[row], 'candidate_digest':'not yet cut'}
    classified = decision.decisions(data, rules())[0]
    assert classified['implementation_ticket_closed'] is True
    assert classified['qualified_against_candidate'] is False
    with pytest.raises(ValueError, match='duplicate'):
        decision.decisions({**data, 'issues':[row,row]}, rules())


def test_snapshot_and_generated_record_are_complete_and_current():
    data = json.loads(decision.INPUTS.read_text())
    config = json.loads(decision.OVERRIDES.read_text())
    rows = decision.decisions(data, config)
    assert len(rows) >= 264
    assert len({decision.issue_key(r) for r in rows}) == len(rows)
    table = decision.tables(rows)
    for row in rows:
        assert row['bucket'] in decision.BUCKETS
        assert 'body' not in row  # issue bodies / raw API responses never enter the repo
        if row['state']=='open' and row['bucket']=='must-fix-before-cut':
            assert decision.link(decision.issue_key(row)) in table
    assert decision.RECORD.read_text() == decision.render(data, rows)
    ledger = json.loads(decision.LEDGER.read_text())
    assert ledger['issues'] == rows


def test_transient_failures_retry_identical_request_for_full_budget():
    failure = type('Result', (), {'returncode':1,'stderr':'error connecting to api.github.com','stdout':''})()
    with patch.object(decision.subprocess, 'run', return_value=failure) as run, patch.object(decision.time,'sleep') as sleep:
        with pytest.raises(ValueError, match='300-second'):
            decision.gh('api','orgs/honua-io/repos')
        assert run.call_count == 6
        assert all(call == run.call_args_list[0] for call in run.call_args_list)
        assert sum(call.args[0] for call in sleep.call_args_list) == 300


def test_403_sleeps_then_retries_without_authentication():
    forbidden = type('Result', (), {'returncode':1,'stderr':'HTTP 403','stdout':''})()
    success = type('Result', (), {'returncode':0,'stderr':'','stdout':'[]'})()
    with patch.object(decision.subprocess, 'run', side_effect=[forbidden,success]) as run, patch.object(decision.time,'sleep') as sleep:
        assert decision.gh('api','orgs/honua-io/repos') == []
        sleep.assert_called_once_with(60)
        assert run.call_args_list[0] == run.call_args_list[1]
