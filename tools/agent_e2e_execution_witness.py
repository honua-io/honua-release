#!/usr/bin/env python3
"""Private read-only projection of original installed-execution evidence.

Human response text is parsed here with the shared Python canonical digest.
It is only a join to independent task/provider/native originals. No browser
read, model narrative or caller-supplied witness can establish execution proof.
"""
from __future__ import annotations

import argparse
import base64
import copy
import json
import re
import sys
from types import SimpleNamespace

from agent_e2e_budget import validate as validate_budget, money
from agent_e2e_campaign import source_file, validate_model_source
from agent_e2e_console_runtime import require, identifier, execution_id as checked_execution_id, digest, validate_execution_history, STATES
from agent_e2e_console_loop import original_messages, provider_message, final_text
from agent_e2e_controller import BedrockModel
from agent_e2e_interactive_budget import validate_retained_funding
from agent_e2e_pack import canonical_digest, digest_bytes, load_json
from agent_e2e_semantics import parse_response, validate_response
from agent_e2e_worker_lifecycle import WorkerLifecycle

SCHEMA = 'honua.workspace-execution-witness/v1'
MAX_OBJECT = 64 * 1024 * 1024


def human_context_digest(response_text):
    require(isinstance(response_text, str) and 0 < len(response_text.encode()) <= 1024*1024,
            'original human turn response text missing or oversized')
    body = parse_response(response_text.encode())
    require(isinstance(body, dict) and isinstance(body.get('context'), dict), 'original human turn context missing')
    return canonical_digest(body['context'])


def object_bytes(s3, reference, bucket):
    require(isinstance(reference, dict) and set(reference) == {'bucket', 'key', 'versionId', 'sha256'} and
            reference['bucket'] == bucket and isinstance(reference['key'], str) and
            isinstance(reference['versionId'], str) and reference['versionId'] not in ('', 'null'),
            'immutable private evidence reference missing')
    digest(reference['sha256'])
    response = s3.get_object(Bucket=bucket, Key=reference['key'], VersionId=reference['versionId'])
    require(response.get('VersionId') == reference['versionId'] and not response.get('DeleteMarker') and
            type(response.get('ContentLength')) is int and 0 < response['ContentLength'] <= MAX_OBJECT,
            'private evidence object identity/size differs')
    raw = response['Body'].read(MAX_OBJECT+1)
    require(type(raw) is bytes and len(raw) == response['ContentLength'] and
            digest_bytes(raw) == reference['sha256'], 'private evidence original bytes differ')
    return raw


def original_http(records, reference, *, path, endpoint, candidate, runtime=None, execution=None):
    require(isinstance(reference, dict) and set(reference) == {'key', 'sha256'}, 'original HTTP reference missing')
    row = records.get(reference['key'])
    require(isinstance(row, dict) and canonical_digest(row) == reference['sha256'], 'original HTTP response missing or changed')
    intent_ref = row.get('intentReference', {})
    intent = records.get(intent_ref.get('key'))
    require(isinstance(intent, dict) and canonical_digest(intent) == intent_ref.get('sha256') == row.get('intentDigest') and
            intent.get('method') == 'GET' and intent.get('credentialKind') == 'api-key' and
            intent.get('candidateKey') == candidate and intent.get('endpoint') == endpoint and intent.get('path') == path,
            'original HTTP intent is not the exact authenticated native read')
    if runtime is not None:
        require(intent.get('runtimeId') == runtime and intent.get('executionId') == execution,
                'native original belongs to another installed execution')
    validate_response(row['response'])
    require(row['response']['httpStatus'] == 200, 'original native read did not succeed')
    return row['response']['body']


def _worker(events, native, registration, record):
    bindings = events('worker-binding-observation')
    observations = events('worker-observation')
    installs = events('worker-installation-observation')
    launches = events('worker-intent')
    require(len(bindings) == len(launches) == len(installs) == 1 and observations,
            'original native task acknowledgement/worker installation/launch is missing')
    binding = native(bindings[0]['reference']).get('workerBinding')
    require(isinstance(binding, dict) and binding.get('schemaVersion') == 1 and binding.get('state') == 'running' and
            all(binding.get(k) == record[k] for k in ('candidateKey', 'runtimeId', 'executionId', 'workspaceId', 'turnId')),
            'native task acknowledgement belongs to another execution')
    acknowledgement = binding.get('acknowledgement', {})
    principal = registration['taskPrincipal']
    require(set(acknowledgement) == {'actorId', 'credentialVersion'} and
            all(acknowledgement[k] == principal[k] for k in acknowledgement),
            'worker context was not acknowledged by the actual registered task credential')
    require(isinstance(binding.get('context'), dict) and binding['context'].get('workspaceId') == record['workspaceId'] and
            canonical_digest(binding['context']) == binding.get('contextDigest'),
            'task-authenticated native context bytes differ from their acknowledged digest')
    slot = [row for row in registration['slots'] if row['slotId'] == binding.get('slotId')]
    require(len(slot) == 1, 'worker acknowledgement has no original registered physical slot')
    slot = slot[0]
    launch = launches[0]
    supplied = launch['input']
    require(launch['inputDigest'] == canonical_digest(supplied) and supplied['nativeBindingDigest'] == binding['nativeBindingDigest'] and
            supplied['claimGeneration'] == binding['claimGeneration'] and supplied['expiresEpoch'] == binding['expiresEpoch'] and
            all(supplied.get(k) == binding[k] for k in ('candidateKey', 'runtimeId', 'executionId', 'workspaceId', 'turnId', 'slotId')),
            'worker acknowledgement changed the original immutable launch input')
    observed = observations[-1]
    aws, physical = observed['execution'], observed['observation']
    task = physical.get('physicalTask', {})
    require(aws.get('executionArn') == launch['executionArn'] and aws.get('inputDetails', {}).get('included') is True and
            aws.get('redriveCount', 0) == 0 and json.loads(aws.get('input', 'null')) == supplied and
            physical.get('taskArn') == binding.get('workerId') == task.get('taskArn') and
            task.get('clusterArn') == slot['clusterArn'] and task.get('taskDefinitionArn') == slot['taskDefinitionArn'] and
            task.get('launchType') == 'FARGATE' and task.get('lastStatus') in
                ('RUNNING', 'DEACTIVATING', 'STOPPING', 'DEPROVISIONING', 'STOPPED'),
            'original AWS worker readback does not corroborate native task binding')
    response = physical.get('taskResponse', {})
    require(not response.get('failures') and response.get('tasks') == [task], 'original physical task response is ambiguous')
    pages = copy.deepcopy(observed.get('launchHistory', []))
    require(pages and len(pages) <= 8, 'original Step Functions launch history missing')
    class History:
        def get_execution_history(self, **request):
            require(pages and pages[0]['request'] == request, 'original launch history request or pagination differs')
            return pages.pop(0)['response']
    reader = SimpleNamespace(states=History(), execution_arn=launch['executionArn'],
        task_prefix=slot['clusterArn'].replace(':cluster/', ':task/')+'/')
    arn, event = WorkerLifecycle._launched_task(reader)
    require(not pages and arn == binding['workerId'] and event == physical['launchEventId'],
            'original physical task was not the single actual RunWorker result')
    task_definition = installs[0]['taskDefinition'].get('taskDefinition', {})
    containers = task_definition.get('containerDefinitions', [])
    require(task_definition.get('taskDefinitionArn') == slot['taskDefinitionArn'] and
            task_definition.get('taskRoleArn') == slot['taskRoleArn'] and len(containers) == 1 and
            containers[0].get('image') == slot['image'] and containers[0].get('secrets') == [{
                'name': 'HONUA_AGENT_MCP_CONFIG', 'valueFrom': slot['mcpSecretArn']+':::'+slot['mcpSecretVersionId']}],
            'actual worker task role/image/immutable MCP readback differs')
    tags = task.get('tags', [])
    wanted = {'candidate': record['candidateKey'], 'attempt': slot['slotId'], 'runtime': record['runtimeId'],
              'execution': record['executionId']}
    require(all([row.get('value') for row in tags if row.get('key') == 'honua-agent-'+key] == [value]
                for key, value in wanted.items()), 'original physical task tags differ')
    return {'taskArn': arn, 'slotId': slot['slotId'], 'claimGeneration': binding['claimGeneration'],
        'contextDigest': digest(binding['contextDigest']), 'taskPrincipal': copy.deepcopy(acknowledgement)}, bindings[0]['reference']


def project_execution(reference, *, s3, candidate_key, runtime_id, workspace_id, turn_id, execution_id,
                      registration_digest, human_turn_response_text=None, source_reader=source_file):
    digest(candidate_key); digest(registration_digest)
    identifier(runtime_id, 'runtime ID'); identifier(workspace_id, 'workspace ID'); identifier(turn_id, 'turn ID')
    original_id = checked_execution_id(execution_id)
    human = None
    if human_turn_response_text is not None:
        require(isinstance(human_turn_response_text, str) and len(human_turn_response_text.encode()) <= 1024*1024,
                'original human turn response text missing or oversized')
        human = parse_response(human_turn_response_text.encode())
        require(isinstance(human, dict) and human.get('turnId') == turn_id and
                human.get('execution', {}).get('executionId') == original_id and isinstance(human.get('context'), dict),
                'original human response does not identify the accepted turn/execution')
    prefix = 'agent-e2e/'+candidate_key[7:]+'/'
    require(isinstance(reference, dict) and isinstance(reference.get('key'), str) and re.fullmatch(
            re.escape(prefix)+r'[1-9][0-9]*-[0-9a-f-]{36}\.json', reference['key']),
            'witness source must be an original candidate snapshot')
    snapshot = parse_response(object_bytes(s3, reference, reference['bucket']))
    require(set(snapshot) == {'candidateKey', 'version', 'generationDigest', 'payload'} and
            snapshot['candidateKey'] == candidate_key and type(snapshot['version']) is int and snapshot['version'] > 0,
            'original snapshot identity differs')
    digest(snapshot['generationDigest'])
    state = snapshot['payload']
    validate_retained_funding(state)
    validate_budget(state['ledger'], allow_overrun=True)
    require(state['ledger']['candidateKey'] == candidate_key, 'snapshot funding belongs to another candidate')
    record = state.get('interactiveExecutions', {}).get(original_id)
    validate_execution_history(record)
    require(all(record[k] == v for k, v in {'candidateKey': candidate_key, 'runtimeId': runtime_id,
        'workspaceId': workspace_id, 'turnId': turn_id, 'runtimeBindingDigest': registration_digest}.items()),
        'original journal identity differs from human/native execution')
    events = lambda kind: [row['value'] for row in record['events'] if row['kind'] == kind]
    path = '/api/v1/agent/runtimes/'+runtime_id+'/executions/'+original_id+'?workspaceId='+workspace_id+'&turnId='+turn_id
    def native(ref, *, effects=False):
        selected = path.replace('?workspaceId=', '/effects?workspaceId=') if effects else path
        body = original_http(state.get('nativeRuntimeHttp', {}), ref, path=selected, endpoint=record['endpoint'],
            candidate=candidate_key, runtime=runtime_id, execution=original_id)
        require(isinstance(body, dict) and body.get('schemaVersion') == 1 and body.get('candidateKey') == candidate_key and
                body.get('runtimeId') == runtime_id and body.get('targetId') == record['targetId'] and
                body.get('requestDigest') == record['requestDigest'], 'original native execution/effect identity changed')
        return body
    original = native(record['nativeOriginal'])
    turn = original['binding']['turn']
    context_digest = canonical_digest(turn['context'])
    submission = original.get('submission', {})
    require(isinstance(turn.get('actorId'), str) and turn['actorId'] and submission.get('disposition') == 'accepted' and
            submission.get('executionId') == original_id and submission.get('turnId') == turn_id,
            'original native human turn/submission identity missing')
    if human is not None:
        require(human_context_digest(human_turn_response_text) == context_digest and human.get('originatingActorId') == turn['actorId'] and
                human.get('submission') == submission, 'human response changed original context/author/submission')
    digest(submission.get('requestDigest'))
    registrations = state.get('scenarioOperations', {})
    registration_key = 'native-runtime-registration:'+canonical_digest({'candidateKey': candidate_key, 'runtimeId': runtime_id})[7:]+'/completed'
    retained = registrations.get(registration_key, {})
    installation = retained.get('value', {})
    require(retained.get('producer') == 'private-native-runtime-registration' and retained.get('candidateKey') == candidate_key and
            installation.get('schema') == 'honua.private-native-runtime-registration/v1' and
            installation.get('activation') is False and installation.get('qualification') is False,
            'original native runtime installation custody missing')
    registration = original_http(registrations, installation['nativeOriginals']['registration-final'],
        path='/api/v1/agent/runtimes/'+runtime_id+'/registration', endpoint=record['endpoint'], candidate=candidate_key)
    require(registration == installation.get('registration') and registration.get('registrationDigest') == registration_digest and
            registration.get('candidateKey') == candidate_key and registration.get('runtimeId') == runtime_id and
            registration.get('targetId') == record['targetId'],
            'actual original runtime registration differs')
    source = installation['provenance']['sourceConfiguration']
    require(isinstance(source, dict) and set(source) == {'path', 'sha256'} and
            source['path'] == 'certification/agent-e2e/installed-runtimes/'+runtime_id+'.json',
            'original runtime declaration source missing')
    declaration = load_json(source_reader(source['path'], source['sha256']))
    require(declaration.get('schema') == 'honua.installed-console-declaration/v1' and
            canonical_digest(declaration) == registration['configurationDigest'] and declaration['candidateKey'] == candidate_key and
            declaration['runtimeId'] == runtime_id and declaration['targetId'] == record['targetId'] and
            declaration['endpoint'] == record['endpoint'] and declaration['fundingBinding'] == state['ledger']['interactiveAccounting'] and
            registration['originalFundingExpiresEpoch'] == declaration['fundingBinding']['originalExpiresEpoch'] and
            declaration['evidence']['bucket'] == reference['bucket'] and
            installation['provenance'].get('evidence') == declaration['evidence'],
            'reviewed declaration differs from actual registered configuration/private custody')
    model_config = declaration['model']
    validate_model_source(model_config, source_reader=source_reader)
    model = BedrockModel(model_config, client=object())
    worker, worker_ref = _worker(events, native, registration, record)
    require(worker['contextDigest'] == context_digest, 'task-authenticated worker context differs from original human context')
    loop = events('loop-binding')
    require(loop == [{'modelConfigurationDigest': canonical_digest(model_config), 'operationPolicy': declaration['operationPolicy'],
                     'originalRequestDigest': record['requestDigest']}], 'actual invocation model/operation policy differs')
    requests, responses, providers = events('model-intent'), events('model-response'), events('provider-response')
    require(requests and len(requests) == len(responses) == len(providers), 'original provider outcome is missing or unknown')
    journal = SimpleNamespace(record=record, durable=SimpleNamespace(state=state))
    original_conversation = original_messages(journal, turn)
    rounds = []
    for ordinal, (request, response, provider) in enumerate(zip(requests, responses, providers)):
        raw = base64.b64decode(request['requestBodyBase64'], validate=True)
        body = parse_response(raw)
        expected_request = model.request(body['messages'])
        expected_request['tools'] = [tool for tool in expected_request['tools'] if tool['name'] in declaration['operationPolicy']['allowedActions']]
        require(request['round'] == response['round'] == provider['round'] == ordinal and
                digest_bytes(raw) == request['requestDigest'] == provider['provider']['requestDigest'] and
                response['provider'] == provider['provider'] and body == expected_request,
                'original provider request/response/model policy linkage changed')
        if ordinal == 0:
            require(body['messages'] == original_conversation, 'actual model request did not contain the original human turn/context')
        message, cost = provider_message(provider['provider'], model)
        charges = [row for row in state['ledger']['interactiveEntries'] if row['id'] == request['reservationId']]
        require(message == response['message'] and money(response['costUsd']) == cost and len(charges) == 1 and
                charges[0]['executionId'] == original_id and charges[0]['kind'] == 'model' and charges[0]['state'] == 'settled' and
                money(charges[0]['actualUsd']) == cost and charges[0]['basisDigest'] == model_config['pricingDigest'],
                'original provider usage has no matching settled actual execution charge')
        rounds.append({'round': ordinal, 'requestDigest': request['requestDigest'],
            'providerRequestId': provider['provider']['providerRequestId'], 'responseDigest': provider['provider']['responseDigest'],
            'requestedModelId': provider['provider']['requestedModelId'], 'returnedModelId': message['model'],
            'usage': {'inputTokens': message['usage']['input_tokens'], 'outputTokens': message['usage']['output_tokens']},
            'pricingDigest': model_config['pricingDigest'], 'costUsd': str(cost)})
    effect_rows = events('effect-observation')
    require(effect_rows, 'independent canonical native effect read missing')
    effects = native(effect_rows[-1]['reference'], effects=True)
    latest = native(effect_rows[-1]['executionReference'])
    execution = latest['execution']
    require(execution.get('executionId') == original_id and execution.get('state') in STATES and
            type(execution.get('stateRevision')) is int and execution['stateRevision'] >= 0 and
            (human is None or execution == human['execution']) and effects.get('effects') == {'operations': execution.get('operations', []),
                'artifacts': execution.get('artifacts', [])}, 'native effects/current revision differ from original human read')
    public_native = {k: copy.deepcopy(execution[k]) for k in ('state', 'stateRevision', 'operations', 'artifacts', 'finalMessageId') if k in execution}
    finals = events('final-response-observation')
    proof = {'nativeOriginal': record['nativeOriginal'], 'workerBindingOriginal': worker_ref,
        'workerObservationEventDigest': [row['digest'] for row in record['events'] if row['kind'] == 'worker-observation'][-1],
        'providerEventDigests': [row['digest'] for row in record['events'] if row['kind'] in ('model-intent', 'provider-response')],
        'effectOriginals': [effect_rows[-1]['reference'], effect_rows[-1]['executionReference']]}
    if execution.get('finalMessageId') is not None:
        require(len(finals) == 1, 'original final response readback missing')
        final = native(finals[0]['reference'])
        require(finals[0]['messageId'] == execution['finalMessageId'] == final.get('finalMessage', {}).get('id') and
                final['finalMessage'] == {'id': finals[0]['messageId'], 'turnId': turn_id, 'role': 'assistant',
                    'content': final_text(responses[-1]['message']), 'evidenceKind': 'conversation'},
                'original final conversation identity/content differs from the genuine provider response')
        proof['finalMessageOriginal'] = finals[0]['reference']
    return {'schema': SCHEMA, **{k: record[k] for k in ('candidateKey', 'runtimeId', 'executionId', 'workspaceId', 'turnId', 'targetId')},
        'registrationDigest': registration_digest, 'requestDigest': record['requestDigest'], 'sourceSnapshot': copy.deepcopy(reference),
        'stateVersion': snapshot['version'], 'generationDigest': snapshot['generationDigest'], 'journalDigest': canonical_digest(record),
        'humanTurn': {'contextDigest': context_digest, 'submissionRequestDigest': submission['requestDigest'],
            'originatingActorId': turn['actorId']}, 'worker': worker, 'modelRounds': rounds, 'native': public_native,
        'proof': proof, 'qualification': False}


def publish_execution(*, durable, journal, s3, bucket, guard, source_reader=source_file):
    """Export once from the original state head; lost PUT acknowledgement reads only."""
    require(callable(guard) and bucket == durable.store.bucket, 'original private witness bucket/guard missing')
    guard(); durable._require_live_lease()
    record = journal.record
    if record['events'] and record['events'][-1]['kind'] == 'witness-export-observation':
        return copy.deepcopy(record['events'][-1]['value']['reference'])
    intents = journal.values('witness-export-intent')
    observations = journal.values('witness-export-observation')
    require(len(intents) in (len(observations), len(observations)+1), 'original witness export history is ambiguous')
    recovering = len(intents) > len(observations)
    if recovering:
        intent = intents[-1]
        source = intent['sourceSnapshot']
    else:
        store = durable.store
        head = store.db.get_item(TableName=store.table, Key=store.key, ConsistentRead=True).get('Item', {})
        require(head.get('leaseOwner', {}).get('S') == store.owner and head.get('fence') == {'N': str(store.fence)} and
                int(head.get('leaseUntil', {'N': '0'})['N']) >= store.clock() and
                head.get('stateVersion') == {'N': str(store.version)} and
                head.get('generationDigest') == {'S': store.generation['digest']},
                'private witness source is not the original fenced candidate head')
        source = {'bucket': bucket, 'key': head['snapshotKey']['S'], 'versionId': head['snapshotVersion']['S'],
                  'sha256': head['snapshotDigest']['S']}
        state = parse_response(object_bytes(s3, source, bucket))
        require(state['payload'] == durable.state and state['version'] == store.version,
                'private witness source has uncommitted or stale evidence')
    proof = project_execution(source, s3=s3, candidate_key=record['candidateKey'], runtime_id=record['runtimeId'],
        workspace_id=record['workspaceId'], turn_id=record['turnId'], execution_id=record['executionId'],
        registration_digest=record['runtimeBindingDigest'], source_reader=source_reader)
    raw = json.dumps(proof, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    expected = {'sourceSnapshot': source, 'sha256': digest_bytes(raw),
        'key': witness_prefix(record['candidateKey'], record['runtimeId'], record['executionId'])+
            str(proof['stateVersion'])+'-'+digest_bytes(raw)[7:]+'.json'}
    if recovering:
        require(intent == expected, 'original witness export intent changed')
    else:
        journal.append('witness-export-intent', expected)
        guard(); durable._require_live_lease()
        try:
            response = s3.put_object(Bucket=bucket, Key=expected['key'], Body=raw,
                ContentType='application/json', IfNoneMatch='*')
            require(response.get('VersionId') not in (None, '', 'null'), 'private witness export was not versioned')
        except Exception:
            # The durable intent already precedes the API call. A failed or lost
            # response only permits exact original readback, never another PUT.
            pass
    durable._require_live_lease()
    observed = s3.get_object(Bucket=bucket, Key=expected['key'])
    require(observed.get('VersionId') not in (None, '', 'null') and not observed.get('DeleteMarker') and
            observed.get('ContentLength') == len(raw) and observed['Body'].read(MAX_OBJECT+1) == raw,
            'original private witness PUT outcome remains unknown')
    reference = {'bucket': bucket, 'key': expected['key'], 'versionId': observed['VersionId'], 'sha256': expected['sha256']}
    journal.append('witness-export-observation', {'sourceSnapshot': source, 'reference': reference})
    return reference


def witness_prefix(candidate_key, runtime_id, execution):
    digest(candidate_key); identifier(runtime_id, 'runtime ID'); checked_execution_id(execution)
    return 'agent-e2e/'+candidate_key[7:]+'/execution-witnesses/'+runtime_id+'/'+execution+'/'


def resolve_reference(s3, *, bucket, candidate_key, runtime_id, execution):
    """Bounded private version discovery; never accept an overwritten witness."""
    prefix = witness_prefix(candidate_key, runtime_id, execution)
    response = s3.list_object_versions(Bucket=bucket, Prefix=prefix, MaxKeys=1000)
    require(not response.get('IsTruncated') and not response.get('DeleteMarkers'),
            'private witness discovery is truncated or contains a deletion')
    versions = response.get('Versions', [])
    require(isinstance(versions, list) and versions, 'original private execution witness has not been exported')
    seen, choices = set(), []
    for row in versions:
        match = re.fullmatch(re.escape(prefix)+r'([1-9][0-9]*)-([0-9a-f]{64})\.json', str(row.get('Key')))
        require(match is not None and row['Key'] not in seen and row.get('IsLatest') is True and
                row.get('VersionId') not in (None, '', 'null'), 'private witness key is ambiguous or was overwritten')
        seen.add(row['Key'])
        choices.append((int(match[1]), {'bucket': bucket, 'key': row['Key'], 'versionId': row['VersionId'],
            'sha256': 'sha256:'+match[2]}))
    greatest = max(row[0] for row in choices)
    selected = [ref for version, ref in choices if version == greatest]
    require(len(selected) == 1, 'multiple private witnesses claim the same original state version')
    return selected[0]


def read_witness(reference, *, s3, bucket, candidate_key, runtime_id, workspace_id, turn_id,
                 execution_id, registration_digest, human_turn_response_text, source_reader=source_file):
    prefix = witness_prefix(candidate_key, runtime_id, execution_id)
    require(isinstance(reference, dict), 'immutable private witness reference missing')
    match = re.fullmatch(re.escape(prefix)+r'([1-9][0-9]*)-([0-9a-f]{64})\.json', str(reference.get('key')))
    require(match is not None and reference.get('sha256') == 'sha256:'+match[2], 'private witness reference differs from its exact bytes key')
    retained = parse_response(object_bytes(s3, reference, bucket))
    require(isinstance(retained, dict) and retained.get('schema') == SCHEMA and retained.get('qualification') is False and
            retained.get('stateVersion') == int(match[1]), 'original private witness schema/state differs')
    projected = project_execution(retained['sourceSnapshot'], s3=s3, candidate_key=candidate_key,
        runtime_id=runtime_id, workspace_id=workspace_id, turn_id=turn_id, execution_id=execution_id,
        registration_digest=registration_digest, human_turn_response_text=human_turn_response_text, source_reader=source_reader)
    require(retained == projected, 'retained private witness no longer matches independently replayed originals')
    return {'reference': copy.deepcopy(reference), 'witness': projected}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)  # Fixed read-only entry: no URLs, commands or source paths in arguments.
    raw = sys.stdin.buffer.read(2*1024*1024+1)
    require(len(raw) <= 2*1024*1024, 'private witness reader input exceeds bound')
    request = parse_response(raw)
    fields = {'schemaVersion', 'evidence', 'candidateKey', 'runtimeId', 'executionId', 'workspaceId', 'turnId',
              'registrationDigest', 'humanTurnResponseText'}
    require(isinstance(request, dict) and set(request) in (fields, fields | {'reference'}) and request['schemaVersion'] == 1 and
            isinstance(request['humanTurnResponseText'], str) and 0 < len(request['humanTurnResponseText'].encode()) <= 1024*1024 and
            isinstance(request['evidence'], dict) and set(request['evidence']) == {'region', 'bucket'} and
            re.fullmatch(r'[a-z]{2}-[a-z]+-[0-9]', str(request['evidence']['region'])) and
            re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', str(request['evidence']['bucket'])),
            'invalid private witness reader binding')
    import boto3
    from botocore.config import Config
    s3 = boto3.client('s3', region_name=request['evidence']['region'],
        config=Config(connect_timeout=5, read_timeout=15, retries={'total_max_attempts': 1}))
    reference = request.get('reference')
    if reference is None:
        reference = resolve_reference(s3, bucket=request['evidence']['bucket'], candidate_key=request['candidateKey'],
            runtime_id=request['runtimeId'], execution=request['executionId'])
    result = read_witness(reference, s3=s3, bucket=request['evidence']['bucket'], candidate_key=request['candidateKey'],
        runtime_id=request['runtimeId'], workspace_id=request['workspaceId'], turn_id=request['turnId'],
        execution_id=request['executionId'], registration_digest=request['registrationDigest'],
        human_turn_response_text=request['humanTurnResponseText'])
    output = json.dumps(result, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
    require(len(output) <= 1024*1024, 'private execution projection exceeds bound')
    sys.stdout.buffer.write(output+b'\n')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Private execution witness unavailable; original evidence and liabilities remain unresolved.', file=sys.stderr)
        sys.exit(1)
