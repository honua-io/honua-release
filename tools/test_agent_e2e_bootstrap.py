"""Offline bootstrap custody/accounting checks; never live qualification proof."""
import copy
import json

import pytest

from agent_e2e_bootstrap import verify_import, validate_import
from agent_e2e_budget import BudgetError, update, validate
from agent_e2e_pack import PackError, digest_bytes
from agent_e2e_state import DurableExecution, preserve_history
from test_agent_e2e_state import MemoryStore
from test_verify_agent_e2e_campaign import verified_statement
from verify_agent_e2e_campaign import REPOSITORY, WORKFLOW


KEY = MemoryStore.candidate
SOURCE = {'repository':REPOSITORY, 'workflow':WORKFLOW, 'sourceSha':'a'*40, 'runId':123, 'runAttempt':2}


@pytest.fixture
def bootstrap(tmp_path):
    ledger = tmp_path/'pre-bootstrap-ledger.json'
    update(ledger, 'init', candidate_key=KEY)
    for kind, amount in [('infrastructure','440'), ('cleanup','10')]:
        update(ledger, 'reserve-operation', candidate_key=KEY, reservation_id='bootstrap-'+kind,
            operation_id='bootstrap', purpose='bootstrap', kind=kind, amount=amount, basis_digest='sha256:'+'b'*64)
    artifact = {'schemaVersion':1, 'candidateKey':KEY, 'ledger':json.loads(ledger.read_text()), 'source':SOURCE}
    path = tmp_path/'bootstrap.json'
    path.write_text(json.dumps(artifact), encoding='utf-8')
    return path, {'artifactDigest':digest_bytes(path.read_bytes()), 'artifact':artifact}


def verified_invoke(path):
    def invoke(args):
        if args[:2] == ['gh','api']:
            return {'id':123, 'run_attempt':2, 'head_sha':'a'*40, 'head_branch':'trunk', 'path':WORKFLOW,
                    'status':'completed', 'conclusion':'success', 'event':'workflow_dispatch'}
        return verified_statement(path)
    return invoke


def test_import_authenticates_exact_bootstrap_producer(bootstrap):
    path, value = bootstrap
    calls = []
    def invoke(args):
        calls.append(args)
        return verified_invoke(path)(args)
    assert verify_import(path, value['artifactDigest'], KEY, 'a'*40, 123, 2, invoke=invoke) == value
    assert calls[1][:3] == ['gh', 'attestation', 'verify']
    assert '--deny-self-hosted-runners' in calls[1]


@pytest.mark.parametrize('field,value', [('digest','sha256:'+'c'*64), ('candidate','sha256:'+'c'*64),
                                      ('source','c'*40), ('run',124), ('attempt',3)])
def test_bootstrap_cannot_choose_its_own_trusted_identity(bootstrap, field, value):
    path, bundle = bootstrap
    args = {'digest':bundle['artifactDigest'], 'candidate':KEY, 'source':'a'*40, 'run':123, 'attempt':2}
    args[field] = value
    with pytest.raises(PackError):
        verify_import(path, *args.values(), invoke=lambda args:pytest.fail('untrusted input reached authentication'))


def test_failed_producer_and_foreign_attestation_refuse(bootstrap):
    path, value = bootstrap
    def failed(args):
        result = verified_invoke(path)(args)
        result['conclusion'] = 'failure'
        return result
    with pytest.raises(PackError, match='successful'):
        verify_import(path, value['artifactDigest'], KEY, 'a'*40, 123, 2, invoke=failed)
    def foreign(args):
        result = verified_invoke(path)(args)
        if args[:3] == ['gh','attestation','verify']:
            result[0]['verificationResult']['signature']['certificate']['runnerEnvironment'] = 'self-hosted'
        return result
    with pytest.raises(ValueError, match='identity mismatch'):
        verify_import(path, value['artifactDigest'], KEY, 'a'*40, 123, 2, invoke=foreign)


def test_initial_import_and_recovery_never_reset_bootstrap_liability(tmp_path, bootstrap):
    _, value = bootstrap
    store = MemoryStore()
    destination = tmp_path/'controller/ledger.json'
    execution = DurableExecution(store, destination, initialize=True, bootstrap=value)
    try:
        assert execution.state['bootstrapImport'] == value
        assert execution.state['ledger'] == value['artifact']['ledger']
        assert execution.state['journals'] == {}
        with pytest.raises(BudgetError, match='reserve'):
            execution.update(destination, 'reserve', candidate_key=KEY, reservation_id='too-much-model',
                campaign_id='new-campaign', case_id='P10:A:local-docker', attempt=1,
                kind='model', amount='11', basis_digest='sha256:'+'b'*64)
    finally:
        execution.close()
    resumed = DurableExecution(store, tmp_path/'replacement/ledger.json')
    try:
        assert validate(resumed.state['ledger'])['liabilityUsd'] == '450'
        assert resumed.state['bootstrapImport'] == value
    finally:
        resumed.close()


def test_import_cannot_overwrite_existing_remote_or_local_history(tmp_path, bootstrap):
    _, value = bootstrap
    path = tmp_path/'ledger.json'
    store = MemoryStore()
    execution = DurableExecution(store, path, initialize=True, bootstrap=value)
    execution.close()
    before = copy.deepcopy(store.state)
    with pytest.raises(PackError, match='already exists'):
        DurableExecution(store, tmp_path/'other.json', initialize=True, bootstrap=value)
    assert store.state == before
    with pytest.raises(PackError, match='existing local ledger'):
        DurableExecution(MemoryStore(), path, initialize=True, bootstrap=value)
    with pytest.raises(PackError, match='explicit initialization'):
        DurableExecution(MemoryStore(), tmp_path/'third.json', bootstrap=value)


def test_bootstrap_binding_and_operation_history_are_immutable(bootstrap):
    _, value = bootstrap
    before = {'bootstrapImport':value, 'ledger':copy.deepcopy(value['artifact']['ledger'])}
    for change in ('bootstrap', 'delete-operation', 'reprice', 'reclassify'):
        after = copy.deepcopy(before)
        if change == 'bootstrap':
            del after['bootstrapImport']
        elif change == 'delete-operation':
            after['ledger']['operations'].pop()
        elif change == 'reprice':
            after['ledger']['operations'][0]['amountUsd'] = '1'
        else:
            after['ledger']['operations'][0]['purpose'] = 'retention'
        with pytest.raises(PackError):
            preserve_history(before, after)
    settled = copy.deepcopy(before)
    settled['ledger']['operations'][0].update(state='settled', actualUsd='3')
    preserve_history(before, settled)
    with pytest.raises(PackError, match='charge cannot change'):
        preserve_history(settled, before)
    with pytest.raises(PackError, match='first state'):
        preserve_history({'ledger':before['ledger']}, before)


def test_bootstrap_requires_both_infrastructure_and_cleanup(bootstrap):
    _, value = bootstrap
    changed = copy.deepcopy(value)
    changed['artifact']['ledger']['operations'].pop()
    with pytest.raises(PackError, match='infrastructure and cleanup'):
        validate_import(changed, KEY)


def test_changed_file_during_authentication_is_not_imported(bootstrap):
    path, value = bootstrap
    def mutate(args):
        result = verified_invoke(path)(args)
        if args[:3] == ['gh','attestation','verify']:
            path.write_text('{}')
        return result
    with pytest.raises(ValueError):
        verify_import(path, value['artifactDigest'], KEY, 'a'*40, 123, 2, invoke=mutate)


@pytest.mark.parametrize('extra', [['--initialize-state'], ['--bootstrap-digest','sha256:'+'a'*64]])
def test_production_cli_refuses_unbound_initialization_before_cloud_calls(tmp_path, monkeypatch, extra):
    import agent_e2e_controller as controller
    pack = tmp_path/'pack.json'
    pack.write_text(json.dumps({'corpusManifest':'corpus.json'}))
    corpus = tmp_path/'corpus.json'
    corpus.write_text('{}')
    campaign = tmp_path/'campaign.json'
    campaign.write_text(json.dumps({'packDigest':digest_bytes(pack.read_bytes()),
        'corpusDigest':digest_bytes(corpus.read_bytes()), 'componentBindingDigest':KEY}))
    monkeypatch.setattr(controller, 'verify_corpus', lambda *args:None)
    monkeypatch.setattr('agent_e2e_state.open_store', lambda *args:pytest.fail('unbound initialization reached AWS'))
    args = ['--pack',str(pack), '--campaign',str(campaign), '--case-id','P10:A:local-docker']
    for name in ('backend','model','ledger','journal-root','output','state-config'):
        args += ['--'+name,str(tmp_path/name)]
    assert controller.main(args+extra) == 1
    assert json.loads((tmp_path/'output').read_text())['errorType'] == 'ControllerError'


@pytest.fixture
def amended_bootstrap(bootstrap, tmp_path):
    path, value = bootstrap
    ledger = tmp_path/'amended-existing-operations.json'
    ledger.write_text(json.dumps(value['artifact']['ledger']))
    update(ledger, 'settle', candidate_key=KEY, reservation_id='bootstrap-infrastructure', amount='12.34')
    update(ledger, 'amend-foss4g', candidate_key=KEY)
    value['artifact']['ledger'] = json.loads(ledger.read_text())
    path.write_text(json.dumps(value['artifact']))
    value['artifactDigest'] = digest_bytes(path.read_bytes())
    return path, value


def test_named_owner_amendment_imports_actual_prior_v2_operation_history(amended_bootstrap, tmp_path):
    path, value = amended_bootstrap
    original = copy.deepcopy(value['artifact']['ledger'])
    assert original['policyAmendment']['priorLedger']['schemaVersion'] == 2
    assert original['policyAmendment']['priorLedger']['operations'][0]['actualUsd'] == '12.34'
    assert validate_import(value, KEY) == original
    assert verify_import(path, value['artifactDigest'], KEY, 'a'*40, 123, 2, invoke=verified_invoke(path)) == value
    execution = DurableExecution(MemoryStore(), tmp_path/'imported-amended-ledger.json', initialize=True, bootstrap=value)
    try:
        assert execution.state['ledger'] == original
        assert validate(execution.state['ledger'])['liabilityUsd'] == '22.34'
    finally:
        execution.close()


@pytest.mark.parametrize('change', ['erase-prior', 'rewrite-charge', 'unknown-policy', 'wrong-ceiling', 'prior-v1', 'model-attempt', 'paid-admission'])
def test_amended_bootstrap_cannot_reset_history_or_claim_model_execution(amended_bootstrap, change):
    _, value = amended_bootstrap
    ledger = value['artifact']['ledger']
    if change == 'erase-prior': ledger['operations'].pop(0)
    elif change == 'rewrite-charge': ledger['operations'][0]['actualUsd'] = '0'
    elif change == 'unknown-policy': ledger['policyAmendment']['policyId'] = 'unreviewed-increase'
    elif change == 'wrong-ceiling': ledger['ceilingUsd'] = '2001'
    elif change == 'prior-v1':
        from agent_e2e_budget import history_digest
        prior = ledger['policyAmendment']['priorLedger']
        prior['schemaVersion'] = 1
        del prior['operations']
        ledger['policyAmendment']['priorLedgerDigest'] = history_digest(prior)
    elif change == 'model-attempt':
        ledger['entries'].append({'id': 'unbound-attempt', 'campaignId': 'new-campaign', 'caseId': 'P10:A:aws-ecs-redis-on',
            'attempt': 1, 'kind': 'model', 'amountUsd': '1', 'basisDigest': 'sha256:'+'d'*64, 'state': 'reserved', 'actualUsd': None})
    else:
        ledger['paidAdmissions'].append({'id': 'unbound-admission', 'campaignId': 'new-campaign', 'caseId': 'P10:A:aws-ecs-redis-on',
            'attempt': 1, 'campaignMode': 'development', 'ownerToken': 'original-controller-owner-0001',
            'createdEpoch': 1000, 'expiresEpoch': 1100, 'state': 'active', 'reservationIds': None, 'cleanupStatus': None})
    with pytest.raises((PackError, BudgetError)): validate_import(value, KEY)
