"""Offline lease/admission contracts; fixture handlers are never live evidence."""
import base64
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
import yaml

from agent_e2e_backend import InProcessBackend, PROTOCOL
from agent_e2e_pack import PackError, canonical_digest, digest_bytes
from agent_e2e_state import DurableExecution, preserve_history


CANDIDATE = 'sha256:'+'a'*64
CASE = 'P10:A:local-docker'
CAMPAIGN = {'componentBindingDigest': CANDIDATE, 'qualificationLockDigest': 'sha256:'+'b'*64,
            'campaignId': 'offline-campaign', 'campaignMode': 'development', 'knowledge': {'digest': 'offline'}}
CONTEXT = {**CAMPAIGN, 'caseId': CASE, 'attempt': 1}


class Store:
    candidate = CANDIDATE

    def __init__(self):
        self.state = None
        self.acquisitions = self.releases = self.renewals = 0

    def acquire(self):
        self.acquisitions += 1
        return copy.deepcopy(self.state)

    def commit(self, state):
        preserve_history(self.state, state)
        self.state = copy.deepcopy(state)

    def renew(self):
        self.renewals += 1

    def release(self):
        self.releases += 1


def configured(tmp_path, script='raise RuntimeError("must never import during preflight")\n'):
    entry = tmp_path/'entry.py'
    entry.write_text(script)
    settings = tmp_path/'execution.json'
    settings.write_text(json.dumps({'schemaVersion': 1, 'configuration': {'public': 'fixture'}, 'contract': {
        'componentBindingDigest': CANDIDATE, 'qualificationLockDigest': CAMPAIGN['qualificationLockDigest'],
        'capabilities': ['scenario-setup', 'independent-oracles'], 'cases': {CASE: {
            'maximumInfrastructureUsd': '0.80', 'maximumCleanupUsd': '0.20',
            'liabilityBasisDigest': 'sha256:'+'c'*64}},
        'expiresAt': (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()}}))
    config = {'kind': 'in-process', 'script': str(entry), 'scriptSha256': digest_bytes(entry.read_bytes()),
              'configuration': {'path': str(settings), 'sha256': digest_bytes(settings.read_bytes())}}
    return InProcessBackend(config, source_root=tmp_path), config


@pytest.fixture
def execution(tmp_path):
    store = Store()
    durable = DurableExecution(store, tmp_path/'ledger.json', initialize=True)
    try:
        yield durable
    finally:
        durable.close()


def reserve(execution, *, attempt=1, mode='development'):
    for kind in ('infrastructure', 'cleanup'):
        execution.update(execution.path, 'reserve', candidate_key=CANDIDATE,
            reservation_id=f'{kind}-{attempt}', campaign_id=CAMPAIGN['campaignId'],
            case_id=CASE, attempt=attempt, campaign_mode=mode, kind=kind,
            amount='0.20', basis_digest='sha256:'+'c'*64)


def bind(backend, execution, tmp_path):
    backend.bind_execution(execution, CAMPAIGN, tmp_path)


def test_preflight_only_reads_reviewed_bytes_without_import_or_lease(tmp_path):
    backend, _ = configured(tmp_path)
    response = backend.call('campaign_preflight', {'campaign': CAMPAIGN})
    assert response['status'] == 'configured' and response['readOnly'] is True
    assert response['caseIds'] == [CASE] and backend.module is backend.durable is None
    assert backend.preflight(CONTEXT)['maximumCleanupUsd'] == '0.20'
    assert backend.module is None


def test_preflight_rejects_wrong_candidate_or_missing_case_without_import(tmp_path):
    backend, _ = configured(tmp_path)
    with pytest.raises(PackError, match='differs from candidate'):
        backend.preflight({**CONTEXT, 'componentBindingDigest': 'sha256:'+'f'*64})
    with pytest.raises(PackError, match='liability quote'):
        backend.preflight({**CONTEXT, 'caseId': 'P10:B:local-docker'})
    assert backend.module is None


def test_frozen_relative_inputs_relocate_to_private_execution_source_without_rebinding(tmp_path):
    import shutil
    original, relocated = tmp_path/'github-checkout', tmp_path/'private-controller/source'
    original.mkdir(); relocated.mkdir(parents=True)
    _, absolute = configured(original)
    config = {**absolute, 'script': 'entry.py', 'configuration': {**absolute['configuration'], 'path': 'execution.json'}}
    frozen = json.loads(json.dumps(config))
    shutil.copytree(original, relocated, dirs_exist_ok=True)
    github = InProcessBackend(config, source_root=original)
    controller = InProcessBackend(frozen, source_root=relocated)
    assert github.config == controller.config == frozen
    assert github.script.parent == original and controller.script.parent == relocated
    assert github.inspect(CAMPAIGN) == controller.inspect(CAMPAIGN)
    assert 'github-checkout' not in json.dumps(frozen)


def test_production_config_cannot_enable_absolute_traversal_or_symlink_sources(tmp_path):
    _, config = configured(tmp_path)
    with pytest.raises(PackError, match='must be relative'):
        InProcessBackend(config)
    for field, value in [('script', '../entry.py'), ('configuration', '../execution.json')]:
        changed = copy.deepcopy(config)
        if field == 'script': changed[field] = value
        else: changed[field]['path'] = value
        with pytest.raises(PackError):
            InProcessBackend(changed, source_root=tmp_path)
    (tmp_path/'entry-link.py').symlink_to(tmp_path/'entry.py')
    with pytest.raises(PackError, match='symlinked'):
        InProcessBackend({**config, 'script': 'entry-link.py'}, source_root=tmp_path)


def test_mutations_cannot_load_code_without_existing_lease_and_liabilities(tmp_path, execution):
    backend, _ = configured(tmp_path)
    with pytest.raises(PackError, match='producer-owned durable lease'):
        backend.call('prepare', CONTEXT)
    bind(backend, execution, tmp_path)
    with pytest.raises(PackError, match='retained infrastructure and cleanup'):
        backend.call('prepare', CONTEXT)
    assert backend.module is None and execution.store.acquisitions == 1


@pytest.mark.parametrize('key,value', [('campaignId', 'another'), ('campaignMode', 'qualification'),
    ('qualificationLockDigest', 'sha256:'+'f'*64), ('knowledge', {}), ('attempt', True)])
def test_mutations_bind_complete_frozen_context(tmp_path, execution, key, value):
    backend, _ = configured(tmp_path)
    bind(backend, execution, tmp_path)
    reserve(execution)
    with pytest.raises(PackError):
        backend.call('prepare', {**CONTEXT, key: value})
    assert backend.module is None


def test_wrong_budget_mode_cannot_fund_backend_effects(tmp_path, execution):
    backend, _ = configured(tmp_path)
    bind(backend, execution, tmp_path)
    reserve(execution, mode='qualification')
    with pytest.raises(PackError, match='liability'):
        backend.call('prepare', CONTEXT)
    assert backend.module is None


def test_handler_receives_exact_producer_lease_and_retains_baseline_before_effects(tmp_path, execution):
    backend, _ = configured(tmp_path, '''
def handle(operation, payload, *, durable, campaign, bundle, configuration, timeout):
    global RECEIVED_DURABLE
    RECEIVED_DURABLE = durable
    baseline = {'candidateKey': campaign['componentBindingDigest'], 'nativeDatabaseOid': 123}
    ack = durable.retain_fixture_baseline(payload, baseline)
    assert durable.store.state['fixtureBaselines'][ack['key']] == baseline
    return {'retained': ack}
''')
    bind(backend, execution, tmp_path)
    reserve(execution)
    result = backend.call('prepare', CONTEXT)
    assert result['protocol'] == PROTOCOL
    assert backend.module.RECEIVED_DURABLE is execution
    assert execution.store.acquisitions == 1
    assert execution.store.state['fixtureBaselines'][result['retained']['key']]['nativeDatabaseOid'] == 123


def test_console_admission_runs_before_case_effects_and_stays_out_of_public_inputs(tmp_path, execution, monkeypatch):
    from types import SimpleNamespace
    backend, _ = configured(tmp_path, '''
def handle(operation, payload, **kwargs):
    assert 'consoleConversation' not in kwargs['configuration']['cases'][payload['caseId']]
    return {'publicInputs': {'files': []}}
''')
    backend.settings['configuration']['cases'] = {CASE: {'consoleConversation': {'original': 'reviewed-reference'}}}
    bind(backend, execution, tmp_path); reserve(execution)
    calls = []
    def admission(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(append=lambda payload: {'status': 'delivered', 'evidenceKind': 'conversation'})
    monkeypatch.setattr('agent_e2e_console_conversation.Conversation', admission)
    result = backend.call('prepare', CONTEXT)
    assert len(calls) == 1 and calls[0]['durable'] is execution
    assert result['consoleConversation'] is True and result['publicInputs'] == {'files': []}
    assert backend.settings['configuration']['cases'][CASE]['consoleConversation'] == {'original': 'reviewed-reference'}
    result = backend.call('conversation_response', {**CONTEXT, 'response': {'original': 'model-payload'}})
    assert result['evidenceKind'] == 'conversation'


def test_failed_console_turn_admission_does_not_load_or_start_case_backend(tmp_path, execution, monkeypatch):
    backend, _ = configured(tmp_path)
    backend.settings['configuration']['cases'] = {CASE: {'consoleConversation': {}}}
    bind(backend, execution, tmp_path); reserve(execution)
    def refuse(**kwargs):
        raise PackError('original human turn unavailable')
    monkeypatch.setattr('agent_e2e_console_conversation.Conversation', refuse)
    with pytest.raises(PackError, match='original human turn'):
        backend.call('prepare', CONTEXT)
    assert backend.module is None
    assert all(row['state'] == 'reserved' for row in execution.state['ledger']['entries'])


def test_paid_prepare_loads_pinned_handler_and_rechecks_script(tmp_path, execution):
    backend, _ = configured(tmp_path, 'def handle(*args, **kwargs):\n    return {"observed": "offline-handler"}\n')
    bind(backend, execution, tmp_path)
    reserve(execution)
    backend.script.write_text('raise RuntimeError("changed source")\n')
    with pytest.raises(PackError, match='changed after inspection'):
        backend.call('prepare', CONTEXT)
    assert backend.module is None


def test_backend_deadline_keeps_both_liabilities_on_unknown_effects(tmp_path, execution):
    backend, _ = configured(tmp_path, 'import time\ndef handle(*args, **kwargs):\n    time.sleep(5)\n')
    bind(backend, execution, tmp_path)
    reserve(execution)
    with pytest.raises(PackError, match='deadline exceeded'):
        backend.call('prepare', CONTEXT, timeout=0.02)
    assert all(row['state'] == 'reserved' for row in execution.store.state['ledger']['entries'])


def test_backend_import_is_also_deadline_bounded(tmp_path, execution):
    backend, _ = configured(tmp_path, 'import time\ntime.sleep(5)\n')
    bind(backend, execution, tmp_path)
    reserve(execution)
    with pytest.raises(PackError, match='deadline exceeded'):
        backend.call('prepare', CONTEXT, timeout=0.02)
    assert backend.module is None


def test_export_recovery_requires_exact_settled_attempt(tmp_path, execution):
    backend, _ = configured(tmp_path, 'def handle(*args, **kwargs):\n    return {"evidence": {}}\n')
    bind(backend, execution, tmp_path)
    reserve(execution)
    identity = {key: CONTEXT[key] for key in ('campaignId', 'caseId', 'attempt')}
    execution.state['attemptArtifacts'] = {canonical_digest(identity): {'receipt': identity, 'evidence': {}}}
    execution._persist()
    payload = {**identity, 'campaign': CAMPAIGN}
    with pytest.raises(PackError, match='settled evidence recovery'):
        backend.call('export_attempt', payload)
    for kind in ('infrastructure', 'cleanup'):
        execution.update(execution.path, 'settle', candidate_key=CANDIDATE, reservation_id=kind+'-1', amount='0.10')
    with pytest.raises(PackError, match='settled evidence recovery'):
        backend.call('export_attempt', {**payload, 'attempt': 2})
    assert backend.module is None
    assert backend.call('export_attempt', payload)['evidence'] == {}


def test_retry_uses_retained_independent_bytes_without_import_or_new_liability(tmp_path, execution):
    backend, _ = configured(tmp_path)
    bind(backend, execution, tmp_path)
    assert backend.call('authorize_retry', {**CONTEXT, 'attempt': 2})['authorized'] is False
    identity = {key: CONTEXT[key] for key in ('campaignId', 'caseId', 'attempt')}
    key = canonical_digest(identity)
    diagnosis = b'independent offline probe fixture'
    digest = digest_bytes(diagnosis)
    receipt = {**identity, 'failureAttribution': 'infrastructure', 'status': 'fail',
               'cleanup': {'status': 'pass'}, 'failureEvidence': {'path': 'probe.json', 'sha256': digest}}
    execution.state['attemptArtifacts'] = {key: {'receipt': receipt,
        'evidence': {'probe.json': base64.b64encode(diagnosis).decode()}}}
    execution.state['journals'] = {key: [{'event': 'diagnosis', 'attribution': 'infrastructure',
        'basis': 'independent-infrastructure-probe', 'evidenceDigest': digest}]}
    result = backend.call('authorize_retry', {**CONTEXT, 'attempt': 2})
    assert result['authorized'] is True and result['previousAttemptDigest']
    assert backend.module is None and execution.state['ledger']['entries'] == []
    execution.state['attemptArtifacts'][key]['evidence']['probe.json'] = base64.b64encode(b'changed').decode()
    assert backend.call('authorize_retry', {**CONTEXT, 'attempt': 2})['authorized'] is False


def test_fixture_intent_and_evidence_are_exact_create_only_durable_records(execution):
    reserve(execution)
    baseline = {'candidateKey': CANDIDATE, 'nativeDatabaseOid': 123, 'tables': []}
    ack = execution.retain_fixture_baseline(CONTEXT, baseline)
    assert ack['sha256'] == canonical_digest(baseline)
    with pytest.raises(PackError, match='already retained'):
        execution.retain_fixture_baseline(CONTEXT, baseline)
    record = {'candidateKey': CANDIDATE, 'baseline': baseline, 'ownedTableOid': 456}
    with pytest.raises(PackError, match='independent baseline'):
        execution.retain_fixture_ownership(CONTEXT, {**record, 'baseline': {}})
    execution.retain_fixture_ownership(CONTEXT, record)
    with pytest.raises(PackError, match='already retained'):
        execution.retain_fixture_ownership(CONTEXT, record)
    with pytest.raises(PackError, match='exact retained'):
        execution.require_fixture_ownership(CONTEXT, {**record, 'ownedTableOid': 789})
    evidence = {'candidateKey': CANDIDATE, 'ownershipDigest': canonical_digest(record), 'absence': True}
    with pytest.raises(PackError, match='before-effect intent'):
        execution.retain_fixture_cleanup(CONTEXT, 'observation', evidence)
    execution.retain_fixture_cleanup(CONTEXT, 'intent', evidence)
    execution.retain_fixture_cleanup(CONTEXT, 'observation', evidence)
    with pytest.raises(PackError, match='stage already retained'):
        execution.retain_fixture_cleanup(CONTEXT, 'intent', evidence)
    for field in ('fixtureBaselines', 'fixtureOwnership', 'fixtureCleanup'):
        changed = copy.deepcopy(execution.store.state)
        changed[field].clear()
        with pytest.raises(PackError, match='retained campaign evidence'):
            preserve_history(execution.store.state, changed)


def test_fixture_records_cannot_be_created_before_liability_or_after_lease_close(execution):
    baseline = {'candidateKey': CANDIDATE}
    with pytest.raises(PackError, match='liability'):
        execution.retain_fixture_baseline(CONTEXT, baseline)
    reserve(execution)
    execution.stop.set()
    with pytest.raises(PackError, match='lease is unavailable'):
        execution.retain_fixture_baseline(CONTEXT, baseline)


def test_fixture_owner_handoff_is_exact_new_oid_request_retained_once_before_provisioner(execution):
    reserve(execution)
    baseline = {'candidateKey': CANDIDATE, 'database': {'databaseOid': 42},
                'snapshot': {'relations': [{'oid': 1, 'schema': 'honua_data', 'name': 'shared', 'owner': 'app'}]}}
    execution.retain_fixture_baseline(CONTEXT, baseline)
    request = {'schemaVersion': 1, 'candidateKey': CANDIDATE, 'database': baseline['database'],
        'baselineDigest': canonical_digest(baseline), 'expectedCleanupRole': 'fixture_cleanup', 'tables': [
            {'oid': number, 'schema': 'honua_data', 'name': 'created_'+name, 'owner': 'fixture_app'}
            for number, name in enumerate(('parcels', 'flood_zones', 'shelters'), 10)]}
    changed = copy.deepcopy(request)
    changed['tables'][0]['oid'] = 1
    with pytest.raises(PackError, match='preexisting'):
        execution.retain_fixture_transfer_request(CONTEXT, changed)
    assert not execution.state.get('fixtureTransferRequests')
    ack = execution.retain_fixture_transfer_request(CONTEXT, request)
    assert execution.store.state['fixtureTransferRequests'][ack['key']] == request
    assert ack['sha256'] == canonical_digest(request)
    with pytest.raises(PackError, match='already retained'):
        execution.retain_fixture_transfer_request(CONTEXT, request)
    changed_state = copy.deepcopy(execution.store.state)
    changed_state['fixtureTransferRequests'].clear()
    with pytest.raises(PackError, match='retained campaign evidence'):
        preserve_history(execution.store.state, changed_state)
def test_postgres_ci_explicitly_provisions_actual_fixture_and_rejects_skips():
    root = Path(__file__).resolve().parents[1]
    job = yaml.safe_load((root/'.github/workflows/agent-e2e-contracts.yml').read_text())['jobs']['postgres-fixture-contracts']
    assert job['env']['HONUA_AGENT_POSTGRES_TESTS'] == '1'
    assert '@sha256:' in job['env']['HONUA_AGENT_POSTGRES_TEST_IMAGE']
    commands = '\n'.join(step.get('run', '') for step in job['steps'])
    assert 'psycopg[binary]' in commands and 'docker pull "$HONUA_AGENT_POSTGRES_TEST_IMAGE"' in commands
    assert 'test_agent_e2e_fixture_database.py' in commands
    assert "suite.get('tests', 0)" in commands and "suite.get('skipped', 0)" in commands
    assert '>= 12' in commands and '== 0' in commands
