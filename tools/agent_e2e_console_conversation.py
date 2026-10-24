"""Deliver the original external-model final response as conversation only."""
from __future__ import annotations
import copy
from pathlib import Path
import sys

directory = str(Path(__file__).resolve().parent/'hosted_pilot')
if directory not in sys.path:
    sys.path.insert(0, directory)

from agent_e2e_pack import canonical_digest
from agent_e2e_p10_backend import verify_worker_definition, sdk
from agent_e2e_scenario_backend import ScenarioHttp, require
from agent_e2e_scenario_operations import body

IDENTITY = ('campaignId', 'caseId', 'attempt', 'campaignMode', 'componentBindingDigest')


class Conversation:
    def __init__(self, *, durable, context, configuration, case, ecs=None, http_factory=ScenarioHttp):
        from hosted_pilot.worker_mcp_configuration import retained_context, verify_injected_credential
        require(isinstance(configuration, dict) and set(configuration) == {'nativeContext', 'workerMcp'},
            'conversation requires original native context and immutable worker MCP references')
        self.durable, self.context = durable, copy.deepcopy(context)
        endpoint = case['http']['endpoint']
        self.native = retained_context(durable, configuration['nativeContext'], endpoint)
        require('consoleTurn' in self.native and
            self.native['consoleTurn']['configuration']['caseId'] == context['caseId'],
            'conversation requires an adopted original Console turn for this frozen case')
        from hosted_pilot.console_turn import observe_session
        self.observed_session = observe_session(durable,
            self.native['consoleTurn']['configuration']['sessionCustody'], endpoint, self.native['humanCreatorPrincipal'])
        verify_injected_credential(self.native, 'us-east-1', ecs=ecs)
        reference = configuration['workerMcp']
        require(isinstance(reference, dict) and set(reference) == {'key', 'sha256'} and reference['key'].endswith('/completed'),
            'conversation worker MCP reference must be an original completion')
        records = durable.state.get('scenarioOperations', {})
        completed = records.get(reference['key'])
        intent = records.get(reference['key'].removesuffix('/completed')+'/intent')
        require(isinstance(completed, dict) and canonical_digest(completed) == reference['sha256'] and
            all(isinstance(row, dict) and row.get('producer') == 'private-worker-mcp' and
                row.get('candidateKey') == durable.store.candidate for row in (completed, intent)),
            'conversation worker MCP original intent/completion is missing')
        binding, public = completed['value'], intent['value']
        require(binding['nativeContext'] == public['nativeContext'] == configuration['nativeContext'] and
            public['issuedAgentKey'] == self.native['issuedAgentKey'] and
            public['agentContext'] == self.native['agentContext'] and public['endpoint'] == endpoint+'/mcp' and
            binding['secretArn'] == public['secretArn'] and binding['versionId'] == public['versionId'] and
            binding['publicBindingDigest'] == canonical_digest(public), 'conversation immutable MCP binding differs')
        self.worker = case['attempts'][str(context['attempt'])]
        self.http = http_factory(durable=durable, context=context, endpoint=endpoint,
            credential_env='HONUA_AGENT_REQUESTER_KEY', credential_kind='api-key', agent_context=self.native['agentContext'],
            attempt_id=self.worker['attemptId']+'-conversation')
        self.http.authorize()
        ecs = ecs or sdk('ecs', 'us-east-1')
        definition = ecs.describe_task_definition(taskDefinition=self.worker['lifecycle']['taskDefinitionArn'])['taskDefinition']
        self.http.authorize()
        require(definition.get('taskDefinitionArn') == self.worker['lifecycle']['taskDefinitionArn'],
            'conversation observed a different immutable worker task revision')
        container = verify_worker_definition(definition, self.worker)
        require(container['secrets'] == [{'name': 'HONUA_AGENT_MCP_CONFIG',
            'valueFrom': binding['secretArn']+':::'+binding['versionId']}],
            'conversation model worker does not carry the exact adopted-turn MCP version')
        self.identity = {name: context[name] for name in IDENTITY}
        self.key = 'console-conversation:'+canonical_digest(self.identity)[7:]
        claim_key = 'console-turn-claim:'+canonical_digest(configuration['nativeContext'])[7:]
        claim = {'producer': 'private-console-conversation', 'candidateKey': durable.store.candidate,
            'identity': self.identity, 'configuration': configuration}
        require(claim_key not in records or records[claim_key] == claim,
            'original Console turn is already bound to a different worker attempt')
        self.http.authorize()
        durable.state.setdefault('scenarioOperations', {})[claim_key] = copy.deepcopy(claim)
        durable._persist()

    def append(self, payload):
        require(set(payload) == {'message', 'providerRequestId', 'turn'} and
            type(payload['turn']) is int and payload['turn'] > 0 and
            isinstance(payload['providerRequestId'], str) and payload['providerRequestId'],
            'final conversation lacks original provider response identity')
        message = payload['message']
        require(isinstance(message, dict) and message.get('stop_reason') == 'end_turn' and
            isinstance(message.get('content'), list) and message['content'] and
            all(isinstance(block, dict) and set(block) == {'type', 'text'} and block['type'] == 'text' and
                isinstance(block['text'], str) for block in message['content']),
            'only the actual completed text response can become final conversation')
        text = ''.join(block['text'] for block in message['content'])
        require(text.strip() and len(text.encode('utf-16-le'))//2 <= 16000,
            'actual final response exceeds native conversation bounds; do not truncate or invent a summary')
        journal_key = canonical_digest({name: self.context[name] for name in ('campaignId', 'caseId', 'attempt')})
        journal = self.durable.state.get('journals', {}).get(journal_key, [])
        models = [row for row in journal if row.get('event') == 'model' and row.get('turn') == payload['turn']]
        requests = [row for row in journal if row.get('event') == 'provider-request' and row.get('turn') == payload['turn']]
        require(len(models) == len(requests) == 1 and models[0].get('responseDigest') == canonical_digest(message) and
            requests[0].get('requestId') == payload['providerRequestId'],
            'final text is not the original paid model response and provider identity')
        turn_id = self.native['agentContext']['turnId']
        message_id = 'external-'+canonical_digest({'identity': self.identity, 'response': payload})[7:]
        expected = {'id': message_id, 'turnId': turn_id, 'role': 'assistant', 'content': text, 'evidenceKind': 'conversation'}
        records = self.durable.state.setdefault('scenarioOperations', {})
        record = {'producer': 'private-console-conversation', 'candidateKey': self.durable.store.candidate,
            'identity': self.identity, 'response': copy.deepcopy(payload), 'message': expected}
        intent_key = self.key+'/final-response-intent'
        existed = intent_key in records
        require(not existed or records[intent_key] == record, 'original final response identity/content changed')
        self.http.authorize()
        if not existed:
            records[intent_key] = record
            self.durable._persist()
        base = '/api/v1/agent/workspaces/'+self.native['agentContext']['workspaceId']+'/conversation'
        # Every recovery is a fresh authenticated read. A lost POST reply never
        # causes another POST, model call, or replacement message identity.
        step = self.key.rsplit(':', 1)[1][:24]+'-'+str(len(records))
        observed = self.http.json(step+'-before', 'GET', base)
        messages = body(observed).get('messages')
        require(isinstance(messages, list), 'native conversation read lacks actual messages')
        matching = [row for row in messages if row.get('id') == message_id]
        require(len(matching) <= 1 and (not matching or matching == [expected]),
            'native conversation message id is bound to different content')
        if not matching:
            require(not existed, 'original final response outcome unresolved; observe without replaying POST')
            try:
                answer = self.http.json(step+'-append', 'POST', base,
                    {'messageId': message_id, 'turnId': turn_id, 'content': text})
            except Exception:
                # Resolve a lost acknowledgement inside the still-funded
                # attempt, before ordinary worker cleanup can end admission.
                after = self.http.json(step+'-lost-reply', 'GET', base)
                require([row for row in body(after).get('messages', []) if row.get('id') == message_id] == [expected],
                    'original final response outcome unresolved; observe without replaying POST')
            else:
                require(body(answer) == expected, 'native final conversation response differs')
                after = self.http.json(step+'-after', 'GET', base)
            require([row for row in body(after).get('messages', []) if row.get('id') == message_id] == [expected],
                'native final conversation was not independently read back')
            observed = after
        result = {'producer': 'private-console-conversation', 'candidateKey': self.durable.store.candidate,
            'identity': self.identity, 'messageId': message_id, 'responseDigest': canonical_digest(message),
            'providerRequestId': payload['providerRequestId'], 'observation': observed, 'evidenceKind': 'conversation'}
        key = self.key+'/final-response-readback/'+canonical_digest(observed)[7:]
        self.http.authorize()
        records[key] = result
        self.durable._persist()
        return {'status': 'delivered', 'messageId': message_id, 'evidenceKind': 'conversation'}
