"""Mixed financial history controls; these fixtures do not qualify human sessions."""
import copy
from datetime import datetime, timezone
import json

import pytest

from agent_e2e_budget import BudgetError, update, validate
from agent_e2e_bootstrap import validate_import
from agent_e2e_interactive_budget import funding_binding
from agent_e2e_pack import PackError, digest_bytes
from agent_e2e_state import DurableExecution, preserve_history
from test_agent_e2e_budget import KEY, BASIS, reserve, reserve_operation
from test_agent_e2e_state import MemoryStore


@pytest.fixture
def interactive(tmp_path):
    path = tmp_path/'ledger.json'
    update(path, 'init', candidate_key=KEY)
    reserve_operation(path, amount='100')
    reserve_operation(path, rid='original-cleanup', amount='10', kind='cleanup')
    source = {'repository': 'honua-io/honua-release', 'workflow': '.github/workflows/agent-e2e-campaign.yml',
              'sourceSha': 'a'*40, 'runId': 123, 'runAttempt': 2}
    original = json.loads(path.read_text())
    artifact = {'schemaVersion': 1, 'candidateKey': KEY, 'ledger': original, 'source': source}
    raw_artifact = json.dumps(artifact, sort_keys=True).encode()
    imported = {'artifact': artifact, 'artifactDigest': digest_bytes(raw_artifact)}
    now = int(datetime.now(timezone.utc).timestamp())
    configuration = {'schemaVersion': 1, 'candidateKey': KEY, 'ledger': copy.deepcopy(original), 'history': {'path': 'original-history.json'},
        'expiresAt': datetime.fromtimestamp(now+1200, timezone.utc).isoformat(), 'liveLiabilityReferences': [{'path': 'original-liability.json'}]}
    raw_configuration = json.dumps(configuration).encode()
    reads = []
    def read(sha, name):
        reads.append((sha, name))
        return raw_configuration
    binding, retained = funding_binding(imported, source_reader=read, now_epoch=now)
    assert reads == [('a'*40, 'certification/agent-e2e/bootstrap.json')]
    assert retained == raw_configuration
    update(path, 'amend-foss4g', candidate_key=KEY)
    update(path, 'enable-interactive-accounting', candidate_key=KEY, binding=binding)
    return path, binding, now, imported, configuration


def acquire(path, binding, now, execution='execution-original', **overrides):
    return update(path, 'acquire-interactive-attempt', candidate_key=KEY,
        **({'execution_id': execution, 'target_id': 'target-original', 'workspace_id': 'workspace-original',
            'turn_id': execution+'-turn', 'owner_token': 'a'*32, 'expires_epoch': binding['originalExpiresEpoch'],
            'now_epoch': now} | overrides))


def charge(path, execution='execution-original', rid='interactive-infra', kind='infrastructure', amount='10'):
    return update(path, 'reserve-interactive', candidate_key=KEY, execution_id=execution,
        target_id='target-original', workspace_id='workspace-original', turn_id=execution+'-turn',
        owner_token='a'*32, reservation_id=rid, kind=kind, amount=amount, basis_digest=BASIS)


def test_mixed_costs_have_one_ceiling_and_preserve_all_original_rows(interactive):
    path, binding, now, imported, _ = interactive
    acquire(path, binding, now)
    charge(path, amount='1650')
    reserve(path, 'case-infra', '40', kind='infrastructure')
    before = path.read_bytes()
    with pytest.raises(BudgetError, match='reserve'):
        reserve(path, 'case-over-cap', '11')
    assert path.read_bytes() == before
    with pytest.raises(BudgetError, match='reserve'):
        charge(path, rid='interactive-over-cap', amount='11')
    value = json.loads(path.read_text())
    assert value['operations'] == imported['artifact']['ledger']['operations']
    assert value['policyAmendment']['priorLedger'] == imported['artifact']['ledger']
    assert len(value['entries']) == 1 and len(value['interactiveEntries']) == 1
    assert validate(value)['liabilityUsd'] == '1800'


def test_combined_concurrent_admission_limit_and_original_execution_identity(interactive):
    path, binding, now, *_ = interactive
    for number in range(3):
        update(path, 'acquire-paid-attempt', candidate_key=KEY, admission_id='case-'+str(number),
            owner_token='b'*32, expires_epoch=now+600, now_epoch=now,
            campaign_id='original-campaign', case_id='P'+str(number), attempt=1, campaign_mode='development')
    acquire(path, binding, now)
    original = path.read_bytes()
    with pytest.raises(BudgetError, match='concurrent'):
        acquire(path, binding, now, execution='execution-fifth')
    assert path.read_bytes() == original
    # Lost acquire acknowledgement reuses the same exact admission, never buys
    # another worker/model attempt or extends the retained absolute expiry.
    acquire(path, binding, now+1)
    assert path.read_bytes() == original
    with pytest.raises(BudgetError):
        acquire(path, binding, now+1, owner_token='c'*32)
    with pytest.raises(BudgetError):
        acquire(path, binding, now+1, expires_epoch=binding['originalExpiresEpoch']+1)


def test_global_reservation_ids_cannot_be_reused_between_case_operation_and_human(interactive):
    path, binding, now, *_ = interactive
    acquire(path, binding, now)
    for duplicate in ('bootstrap-infra', 'case-model'):
        if duplicate == 'case-model': reserve(path, duplicate, '1')
        before = path.read_bytes()
        with pytest.raises(BudgetError, match='duplicate'):
            charge(path, rid=duplicate)
        assert path.read_bytes() == before


def test_unknown_costs_and_expiry_keep_original_hold_and_sequential_work_is_not_reset(interactive):
    path, binding, now, *_ = interactive
    acquire(path, binding, now)
    charge(path)
    charge(path, rid='interactive-cleanup', kind='cleanup', amount='1')
    charge(path, rid='interactive-model', kind='model', amount='2')
    release = {'candidate_key': KEY, 'execution_id': 'execution-original', 'owner_token': 'a'*32,
        'cleanup_status': 'pass', 'reservation_ids': {'infrastructure': 'interactive-infra', 'cleanup': 'interactive-cleanup'}}
    with pytest.raises(BudgetError, match='settle'):
        update(path, 'release-interactive-attempt', **release)
    for rid, cost in [('interactive-infra', '9'), ('interactive-cleanup', '0.7'), ('interactive-model', '1.5')]:
        update(path, 'settle', candidate_key=KEY, reservation_id=rid, amount=cost)
    update(path, 'release-interactive-attempt', **release)
    first = copy.deepcopy(json.loads(path.read_text())['interactivePaidAdmissions'][0])
    acquire(path, binding, now+5, execution='execution-next')
    value = json.loads(path.read_text())
    assert value['interactivePaidAdmissions'][0] == first
    assert len(value['interactivePaidAdmissions']) == 2
    assert validate(value)['liabilityUsd'] == '121.2'
    with pytest.raises(BudgetError, match='expired|stale'):
        acquire(path, binding, binding['originalExpiresEpoch']+1, execution='execution-after-expiry')


def test_interactive_overrun_is_retained_and_blocks_both_families(interactive):
    path, binding, now, *_ = interactive
    acquire(path, binding, now)
    charge(path, amount='1')
    with pytest.raises(BudgetError, match='exceeded'):
        update(path, 'settle', candidate_key=KEY, reservation_id='interactive-infra', amount='2')
    assert json.loads(path.read_text())['interactiveEntries'][0]['actualUsd'] == '2'
    with pytest.raises(BudgetError, match='exceeded'):
        reserve(path, 'case-after-overrun', '1')
    with pytest.raises(BudgetError, match='exceeded'):
        charge(path, rid='human-after-overrun', amount='1')


@pytest.mark.parametrize('change', ['wrong-source', 'wrong-ledger', 'missing-expiry', 'expired'])
def test_original_source_binding_refuses_replacement_or_invented_expiry(interactive, change):
    _, _, now, imported, configuration = interactive
    if change == 'wrong-source': imported['artifact']['source']['sourceSha'] = 'b'*40
    elif change == 'wrong-ledger': configuration['ledger']['operations'][0]['amountUsd'] = '101'
    elif change == 'missing-expiry': configuration.pop('expiresAt')
    else: configuration['expiresAt'] = datetime.fromtimestamp(now-1, timezone.utc).isoformat()
    raw = json.dumps(configuration).encode()
    def read(sha, path):
        if sha != 'a'*40: raise PackError('original source configuration unavailable')
        return raw
    with pytest.raises(PackError): funding_binding(imported, source_reader=read, now_epoch=now)


def test_durable_install_and_restart_keep_original_source_nonce_and_unsettled_cost(interactive, tmp_path):
    path, binding, now, imported, configuration = interactive
    prior = json.loads(path.read_text())
    for field in ('interactiveAccounting', 'interactiveEntries', 'interactivePaidAdmissions'): prior.pop(field)
    store = MemoryStore({'ledger': prior, 'bootstrapImport': imported, 'journals': {}})
    store.candidate = KEY
    local = tmp_path/'installer.json'
    execution = DurableExecution(store, local)
    try:
        observed = execution.enable_interactive_accounting(source_reader=lambda sha, name: json.dumps(configuration).encode(), now_epoch=now)
        assert observed == binding
        execution.update(local, 'acquire-interactive-attempt', candidate_key=KEY,
            execution_id='execution-original', target_id='target-original', workspace_id='workspace-original',
            turn_id='execution-original-turn', owner_token='a'*32, expires_epoch=binding['originalExpiresEpoch'], now_epoch=now)
        execution.update(local, 'reserve-interactive', candidate_key=KEY, execution_id='execution-original',
            target_id='target-original', workspace_id='workspace-original', turn_id='execution-original-turn',
            owner_token='a'*32, reservation_id='unknown-response', kind='model', amount='3', basis_digest=BASIS)
    finally: execution.close()
    original = copy.deepcopy(store.state)
    recovered = DurableExecution(store, tmp_path/'recovered.json')
    try:
        def forbidden(*args): raise AssertionError('restart must replay retained source, not fetch a replacement')
        assert recovered.enable_interactive_accounting(source_reader=forbidden) == binding
        assert recovered.state == original
        assert recovered.state['ledger']['interactiveEntries'][0]['state'] == 'reserved'
        assert recovered.state['ledger']['interactivePaidAdmissions'][0]['ownerToken'] == 'a'*32
    finally: recovered.close()


@pytest.mark.parametrize('change', ['remove-charge', 'remove-admission', 'extend-expiry', 'replace-source'])
def test_history_cannot_reset_or_rewrite_any_interactive_original(interactive, change):
    path, binding, now, *_ = interactive
    acquire(path, binding, now)
    charge(path)
    original = {'ledger': json.loads(path.read_text())}
    replacement = copy.deepcopy(original)
    if change == 'remove-charge': replacement['ledger']['interactiveEntries'].clear()
    elif change == 'remove-admission': replacement['ledger']['interactivePaidAdmissions'].clear()
    elif change == 'extend-expiry': replacement['ledger']['interactivePaidAdmissions'][0]['expiresEpoch'] += 1
    else: replacement['ledger']['interactiveAccounting']['configurationSha256'] = 'sha256:'+'d'*64
    with pytest.raises(PackError): preserve_history(original, replacement)


def test_unmarked_or_legacy_ledger_cannot_hide_interactive_costs(interactive):
    path, binding, now, *_ = interactive
    acquire(path, binding, now)
    charge(path)
    value = json.loads(path.read_text())
    value.pop('interactiveAccounting')
    with pytest.raises(BudgetError): validate(value)
    original = copy.deepcopy(value['policyAmendment']['priorLedger'])
    original['interactiveEntries'] = value['interactiveEntries']
    with pytest.raises(BudgetError): validate(original)


def test_import_cannot_turn_active_interactive_history_into_a_fresh_generation(interactive):
    path, binding, now, imported, _ = interactive
    acquire(path, binding, now)
    replacement = copy.deepcopy(imported)
    replacement['artifact']['ledger'] = json.loads(path.read_text())
    with pytest.raises(PackError, match='reinitialize interactive'):
        validate_import(replacement, KEY)


def test_legacy_case_mode_cannot_duplicate_an_admission_with_different_mode(interactive):
    path, _, now, *_ = interactive
    values = {'candidate_key': KEY, 'owner_token': 'b'*32, 'expires_epoch': now+600, 'now_epoch': now,
        'campaign_id': 'original-campaign', 'case_id': 'P01:A:local-docker', 'attempt': 1}
    update(path, 'acquire-paid-attempt', admission_id='original', campaign_mode='development', **values)
    with pytest.raises(BudgetError, match='readmitted'):
        update(path, 'acquire-paid-attempt', admission_id='replacement', campaign_mode='qualification', **values)
