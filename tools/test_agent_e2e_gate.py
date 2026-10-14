"""Adversarial controller-gate checks; fixture receipts are never qualification evidence."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

import agent_e2e_gate as gate


NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + '\n', encoding='utf-8')
    return {'path': path.name, 'sha256': gate.digest(path.read_bytes())}


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    targets = ['local-docker', 'aws-ecs-redis-on', 'aws-eks', 'aws-lambda', 'aws-batch']
    scenarios = []
    for prefix, count, cells in [('P', 24, targets[:3]), ('L', 6, [targets[3]]), ('B', 6, [targets[4]])]:
        for i in range(1, count + 1):
            sid = f'{prefix}{i:02}'
            scenarios.append({'id': sid, 'targets': cells,
                              'prompts': {v: f'{sid} goal {v}' for v in 'ABC'},
                              'assertions': ['catalog.exact', 'filter.exact'],
                              'limits': {'maxTurns': 40, 'maxActions': 80, 'timeoutSeconds': 900}})
    pack = {'schemaVersion': 1, 'packId': 'honua-agent-e2e-rc-v1', 'totalBudgetUsd': 500,
            'variants': list('ABC'), 'targets': targets, 'scenarios': scenarios,
            'corpusManifest': 'corpus.json'}
    write(tmp_path / 'pack.json', pack)
    monkeypatch.setattr(gate, 'FROZEN_PACK_BYTES', (tmp_path / 'pack.json').read_bytes())
    fixture_ref = write(tmp_path / 'fixture.json', {'data': 'immutable-test-fixture'})
    write(tmp_path / 'corpus.json', {'files': [{'path': 'fixture.json', 'bytes': (tmp_path / 'fixture.json').stat().st_size,
                                             'sha256': fixture_ref['sha256'].removeprefix('sha256:')}]})
    lock = {'components': {'server': {'source': 'a' * 40}}, 'contentDigests': {'data': 'b' * 64}, 'fixtures': []}
    write(tmp_path / 'lock.json', lock)
    expected = {'schemaVersion': 1, 'campaignId': 'test-campaign',
                'packDigest': gate.digest((tmp_path / 'pack.json').read_bytes()),
                'corpusDigest': gate.digest((tmp_path / 'corpus.json').read_bytes()),
                'qualificationLockDigest': gate.digest((tmp_path / 'lock.json').read_bytes()),
                'componentBindingDigest': gate.component_binding(lock),
                'modelId': 'configured-model', 'returnedModelId': 'actual-model',
                'controller': {'repository': 'honua-io/honua-release',
                               'workflow': '.github/workflows/agent-e2e.yml', 'sourceSha': 'a' * 40,
                               'runId': 1, 'runAttempt': 1},
                'startedAt': '2026-10-14T10:00:00Z', 'expiresAt': '2026-10-14T13:00:00Z',
                'totalBudgetUsd': 500}
    write(tmp_path / 'campaign.json', expected)
    evidence_root = tmp_path / 'evidence'
    observation = write(evidence_root / 'observed.json', {'resourceIds': ['fixture-one']})
    cleanup_ref = write(evidence_root / 'cleanup.json', {'resourceIds': []})
    expectations = {'schemaVersion': 1, 'campaignId': expected['campaignId'],
                    'createdAt': '2026-10-14T09:59:00Z', 'cases': {}}
    receipts, ledger_entries, registered = [], [], []
    for number, (cid, case) in enumerate(gate.inventory(pack).items()):
        expectations['cases'][cid] = {'assertions': {a: {'resourceIds': ['fixture-one']}
            for a in case['assertions']}, 'cleanup': {'resourceIds': []}}
        transcript_ref = write(evidence_root / f'transcript-{number}.json',
            {'campaignId': expected['campaignId'], 'caseId': cid, 'attempt': 1,
             'modelId': expected['modelId'], 'returnedModelId': expected['returnedModelId'],
             'turns': [{'response': 'model-response-1'}, {'response': 'model-response-2'}],
             'actions': [{'command': 'read'}, {'command': 'create'}, {'command': 'verify'}]})
        report = {'campaignId': expected['campaignId'], 'caseId': cid, 'attempt': 1,
                  'componentBindingDigest': expected['componentBindingDigest'], 'producer': 'independent-oracle',
                  'assertions': [{'id': a, 'status': 'pass', 'evidence': observation}
                                 for a in sorted(case['assertions'])]}
        oracle = write(evidence_root / f'oracle-{number}.json', report)
        receipt = {k: copy.deepcopy(expected[k]) for k in ('campaignId', 'packDigest', 'corpusDigest',
                   'qualificationLockDigest', 'componentBindingDigest', 'modelId', 'returnedModelId', 'controller')}
        receipt.update({k: case[k] for k in ('scenarioId', 'variant', 'target', 'promptDigest')})
        receipt.update(schemaVersion=1, receiptSchema='agent-e2e-receipt.v1', caseId=cid,
                       attempt=1, mode='genuine-model', startedAt=f'2026-10-14T10:{targets.index(case["target"])*2:02}:00Z',
                       completedAt=f'2026-10-14T10:{targets.index(case["target"])*2+1:02}:00Z', status='pass', failureAttribution=None,
                       namespace=f'ns-{number}', costs={'modelUsd': 0.01, 'infrastructureUsd': 0.01,
                       'cleanupUsd': 0.01, 'cleanupReservedUsd': 0.02},
                       usage={'modelTurns': 2, 'executedActions': 3},
                       cleanup={'status': 'pass', 'evidence': cleanup_ref}, oracle=oracle,
                       transcript=transcript_ref, failureEvidence=None)
        receipts.append(write(tmp_path / 'receipts' / f'{number}.json', receipt))
        registered.append({**{k: receipt[k] for k in ('campaignId','caseId','attempt','status','failureAttribution','packDigest','corpusDigest','modelId')},
                           'receiptDigest': receipts[-1]['sha256']})
        for kind in ('model', 'infrastructure', 'cleanup'):
            ledger_entries.append({'id': f'{cid}/1/{kind}', 'campaignId': expected['campaignId'],
                                   'caseId': cid, 'attempt': 1, 'kind': kind, 'amountUsd': '0.02',
                                   'actualUsd': '0.01', 'state': 'settled', 'basisDigest': 'sha256:' + 'b' * 64})
    ledger = {'schemaVersion': 1, 'candidateKey': expected['componentBindingDigest'], 'ceilingUsd': '500',
              'cleanupReserveUsd': '50', 'entries': ledger_entries}
    write(tmp_path / 'ledger.json', ledger)
    write(tmp_path / 'registry.json', {'schemaVersion': 1, 'candidateKey': expected['componentBindingDigest'], 'attempts': registered})
    write(tmp_path / 'expectations.json', expectations)
    expected['expectationsDigest'] = gate.digest((tmp_path / 'expectations.json').read_bytes())
    write(tmp_path / 'campaign.json', expected)
    trusted = {'schemaVersion': 1, 'campaignId': expected['campaignId'],
               'campaignDigest': gate.digest((tmp_path / 'campaign.json').read_bytes()),
               'controller': expected['controller'], 'receipts': receipts,
               'budgetLedgerDigest': gate.digest((tmp_path / 'ledger.json').read_bytes()),
               'attemptRegistryDigest': gate.digest((tmp_path / 'registry.json').read_bytes())}
    write(tmp_path / 'trusted.json', trusted)
    return tmp_path


def validate(root):
    return gate.validate(root / 'pack.json', root / 'campaign.json', root / 'lock.json',
                         root / 'trusted.json', root / 'receipts', root / 'evidence', root / 'ledger.json', root / 'expectations.json', root / 'registry.json', now=NOW)


def change_receipt(root, transform, *, reattest=True):
    path = root / 'receipts' / '0.json'
    receipt = gate.read_json(path)
    transform(receipt)
    ref = write(path, receipt)
    if reattest:
        trusted = gate.read_json(root / 'trusted.json')
        trusted['receipts'][0] = ref
        registry = gate.read_json(root / 'registry.json')
        registry['attempts'][0].update({k: receipt[k] for k in ('status','failureAttribution','packDigest','corpusDigest','modelId')})
        registry['attempts'][0]['receiptDigest'] = ref['sha256']
        write(root / 'registry.json', registry)
        trusted['attemptRegistryDigest'] = gate.digest((root / 'registry.json').read_bytes())
        write(root / 'trusted.json', trusted)


def test_full_inventory_passes(campaign):
    result = validate(campaign)
    assert result['casesPassed'] == result['casesExpected'] == 252
    assert result['attempts'] == 252
    assert result['totalCostUsd'] == '7.56'


@pytest.mark.parametrize('field,value,match', [
    ('mode', 'mock', 'mock/test'),
    ('modelId', 'other', 'wrong modelId'),
    ('returnedModelId', 'other', 'wrong returnedModelId'),
    ('qualificationLockDigest', 'sha256:' + 'f' * 64, 'wrong qualificationLockDigest'),
    ('componentBindingDigest', 'sha256:' + 'f' * 64, 'wrong componentBindingDigest'),
    ('promptDigest', 'sha256:' + 'f' * 64, 'wrong promptDigest'),
    ('caseId', 'P99:A:local-docker', 'unknown case'),
    ('status', 'skipped', 'skip/unknown|registered outcome'),
    ('attempt', True, 'invalid attempt'),
    ('schemaVersion', True, 'unsupported receipt'),
    ('completedAt', '2026-10-14T09:00:00Z', 'time window'),
    ('startedAt', '2026-10-14T10:00:00', 'timezone'),
    ('namespace', 'ns-1', 'fresh namespace'),
])
def test_bad_receipt_rejected_even_if_controller_attested(campaign, field, value, match):
    change_receipt(campaign, lambda r: r.__setitem__(field, value))
    with pytest.raises(gate.Invalid, match=match):
        validate(campaign)


def test_forged_unattested_receipt_rejected(campaign):
    change_receipt(campaign, lambda r: r.__setitem__('status', 'pass'), reattest=False)
    path = campaign / 'receipts' / '0.json'
    path.write_text(path.read_text() + ' ', encoding='utf-8')
    with pytest.raises(gate.Invalid, match='hash mismatch'):
        validate(campaign)


def test_missing_case_cannot_be_removed_from_inventory(campaign):
    trusted = gate.read_json(campaign / 'trusted.json')
    ref = trusted['receipts'].pop()
    (campaign / 'receipts' / ref['path']).unlink()
    write(campaign / 'trusted.json', trusted)
    with pytest.raises(gate.Invalid, match='missing cases'):
        validate(campaign)


def test_unlisted_failed_attempt_cannot_be_hidden(campaign):
    write(campaign / 'receipts' / 'hidden.json', {'status': 'fail'})
    with pytest.raises(gate.Invalid, match='unlisted'):
        validate(campaign)


def test_duplicate_receipt_rejected(campaign):
    trusted = gate.read_json(campaign / 'trusted.json')
    trusted['receipts'].append(trusted['receipts'][0])
    write(campaign / 'trusted.json', trusted)
    with pytest.raises(gate.Invalid, match='duplicate receipt'):
        validate(campaign)


@pytest.mark.parametrize('change,match', [
    (lambda r: r['cleanup'].__setitem__('status', 'blocked'), 'cleanup incomplete'),
    (lambda r: r['costs'].__setitem__('modelUsd', -1), 'nonnegative'),
    (lambda r: r['costs'].__setitem__('modelUsd', True), 'not a boolean'),
    (lambda r: r['costs'].__setitem__('modelUsd', 0), 'cost does not match'),
    (lambda r: r['costs'].__setitem__('cleanupReservedUsd', 0), 'cleanup reserve'),
    (lambda r: r['usage'].__setitem__('modelTurns', 0), 'without actual execution'),
    (lambda r: r['usage'].__setitem__('executedActions', 999), 'invalid/excess'),
    (lambda r: r['transcript'].__setitem__('path', '../campaign.json'), 'unsafe evidence path'),
])
def test_execution_and_cleanup_fail_closed(campaign, change, match):
    change_receipt(campaign, change)
    with pytest.raises(gate.Invalid, match=match):
        validate(campaign)


@pytest.mark.parametrize('mutation,match', [
    (lambda r: r.__setitem__('producer', 'model'), 'self-report'),
    (lambda r: r['assertions'].pop(), 'missing independent assertions'),
    (lambda r: r['assertions'].append(r['assertions'][0]), 'duplicate assertion'),
    (lambda r: r['assertions'][0].__setitem__('status', 'fail'), 'failed assertion'),
    (lambda r: r.__setitem__('attempt', 2), 'different attempt'),
])
def test_oracle_cannot_lie_by_omission_or_replay(campaign, mutation, match):
    path = campaign / 'evidence' / 'oracle-0.json'
    report = gate.read_json(path)
    mutation(report)
    ref = write(path, report)
    change_receipt(campaign, lambda r: r.__setitem__('oracle', ref))
    with pytest.raises(gate.Invalid, match=match):
        validate(campaign)


def test_evidence_bytes_are_hash_bound(campaign):
    (campaign / 'evidence' / 'observed.json').write_text('{}', encoding='utf-8')
    with pytest.raises(gate.Invalid, match='hash mismatch'):
        validate(campaign)


def test_controller_attested_arbitrary_blob_is_not_an_oracle(campaign):
    ref = write(campaign / 'evidence' / 'arbitrary.json', {'observed': 'separate-controller'})
    report_path = campaign / 'evidence' / 'oracle-0.json'
    report = gate.read_json(report_path)
    report['assertions'][0]['evidence'] = ref
    updated = write(report_path, report)
    change_receipt(campaign, lambda r: r.__setitem__('oracle', updated))
    with pytest.raises(ValueError, match='malformed|missing'):
        validate(campaign)


def test_target_overlap_cannot_qualify(campaign):
    change_receipt(campaign, lambda r: r.__setitem__('completedAt', '2026-10-14T10:03:00Z'))
    with pytest.raises(gate.Invalid, match='serially'):
        validate(campaign)


def test_corpus_bytes_cannot_drift_under_unchanged_manifest(campaign):
    (campaign / 'fixture.json').write_text('{"data":"wrong"}', encoding='utf-8')
    with pytest.raises(gate.Invalid, match='corpus artifact bytes'):
        validate(campaign)


def test_new_campaign_id_cannot_erase_model_failure(campaign):
    registry = gate.read_json(campaign / 'registry.json')
    prior = {**registry['attempts'][0], 'campaignId': 'failed-earlier', 'status': 'fail', 'failureAttribution': 'model'}
    registry['attempts'].append(prior)
    write(campaign / 'registry.json', registry)
    trusted = gate.read_json(campaign / 'trusted.json')
    trusted['attemptRegistryDigest'] = gate.digest((campaign / 'registry.json').read_bytes())
    write(campaign / 'trusted.json', trusted)
    with pytest.raises(gate.Invalid, match='campaign reset forbidden'):
        validate(campaign)


def test_prior_campaign_spending_counts(campaign):
    ledger = gate.read_json(campaign / 'ledger.json')
    ledger['entries'].append({'id': 'prior', 'campaignId': 'prior-campaign', 'caseId': 'prior-case',
                             'attempt': 1, 'kind': 'infrastructure', 'amountUsd': '500',
                             'actualUsd': '500', 'state': 'settled', 'basisDigest': 'sha256:' + 'b' * 64})
    write(campaign / 'ledger.json', ledger)
    trusted = gate.read_json(campaign / 'trusted.json')
    trusted['budgetLedgerDigest'] = gate.digest((campaign / 'ledger.json').read_bytes())
    write(campaign / 'trusted.json', trusted)
    with pytest.raises(ValueError, match='budget|reserve'):
        validate(campaign)


def test_stale_campaign_rejected(campaign):
    with pytest.raises(gate.Invalid, match='stale'):
        gate.validate(campaign / 'pack.json', campaign / 'campaign.json', campaign / 'lock.json',
                      campaign / 'trusted.json', campaign / 'receipts', campaign / 'evidence', campaign / 'ledger.json', campaign / 'expectations.json', campaign / 'registry.json',
                      now=datetime(2026, 10, 15, tzinfo=timezone.utc))


def add_retry(root, attribution):
    original = gate.read_json(root / 'receipts' / '0.json')
    retry = copy.deepcopy(original)
    retry.update(attempt=2, namespace='fresh-retry', startedAt='2026-10-14T10:01:01Z',
                 completedAt='2026-10-14T10:01:59Z')
    report = gate.read_json(root / 'evidence' / original['oracle']['path'])
    report['attempt'] = 2
    retry['oracle'] = write(root / 'evidence' / 'retry-oracle.json', report)
    transcript = gate.read_json(root / 'evidence' / original['transcript']['path'])
    transcript['attempt'] = 2
    retry['transcript'] = write(root / 'evidence' / 'retry-transcript.json', transcript)
    def fail(receipt):
        receipt.update(status='fail', failureAttribution=attribution, oracle=None,
                       failureEvidence=receipt['transcript'])
    change_receipt(root, fail)
    ref = write(root / 'receipts' / 'retry.json', retry)
    ledger = gate.read_json(root / 'ledger.json')
    for row in list(ledger['entries']):
        if row['caseId'] == original['caseId']:
            updated = {**row, 'id': row['id'] + '/retry', 'attempt': 2}
            ledger['entries'].append(updated)
    write(root / 'ledger.json', ledger)
    trusted = gate.read_json(root / 'trusted.json')
    trusted['receipts'].append(ref)
    registry = gate.read_json(root / 'registry.json')
    registry['attempts'].append({**{k: retry[k] for k in ('campaignId','caseId','attempt','status','failureAttribution','packDigest','corpusDigest','modelId')},
                                 'receiptDigest': ref['sha256']})
    write(root / 'registry.json', registry)
    trusted['attemptRegistryDigest'] = gate.digest((root / 'registry.json').read_bytes())
    trusted['budgetLedgerDigest'] = gate.digest((root / 'ledger.json').read_bytes())
    write(root / 'trusted.json', trusted)


def test_one_attributed_infrastructure_retry_preserves_both_attempts(campaign):
    add_retry(campaign, 'infrastructure')
    result = validate(campaign)
    assert result['attempts'] == 253
    assert result['infrastructureRetries'] == 1
    assert result['totalCostUsd'] == '7.59'


@pytest.mark.parametrize('attribution', ['model', 'product'])
def test_model_and_product_failure_cannot_be_retried_to_green(campaign, attribution):
    add_retry(campaign, attribution)
    with pytest.raises(gate.Invalid, match='cannot be retried'):
        validate(campaign)


def test_unsettled_current_campaign_blocks(campaign):
    ledger = gate.read_json(campaign / 'ledger.json')
    ledger['entries'][0].update(state='reserved', actualUsd=None)
    write(campaign / 'ledger.json', ledger)
    trusted = gate.read_json(campaign / 'trusted.json')
    trusted['budgetLedgerDigest'] = gate.digest((campaign / 'ledger.json').read_bytes())
    write(campaign / 'trusted.json', trusted)
    with pytest.raises(gate.Invalid, match='unsettled'):
        validate(campaign)


def test_replaced_lock_file_is_not_accepted(campaign):
    lock = gate.read_json(campaign / 'lock.json')
    lock['components']['server']['source'] = 'f' * 40
    write(campaign / 'lock.json', lock)
    with pytest.raises(gate.Invalid, match='wrong qualification lock'):
        validate(campaign)


def test_pack_cannot_shrink_inventory_even_with_new_hash(campaign):
    pack = gate.read_json(campaign / 'pack.json')
    pack['scenarios'].pop()
    write(campaign / 'pack.json', pack)
    with pytest.raises(gate.Invalid, match='252-case inventory'):
        validate(campaign)


def test_cli_missing_inputs_fails_closed(tmp_path, capsys):
    flags = ['--pack', '--campaign', '--lock', '--trusted-inventory', '--receipts', '--evidence-root', '--budget-ledger', '--expectations', '--attempt-registry']
    argv = [part for flag in flags for part in (flag, str(tmp_path / 'absent'))]
    assert gate.main(argv) == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'fail'


def test_unsigned_and_signed_component_binding_is_stable():
    lock = {'components': {'a': 1}, 'contentDigests': {}, 'fixtures': [], 'platform': {'status': 'draft'}}
    released = {**lock, 'platform': {'status': 'released'}, 'signature': 'later'}
    assert gate.component_binding(lock) == gate.component_binding(released)
    released['fixtures'] = [{'repository': 'observed-by-gate', 'revision': 'a' * 40}]
    assert gate.component_binding(lock) == gate.component_binding(released)
    released['contentDigests'] = {'data': 'different'}
    assert gate.component_binding(lock) != gate.component_binding(released)
    released['contentDigests'] = lock['contentDigests']
    released['components'] = {'a': 2}
    assert gate.component_binding(lock) != gate.component_binding(released)


@pytest.mark.parametrize('payload', ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}'])
def test_ambiguous_json_rejected(tmp_path, payload):
    path = tmp_path / 'bad.json'
    path.write_text(payload, encoding='utf-8')
    with pytest.raises(gate.Invalid):
        gate.read_json(path)
