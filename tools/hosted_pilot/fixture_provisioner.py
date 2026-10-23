#!/usr/bin/env python3
"""One fenced, retained P10 ownership transfer in a private privileged task.

The controller and workers never receive the application database login. This
task cannot create a ledger, synthesize a fixture, retry a claimed effect, or
select tables by prefix. A failed/ambiguous task retains its claim and liability.
"""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_e2e_pack import canonical_digest
from agent_e2e_budget import validate as validate_ledger
from fixture_handoff import IMPORT_PROFILE, encoded, ownership_claim, require, sdk, validate_request
from controller_budget import read_version


def observe_authority(config, envelope, s3, db):
    request, fence, context = envelope['request'], envelope['fence'], envelope['context']
    require(set(fence) == {'owner', 'fence', 'candidateKey', 'stateTable', 'stateBucket', 'stateKey'} and
            fence['candidateKey'] == config['candidateKey'] == request['candidateKey'] and
            fence['stateTable'] == config['state']['table'] and fence['stateBucket'] == config['state']['bucket'] and
            fence['stateKey'] == {'candidateKey': {'S': config['candidateKey']}}, 'fixture state namespace differs')
    row = db.get_item(TableName=config['state']['table'], Key=fence['stateKey'], ConsistentRead=True).get('Item', {})
    require(row.get('leaseOwner', {}).get('S') == fence['owner'] and
            int(row.get('fence', {}).get('N', '-1')) == fence['fence'] and
            int(row.get('leaseUntil', {}).get('N', '0')) >= int(time.time()) + 10,
            'fixture handoff no longer owns a live native controller fence')
    require(row.get('generationDigest', {}).get('S') == config['generationDigest'] and
            row.get('snapshotKey', {}).get('S', '').startswith('agent-e2e/' + config['candidateKey'][7:] + '/'),
            'fixture handoff changed original accounting generation')
    raw = read_version(s3, config['state']['bucket'], row['snapshotKey']['S'], row['snapshotVersion']['S'])
    snapshot = json.loads(raw)
    require('sha256:' + hashlib.sha256(raw).hexdigest() == row['snapshotDigest']['S'] and
            snapshot['candidateKey'] == config['candidateKey'] and snapshot['generationDigest'] == config['generationDigest'] and
            snapshot['version'] == int(row['stateVersion']['N']), 'fixture retained state digest differs')
    identity = {key: context[key] for key in ('campaignId', 'caseId', 'attempt')}
    key = canonical_digest(identity)
    imported = request.get('profile') == IMPORT_PROFILE
    request_digest = canonical_digest(request)
    retained_key = envelope['retained'].get('key')
    allowed_keys = (key, key+'/'+request_digest) if imported else (key,)
    require(context['campaignMode'] in ('development', 'qualification') and
            set(envelope['retained']) == {'key', 'sha256'} and retained_key in allowed_keys and
            envelope['retained']['sha256'] == request_digest, 'fixture retained request identity differs')
    state = snapshot['payload']
    validate_ledger(state['ledger'])
    require(state['ledger']['candidateKey'] == config['candidateKey'], 'fixture allowance belongs to another candidate')
    if imported:
        from agent_e2e_state import validate_retained_import_transfer
        require(validate_retained_import_transfer(state, context, request, config['candidateKey']) == key,
                'model import native original revalidation differs from original attempt')
    request_map = 'importTransferRequests' if imported else 'fixtureTransferRequests'
    if imported:
        identical = {name for name, value in state.get(request_map, {}).items()
                     if (name == key or name.startswith(key+'/')) and value == request}
        require(identical == {retained_key}, 'model import cannot migrate an original request into a new claim')
    require(state.get(request_map, {}).get(retained_key) == request and
            canonical_digest(state.get('fixtureBaselines', {}).get(key)) == request['baselineDigest'],
            'fixture transfer is absent from authenticated retained state')
    baseline = state['fixtureBaselines'][key]
    require(baseline['database'] == request['database'] and not
            ({table['oid'] for table in request['tables']} & {table['oid'] for table in baseline['snapshot']['relations']}),
            'fixture transfer includes a preexisting source relation')
    entries = [entry for entry in state['ledger']['entries'] if entry['state'] == 'reserved' and
               all(entry.get(name) == value for name, value in identity.items()) and
               entry.get('campaignMode', 'qualification') == context['campaignMode']]
    require({'infrastructure', 'cleanup'} <= {entry['kind'] for entry in entries}, 'fixture transfer lacks retained effect and cleanup liabilities')
    return row


def master_settings(raw, config):
    # The pinned IaC module excludes connection-string delimiters from its
    # generated password. Refuse alternate syntaxes instead of guessing a parser.
    pairs = [part.split('=', 1) for part in raw.split(';') if part]
    require(all(len(part) == 2 for part in pairs), 'invalid private master connection binding')
    settings = dict(pairs)
    require(len(settings) == len(pairs) and set(settings) <= {'Host', 'Port', 'Database', 'Username', 'Password', 'SSL Mode', 'Trust Server Certificate'} and
            set(settings) >= {'Host', 'Port', 'Database', 'Username', 'Password'} and
            settings['Host'] == config['databaseHost'] and settings['Database'] == config['database'] and
            settings['Port'] == '5432' and settings['Username'] == config['appRole'] and
            re.fullmatch(r'[A-Za-z0-9#%*()_+\[\]{}:?.-]{32,128}', settings['Password']), 'master connection differs from reviewed isolated target')
    return {'host': settings['Host'], 'port': 5432, 'dbname': settings['Database'], 'user': settings['Username'],
            'password': settings['Password'], 'sslmode': 'verify-full',
            'sslrootcert': '/controller/certification/hosted-pilot/controller/rds-global-bundle.pem'}


def transfer(connection, request, *, authorize):
    from psycopg import sql
    authorize()
    with connection.transaction():
        connection.execute("SET LOCAL statement_timeout = '5s'")
        connection.execute("SET LOCAL lock_timeout = '1s'")
        identity = connection.execute('''SELECT current_database() AS database,
            (SELECT oid::bigint FROM pg_database WHERE datname = current_database()) AS "databaseOid",
            inet_server_addr()::text AS "serverAddress", inet_server_port() AS "serverPort",
            (SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()) AS ssl''').fetchone()
        require(identity == request['database'] | {'ssl': True}, 'fixture privileged SQL connection differs from observed database')
        flags = connection.execute('''SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls
            FROM pg_roles WHERE rolname = %s''', (request['expectedCleanupRole'],)).fetchone()
        require(flags is not None and not any(flags.values()), 'cleanup role must already exist without database administration')
        for table in sorted(request['tables'], key=lambda row: row['oid']):
            connection.execute(sql.SQL('LOCK TABLE {} IN ACCESS EXCLUSIVE MODE').format(sql.Identifier(table['schema'], table['name'])))
        rows = connection.execute('''SELECT c.oid::bigint AS oid, n.nspname AS schema, c.relname AS name,
            pg_get_userbyid(c.relowner) AS owner FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.oid = ANY(%s::oid[]) AND c.relkind = 'r' ORDER BY c.oid''',
            ([table['oid'] for table in request['tables']],)).fetchall()
        require(rows == sorted(request['tables'], key=lambda row: row['oid']), 'fixture source OID/name/owner changed before transfer')
        require(connection.execute("SELECT 1 FROM pg_event_trigger WHERE evtenabled <> 'D' LIMIT 1").fetchone() is None,
                'fixture ownership refuses unknown DDL event triggers')
        if request.get('profile') == IMPORT_PROFILE:
            markers = connection.execute("SELECT oid::bigint AS oid, obj_description(oid, 'pg_class') AS marker FROM pg_class WHERE oid=ANY(%s::oid[])",
                                         ([table['oid'] for table in rows],)).fetchall()
            require(len(markers) == len(rows) and all(row['marker'] == 'honua:import-owned:v1' for row in markers),
                    'model import output lost its actual native import-owned table marker')
        authorize()
        for table in rows:
            connection.execute(sql.SQL('ALTER TABLE {} OWNER TO {}').format(
                sql.Identifier(table['schema'], table['name']), sql.Identifier(request['expectedCleanupRole'])))
            # The read-only P10 model still queries through the application.
            # Changing ownership must retain that exact original app's SELECT.
            connection.execute(sql.SQL('GRANT SELECT ON TABLE {} TO {}').format(
                sql.Identifier(table['schema'], table['name']), sql.Identifier(table['owner'])))
        after = connection.execute('''SELECT c.oid::bigint AS oid, n.nspname AS schema, c.relname AS name,
            pg_get_userbyid(c.relowner) AS owner FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.oid = ANY(%s::oid[]) ORDER BY c.oid''', ([table['oid'] for table in rows],)).fetchall()
        require(after == [table | {'owner': request['expectedCleanupRole']} for table in rows], 'fixture owner effects differ')
        authorize()
    return [table | {'owner': request['expectedCleanupRole']} for table in request['tables']]


def main():
    require(sys.argv[1:] == ['transfer-ownership'] and os.getuid() == 10001, 'fixture provisioner requires its fixed nonroot entry')
    config = json.loads(os.environ['PILOT_PROVISIONER_CONFIG'])
    pointer = json.loads(os.environ['PILOT_FIXTURE_REQUEST'])
    require(set(config) == {'schemaVersion', 'region', 'accountId', 'candidateKey', 'generationDigest', 'state',
            'bucket', 'requestPrefix', 'receiptPrefix', 'claimTable', 'clusterArn', 'taskDefinitionFamily',
            'imageUri', 'databaseHost', 'database', 'appRole', 'cleanupRole'} and config['schemaVersion'] == 1,
            'fixture provisioner reviewed base configuration missing')
    require(pointer['bucket'] == config['bucket'] and pointer['key'].startswith(config['requestPrefix']) and
            re.fullmatch(r'sha256:[a-f0-9]{64}', pointer['claimKey']), 'fixture request escaped fixed custody')
    s3, db, ecs = (sdk(service, config['region']) for service in ('s3', 'dynamodb', 'ecs'))
    require(sdk('sts', config['region']).get_caller_identity()['Account'] == config['accountId'], 'fixture AWS account differs')
    raw = read_version(s3, pointer['bucket'], pointer['key'], pointer['versionId'], limit=65536)
    require('sha256:' + hashlib.sha256(raw).hexdigest() == pointer['sha256'], 'fixture immutable request digest differs')
    envelope = json.loads(raw)
    require(set(envelope) == {'schema', 'request', 'context', 'retained', 'fence', 'provisionerConfigSha256'} and
            envelope['schema'] == 'honua.fixture-ownership-handoff/v1' and
            envelope['provisionerConfigSha256'] == canonical_digest(config), 'fixture request configuration differs')
    request = envelope['request']
    validate_request(request)
    require(request['database']['database'] == config['database'] and request['expectedCleanupRole'] == config['cleanupRole'] and
            all(table['owner'] == config['appRole'] for table in request['tables']) and
            pointer['claimKey'] == ownership_claim(request, envelope['context'], envelope['retained']) and
            pointer['key'] == config['requestPrefix'] + pointer['claimKey'][7:] + '.json', 'fixture effect differs from exact reviewed application-owned request')
    metadata_uri = os.environ['ECS_CONTAINER_METADATA_URI_V4']
    require(re.fullmatch(r'http://169\.254\.170\.2/v4/[A-Za-z0-9_-]+', metadata_uri), 'fixture native task metadata missing')
    from controller_entry import http_json
    metadata = http_json(metadata_uri + '/task', limit=65536)
    task_arn = metadata['TaskARN']
    observed = ecs.describe_tasks(cluster=config['clusterArn'], tasks=[task_arn])
    require(not observed.get('failures') and len(observed.get('tasks', [])) == 1, 'fixture task cannot be observed')
    task = observed['tasks'][0]
    require(task['taskDefinitionArn'].rsplit(':', 1)[0] == config['taskDefinitionFamily'] and
            len(task['containers']) == 1 and task['containers'][0]['image'] == config['imageUri'] and
            not task.get('enableExecuteCommand', True), 'fixture task revision/image differs')
    authorize = lambda: observe_authority(config, envelope, s3, db)
    row = authorize()
    # Conditional transaction claims once while the original controller still
    # owns the exact state snapshot. A crash never removes or resets this claim.
    db.transact_write_items(TransactItems=[{'ConditionCheck': {
        'TableName': config['state']['table'], 'Key': envelope['fence']['stateKey'],
        'ConditionExpression': 'leaseOwner = :owner AND fence = :fence AND snapshotDigest = :snapshot AND leaseUntil >= :now',
        'ExpressionAttributeValues': {':owner': row['leaseOwner'], ':fence': row['fence'], ':snapshot': row['snapshotDigest'], ':now': {'N': str(int(time.time()) + 10)}}}},
        {'Put': {'TableName': config['claimTable'], 'Item': {'claimKey': {'S': pointer['claimKey']},
            'requestDigest': {'S': pointer['sha256']}, 'taskArn': {'S': task_arn}}, 'ConditionExpression': 'attribute_not_exists(claimKey)'}}])
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(**master_settings(os.environ['PILOT_FIXTURE_MASTER_CONNECTION'], config),
                         connect_timeout=5, row_factory=dict_row, autocommit=True) as connection:
        after = transfer(connection, request, authorize=authorize)
    receipt = {'schema': 'honua.fixture-ownership-receipt/v1', 'status': 'completed',
        'candidateKey': request['candidateKey'], 'context': envelope['context'],
        'requestDigest': pointer['sha256'], 'ownershipRequestDigest': canonical_digest(request),
        'claimKey': pointer['claimKey'], 'taskArn': task_arn, 'taskDefinitionArn': task['taskDefinitionArn'],
        'imageUri': config['imageUri'], 'provisionerConfigSha256': canonical_digest(config),
        'database': request['database'], 'before': request['tables'], 'after': after}
    if request.get('profile') == IMPORT_PROFILE:
        receipt['profile'] = IMPORT_PROFILE
    s3.put_object(Bucket=config['bucket'], Key=config['receiptPrefix'] + pointer['claimKey'][7:] + '.json',
                  Body=encoded(receipt), IfNoneMatch='*', ServerSideEncryption='AES256', ContentType='application/json')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        print('private fixture provisioner refused; retain task, claim and original liability', file=sys.stderr)
        sys.exit(1)
