"""Export private browser inputs from an existing authenticated installation.

This reader never acquires a lease, creates credentials, registers a runtime,
starts a worker, quotes a model, or changes accounting. The binding is an input
to installed browser acceptance, not evidence that a human journey passed.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from types import SimpleNamespace
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'tools'), str(ROOT/'tools/hosted_pilot')]

from accounting_snapshot import HEAD, observe
from agent_e2e_campaign import source_file
from agent_e2e_pack import canonical_digest, digest_bytes, load_json
from agent_e2e_runtime_configuration import _verified_declaration
from agent_e2e_semantics import validate_response
from agent_e2e_state import StateStore
from controller_archive import require
from native_runtime_registration import PRODUCER, key_original, native_target, permission_digest, validate_registration


def registered_runtime(durable, verified):
    """Join native response bytes and original intents to the checked declaration."""
    candidate, runtime = durable.store.candidate, verified['runtimeId']
    request = verified['registrationRequest']
    prefix = 'native-runtime-registration:'+canonical_digest({'candidateKey': candidate, 'runtimeId': runtime})[7:]
    records = durable.state.get('scenarioOperations', {})
    keys = {kind: key_original(durable, value['reference'], request[kind+'ApiKeyId'])
            for kind, value in verified['keys'].items()}
    require(set(keys) == {'control', 'task'} and
            all(keys[kind] == verified['keys'][kind]['issued'] for kind in keys),
            'installed binding issued-key originals differ')
    references = {kind: value['reference'] for kind, value in verified['keys'].items()}
    provenance = {'sourceConfiguration': verified['declarationReference'], 'nativeBootstrap': verified['nativeBootstrap'],
        'fundingBinding': verified['fundingBinding'], 'operation': verified['operation'],
        'workerProofs': [worker['proof'] for worker in verified['workers']], 'evidence': verified['evidence']}
    start = {'producer': PRODUCER, 'candidateKey': candidate, 'value': {
        'endpoint': verified['endpoint'], 'request': request, 'keyReferences': references, 'provenance': provenance}}
    require(records.get(prefix+'/start') == start, 'installed binding has no matching original registration intent')
    completed = records.get(prefix+'/completed')
    require(isinstance(completed, dict) and set(completed) == {'producer', 'candidateKey', 'value'} and
            completed['producer'] == PRODUCER and completed['candidateKey'] == candidate,
            'installed binding requires completed native registration')
    value = completed['value']
    require(isinstance(value, dict) and set(value) == {'schema', 'candidateKey', 'runtimeId', 'registration',
            'keyReferences', 'declarationDigest', 'provenance', 'nativeOriginals', 'activation', 'qualification'} and
            value['schema'] == 'honua.private-native-runtime-registration/v1' and
            value['candidateKey'] == candidate and value['runtimeId'] == runtime and
            value['keyReferences'] == references and value['declarationDigest'] == verified['configurationDigest'] and
            value['provenance'] == provenance and value['activation'] is False and value['qualification'] is False,
            'installed binding completion differs from checked original custody')
    originals = value['nativeOriginals']
    required = {phase+'-'+kind+'-'+observation for phase in ('before', 'after')
                for kind in keys for observation in ('target', 'effective')} | {'registration', 'registration-final'}
    require(isinstance(originals, dict) and required <= set(originals) <= required | {'setup-target', 'register'},
            'installed binding native original response set differs')
    identity = {'schemaVersion': 1, 'candidateKey': candidate,
                'attemptId': 'runtime-register-'+prefix.rsplit(':', 1)[1][:32]}
    path = '/api/v1/agent/runtimes/'+runtime+'/registration'

    def intent(step, method, route):
        return {**identity, 'step': step, 'endpoint': verified['endpoint'], 'method': method,
            'path': route, 'credentialKind': 'api-key', 'contentType': 'application/json',
            'requestBodyDigest': digest_bytes(b'' if method == 'GET' else
                json.dumps(request, allow_nan=False, separators=(',', ':')).encode())}

    def response(name, route, *, method='GET', reference=None):
        ref = originals[name] if reference is None else reference
        require(isinstance(ref, dict) and set(ref) == {'key', 'sha256'} and isinstance(ref['key'], str) and
                ref['key'].startswith(prefix+'/http/response/'), 'installed binding response reference is outside original runtime')
        row = records.get(ref['key'])
        require(isinstance(row, dict) and canonical_digest(row) == ref['sha256'] and
                set(row) == set(identity) | {'step', 'intentDigest', 'intentReference', 'response'} and
                all(row[field] == expected for field, expected in identity.items()),
                'installed binding native response digest or scope differs')
        step = row['step']
        require(isinstance(step, str) and (step == 'register' if method == 'POST' else
                re.fullmatch(re.escape(name)+r'-[0-9]+', step)) and ref['key'] == prefix+'/http/response/'+step,
                'installed binding native response step differs')
        expected_intent = intent(step, method, route)
        intent_key = prefix+'/http/intent/'+step
        require(records.get(intent_key) == expected_intent and row['intentDigest'] == canonical_digest(expected_intent) and
                row['intentReference'] == {'key': intent_key, 'sha256': canonical_digest(expected_intent)},
                'installed binding native response lacks its original request')
        observed = row['response']
        validate_response(observed)
        require(observed['httpStatus'] == (201 if method == 'POST' else 200),
                'installed binding native request did not succeed')
        return observed['body']

    require(records.get(prefix+'/http/intent/register') == intent('register', 'POST', path),
            'installed binding lacks original registration POST intent')
    principals = {}
    for phase in ('before', 'after'):
        principals[phase] = {}
        for kind in keys:
            # This is the public UUID identifying an issued principal, never its
            # credential. Only that canonical identifier belongs in a retained
            # request path and its byte-integrity digest.
            principal_id = str(UUID(keys[kind]['apiKeyId']))
            require(principal_id == keys[kind]['apiKeyId'], 'installed binding principal ID is not canonical')
            principals[phase][kind] = native_target(response(phase+'-'+kind+'-target', '/api/v1/agent/target'),
                                                     verified['targetId'])
            effective = response(phase+'-'+kind+'-effective',
                                 '/api/v1/admin/api-keys/'+principal_id+'/effective-permissions')
            data = effective.get('data', {}) if isinstance(effective, dict) else {}
            require(isinstance(effective, dict) and effective.get('success') is True and
                    data.get('id') == keys[kind]['apiKeyId'] and data.get('status') == 'active' and
                    data.get('canAuthenticate') is True and
                    permission_digest(data.get('permissions')) == permission_digest(keys[kind]['permissions']),
                    'installed binding native effective authority differs')
    require(principals['before'] == principals['after'], 'installed binding native principal changed during registration')
    registration = validate_registration(value['registration'], request, keys, principals['before'])
    for name in ('registration', 'registration-final'):
        require(response(name, path) == registration, 'installed binding original registration readback differs')
    if 'register' in originals:
        require(response('register', path, method='POST') == registration, 'installed binding original POST response differs')
    # A lost POST acknowledgement can complete from GETs, but its original setup
    # identity was observed and durably retained before the mutation was sent.
    setup_rows = [(key, row) for key, row in records.items() if key.startswith(prefix+'/http/response/setup-target-')]
    require(len(setup_rows) == 1, 'installed binding lacks the original separate registrar identity')
    key, row = setup_rows[0]
    setup = native_target(response('setup-target', '/api/v1/agent/target',
        reference={'key': key, 'sha256': canonical_digest(row)}), verified['targetId'])
    require(setup['tenantId'] == registration['tenantId'] and
            setup['actorId'] not in {principal['actorId'] for principal in principals['before'].values()},
            'installed binding registrar is not separate original tenant authority')
    return copy.deepcopy(registration)


def source_guard(commit):
    require(isinstance(commit, str) and re.fullmatch(r'[a-f0-9]{40}', commit), 'binding requires exact release source commit')
    require(not any(name.startswith('GIT_') for name in os.environ), 'binding refuses Git environment overrides')
    actual = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    dirty = subprocess.check_output(['git', '-C', str(ROOT), 'status', '--porcelain', '--untracked-files=all', '--',
        'tools', 'certification/agent-e2e', 'certification/hosted-pilot'], text=True)
    require(actual == commit and not dirty, 'binding requires unchanged checked release tools and declarations')


def export_binding(reference, *, store, s3, ecs, states, release_commit):
    source_guard(release_commit)
    payload, _generation, original_head = observe(store, include_head=True)
    # This view intentionally has no lease/persist methods. Snapshot authentication
    # is a different read-only authority; it must never impersonate a live lease.
    snapshot = SimpleNamespace(store=store, state=payload)

    def guard():
        source_guard(release_commit)
        row = store.db.get_item(TableName=store.table, Key=store.key, ConsistentRead=True).get('Item', {})
        require({field: row.get(field) for field in HEAD} == original_head,
                'accounting head changed during installed binding observation; repeat without replacing state')
        expiry = payload.get('ledger', {}).get('interactiveAccounting', {}).get('originalExpiresEpoch')
        require(type(expiry) is int and time.time() < expiry, 'original installed funding expired')

    guard()
    verified = _verified_declaration(reference, durable=snapshot, s3=s3, ecs=ecs, states=states, guard=guard)
    registration = registered_runtime(snapshot, verified)
    guard()
    return {'schemaVersion': 1, 'candidateKey': store.candidate, 'runtimeId': verified['runtimeId'],
            'registrationDigest': registration['registrationDigest'], 'evidence': verified['evidence'],
            'release': {'root': str(ROOT), 'commit': release_commit}}


def main():
    import argparse
    import boto3
    from controller_handoff import validate_configuration
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--configuration', required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--release-sha', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source_guard(args.release_sha)
    reference = {'path': args.configuration, 'sha256': args.sha256}
    require(re.fullmatch(r'certification/agent-e2e/installed-runtimes/[A-Za-z0-9_-]{1,128}\.json', args.configuration),
            'binding declaration must be a checked installed-runtime source')
    declaration = load_json(source_file(args.configuration, args.sha256))
    controller_ref = declaration['controller']
    require(controller_ref['path'] == 'certification/agent-e2e/controller-handoff.json', 'binding controller source differs')
    controller = load_json(source_file(controller_ref['path'], controller_ref['sha256']))
    validate_configuration(controller)
    budget = controller['dispatchBudget']
    session = boto3.Session(region_name=controller['region'])
    s3, db = session.client('s3'), session.client('dynamodb')
    store = StateStore(s3=s3, dynamodb=db, bucket=budget['state']['bucket'], table=budget['state']['table'],
                       candidate_key=budget['candidateKey'])
    binding = export_binding(reference, store=store, s3=s3, ecs=session.client('ecs'), states=session.client('stepfunctions'),
                             release_commit=args.release_sha)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(binding, stream, sort_keys=True, allow_nan=False)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Installed evaluation binding refused; retain original installation/accounting for diagnosis.', file=sys.stderr)
        sys.exit(1)
