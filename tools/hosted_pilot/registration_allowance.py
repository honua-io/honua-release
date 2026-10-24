"""Original registrar quote/admission; no allowance initialization or renewal.

The trusted source producer retains one absolute admission before reserving its
two distinct rows. The registrar consumes only verify_retained_admission.
"""
from __future__ import annotations

import copy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import time

from agent_e2e_pack import canonical_digest
from agent_e2e_budget import validate as validate_budget
from agent_e2e_interactive_budget import validate_retained_funding


def require(condition, message):
    if not condition:
        raise ValueError(message)


def money(value):
    require(isinstance(value, str), 'registrar quoted amounts must be decimal strings')
    amount = Decimal(value)
    require(amount.is_finite() and amount > 0, 'registrar quoted amounts must be positive finite bounds')
    return amount


def source_object(reference, source_reader):
    require(isinstance(reference, dict) and set(reference) == {'path', 'sha256'} and
            re.fullmatch(r'certification/hosted-pilot/quotes/[A-Za-z0-9_./-]+\.json', str(reference['path'])) and
            '..' not in reference['path'].split('/') and
            re.fullmatch(r'sha256:[a-f0-9]{64}', str(reference['sha256'])), 'registrar quote needs checked source references')
    path = Path(source_reader(reference['path'], reference['sha256']))
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 2*1024*1024,
            'registrar source original is missing or oversized')
    raw = path.read_bytes()
    require('sha256:'+hashlib.sha256(raw).hexdigest() == reference['sha256'], 'registrar source original bytes differ')
    return json.loads(raw)


def unit_price(original, *, region, usage, unit):
    require(isinstance(original, dict) and not original.get('NextToken') and len(original.get('PriceList', [])) == 1,
            'registrar pricing original must be complete and unambiguous')
    value = json.loads(original['PriceList'][0])
    attributes = value['product']['attributes']
    require(attributes.get('regionCode') == region and attributes.get('usagetype') == usage,
            'registrar original pricing region/meter differs')
    terms = list(value['terms']['OnDemand'].values())
    require(len(terms) == 1, 'registrar pricing has ambiguous offer terms')
    dimensions = list(terms[0]['priceDimensions'].values())
    require(len(dimensions) == 1 and dimensions[0]['unit'] == unit and
            dimensions[0]['beginRange'] == '0' and dimensions[0]['endRange'] == 'Inf' and
            set(dimensions[0]['pricePerUnit']) == {'USD'}, 'registrar pricing unit/range differs')
    return money(dimensions[0]['pricePerUnit']['USD'])


def verify_quote(config, durable, *, source_reader):
    quote = source_object(config['liabilityQuote'], source_reader)
    fields = {'schema', 'candidateKey', 'runtimeId', 'region', 'cpu', 'memory', 'maxExecutionSeconds',
              'image', 'maximumUsd', 'bounds', 'pricingOriginals', 'imageOriginal', 'sharedInfrastructureOriginal'}
    runtime = config['declaration']['path'].rsplit('/', 1)[-1].removesuffix('.json')
    require(set(quote) == fields and quote['schema'] == 'honua.registrar-liability-quote/v1' and
            quote['candidateKey'] == config['candidateKey'] and quote['runtimeId'] == runtime and
            quote['region'] == config['region'] == 'us-east-1' and quote['cpu'] == 256 and quote['memory'] == 1024 and
            type(config['maxExecutionSeconds']) is int and 1 <= config['maxExecutionSeconds'] <= 600 and
            quote['maxExecutionSeconds'] == config['maxExecutionSeconds'] and quote['image'] == config['imageUri'] and
            quote['maximumUsd'] == config['maximumUsd'] and set(config['maximumUsd']) == {'infrastructure', 'cleanup'},
            'registrar quote differs from exact reviewed configuration')
    ceilings = {kind: money(value) for kind, value in config['maximumUsd'].items()}
    bounds = quote['bounds']
    require(set(bounds) == {'maximumImagePullBytes', 'maximumRequestResponseBytes', 'maximumLogBytes',
            'maximumTaskStarts', 'maximumCleanupTaskStarts', 'cleanupMaxExecutionSeconds'} and
            all(type(v) is int and v > 0 for v in bounds.values()) and bounds['maximumTaskStarts'] == 1 and
            bounds['maximumCleanupTaskStarts'] == 1 and bounds['cleanupMaxExecutionSeconds'] <= 600,
            'registrar quote needs finite installation and cleanup resource bounds')
    meters = {'fargate-vcpu': ('USE1-Fargate-vCPU-Hours:perCPU', 'hours'),
              'fargate-memory': ('USE1-Fargate-GB-Hours', 'hours'),
              'nat-transfer': ('NatGateway-Bytes', 'GB'),
              'cloudwatch-ingestion': ('USE1-DataProcessing-Bytes', 'GB')}
    originals = quote['pricingOriginals']
    require(isinstance(originals, list) and len(originals) == 4 and all(isinstance(row, dict) and
            set(row) == {'kind', 'reference'} for row in originals) and
            {row['kind'] for row in originals} == set(meters), 'registrar original pricing meters are incomplete')
    prices = {row['kind']: unit_price(source_object(row['reference'], source_reader), region=config['region'],
              usage=meters[row['kind']][0], unit=meters[row['kind']][1]) for row in originals}
    image = source_object(quote['imageOriginal'], source_reader)
    uri = config['imageUri']
    match = re.fullmatch(r'([0-9]{12})\.dkr\.ecr\.(us-east-1)\.amazonaws\.com/([a-z0-9/_-]+)@(sha256:[a-f0-9]{64})', uri)
    require(match and set(image) == {'schema', 'region', 'accountId', 'repository', 'imageDigest', 'response'} and
            image['schema'] == 'honua.registrar-ecr-image-original/v1' and
            (image['accountId'], image['region'], image['repository'], image['imageDigest']) == match.groups() and
            len(image['response'].get('imageDetails', [])) == 1, 'registrar image original differs')
    observed = image['response']['imageDetails'][0]
    require(observed.get('registryId') == config['accountId'] == match[1] and observed.get('repositoryName') == match[3] and
            observed.get('imageDigest') == match[4] and type(observed.get('imageSizeInBytes')) is int and
            0 < observed['imageSizeInBytes'] <= bounds['maximumImagePullBytes'], 'registrar image transfer bound is insufficient')
    shared = source_object(quote['sharedInfrastructureOriginal'], source_reader)
    require(set(shared) == {'schema', 'candidateKey', 'operationId', 'basisDigest', 'reservationIds'} and
            shared['schema'] == 'honua.registrar-shared-infrastructure-binding/v1' and
            shared['candidateKey'] == config['candidateKey'] and set(shared['reservationIds']) == {'infrastructure', 'cleanup'} and
            len(set(shared['reservationIds'].values())) == 2 and not set(shared['reservationIds'].values()) &
            set(config['operation']['reservationIds'].values()), 'registrar shared infrastructure cannot reuse its new effect rows')
    for kind, rid in shared['reservationIds'].items():
        rows = [row for row in durable.state['ledger'].get('operations', []) if row['id'] == rid]
        require(len(rows) == 1 and rows[0]['operationId'] == shared['operationId'] and
                rows[0]['basisDigest'] == shared['basisDigest'] and rows[0]['kind'] == kind and
                rows[0]['purpose'] == 'shared-environment' and rows[0]['state'] == 'reserved' and rows[0]['actualUsd'] is None,
                'registrar shared network/reaper originals are not retained liabilities')
        money(rows[0]['amountUsd'])
    hourly = Decimal('0.25')*prices['fargate-vcpu']+prices['fargate-memory']
    # Decimal GiB transfer/log bounds conservatively include image and response
    # bytes; fixed shared network/reaper time remains separately reserved above.
    transfer = Decimal(bounds['maximumImagePullBytes']+bounds['maximumRequestResponseBytes'])/Decimal(1024**3)
    logs = Decimal(bounds['maximumLogBytes'])/Decimal(1024**3)
    base = hourly*Decimal(config['maxExecutionSeconds'])/Decimal(3600)+transfer*prices['nat-transfer']+logs*prices['cloudwatch-ingestion']
    cleanup = hourly*Decimal(bounds['cleanupMaxExecutionSeconds'])/Decimal(3600)+transfer*prices['nat-transfer']+logs*prices['cloudwatch-ingestion']
    require(ceilings['infrastructure'] >= base and ceilings['cleanup'] >= cleanup, 'registrar quote does not cover original bounded resources')
    return quote


def identity(config, durable, *, source_reader, clock):
    durable._require_live_lease()
    validate_retained_funding(durable.state)
    ledger = durable.state['ledger']
    validate_budget(ledger)
    require(config['state'] == {'region':config['region'], 'bucket':durable.store.bucket, 'table':durable.store.table},
            'registrar cannot substitute original accounting custody')
    funding = ledger.get('interactiveAccounting')
    require(isinstance(funding, dict) and type(funding.get('originalExpiresEpoch')) is int and
            clock() < funding['originalExpiresEpoch'] and ledger['candidateKey'] == durable.store.candidate == config['candidateKey'] and
            durable.store.generation['digest'] == config['generationDigest'] and
            durable.state.get('bootstrapImport', {}).get('artifactDigest') == config['bootstrapArtifactDigest'],
            'registrar original funding/generation is absent, changed or expired')
    runtime = config['declaration']['path'].rsplit('/', 1)[-1].removesuffix('.json')
    operation = config['operation']
    require(operation['operationId'] == 'runtime-registration-'+runtime and
            operation['liabilityBasisDigest'] == canonical_digest({k:v for k,v in config.items() if k != 'operation'}) and
            set(operation['reservationIds']) == {'infrastructure', 'cleanup'} and
            len(set(operation['reservationIds'].values())) == 2, 'registrar original operation/basis differs')
    verify_quote(config, durable, source_reader=source_reader)
    return runtime, funding['originalExpiresEpoch']


def admission_key(config):
    return 'runtime-registration-admission:'+canonical_digest({'candidateKey':config['candidateKey'],
        'operationId':config['operation']['operationId']})[7:]+'/admitted'


def admission_record(config, durable, *, runtime, funding_expiry, clock):
    operation = config['operation']
    row = durable.state.get('runtimeRegistrationAdmissions', {}).get(operation['operationId'])
    fields = {'schema', 'candidateKey', 'runtimeId', 'operationId', 'configurationDigest', 'basisDigest', 'quoteDigest',
              'admittedEpoch', 'deadlineEpoch', 'originalFundingExpiresEpoch', 'sourceHead'}
    require(isinstance(row, dict) and set(row) == fields and row['schema'] == 'honua.runtime-registration-admission/v1' and
            row['candidateKey'] == config['candidateKey'] and row['runtimeId'] == runtime and row['operationId'] == operation['operationId'] and
            row['configurationDigest'] == canonical_digest(config) and row['basisDigest'] == operation['liabilityBasisDigest'] and
            row['quoteDigest'] == config['liabilityQuote']['sha256'] and row['originalFundingExpiresEpoch'] == funding_expiry and
            type(row['admittedEpoch']) is int and row['admittedEpoch'] > 0 and type(row['deadlineEpoch']) is int and
            row['deadlineEpoch'] == min(row['admittedEpoch']+config['maxExecutionSeconds'], funding_expiry) and
            row['admittedEpoch'] <= clock() < row['deadlineEpoch'], 'registrar original admission is missing, changed or expired')
    original = durable.state.get('scenarioOperations', {}).get(admission_key(config))
    require(isinstance(original, dict) and set(original) == {'producer', 'candidateKey', 'value'} and
            original['producer'] == 'private-runtime-registration-admission' and
            original['candidateKey'] == config['candidateKey'] and original['value'] == row,
            'registrar admission lacks unchanged append-only original custody')
    head = row['sourceHead']
    require(isinstance(head, dict) and set(head) == {'generationDigest', 'stateVersion', 'snapshotDigest', 'snapshotVersion'} and
            head['generationDigest'] == config['generationDigest'] and type(head['stateVersion']) is int and head['stateVersion'] > 0 and
            re.fullmatch(r'sha256:[a-f0-9]{64}', str(head['snapshotDigest'])) and
            isinstance(head['snapshotVersion'], str) and head['snapshotVersion'] not in ('', 'null'), 'registrar original admitted state head differs')
    return copy.deepcopy(row)


def verify_retained_admission(config, durable, *, source_reader, clock=time.time):
    """Read-only guard for every registrar effect; never reserve or renew."""
    runtime, funding_expiry = identity(config, durable, source_reader=source_reader, clock=clock)
    row = admission_record(config, durable, runtime=runtime, funding_expiry=funding_expiry, clock=clock)
    operation = config['operation']
    for kind, rid in operation['reservationIds'].items():
        rows = [item for item in durable.state['ledger'].get('operations', []) if item['id'] == rid]
        require(len(rows) == 1 and rows[0]['operationId'] == operation['operationId'] and rows[0]['purpose'] == 'bootstrap' and
                rows[0]['kind'] == kind and rows[0]['basisDigest'] == operation['liabilityBasisDigest'] and
                rows[0]['state'] == 'reserved' and rows[0]['actualUsd'] is None and
                money(rows[0]['amountUsd']) >= money(config['maximumUsd'][kind]), 'registrar original quoted reservation differs')
    return copy.deepcopy(row)


def retain_admission_and_reservations(config, durable, *, source_reader, guard, clock=time.time):
    """Trusted producer only: original admission first, then two exact rows.

    The caller authenticates the checked deployment configuration before this
    function; guard must retain that source/current producer fencing on resume.
    """
    guard()
    runtime, expiry = identity(config, durable, source_reader=source_reader, clock=clock)
    operation = config['operation']
    admissions = durable.state.setdefault('runtimeRegistrationAdmissions', {})
    if operation['operationId'] not in admissions:
        require(admission_key(config) not in durable.state.get('scenarioOperations', {}),
                'registrar orphan admission custody requires reconciliation, never a new clock')
        require(not any(row['operationId'] == operation['operationId'] or row['id'] in operation['reservationIds'].values()
                for row in durable.state['ledger'].get('operations', [])), 'registrar orphan reservations require reconciliation, never a new clock')
        observed = durable.store.db.get_item(TableName=durable.store.table, Key=durable.store.key, ConsistentRead=True)['Item']
        require(observed['leaseOwner']['S'] == durable.store.owner and int(observed['fence']['N']) == durable.store.fence and
                int(observed['leaseUntil']['N']) > clock() and observed['generationDigest']['S'] == config['generationDigest'] and
                int(observed['stateVersion']['N']) == durable.store.version, 'registrar admission observed a different live state head')
        admitted = int(clock())
        admissions[operation['operationId']] = {'schema':'honua.runtime-registration-admission/v1', 'candidateKey':config['candidateKey'],
            'runtimeId':runtime, 'operationId':operation['operationId'], 'configurationDigest':canonical_digest(config),
            'basisDigest':operation['liabilityBasisDigest'], 'quoteDigest':config['liabilityQuote']['sha256'],
            'admittedEpoch':admitted, 'deadlineEpoch':min(admitted+config['maxExecutionSeconds'], expiry),
            'originalFundingExpiresEpoch':expiry, 'sourceHead':{'generationDigest':observed['generationDigest']['S'],
                'stateVersion':int(observed['stateVersion']['N']), 'snapshotDigest':observed['snapshotDigest']['S'],
                'snapshotVersion':observed['snapshotVersion']['S']}}
        durable.state.setdefault('scenarioOperations', {})[admission_key(config)] = {
            'producer':'private-runtime-registration-admission', 'candidateKey':config['candidateKey'],
            'value':copy.deepcopy(admissions[operation['operationId']])}
        guard(); durable._persist()
    admission_record(config, durable, runtime=runtime, funding_expiry=expiry, clock=clock)
    for kind, rid in operation['reservationIds'].items():
        guard()
        rows = [row for row in durable.state['ledger'].get('operations', []) if row['id'] == rid]
        if not rows:
            durable.update(durable.path, 'reserve-operation', candidate_key=config['candidateKey'], reservation_id=rid,
                operation_id=operation['operationId'], purpose='bootstrap', kind=kind,
                basis_digest=operation['liabilityBasisDigest'], amount=config['maximumUsd'][kind])
    guard()
    return verify_retained_admission(config, durable, source_reader=source_reader, clock=clock)
