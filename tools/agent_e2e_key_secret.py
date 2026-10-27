"""One-time P16 operation-secret consumption under the original native task identity.

Secret-bearing response bytes never enter ScenarioHttp or its evidence store.
Only the exact nonsecret handle/reference and a consumed outcome are retained.
A persisted intent prohibits any subsequent consume, including after process loss.
"""
from __future__ import annotations

import copy
import re
import urllib.request

from agent_e2e_mcp import parse, execution_headers
from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_scenario_operations import data


def require(value, reason):
    if not value:
        raise PackError(reason)


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,200}', value),
            'invalid native key secret identity')
    return value


class IssuedCredential:
    """Memory-only issued key, usable only through screened, retained HTTP probes."""
    __slots__ = ('_token', '_source')

    def __init__(self, token, source):
        self._token, self._source = token, source

    def __repr__(self):
        return '<IssuedCredential private>'

    def __reduce__(self):
        raise PackError('issued credential cannot be serialized')

    def close(self):
        self._token = None

    def screen(self, http):
        require(self._token is not None, 'issued credential is no longer available')
        require(http.durable is self._source.durable and http.context == self._source.context and
                http.client.endpoint == self._source.client.endpoint,
                'issued key probe differs from original candidate/attempt/origin')
        if self._token not in http.client.private_values:
            http.client.private_values = (*http.client.private_values, self._token)
        http.client.check_private_metadata(http.durable.state)

    def assert_independent(self, http):
        require(self._token is not None and http.client.token != self._token,
                'independent observer cannot use the issued credential')
        self.screen(http)

    def probe(self, http, step, method, path, body=None):
        self.screen(http)
        # Copy the transport, retaining its lease, deadline, retention and no-retry
        # set. Never install the issued key in process environment or artifacts.
        client = copy.copy(http.client)
        client.kind, client.token = 'api-key', self._token
        client.agent_context = None
        client.execution_context = None
        client.execution_capability = None
        client.private_values = (*client.private_values, self._token)
        return client.json(step, method, path, body)


class OperationKeyConsumer:
    def __init__(self, requester, *, principal):
        self.http, self.principal = requester, copy.deepcopy(principal)
        require(isinstance(principal, dict) and set(principal) ==
                {'actorId', 'studioOwnerId', 'tenantId', 'authenticationKind', 'isHumanSession'} and
                principal['authenticationKind'] == 'api-key' and principal['isHumanSession'] is False and
                isinstance(principal['actorId'], str) and principal['actorId'],
                'private consume requires exact original task principal')
        require(requester.client.kind == 'api-key' and requester.client.agent_context is not None,
                'private consume requires authorized original native task context')

    def consume(self, handle_id, reference_id):
        handle_id, reference_id = identifier(handle_id), identifier(reference_id)
        # One identity per native one-time object, never caller-chosen step aliases.
        step = 'p16-consume-'+canonical_digest({'handle': handle_id, 'reference': reference_id})[7:47]
        http, client = self.http, self.http.client
        scope = {field: http.context[field] for field in ('campaignId', 'caseId', 'attempt', 'componentBindingDigest')}
        consume_identity = canonical_digest({'scope': scope, 'handle': handle_id, 'reference': reference_id})
        require(not any(record.get('producer') == 'p16-private-key-consume' and
                        record.get('consumeIdentity') == consume_identity
                        for record in http.durable.state.get('scenarioOperations', {}).values()),
                'secret consume already attempted; reconcile without retry')
        target = http.json(step+'-target', 'GET', '/api/v1/agent/target')
        require(target['observation']['httpStatus'] == 200 and data(target).get('principal') == self.principal,
                'secret consume principal differs from original native task')
        answer = http.json(step+'-handle', 'GET', '/api/v1/operations/handles/'+handle_id)
        handle = data(answer)
        require(answer['observation']['httpStatus'] == 200 and
                handle.get('operationInstanceId') == handle_id and handle.get('operationId') == 'admin.api-key.create' and
                handle.get('status') == 'Completed' and
                handle.get('result', {}).get('secretReferences') == [{'referenceId': reference_id, 'name': 'key'}],
                'secret consume lacks exact completed native issuance/reference')
        path = '/api/v1/operations/handles/'+handle_id+'/secrets/'+reference_id+'/consume'
        intent = {'schemaVersion': 1, **http.identity, 'step': step,
                  'producer': 'p16-private-key-consume', 'consumeIdentity': consume_identity, 'endpoint': client.endpoint,
                  'method': 'POST', 'path': path, 'principal': self.principal,
                  'agentContext': client.agent_context, 'nativeHandle': answer['reference'],
                  'nativeTarget': target['reference']}
        client.check_private_metadata(intent)
        http.authorize()
        acknowledgement = client.retention_ack(http.retain('intent', step, intent), intent)
        # From this point all failures are unknown consume outcomes. Never retain
        # server text or exceptions: either could carry the newly issued secret.
        try:
            http.authorize()
            headers = {'X-API-Key': client.token, 'Accept': 'application/json',
                       'X-Honua-Workspace': client.agent_context['workspaceId'],
                       'X-Honua-Turn': client.agent_context['turnId']}
            headers.update(execution_headers(client.execution_context))
            if client.execution_capability is not None:
                headers['X-Honua-Execution-Capability'] = client.execution_capability
            request = urllib.request.Request(client.endpoint+path, data=b'', headers=headers, method='POST')
            with client.opener.open(request, timeout=client.remaining()) as response:
                raw = bytearray()
                while True:
                    client.remaining()
                    chunk = response.read1(min(4096, 16385-len(raw)))
                    if not chunk:
                        break
                    raw.extend(chunk)
                    require(len(raw) <= 16384, 'private consume response exceeds bound')
                value = parse(bytes(raw))
                require(response.code == 200 and isinstance(value, dict) and {'success', 'data'} <= set(value) <= {'success', 'data', 'message', 'timestamp'} and
                        value['success'] is True and isinstance(value['data'], dict) and
                        set(value['data']) == {'name', 'value'} and value['data']['name'] == 'key',
                        'private consume failed')
                token = value['data']['value']
                require(isinstance(token, str) and 32 <= len(token) <= 8192 and
                        all(33 <= ord(c) <= 126 for c in token) and token not in client.private_values,
                        'private consume returned invalid credential')
                screening = copy.copy(client)
                screening.private_values = (*client.private_values, token)
                screening.check_private_metadata(http.durable.state)
                # Screen all later requester observations too, including native handles.
                client.private_values = screening.private_values
                # Deliberately no body, body hash, preview, response headers or exception text.
                outcome = {'schemaVersion': 1, **http.identity, 'step': step,
                           'producer': 'p16-private-key-consume', 'intentReference': acknowledgement,
                           'intentDigest': canonical_digest(intent), 'outcome': 'consumed'}
                client.check_private_metadata(outcome)
                client.retention_ack(http.retain('response', step, outcome), outcome)
                return IssuedCredential(token, http)
        except Exception:
            raise PackError('private consume outcome unknown; reconcile without retry') from None
