"""One privileged native installation, followed by current control-key readback.

The dedicated carrier verifies the reviewed installation declaration, original
workflow receipts, immutable credential injections and retained funding.
It does not provision infrastructure, approve an operation or activate a worker.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_e2e_pack import canonical_digest
from agent_e2e_setup_http import SetupHttp, require


PRODUCER = 'private-native-runtime-registration'
CREDENTIAL_ENVS = {'setup': 'PILOT_FIXTURE_ADMIN_PASSWORD',
                   'control': 'HONUA_EXTERNAL_EXECUTION_CONTROL_KEY',
                   'task': 'HONUA_AGENT_REQUESTER_KEY'}
REQUEST_FIELDS = {'schemaVersion', 'runtimeId', 'candidateKey', 'targetId', 'configurationDigest',
                  'originalFundingExpiresEpoch', 'controlApiKeyId', 'taskApiKeyId', 'mutationCeilings', 'slots'}
PRINCIPAL_FIELDS = {'apiKeyId', 'actorId', 'studioOwnerId', 'credentialVersion', 'permissionsDigest'}


def permission_digest(permissions):
    require(isinstance(permissions, list) and permissions and
            all(isinstance(value, str) and 0 < len(value) <= 200 and
                all(33 <= ord(character) <= 126 for character in value) for value in permissions) and
            len(set(permissions)) == len(permissions), 'native permissions must be unique bounded ASCII labels')
    return 'sha256:'+hashlib.sha256('\n'.join(sorted(permissions)).encode()).hexdigest()


def key_original(durable, reference, expected_id):
    require(isinstance(reference, dict) and set(reference) == {'key', 'sha256'},
            'runtime registration needs an original issued-key reference')
    row = durable.state.get('scenarioOperations', {}).get(reference['key'])
    require(isinstance(row, dict) and canonical_digest(row) == reference['sha256'] and
            row.get('producer') == 'private-native-bootstrap' and row.get('candidateKey') == durable.store.candidate,
            'runtime registration issued-key custody is absent or changed')
    key = row.get('value', {})
    require(key.get('apiKeyId') == expected_id and
            isinstance(key.get('secretArn'), str) and isinstance(key.get('versionId'), str),
            'runtime registration cannot substitute an issued key')
    permission_digest(key.get('permissions'))
    return copy.deepcopy(key)


def native_target(body, target_id):
    require(isinstance(body, dict) and body.get('targetId') == target_id and isinstance(body.get('principal'), dict),
            'runtime installation native target differs')
    principal = body['principal']
    require(principal.get('authenticationKind') == 'api-key' and principal.get('isHumanSession') is False and
            all(isinstance(principal.get(name), str) and 0 < len(principal[name]) <= 512
                for name in ('actorId', 'studioOwnerId', 'tenantId')),
            'runtime installation requires an actual machine target principal')
    # actorId and owner are authoritative native observations, never calculated
    # from the API-key ID. The actual registration must return the same values.
    return copy.deepcopy(principal)


def validate_registration(body, request, keys, principals):
    common = REQUEST_FIELDS-{'controlApiKeyId', 'taskApiKeyId'}
    require(isinstance(body, dict) and set(body) == common | {
            'tenantId', 'registeredAt', 'registrationDigest', 'controlPrincipal', 'taskPrincipal'} and
            all(body[name] == request[name] for name in common) and
            re.fullmatch(r'sha256:[a-f0-9]{64}', str(body['registrationDigest'])),
            'native registration differs from the reviewed original installation')
    require(body['tenantId'] == principals['control']['tenantId'] == principals['task']['tenantId'],
            'native runtime principals are not in the original tenant')
    try:
        observed = datetime.fromisoformat(body['registeredAt'].replace('Z', '+00:00'))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('native registration has no original timestamp') from None
    require(observed.tzinfo is not None and observed.timestamp() < request['originalFundingExpiresEpoch'],
            'native runtime registration timestamp exceeds original funding')
    for kind in ('control', 'task'):
        principal = body[kind+'Principal']
        target = principals[kind]
        require(isinstance(principal, dict) and set(principal) == PRINCIPAL_FIELDS and
                principal['apiKeyId'] == keys[kind]['apiKeyId'] and
                principal['actorId'] == target['actorId'] and principal['studioOwnerId'] == target['studioOwnerId'] and
                principal['permissionsDigest'] == permission_digest(keys[kind]['permissions']) and
                isinstance(principal['credentialVersion'], str) and 0 < len(principal['credentialVersion']) <= 512,
                'native registration key, current principal or permission digest differs')
    require(body['controlPrincipal']['actorId'] != body['taskPrincipal']['actorId'],
            'native runtime task and control actors must remain distinct')
    return copy.deepcopy(body)


def provision(durable, *, endpoint, request, key_references, provenance, guard,
              credential_envs=None, http_factory=SetupHttp):
    """Called only by the dedicated verified registrar; recovery sends GET only.

    ``guard`` re-verifies the original declaration/receipts, registrar custody,
    lease and live original funding before every HTTP request. It must not mint
    credentials, renew funding or initialize replacement accounting history.
    """
    require(callable(guard), 'runtime installation requires current verified parent authority')
    guard()
    require(isinstance(request, dict) and set(request) == REQUEST_FIELDS and request['schemaVersion'] == 1 and
            request['candidateKey'] == durable.store.candidate and
            all(re.fullmatch(r'[A-Za-z0-9_-]{1,128}', str(request[name])) for name in ('runtimeId', 'targetId')) and
            re.fullmatch(r'sha256:[a-f0-9]{64}', str(request['configurationDigest'])) and
            type(request['originalFundingExpiresEpoch']) is int and
            request['originalFundingExpiresEpoch'] > datetime.now(timezone.utc).timestamp() and
            re.fullmatch(r'https://[a-z0-9-]+\.pilot\.demo\.honua\.io', endpoint),
            'runtime installation requires an exact live reviewed native request')
    require(set(key_references) == {'control', 'task'} and request['controlApiKeyId'] != request['taskApiKeyId'] and
            isinstance(provenance, dict) and provenance,
            'runtime installation needs distinct original keys and verified declaration provenance')
    keys = {kind: key_original(durable, key_references[kind], request[kind+'ApiKeyId']) for kind in ('control', 'task')}
    require(keys['control']['permissions'] == ['admin:read', 'agent:runtime-control:'+request['runtimeId']] and
            'agent:runtime-task:'+request['runtimeId'] in keys['task']['permissions'],
            'runtime installation cannot substitute setup or legacy task authority')
    envs = dict(CREDENTIAL_ENVS if credential_envs is None else credential_envs)
    require(set(envs) == set(CREDENTIAL_ENVS) and len(set(envs.values())) == 3,
            'registrar, task and control credential injections must remain distinct')
    secrets = [os.environ.get(envs[kind]) for kind in ('setup', 'control', 'task')]
    require(all(isinstance(value, str) and 32 <= len(value) <= 8192 and
                all(33 <= ord(character) <= 126 for character in value) for value in secrets) and len(set(secrets)) == 3,
            'runtime registrar needs three distinct private injected credentials')
    runtime = request['runtimeId']
    prefix = 'native-runtime-registration:'+canonical_digest({'candidateKey': durable.store.candidate, 'runtimeId': runtime})[7:]
    records = durable.state.setdefault('scenarioOperations', {})
    binding = {'endpoint': endpoint, 'request': request, 'keyReferences': key_references, 'provenance': provenance}
    start = {'producer': PRODUCER, 'candidateKey': durable.store.candidate, 'value': copy.deepcopy(binding)}
    require(prefix+'/start' not in records or records[prefix+'/start'] == start,
            'runtime installation already has a different immutable original declaration')
    # Validate public metadata before retaining it, including all injected keys.
    validation_http = http_factory(endpoint, envs['control'], identity={
        'candidateKey': durable.store.candidate, 'attemptId': 'runtime-register-'+prefix.rsplit(':', 1)[1][:32]},
        authorize=guard, retain=lambda *_: None, private_values=secrets, timeout=120)
    validation_http.check_private_metadata(start)
    if prefix+'/start' not in records:
        records[prefix+'/start'] = start
        durable._persist()

    identity = {'candidateKey': durable.store.candidate, 'attemptId': 'runtime-register-'+prefix.rsplit(':', 1)[1][:32]}
    references = {}
    def retain(kind, step, value):
        if kind == 'intent':
            guard()
        else:
            # Preserve an actual response even when the funding deadline passes
            # during the request. Expiry still prevents subsequent HTTP/activation.
            durable._require_live_lease()
        name = prefix+'/http/'+kind+'/'+step
        require(name not in records, 'original runtime installation request already exists; read without replay')
        if kind == 'response':
            original = records.get(value.get('intentReference', {}).get('key'))
            require(original is not None and canonical_digest(original) == value.get('intentDigest'),
                    'runtime installation response lacks its original durable intent')
        records[name] = copy.deepcopy(value)
        durable._persist()
        return {'key': name, 'sha256': canonical_digest(value)}

    clients = {kind: http_factory(endpoint, env, identity=identity, authorize=guard, retain=retain,
                                 private_values=secrets, timeout=120) for kind, env in envs.items()}
    serial = sum(1 for name in records if name.startswith(prefix+'/http/intent/'))
    def call(kind, name, method, path, body=None):
        nonlocal serial
        step = name if method == 'POST' else name+'-'+str(serial)
        serial += 1
        answer = clients[kind].json(step, method, path, body)
        references[name] = answer['reference']
        require(answer['observation']['httpStatus'] == (201 if method == 'POST' else 200),
                'native runtime installation did not succeed; retain original response without replay')
        return answer['observation']['body']

    def observe_principals(phase):
        principals = {}
        for kind in ('control', 'task'):
            principals[kind] = native_target(call(kind, phase+'-'+kind+'-target', 'GET', '/api/v1/agent/target'),
                                             request['targetId'])
            effective = call(kind, phase+'-'+kind+'-effective', 'GET',
                             '/api/v1/admin/api-keys/'+keys[kind]['apiKeyId']+'/effective-permissions')
            value = effective.get('data', {}) if isinstance(effective, dict) else {}
            require(isinstance(effective, dict) and effective.get('success') is True and value.get('id') == keys[kind]['apiKeyId'] and
                    value.get('status') == 'active' and value.get('canAuthenticate') is True and
                    permission_digest(value.get('permissions')) == permission_digest(keys[kind]['permissions']),
                    'original runtime credential is inactive or its native permissions changed')
        require(principals['control']['tenantId'] == principals['task']['tenantId'] and
                principals['control']['actorId'] != principals['task']['actorId'],
                'runtime installation requires separate current actors in the same native tenant')
        return principals

    principals = observe_principals('before')
    path = '/api/v1/agent/runtimes/'+runtime+'/registration'
    post_intent = prefix+'/http/intent/register'
    if post_intent not in records:
        require(prefix+'/completed' not in records, 'runtime completion lacks the original mutation intent')
        setup = native_target(call('setup', 'setup-target', 'GET', '/api/v1/agent/target'), request['targetId'])
        require(setup['tenantId'] == principals['control']['tenantId'] and
                setup['actorId'] not in {value['actorId'] for value in principals.values()},
                'runtime registrar must be separate actual setup authority in the original tenant')
        registered = validate_registration(call('setup', 'register', 'POST', path, request), request, keys, principals)
    else:
        # The server may have accepted a lost response. Never resend, even if
        # GET now returns missing/forbidden/unavailable or the original expired.
        registered = None
    original = validate_registration(call('control', 'registration', 'GET', path), request, keys, principals)
    require(registered is None or original == registered, 'native registration changed between POST and original control GET')
    after = observe_principals('after')
    require(after == principals, 'native actor or tenant changed during runtime installation')
    final = validate_registration(call('control', 'registration-final', 'GET', path), request, keys, after)
    require(final == original, 'original runtime registration or key versions changed during readback')
    guard()
    result = {'schema': 'honua.private-native-runtime-registration/v1', 'candidateKey': durable.store.candidate,
              'runtimeId': runtime, 'registration': original, 'keyReferences': key_references,
              'declarationDigest': request['configurationDigest'], 'provenance': provenance,
              'nativeOriginals': references, 'activation': False, 'qualification': False}
    clients['control'].check_private_metadata(result)
    completed = {'producer': PRODUCER, 'candidateKey': durable.store.candidate, 'value': copy.deepcopy(result)}
    if prefix+'/completed' in records:
        first = records[prefix+'/completed']
        require(first.get('producer') == PRODUCER and first.get('candidateKey') == durable.store.candidate and
                all(first.get('value', {}).get(name) == value for name, value in result.items() if name != 'nativeOriginals'),
                'runtime installation completion differs from its original native registration')
        # Fresh observations remain appended; the first completion is immutable.
        return copy.deepcopy(first['value'])
    records[prefix+'/completed'] = completed
    durable._persist()
    return result


def provision_reviewed(durable, reference, *, s3, ecs, states, guard, source_reader=None,
                       worker_verifier=None, loader=None, credential_envs=None, http_factory=SetupHttp):
    """Compose the checked-source declaration loader with the native protocol.

    The dedicated registrar's guard verifies its own immutable task/image/key
    injection and original operation reservations. The declaration loader reads
    actual installed-worker receipts through the hosted verifier, not descriptors
    supplied by the person or model requesting an installation.
    """
    if loader is None:
        from agent_e2e_runtime_configuration import load_declaration
        loader = load_declaration
    options = {'durable': durable, 's3': s3, 'ecs': ecs, 'states': states, 'guard': guard}
    if source_reader is not None: options['source_reader'] = source_reader
    if worker_verifier is not None: options['worker_verifier'] = worker_verifier
    verified = loader(reference, **options)
    require(verified.get('schema') == 'honua.verified-installed-console-declaration/v1' and
            verified.get('candidateKey') == durable.store.candidate and
            verified.get('registrationRequest', {}).get('configurationDigest') == verified.get('configurationDigest') and
            all(verified['registrationRequest'].get(name) == verified.get(name) for name in
                ('candidateKey', 'runtimeId', 'targetId', 'originalFundingExpiresEpoch', 'mutationCeilings')),
            'runtime installation requires the verified original controller declaration')
    keys = verified['keys']
    require(set(keys) == {'control', 'task'} and all(set(value) == {'reference', 'issued'} for value in keys.values()),
            'verified runtime declaration key references differ')
    for kind in keys:
        require(key_original(durable, keys[kind]['reference'], verified['registrationRequest'][kind+'ApiKeyId']) == keys[kind]['issued'],
                'verified declaration no longer matches original issued-key custody')
    provenance = {'sourceConfiguration': reference, 'nativeBootstrap': verified['nativeBootstrap'],
                  'fundingBinding': verified['fundingBinding'], 'operation': verified['operation'],
                  'workerProofs': [worker['proof'] for worker in verified['workers']], 'evidence': verified['evidence']}
    return provision(durable, endpoint=verified['endpoint'], request=verified['registrationRequest'],
                     key_references={kind: value['reference'] for kind, value in keys.items()},
                     provenance=provenance, guard=guard, credential_envs=credential_envs, http_factory=http_factory)


if __name__ == '__main__':
    try:
        from native_runtime_registration_carrier import main
        main()
    except Exception:
        print('Private native registration refused; retain original intent and liabilities for reconciliation.', file=sys.stderr)
        sys.exit(1)
