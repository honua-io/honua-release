#!/usr/bin/env python3
"""Private one-shot SQL identities and native API-key issuance, under retained costs.

Runs only in the reviewed dedicated task with injected credentials. Intent is
durable before every effect; a partial result never authorizes automatic replay.
Key material is sent directly to private Secrets Manager versions, never logs,
receipts, controller source, or a model. API keys cannot approve publication.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_e2e_pack import canonical_digest, PackError
from agent_e2e_budget import validate as validate_ledger
from agent_e2e_state import DurableExecution, open_store
from agent_e2e_fixture_database import FixtureDatabase
from agent_e2e_studio_database import StudioFixtureDatabase
from fixture_handoff import require, sdk, encoded
from fixture_provisioner import master_settings


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate(config):
    fields = {'schemaVersion', 'region', 'accountId', 'candidateKey', 'state', 'bootstrapArtifactDigest',
        'operation', 'databaseHost', 'database', 'appRole', 'cleanupRole', 'observerRole',
        'endpoint', 'targetId', 'itemId', 'expiresAt', 'readGrants', 'datasetImportScopes', 'managedProcessIds', 'secretArns', 'bucket', 'receiptKey',
        'clusterArn', 'taskDefinitionFamily', 'imageUri', 'generationDigest'}
    require(isinstance(config, dict) and fields <= set(config) <= fields | {'humanMemberships', 'studioRestoreItemIds'} and config['schemaVersion'] == 2, 'native bootstrap reviewed configuration missing')
    if config.get('humanMemberships') is not None:
        from human_memberships import validate as validate_memberships
        validate_memberships(config['humanMemberships'], config)
    require(re.fullmatch(r'sha256:[a-f0-9]{64}', config['candidateKey']) and
            re.fullmatch(r'sha256:[a-f0-9]{64}', config['bootstrapArtifactDigest']) and
            re.fullmatch(r'[0-9]{12}', config['accountId']) and config['state']['region'] == config['region'], 'native bootstrap accounting identity differs')
    require(re.fullmatch(r'https://[a-z0-9-]+\.pilot\.demo\.honua\.io', config['endpoint']) and
            re.fullmatch(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', config['itemId']), 'native bootstrap requires exact pilot origin and preallocated item')
    require(re.fullmatch(r'honua_agent_[a-z0-9_]{1,40}', config['database']) and
            all(re.fullmatch(r'[a-z][a-z0-9_]{0,62}', config[key]) for key in ('appRole', 'cleanupRole', 'observerRole')) and
            len({config[key] for key in ('appRole', 'cleanupRole', 'observerRole')}) == 3, 'native bootstrap SQL identities are ambiguous')
    require(set(config['secretArns']) == {'setup', 'observer', 'agent'} and len(set(config['secretArns'].values())) == 3 and
            all(re.fullmatch('arn:aws:secretsmanager:'+config['region']+':'+config['accountId']+r':secret:honua-pilot-[a-z0-9-]+-agent/native-agent/(setup|observer|agent)-[A-Za-z0-9]{6}', arn) for arn in config['secretArns'].values()), 'native bootstrap secrets are outside isolated native custody')
    require(isinstance(config['readGrants'], list) and len(config['readGrants']) == len(set(config['readGrants'])) and
            all(re.fullmatch(r'read:[a-z0-9_-]+/[0-9]+', grant) for grant in config['readGrants']), 'native bootstrap data access must name exact service/layer grants')
    require(isinstance(config['targetId'], str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', config['targetId']),
            'native bootstrap requires the observed exact native target identity')
    scopes = config['datasetImportScopes']
    require(isinstance(scopes, list) and all(isinstance(scope, str) for scope in scopes) and len(scopes) == len(set(scopes)) and
            all(re.fullmatch(re.escape(config['targetId'])+r'/[a-z_][a-z0-9_]{0,62}/[a-z][a-z0-9_]{0,62}', scope) for scope in scopes),
            'native import authority must name exact observed-target/schema/new-table scopes')
    processes = config['managedProcessIds']
    require(isinstance(processes, list) and all(isinstance(process, str) for process in processes) and
            len(processes) == len(set(processes)) and all(process == 'analytics.buffer-aggregate-managed' for process in processes),
            'native managed-process authority must name the supported exact process')
    restore_items = config.get('studioRestoreItemIds', [])
    require(isinstance(restore_items, list) and all(isinstance(item, str) for item in restore_items) and
            len(restore_items) == len(set(restore_items)) and
            all(re.fullmatch(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', item) and
                item != '00000000-0000-0000-0000-000000000000' for item in restore_items),
            'native Studio restoration authority must name separate exact nonempty item IDs')
    expires = datetime.fromisoformat(config['expiresAt'].replace('Z', '+00:00'))
    require(expires.tzinfo and 0 < (expires-datetime.now(timezone.utc)).total_seconds() <= 7*86400, 'native bootstrap key expiry must be within seven days')
    operation = config['operation']
    require(set(operation) == {'operationId', 'liabilityBasisDigest', 'reservationIds'} and
            set(operation['reservationIds']) == {'infrastructure', 'cleanup'} and
            len(set(operation['reservationIds'].values())) == 2, 'native bootstrap requires separate original effect/cleanup liabilities')


def authorize(config, durable):
    durable._require_live_lease()
    ledger = durable.state['ledger']
    validate_ledger(ledger)
    require(ledger['candidateKey'] == config['candidateKey'] and
            durable.store.generation['digest'] == config['generationDigest'] and
            durable.state.get('bootstrapImport', {}).get('artifactDigest') == config['bootstrapArtifactDigest'], 'native bootstrap cannot initialize or substitute history')
    operation = config['operation']
    for kind, reservation in operation['reservationIds'].items():
        matches = [row for row in ledger.get('operations', []) if row['id'] == reservation]
        require(len(matches) == 1 and matches[0]['operationId'] == operation['operationId'] and
                matches[0]['purpose'] == 'bootstrap' and matches[0]['kind'] == kind and
                matches[0]['basisDigest'] == operation['liabilityBasisDigest'] and
                matches[0]['state'] == 'reserved' and matches[0]['actualUsd'] is None,
                'native bootstrap original liability is absent, settled or bound to another effect')


def sql_identities(connection, config, credentials, *, guard):
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict
    guard()
    with connection.transaction():
        connection.execute("SET LOCAL statement_timeout = '10s'")
        connection.execute("SET LOCAL lock_timeout = '1s'")
        identity = connection.execute('''SELECT current_database() AS database, current_user AS principal,
            (SELECT oid::bigint FROM pg_database WHERE datname=current_database()) AS "databaseOid",
            inet_server_addr()::text AS "serverAddress", inet_server_port() AS "serverPort",
            (SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()) AS ssl''').fetchone()
        require(identity['database'] == config['database'] and identity['principal'] == config['appRole'] and identity['ssl'] is True,
                'native bootstrap SQL connection is not the reviewed TLS application database')
        for kind in ('cleanup', 'observer'):
            role = config[kind+'Role']
            settings = conninfo_to_dict(credentials[kind])
            require(settings.get('user') == role and settings.get('dbname') == config['database'] and
                    settings.get('host') == config['databaseHost'] and settings.get('sslmode') == 'verify-full' and
                    isinstance(settings.get('password'), str) and re.fullmatch(r'[A-Za-z0-9]{48}', settings['password']), 'native bootstrap SQL credential binding differs')
            require(connection.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (role,)).fetchone() is None,
                    'native bootstrap refuses existing SQL roles; reconcile without resetting credentials')
            connection.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS').format(
                sql.Identifier(role), sql.Literal(settings['password'])))
        product = (*FixtureDatabase.CATALOG, 'metadata_v2_current', 'metadata_v2_snapshots', *FixtureDatabase.SIDECARS)
        tables = tuple(dict.fromkeys((*product, *StudioFixtureDatabase.TABLES, *StudioFixtureDatabase.PROTECTED)))
        observed = connection.execute('''SELECT c.relname AS name, c.oid::bigint AS oid FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='honua' AND c.relname=ANY(%s) AND c.relkind='r' ORDER BY c.relname''', (list(tables),)).fetchall()
        require({row['name'] for row in observed} == set(tables), 'native bootstrap refuses absent or unsupported native schema')
        for role in (config['cleanupRole'], config['observerRole']):
            connection.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(config['database']), sql.Identifier(role)))
            connection.execute(sql.SQL('GRANT USAGE ON SCHEMA honua, honua_data TO {}').format(sql.Identifier(role)))
            connection.execute(sql.SQL('GRANT SELECT ON ALL TABLES IN SCHEMA honua_data TO {}').format(sql.Identifier(role)))
            for table in tables:
                connection.execute(sql.SQL('GRANT SELECT ON TABLE {} TO {}').format(sql.Identifier('honua', table), sql.Identifier(role)))
        for table in tables:
            # Trusted cleanup locks protected registry parents, but its reviewed
            # SQL never mutates their rows. PostgreSQL requires UPDATE authority
            # for that lock; the independent observer remains SELECT-only.
            privilege = 'UPDATE' if table in StudioFixtureDatabase.PROTECTED else 'DELETE, UPDATE'
            connection.execute(sql.SQL('GRANT '+privilege+' ON TABLE {} TO {}').format(sql.Identifier('honua', table), sql.Identifier(config['cleanupRole'])))
        connection.execute(sql.SQL('GRANT CREATE ON SCHEMA honua_data TO {}').format(sql.Identifier(config['cleanupRole'])))
        connection.execute(sql.SQL('ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA honua_data GRANT SELECT ON TABLES TO {}').format(
            sql.Identifier(config['appRole']), sql.Identifier(config['observerRole'])))
        guard()
    return {'database': identity, 'nativeTables': observed, 'cleanupRole': config['cleanupRole'], 'observerRole': config['observerRole'], 'qualification': False}


def http(config, root_key, method, path, body=None):
    target_read = method == 'GET' and path == '/api/v1/agent/target'
    key_issue = method == 'POST' and path == '/api/v1/admin/api-keys'
    key_read = method == 'GET' and re.fullmatch(r'/api/v1/admin/api-keys/[a-f0-9-]{36}/effective-permissions', path)
    require(target_read or key_issue or key_read, 'native bootstrap HTTP operation is outside reviewed target/key paths')
    require((key_issue and isinstance(body, dict)) or (not key_issue and body is None), 'native bootstrap HTTP body differs')
    request = urllib.request.Request(config['endpoint']+path, method=method,
        headers={'X-API-Key': root_key, 'Accept': 'application/json', 'Content-Type': 'application/json'},
        data=None if body is None else encoded(body))
    # One request only; a lost mutation response is retained and never retried.
    with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
        raw = response.read(1024*1024+1)
        require(response.status == (201 if method == 'POST' else 200) and len(raw) <= 1024*1024, 'native key issuance response differs')
    value = json.loads(raw)
    require(isinstance(value, dict), 'native bootstrap response must be an object')
    if target_read:
        require(value.get('targetId') == config['targetId'], 'native bootstrap endpoint target differs from reviewed identity')
        return {'targetId': value['targetId']}
    require(value.get('success') is True and isinstance(value.get('data'), dict), 'native key issuance did not return a successful native envelope')
    return value['data']


def provision(config, durable, *, connection, secrets, root_key, native):
    validate(config)
    guard = lambda: authorize(config, durable)
    guard()
    operation_key = canonical_digest({'kind': 'native-bootstrap', 'candidateKey': config['candidateKey'], 'operationId': config['operation']['operationId']})
    retained = durable.state.setdefault('scenarioOperations', {})
    require(operation_key+'/start' not in retained, 'native bootstrap already started; reconcile original SQL/API/secret observations without replay')
    # Existing SQL/key intent refuses before any newly requested human effects.
    # When absent, the separate membership helper can GET-reconcile its original
    # intents before the parent's first irreversible credential issuance.
    memberships = None
    if config.get('humanMemberships') is not None:
        from human_memberships import provision_human_memberships
        memberships = provision_human_memberships(configuration=config['humanMemberships'],
            bootstrap=config, durable=durable, native=native, guard=guard)
    def retain(stage, value):
        guard()
        key = operation_key+'/'+stage
        require(key not in retained, 'native bootstrap stage already retained')
        retained[key] = {'producer': 'private-native-bootstrap', 'candidateKey': config['candidateKey'], 'value': value}
        durable._persist()
    retain('start', {'configurationDigest': canonical_digest(config), 'startedAt': datetime.now(timezone.utc).isoformat()})
    guard()
    observed_target = http(config, root_key, 'GET', '/api/v1/agent/target')
    require(observed_target == {'targetId': config['targetId']}, 'native bootstrap target readback differs')
    require(root_key.encode() not in encoded(observed_target), 'native target response disclosed credential material')
    retain('target', observed_target)
    sql_result = sql_identities(connection, config, {kind: os.environ['HONUA_AGENT_FIXTURE_'+kind.upper()+'_DSN'] for kind in ('cleanup', 'observer')}, guard=guard)
    retain('sql', sql_result)
    permission_sets = {'setup': ['admin:write'], 'observer': ['admin:read'], 'agent': [
        'admin:read', 'agent:workspace', 'agent:propose-publication', 'agent:studio-compose:'+config['itemId'],
        'agent:studio-propose-publication:'+config['itemId'], *config['readGrants'],
        *('agent:studio-restore:'+item for item in config.get('studioRestoreItemIds', [])),
        *('agent:dataset-import:'+scope for scope in config['datasetImportScopes']),
        *('agent:managed-process:'+process for process in config['managedProcessIds'])]}
    issued = {}
    for kind, permissions in permission_sets.items():
        # Secret version identity binds the retained operation and destination.
        # No issued credential or credential-response field enters this digest.
        version = canonical_digest({'operation': operation_key, 'kind': kind, 'secretArn': config['secretArns'][kind]})[7:]
        retain('key-'+kind+'-intent', {'permissions': permissions, 'expiresAt': config['expiresAt'], 'secretVersionId': version})
        guard()
        result = http(config, root_key, 'POST', '/api/v1/admin/api-keys', {'name': 'pilot-'+operation_key[7:23]+'-'+kind,
                      'permissions': permissions, 'expiresAt': config['expiresAt']})
        record, key = result['apiKey'], result['key']
        require(re.fullmatch(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', record['id']) and
                sorted(record['permissions']) == sorted(permissions) and isinstance(key, str) and len(key) >= 32 and
                datetime.fromisoformat(record['expiresAt'].replace('Z', '+00:00')) == datetime.fromisoformat(config['expiresAt'].replace('Z', '+00:00')),
                'native API-key identity/grants differ from reviewed issuance')
        public_record = {'apiKeyId': record['id'], 'permissions': permissions, 'expiresAt': record['expiresAt']}
        require(root_key.encode() not in encoded(public_record) and key.encode() not in encoded(public_record),
                'native credential response disclosed secret material in public identity fields')
        guard()
        response = secrets.put_secret_value(SecretId=config['secretArns'][kind], SecretString=key, ClientRequestToken=version)
        require(response['ARN'] == config['secretArns'][kind] and response['VersionId'] == version, 'native API-key secret custody differs')
        guard()
        effective = http(config, root_key, 'GET', '/api/v1/admin/api-keys/'+record['id']+'/effective-permissions')
        require(effective['canAuthenticate'] is True and sorted(effective['permissions']) == sorted(permissions), 'native effective grants did not confirm active exact key')
        require(root_key.encode() not in encoded(effective) and key.encode() not in encoded(effective), 'native effective permission response disclosed credential material')
        issued[kind] = {'apiKeyId': record['id'], 'secretArn': response['ARN'], 'versionId': version, 'permissions': permissions, 'effectivePermissions': effective}
        retain('key-'+kind, issued[kind])
    receipt = {'schema': 'honua.private-native-bootstrap/v1', 'candidateKey': config['candidateKey'],
        'configurationDigest': canonical_digest(config), 'operation': config['operation'], 'target': observed_target, 'sql': sql_result,
        'keys': issued, 'requiresReviewedAgentPrincipalInstallation': True, 'qualification': False,
        'completedAt': datetime.now(timezone.utc).isoformat(), 'native': native}
    if memberships is not None:
        receipt['humanMemberships'] = {'schema': memberships['schema'],
            'enrollment': config['humanMemberships']['enrollment'],
            'receiptDigest': canonical_digest(memberships), 'qualification': False}
    retain('completed', receipt)
    return receipt


def main():
    require(os.getuid() == 10001 and sys.argv[1:] == [], 'native bootstrap requires its fixed reviewed nonroot task entry')
    config = json.loads(os.environ['PILOT_NATIVE_BOOTSTRAP_CONFIG'])
    validate(config)
    require(sdk('sts', config['region']).get_caller_identity()['Account'] == config['accountId'], 'native bootstrap AWS account differs')
    from controller_entry import http_json
    metadata_uri = os.environ['ECS_CONTAINER_METADATA_URI_V4']
    require(re.fullmatch(r'http://169\.254\.170\.2/v4/[A-Za-z0-9_-]+', metadata_uri), 'native bootstrap lacks authentic ECS metadata')
    metadata = http_json(metadata_uri+'/task', limit=65536)
    observed = sdk('ecs', config['region']).describe_tasks(cluster=config['clusterArn'], tasks=[metadata['TaskARN']])
    require(not observed.get('failures') and len(observed.get('tasks', [])) == 1, 'native bootstrap task cannot be observed')
    task = observed['tasks'][0]
    require(task['taskDefinitionArn'].rsplit(':', 1)[0] == config['taskDefinitionFamily'] and
            len(task['containers']) == 1 and task['containers'][0]['image'] == config['imageUri'] and
            not task.get('enableExecuteCommand', True), 'native bootstrap image/task differs')
    native = {'taskArn': task['taskArn'], 'taskDefinitionArn': task['taskDefinitionArn'], 'imageUri': config['imageUri'],
        'imageSource': json.loads(Path('/opt/honua-controller-source.json').read_text())}
    durable = DurableExecution(open_store(config['state'], config['candidateKey']), '/work/native-bootstrap-ledger.json')
    try:
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(**master_settings(os.environ['PILOT_FIXTURE_MASTER_CONNECTION'], config),
                connect_timeout=5, autocommit=True, row_factory=dict_row) as connection:
            receipt = provision(config, durable, connection=connection, secrets=sdk('secretsmanager', config['region']), root_key=os.environ['PILOT_FIXTURE_ADMIN_PASSWORD'], native=native)
        sdk('s3', config['region']).put_object(Bucket=config['bucket'], Key=config['receiptKey'], Body=encoded(receipt),
            IfNoneMatch='*', ServerSideEncryption='AES256', ContentType='application/json')
    finally:
        durable.close()
    print('Native SQL identities and API keys retained privately. Reviewed agent-principal installation and human qualification remain required.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('private native bootstrap refused; retain original intent, credentials and liabilities for reconciliation', file=sys.stderr)
        sys.exit(1)
