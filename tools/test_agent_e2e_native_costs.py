"""Native measured cost calculations and bounded shared overhead accounting."""
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agent_e2e_native_costs import OVERHEAD, fargate_compute, measured_settlement, reserve_overhead, reconcile_measured
from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_state import DurableExecution, preserve_history
from test_agent_e2e_backend import Store, CONTEXT, CANDIDATE


def native():
    start = datetime(2026, 10, 22, 1, tzinfo=timezone.utc)
    task = {'taskArn': 'arn:aws:ecs:us-east-1:123456789012:task/worker/'+'a'*32,
        'taskDefinitionArn': 'arn:aws:ecs:us-east-1:123456789012:task-definition/worker:1',
        'lastStatus': 'STOPPED', 'launchType': 'FARGATE', 'cpu': '256', 'memory': '512',
        'pullStartedAt': start, 'stoppedAt': start+timedelta(seconds=61.01)}
    definition = {key: task[key] for key in ('taskDefinitionArn', 'cpu', 'memory')}
    prices = []
    for kind, price in [('vCPU-Hours:perCPU', '0.04048'), ('GB-Hours', '0.004445')]:
        prices.append({'product': {'attributes': {'servicecode': 'AmazonECS', 'regionCode': 'us-east-1',
            'tenancy': 'Shared', 'usagetype': 'USE1-Fargate-'+kind}}, 'terms': {'OnDemand': {'sku': {
                'effectiveDate': '2026-01-01T00:00:00Z', 'priceDimensions': {'rate': {
                    'beginRange': '0', 'endRange': 'Inf', 'unit': 'Hrs', 'pricePerUnit': {'USD': price}}}}}}})
    receipt = {'schemaVersion': 1, 'source': 'aws-pricing:GetProducts', 'region': 'us-east-1',
               'capturedAt': (start-timedelta(hours=1)).isoformat(), 'priceLists': prices}
    return task, definition, receipt


def test_uses_native_pull_to_stop_duration_and_frozen_provider_rates():
    task, definition, receipt = native()
    result = fargate_compute(task, definition, receipt)
    assert result['billingSeconds'] == 62
    wanted = (Decimal('0.25')*Decimal('0.04048')+Decimal('0.5')*Decimal('0.004445'))*62/3600
    assert Decimal(result['listPriceComputeUsd']) >= wanted
    assert Decimal(result['listPriceComputeUsd'])-wanted < Decimal('0.000000001')
    assert result['completeAttemptCost'] is False
    task['stoppedAt'] = task['pullStartedAt']+timedelta(seconds=2)
    assert fargate_compute(task, definition, receipt)['billingSeconds'] == 60


@pytest.mark.parametrize('change', ['running', 'foreign-definition', 'wrong-region', 'missing-memory',
                                  'future-price', 'tier', 'extra-storage', 'ambiguous-price'])
def test_native_costs_refuse_unmeasured_or_ambiguous_charges(change):
    task, definition, receipt = native()
    if change == 'running': task['lastStatus'] = 'RUNNING'
    elif change == 'foreign-definition': definition['taskDefinitionArn'] = 'foreign'
    elif change == 'wrong-region': receipt['region'] = 'eu-west-1'
    elif change == 'missing-memory': receipt['priceLists'].pop()
    elif change == 'future-price': receipt['capturedAt'] = '2027-01-01T00:00:00Z'
    elif change == 'tier': receipt['priceLists'][0]['terms']['OnDemand']['sku']['priceDimensions']['rate']['endRange'] = '10'
    elif change == 'extra-storage': definition['ephemeralStorage'] = {'sizeInGiB': 50}
    else: receipt['priceLists'].append(copy.deepcopy(receipt['priceLists'][0]))
    with pytest.raises(PackError):
        fargate_compute(task, definition, receipt)


@pytest.fixture
def accounting(tmp_path):
    durable = DurableExecution(Store(), tmp_path/'ledger.json', initialize=True)
    for kind in ('infrastructure', 'cleanup'):
        durable.update(durable.path, 'reserve', candidate_key=CANDIDATE, reservation_id=kind,
            campaign_id=CONTEXT['campaignId'], case_id=CONTEXT['caseId'], attempt=1, campaign_mode='development',
            kind=kind, amount='1', basis_digest='sha256:'+'b'*64)
    durable.update(durable.path, 'reserve-operation', candidate_key=CANDIDATE,
        reservation_id='shared-overhead', operation_id='isolated-controller-and-target',
        purpose='shared-environment', kind='infrastructure', amount='1', basis_digest='sha256:'+'d'*64)
    coverage = {'schemaVersion': 1, 'allocation': 'compute-infrastructure-including-cleanup', 'bindings': [{
        'reservationId': 'shared-overhead', 'basisDigest': 'sha256:'+'d'*64, 'categories': sorted(OVERHEAD),
        'perAttemptCeilingUsd': '0.25', 'maximumAttempts': 4}]}
    try:
        yield durable, coverage
    finally:
        durable.close()


def test_prepaid_overhead_and_native_compute_settle_without_billing_delay_or_hidden_cleanup(accounting):
    durable, coverage = accounting
    reserve_overhead(durable, CONTEXT, coverage)
    compute = fargate_compute(*native())
    retained = []
    result = measured_settlement(durable, CONTEXT, compute, coverage, retain=lambda *args: retained.append(args))
    assert result['status'] == 'settled' and result['infrastructureUsd'] == compute['listPriceComputeUsd']
    allocation = next(iter(durable.store.state['nativeCostAllocations'].values()))
    assert allocation['cleanupUsd'] == '0' and 'infrastructureUsd' in allocation['cleanupCostIncludedIn']
    assert allocation['overheadStatus'] == 'liability-retained'
    assert durable.store.state['ledger']['operations'][0]['state'] == 'reserved'
    assert retained[0][0] == 'measured-cost-allocation'
    with pytest.raises(PackError, match='double count'):
        measured_settlement(durable, CONTEXT, compute, coverage, retain=lambda *_: None)


def test_crash_reconciliation_settles_only_retained_native_amounts_without_replaying_or_reclassifying(accounting):
    durable, coverage = accounting
    reserve_overhead(durable, CONTEXT, coverage)
    compute = fargate_compute(*native())
    measured_settlement(durable, CONTEXT, compute, coverage, retain=lambda *_: None)
    durable.state['attemptArtifacts'] = {'original': {'receipt': {'status': 'blocked', 'failureAttribution': 'undetermined'}}}
    durable._persist()
    old = copy.deepcopy(durable.store.state)
    campaign = {key: CONTEXT[key] for key in ('campaignId', 'campaignMode', 'componentBindingDigest')}
    assert reconcile_measured(durable, campaign)['settlements'] == 2
    assert durable.store.state['attemptArtifacts'] == old['attemptArtifacts']
    assert durable.store.state['nativeOverheadAllocations'] == old['nativeOverheadAllocations']
    assert durable.store.state['ledger']['operations'] == old['ledger']['operations']
    assert [row['actualUsd'] for row in durable.store.state['ledger']['entries']] == [compute['listPriceComputeUsd'], '0']
    assert reconcile_measured(durable, campaign)['settlements'] == 0


def test_shared_capacity_is_consumed_before_effects_and_cannot_fund_unlimited_cases(accounting):
    durable, coverage = accounting
    for index in range(4):
        reserve_overhead(durable, {**CONTEXT, 'campaignId': 'bounded-'+str(index)}, coverage)
    with pytest.raises(PackError, match='capacity exhausted'):
        reserve_overhead(durable, {**CONTEXT, 'campaignId': 'fifth'}, coverage)
    assert len(durable.store.state['nativeOverheadAllocations']) == 4


@pytest.mark.parametrize('change', ['missing-category', 'duplicate-category', 'wrong-basis', 'unfunded-total'])
def test_overhead_cannot_be_omitted_reused_or_rebound(accounting, change):
    durable, coverage = accounting
    binding = coverage['bindings'][0]
    if change == 'missing-category': binding['categories'].pop()
    elif change == 'duplicate-category': binding['categories'].append(binding['categories'][0])
    elif change == 'wrong-basis': binding['basisDigest'] = 'sha256:'+'f'*64
    else: binding['maximumAttempts'] = 5
    with pytest.raises(PackError):
        reserve_overhead(durable, CONTEXT, coverage)
    assert not durable.state.get('nativeOverheadAllocations')


def test_costs_require_exact_before_effect_admission_and_history_is_immutable(accounting):
    durable, coverage = accounting
    compute = fargate_compute(*native())
    with pytest.raises(PackError, match='before-effect'):
        measured_settlement(durable, CONTEXT, compute, coverage, retain=lambda *_: None)
    reserve_overhead(durable, CONTEXT, coverage)
    with pytest.raises(PackError, match='already retained'):
        reserve_overhead(durable, CONTEXT, coverage)
    changed = copy.deepcopy(durable.store.state)
    changed['nativeOverheadAllocations'].clear()
    with pytest.raises(PackError, match='retained campaign evidence'):
        preserve_history(durable.store.state, changed)
