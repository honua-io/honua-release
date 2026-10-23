#!/usr/bin/env python3
"""Collect authenticated read-only history for a reviewed pilot initialization.

This is evidence, never a ledger initializer or proof of whole-account zero cost.
Only resource identities and aggregate usage are persisted. Raw request payloads,
credential overrides, role policies and personal caller details are never cached.
"""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import hashlib
import json
import re
from pathlib import Path
import subprocess
from urllib.parse import urlencode

ACCOUNT = '585192672263'
REGION = 'us-east-1'
REPOSITORIES = ('honua-io/honua-release', 'honua-io/honua-iac')
PREFIXES = ('honua-pilot-', 'honua-release-pilot-', 'honua-agent-')
EVENTS = ('StartExecution', 'CreateStateMachine', 'RunTask', 'CreateRole', 'CreateBucket')


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(raw):
    return 'sha256:'+hashlib.sha256(raw).hexdigest()


def invoke(argv):
    result = subprocess.run(argv, capture_output=True, check=False)
    require(result.returncode == 0, 'authenticated metadata query failed: '+argv[0]+' '+argv[1])
    return json.loads(result.stdout)


def producer_path(path):
    return path.startswith('.github/workflows/hosted-pilot-') or path == '.github/workflows/agent-e2e-campaign.yml'


def dispatch_history(pages):
    require(isinstance(pages, list) and pages, 'dispatch pagination missing')
    total = pages[0]['total_count']
    rows = [row for page in pages for row in page['workflow_runs']]
    require(total <= 1000 and len(rows) == total and len({row['id'] for row in rows}) == total and
            all(page['total_count'] == total for page in pages), 'dispatch history incomplete, changed or capped; narrow the time window')
    require(all(row['event'] == 'workflow_dispatch' for row in rows), 'dispatch history contains another event')
    return {'totalRuns': total, 'producerRuns': [{key: row[key] for key in
        ('id', 'path', 'head_sha', 'head_branch', 'created_at', 'event', 'status', 'conclusion', 'run_attempt')}
        for row in rows if producer_path(row['path'])],
        'allRunBindingsDigest': digest(canonical([{key: row[key] for key in ('id', 'path', 'head_sha', 'created_at')} for row in rows]))}


def resource_identities(row, native):
    """Only API resource identities, never execution inputs/overrides/policies."""
    name = native['eventName']
    request = native.get('requestParameters') or {}
    response = native.get('responseElements') or {}
    fields = {
        'RunTask': ('cluster', 'taskDefinition'),
        'CreateRole': ('roleName',),
        'CreateBucket': ('bucketName',),
        'CreateStateMachine': ('name',),
        'StartExecution': ('stateMachineArn', 'name'),
    }[name]
    selected = [request.get(key) for key in fields]
    for key in ('stateMachineArn', 'executionArn'):
        selected.append(response.get(key))
    role = response.get('role') or {}
    selected.extend(role.get(key) for key in ('arn', 'roleName'))
    selected.extend(task.get(key) for task in response.get('tasks', []) for key in ('taskArn', 'clusterArn', 'taskDefinitionArn'))
    selected.extend(resource.get('ResourceName') for resource in row.get('Resources', []))
    # AWS identifiers have a narrow alphabet; arbitrary free-form values are not
    # useful identity evidence and cannot enter the retained projection.
    return sorted({value for value in selected if isinstance(value, str) and
                   0 < len(value) <= 512 and re.fullmatch(r'[A-Za-z0-9_+=,.@:/-]+', value)})


def event_history(value, event_name):
    require(not value.get('NextToken'), 'CloudTrail pagination incomplete')
    rows = value['Events']
    require(len({row['EventId'] for row in rows}) == len(rows), 'duplicate CloudTrail event')
    matches = []
    for row in rows:
        native = json.loads(row['CloudTrailEvent'])
        require(row['EventName'] == native['eventName'] == event_name and native['awsRegion'] == REGION,
                'CloudTrail event/region differs')
        bindings = {key: native.get(key) for key in ('requestParameters', 'responseElements', 'resources')}
        # Preserve the original conservative detection scope, including a pilot
        # mention outside projected identities. Payloads are inspected transiently
        # and hashed, never copied into the report or response cache.
        if any(prefix in canonical(bindings).decode() for prefix in PREFIXES):
            identities = resource_identities(row, native)
            error = native.get('errorCode')
            if error is not None and (not isinstance(error, str) or not re.fullmatch(r'[A-Za-z0-9_.]{1,80}', error)):
                error = 'unclassified'
            matches.append({'eventId': row['EventId'], 'eventTime': native['eventTime'],
                'eventName': event_name, 'resourceIdentities': identities, 'errorCode': error,
                'unprojectedNamespaceReference': not any(prefix in identity for prefix in PREFIXES for identity in identities),
                'nativeEventSha256': digest(canonical(native))})
    return {'totalEvents': len(rows), 'pilotEvents': matches,
            'allEventIdsDigest': digest(canonical(sorted(row['EventId'] for row in rows)))}


def cache_projection(label, value):
    """Persist only evidence needed for this audit; never an API response dump."""
    if label.startswith('cloudtrail-'):
        return event_history(value, label.removeprefix('cloudtrail-'))
    if label == 'caller':
        return {'Account': value['Account'], 'Arn': value['Arn']}
    if label.endswith('-trunk'):
        return {'name': value['name'], 'protected': value['protected'], 'commit': {'sha': value['commit']['sha']}}
    if label.endswith('-workflow-source'):
        return [{key: row[key] for key in ('path', 'sha', 'type')} for row in value]
    if label.endswith('-dispatches'):
        fields = ('id', 'path', 'head_sha', 'head_branch', 'created_at', 'event', 'status', 'conclusion', 'run_attempt')
        return [{'total_count': page['total_count'], 'workflow_runs': [
            {key: row[key] for key in fields} for row in page['workflow_runs']]} for page in value]
    if label == 'roles':
        return {'Roles': [{key: row[key] for key in ('RoleName', 'Arn')} for row in value['Roles']],
                'paginationComplete': not value.get('IsTruncated') and not value.get('Marker')}
    if label == 'buckets':
        return {'Buckets': [{'Name': row['Name']} for row in value['Buckets']]}
    if label == 'tables':
        return {'TableNames': value['TableNames'], 'paginationComplete': not value.get('LastEvaluatedTableName')}
    if label == 'account-costs':
        return costs(value)
    raise ValueError('history response has no reviewed identity projection')


def costs(value):
    require(not value.get('NextPageToken') and value.get('ResultsByTime'), 'cost history is incomplete')
    records = Counter()
    services = Counter()
    periods = []
    for period in value['ResultsByTime']:
        periods.append({**period['TimePeriod'], 'estimated': period['Estimated']})
        for row in period['Groups']:
            service, kind = row['Keys']
            metric = row['Metrics']['UnblendedCost']
            require(metric['Unit'] == 'USD', 'cost history unit differs')
            amount = Decimal(metric['Amount'])
            require(amount.is_finite(), 'nonfinite account cost')
            records[kind] += amount
            if kind == 'Usage':
                services[service] += amount
    return {'periods': periods, 'recordTypeUsd': {k: str(v) for k,v in sorted(records.items())},
        'grossUsageUsd': str(records['Usage']), 'usageByServiceUsd': {k: str(v) for k,v in sorted(services.items())},
        'pilotAttribution': 'unknown', 'settledPilotActualUsd': None,
        'creditsReduceConservativeHold': False}


def collect(start, cutoff, output, *, call=invoke):
    begin = datetime.fromisoformat(start.replace('Z', '+00:00'))
    end = datetime.fromisoformat(cutoff.replace('Z', '+00:00'))
    now = datetime.now(timezone.utc)
    require(begin.tzinfo and end.tzinfo and begin < end <= now and now-begin < timedelta(days=89),
            'history window must be explicit, elapsed and within CloudTrail event retention')
    output = Path(output)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    evidence = {}
    def query(label, argv):
        value = call(argv)
        response_sha256 = digest(canonical(value)+b'\n')
        raw = canonical(cache_projection(label, value))+b'\n'
        path = output/(label+'.json')
        path.write_bytes(raw); path.chmod(0o600)
        evidence[label] = {'path': path.name, 'sha256': digest(raw), 'responseSha256': response_sha256,
                           'projection': 'identity-and-aggregate-usage-only', 'command': argv}
        return value
    identity = query('caller', ['aws', 'sts', 'get-caller-identity', '--output', 'json'])
    require(identity['Account'] == ACCOUNT, 'wrong budget history account')
    repos = {}
    for repo in REPOSITORIES:
        label = repo.rsplit('/', 1)[1]
        branch = query(label+'-trunk', ['gh', 'api', 'repos/'+repo+'/branches/trunk'])
        require(branch['name'] == 'trunk' and branch['protected'] is True, 'history requires protected trunk')
        sha = branch['commit']['sha']
        source = query(label+'-workflow-source', ['gh', 'api', 'repos/'+repo+'/contents/.github/workflows?ref='+sha])
        pages = query(label+'-dispatches', ['gh', 'api', '--paginate', '--slurp', 'repos/'+repo+'/actions/runs?'+urlencode({
            'event': 'workflow_dispatch', 'created': start+'..'+cutoff, 'per_page': 100})])
        repos[repo] = {**dispatch_history(pages), 'protectedTrunkSha': sha,
            'producerPathsOnTrunk': sorted(row['path'] for row in source if producer_path(row['path']))}
    cloud = {}
    for name in EVENTS:
        value = query('cloudtrail-'+name, ['aws', 'cloudtrail', 'lookup-events', '--region', REGION,
            '--lookup-attributes', 'AttributeKey=EventName,AttributeValue='+name,
            '--start-time', start, '--end-time', cutoff, '--output', 'json'])
        cloud[name] = event_history(value, name)
    roles = query('roles', ['aws', 'iam', 'list-roles', '--output', 'json'])
    require(not roles.get('IsTruncated') and not roles.get('Marker'), 'IAM role history incomplete')
    pilot_roles = [row['Arn'] for row in roles['Roles'] if row['RoleName'].startswith(PREFIXES)]
    buckets = query('buckets', ['aws', 's3api', 'list-buckets', '--output', 'json'])
    tables = query('tables', ['aws', 'dynamodb', 'list-tables', '--region', REGION, '--output', 'json'])
    require(not tables.get('LastEvaluatedTableName'), 'DynamoDB inventory incomplete')
    resources = {'pilotRoles': pilot_roles,
        'pilotBuckets': [row['Name'] for row in buckets['Buckets'] if row['Name'].startswith(PREFIXES)],
        'pilotTables': [name for name in tables['TableNames'] if name.startswith(PREFIXES)]}
    # One explicitly accounted billable request. Its own charge may not yet be
    # represented in this lagged response, so retain an additional nonzero hold.
    usage = query('account-costs', ['aws', 'ce', 'get-cost-and-usage', '--region', REGION,
        '--time-period', 'Start='+begin.date().isoformat()+',End='+end.date().isoformat(),
        '--granularity', 'DAILY', '--metrics', 'UnblendedCost', '--group-by',
        'Type=DIMENSION,Key=SERVICE', 'Type=DIMENSION,Key=RECORD_TYPE', '--output', 'json'])
    usage_totals = costs(usage)
    usage_totals['collectorRequestFeeLowerBoundUsd'] = '0.01'
    usage_totals['pricingSource'] = 'https://aws.amazon.com/aws-cost-management/aws-cost-explorer/pricing/'
    no_observed_producer = not any(row['producerRuns'] for row in repos.values()) and not any(row['pilotEvents'] for row in cloud.values())
    report = {'schema': 'honua.hosted-pilot-budget-history/v1', 'accountId': ACCOUNT, 'resourceRegion': REGION,
        'observedAt': datetime.now(timezone.utc).isoformat(), 'coverage': {'start': start, 'cutoff': cutoff,
            'workflowEvents': ['workflow_dispatch'], 'cloudTrailEvents': list(EVENTS), 'cloudTrailRegion': REGION},
        'repositories': repos, 'cloudTrail': cloud, 'currentNamespace': resources, 'billing': usage_totals,
        'boundedFinding': 'no pilot producer dispatch or named resource creation observed in covered history' if no_observed_producer else 'pilot producer/resource history requires reconciliation',
        'initializationAllowed': False, 'wholeAccountZeroSpend': False,
        'policy': {'pilotCeilingUsd': '2000', 'pilotProtectedCleanupUsd': '200', 'maximumActivePaidAttempts': 4,
            'legacyCeilingUsd': '500', 'legacyProtectedCleanupUsd': '50', 'legacyHistoryMayBeReset': False},
        'requiredBeforeInitialization': ['review candidate-bound source reservations and evidence',
            'carry gross usage and diagnostic fees as unresolved holds until attribution/settlement',
            'retain all existing legacy liabilities and current live resource/cleanup obligations',
            'recheck namespace and durable object history before any create-only generation write'],
        'limitations': ['CloudTrail ingestion and billing have reporting lag', 'named events in us-east-1 do not prove absence of all spending or all resource classes',
            'deleted GitHub runs and activity outside the covered window are not reconstructed', 'absence of current resources is never an independent zero-cost proof'],
        'evidence': evidence}
    (output/'history.json').write_bytes(canonical(report)+b'\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', required=True)
    parser.add_argument('--cutoff', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    report = collect(args.start, args.cutoff, args.output)
    print(json.dumps({'status': 'observed', 'historySha256': digest((args.output/'history.json').read_bytes()),
                      'initializationAllowed': report['initializationAllowed'], 'wholeAccountZeroSpend': False}))


if __name__ == '__main__':
    main()
