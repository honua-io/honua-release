"""Private SCIM membership setup under original bootstrap custody and liabilities.

No Cognito writes, SQL, credential issuance, model calls, or login qualification.
"""
from __future__ import annotations

import base64
import copy
import json
import os
from pathlib import Path
import re
import sys
import urllib.parse
import urllib.request
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_e2e_pack import canonical_digest, digest_bytes
from fixture_handoff import require, sdk
from native_bootstrap import NoRedirect

REPOSITORY = 'honua-io/honua-release'
WORKFLOW = '.github/workflows/hosted-pilot-deploy.yml'
SCHEMA = 'honua.hosted-pilot-human-identities/v1'
SCIM_ENV = 'HONUA_NATIVE_SCIM_TOKEN'
ROOT_ENV = 'PILOT_FIXTURE_ADMIN_PASSWORD'
LIMIT = 1024*1024
SHA = r'sha256:[a-f0-9]{64}'


def validate(configuration, bootstrap):
    require(isinstance(configuration, dict) and set(configuration) == {'schemaVersion', 'enrollment', 'credentials'} and
        configuration['schemaVersion'] == 1 and set(configuration['credentials']) == {'scim', 'root'},
        'human membership configuration requires original enrollment and separate immutable credentials')
    for value in configuration['credentials'].values():
        require(isinstance(value, dict) and set(value) == {'secretArn', 'versionId'} and
            re.fullmatch('arn:aws:secretsmanager:'+re.escape(bootstrap['region'])+':'+bootstrap['accountId']+r':secret:honua-pilot-[A-Za-z0-9/_-]+', value['secretArn']) and
            re.fullmatch(r'[A-Za-z0-9-]{32,64}', value['versionId']), 'human membership credential reference is not immutable private custody')
    require(configuration['credentials']['scim']['secretArn'] != configuration['credentials']['root']['secretArn'],
        'SCIM provisioning and privileged native credential must remain separate')
    require(bootstrap['schemaVersion'] == 2 and isinstance(bootstrap['targetId'], str) and
        re.fullmatch(r'[A-Za-z0-9_-]{1,128}', bootstrap['targetId']) and
        isinstance(bootstrap['datasetImportScopes'], list) and all(isinstance(scope, str) for scope in bootstrap['datasetImportScopes']) and
        len(set(bootstrap['datasetImportScopes'])) == len(bootstrap['datasetImportScopes']) and
        all(re.fullmatch(re.escape(bootstrap['targetId'])+r'/[a-z_][a-z0-9_]{0,62}/[a-z][a-z0-9_]{0,62}', scope)
            for scope in bootstrap['datasetImportScopes']), 'human author grants must use exact parent bootstrap target/import scopes')


def object_json(s3, reference, *, guard):
    require(isinstance(reference, dict) and set(reference) == {'bucket', 'key', 'versionId', 'sha256', 'kmsArn'} and
        isinstance(reference['versionId'], str) and reference['versionId'] not in ('', 'null') and
        re.fullmatch(SHA, reference['sha256']) and reference['key'].startswith('hosted-pilot/plans/') and
        '..' not in reference['key'].split('/'), 'original enrollment/review object reference is missing')
    guard()
    result = s3.get_object(Bucket=reference['bucket'], Key=reference['key'], VersionId=reference['versionId'])
    guard()
    require(result.get('VersionId') == reference['versionId'] and result.get('ServerSideEncryption') == 'aws:kms' and
        result.get('SSEKMSKeyId') == reference['kmsArn'] and result.get('BucketKeyEnabled', False) is not True,
        'original private object version, encryption or bucket-key context differs')
    raw = result['Body'].read(LIMIT+1)
    guard()
    require(len(raw) <= LIMIT and digest_bytes(raw) == reference['sha256'],
        'original private object version, encryption or bytes differ')
    return json.loads(raw)


def github_json(path):
    require(path.startswith('repos/'+REPOSITORY+'/') and '..' not in path.split('/'), 'public source proof escaped the original repository')
    request = urllib.request.Request('https://api.github.com/'+path, headers={
        'Accept': 'application/vnd.github+json', 'User-Agent': 'honua-private-membership-setup'})
    with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
        raw = response.read(LIMIT+1)
        require(response.status == 200 and len(raw) <= LIMIT, 'public source/job proof is unavailable')
    return json.loads(raw)


def enrollment_original(configuration, bootstrap, *, s3, github=github_json, guard):
    reference = configuration['enrollment']
    value = object_json(s3, reference, guard=guard)
    require(set(value) == {'schema', 'accountId', 'candidateKey', 'producer', 'sourceConfiguration', 'plan',
        'identity', 'deployment', 'users', 'qualification'} and value['schema'] == SCHEMA and value['qualification'] is False and
        value['candidateKey'] == bootstrap['candidateKey'] and value['accountId'] == bootstrap['accountId'],
        'human enrollment is not the original candidate identity projection')
    producer, plan = value['producer'], value['plan']
    require(set(producer) == {'repository', 'workflow', 'sourceSha', 'runId', 'runAttempt', 'jobKey', 'ref', 'event'} and
        producer['repository'] == REPOSITORY and producer['workflow'] == WORKFLOW and producer['jobKey'] == 'apply' and
        producer['ref'] == 'refs/heads/trunk' and producer['event'] == 'workflow_dispatch' and
        re.fullmatch(r'[a-f0-9]{40}', producer['sourceSha']) and
        all(type(producer[name]) is int and producer[name] > 0 for name in ('runId', 'runAttempt')) and
        set(plan) == {'sha256', 'versionId', 'attempt', 'reviewSha256', 'reviewArtifactId', 'review'} and
        type(plan['attempt']) is int and 0 < plan['attempt'] <= producer['runAttempt'] and
        type(plan['reviewArtifactId']) is int and plan['reviewArtifactId'] > 0 and
        re.fullmatch(SHA, plan['sha256']) and re.fullmatch(SHA, plan['reviewSha256']), 'human enrollment producing workflow/plan identity differs')
    prefix = 'hosted-pilot/plans/'+str(producer['runId'])+'/'+str(plan['attempt'])+'/'
    require(reference['key'] == prefix+'human-identities-'+str(producer['runAttempt'])+'.json' and
        plan['review']['bucket'] == reference['bucket'] and plan['review']['kmsArn'] == reference['kmsArn'] and
        plan['review']['key'] == prefix+'review.json' and
        plan['review']['sha256'] == plan['reviewSha256'], 'human enrollment/review custody escaped its original plan')
    review = object_json(s3, plan['review'], guard=guard)
    source = value['sourceConfiguration']
    root = review.get('root_path')
    require(root in ('certification/hosted-pilot', 'certification/hosted-pilot/campaign'),
        'enrollment review must bind one exact supported Terraform root')
    require(set(source) == {'path', 'sha256'} and
        re.fullmatch(re.escape(root)+r'/deployments/[A-Za-z0-9_.-]+\.tfvars\.json', source['path']) and
        re.fullmatch(SHA, source['sha256']) and review.get('schema') == 'honua.hosted-pilot-saved-plan/v1' and
        review.get('context') == {'GITHUB_SHA': producer['sourceSha'], 'GITHUB_RUN_ID': str(producer['runId']),
            'GITHUB_RUN_ATTEMPT': str(plan['attempt']), 'GITHUB_REPOSITORY': REPOSITORY} and
        review.get('operation') == 'apply' and
        review.get('config_path') == source['path'] and 'sha256:'+review.get('config_sha256', '') == source['sha256'] and
        'sha256:'+review.get('plan_sha256', '') == plan['sha256'] and review.get('s3_version_id') == plan['versionId'],
        'enrollment sidecar is not its original value-free approved configuration/plan')
    proofs = {}
    for attempt, job_name in {(producer['runAttempt'], 'apply'), (plan['attempt'], 'plan')}:
        path = 'repos/'+REPOSITORY+'/actions/runs/'+str(producer['runId'])+'/attempts/'+str(attempt)
        guard(); run = github(path); jobs = github(path+'/jobs?per_page=100'); guard()
        require(run.get('id') == producer['runId'] and run.get('run_attempt') == attempt and
            run.get('head_sha') == producer['sourceSha'] and run.get('head_branch') == 'trunk' and
            run.get('path', '').split('@')[0] == WORKFLOW and run.get('event') == 'workflow_dispatch' and
            run.get('repository', {}).get('full_name') == REPOSITORY and run.get('status') == 'completed' and
            (job_name != 'apply' or run.get('conclusion') == 'success') and
            type(jobs.get('total_count')) is int and jobs['total_count'] <= 100,
            'original successful deployment workflow metadata is absent or differs')
        selected = [job for job in jobs.get('jobs', []) if job.get('name') == job_name]
        require(len(selected) == 1 and selected[0].get('run_id') == producer['runId'] and
            selected[0].get('head_sha') == producer['sourceSha'] and selected[0].get('status') == 'completed' and
            selected[0].get('conclusion') == 'success', 'original plan/apply job did not succeed')
        proofs[str(attempt)+'/'+job_name] = {'run': run, 'jobs': jobs}
    guard()
    original = github('repos/'+REPOSITORY+'/contents/'+source['path']+'?ref='+producer['sourceSha'])
    guard()
    require(original.get('encoding') == 'base64', 'original reviewed enrollment configuration is unavailable')
    raw = base64.b64decode(''.join(original['content'].split()), validate=True)
    require(len(raw) <= LIMIT and digest_bytes(raw) == source['sha256'], 'original reviewed enrollment configuration bytes differ')
    config = json.loads(raw)
    if root == 'certification/hosted-pilot':
        application, foundation, domain = config, config.get('name_prefix'), config.get('server_domain')
    else:
        application = config.get('application')
        require(isinstance(application, dict) and isinstance(config.get('name_prefix'), str) and
            config['name_prefix'].endswith('-agent') and isinstance(application.get('candidate'), dict) and
            config.get('candidate_digest') == bootstrap['candidateKey'] == 'sha256:'+application['candidate'].get('manifest_sha256', '') and
            config.get('server_image') == application['candidate'].get('server_image'),
            'campaign enrollment application/candidate/image differs from the exact parent target')
        foundation, domain = config['name_prefix'].removesuffix('-agent'), config.get('target_domain')
    require(isinstance(application.get('human_enrollment'), dict) and isinstance(application.get('candidate'), dict) and
        isinstance(foundation, str) and re.fullmatch(r'honua-pilot-[a-z0-9-]{4,16}', foundation) and
        isinstance(domain, str), 'original reviewed application/enrollment inputs are missing')
    identity, users = value['identity'], value['users']
    require(set(identity) == {'region', 'poolId', 'issuer', 'authorRole', 'approverRole'} and
        identity['region'] == bootstrap['region'] and re.fullmatch(re.escape(identity['region'])+r'_[A-Za-z0-9]+', identity['poolId']) and
        identity['issuer'] == 'https://cognito-idp.'+identity['region']+'.amazonaws.com/'+identity['poolId'] and
        identity['authorRole'] == foundation+'-authors' and identity['approverRole'] == foundation+'-approvers' and
        application['human_enrollment']['pool_id'] == identity['poolId'] and isinstance(users, dict) and 2 <= len(users) <= 10 and
        set(users) == set(application['human_enrollment']['users']) and {user['role'] for user in users.values()} == {'authors', 'approvers'},
        'actual enrollment pool/issuer/nominated aliases or distinct roles differ')
    subjects = set()
    for alias, user in users.items():
        require(re.fullmatch(r'[a-z][a-z0-9_]{2,31}', alias) and set(user) == {'subjectId', 'role', 'status'} and
            user['role'] == application['human_enrollment']['users'][alias]['role'] and
            user['status'] in ('FORCE_CHANGE_PASSWORD', 'CONFIRMED') and
            str(UUID(user['subjectId'])) == user['subjectId'] and user['subjectId'] not in subjects,
            'actual nominated human subject/role/status is missing, ambiguous or substituted')
        subjects.add(user['subjectId'])
    deployment = value['deployment']
    require(set(deployment) == {'clusterArn', 'serverServiceArn', 'serverTaskDefinitionArn', 'consoleServiceArn',
        'consoleTaskDefinitionArn', 'serverOrigin', 'serverImage', 'consoleImage'} and
        deployment['serverOrigin'] == bootstrap['endpoint'] == 'https://'+domain and
        deployment['serverImage'] == application['candidate']['server_image'] and
        deployment['consoleImage'] == application['candidate']['console_image'],
        'enrollment Server/Console origin or images differ from original reviewed source')
    return value, {'enrollment': reference, 'review': plan['review'], 'publicOriginals': proofs,
        'sourceConfiguration': source}


def immutable_secret(value):
    return value['secretArn']+':::'+value['versionId']


def deployed_server(ecs, enrollment, configuration, bootstrap, native, *, guard):
    deployment = enrollment['deployment']
    regional = re.escape(bootstrap['region']+':'+bootstrap['accountId'])
    require(re.fullmatch('arn:aws:ecs:'+regional+r':cluster/honua-pilot-[a-z0-9-]+', deployment['clusterArn']) and
        deployment['serverServiceArn'].startswith(deployment['clusterArn'].replace(':cluster/', ':service/')+'/') and
        re.fullmatch('arn:aws:ecs:'+regional+r':task-definition/[A-Za-z0-9_-]+:[1-9][0-9]*', deployment['serverTaskDefinitionArn']),
        'original Server resource identity is outside reviewed pilot custody')
    guard()
    answer = ecs.describe_services(cluster=deployment['clusterArn'], services=[deployment['serverServiceArn']])
    guard()
    require(not answer.get('failures') and len(answer.get('services', [])) == 1, 'original Server service is unavailable')
    service = answer['services'][0]
    require(service.get('serviceArn') == deployment['serverServiceArn'] and service.get('clusterArn') == deployment['clusterArn'] and
        service.get('taskDefinition') == deployment['serverTaskDefinitionArn'] and service.get('status') == 'ACTIVE' and
        type(service.get('desiredCount')) is int and service['desiredCount'] > 0 and
        service.get('runningCount') == service['desiredCount'] and service.get('pendingCount') == 0 and
        len(service.get('deployments', [])) == 1 and service['deployments'][0].get('status') == 'PRIMARY' and
        service['deployments'][0].get('rolloutState') == 'COMPLETED' and
        service['deployments'][0].get('taskDefinition') == deployment['serverTaskDefinitionArn'],
        'SCIM Server is not the original stable deployed revision')
    guard()
    definition = ecs.describe_task_definition(taskDefinition=deployment['serverTaskDefinitionArn'])['taskDefinition']
    guard()
    containers = [row for row in definition.get('containerDefinitions', []) if row.get('image') == deployment['serverImage']]
    require(definition.get('taskDefinitionArn') == deployment['serverTaskDefinitionArn'] and len(containers) == 1,
        'SCIM Server task/image revision differs')
    server = containers[0]
    env = {row['name']: row['value'] for row in server.get('environment', [])}
    require(env.get('Scim__OidcIssuer') == enrollment['identity']['issuer'] and
        env.get('Oidc__Authority') == enrollment['identity']['issuer'] and
        {name: value for name, value in env.items() if name.startswith('Oidc__AdminRoles')} == {
            'Oidc__AdminRoles__0': enrollment['identity']['approverRole'], 'Oidc__AdminRoles__1': enrollment['identity']['approverRole']} and
        'Scim__BearerToken' not in env and not server.get('environmentFiles') and
        [row['valueFrom'] for row in server.get('secrets', []) if row.get('name') == 'Scim__BearerToken'] ==
            [immutable_secret(configuration['credentials']['scim'])],
        'Server SCIM issuer/immutable version or existing approver ceiling differs')
    guard()
    task_answer = ecs.describe_tasks(cluster=bootstrap['clusterArn'], tasks=[native['taskArn']])
    guard()
    tasks = task_answer.get('tasks', [])
    require(not task_answer.get('failures') and len(tasks) == 1 and tasks[0].get('taskArn') == native['taskArn'] and
        tasks[0].get('clusterArn') == bootstrap['clusterArn'] and tasks[0].get('taskDefinitionArn') == native['taskDefinitionArn'] and
        tasks[0].get('lastStatus') == 'RUNNING' and not tasks[0].get('enableExecuteCommand', True),
        'current private bootstrap task identity is not independently running')
    require(all(not row.get(field) for row in tasks[0].get('overrides', {}).get('containerOverrides', [])
        for field in ('command', 'environment', 'environmentFiles')), 'private bootstrap task has unreviewed credential/command overrides')
    guard()
    own = ecs.describe_task_definition(taskDefinition=native['taskDefinitionArn'])['taskDefinition']
    guard()
    containers = own.get('containerDefinitions', [])
    require(own.get('taskDefinitionArn') == native['taskDefinitionArn'] and len(containers) == 1 and
        containers[0].get('name') == 'native-bootstrap' and containers[0].get('image') == native['imageUri'] == bootstrap['imageUri'] and
        not containers[0].get('environmentFiles'), 'private bootstrap task definition/image differs')
    own = containers[0]
    for name, kind in ((SCIM_ENV, 'scim'), (ROOT_ENV, 'root')):
        require(not any(row.get('name') == name for row in own.get('environment', [])) and
            [row['valueFrom'] for row in own.get('secrets', []) if row.get('name') == name] ==
                [immutable_secret(configuration['credentials'][kind])], 'private bootstrap credential version is not its exact injection')
    # Never retain definitions containing env/secret material. These identity-only
    # projections are independently reobserved around each native request.
    return {'serverTaskDefinitionArn': definition['taskDefinitionArn'], 'serverServiceArn': service['serviceArn'],
        'issuer': env['Scim__OidcIssuer'], 'scim': configuration['credentials']['scim'],
        'bootstrapTaskArn': native['taskArn'], 'bootstrapTaskDefinitionArn': native['taskDefinitionArn']}


class NativeHttp:
    def __init__(self, endpoint, *, fence):
        require(re.fullmatch(r'https://[a-z0-9-]+\.pilot\.demo\.honua\.io', endpoint), 'SCIM setup needs the reviewed isolated native origin')
        self.endpoint, self.fence = endpoint, fence
        self.credentials = {kind: os.environ.get(name) for kind, name in (('scim', SCIM_ENV), ('root', ROOT_ENV))}
        require(all(isinstance(secret, str) and len(secret) >= 32 and '\r' not in secret and '\n' not in secret
            for secret in self.credentials.values()) and len(set(self.credentials.values())) == 2,
            'separate actual private SCIM/root credentials are missing')
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, kind, method, path, payload=None):
        require((kind == 'scim' and (method == 'GET' and (path == '/scim/v2/ServiceProviderConfig' or
            path.startswith('/scim/v2/Users?filter=') or re.fullmatch(r'/scim/v2/Users/[A-Za-z0-9_.:-]+', path)) or
            method == 'POST' and path == '/scim/v2/Users')) or
            (kind == 'root' and (method == 'GET' and (path in ('/api/v1/agent/target', '/api/v1/admin/roles') or
                re.fullmatch(r'/api/v1/admin/(?:roles|users)/[A-Za-z0-9_.:-]+(?:/permissions|/effective-permissions)?', path)) or
                method == 'POST' and path == '/api/v1/admin/roles' or
                method == 'PUT' and re.fullmatch(r'/api/v1/admin/(?:roles/[a-f0-9-]{36}/permissions|users/[A-Za-z0-9_.:-]+/roles)', path))),
            'human membership HTTP method/path is outside its finite provisioning contract')
        header = 'Authorization' if kind == 'scim' else 'X-API-Key'
        token = ('Bearer ' if kind == 'scim' else '')+self.credentials[kind]
        raw = None if payload is None else json.dumps(payload, separators=(',', ':'), allow_nan=False).encode()
        require(raw is None or len(raw) <= LIMIT, 'native membership request exceeds bounded HTTP input; do not truncate grants')
        request = urllib.request.Request(self.endpoint+path, method=method, data=raw, headers={
            header: token, 'Accept': 'application/scim+json' if kind == 'scim' else 'application/json',
            'Content-Type': 'application/scim+json' if kind == 'scim' else 'application/json'})
        self.fence()
        with self.opener.open(request, timeout=30) as answer:
            data = answer.read(LIMIT+1)
            status = answer.status
            require(status == (201 if method == 'POST' else 200) and len(data) <= LIMIT,
                'native membership request did not return bounded success')
        self.fence()
        value = json.loads(data)
        require(all(secret not in json.dumps(value, ensure_ascii=False) for secret in self.credentials.values()),
            'native membership response contains private credential material')
        if kind == 'root' and path != '/api/v1/agent/target':
            require(isinstance(value, dict) and value.get('success') is True and 'data' in value,
                'native role request lacks successful envelope')
        return {'httpStatus': status, 'body': value}


def grants(value):
    require(isinstance(value, list) and all(isinstance(row, dict) and set(row) == {'service', 'layer', 'operation'} and
        all(isinstance(part, str) for part in row.values()) for row in value), 'native permission read lacks typed grants')
    tuples = [(row['service'], row['operation'], row['layer']) for row in value]
    require(len(tuples) == len(set(tuples)), 'native permission read contains ambiguous duplicate grants')
    return set(tuples)


def provision_human_memberships(*, configuration, bootstrap, durable, native, guard,
                              s3=None, ecs=None, github=github_json, http_factory=NativeHttp):
    """Called by checked bootstrap only; retries observe original intents, never resend."""
    validate(configuration, bootstrap)
    guard()
    require(durable.store.candidate == bootstrap['candidateKey'], 'human membership setup belongs to another candidate')
    s3 = s3 or sdk('s3', bootstrap['region'])
    ecs = ecs or sdk('ecs', bootstrap['region'])
    enrollment, proof = enrollment_original(configuration, bootstrap, s3=s3, github=github, guard=guard)
    prefix = 'human-memberships:'+canonical_digest({'candidateKey': bootstrap['candidateKey'],
        'operationId': bootstrap['operation']['operationId'], 'configuration': configuration})[7:]
    records = durable.state.setdefault('scenarioOperations', {})
    producer = 'private-human-memberships'
    def retain(stage, value):
        guard()
        key = prefix+'/'+stage
        record = {'producer': producer, 'candidateKey': bootstrap['candidateKey'], 'value': copy.deepcopy(value)}
        require(key not in records or records[key] == record, 'cannot replace original human membership observation')
        records[key] = record
        durable._persist()
        return {'key': key, 'sha256': canonical_digest(record)}
    def original(stage):
        record = records.get(prefix+'/'+stage)
        if record is None:
            return None
        require(record.get('producer') == producer and record.get('candidateKey') == bootstrap['candidateKey'],
            'original membership stage producer/candidate differs')
        return record['value']
    start = {'configurationDigest': canonical_digest(configuration), 'operation': bootstrap['operation'],
        'targetId': bootstrap['targetId'], 'datasetImportScopes': bootstrap['datasetImportScopes']}
    retain('start', start)
    prior_proof = original('enrollment-original')
    if prior_proof is None:
        retain('enrollment-original', proof)
    else:
        require(all(prior_proof[name] == proof[name] for name in ('enrollment', 'review', 'sourceConfiguration')),
            'original human enrollment source/reference changed')
        retain('enrollment-reobserved-'+canonical_digest(proof)[7:], proof)
    def server_fence():
        return deployed_server(ecs, enrollment, configuration, bootstrap, native, guard=guard)
    retain('deployment-'+canonical_digest(native)[7:], server_fence())
    client = http_factory(bootstrap['endpoint'], fence=guard)
    def request(stage, kind, method, path, payload=None):
        guard()
        observed = client.request(kind, method, path, payload)
        guard()
        require(isinstance(observed, dict) and set(observed) == {'httpStatus', 'body'} and
            observed['httpStatus'] == (201 if method == 'POST' else 200), 'native membership original response is malformed')
        reference = retain('http-'+stage+'-'+str(len(records)), {'request': {'credential': configuration['credentials'][kind],
            'method': method, 'path': path, 'body': payload}, 'response': observed})
        body = observed['body']
        if kind == 'root' and path != '/api/v1/agent/target':
            require(isinstance(body, dict) and body.get('success') is True and 'data' in body, 'native role read lacks successful envelope')
            body = body['data']
        return body, reference
    def once(stage, kind, method, path, payload, observe, matches, empty):
        intent = {'credential': configuration['credentials'][kind], 'method': method, 'path': path, 'body': payload}
        existed = original(stage+'-intent')
        require(existed is None or existed == intent, 'original human membership mutation identity changed')
        before, before_ref = observe()
        if existed is not None:
            require(matches(before), 'original membership mutation outcome unresolved; observe without repeating it')
            retain(stage+'-observed-'+canonical_digest({'value': before, 'reference': before_ref})[7:], {'value': before, 'reference': before_ref})
            return before
        require(empty(before), 'human membership setup refuses preexisting or unknown authority')
        retain(stage+'-baseline', {'value': before, 'reference': before_ref})
        retain(stage+'-intent', intent)
        server_fence()
        try:
            response, response_ref = request(stage+'-mutation', kind, method, path, payload)
        except Exception:
            # The response may have been lost after commit. Only native readback
            # can resolve it; the same POST/PUT is never issued a second time.
            server_fence()
            current, reference = observe()
            require(matches(current), 'original membership mutation outcome unresolved; preserve its liability')
            retain(stage+'-observed-'+canonical_digest({'value': current, 'reference': reference})[7:], {'value': current, 'reference': reference})
            return current
        server_fence()
        retain(stage+'-acknowledged', {'value': response, 'reference': response_ref})
        current, reference = observe()
        require(matches(current), 'native mutation did not produce the exact reviewed membership authority')
        retain(stage+'-observed-'+canonical_digest({'value': current, 'reference': reference})[7:], {'value': current, 'reference': reference})
        return current
    capability, capability_ref = request('scim-capability', 'scim', 'GET', '/scim/v2/ServiceProviderConfig')
    require(isinstance(capability, dict) and capability.get('schemas') == ['urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig'] and
        capability.get('filter', {}).get('supported') is True and
        any(row.get('type') == 'oauthbearertoken' for row in capability.get('authenticationSchemes', [])),
        'actual Server does not expose the required SCIM provisioning capability')
    target, target_ref = request('native-target', 'root', 'GET', '/api/v1/agent/target')
    require(target.get('targetId') == bootstrap['targetId'], 'membership target differs from actual native deployment')
    capability_binding = {'scim': capability_ref, 'target': target_ref}
    retain('capability-'+canonical_digest(capability_binding)[7:], capability_binding)
    roles = {}
    author_grants = [{'service': 'agent', 'operation': 'workspace', 'layer': '*'}, *[
        {'service': 'agent', 'operation': 'dataset-import', 'layer': scope} for scope in bootstrap['datasetImportScopes']]]
    for kind in ('authors', 'approvers'):
        role_name = enrollment['identity']['authorRole' if kind == 'authors' else 'approverRole']
        description = 'Pilot human membership '+prefix+' '+kind
        def role_read(kind=kind, role_name=role_name):
            values, ref = request('roles-'+kind, 'root', 'GET', '/api/v1/admin/roles')
            require(isinstance(values, list) and len(values) <= 200, 'bounded native role list is unavailable')
            selected = [row for row in values if row.get('name') == role_name]
            require(len(selected) <= 1, 'native role name is ambiguous')
            return selected, ref
        def role_matches(values, role_name=role_name, description=description):
            return (len(values) == 1 and values[0].get('name') == role_name and values[0].get('description') == description and
                values[0].get('isBuiltIn') is False and isinstance(values[0].get('roleId'), str) and
                str(UUID(values[0]['roleId'])) == values[0]['roleId'])
        result = once('role-'+kind, 'root', 'POST', '/api/v1/admin/roles',
            {'name': role_name, 'description': description}, role_read, role_matches, lambda values: values == [])
        role_id = result[0]['roleId']
        path = '/api/v1/admin/roles/'+role_id+'/permissions'
        wanted = author_grants if kind == 'authors' else []
        def permission_read(path=path, kind=kind):
            return request('permissions-'+kind, 'root', 'GET', path)
        if kind == 'authors':
            once('permissions-'+kind, 'root', 'PUT', path, {'permissions': wanted}, permission_read,
                lambda value: grants(value) == grants(wanted), lambda value: grants(value) == set())
        else:
            existing, ref = permission_read()
            require(grants(existing) == set(), 'existing approver role must not receive wildcard or additional grants')
            retain('approver-permissions-'+canonical_digest(ref)[7:], {'reference': ref, 'permissions': []})
        roles[kind] = {'roleId': role_id, 'name': role_name, 'permissions': wanted}
    users = {}
    for alias, person in sorted(enrollment['users'].items()):
        query = '/scim/v2/Users?filter='+urllib.parse.quote('userName eq "'+alias+'"', safe='')+'&count=2'
        def user_read(alias=alias, query=query):
            value, ref = request('user-'+alias, 'scim', 'GET', query)
            require(isinstance(value, dict) and type(value.get('totalResults')) is int and
                value['totalResults'] in (0, 1) and value.get('startIndex') == 1 and
                isinstance(value.get('resources'), list) and len(value['resources']) == value['totalResults'],
                'exact SCIM user lookup is missing or ambiguous')
            return value['resources'], ref
        def user_matches(values, alias=alias, person=person):
            return (len(values) == 1 and values[0].get('userName') == alias and
                values[0].get('externalId') == person['subjectId'] and values[0].get('active') is True and
                isinstance(values[0].get('id'), str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', values[0]['id']) is not None)
        selected = once('user-'+alias, 'scim', 'POST', '/scim/v2/Users',
            {'schemas': ['urn:ietf:params:scim:schemas:core:2.0:User'], 'userName': alias,
             'externalId': person['subjectId'], 'active': True}, user_read, user_matches, lambda value: value == [])
        user_id = selected[0]['id']
        role = roles[person['role']]
        def assigned_roles(alias=alias, user_id=user_id):
            value, ref = request('user-roles-'+alias, 'root', 'GET', '/api/v1/admin/users/'+user_id)
            require(value.get('userId') == user_id and value.get('isActive') is True and
                value.get('provisioningSource') == 'scim' and isinstance(value.get('roles'), list),
                'native managed user differs from original SCIM identity')
            return value['roles'], ref
        once('assignment-'+alias, 'root', 'PUT', '/api/v1/admin/users/'+user_id+'/roles',
            {'roles': [role['name']]}, assigned_roles, lambda value: value == [role['name']], lambda value: value == [])
        effective, ref = request('effective-'+alias, 'root', 'GET', '/api/v1/admin/users/'+user_id+'/effective-permissions')
        require(effective.get('userId') == user_id and effective.get('roles') == [role['name']] and
            grants(effective.get('permissions')) == grants(role['permissions']), 'native effective human grants differ from the exact reviewed ceiling')
        users[alias] = {'userId': user_id, 'subjectId': person['subjectId'], 'role': person['role'],
            'roleId': role['roleId'], 'effectiveReference': ref}
    server_fence()
    result = {'schema': 'honua.private-human-memberships/v1', 'candidateKey': bootstrap['candidateKey'],
        'enrollment': configuration['enrollment'], 'targetId': bootstrap['targetId'], 'issuer': enrollment['identity']['issuer'],
        'roles': roles, 'users': users, 'native': native, 'qualification': False}
    previous = original('completed')
    if previous is not None:
        require(all(previous[name] == result[name] for name in ('schema', 'candidateKey', 'enrollment', 'targetId', 'issuer', 'roles', 'qualification')) and
            set(previous['users']) == set(users) and all(all(previous['users'][alias][name] == user[name]
                for name in ('userId', 'subjectId', 'role', 'roleId')) for alias, user in users.items()),
            'previously completed human memberships no longer match native identities/authority')
        retain('completed-reobserved-'+canonical_digest(result)[7:], result)
        return previous
    retain('completed', result)
    return result
