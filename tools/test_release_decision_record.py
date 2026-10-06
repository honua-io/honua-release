"""Decision classifier rejection, coverage, label safety, and retry behavior."""
import json
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
    # Ruling 2026-09-11: a bug-hunt finding is a bug and is pre-cut even when its
    # fix family is parked; only a test(...) expansion item keeps the old rule.
    row['family']['status'] = 'parked'
    assert decision.classify(row, rules(False))[0] == 'must-fix-before-cut'
    row['title'] = 'test(grpc): the only real-database tests assert a tautology'
    assert decision.classify(row, rules(False))[0] == 'post-cut-hardening'
    for priority in ('priority/P2','priority/P3'):
        assert decision.classify(issue(priority), rules())[0] == 'post-cut-hardening'
    assert decision.classify(issue(), rules())[0] == 'post-cut-hardening'


def test_bugs_and_ci_are_pre_cut_whatever_the_priority():
    # Operator ruling 2026-09-11 ("bugs and ci should be pre cut").
    for labels in [('priority/P2', 'bug'), ('priority/P3', 'bug-hunt/esri-2026-09-03'), ('area/ci',)]:
        assert decision.classify(issue(*labels), rules())[0] == 'must-fix-before-cut'
    titled = issue('priority/P2'); titled['title'] = 'bug: long WFS feature type names break rerun idempotency'
    assert decision.classify(titled, rules())[0] == 'must-fix-before-cut'
    ci = issue('priority/P3'); ci['title'] = 'perf(ci): build-time deep cut'
    assert decision.classify(ci, rules())[0] == 'must-fix-before-cut'
    # Explicit 2026.2 still wins; epics and hunt program issues are not bugs.
    later = issue('priority/P2', 'bug', 'release/2026.2')
    assert decision.classify(later, rules())[0] == '2026.2'
    program = issue('bug-hunt/2026-09-03'); program['title'] = '2026.1 GA Bug-Hunt & Quality Program'
    assert decision.classify(program, rules())[0] == 'post-cut-hardening'
    epic = issue('priority/P2', 'bug'); epic['title'] = 'Epic: hunt follow-ups'
    assert decision.classify(epic, rules())[0] == 'post-cut-hardening'


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


def test_working_candidate_is_rendered_without_cutting_the_candidate():
    data = json.loads(decision.INPUTS.read_text())
    config = json.loads(decision.OVERRIDES.read_text())
    rows = decision.decisions(data, config)
    wc = data['working_candidate']
    record = decision.render(data, rows, config)
    assert '**Candidate digest: not yet cut ·' in record
    assert f'**Working candidate {wc["label"]} ({wc["status"]}) · {wc["train_kind"]} train [' in record
    for name, sha, _ in wc['pins']:
        assert f'| {name} | `{sha}` |' in record
    assert all(not r['qualified_against_candidate'] for r in rows)
    without = {k: v for k, v in data.items() if k != 'working_candidate'}
    assert 'Working candidate' not in decision.render(without, rows, config)


def test_working_candidate_note_is_rendered_and_optional():
    data = json.loads(decision.INPUTS.read_text())
    config = json.loads(decision.OVERRIDES.read_text())
    rows = decision.decisions(data, config)
    wc = data['working_candidate']
    assert wc['note'] in decision.render(data, rows, config)
    stripped = {k: v for k, v in wc.items() if k != 'note'}
    record = decision.render({**data, 'working_candidate': stripped}, rows, config)
    assert wc['note'] not in record
    assert f'**Working candidate {wc["label"]}' in record


def test_closed_implementation_never_proves_candidate():
    row = issue('priority/P0')
    row['state'] = 'closed'
    data = {'issues':[row], 'candidate_digest':'not yet cut'}
    classified = decision.decisions(data, rules())[0]
    assert classified['implementation_ticket_closed'] is True
    assert classified['qualified_against_candidate'] is False
    with pytest.raises(ValueError, match='duplicate'):
        decision.decisions({**data, 'issues':[row,row]}, rules())


def test_release_label_drift_is_reported_and_never_silently_reconciled():
    kept = {**issue('priority/P1', 'release/2026.2'), 'bucket':'post-cut-hardening'}
    moved = {**issue('priority/P1', 'release/2026.1', number=2), 'bucket':'2026.2'}
    agreed = {**issue('priority/P1', 'release/2026.1', number=3), 'bucket':'must-fix-before-cut'}
    closed = {**issue('priority/P1', 'release/2026.2', number=4), 'bucket':'post-cut-hardening', 'state':'closed'}
    drift = dict(decision.label_drift([kept, moved, agreed, closed]))
    assert drift == {'honua-server#1':'recorded post-cut-hardening, labelled release/2026.2',
                     'honua-server#2':'recorded 2026.2, labelled release/2026.1'}
    # The recorded bucket is what the record and the totals keep; drift is reported, not applied.
    assert decision.label_plan(kept, rules())[:2] == (['bucket/post-cut-hardening'], [])
    assert 'release/2026.2' not in decision.label_plan(kept, rules())[1]


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
            assert f"https://github.com/honua-io/{row['repo']}/issues/{row['number']}" in table
    assert decision.RECORD.read_text() == decision.render(data, rows, config)
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


@pytest.mark.parametrize('errors', [
    ['HTTP 403'],
    ['error connecting to api.github.com', 'HTTP 403'],
    ['HTTP 403', 'error connecting to api.github.com'],
])
def test_403_and_transient_failures_share_one_retry_budget(errors):
    failures = [type('Result', (), {'returncode':1, 'stderr':error, 'stdout':''})()
                for error in errors] * 10
    success = type('Result', (), {'returncode':0, 'stderr':'', 'stdout':'[]'})()
    # A final success ensures the old unbounded loop fails this test without hanging.
    with patch.object(decision.subprocess, 'run', side_effect=[*failures, success]) as run, \
            patch.object(decision.time, 'sleep') as sleep:
        with pytest.raises(ValueError, match='300-second'):
            decision.gh('api', 'repos/honua-io/honua-release/labels', '--method', 'POST',
                        payload={'name':'bucket/2026.2'})
        assert sum(call.args[0] for call in sleep.call_args_list) == 300
        assert all(call.args[0] > 0 for call in sleep.call_args_list)
        assert run.call_count == sleep.call_count + 1
        assert all(call == run.call_args_list[0] for call in run.call_args_list)


def test_403_can_recover_on_final_budgeted_attempt():
    forbidden = type('Result', (), {'returncode':1, 'stderr':'HTTP 403', 'stdout':''})()
    success = type('Result', (), {'returncode':0, 'stderr':'', 'stdout':'[]'})()
    with patch.object(decision.subprocess, 'run', side_effect=[forbidden] * 5 + [success]), \
            patch.object(decision.time, 'sleep') as sleep:
        assert decision.gh('api', 'orgs/honua-io/repos') == []
        assert sum(call.args[0] for call in sleep.call_args_list) == 300


def test_interrupted_apply_retains_new_cohort_before_removing_release_label(tmp_path, monkeypatch):
    original = {'observed_at':'2026-09-05T00:00:00Z','candidate_digest':'not yet cut','issues':[]}
    fresh = {**original, 'issues':[issue('type/feature','release/2026.1')]}
    inputs = tmp_path / 'inputs.json'
    inputs.write_text(json.dumps(original))
    overrides = tmp_path / 'overrides.json'
    overrides.write_text(json.dumps(rules()))
    monkeypatch.setattr(decision, 'INPUTS', inputs)
    monkeypatch.setattr(decision, 'OVERRIDES', overrides)
    monkeypatch.setattr(decision, 'refresh', lambda data: fresh)
    def interrupted(rows, config):
        assert json.loads(inputs.read_text())['issues'] == fresh['issues']
        raise ValueError('simulated interruption after label removal')
    monkeypatch.setattr(decision, 'apply_labels', interrupted)
    monkeypatch.setattr(decision.sys, 'argv', ['record','--refresh','--apply'])
    with pytest.raises(ValueError, match='simulated interruption'):
        decision.main()
    assert json.loads(inputs.read_text())['issues'] == fresh['issues']


def test_working_candidate_train_is_labelled_by_its_kind():
    data = json.loads(decision.INPUTS.read_text())
    config = json.loads(decision.OVERRIDES.read_text())
    rows = decision.decisions(data, config)
    wc = data['working_candidate']
    # Run 37132168180 was the first scheduled nightly strict train, not a dry run.
    assert wc['train'].endswith('/37132168180') and wc['train_kind'] == 'scheduled strict'
    assert '· scheduled strict train [37132168180]' in decision.render(data, rows, config)
    assert 'dry-run train [37132168180]' not in decision.render(data, rows, config)
    for kind in (None, 'nightly', ''):
        bad = {**wc, 'train_kind': kind} if kind is not None else {k: v for k, v in wc.items() if k != 'train_kind'}
        with pytest.raises(ValueError, match='train_kind'):
            decision.render({**data, 'working_candidate': bad}, rows, config)


SEC_COUNTS = {'security-review-2026-10-03': {'counts': {'ga_blocker': 2}}}


def sec_rules(*rows):
    return {**rules(), 'rulings': SEC_COUNTS, 'security_findings': [dict(r) for r in rows]}


OPEN = {'id':'SEC-4', 'repo':'honua-io/honua-server', 'status':'open'}
FIXED = {'id':'SEC-9', 'repo':'honua-io/honua-server', 'status':'fixed', 'fixedBy':'5082'}
LOCK = {'tag':'2026.1-rc.3', 'digest':'sha256:' + 'a' * 64}


@pytest.mark.parametrize('rows', [
    [],
    [OPEN],  # fewer rows than the ruling's GA-blocker count
    [OPEN, {**OPEN}],  # duplicate id
    [OPEN, {**FIXED, 'id':'SEC-09'}],
    [OPEN, {**FIXED, 'id':'4'}],
    [OPEN, {**FIXED, 'repo':'mikemcdougall/honua-security-findings'}],
    [OPEN, {k: v for k, v in FIXED.items() if k != 'fixedBy'}],  # fixed needs its PR
    [OPEN, {**FIXED, 'fixedBy':'#5082'}],
    [OPEN, {**FIXED, 'status':'open'}],  # open cannot claim a fix
    [OPEN, {**FIXED, 'status':'mitigated'}],
])
def test_security_rows_fail_closed(rows):
    with pytest.raises(ValueError):
        decision.security_findings(sec_rules(*rows))
    if not rows:
        with pytest.raises(ValueError, match='missing'):
            decision.security_findings({**rules(), 'rulings': SEC_COUNTS})


def test_committed_security_rows_cover_the_ruled_ga_blockers():
    config = json.loads(decision.OVERRIDES.read_text())
    ids = [f['id'] for f in decision.security_findings(config)]
    assert ids == ['SEC-4', 'SEC-5', 'SEC-9', 'SEC-13', 'SEC-14', 'SEC-16', 'SEC-18', 'SEC-21', 'SEC-23', 'SEC-28']


def blocker(state='open'):
    return {**issue('priority/P0'), 'state': state, 'bucket': 'must-fix-before-cut'}


def test_decision_is_go_only_with_no_blocker_every_finding_fixed_and_a_signed_lock():
    fixed = sec_rules({**OPEN, 'status':'fixed', 'fixedBy':'5398'}, FIXED)
    clear = [blocker('closed'), {**issue('priority/P2', number=2), 'bucket': 'post-cut-hardening'}]
    data = {'candidate_digest':'not yet cut', 'signed_lock': LOCK}
    assert decision.decision(data, clear, fixed) == ('GO', 'Decision: GO (signed lock 2026.1-rc.3)')
    assert decision.decision(data, [blocker()], fixed) == ('HOLD', 'Decision: HOLD (1 pre-cut blockers)')
    assert decision.decision(data, clear, sec_rules(OPEN, FIXED)) == (
        'HOLD', 'Decision: HOLD (1 GA-blocking security findings open)')
    assert decision.decision({'candidate_digest':'not yet cut'}, clear, fixed) == ('HOLD', 'Decision: HOLD (no signed lock)')
    assert decision.decision({}, [blocker()], sec_rules(OPEN, FIXED))[1] == (
        'Decision: HOLD (1 pre-cut blockers; 1 GA-blocking security findings open; no signed lock)')
    for lock in ({'tag':'2026.1.0', 'digest':LOCK['digest']}, {'tag':'2026.1-rc.3'}, 'rc.3'):
        with pytest.raises(ValueError, match='signed_lock'):
            decision.decision({'signed_lock': lock}, clear, fixed)


def test_committed_record_computes_hold():
    data = json.loads(decision.INPUTS.read_text())
    config = json.loads(decision.OVERRIDES.read_text())
    rows = decision.decisions(data, config)
    verdict, line = decision.decision(data, rows, config)
    assert verdict == 'HOLD' and 'GA-blocking security findings open' in line
    assert f'· {line} ·' in decision.RECORD.read_text()


def pr(merged=True, body='Closes #5073. SEC-9.', base='trunk'):
    return {'merged_at':'2026-09-22T14:59:38Z' if merged else None, 'title':'fix(auth): bind tokens', 'body':body,
            'base':{'ref':base, 'repo':{'default_branch':'trunk'}}}


def verify(config, responses, record=None):
    data = {'candidate_digest':'not yet cut'}
    rows = [blocker('closed')]
    text = record if record is not None else decision.decision(data, rows, config)[1]
    with patch.object(decision, 'gh', side_effect=responses) as call:
        verdict = decision.verify_security(data, rows, config, text)
    return verdict, call


def test_security_gate_verifies_each_fixed_row_against_its_merged_pr():
    config = sec_rules(OPEN, FIXED)
    verdict, call = verify(config, [pr()])
    assert verdict == 'HOLD'
    call.assert_called_once_with('api', 'repos/honua-io/honua-server/pulls/5082')
    for response, why in [(pr(merged=False), 'not merged'), (pr(body='Hardening.'), 'does not cite SEC-9'),
                          (pr(body='SEC-90 only.'), 'does not cite SEC-9'), (pr(base='release/2026.0'), 'not the default branch')]:
        with pytest.raises(ValueError, match=why):
            verify(config, [response])


def test_security_gate_fails_closed_on_unreadable_pr():
    with pytest.raises(ValueError, match='GitHub request failed'):
        verify(sec_rules(OPEN, FIXED), ValueError('GitHub request failed (HTTP 404): repos/honua-io/honua-server/pulls/5082'))


def test_security_gate_requires_hold_while_a_row_is_open():
    config = sec_rules(OPEN, FIXED)
    with pytest.raises(ValueError, match='computed decision'):
        verify(config, [pr()], record='Decision: GO (signed lock 2026.1-rc.3)')
    # With every row fixed, the open-row rule no longer applies; the computed line still must match.
    fixed = sec_rules({**OPEN, 'status':'fixed', 'fixedBy':'5398'}, FIXED)
    assert verify(fixed, [pr(body='SEC-4 follow-up.'), pr()])[0] == 'HOLD'


def test_public_issue_redacts_titles_the_vendor_terms_classifier_calls_confidential():
    # Built from fragments so this test file names no confidential term itself (R30).
    for title in ('Fix ' + 'arc' + 'py import', 'Arc' + 'GIS P' + 'ro crashes', 'Open project.' + 'ap' + 'rx'):
        redacted = decision.public_issue({**issue(), 'title': title})
        assert redacted['title'] == 'Evidence honua-server#1'
    assert decision.public_issue(issue())['title'] == 'Example'


def test_redacted_title_keeps_the_bucket_the_live_title_produced():
    # Unlabelled P2: the pre-cut bucket comes only from the title prefix. The
    # product words are split so this file does not itself name them (R30).
    blocker = issue('priority/P2')
    blocker['title'] = 'fix: ' + 'Arc' + 'GIS P' + 'ro import'
    epic = issue('priority/P2', 'bug')
    epic['title'] = 'Epic: ' + 'Arc' + 'GIS P' + 'ro follow-ups'
    ci = issue('priority/P3')
    ci['title'] = 'ci: ' + 'Arc' + 'GIS P' + 'ro runner'
    expansion = issue('priority/P2', 'bug-hunt/ga-vectors-2026-09-04')
    expansion['title'] = 'test: ' + 'Arc' + 'GIS P' + 'ro coverage'
    for raw in (blocker, epic, ci, expansion):
        before = decision.classify(raw, rules())
        persisted = decision.public_issue(raw)
        assert persisted['title'] == 'Evidence honua-server#1'
        assert decision.classify(persisted, rules()) == before
        again = decision.public_issue(persisted)
        assert again.get('title_signals') == persisted.get('title_signals')
        assert decision.classify(again, rules()) == before
    assert decision.classify(blocker, rules())[0] == 'must-fix-before-cut'
    assert decision.classify(epic, rules())[0] == 'post-cut-hardening'
    data = {'observed_at': '2026-10-06T00:00:00Z', 'candidate_digest': 'not yet cut', 'issues': [blocker]}
    snapshot = json.loads(decision.compact_snapshot(decision.public_snapshot(data)))
    assert decision.decisions(snapshot, rules())[0]['bucket'] == 'must-fix-before-cut'
    stale = {**issue('priority/P2'), 'title_signals': ['bug']}
    assert 'title_signals' not in decision.public_issue(stale)
    assert decision.classify(stale, rules())[0] == 'post-cut-hardening'
    corrupt = {**issue(), 'title': 'Evidence honua-server#1', 'title_signals': ['secret']}
    with pytest.raises(ValueError, match='invalid title_signals'):
        decision.classify(corrupt, rules())
