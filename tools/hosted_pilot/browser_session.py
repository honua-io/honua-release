"""Read-only actual human Console custody; never construct cookies from keys."""
from __future__ import annotations
from datetime import datetime, timezone
from http.cookiejar import Cookie, CookieJar
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_e2e_pack import canonical_digest
from controller_archive import require
from fixture_handoff import sdk
from native_bootstrap import NoRedirect
from worker_mcp_configuration import authorize, verify_injected_secret

STORAGE_ENV = 'HONUA_CONSOLE_BROWSER_STORAGE_STATE'
SESSION_PATH = '/api/agent-workspace/session'
TARGET_PATH = '/api/studio/v1/agent/target'


def operator_partition(subject_id, profile):
    """Exact Console OperatorPartition, with .NET UTF-16 String.Length."""
    require(isinstance(subject_id, str) and 0 < len(subject_id.encode('utf-8')) <= 2048 and
        isinstance(profile, dict) and set(profile) == {'id', 'serverUri'} and
        isinstance(profile['id'], str) and profile['id'].isprintable() and 0 < len(profile['id'].encode('utf-8')) <= 512 and
        re.fullmatch(r'https://(?:api|agent)\.pilot\.demo\.honua\.io/?', profile['serverUri']),
        'browser partition needs actual validated subject and exact reviewed canonical profile/server origin')
    # These reviewed HTTPS root origins already equal .NET SchemeAndServer|Path
    # UriEscaped output after TrimEnd('/'). No opaque ID/URI normalization or
    # parsing of actorId/studioOwnerId can supply the validated subject.
    length = lambda value: len(value.encode('utf-16-le'))//2
    payload = str(length(subject_id))+':'+subject_id+str(length(profile['id']))+':'+profile['id']+':'+profile['serverUri'].rstrip('/')
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def binding(value):
    require(isinstance(value, dict) and set(value) == {'credentialEnv', 'secretArn', 'versionId'} and
        value['credentialEnv'] == STORAGE_ENV and
        re.fullmatch(r'arn:aws:secretsmanager:us-east-1:585192672263:secret:honua-pilot-[a-z0-9-]+/human-session/browser-[A-Za-z0-9]{6}', value['secretArn']) and
        re.fullmatch(r'[A-Za-z0-9-]{32,64}', value['versionId']), 'browser needs actual immutable human storage custody')


def storage(value):
    require(isinstance(value, str) and len(value.encode()) <= 1024*1024, 'bounded actual human browser storage is missing')
    parsed = json.loads(value)
    require(isinstance(parsed, dict) and set(parsed) == {'cookies', 'origins'} and
        isinstance(parsed['cookies'], list) and 0 < len(parsed['cookies']) <= 100 and
        isinstance(parsed['origins'], list) and len(parsed['origins']) <= 20,
        'browser custody must be original Playwright storage state')
    return parsed


def deployment(value, *, ecs=None):
    require(isinstance(value, dict) and set(value) == {'region', 'clusterArn', 'serviceArn', 'taskDefinitionArn', 'imageUri', 'edgeImageUri', 'issuer'} and
        value['region'] == 'us-east-1' and
        re.fullmatch(r'arn:aws:ecs:us-east-1:585192672263:cluster/honua-pilot-[a-z0-9-]+', value['clusterArn']) and
        value['serviceArn'].startswith(value['clusterArn'].replace(':cluster/', ':service/')+'/') and
        re.fullmatch(r'arn:aws:ecs:us-east-1:585192672263:task-definition/honua-pilot-[a-z0-9-]+-console:[1-9][0-9]*', value['taskDefinitionArn']) and
        re.fullmatch(r'ghcr.io/honua-io/honua-console@sha256:[a-f0-9]{64}', value['imageUri']) and
        re.fullmatch(r'[A-Za-z0-9./_-]+@sha256:[a-f0-9]{64}', value['edgeImageUri']) and
        re.fullmatch(r'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_[A-Za-z0-9]+', value['issuer']),
        'browser requires source-reviewed exact production Console/OIDC deployment')
    ecs = ecs or sdk('ecs', value['region'])
    response = ecs.describe_services(cluster=value['clusterArn'], services=[value['serviceArn']])
    require(not response.get('failures') and len(response.get('services', [])) == 1, 'actual Console service is not independently observed')
    service = response['services'][0]
    require(service['serviceArn'] == value['serviceArn'] and service['clusterArn'] == value['clusterArn'] and
        service['taskDefinition'] == value['taskDefinitionArn'] and service['status'] == 'ACTIVE' and
        type(service['desiredCount']) is int and service['desiredCount'] > 0 and service['runningCount'] == service['desiredCount'] and
        service['pendingCount'] == 0 and len(service['deployments']) == 1 and
        service['deployments'][0]['taskDefinition'] == value['taskDefinitionArn'] and
        service['deployments'][0]['status'] == 'PRIMARY' and service['deployments'][0].get('rolloutState') == 'COMPLETED',
        'Console deployment is not the exact stable source-reviewed revision')
    task = ecs.describe_task_definition(taskDefinition=value['taskDefinitionArn'])['taskDefinition']
    require(task['taskDefinitionArn'] == value['taskDefinitionArn'], 'Console task revision differs')
    containers = {row['name']: row for row in task['containerDefinitions']}
    require(len(task['containerDefinitions']) == 2 and set(containers) == {'console', 'edge'} and
        containers['console']['image'] == value['imageUri'] and containers['edge']['image'] == value['edgeImageUri'],
        'Console or edge image differs from reviewed immutable deployment')
    env = {row['name']: row['value'] for row in containers['console'].get('environment', [])}
    require(env.get('ASPNETCORE_ENVIRONMENT') == 'Production' and env.get('Honua__Console__Auth__Mode') == 'EdgeForwarded' and
        env.get('Honua__Console__Auth__EdgeForwarded__ServerSessionIssuer') == value['issuer'],
        'browser cannot use Development, API-key or caller-declared human authentication')
    edge = {row['name']: row['value'] for row in containers['edge'].get('environment', [])}
    configuration = json.loads(edge.get('HONUA_EDGE_CONFIG', '{}'))
    providers = configuration.get('providers', [])
    require(len(providers) == 1 and providers[0].get('provider') == 'oidc', 'Console edge lacks exact actual OIDC provider')
    oidc = providers[0].get('oidcConfig', {})
    require(oidc.get('issuerURL') == value['issuer'] and oidc.get('userIDClaim') == 'sub' and
        all(oidc.get(key) is False for key in ('insecureSkipNonce', 'insecureSkipIssuerVerification', 'insecureAllowUnverifiedEmail')) and
        edge.get('OAUTH2_PROXY_COOKIE_SECURE') == 'true' and edge.get('OAUTH2_PROXY_COOKIE_HTTPONLY') == 'true',
        'Console edge OIDC identity checks are not the reviewed production binding')
    headers = {row['name']: row for row in configuration.get('injectRequestHeaders', [])}
    for name in ('X-API-Key', 'Authorization', 'X-Forwarded-Access-Token'):
        require(headers.get(name) == {'name': name, 'preserveRequestValue': False, 'values': []}, 'Console edge permits caller-supplied machine credentials')
    require(headers.get('X-Forwarded-User') == {'name': 'X-Forwarded-User', 'preserveRequestValue': False, 'values': [{'claimSource': {'claim': 'user'}}]} and
        headers.get('X-Honua-Edge-Auth') == {'name': 'X-Honua-Edge-Auth', 'preserveRequestValue': False,
            'values': [{'secretSource': {'fromEnv': 'HONUA_EDGE_SHARED_SECRET'}}]}, 'Console edge trusts unbound forwarded human identity')
    shared_console = [row['valueFrom'] for row in containers['console'].get('secrets', []) if row['name'] == 'Honua__Console__Auth__EdgeForwarded__SharedSecret']
    shared_edge = [row['valueFrom'] for row in containers['edge'].get('secrets', []) if row['name'] == 'HONUA_EDGE_SHARED_SECRET']
    require(len(shared_console) == 1 and shared_console == shared_edge and shared_console[0].endswith(':shared::') and
        env.get('ASPNETCORE_URLS') == 'http://127.0.0.1:8081' and not containers['console'].get('portMappings'),
        'Console forwarded identity must come from its authenticated local edge only')
    # Project only identity/configuration references. Never retain raw task env,
    # secret material, a session/CSRF body, or a hash of credential-bearing data.
    return {**value, 'authenticationMode': 'EdgeForwarded', 'runningCount': service['runningCount'],
        'provider': {'issuer': oidc['issuerURL'], 'subjectClaim': 'sub', 'clientId': providers[0]['clientID']}}


def reader(endpoint, state):
    require(endpoint == 'https://pilot.demo.honua.io', 'human browser capture must use the isolated reviewed Console origin')
    jar = CookieJar()
    for row in state['cookies']:
        require(isinstance(row, dict) and all(isinstance(row.get(key), str) for key in ('name', 'value', 'domain', 'path')) and
            row.get('secure') is True and row.get('httpOnly') in (True, False) and '\n' not in row['value'] and '\r' not in row['value'],
            'actual human browser cookie shape or transport differs')
        expires = row.get('expires', -1)
        require(type(expires) in (int, float), 'browser cookie expiry is invalid')
        jar.set_cookie(Cookie(0, row['name'], row['value'], None, False, row['domain'], row['domain'].startswith('.'),
            row['domain'].startswith('.'), row['path'], True, True, None if expires == -1 else int(expires),
            expires == -1, None, None, {'HttpOnly': row['httpOnly']}, False))
    opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPCookieProcessor(jar))
    def read(path, session=None):
        require(path in (SESSION_PATH, TARGET_PATH), 'browser custody reader allows only actual session and human principal GETs')
        headers = {'Accept': 'application/json'}
        if session:
            headers.update({'X-Honua-Environment': session['environmentId'], 'X-Honua-Session-Partition': session['principalId']})
        try:
            with opener.open(urllib.request.Request(endpoint+path, headers=headers, method='GET'), timeout=30) as response:
                raw = response.read(1024*1024+1)
                require(response.status == 200 and len(raw) <= 1024*1024 and
                    'no-store' in {part.strip().lower() for part in response.headers.get('Cache-Control', '').split(',')},
                    'actual human browser session read failed or permits cached identity')
                return json.loads(raw)
        except Exception:
            raise ValueError('actual human browser session read failed') from None
    return read


def observe(configuration, state, *, ecs=None, read=None):
    actual_deployment = deployment(configuration['consoleDeployment'], ecs=ecs)
    read = read or reader(configuration['endpoint'], state)
    response = read(SESSION_PATH)
    require(isinstance(response, dict) and response.get('authenticated') is True and
        all(isinstance(response.get(key), str) and response[key] for key in ('principalId', 'environmentId', 'expiresAt')) and
        datetime.fromisoformat(response['expiresAt'].replace('Z', '+00:00')) > datetime.now(timezone.utc),
        'actual Console session is absent, expired or not authenticated')
    session = {key: response[key] for key in ('principalId', 'environmentId')}
    answer = read(TARGET_PATH, session)
    target = answer.get('data', answer)
    principal = target.get('principal', {})
    require(target.get('targetId') == configuration['targetId'] and principal.get('isHumanSession') is True and
        principal.get('authenticationKind') == 'operator-bearer' and
        isinstance(principal.get('actorId'), str) and principal['actorId'] and
        not principal['actorId'].startswith('apikey:') and
        isinstance(principal.get('subjectId'), str) and principal['subjectId'] and
        set(principal) <= {'actorId', 'studioOwnerId', 'subjectId', 'tenantId', 'authenticationKind', 'isHumanSession'},
        'same Console browser is not a native server-observed actual human on the selected target')
    profile = configuration['consoleProfile']
    require(session['environmentId'] == profile['id'] and
        session['principalId'] == operator_partition(principal['subjectId'], profile),
        'actual native subject does not match the same Console partition and reviewed profile/origin')
    return {'session': session | {'authenticated': True}, 'expiresAt': response['expiresAt'],
        'nativeTarget': {'targetId': target['targetId'], 'principal': principal}, 'consoleProfile': profile, 'deployment': actual_deployment}


def capture(*, durable, configuration, ecs=None, read=None):
    require(isinstance(configuration, dict) and set(configuration) == {'schemaVersion', 'candidateKey', 'operation', 'endpoint', 'storageState', 'consoleDeployment', 'consoleProfile', 'targetId'} and
        configuration['schemaVersion'] == 1 and configuration['candidateKey'] == durable.store.candidate,
        'browser capture requires reviewed original candidate/bootstrap custody')
    authorize(durable, configuration['operation'])
    binding(configuration['storageState'])
    verify_injected_secret(configuration['storageState'], STORAGE_ENV, 'us-east-1', ecs=ecs)
    state = storage(os.environ.get(STORAGE_ENV))
    # Both HTTP results are observed using original cookies. Neither a human
    # boolean nor cookie material is accepted from a caller-supplied record.
    observed = observe(configuration, state, ecs=ecs, read=read)
    authorize(durable, configuration['operation'])
    key = 'console-human-capture:'+canonical_digest(configuration)[7:]
    records = durable.state.setdefault('scenarioOperations', {})
    require(key not in records, 'original Console session custody already retained; preserve its immutable reference')
    value = {'schema': 'honua.private-console-human-session/v1', 'producer': 'private-console-human-capture',
        'candidateKey': durable.store.candidate, 'configuration': configuration, 'storageState': configuration['storageState'],
        'producerTaskArn': os.environ['PILOT_CONTROLLER_TASK_ARN'], 'observed': observed, 'qualification': False}
    records[key] = value
    durable._persist()
    return {'key': key, 'sha256': canonical_digest(value), 'session': observed['session'], 'qualification': False}
