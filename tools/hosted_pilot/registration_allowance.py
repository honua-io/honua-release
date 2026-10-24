"""Original registrar quote/admission; no allowance initialization or renewal.

The trusted source producer retains one absolute admission before reserving its
two distinct rows. The registrar consumes only verify_retained_admission.
"""
from __future__ import annotations

import copy
from decimal import Decimal, ROUND_CEILING
from datetime import datetime, timezone
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



def shared_history_start(durable, source_reader):
    """Conservative original coverage start; never manufacture a ledger clock."""
    funding = json.loads(durable.state['interactiveFunding']['configuration'])
    seen = set()
    cached = {}
    def read(reference):
        require(isinstance(reference,dict) and set(reference) == {'path','sha256'} and
                reference['path'].startswith(('certification/hosted-pilot/history/','certification/agent-e2e/')) and
                '..' not in reference['path'].split('/') and re.fullmatch(r'sha256:[a-f0-9]{64}',reference['sha256']),
                'shared coverage requires the retained original funding history reference')
        if reference['sha256'] in cached:return cached[reference['sha256']]
        require(reference['sha256'] not in seen and len(seen)+len(cached) < 32, 'shared history contains cycles or too many originals')
        seen.add(reference['sha256'])
        path=Path(source_reader(reference['path'],reference['sha256']))
        require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 2*1024*1024,
                'shared original history is absent or oversized')
        raw=path.read_bytes()
        require('sha256:'+hashlib.sha256(raw).hexdigest() == reference['sha256'], 'shared original history bytes differ')
        value=json.loads(raw)
        if value.get('schema') == 'honua.hosted-pilot-reconciled-history/v1':
            require(set(value) == {'schema','currentHistory','retainedHistories','producerReconciliations'} and
                    isinstance(value['retainedHistories'],list), 'shared reconciled original history differs')
            result=min(read(ref) for ref in [value['currentHistory'],*value['retainedHistories']])
            seen.remove(reference['sha256']);cached[reference['sha256']]=result
            return result
        require(value.get('schema') == 'honua.hosted-pilot-budget-history/v1', 'shared original history schema differs')
        start=datetime.fromisoformat(value['coverage']['start'].replace('Z','+00:00'))
        require(start.tzinfo is not None and start.utcoffset().total_seconds() == 0 and
                start.timestamp() > 0 and start.timestamp() == int(start.timestamp()), 'shared original history start is invalid')
        result=int(start.timestamp());seen.remove(reference['sha256']);cached[reference['sha256']]=result
        return result
    return funding['history'],read(funding['history'])



def verify_shared_source_bounds():
    """Refuse source drift that invalidates the finite quote's primitive bounds."""
    root=Path(__file__).resolve().parents[2]/'certification/hosted-pilot/campaign'
    network=(root/'target.tf').read_text()
    require(re.search(r'single_nat_gateway\s*=\s*true',network) and
            re.search(r'enable_nat_gateway\s*=\s*true',network), 'shared single NAT primitive differs')
    source=(root/'runtime-registration-reaper.asl.json.tftpl').read_text()
    machine=json.loads(source.replace('${installation_json}','{}'))
    require(machine['States']['ListOriginalTasks']['Arguments']['MaxResults'] == 100,
            'shared reaper inventory bound differs')
    def bound(states):
        total=len(states)
        edges={name:set() for name in states}
        def targets(value):
            result=[]
            if isinstance(value,dict):
                require('Retry' not in value, 'shared reaper retry costs are unquoted')
                for key,item in value.items():
                    if key=='ItemProcessor':continue
                    if key in ('Next','Default'):result.append(item)
                    else:result+=targets(item)
            elif isinstance(value,list):
                for item in value:result+=targets(item)
            return result
        for name,row in states.items():
            edges[name]=set(targets(row))
            require(edges[name] <= set(states),'shared reaper transition target differs')
            if row['Type']=='Map':
                require(row.get('MaxConcurrency') == 1,'shared reaper concurrency differs')
                total+=100*bound(row['ItemProcessor']['States'])
        def visit(name,active):
            require(name not in active,'shared reaper cycle costs are unquoted')
            for target in edges[name]:visit(target,active|{name})
        for name in states:visit(name,set())
        return total
    require(bound(machine['States']) <= 512 and source.count('aws-sdk:dynamodb:getItem') == 1 and
            not any(operation in source for operation in ('dynamodb:updateItem','dynamodb:putItem','dynamodb:query','dynamodb:scan')),
            'shared reaper state/read bounds differ')
    lifecycle=(root/'runtime-registration-lifecycle.tf').read_text()
    require('schedule_expression          = "rate(1 minute)"' in lifecycle and
            re.search(r'maximum_retry_attempts\s*=\s*0',lifecycle) and
            'timeadd("1970-01-01T00:00:00Z", "${var.native_runtime_registration.originalFundingExpiresEpoch}s")' in lifecycle,
            'shared reaper schedule exceeds original funding or invocation bounds')
    logs=(root/'controller.tf').read_text()
    block=re.search(r'resource "aws_cloudwatch_log_group" "controller"\s*\{.*?\n\}',logs,re.S)
    require(block and re.search(r'retention_in_days\s*=\s*14',block[0]),
            'shared log retention bound differs')

def scheduler_price(original, *, region):
    require(isinstance(original,dict) and not original.get('NextToken') and len(original.get('PriceList',[])) == 1,
            'shared scheduler pricing original is incomplete')
    value=json.loads(original['PriceList'][0]);attributes=value['product']['attributes']
    require(attributes.get('regionCode') == region and attributes.get('usagetype') == 'USE1-ScheduledInvocation',
            'shared scheduler pricing region/meter differs')
    terms=list(value['terms']['OnDemand'].values())
    require(len(terms) == 1,'shared scheduler pricing terms are ambiguous')
    rows=sorted(terms[0]['priceDimensions'].values(),key=lambda row:Decimal(row['beginRange']))
    require(len(rows) == 2 and [(row['beginRange'],row['endRange'],row['unit']) for row in rows] ==
            [('0','14000000','Invocations'),('14000000','Inf','Invocations')] and
            all(set(row['pricePerUnit']) == {'USD'} for row in rows) and
            Decimal(rows[0]['pricePerUnit']['USD']) == 0, 'shared scheduler pricing tiers differ')
    # Quote the paid tier for every invocation; account free-tier availability is
    # never assumed and existing unknown costs remain separate.
    return money(rows[1]['pricePerUnit']['USD'])


def verify_quote(config, durable, *, source_reader):
    verify_shared_source_bounds()
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
            'maximumTaskStarts', 'maximumCleanupTaskStarts', 'cleanupMaxExecutionSeconds',
            'maximumStateTransitions', 'maximumDynamoReadRequestUnits', 'maximumDynamoWriteRequestUnits'} and
            all(type(v) is int and v > 0 for v in bounds.values()) and bounds['maximumTaskStarts'] == 1 and
            bounds['maximumCleanupTaskStarts'] == 1 and bounds['cleanupMaxExecutionSeconds'] <= 600 and
            bounds['maximumStateTransitions'] >= 1200 and bounds['maximumDynamoReadRequestUnits'] >= 512 and
            bounds['maximumDynamoWriteRequestUnits'] >= 256,
            'registrar quote needs finite installation and cleanup resource bounds')
    meters = {'fargate-vcpu': ('USE1-Fargate-vCPU-Hours:perCPU', 'hours'),
              'fargate-memory': ('USE1-Fargate-GB-Hours', 'hours'),
              'nat-transfer': ('NatGateway-Bytes', 'GB'),
              'cloudwatch-ingestion': ('USE1-DataProcessing-Bytes', 'GB'),
              'standard-state-transitions': ('USE1-StateTransition', 'StateTransitions'),
              'dynamodb-read': ('ReadRequestUnits', 'ReadRequestUnits'),
              'dynamodb-write': ('WriteRequestUnits', 'WriteRequestUnits'),
              'nat-hours': ('NatGateway-Hours','Hrs'),
              'cloudwatch-retention': ('USE1-TimedStorage-ByteHrs','GB-Mo'),
              'scheduler-invocations': ('USE1-ScheduledInvocation','Invocations')}
    originals = quote['pricingOriginals']
    require(isinstance(originals, list) and len(originals) == len(meters) and all(isinstance(row, dict) and
            set(row) == {'kind', 'reference'} for row in originals) and
            {row['kind'] for row in originals} == set(meters), 'registrar original pricing meters are incomplete')
    prices = {row['kind']: (scheduler_price(source_object(row['reference'],source_reader),region=config['region'])
              if row['kind'] == 'scheduler-invocations' else
              unit_price(source_object(row['reference'], source_reader), region=config['region'],
              usage=meters[row['kind']][0], unit=meters[row['kind']][1])) for row in originals}
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
    require(set(shared) == {'schema', 'candidateKey', 'operationId', 'basisDigest', 'reservationIds', 'historyOriginal',
            'originalFundingExpiresEpoch', 'plannedRetirementAtEpoch'} and
            shared['schema'] == 'honua.registrar-shared-infrastructure-binding/v2' and
            shared['candidateKey'] == config['candidateKey'] and set(shared['reservationIds']) == {'infrastructure', 'cleanup'} and
            len(set(shared['reservationIds'].values())) == 2 and not set(shared['reservationIds'].values()) &
            set(config['operation']['reservationIds'].values()), 'registrar shared infrastructure cannot reuse its new effect rows')
    history, start = shared_history_start(durable,source_reader)
    expiry = durable.state['ledger']['interactiveAccounting']['originalExpiresEpoch']
    require(shared['historyOriginal'] == history and shared['originalFundingExpiresEpoch'] == expiry and
            shared['plannedRetirementAtEpoch'] == expiry-900 and 0 < start < expiry-900,
            'shared coverage must preserve original history and funding/cleanup deadlines')
    def shared_cost(seconds):
        invocations=(seconds+59)//60+2
        # One reviewed single NAT gateway; the reaper is no-retry, max100 tasks,
        # <=512 transitions and <=128 consistent DDB request units per run.
        control=Decimal(invocations)*(512*prices['standard-state-transitions']+
                    128*prices['dynamodb-read']+prices['scheduler-invocations'])
        # Actual controller log retention is14 days. Use the shortest28-day
        # billing month and charge both bounded task starts; never apply that
        # object-retention period as a NAT cleanup guarantee.
        storage=Decimal(2*bounds['maximumLogBytes'])/Decimal(1024**3)*Decimal(14)/Decimal(28)*prices['cloudwatch-retention']
        return Decimal(seconds)/3600*prices['nat-hours']+control+storage
    shared_minimum={'infrastructure':shared_cost(expiry-start+120),'cleanup':shared_cost(900+120)}
    for kind, rid in shared['reservationIds'].items():
        rows = [row for row in durable.state['ledger'].get('operations', []) if row['id'] == rid]
        require(len(rows) == 1 and rows[0]['operationId'] == shared['operationId'] and
                rows[0]['basisDigest'] == shared['basisDigest'] and rows[0]['kind'] == kind and
                rows[0]['purpose'] == 'shared-environment' and rows[0]['state'] == 'reserved' and rows[0]['actualUsd'] is None,
                'registrar shared network/reaper originals are not retained liabilities')
        require(money(rows[0]['amountUsd']) >= shared_minimum[kind],
                'shared reservation does not cover the whole original network/reaper window and log retention')
    hourly = Decimal('0.25')*prices['fargate-vcpu']+prices['fargate-memory']
    # Decimal GiB transfer/log bounds conservatively include image and response
    # bytes; fixed shared network/reaper time remains separately reserved above.
    transfer = Decimal(bounds['maximumImagePullBytes']+bounds['maximumRequestResponseBytes'])/Decimal(1024**3)
    logs = Decimal(bounds['maximumLogBytes'])/Decimal(1024**3)
    # Standard lifecycle transitions and transactional state/claim requests are
    # paid effects too. Quoted bounds include full native GET/POST-once custody,
    # fenced head persistence and the no-retry lifecycle's maximum polling path.
    control = (Decimal(bounds['maximumStateTransitions'])*prices['standard-state-transitions']+
               Decimal(bounds['maximumDynamoReadRequestUnits'])*prices['dynamodb-read']+
               Decimal(bounds['maximumDynamoWriteRequestUnits'])*prices['dynamodb-write'])
    base = control+hourly*Decimal(config['maxExecutionSeconds'])/Decimal(3600)+transfer*prices['nat-transfer']+logs*prices['cloudwatch-ingestion']
    cleanup = control+hourly*Decimal(bounds['cleanupMaxExecutionSeconds'])/Decimal(3600)+transfer*prices['nat-transfer']+logs*prices['cloudwatch-ingestion']
    require(ceilings['infrastructure'] >= base and ceilings['cleanup'] >= cleanup, 'registrar quote does not cover original bounded resources')
    return quote


def identity(config, durable, *, source_reader, clock):
    durable._require_live_lease()
    return _retained_identity(config, durable, source_reader=source_reader, clock=clock)


def _retained_identity(config, durable, *, source_reader, clock):
    validate_retained_funding(durable.state)
    ledger = durable.state['ledger']
    validate_budget(ledger)
    require(config['state'] == {'region':config['region'], 'bucket':durable.store.bucket, 'table':durable.store.table},
            'registrar cannot substitute original accounting custody')
    funding = ledger.get('interactiveAccounting')
    require(isinstance(funding, dict) and type(funding.get('originalExpiresEpoch')) is int and
            clock() < funding['originalExpiresEpoch']-900 and ledger['candidateKey'] == durable.store.candidate == config['candidateKey'] and
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
            row['deadlineEpoch'] == min(row['admittedEpoch']+config['maxExecutionSeconds'], funding_expiry-900) and
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
    durable._require_live_lease()
    return _verify_retained_admission(config, durable, source_reader=source_reader, clock=clock)


def _verify_retained_admission(config, durable, *, source_reader, clock):
    """Shared pure checks, called only after lease or original-SDK authentication."""
    runtime, funding_expiry = _retained_identity(config, durable, source_reader=source_reader, clock=clock)
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
            'admittedEpoch':admitted, 'deadlineEpoch':min(admitted+config['maxExecutionSeconds'], expiry-900),
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
