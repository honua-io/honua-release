"""Load a reviewed installed controller declaration from original custody.

The declaration predates native registration. Its hash cannot include a future
registration receipt. Native registration and later execution binding are
separate original observations, never caller-authored expected slot bodies.
"""
from __future__ import annotations

import copy
import re
import time

from agent_e2e_budget import validate as validate_budget, money
from agent_e2e_campaign import source_file, validate_model_source
from agent_e2e_console_runtime import require, identifier, digest
from agent_e2e_controller import BedrockModel
from agent_e2e_interactive_budget import validate_retained_funding
from agent_e2e_pack import canonical_digest, load_json

SCHEMA = 'honua.installed-console-declaration/v1'


def reference(value):
    require(isinstance(value, dict) and set(value) == {'key', 'sha256'} and
            isinstance(value['key'], str) and value['key'] and len(value['key']) <= 2048,
            'original durable installation reference missing')
    digest(value['sha256'])
    return value


def retained(durable, ref, producer):
    reference(ref)
    row = durable.state.get('scenarioOperations', {}).get(ref['key'])
    require(isinstance(row, dict) and canonical_digest(row) == ref['sha256'] and
            row.get('producer') == producer and row.get('candidateKey') == durable.store.candidate and
            isinstance(row.get('value'), dict), 'actual original installation record missing or changed')
    return row['value']


def load_declaration(reference, *, durable, s3, ecs, states, guard,
                     source_reader=source_file, worker_verifier=None):
    """Return verified installation inputs; never creates a key/task/admission."""
    require(callable(guard), 'installed declaration requires original live custody guard')
    guard(); durable._require_live_lease()
    validate_retained_funding(durable.state)
    validate_budget(durable.state['ledger'])
    require(isinstance(reference, dict) and set(reference) == {'path', 'sha256'} and
            isinstance(reference['path'], str) and re.fullmatch(
                r'certification/agent-e2e/installed-runtimes/[A-Za-z0-9_-]{1,128}\.json', reference['path']),
            'installed declaration must be an exact reviewed source file')
    digest(reference['sha256'])
    declaration = load_json(source_reader(reference['path'], reference['sha256']))
    fields = {'schema', 'candidateKey', 'runtimeId', 'targetId', 'endpoint', 'controller', 'nativeBootstrap', 'workers',
        'mutationCeilings', 'model', 'operationPolicy', 'operation', 'fundingBinding', 'evidence'}
    require(isinstance(declaration, dict) and set(declaration) == fields and declaration['schema'] == SCHEMA and
            declaration['candidateKey'] == durable.store.candidate and
            declaration['fundingBinding'] == durable.state['ledger'].get('interactiveAccounting'),
            'installed declaration differs from original candidate funding/capability')
    runtime = identifier(declaration['runtimeId'], 'runtime ID')
    target = identifier(declaration['targetId'], 'target ID')
    require(reference['path'] == 'certification/agent-e2e/installed-runtimes/'+runtime+'.json' and
            re.fullmatch(r'https://[a-z0-9-]+\.pilot\.demo\.honua\.io', declaration['endpoint']),
            'installed declaration origin/runtime source differs')
    expiry = declaration['fundingBinding']['originalExpiresEpoch']
    require(type(expiry) is int and time.time() < expiry, 'original installed runtime funding expired')
    # Guard owns the existing installation liability and original expiry. This
    # loader never invents a new controller window, budget, source or allowance.
    guard()
    native_ref = declaration['nativeBootstrap']
    native = retained(durable, native_ref, 'private-native-bootstrap')
    require(native_ref['key'].endswith('/completed') and native.get('schema') == 'honua.private-native-bootstrap/v1' and
            native.get('candidateKey') == durable.store.candidate and native.get('target') == {'targetId': target} and
            native.get('qualification') is False and isinstance(native.get('keys'), dict),
            'actual original native bootstrap completion differs')
    prefix = native_ref['key'].removesuffix('/completed')
    native_operation = native.get('operation')
    require(isinstance(native_operation, dict) and set(native_operation) == {'operationId', 'liabilityBasisDigest', 'reservationIds'} and
            prefix == canonical_digest({'kind': 'native-bootstrap', 'candidateKey': durable.store.candidate,
                'operationId': native_operation['operationId']}), 'native bootstrap original operation identity differs')
    start_ref = {'key': prefix+'/start', 'sha256': canonical_digest(
        durable.state.get('scenarioOperations', {}).get(prefix+'/start'))}
    start = retained(durable, start_ref, 'private-native-bootstrap')
    require(start.get('configurationDigest') == native.get('configurationDigest'), 'native issuance start/completion differs')
    keys = {}
    for output, kind in (('control', 'control'), ('task', 'agent')):
        ref = {'key': prefix+'/key-'+kind, 'sha256': canonical_digest(
            durable.state.get('scenarioOperations', {}).get(prefix+'/key-'+kind))}
        issued = retained(durable, ref, 'private-native-bootstrap')
        require(issued == native['keys'].get(kind) and set(issued) == {'apiKeyId', 'secretArn', 'versionId', 'permissions', 'effectivePermissions'} and
                re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', issued['apiKeyId']) and
                isinstance(issued['permissions'], list) and len(set(issued['permissions'])) == len(issued['permissions']) and
                issued['effectivePermissions'].get('id') == issued['apiKeyId'] and
                issued['effectivePermissions'].get('canAuthenticate') is True and
                issued['effectivePermissions'].get('status') == 'active' and
                sorted(issued['effectivePermissions'].get('permissions', [])) == sorted(issued['permissions']),
                'actual issued runtime key/effective identity differs')
        intent_ref = {'key': prefix+'/key-'+kind+'-intent', 'sha256': canonical_digest(
            durable.state.get('scenarioOperations', {}).get(prefix+'/key-'+kind+'-intent'))}
        intent = retained(durable, intent_ref, 'private-native-bootstrap')
        from datetime import datetime
        expires = datetime.fromisoformat(intent['expiresAt'].replace('Z', '+00:00'))
        require(expires.tzinfo is not None and expires.timestamp() == expiry and
                intent['permissions'] == issued['permissions'] and intent['secretVersionId'] == issued['versionId'],
                'runtime key original expiry/version/grants differ')
        keys[output] = {'reference': ref, 'issued': copy.deepcopy(issued)}
    require(keys['control']['issued']['apiKeyId'] != keys['task']['issued']['apiKeyId'] and
            keys['control']['issued']['secretArn'] != keys['task']['issued']['secretArn'] and
            set(keys['control']['issued']['permissions']) == {'admin:read', 'agent:runtime-control:'+runtime},
            'runtime control must be a distinct exact installed key')
    task_permissions = set(keys['task']['issued']['permissions'])
    require({'admin:read', 'agent:workspace', 'agent:runtime-task:'+runtime} <= task_permissions and
            {p for p in task_permissions if p.startswith('agent:runtime-task:')} == {'agent:runtime-task:'+runtime} and
            not any(p.startswith('agent:runtime-control:') or p.lower() in ('admin', '*') or
                p.lower().startswith('admin:') and p != 'admin:read' or p.lower().startswith('write:') for p in task_permissions),
            'installed task key has no exact non-authorizing runtime marker or crosses control scope')
    ceiling = declaration['mutationCeilings']
    names = {'createMap', 'createDashboard', 'compose', 'proposePublication', 'proposeRestore', 'datasetImportScopes', 'managedProcessIds'}
    require(isinstance(ceiling, dict) and set(ceiling) == names and all(type(ceiling[k]) is bool for k in names-
            {'datasetImportScopes', 'managedProcessIds'}), 'installed mutation ceilings missing')
    require(all(isinstance(ceiling[name], list) and all(isinstance(v, str) for v in ceiling[name]) and
                len(set(ceiling[name])) == len(ceiling[name]) for name in ('datasetImportScopes', 'managedProcessIds')) and
            all(re.fullmatch(re.escape(target)+r'/[a-z_][a-z0-9_]{0,62}/[a-z][a-z0-9_]{0,62}', scope)
                for scope in ceiling['datasetImportScopes']) and
            all(process == 'analytics.buffer-aggregate-managed' for process in ceiling['managedProcessIds']),
            'installed mutation scopes must name unique exact native destinations/processes')
    studio = (('createMap', 'studio-create-map-runtime'), ('createDashboard', 'studio-create-dashboard-runtime'),
            ('compose', 'studio-compose-runtime'), ('proposePublication', 'studio-propose-publication-runtime'),
            ('proposeRestore', 'studio-restore-runtime'))
    for name, operation in studio:
        require(('agent:'+operation+':'+runtime in task_permissions) == ceiling[name], 'runtime Studio grant/ceiling mismatch')
    allowed_studio = {'agent:'+operation+':'+runtime for name, operation in studio if ceiling[name]}
    require(not any(p.startswith('agent:studio-') and '-runtime:' in p and p not in allowed_studio for p in task_permissions),
            'installed task key contains foreign runtime Studio authority')
    require(sorted(p.removeprefix('agent:dataset-import:') for p in task_permissions if p.startswith('agent:dataset-import:')) ==
                sorted(ceiling['datasetImportScopes']) and
            sorted(p.removeprefix('agent:managed-process:') for p in task_permissions if p.startswith('agent:managed-process:')) ==
                sorted(ceiling['managedProcessIds']), 'runtime import/process ceilings differ from exact original key')
    controller_ref = declaration['controller']
    require(isinstance(controller_ref, dict) and set(controller_ref) == {'path', 'sha256'} and
            controller_ref['path'] == 'certification/agent-e2e/controller-handoff.json', 'reviewed controller handoff source missing')
    digest(controller_ref['sha256'])
    from hosted_pilot.controller_handoff import validate_configuration, model_runtime
    controller = load_json(source_reader(controller_ref['path'], controller_ref['sha256']))
    validate_configuration(controller)
    budget = controller['dispatchBudget']
    require(budget['candidateKey'] == durable.store.candidate and budget['state']['bucket'] == durable.store.bucket and
            budget['state']['table'] == durable.store.table, 'installed controller does not use the original candidate store')
    model = declaration['model']
    validate_model_source(model, source_reader=source_reader)
    BedrockModel(model, client=object())  # Constructor validation only; no model or quote call.
    pin = {key: model[key] for key in ('modelId', 'returnedModelId', 'profileArn', 'tokenCountingModelArn')}
    pin['profileSha256'] = model['profile']['sha256']
    require(controller.get('model') == pin, 'installed controller model/profile differs from reviewed declaration')
    model_runtime(pin, controller['region'], controller['accountId'])
    policy = declaration['operationPolicy']
    require(isinstance(policy, dict) and set(policy) == {'maxTurns', 'maxActions', 'allowedActions'} and
            type(policy['maxTurns']) is int and 1 <= policy['maxTurns'] <= 100 and
            type(policy['maxActions']) is int and 1 <= policy['maxActions'] <= 100 and
            isinstance(policy['allowedActions'], list) and len(set(policy['allowedActions'])) == len(policy['allowedActions']) and
            set(policy['allowedActions']) == {'mcp_list_tools', 'mcp_call_tool', 'ask_user'},
            'installed Console requires its reviewed native MCP operation policy')
    operation = declaration['operation']
    require(isinstance(operation, dict) and set(operation) == {'operationId', 'liabilityBasisDigest', 'reservationIds'} and
            set(operation['reservationIds']) == {'infrastructure', 'cleanup'} and
            len(set(operation['reservationIds'].values())) == 2, 'original installed controller liability missing')
    for kind, rid in operation['reservationIds'].items():
        rows = [row for row in durable.state['ledger']['operations'] if row['id'] == rid]
        require(len(rows) == 1 and rows[0]['kind'] == kind and rows[0]['operationId'] == operation['operationId'] and
                rows[0]['basisDigest'] == operation['liabilityBasisDigest'] and rows[0]['purpose'] == 'shared-environment' and
                rows[0]['state'] == 'reserved' and rows[0]['actualUsd'] is None and money(rows[0]['amountUsd']) > 0,
                'installed controller lacks original reserved infrastructure/cleanup operation')
    evidence = declaration['evidence']
    require(isinstance(evidence, dict) and set(evidence) == {'region', 'bucket'} and
            evidence['region'] == controller['region'] and evidence['bucket'] == durable.store.bucket,
            'installed evidence must use original regional versioned candidate custody')
    if worker_verifier is None:
        from hosted_pilot.installed_worker_receipt import verify_installation
        worker_verifier = verify_installation
    refs = declaration['workers']
    require(isinstance(refs, list) and 1 <= len(refs) <= 4 and len({canonical_digest(ref) for ref in refs}) == len(refs),
            'installed declaration requires unique actual isolated worker originals')
    workers, slots = [], []
    for ref in refs:
        guard()
        require(time.time() < expiry, 'original installed runtime funding expired')
        require(isinstance(ref, dict) and set(ref) == {'bucket', 'key', 'versionId', 'sha256'} and
                ref['bucket'] == durable.store.bucket and isinstance(ref['key'], str) and ref['key'].startswith(
                    'agent-e2e/'+durable.store.candidate[7:]+'/installed-workers/'+runtime+'/') and
                isinstance(ref['versionId'], str) and ref['versionId'] and ref['versionId'] != 'null',
                'versioned original worker installation reference missing')
        digest(ref['sha256'])
        verified = worker_verifier(ref, durable=durable, runtime_id=runtime, target_id=target,
            original_expires_epoch=expiry, s3=s3, ecs=ecs, states=states, guard=guard)
        require(isinstance(verified, dict) and set(verified) == {'descriptor', 'mcpBootstrap', 'issuedAgentKey', 'nativeBootstrap',
                'producer', 'sourceConfiguration', 'plan', 'proof', 'activation', 'qualification'} and
                verified['activation'] is False and verified['qualification'] is False and verified['nativeBootstrap'] == native_ref and
                verified['issuedAgentKey'] == keys['task']['reference'] and verified['proof'].get('nativeEndpoint') == declaration['endpoint'],
                'worker original does not bind the same actual native issuance/origin')
        descriptor = verified['descriptor']
        require(descriptor['schema'] == 'honua.installed-worker-slot/v1' and descriptor['candidateKey'] == durable.store.candidate and
                descriptor['runtimeId'] == runtime and descriptor['originalFundingExpiresEpoch'] == expiry,
                'actual worker descriptor differs from original installed runtime/funding')
        slots.append({'slotId': descriptor['slotId'], 'taskDefinitionArn': descriptor['taskDefinitionArn'],
            'taskRoleArn': descriptor['taskRoleArn'], 'image': descriptor['image'],
            'mcpSecretArn': descriptor['mcpSecret']['secretArn'], 'mcpSecretVersionId': descriptor['mcpSecret']['versionId']})
        workers.append(copy.deepcopy(verified))
    require(len({slot['slotId'] for slot in slots}) == len(slots) and
            len({slot['taskRoleArn'] for slot in slots}) == len(slots) and
            len({slot['taskDefinitionArn'] for slot in slots}) == len(slots), 'installed paid slots overlap physical identities')
    configuration_digest = canonical_digest(declaration)
    request = {'schemaVersion': 1, 'runtimeId': runtime, 'candidateKey': durable.store.candidate,
        'targetId': target, 'configurationDigest': configuration_digest, 'originalFundingExpiresEpoch': expiry,
        'controlApiKeyId': keys['control']['issued']['apiKeyId'], 'taskApiKeyId': keys['task']['issued']['apiKeyId'],
        'mutationCeilings': copy.deepcopy(ceiling), 'slots': slots}
    guard()
    require(time.time() < expiry, 'original installed runtime funding expired')
    return {'schema': 'honua.verified-installed-console-declaration/v1', 'configurationDigest': configuration_digest,
        'endpoint': declaration['endpoint'], 'candidateKey': durable.store.candidate, 'runtimeId': runtime, 'targetId': target,
        'originalFundingExpiresEpoch': expiry, 'mutationCeilings': copy.deepcopy(ceiling), 'registrationRequest': request,
        'keys': keys, 'nativeBootstrap': copy.deepcopy(native_ref), 'fundingBinding': copy.deepcopy(declaration['fundingBinding']),
        'operation': copy.deepcopy(operation), 'workers': workers, 'model': copy.deepcopy(model),
        'operationPolicy': copy.deepcopy(policy), 'evidence': copy.deepcopy(evidence), 'controller': controller,
        'declarationReference': copy.deepcopy(reference)}
