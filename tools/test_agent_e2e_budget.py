"""Adversarial tests for reservations, not a retrospective billing calculator."""
from decimal import Decimal
import json
from pathlib import Path
import pytest

from agent_e2e_budget import BudgetError, update, validate


KEY = "sha256:" + "1" * 64
BASIS = "sha256:" + "2" * 64


def reserve(path, rid, amount, *, campaign="c1", kind="model"):
    return update(path, "reserve", candidate_key=KEY, reservation_id=rid,
                  amount=amount, campaign_id=campaign, case_id="P01:A:local-docker",
                  attempt=1, kind=kind, basis_digest=BASIS)


def test_candidate_cannot_reset_spend_with_new_campaign(tmp_path):
    path = tmp_path / "ledger.json"
    update(path, "init", candidate_key=KEY)
    reserve(path, "r1", 440)
    update(path, "settle", candidate_key=KEY, reservation_id="r1", amount=440)
    update(path, "init", candidate_key=KEY)
    with pytest.raises(BudgetError, match="reserve"):
        reserve(path, "r2", 11, campaign="c2")
    assert len(json.loads(path.read_text())["entries"]) == 1


def test_cleanup_can_spend_reserved_funds_but_model_cannot(tmp_path):
    path = tmp_path / "ledger.json"
    update(path, "init", candidate_key=KEY)
    reserve(path, "compute", 450, kind="infrastructure")
    with pytest.raises(BudgetError):
        reserve(path, "model", 1)
    assert Decimal(reserve(path, "cleanup", 50, kind="cleanup")["remainingUsd"]) == 0
    with pytest.raises(BudgetError):
        reserve(path, "extra-cleanup", 1, kind="cleanup")


def test_actual_overrun_is_retained_and_blocks_later_reservations(tmp_path):
    path = tmp_path / "ledger.json"
    update(path, "init", candidate_key=KEY)
    reserve(path, "r1", 1)
    with pytest.raises(BudgetError, match="exceeded"):
        update(path, "settle", candidate_key=KEY, reservation_id="r1", amount=2)
    assert json.loads(path.read_text())["entries"][0]["actualUsd"] == "2"
    with pytest.raises(BudgetError, match="exceeded"):
        reserve(path, "r2", 1)


@pytest.mark.parametrize("value", [True, -1, "NaN", "Infinity", "-Infinity"])
def test_invalid_cost_cannot_enter_ledger(tmp_path, value):
    path = tmp_path / "ledger.json"
    update(path, "init", candidate_key=KEY)
    with pytest.raises(BudgetError):
        reserve(path, "bad", value)
    assert json.loads(path.read_text())["entries"] == []


def test_reservation_is_not_reusable_and_unsettled_liability_counts(tmp_path):
    path = tmp_path / "ledger.json"
    update(path, "init", candidate_key=KEY)
    reserve(path, "r1", 400)
    with pytest.raises(BudgetError):
        reserve(path, "r1", 1)
    with pytest.raises(BudgetError):
        reserve(path, "r2", 51)
    update(path, "settle", candidate_key=KEY, reservation_id="r1", amount=10)
    reserve(path, "r2", 400)
    with pytest.raises(BudgetError):
        update(path, "settle", candidate_key=KEY, reservation_id="r1", amount=0)


def test_wrong_candidate_and_concurrent_writer_refused(tmp_path):
    path = tmp_path / "ledger.json"
    update(path, "init", candidate_key=KEY)
    with pytest.raises(BudgetError):
        update(path, "init", candidate_key="sha256:" + "3" * 64)
    path.with_suffix(".json.lock").touch()
    with pytest.raises(BudgetError, match="in use"):
        reserve(path, "r1", 1)


def reserve_operation(path, rid='bootstrap-infra', amount='400', **overrides):
    values = {'candidate_key':KEY, 'reservation_id':rid, 'operation_id':'bootstrap',
              'purpose':'bootstrap', 'kind':'infrastructure', 'amount':amount, 'basis_digest':BASIS}
    return update(path, 'reserve-operation', **{**values, **overrides})


def test_shared_operations_and_attempts_share_one_ceiling(tmp_path):
    path = tmp_path/'ledger.json'
    update(path, 'init', candidate_key=KEY)
    reserve_operation(path)
    reserve(path, 'attempt', '50')
    with pytest.raises(BudgetError, match='reserve'):
        reserve(path, 'another', '0.01', campaign='new-campaign')
    assert Decimal(reserve_operation(path, 'cleanup', '50', kind='cleanup')['remainingUsd']) == 0
    update(path, 'init', candidate_key=KEY)
    assert len(json.loads(path.read_text())['operations']) == 2


def test_operation_overrun_is_retained_and_blocks_attempt(tmp_path):
    path = tmp_path/'ledger.json'
    update(path, 'init', candidate_key=KEY)
    reserve_operation(path, amount='1')
    with pytest.raises(BudgetError, match='exceeded'):
        update(path, 'settle', candidate_key=KEY, reservation_id='bootstrap-infra', amount='2')
    assert json.loads(path.read_text())['operations'][0]['actualUsd'] == '2'
    with pytest.raises(BudgetError, match='exceeded'):
        reserve(path, 'model', '1')


@pytest.mark.parametrize('override', [{'kind':'model'}, {'purpose':'test-case'}, {'operation_id':'../case'}, {'operation_id':''}])
def test_operations_cannot_disguise_model_attempts(tmp_path, override):
    path = tmp_path/'ledger.json'
    update(path, 'init', candidate_key=KEY)
    with pytest.raises(BudgetError, match='operation'):
        reserve_operation(path, **override)
    assert json.loads(path.read_text())['operations'] == []


def test_reservation_ids_are_unique_across_scopes(tmp_path):
    path = tmp_path/'ledger.json'
    update(path, 'init', candidate_key=KEY)
    reserve(path, 'shared-id', 1)
    with pytest.raises(BudgetError, match='duplicate'):
        reserve_operation(path, rid='shared-id')


def test_legacy_ledger_cannot_silently_acquire_operation_charges(tmp_path):
    path = tmp_path/'ledger.json'
    path.write_text(json.dumps({'schemaVersion':1, 'candidateKey':KEY, 'ceilingUsd':'500',
        'cleanupReserveUsd':'50', 'entries':[]}))
    original = path.read_bytes()
    with pytest.raises(BudgetError, match='migration'):
        reserve_operation(path)
    assert path.read_bytes() == original


def test_overrun_blocks_new_work_but_later_cleanup_charge_is_retained(tmp_path):
    path = tmp_path/'ledger.json'
    update(path, 'init', candidate_key=KEY)
    reserve_operation(path, rid='infra', amount='1')
    reserve_operation(path, rid='cleanup', amount='1', kind='cleanup')
    for rid, actual in [('infra','2'), ('cleanup','3')]:
        with pytest.raises(BudgetError, match='exceeded'):
            update(path, 'settle', candidate_key=KEY, reservation_id=rid, amount=actual)
    rows = json.loads(path.read_text())['operations']
    assert [row['actualUsd'] for row in rows] == ['2','3']
    with pytest.raises(BudgetError, match='exceeded'):
        reserve(path, 'model', '1')


def test_foss4g_amendment_requires_initialized_same_candidate_ledger(tmp_path):
    path=tmp_path/'ledger.json'
    with pytest.raises(BudgetError, match='initialized'):
        update(path,'amend-foss4g',candidate_key=KEY)
    update(path,'init',candidate_key=KEY)
    original=path.read_bytes()
    with pytest.raises(BudgetError, match='another candidate'):
        update(path,'amend-foss4g',candidate_key='sha256:'+'3'*64)
    assert path.read_bytes()==original


def test_foss4g_amendment_preserves_attempts_operations_and_is_idempotent(tmp_path):
    path=tmp_path/'ledger.json'
    update(path,'init',candidate_key=KEY)
    reserve(path,'model',40)
    update(path,'settle',candidate_key=KEY,reservation_id='model',amount=30)
    reserve_operation(path,rid='shared',amount=100)
    before=json.loads(path.read_text())
    result=update(path,'amend-foss4g',candidate_key=KEY)
    amended=json.loads(path.read_text())
    assert amended['schemaVersion']==3
    assert amended['ceilingUsd']=='2000' and amended['cleanupReserveUsd']=='200'
    assert amended['entries']==before['entries'] and amended['operations']==before['operations']
    assert amended['policyAmendment']['priorLedger']==before
    assert Decimal(result['liabilityUsd'])==130
    bytes_before=path.read_bytes()
    update(path,'amend-foss4g',candidate_key=KEY)
    update(path,'init',candidate_key=KEY)
    assert path.read_bytes()==bytes_before
    # Settling a pre-amendment reservation is a permitted history transition.
    update(path,'settle',candidate_key=KEY,reservation_id='shared',amount=80)
    assert Decimal(update(path,'check',candidate_key=KEY)['liabilityUsd'])==110


def test_foss4g_protects_200_cleanup_and_conserves_inherited_liability(tmp_path):
    path=tmp_path/'ledger.json'
    update(path,'init',candidate_key=KEY)
    reserve(path,'old',100)
    update(path,'amend-foss4g',candidate_key=KEY)
    reserve_operation(path,rid='new',amount=1700)
    with pytest.raises(BudgetError, match='reserve'):
        reserve(path,'one-more',1)
    reserve_operation(path,rid='cleanup',amount=200,kind='cleanup')
    assert Decimal(update(path,'check',candidate_key=KEY)['remainingUsd'])==0


def test_prior_actual_overrun_cannot_be_amended_away(tmp_path):
    path=tmp_path/'ledger.json'
    update(path,'init',candidate_key=KEY)
    reserve(path,'old',1)
    with pytest.raises(BudgetError):
        update(path,'settle',candidate_key=KEY,reservation_id='old',amount=2)
    original=path.read_bytes()
    with pytest.raises(BudgetError, match='exceeded'):
        update(path,'amend-foss4g',candidate_key=KEY)
    assert path.read_bytes()==original


@pytest.mark.parametrize('mutation',[
    lambda l:l.update(ceilingUsd='2500'),
    lambda l:l.update(cleanupReserveUsd='0'),
    lambda l:l['policyAmendment'].update(policyId='arbitrary-policy'),
    lambda l:l['policyAmendment'].update(priorLedgerDigest='sha256:'+'0'*64),
    lambda l:l.update(entries=[]),
    lambda l:l['entries'][0].update(actualUsd='0'),
    lambda l:l['operations'][0].update(amountUsd='1'),
    lambda l:l['policyAmendment']['priorLedger'].update(candidateKey='sha256:'+'4'*64),
])
def test_foss4g_fixed_policy_and_historical_rows_cannot_be_rewritten(tmp_path,mutation):
    path=tmp_path/'ledger.json';update(path,'init',candidate_key=KEY)
    reserve(path,'old',10);update(path,'settle',candidate_key=KEY,reservation_id='old',amount=9)
    reserve_operation(path,rid='old-operation',amount=10)
    update(path,'amend-foss4g',candidate_key=KEY)
    ledger=json.loads(path.read_text());mutation(ledger)
    with pytest.raises(BudgetError):validate(ledger)


def test_reviewed_amendment_can_preserve_legacy_v1_without_reset(tmp_path):
    path=tmp_path/'ledger.json'
    path.write_text(json.dumps({'schemaVersion':1,'candidateKey':KEY,'ceilingUsd':'500',
                               'cleanupReserveUsd':'50','entries':[]}))
    reserve(path,'legacy',10)
    update(path,'amend-foss4g',candidate_key=KEY)
    reserve_operation(path,rid='new-operation',amount=10)
    assert Decimal(update(path,'check',candidate_key=KEY)['liabilityUsd'])==20


def test_post_amendment_overrun_is_retained_and_blocks_new_work(tmp_path):
    path=tmp_path/'ledger.json';update(path,'init',candidate_key=KEY)
    update(path,'amend-foss4g',candidate_key=KEY)
    reserve(path,'work',1);reserve_operation(path,rid='cleanup',amount=1,kind='cleanup')
    for reservation,actual in [('work',2),('cleanup',3)]:
        with pytest.raises(BudgetError,match='exceeded'):
            update(path,'settle',candidate_key=KEY,reservation_id=reservation,amount=actual)
    assert json.loads(path.read_text())['operations'][0]['actualUsd']=='3'
    with pytest.raises(BudgetError,match='exceeded'):
        update(path,'amend-foss4g',candidate_key=KEY)


def test_legacy_252_campaign_rejects_an_unbound_amended_allowance(tmp_path):
    from agent_e2e_campaign_budget import bind_ledger
    from agent_e2e_pack import PackError
    path=tmp_path/'ledger.json';update(path,'init',candidate_key=KEY)
    update(path,'amend-foss4g',candidate_key=KEY)
    with pytest.raises(PackError, match='unbound amended allowance'):
        bind_ledger({'componentBindingDigest': KEY, 'totalBudgetUsd': 500}, json.loads(path.read_text()), tmp_path)


def admit(path, index, *, now=1000, **overrides):
    values={'candidate_key':KEY,'admission_id':f'admission-{index}','owner_token':f'controller-owner-token-{index:04}',
            'campaign_id':f'campaign-{index}','case_id':'AI-01:service-migration:A:local-docker:console',
            'attempt':1,'expires_epoch':1100,'now_epoch':now}
    values.update(overrides)
    return update(path,'acquire-paid-attempt',**values)


def paid_ledger(tmp_path):
    path=tmp_path/'paid.json';update(path,'init',candidate_key=KEY)
    update(path,'amend-foss4g',candidate_key=KEY)
    return path


def settle_paid(path,index):
    context={'candidate_key':KEY,'campaign_id':f'campaign-{index}',
             'case_id':'AI-01:service-migration:A:local-docker:console','attempt':1,'basis_digest':BASIS}
    for kind in ('infrastructure','cleanup'):
        update(path,'reserve',reservation_id=f'{kind}-{index}',kind=kind,amount=1,**context)
        update(path,'settle',candidate_key=KEY,reservation_id=f'{kind}-{index}',amount=1)
    return {kind:f'{kind}-{index}' for kind in ('infrastructure','cleanup')}


def test_fifth_candidate_admission_blocked_and_expiry_never_frees_capacity(tmp_path):
    path=paid_ledger(tmp_path)
    for n in range(4):admit(path,n)
    before=path.read_bytes()
    update(path,'amend-foss4g',candidate_key=KEY)
    assert path.read_bytes()==before
    for now in (1000,1200):
        with pytest.raises(BudgetError,match='four concurrent'):
            admit(path,4,now=now,expires_epoch=now+100)
        assert path.read_bytes()==before
    with pytest.raises(BudgetError,match='stale'):
        update(path,'check-paid-attempt',candidate_key=KEY,admission_id='admission-0',
               owner_token='controller-owner-token-0000',now_epoch=1200)


def test_release_requires_current_owner_successful_cleanup_and_exact_settled_liabilities(tmp_path):
    path=paid_ledger(tmp_path);admit(path,0)
    values={'candidate_key':KEY,'admission_id':'admission-0','owner_token':'controller-owner-token-0000',
            'reservation_ids':{'infrastructure':'infrastructure-0','cleanup':'cleanup-0'},'cleanup_status':'pass','now_epoch':1200}
    with pytest.raises(BudgetError,match='accounting'):
        update(path,'release-paid-attempt',**values)
    settle_paid(path,0)
    for patch in ({'owner_token':'wrong-owner-token-0000'},{'cleanup_status':'fail'},
                  {'reservation_ids':{'infrastructure':'infrastructure-1','cleanup':'cleanup-1'}}):
        with pytest.raises(BudgetError):update(path,'release-paid-attempt',**{**values,**patch})
    update(path,'release-paid-attempt',**values)
    assert json.loads(path.read_text())['paidAdmissions'][0]['state']=='released'
    with pytest.raises(BudgetError,match='released'):
        update(path,'check-paid-attempt',candidate_key=KEY,admission_id='admission-0',
               owner_token='controller-owner-token-0000',now_epoch=1001)
    with pytest.raises(BudgetError):admit(path,0,now=1200,expires_epoch=1300)
    with pytest.raises(BudgetError,match='readmitted'):
        admit(path,7,campaign_id='campaign-0',now=1200,expires_epoch=1300)
    # New distinct attempt can use capacity, but old history/costs remain.
    admit(path,1,now=1200,expires_epoch=1300)
    assert len(json.loads(path.read_text())['paidAdmissions'])==2
    assert Decimal(update(path,'check',candidate_key=KEY)['liabilityUsd'])==2


@pytest.mark.parametrize('override',[{'expires_epoch':1000},{'expires_epoch':7001},{'expires_epoch':True},
                                     {'attempt':3},{'owner_token':'short'}])
def test_invalid_paid_admission_cannot_enter_history(tmp_path,override):
    path=paid_ledger(tmp_path);before=path.read_bytes()
    with pytest.raises(BudgetError):admit(path,0,**override)
    assert path.read_bytes()==before


def test_legacy_candidate_does_not_implicitly_opt_into_paid_admissions(tmp_path):
    path=tmp_path/'legacy.json';update(path,'init',candidate_key=KEY)
    with pytest.raises(BudgetError,match='amendment'):admit(path,0)


def test_admission_cleanup_failure_does_not_remove_reservations_or_spending(tmp_path):
    path=paid_ledger(tmp_path);admit(path,0);settle_paid(path,0)
    before=path.read_bytes()
    with pytest.raises(BudgetError,match='cleanup failure'):
        update(path,'release-paid-attempt',candidate_key=KEY,admission_id='admission-0',
               owner_token='controller-owner-token-0000',cleanup_status='blocked',
               reservation_ids={'infrastructure':'infrastructure-0','cleanup':'cleanup-0'},now_epoch=1200)
    assert path.read_bytes()==before


def test_only_reconciled_release_of_one_of_four_admissions_allows_fifth(tmp_path):
    path=paid_ledger(tmp_path)
    for n in range(4):admit(path,n)
    reservations=settle_paid(path,0)
    update(path,'release-paid-attempt',candidate_key=KEY,admission_id='admission-0',
           owner_token='controller-owner-token-0000',cleanup_status='pass',reservation_ids=reservations,now_epoch=1200)
    admit(path,4,now=1200,expires_epoch=1300)
    ledger=json.loads(path.read_text())
    assert len(ledger['paidAdmissions'])==5
    assert sum(r['state']=='active' for r in ledger['paidAdmissions'])==4
    assert len(ledger['entries'])==2


def test_outstanding_model_liability_prevents_admission_release_until_observed_settlement(tmp_path):
    path=paid_ledger(tmp_path);admit(path,0)
    reservations=settle_paid(path,0)
    update(path,'reserve',candidate_key=KEY,reservation_id='model-in-flight',kind='model',amount=2,
           basis_digest=BASIS,campaign_id='campaign-0',
           case_id='AI-01:service-migration:A:local-docker:console',attempt=1)
    release={'candidate_key':KEY,'admission_id':'admission-0','owner_token':'controller-owner-token-0000',
             'cleanup_status':'pass','reservation_ids':reservations,'now_epoch':1200}
    before=path.read_bytes()
    with pytest.raises(BudgetError,match='including model'):
        update(path,'release-paid-attempt',**release)
    assert path.read_bytes()==before
    assert json.loads(path.read_text())['paidAdmissions'][0]['state']=='active'
    assert Decimal(update(path,'check',candidate_key=KEY)['liabilityUsd'])==4
    # Only independently supplied actual accounting settles the model liability.
    update(path,'settle',candidate_key=KEY,reservation_id='model-in-flight',amount='1.25')
    update(path,'release-paid-attempt',**release)
    retained=json.loads(path.read_text())
    assert retained['paidAdmissions'][0]['state']=='released'
    assert next(r for r in retained['entries'] if r['id']=='model-in-flight')['actualUsd']=='1.25'
    assert Decimal(update(path,'check',candidate_key=KEY)['liabilityUsd'])==Decimal('3.25')
