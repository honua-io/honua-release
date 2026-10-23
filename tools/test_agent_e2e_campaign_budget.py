"""Named funding integration controls; these fixtures never qualify a live run."""
import copy
import json
from pathlib import Path
import time

import pytest

import agent_e2e_campaign as producer
import agent_e2e_campaign_budget as policy
import agent_e2e_controller as controller
from agent_e2e_budget import FOSS4G_POLICY, history_digest, update, validate as validate_budget
from agent_e2e_pack import PackError, digest_bytes, load_json
from test_agent_e2e_controller import environment
from test_agent_e2e_gate import campaign, validate as validate_complete, write
from verify_agent_e2e_campaign import REPOSITORY, WORKFLOW


def artifact(root, ledger):
    original = {'schemaVersion': 1, 'candidateKey': ledger['candidateKey'], 'ledger': copy.deepcopy(ledger),
        'source': {'repository': REPOSITORY, 'workflow': WORKFLOW, 'sourceSha': 'a'*40, 'runId': 123, 'runAttempt': 2}}
    path = root/policy.ORIGINAL
    write(path, original)
    imported = {'artifactDigest': digest_bytes(path.read_bytes()), 'artifact': original}
    reference = {'artifactDigest': imported['artifactDigest'], 'sourceSha': 'a'*40, 'runId': 123, 'runAttempt': 2}
    return path, imported, reference


@pytest.fixture
def amended(environment):
    env = environment
    key, ledger = env['campaign']['componentBindingDigest'], env['ledger']
    for kind in ('infrastructure', 'cleanup'):
        update(ledger, 'reserve-operation', candidate_key=key, reservation_id='original-'+kind,
            operation_id='original-bootstrap', purpose='bootstrap', kind=kind, amount='1', basis_digest='sha256:'+'b'*64)
    original, imported, reference = artifact(ledger.parent, load_json(ledger))
    update(ledger, 'amend-foss4g', candidate_key=key)
    declaration = {name: load_json(ledger)['policyAmendment'][name] for name in ('policyId', 'priorLedgerDigest')}
    frozen, _ = policy.freeze_policy({'budgetPolicy': declaration, 'bootstrap': reference}, key, original, imported)
    pack_path = Path(__file__).resolve().parents[1]/'certification/agent-e2e/pack.v2.json'
    env['campaign'].update(totalBudgetUsd=2000, budgetPolicy=frozen, packDigest=digest_bytes(pack_path.read_bytes()))
    preflight = env['backend'].preflight
    env['expiry'] = int(time.time())+1200
    env['backend'].preflight = lambda context: preflight(context) | {'paidAttempt': {'id': 'original-worker-attempt', 'expiresEpoch': env['expiry']}}
    return env


def execute(env, **kwargs):
    return controller.run_goal(**{key: value for key, value in env.items() if key != 'expiry'}, **kwargs)


def test_named_amendment_uses_original_expiry_and_releases_only_settled_cleaned_attempt(amended):
    before = load_json(amended['ledger'])
    result = execute(amended)
    ledger = load_json(amended['ledger'])
    assert result['status'] == 'pass'
    assert ledger['operations'] == before['operations']
    assert ledger['policyAmendment'] == before['policyAmendment']
    assert len(ledger['paidAdmissions']) == 1
    admission = ledger['paidAdmissions'][0]
    assert admission['state'] == 'released' and admission['cleanupStatus'] == 'pass'
    assert admission['expiresEpoch'] == amended['expiry']
    assert len(ledger['entries']) == 3 and all(row['state'] == 'settled' for row in ledger['entries'])
    assert len(amended['model'].inputs) == 1


@pytest.mark.parametrize('change', ['missing-policy', 'unknown-policy', 'wrong-prior', 'wrong-original', 'different-pack'])
def test_unbound_funding_cannot_start_a_worker_or_model(amended, change):
    value = amended['campaign']['budgetPolicy']
    if change == 'missing-policy':
        amended['campaign'].pop('budgetPolicy'); amended['campaign']['totalBudgetUsd'] = 500
    elif change == 'unknown-policy': value['policyId'] = 'another-policy'
    elif change == 'wrong-prior': value['priorLedgerDigest'] = 'sha256:'+'d'*64
    elif change == 'wrong-original': (amended['ledger'].parent/policy.ORIGINAL).write_text('{}')
    else: amended['campaign']['packDigest'] = 'sha256:'+'e'*64
    result = execute(amended)
    assert result['status'] == 'blocked' and not amended['backend'].operations
    assert not amended['model'].inputs and not load_json(amended['ledger'])['paidAdmissions']


def test_fifth_attempt_preserves_all_four_existing_holds_without_effects(amended):
    key = amended['campaign']['componentBindingDigest']
    for i in range(4):
        update(amended['ledger'], 'acquire-paid-attempt', candidate_key=key, admission_id='existing-'+str(i),
            owner_token='x'*32, expires_epoch=amended['expiry'], campaign_id='other-campaign',
            case_id='P'+str(i), attempt=1, campaign_mode='development')
    before = load_json(amended['ledger'])
    assert execute(amended)['status'] == 'blocked'
    assert load_json(amended['ledger']) == before
    assert not amended['backend'].operations and not amended['model'].inputs


def test_expired_original_worker_policy_cannot_be_extended_or_started(amended):
    amended['expiry'] = int(time.time())-1
    assert execute(amended)['status'] == 'blocked'
    assert not load_json(amended['ledger'])['paidAdmissions']
    assert not amended['backend'].operations and not amended['model'].inputs


def test_unknown_cleanup_keeps_original_admission_and_unsettled_liabilities(amended):
    amended['backend'].cleanup_error = True
    assert execute(amended)['status'] == 'blocked'
    ledger = load_json(amended['ledger'])
    assert ledger['paidAdmissions'][0]['state'] == 'active'
    assert ledger['paidAdmissions'][0]['expiresEpoch'] == amended['expiry']
    assert {row['kind'] for row in ledger['entries'] if row['state'] == 'reserved'} == {'infrastructure', 'cleanup'}


def test_unknown_model_charge_cannot_release_an_otherwise_clean_worker(amended):
    def unknown(_): raise TimeoutError('unobserved provider outcome')
    amended['model'].invoke = unknown
    assert execute(amended)['status'] == 'blocked'
    ledger = load_json(amended['ledger'])
    assert ledger['paidAdmissions'][0]['state'] == 'active'
    assert any(row['kind'] == 'model' and row['state'] == 'reserved' for row in ledger['entries'])


def test_lost_admission_acknowledgement_is_not_released_or_reacquired(amended):
    def lost(path, operation, **values):
        result = update(path, operation, **values)
        if operation == 'acquire-paid-attempt': raise TimeoutError('lost original acknowledgement')
        return result
    assert execute(amended, budget_update=lost)['status'] == 'blocked'
    ledger = load_json(amended['ledger'])
    assert len(ledger['paidAdmissions']) == 1 and ledger['paidAdmissions'][0]['state'] == 'active'
    assert not amended['backend'].operations and not amended['model'].inputs


def test_later_real_amendment_preserves_charges_accumulated_after_original_import(amended):
    ledger = load_json(amended['ledger'])
    prior = copy.deepcopy(ledger['policyAmendment']['priorLedger'])
    prior['operations'].append({'id': 'later-original-infra', 'operationId': 'later-shared',
        'purpose': 'shared-environment', 'kind': 'infrastructure', 'amountUsd': '2',
        'actualUsd': None, 'state': 'reserved', 'basisDigest': 'sha256:'+'c'*64})
    ledger['operations'] = copy.deepcopy(prior['operations'])
    ledger['policyAmendment'].update(priorLedger=prior, priorLedgerDigest=history_digest(prior))
    amended['campaign']['budgetPolicy']['priorLedgerDigest'] = history_digest(prior)
    result = policy.bind_ledger(amended['campaign'], ledger, amended['ledger'].parent)
    assert result['liabilityUsd'] == '4'


def test_expiry_after_quote_blocks_inference_and_keeps_the_original_hold(amended, monkeypatch):
    quote = amended['model'].quote
    def expires(request):
        result = quote(request)
        monkeypatch.setattr(time, 'time', lambda: amended['expiry']+1)
        return result
    amended['model'].quote = expires
    invoked = []
    amended['model'].invoke = lambda body: invoked.append(body)
    assert execute(amended)['status'] == 'blocked'
    assert not invoked and load_json(amended['ledger'])['paidAdmissions'][0]['state'] == 'active'


def gate_amend(root):
    current = load_json(root/'ledger.json')
    prior = copy.deepcopy(current); prior['entries'] = []
    path, imported, reference = artifact(root, prior)
    current.update(schemaVersion=3, ceilingUsd='2000', cleanupReserveUsd='200',
        policyAmendment={'policyId': FOSS4G_POLICY, 'priorLedger': prior, 'priorLedgerDigest': history_digest(prior)}, paidAdmissions=[])
    declaration = {name: current['policyAmendment'][name] for name in ('policyId', 'priorLedgerDigest')}
    manifest, _ = policy.freeze_policy({'budgetPolicy': declaration, 'bootstrap': reference}, current['candidateKey'], path, imported)
    campaign = load_json(root/'campaign.json'); campaign.update(totalBudgetUsd=2000, budgetPolicy=manifest)
    for i, row in enumerate(load_json(root/'registry.json')['attempts']):
        current['paidAdmissions'].append({'id': 'original-worker-'+str(i), 'ownerToken': 'x'*32,
            **{name: row[name] for name in ('campaignId', 'caseId', 'attempt', 'campaignMode')},
            'createdEpoch': 1000000, 'expiresEpoch': 1001200, 'state': 'released', 'cleanupStatus': 'pass',
            'reservationIds': {kind: row['caseId']+'/1/'+kind for kind in ('infrastructure', 'cleanup')}})
    validate_budget(current)
    write(root/'campaign.json', campaign); write(root/'ledger.json', current)
    trusted = load_json(root/'trusted.json')
    trusted.update(campaignDigest=digest_bytes((root/'campaign.json').read_bytes()), budgetLedgerDigest=digest_bytes((root/'ledger.json').read_bytes()))
    write(root/'trusted.json', trusted)
    return current, campaign, trusted


def test_full_252_gate_accepts_only_original_named_amendment_and_released_admissions(campaign):
    gate_amend(campaign)
    result = validate_complete(campaign)
    assert result['status'] == 'pass' and result['casesPassed'] == 252


@pytest.mark.parametrize('change', ['active', 'missing-admission', 'original', 'policy', 'removed-history'])
def test_reattested_amended_gate_still_rejects_unknown_hold_or_changed_original(campaign, change):
    ledger, manifest, trusted = gate_amend(campaign)
    if change == 'active': ledger['paidAdmissions'][0].update(state='active', cleanupStatus=None, reservationIds=None)
    elif change == 'missing-admission': ledger['paidAdmissions'].pop()
    elif change == 'original': (campaign/policy.ORIGINAL).write_text('{}')
    elif change == 'policy': manifest['budgetPolicy']['priorLedgerDigest'] = 'sha256:'+'d'*64
    else: ledger['operations'].pop()
    write(campaign/'ledger.json', ledger); write(campaign/'campaign.json', manifest)
    trusted.update(budgetLedgerDigest=digest_bytes((campaign/'ledger.json').read_bytes()), campaignDigest=digest_bytes((campaign/'campaign.json').read_bytes()))
    write(campaign/'trusted.json', trusted)
    with pytest.raises(ValueError): validate_complete(campaign)


@pytest.mark.parametrize('observed', [False, True])
def test_freeze_retains_raw_authenticated_funding_and_full_252_scope(campaign, monkeypatch, observed):
    root = campaign
    old = load_json(root/'campaign.json')
    prior = load_json(root/'ledger.json'); prior['entries'] = []
    ledger = copy.deepcopy(prior)
    if observed:
        ledger.update(schemaVersion=3, ceilingUsd='2000', cleanupReserveUsd='200', paidAdmissions=[],
            policyAmendment={'policyId': FOSS4G_POLICY, 'priorLedger': prior, 'priorLedgerDigest': history_digest(prior)})
    path, imported, reference = artifact(root, ledger)
    config = {'budgetPolicy': {'policyId': FOSS4G_POLICY, 'priorLedgerDigest': history_digest(prior)},
        'bootstrap': reference, 'model': {name: old[name] for name in ('modelId', 'returnedModelId')},
        'expectations': {'path': 'reviewed-expectations.json'},
        'knowledge': {'path': 'evidence/knowledge/knowledge-manifest.json'}}
    write(root/'reviewed-expectations.json', {'cases': load_json(root/'expectations.json')['cases']})
    monkeypatch.setattr(producer, 'ROOT', root); monkeypatch.setattr(producer, 'PACK', root/'pack.json')
    producer.freeze(config, root/'lock.json', root/'frozen', 'qualification',
        identity=old['controller'], bootstrap_artifact=path, authenticated_bootstrap=imported)
    frozen = load_json(root/'frozen/campaign.json')
    assert frozen['totalBudgetUsd'] == 2000
    assert frozen['budgetPolicy']['amendmentObservedAtFreeze'] is observed
    assert (root/'frozen'/policy.ORIGINAL).read_bytes() == path.read_bytes()
    cases = load_json(root/'frozen/expectations.json')['cases']
    assert len(cases) == 252 and sum(case.endswith(':aws-eks') for case in cases) == 72
    if not observed:
        with pytest.raises(PackError, match='actual durable ledger'):
            policy.bind_ledger(frozen, ledger, root/'frozen', bootstrap=imported)


@pytest.mark.parametrize('change', ['sourceSha', 'runId', 'runAttempt', 'artifactDigest', 'authentication', 'policy'])
def test_freeze_rejects_original_funding_rebinding(amended, change):
    root = amended['ledger'].parent
    original, imported, reference = artifact(root, load_json(amended['ledger'])['policyAmendment']['priorLedger'])
    config = {'budgetPolicy': {name: amended['campaign']['budgetPolicy'][name] for name in ('policyId', 'priorLedgerDigest')}, 'bootstrap': reference}
    if change == 'sourceSha': reference['sourceSha'] = 'c'*40
    elif change in ('runId', 'runAttempt'): reference[change] += 1
    elif change == 'artifactDigest': reference[change] = 'sha256:'+'d'*64
    elif change == 'authentication': imported = None
    else: config['budgetPolicy']['policyId'] = 'caller-label'
    with pytest.raises(PackError): policy.freeze_policy(config, amended['campaign']['componentBindingDigest'], original, imported)


def test_amended_freeze_downloads_only_original_release_artifact_without_aws_identity():
    import yaml
    workflow = yaml.safe_load((Path(__file__).resolve().parents[1]/'.github/workflows/agent-e2e-campaign.yml').read_text())
    freeze = workflow['jobs']['freeze']
    assert not any('configure-aws-credentials' in step.get('uses', '') for step in freeze['steps'])
    step = next(step for step in freeze['steps'] if step.get('name') == 'Download the original funding artifact without AWS authority')
    assert step['with'] == {'github-token': '${{ github.token }}', 'run-id': '${{ steps.funding.outputs.run_id }}',
        'name': 'agent-e2e-budget-bootstrap-${{ steps.funding.outputs.run_attempt }}', 'path': 'bootstrap-import'}
    command = next(step for step in freeze['steps'] if step.get('name') == 'Freeze reviewed campaign before paid execution')
    assert '--bootstrap-artifact bootstrap-import/bootstrap-accounting.json' in command['run']
    assert command['env']['GH_TOKEN'] == '${{ github.token }}'
