"""Native stopped-task usage and eventual provider billing, never guessed costs.

AWS bills Fargate from image pull through task termination, per second with a
Linux one-minute minimum: https://aws.amazon.com/fargate/pricing/ . PriceList
quotes supply independently replayable compute evidence. They do not cover
queues, workflow transitions, networking, logs or shared controller resources.
Noncompute overhead stays in separate bounded durable operation liabilities.
Cost Explorer is optional later reconciliation, never a prerequisite to a case.
"""
from datetime import datetime
from decimal import Decimal, ROUND_CEILING
import copy
import math

from agent_e2e_budget import money
from agent_e2e_pack import PackError, canonical_digest

OVERHEAD = {'worker-queues', 'worker-lifecycle', 'worker-logs', 'worker-network',
            'controller-runtime', 'controller-state', 'retained-evidence', 'target-runtime',
            'fixture-sql', 'cleanup-api'}


def require(condition, reason):
    if not condition:
        raise PackError(reason)


def instant(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(parsed.tzinfo is not None, 'native cost timestamp lacks timezone')
    return parsed


def serializable(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(item) for item in value]
    return value


def fargate_compute(task, definition, receipt):
    """Replay actual duration/allocation against frozen official AWS price rows."""
    require(task.get('lastStatus') == 'STOPPED' and task.get('launchType') == 'FARGATE' and
            task.get('taskDefinitionArn') == definition.get('taskDefinitionArn') and
            definition.get('runtimePlatform', {}).get('operatingSystemFamily', 'LINUX') == 'LINUX' and
            definition.get('runtimePlatform', {}).get('cpuArchitecture', 'X86_64') == 'X86_64',
            'cost observation lacks exact stopped Linux x86 Fargate task')
    start, end = instant(task['pullStartedAt']), instant(task['stoppedAt'])
    require(end >= start, 'native Fargate duration is negative')
    seconds = max(60, math.ceil((end-start).total_seconds()))
    require(type(definition.get('cpu')) is str and definition['cpu'].isdigit() and
            type(definition.get('memory')) is str and definition['memory'].isdigit(), 'invalid native task allocation')
    require(task.get('cpu') == definition['cpu'] and task.get('memory') == definition['memory'],
            'native task allocation differs from exact definition')
    require(definition.get('ephemeralStorage', {}).get('sizeInGiB', 20) <= 20,
            'additional Fargate storage requires separate observed pricing')
    require(set(receipt) == {'schemaVersion', 'source', 'region', 'capturedAt', 'priceLists'} and
            receipt['schemaVersion'] == 1 and receipt['source'] == 'aws-pricing:GetProducts' and
            task['taskArn'].split(':')[3] == receipt['region'], 'unbound official Fargate price receipt')
    captured = instant(receipt['capturedAt'])
    require(captured <= start and (start-captured).total_seconds() <= 7*86400,
            'Fargate pricing was not frozen recently before execution')
    rates = {}
    for product in receipt['priceLists']:
        attributes = product.get('product', {}).get('attributes', {})
        require(attributes.get('servicecode') == 'AmazonECS' and attributes.get('regionCode') == receipt['region'] and
                attributes.get('tenancy') == 'Shared' and 'Spot' not in attributes.get('usagetype', ''),
                'price receipt belongs to another Fargate service/region/purchase type')
        usage = attributes.get('usagetype', '')
        kind = 'vcpu' if usage.endswith('Fargate-vCPU-Hours:perCPU') else 'memory' if usage.endswith('Fargate-GB-Hours') else None
        require(kind is not None and kind not in rates, 'unsupported or ambiguous Fargate price SKU')
        offers = list(product.get('terms', {}).get('OnDemand', {}).values())
        require(len(offers) == 1 and instant(offers[0]['effectiveDate']) <= start, 'ambiguous/inapplicable Fargate price term')
        dimensions = list(offers[0].get('priceDimensions', {}).values())
        require(len(dimensions) == 1 and dimensions[0].get('beginRange') == '0' and
                dimensions[0].get('endRange') == 'Inf' and dimensions[0].get('unit') in ('Hrs', 'hours'),
                'tiered or unsupported Fargate price dimension')
        rates[kind] = money(dimensions[0]['pricePerUnit']['USD'])
        require(rates[kind] > 0, 'missing positive official Fargate price')
    require(set(rates) == {'vcpu', 'memory'}, 'incomplete Fargate compute pricing')
    compute = (Decimal(definition['cpu'])/1024*rates['vcpu'] + Decimal(definition['memory'])/1024*rates['memory'])*seconds/3600
    return {'producer': 'native-ecs-duration-and-official-price', 'taskArn': task['taskArn'],
        'taskDefinitionArn': definition['taskDefinitionArn'], 'billingSeconds': seconds,
        'vcpu': str(Decimal(definition['cpu'])/1024), 'memoryGiB': str(Decimal(definition['memory'])/1024),
        'listPriceComputeUsd': str(compute.quantize(Decimal('0.000000001'), rounding=ROUND_CEILING)),
        'priceReceiptDigest': canonical_digest(receipt), 'completeAttemptCost': False}


def reserve_overhead(durable, context, coverage):
    """Consume exact bounded shared capacity durably before native effects.

    Reservations are not actual charges. The existing operation ledger retains
    their full liability until a later native collector knows the shared totals.
    Each attempt consumes bounded capacity under the original authenticated
    basis, preventing indefinite reuse of one allowance across new cases.
    """
    durable._require_live_lease()
    require(set(coverage) == {'schemaVersion', 'allocation', 'bindings'} and coverage['schemaVersion'] == 1 and
            coverage['allocation'] == 'compute-infrastructure-including-cleanup' and
            isinstance(coverage['bindings'], list) and coverage['bindings'], 'incomplete native cost attribution')
    assigned, bindings, reservations = set(), [], set()
    ledger = durable.state['ledger']
    from agent_e2e_budget import validate
    validate(ledger)
    allocation_id = canonical_digest({key: context[key] for key in ('campaignId', 'caseId', 'attempt')})
    retained = durable.state.get('nativeOverheadAllocations', {})
    require(allocation_id not in retained, 'attempt cost allocation already retained; reconcile without double counting')
    for binding in coverage['bindings']:
        require(set(binding) == {'reservationId', 'basisDigest', 'categories', 'perAttemptCeilingUsd', 'maximumAttempts'} and
                isinstance(binding['categories'], list) and binding['categories'] and
                not assigned.intersection(binding['categories']) and set(binding['categories']) <= OVERHEAD and
                len(set(binding['categories'])) == len(binding['categories']) and
                binding['reservationId'] not in reservations and
                type(binding['maximumAttempts']) is int and 1 <= binding['maximumAttempts'] <= 504,
                'ambiguous or unsupported shared cost coverage')
        assigned.update(binding['categories'])
        reservations.add(binding['reservationId'])
        rows = [row for row in ledger.get('operations', []) if row['id'] == binding['reservationId']]
        require(len(rows) == 1 and rows[0]['state'] == 'reserved' and rows[0]['basisDigest'] == binding['basisDigest'],
                'cost coverage lacks its original unresolved operation liability')
        per_attempt = money(binding['perAttemptCeilingUsd'])
        require(per_attempt > 0 and per_attempt*binding['maximumAttempts'] <= money(rows[0]['amountUsd']),
                'reviewed shared operation cannot fund its promised bounded attempts')
        prior = [old_binding for allocation in retained.values() for old_binding in allocation['bindings']
                 if old_binding['reservationId'] == binding['reservationId']]
        require(all(old['basisDigest'] == binding['basisDigest'] for old in prior) and
                len(prior)+1 <= binding['maximumAttempts'] and
                sum((money(old['perAttemptCeilingUsd']) for old in prior), Decimal(0))+per_attempt <= money(rows[0]['amountUsd']),
                'shared operation allocation capacity exhausted or basis changed')
        bindings.append({**copy.deepcopy(binding), 'operationId': rows[0]['operationId'], 'purpose': rows[0]['purpose']})
    require(assigned == OVERHEAD, 'native compute charge omits an overhead/shared/cleanup category')
    allocation = {'schemaVersion': 1, 'candidateKey': ledger['candidateKey'], 'identity': {
        key: context[key] for key in ('campaignId', 'caseId', 'attempt')},
        'allocation': coverage['allocation'], 'coverageDigest': canonical_digest(coverage),
        'bindings': bindings, 'overheadStatus': 'liability-retained'}
    retained[allocation_id] = copy.deepcopy(allocation)
    durable.state['nativeOverheadAllocations'] = retained
    durable._persist()
    return allocation


def measured_settlement(durable, context, compute, coverage, *, retain, recover=False):
    """Charge native usage once, leaving exact prepaid overhead unresolved."""
    durable._require_live_lease()
    require(compute.get('producer') == 'native-ecs-duration-and-official-price' and
            money(compute['listPriceComputeUsd']) > 0, 'missing independent measured compute charge')
    allocation_id = canonical_digest({key: context[key] for key in ('campaignId', 'caseId', 'attempt')})
    overhead = durable.state.get('nativeOverheadAllocations', {}).get(allocation_id)
    require(overhead is not None and overhead['candidateKey'] == durable.store.candidate and
            overhead['coverageDigest'] == canonical_digest(coverage), 'measured cost lacks exact before-effect overhead admission')
    retained = durable.state.setdefault('nativeCostAllocations', {})
    require(recover or allocation_id not in retained, 'measured task cost already retained; do not double count')
    rows = [row for row in durable.state['ledger']['entries'] if row['state'] == 'reserved' and
            all(row.get(key) == context[key] for key in ('campaignId', 'caseId', 'attempt', 'campaignMode'))]
    funded = {kind: [row for row in rows if row['kind'] == kind] for kind in ('infrastructure', 'cleanup')}
    require(all(len(values) == 1 for values in funded.values()) and
            funded['infrastructure'][0]['basisDigest'] == funded['cleanup'][0]['basisDigest'],
            'measured cost lacks unique prepaid attempt reservations')
    allocation = {'schemaVersion': 1, 'candidateKey': durable.store.candidate,
        'identity': copy.deepcopy(overhead['identity']), 'taskArn': compute['taskArn'],
        'campaignMode': context['campaignMode'],
        'reservationIds': {kind: values[0]['id'] for kind, values in funded.items()},
        'liabilityBasisDigest': funded['infrastructure'][0]['basisDigest'],
        'allocation': coverage['allocation'], 'computeObservationDigest': canonical_digest(compute),
        'overheadAdmissionDigest': canonical_digest(overhead),
        'infrastructureUsd': compute['listPriceComputeUsd'], 'cleanupUsd': '0',
        'cleanupCostIncludedIn': 'infrastructureUsd; noncompute cleanup remains in operation liabilities',
        'overheadStatus': 'liability-retained'}
    if allocation_id in retained:
        require(retained[allocation_id] == allocation, 'recovered measured task cost differs from original allocation')
    else:
        retained[allocation_id] = copy.deepcopy(allocation)
        durable._persist()
    retain('measured-cost-allocation', allocation)
    return {'status': 'settled', 'infrastructureUsd': allocation['infrastructureUsd'],
            'cleanupUsd': allocation['cleanupUsd'], 'allocationDigest': canonical_digest(allocation),
            'overheadStatus': 'liability-retained'}


def reconcile_measured(durable, campaign):
    """Finish only exact retained native charges; never run or reclassify a goal."""
    durable._require_live_lease()
    require(campaign['componentBindingDigest'] == durable.store.candidate, 'cost reconciliation belongs to another candidate')
    pending = []
    for key, allocation in durable.state.get('nativeCostAllocations', {}).items():
        if allocation['identity']['campaignId'] != campaign['campaignId']:
            continue
        overhead = durable.state.get('nativeOverheadAllocations', {}).get(key)
        require(allocation['candidateKey'] == durable.store.candidate and
                allocation['campaignMode'] == campaign['campaignMode'] and
                key == canonical_digest(allocation['identity']) and overhead is not None and
                allocation['overheadAdmissionDigest'] == canonical_digest(overhead), 'native cost allocation binding differs')
        for kind in ('infrastructure', 'cleanup'):
            rows = [row for row in durable.state['ledger']['entries'] if row['id'] == allocation['reservationIds'][kind]]
            require(len(rows) == 1 and rows[0]['kind'] == kind and
                    all(rows[0][field] == value for field, value in allocation['identity'].items()) and
                    rows[0]['campaignMode'] == campaign['campaignMode'] and
                    rows[0]['basisDigest'] == allocation['liabilityBasisDigest'],
                    'native cost reconciliation cannot rebind reservation identity or basis')
            actual = allocation[kind+'Usd']
            require(money(actual) >= 0, 'invalid retained native charge')
            if rows[0]['state'] == 'settled':
                require(money(rows[0]['actualUsd']) == money(actual), 'native charge differs from existing settlement')
            else:
                require(rows[0]['state'] == 'reserved', 'unknown reservation state')
                pending.append((rows[0]['id'], actual))
    errors = []
    for reservation_id, actual in pending:
        try:
            durable.update(durable.path, 'settle', candidate_key=durable.store.candidate,
                           reservation_id=reservation_id, amount=actual)
        except Exception as exc:
            # As in the controller, retain later known charges even on overrun.
            errors.append(type(exc).__name__)
    require(not errors, 'native cost reconciliation retained charges with settlement errors')
    return {'status': 'reconciled', 'settlements': len(pending), 'qualification': False,
            'note': 'Original attempts, failures, journals and shared liabilities remain retained.'}
