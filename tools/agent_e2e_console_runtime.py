"""Installed private controller for original native Console executions.

The control credential never reaches the model worker. Discovery is not an
admission; every paid effect must bind the same native E, original funding and
one isolated physical worker. Static campaign case configuration is separate.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import os
import re
import time
from urllib.parse import quote

from agent_e2e_budget import validate as validate_budget
from agent_e2e_interactive_budget import validate_retained_funding
from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_setup_http import SetupHttp
from agent_e2e_execution_protocol import require, identifier, execution_id, digest

CONTROL_ENV = 'HONUA_EXTERNAL_EXECUTION_CONTROL_KEY'
STATES = {'queued', 'claimed', 'running', 'awaiting-input', 'awaiting-approval', 'interrupted', 'completed', 'failed'}


def secret_for(purpose, *, candidate, runtime, execution, generation=None):
    """Reconstruct private E ownership after lost ACK without another secret write.

    This is a domain-separated credential derived from the original immutable
    control key. Source configuration/key rotation cannot replace an active E's
    original runtime binding. No derived value is a native resource identity.
    """
    require(purpose in ('admission-owner/v1', 'execution-capability/v1'), 'unknown private runtime credential purpose')
    digest(candidate); identifier(runtime, 'runtime ID'); execution_id(execution)
    require(generation is None or type(generation) is int and generation > 0, 'invalid native claim generation')
    secret = os.environ.get(CONTROL_ENV)
    require(isinstance(secret, str) and 32 <= len(secret) <= 8192 and all(33 <= ord(c) <= 126 for c in secret),
            'original immutable private control credential missing')
    message = {'purpose': purpose, 'candidateKey': candidate, 'runtimeId': runtime, 'executionId': execution,
               'claimGeneration': generation}
    # canonical_digest is intentionally a hash of the full typed message, not
    # an ambiguous concatenation of human or resource identifiers.
    return hmac.new(secret.encode(), canonical_digest(message).encode(), hashlib.sha256).hexdigest()


class NativeControl:
    """Finite actual native routes; no arbitrary URL, owner or approval method."""
    def __init__(self, *, durable, configuration, http_factory=SetupHttp, clock=time.time):
        required = {'endpoint', 'runtimeId', 'targetId', 'runtimeBindingDigest', 'overhead'}
        require(isinstance(configuration, dict) and set(configuration) == required, 'installed native runtime binding missing')
        self.configuration, self.durable = copy.deepcopy(configuration), durable
        self.factory, self.clock = http_factory, clock
        self.runtime = identifier(configuration['runtimeId'], 'runtime ID')
        self.target = identifier(configuration['targetId'], 'target ID')
        digest(configuration['runtimeBindingDigest'])
        self.base = '/api/v1/agent/runtimes/'+quote(self.runtime, safe='')
        overhead = configuration['overhead']
        require(isinstance(overhead, dict) and set(overhead) == {'operationId', 'basisDigest', 'reservationIds'} and
                isinstance(overhead['reservationIds'], dict) and set(overhead['reservationIds']) == {'infrastructure', 'cleanup'} and
                len(set(overhead['reservationIds'].values())) == 2, 'installed controller requires original nonoverlapping overhead reservations')

    def authorize(self, *, reconcile=False):
        self.durable._require_live_lease()
        ledger = self.durable.state['ledger']
        validate_budget(ledger, allow_overrun=reconcile)
        validate_retained_funding(self.durable.state)
        require(ledger['candidateKey'] == self.durable.store.candidate and 'interactiveAccounting' in ledger,
                'installed execution requires original retained interactive funding')
        if not reconcile:
            require(self.clock() < ledger['interactiveAccounting']['originalExpiresEpoch'], 'original runtime funding expired')
        overhead = self.configuration['overhead']
        for kind, reservation in overhead['reservationIds'].items():
            rows = [row for row in ledger['operations'] if row['id'] == reservation]
            require(len(rows) == 1 and rows[0]['operationId'] == overhead['operationId'] and
                    rows[0]['purpose'] == 'shared-environment' and rows[0]['basisDigest'] == overhead['basisDigest'] and
                    rows[0]['kind'] == kind and rows[0]['state'] == 'reserved' and rows[0]['actualUsd'] is None,
                    'runtime control lacks its original reserved shared infrastructure/cleanup liability')

    def _call(self, operation, method, suffix, *, execution=None, body=None, mutation=None, capability=None, reconcile=False):
        identifier(operation, 'control operation')
        self.authorize(reconcile=reconcile)
        identity = {'candidateKey': self.durable.store.candidate, 'runtimeId': self.runtime}
        if execution is not None: identity['executionId'] = execution_id(execution)
        records = self.durable.state.setdefault('nativeRuntimeHttp', {})
        if method != 'GET':
            require(mutation is not None, 'native mutation requires original fixed request identity')
            step = operation+'-'+canonical_digest(mutation)[7:39]
        else:
            step = operation+'-'+str(len(records))
        scope = canonical_digest(identity)
        def retain(kind, selected, value):
            self.authorize(reconcile=reconcile)
            key = canonical_digest({'scope': scope, 'kind': kind, 'step': selected})
            require(key not in records, 'original native control intent already exists; reconcile without resending')
            records[key] = copy.deepcopy(value)
            self.durable._persist()
            return {'key': key, 'sha256': canonical_digest(value)}
        http = self.factory(self.configuration['endpoint'], CONTROL_ENV, identity=identity,
            authorize=lambda: self.authorize(reconcile=reconcile), retain=retain, credential_kind='api-key', timeout=120,
            execution_capability=capability)
        answer = http.json(step, method, self.base+suffix, body)
        require(answer['observation']['httpStatus'] in (200, 201), 'native external execution operation did not succeed')
        value = answer['observation']['body']
        require(isinstance(value, dict) and value.get('schemaVersion') == 1 and value.get('runtimeId') == self.runtime and
                value.get('targetId') == self.target and value.get('candidateKey') == self.durable.store.candidate,
                'native runtime response target/schema/identity differs')
        return {'value': value, 'reference': answer['reference']}

    def pending(self):
        observed = self._call('pending', 'GET', '/executions?limit=16')
        rows = observed['value'].get('executions')
        require(isinstance(rows, list) and len(rows) <= 16, 'native pending execution list is unbounded')
        seen = set()
        for row in rows:
            require(isinstance(row, dict), 'invalid native queued execution')
            original = execution_id(row.get('executionId'))
            require(original not in seen and row.get('state') == 'queued', 'duplicate or unqueued native discovery')
            seen.add(original)
            identifier(row.get('workspaceId'), 'workspace ID'); identifier(row.get('turnId'), 'turn ID')
        return observed

    def read(self, execution, workspace, turn, *, reconcile=False):
        execution_id(execution); identifier(workspace, 'workspace ID'); identifier(turn, 'turn ID')
        observed = self._call('read', 'GET', '/executions/'+execution+'?workspaceId='+workspace+'&turnId='+turn,
            execution=execution, reconcile=reconcile)
        record = observed['value'].get('execution')
        binding = observed['value'].get('binding')
        require(isinstance(record, dict) and record.get('executionId') == execution and record.get('state') in STATES and
                type(record.get('stateRevision')) is int and record['stateRevision'] >= 0 and isinstance(binding, dict) and
                binding.get('workspaceId') == workspace and binding.get('targetId') == self.target and
                binding.get('runtimeId') == self.runtime and binding.get('runtimeBindingDigest') == self.configuration['runtimeBindingDigest'] and
                isinstance(binding.get('turn'), dict) and binding['turn'].get('turnId') == turn,
                'original native execution/turn/runtime binding changed')
        return observed

    def mutate(self, operation, original, payload, *, capability=None, reconcile=False):
        require(operation in ('claim', 'renew', 'worker', 'question', 'final-response', 'complete', 'fence', 'retirement'),
                'native runtime control operation is outside reviewed lifecycle')
        value = original['value']
        record, binding = value['execution'], value['binding']
        require(isinstance(payload, dict), 'native control payload must be a typed object')
        body = {'workspaceId': binding['workspaceId'], 'turnId': binding['turn']['turnId'],
                'expectedVersion': record['stateRevision'], **payload}
        # A model payload can never replace the original native route/binding.
        require(body['workspaceId'] == binding['workspaceId'] and body['turnId'] == binding['turn']['turnId'] and
                body['expectedVersion'] == record['stateRevision'], 'native control payload replaced captured execution')
        return self._call(operation, 'POST', '/executions/'+execution_id(record['executionId'])+'/'+operation,
            execution=record['executionId'], body=body,
            mutation={'executionId': record['executionId'], 'operation': operation, 'body': body},
            capability=capability, reconcile=reconcile)

    def effects(self, original):
        value = original['value']
        return self._call('effects', 'GET', '/executions/'+execution_id(value['execution']['executionId'])+'/effects'+
            '?workspaceId='+identifier(value['binding']['workspaceId'], 'workspace ID')+
            '&turnId='+identifier(value['binding']['turn']['turnId'], 'turn ID'),
            execution=value['execution']['executionId'], reconcile=True)


EVENTS = {'native-observation', 'admission', 'reservation', 'worker-intent', 'worker-observation',
          'model-intent', 'model-response', 'action-intent', 'action-response', 'question-intent',
          'question-observation', 'final-response-intent', 'final-response-observation', 'effect-observation',
          'fence-observation', 'retirement-observation', 'settlement', 'slot-release-observation'}


def validate_execution_history(record):
    fields = {'schema', 'candidateKey', 'runtimeId', 'executionId', 'workspaceId', 'turnId', 'targetId',
              'requestDigest', 'runtimeBindingDigest', 'nativeOriginal', 'events'}
    require(isinstance(record, dict) and set(record) == fields and
            record['schema'] == 'honua.workspace-execution-journal/v1', 'invalid retained original execution journal')
    digest(record['candidateKey']); execution_id(record['executionId'])
    for field in ('runtimeId', 'workspaceId', 'turnId', 'targetId'): identifier(record[field], field)
    digest(record['requestDigest']); digest(record['runtimeBindingDigest'])
    require(isinstance(record['nativeOriginal'], dict) and set(record['nativeOriginal']) == {'key', 'sha256'},
            'original native execution response reference missing')
    digest(record['nativeOriginal']['sha256'])
    require(isinstance(record['events'], list), 'invalid original execution events')
    previous = canonical_digest({k: v for k, v in record.items() if k != 'events'})
    for ordinal, row in enumerate(record['events']):
        require(isinstance(row, dict) and set(row) == {'ordinal', 'kind', 'previousDigest', 'value', 'digest'} and
                row['ordinal'] == ordinal and row['kind'] in EVENTS and row['previousDigest'] == previous and
                isinstance(row['value'], dict), 'execution event identity/order changed')
        require(row['digest'] == canonical_digest({k: v for k, v in row.items() if k != 'digest'}),
                'retained execution event content changed')
        previous = row['digest']


class ExecutionJournal:
    """One append-only financial/effect history for the original native E."""
    def __init__(self, durable, original):
        self.durable = durable
        durable._require_live_lease()
        value, reference = original['value'], original['reference']
        raw = durable.state.get('nativeRuntimeHttp', {}).get(reference.get('key'))
        require(isinstance(raw, dict) and canonical_digest(raw) == reference.get('sha256') and
                isinstance(raw.get('response'), dict) and raw['response'].get('httpStatus') == 200 and
                raw['response'].get('body') == value,
                'execution journal requires original retained native GET response')
        intent = durable.state.get('nativeRuntimeHttp', {}).get(raw.get('intentReference', {}).get('key'))
        require(isinstance(intent, dict) and canonical_digest(intent) == raw.get('intentReference', {}).get('sha256') and
                canonical_digest(intent) == raw.get('intentDigest') and
                intent.get('method') == 'GET' and intent.get('credentialKind') == 'api-key' and
                intent.get('candidateKey') == durable.store.candidate,
                'execution original lacks its actual authenticated native read')
        record, binding = value['execution'], value['binding']
        original_id = execution_id(record['executionId'])
        require(value.get('candidateKey') == durable.store.candidate and
                intent.get('runtimeId') == value['runtimeId'] and intent.get('executionId') == original_id and
                intent.get('path') == '/api/v1/agent/runtimes/'+value['runtimeId']+'/executions/'+original_id+
                    '?workspaceId='+binding['workspaceId']+'&turnId='+binding['turn']['turnId'],
                'execution original route or identity differs')
        identity = {'schema': 'honua.workspace-execution-journal/v1', 'candidateKey': durable.store.candidate,
            'runtimeId': value['runtimeId'], 'executionId': original_id, 'workspaceId': binding['workspaceId'],
            'turnId': binding['turn']['turnId'], 'targetId': value['targetId'], 'requestDigest': value['requestDigest'],
            'runtimeBindingDigest': binding['runtimeBindingDigest']}
        records = durable.state.setdefault('interactiveExecutions', {})
        existing = records.get(original_id)
        if existing is None:
            records[original_id] = {**identity, 'nativeOriginal': copy.deepcopy(reference), 'events': []}
            validate_execution_history(records[original_id])
            durable._persist()
        else:
            validate_execution_history(existing)
            require(all(existing[k] == v for k, v in identity.items()), 'original E cannot move to another runtime/turn')
        self.record = records[original_id]

    def append(self, kind, value):
        require(kind in EVENTS and isinstance(value, dict), 'unsupported original execution event')
        self.durable._require_live_lease()
        validate_execution_history(self.record)
        rows = self.record['events']
        previous = rows[-1]['digest'] if rows else canonical_digest({k: v for k, v in self.record.items() if k != 'events'})
        event = {'ordinal': len(rows), 'kind': kind, 'previousDigest': previous, 'value': copy.deepcopy(value)}
        event['digest'] = canonical_digest(event)
        rows.append(event)
        self.durable._persist()
        return event

    def values(self, kind):
        return [row['value'] for row in self.record['events'] if row['kind'] == kind]

    def before_model(self, ordinal, request_digest, reservation_id, admission):
        require(type(ordinal) is int and ordinal >= 0 and
                not any(row.get('round') == ordinal for row in self.values('model-intent')),
                'model invocation already intended; recover original response without invoking again')
        intents, responses = self.values('model-intent'), self.values('model-response')
        require(len(intents) == len(responses) == ordinal, 'prior model outcome remains unknown')
        self.durable.update(self.durable.path, 'check-interactive-attempt',
            candidate_key=self.record['candidateKey'], execution_id=self.record['executionId'],
            owner_token=admission['ownerToken'])
        rows = [row for row in self.durable.state['ledger']['interactiveEntries'] if row['id'] == reservation_id]
        require(len(rows) == 1 and rows[0]['executionId'] == self.record['executionId'] and
                rows[0]['kind'] == 'model' and rows[0]['state'] == 'reserved' and rows[0]['actualUsd'] is None,
                'model invocation lacks its original reserved execution liability')
        return self.append('model-intent', {'round': ordinal, 'requestDigest': digest(request_digest),
            'reservationId': reservation_id})
