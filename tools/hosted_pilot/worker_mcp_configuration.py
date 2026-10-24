"""Private immutable agent-only MCP configuration from retained native context."""
from __future__ import annotations
import hmac
import json
import os
from pathlib import Path
import re
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_e2e_budget import validate
from agent_e2e_pack import canonical_digest
from fixture_handoff import encoded, require, sdk


def authorize(durable, operation):
    durable._require_live_lease()
    ledger = durable.state['ledger']
    validate(ledger)
    require(ledger['candidateKey'] == durable.store.candidate and set(operation) == {'operationId', 'liabilityBasisDigest', 'reservationIds'} and
            set(operation['reservationIds']) == {'infrastructure', 'cleanup'}, 'MCP configuration lacks original candidate operation')
    for kind, reservation in operation['reservationIds'].items():
        rows = [row for row in ledger.get('operations', []) if row['id'] == reservation]
        require(len(rows) == 1 and rows[0]['operationId'] == operation['operationId'] and rows[0]['purpose'] == 'bootstrap' and
                rows[0]['kind'] == kind and rows[0]['state'] == 'reserved' and rows[0]['actualUsd'] is None and
                rows[0]['basisDigest'] == operation['liabilityBasisDigest'], 'MCP configuration original effect/cleanup liability differs')


def retained_context(durable, reference, endpoint):
    require(set(reference) == {'key', 'sha256'}, 'actual native context reference required')
    value = durable.state.get('studioBootstrapOperations', {}).get(reference['key'])
    require(isinstance(value, dict) and canonical_digest(value) == reference['sha256'] and
            value.get('schema') == 'honua.native-agent-context/v1' and value.get('candidateKey') == durable.store.candidate,
            'MCP configuration cannot use caller-declared workspace or turn identifiers')
    workspace, turn = value['workspace'], value['turn']
    require(value['agentContext'] == {'workspaceId': workspace['workspaceId'], 'turnId': turn['turnId']} and turn['context'] == workspace,
            'MCP configuration captured turn/context bindings differ')
    originals = durable.state.get('scenarioOperations', {})
    key_reference = value.get('nativeBootstrapReference', {})
    key_record = originals.get(key_reference.get('key'))
    require(isinstance(key_record, dict) and canonical_digest(key_record) == key_reference.get('sha256') and
            key_record.get('producer') == 'private-native-bootstrap' and key_record.get('candidateKey') == durable.store.candidate and
            key_record.get('value') == value['issuedAgentKey'], 'MCP credential is not the original actually issued agent identity')
    permissions = value['issuedAgentKey']['permissions']
    require('agent:workspace' in permissions and 'admin:read' in permissions and
            all(permission != '*' and (not permission.startswith('admin:') or permission == 'admin:read') for permission in permissions),
            'MCP configuration cannot carry privileged setup or approval credentials')
    principal = value.get('agentPrincipal', {})
    human = value.get('humanCreatorPrincipal', {})
    require(value.get('studioOwnerPrincipalId') == value['issuedAgentKey']['apiKeyId'] == principal.get('studioOwnerId') and
            value.get('workspaceAgentPrincipalId') == 'apikey:api-key:'+value['issuedAgentKey']['apiKeyId'] == principal.get('actorId') and
            principal.get('authenticationKind') == 'api-key' and principal.get('isHumanSession') is False and
            human.get('isHumanSession') is True and human.get('authenticationKind') in ('interactive', 'operator-bearer') and
            isinstance(human.get('actorId'), str) and human['actorId'] != principal['actorId'] and
            human.get('tenantId') == principal.get('tenantId'),
            'MCP native human/agent pairing or Studio owner differs')
    paths = {'workspaceReadback': '/api/v1/agent/workspaces/'+workspace['workspaceId']+'?viewId='+workspace['viewId'],
             'turnReadback': '/api/v1/agent/workspaces/'+workspace['workspaceId']+'/turns/'+turn['turnId'],
             'agentTarget': '/api/v1/agent/target', 'target': '/api/v1/agent/target'}
    if 'consoleTurn' in value:
        paths.update(workspace=paths['workspaceReadback'], turn=paths['turnReadback'])
    for name, path in paths.items():
        selected = value['nativeOriginals'][name]['reference']
        response = originals.get(selected['key'])
        require(isinstance(response, dict) and canonical_digest(response) == selected['sha256'], 'MCP context original native readback is absent or changed')
        origin = response.get('intentReference', {})
        intent = originals.get(origin.get('key'))
        require(isinstance(intent, dict) and canonical_digest(intent) == origin.get('sha256') and
                intent.get('method') == 'GET' and intent.get('path') == path and intent.get('endpoint') == endpoint and
                intent.get('credentialKind') == ('bearer' if name in ('target', 'workspace', 'turn') else 'api-key') and intent.get('candidateKey') == durable.store.candidate and
                intent.get('attemptId') == 'native-context-'+reference['key'][7:31] and response.get('attemptId') == intent.get('attemptId') and
                response.get('candidateKey') == durable.store.candidate and response.get('intentDigest') == canonical_digest(intent) and
                response.get('response', {}).get('httpStatus') == 200, 'MCP context is not an authenticated native original on the exact target')
        raw = response['response']['body']
        if name in ('target', 'agentTarget'):
            require(isinstance(raw, dict) and raw.get('targetId') == workspace['targetId'] and
                    raw.get('principal') == (human if name == 'target' else principal),
                    'MCP native principal differs from authenticated original')
        else:
            require(raw == workspace if name in ('workspaceReadback', 'workspace') else isinstance(raw, dict) and
                    raw.get('turnId') == turn['turnId'] and raw.get('context') == workspace and raw.get('status') == turn.get('status'),
                    'MCP retained native context changed after observation')
    if 'consoleTurn' in value:
        from console_turn import retained_session, validate_turn
        adopted = value['consoleTurn']
        require(set(adopted) == {'configuration', 'sessionObservation'}, 'retained Console adoption shape differs')
        custody = retained_session(durable, adopted['configuration']['sessionCustody'], endpoint, human)
        require(all(adopted['sessionObservation'][name] == custody['observed'][name]
            for name in ('session', 'nativeTarget', 'consoleProfile', 'deployment')),
            'retained Console session differs from original custody')
        validate_turn(adopted['configuration'], turn=turn, workspace=workspace, turn_id=turn['turnId'], human=human)
        # Agent and human must independently read exactly the same captured
        # author, prompt, source, and context; not merely a matching turn id.
        selected = value['nativeOriginals']['turnReadback']['reference']
        human_selected = value['nativeOriginals']['turn']['reference']
        require(originals[selected['key']]['response']['body'] == turn == originals[human_selected['key']]['response']['body'],
            'MCP issued agent original human turn/source differs')
    return value


def verify_injected_secret(binding, credential_env, region, *, ecs=None):
    task_arn = os.environ.get('PILOT_CONTROLLER_TASK_ARN', '')
    require(re.fullmatch('arn:aws:ecs:'+re.escape(region)+r':[0-9]{12}:task/honua-pilot-[a-z0-9-]+/[a-f0-9]{32}', task_arn),
            'private MCP custody needs the independently observed current controller task')
    cluster = task_arn.replace(':task/', ':cluster/').rsplit('/', 1)[0]
    ecs = ecs or sdk('ecs', region)
    observed = ecs.describe_tasks(cluster=cluster, tasks=[task_arn])
    require(not observed.get('failures') and len(observed.get('tasks', [])) == 1 and observed['tasks'][0]['taskArn'] == task_arn,
            'MCP current native credential task cannot be observed')
    definition = ecs.describe_task_definition(taskDefinition=observed['tasks'][0]['taskDefinitionArn'])['taskDefinition']
    containers = definition['containerDefinitions']
    require(len(containers) == 1 and containers[0]['name'] == 'campaign-controller', 'MCP current controller container differs')
    injected = [secret['valueFrom'] for secret in containers[0].get('secrets', []) if secret['name'] == credential_env]
    require(injected == [binding['secretArn']+':::'+binding['versionId']], 'private environment credential is not the exact original immutable issued-agent version')


def verify_injected_credential(context, region, *, ecs=None):
    verify_injected_secret(context['issuedAgentKey'], 'HONUA_AGENT_REQUESTER_KEY', region, ecs=ecs)


def provision(durable, reference, operation, configuration, *, secrets=None, ecs=None):
    require(set(configuration) == {'region', 'secretArn', 'endpoint', 'workflowView'} and
            re.fullmatch(r'arn:aws:secretsmanager:'+re.escape(configuration['region'])+r':[0-9]{12}:secret:honua-agent-[a-z0-9-]+-mcp-[A-Za-z0-9]{6}', configuration['secretArn']) and
            re.fullmatch(r'https://[a-z0-9-]+\.pilot\.demo\.honua\.io/mcp', configuration['endpoint']) and
            (configuration['workflowView'] is None or isinstance(configuration['workflowView'], str) and re.fullmatch(r'[A-Za-z0-9._:-]{1,200}', configuration['workflowView'])),
            'MCP configuration must use reviewed private custody and the isolated native target')
    authorize(durable, operation)
    context = retained_context(durable, reference, configuration['endpoint'].removesuffix('/mcp'))
    verify_injected_credential(context, configuration['region'], ecs=ecs)
    identity = {'kind': 'native-worker-mcp', 'candidateKey': durable.store.candidate,
        'operationId': operation['operationId'], 'destination': configuration['secretArn']}
    if 'consoleTurn' in context:
        identity['nativeContext'] = reference
    token = canonical_digest(identity)[7:]
    key = 'native-worker-mcp:'+token
    records = durable.state.setdefault('scenarioOperations', {})
    public = {**configuration, 'agentContext': context['agentContext'], 'issuedAgentKey': context['issuedAgentKey'],
        'nativeContext': reference, 'operation': operation, 'versionId': token}
    credential = os.environ.get('HONUA_AGENT_REQUESTER_KEY')
    require(isinstance(credential, str) and len(credential) >= 32, 'actual issued private agent credential missing')
    payload = {'endpoint': configuration['endpoint'], 'credentialKind': 'api-key', 'credential': credential,
        'workflowView': configuration['workflowView'], 'agentContext': context['agentContext']}
    expected_intent = {'producer': 'private-worker-mcp', 'candidateKey': durable.store.candidate, 'value': public}
    secrets = secrets or sdk('secretsmanager', configuration['region'])
    authorize(durable, operation)
    if key+'/intent' in records:
        require(records[key+'/intent'] == expected_intent, 'MCP retained provisioning intent differs from the original binding')
        # A lost write acknowledgement or completion persist cannot justify a
        # second write. Read only the original deterministic immutable version;
        # an absent version remains unresolved rather than creating a new one.
        response = secrets.get_secret_value(SecretId=configuration['secretArn'], VersionId=token)
        require(response.get('ARN') == configuration['secretArn'] and response.get('VersionId') == token and
                isinstance(response.get('SecretString'), str) and 'SecretBinary' not in response and
                hmac.compare_digest(response['SecretString'].encode(), encoded(payload)),
                'MCP original immutable secret version or private bytes differ')
    else:
        require(key+'/completed' not in records, 'MCP completion lacks its original provisioning intent')
        records[key+'/intent'] = expected_intent
        durable._persist()
        authorize(durable, operation)
        response = secrets.put_secret_value(SecretId=configuration['secretArn'], ClientRequestToken=token, SecretString=encoded(payload).decode())
        require(response['ARN'] == configuration['secretArn'] and response['VersionId'] == token, 'actual private MCP secret version differs')
    authorize(durable, operation)
    result = {'schema': 'honua.private-worker-mcp-binding/v1', 'secretArn': configuration['secretArn'], 'versionId': token,
        'nativeContext': reference, 'publicBindingDigest': canonical_digest(public), 'qualification': False}
    completed = {'producer': 'private-worker-mcp', 'candidateKey': durable.store.candidate, 'value': result}
    require(key+'/completed' not in records or records[key+'/completed'] == completed, 'MCP retained completion differs from the original binding')
    records[key+'/completed'] = completed
    durable._persist()
    return result
