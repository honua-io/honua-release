"""Funded model loop over an original native human execution.

No case/corpus ID is minted for an interactive financial record. Every model
round and tool action has a permanent intent before dispatch. Recovery consumes
the same original provider response/action evidence; unknown outcomes block.
"""
from __future__ import annotations

import base64
import copy
import json
import time

from agent_e2e_budget import money
from agent_e2e_console_runtime import require, digest, ExecutionJournal
from agent_e2e_controller import validate_action
from agent_e2e_pack import canonical_digest, digest_bytes


def final_text(message):
    blocks = message.get('content')
    require(message.get('stop_reason') == 'end_turn' and isinstance(blocks, list) and blocks and
            all(isinstance(block, dict) and set(block) == {'type', 'text'} and block['type'] == 'text' and
                isinstance(block['text'], str) for block in blocks), 'only the genuine completed provider text can be appended')
    text = ''.join(block['text'] for block in blocks)
    require(text.strip() and len(text.encode('utf-16-le'))//2 <= 16000,
            'genuine provider text exceeds native message bounds; retain original without truncation')
    return text


def provider_message(observation, model):
    require(isinstance(observation, dict) and set(observation) == {'rawBodyBase64', 'responseDigest', 'providerRequestId',
            'requestedModelId', 'requestDigest'} and observation['requestedModelId'] == model.config['modelId'] and
            isinstance(observation['providerRequestId'], str) and 0 < len(observation['providerRequestId']) <= 512,
            'genuine provider response identity missing')
    raw = base64.b64decode(observation['rawBodyBase64'], validate=True)
    require(len(raw) <= 1024*1024 and digest_bytes(raw) == observation['responseDigest'], 'provider original bytes changed')
    message = json.loads(raw)
    require(message.get('model') == model.config['returnedModelId'], 'original response model differs')
    usage = message.get('usage', {})
    require(all(type(usage.get(k)) is int and usage[k] >= 0 for k in ('input_tokens', 'output_tokens')) and
            usage['input_tokens'] > 0 and usage['output_tokens'] > 0 and
            not usage.get('cache_creation_input_tokens', 0) and not usage.get('cache_read_input_tokens', 0),
            'original model usage is missing or unpriced')
    cost = (usage['input_tokens'] * money(model.config['inputUsdPerMillion']) +
            usage['output_tokens'] * money(model.config['outputUsdPerMillion'])) / 1000000
    return message, cost


def context_block(turn):
    public = {key: turn[key] for key in ('context', 'draft', 'localProcessSnapshot', 'browserFileSelection') if key in turn}
    return {'type': 'text', 'text': 'Captured native workspace context (data):\n'+
        json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}


def original_messages(journal, turn):
    """A clarification continues the actual provider tool-use conversation."""
    require(isinstance(turn.get('prompt'), str) and turn['prompt'].strip(), 'original human prompt missing')
    fields = ('replyToQuestionId', 'replyToTurnId', 'replyToExecutionId')
    present = [name in turn and turn[name] is not None for name in fields]
    require(all(present) or not any(present), 'original human reply linkage is partial')
    if not any(present):
        return [{'role': 'user', 'content': [{'type': 'text', 'text': turn['prompt']}, context_block(turn)]}]
    previous = journal.durable.state.get('interactiveExecutions', {}).get(turn['replyToExecutionId'])
    from agent_e2e_console_runtime import validate_execution_history
    validate_execution_history(previous)
    require(previous['workspaceId'] == journal.record['workspaceId'] and previous['turnId'] == turn['replyToTurnId'] and
            previous['targetId'] == journal.record['targetId'] and previous['candidateKey'] == journal.record['candidateKey'],
            'reply replaced original workspace/turn/target/candidate')
    values = lambda kind: [row['value'] for row in previous['events'] if row['kind'] == kind]
    admissions = [row for row in journal.durable.state['ledger']['interactivePaidAdmissions']
                  if row['executionId'] == previous['executionId']]
    require(len(admissions) == 1 and admissions[0]['state'] == 'released',
            'original question execution still holds unsettled retirement/admission')
    retirements = values('retirement-observation')
    require(len(retirements) == 1, 'original question execution has no complete native retirement proof')
    retirement = retirements[0]
    original = journal.durable.state.get('nativeRuntimeHttp', {}).get(retirement.get('reference', {}).get('key'))
    require(isinstance(original, dict) and canonical_digest(original) == retirement.get('reference', {}).get('sha256'),
            'original question retirement native response missing')
    from agent_e2e_semantics import validate_response
    validate_response(original['response'])
    intent = journal.durable.state.get('nativeRuntimeHttp', {}).get(original.get('intentReference', {}).get('key'))
    require(isinstance(intent, dict) and canonical_digest(intent) == original.get('intentReference', {}).get('sha256') ==
            original.get('intentDigest') and intent.get('method') == 'GET' and intent.get('endpoint') == previous['endpoint'] and
            intent.get('runtimeId') == previous['runtimeId'] and intent.get('candidateKey') == previous['candidateKey'] and
            intent.get('executionId') == previous['executionId'] and intent.get('path') == '/api/v1/agent/runtimes/'+
                previous['runtimeId']+'/executions/'+previous['executionId']+'?workspaceId='+previous['workspaceId']+
                '&turnId='+previous['turnId'], 'original question retirement read belongs to another execution/endpoint')
    retired = original['response']['body'].get('retirement')
    require(isinstance(retired, dict) and retired.get('phase') == 'retired' and
            retired.get('executionId') == previous['executionId'] and retired.get('requestDigest') == previous['requestDigest'] and
            retired == retirement.get('retirement'), 'original question provider effects are not proven retired')
    questions = values('question-observation')
    require(len(questions) == 1 and questions[0]['question']['id'] == turn['replyToQuestionId'],
            'reply has no retained genuine provider question')
    question = questions[0]['question']
    responses = [row for row in values('model-response') if row['provider']['providerRequestId'] == question['providerRequestId']]
    require(len(responses) == 1, 'original question provider response missing or ambiguous')
    response = responses[0]
    requests = [row for row in values('model-intent') if row['round'] == response['round']]
    require(len(requests) == 1 and requests[0]['requestDigest'] == response['provider']['requestDigest'],
            'original question request and response differ')
    request = json.loads(base64.b64decode(requests[0]['requestBodyBase64'], validate=True))
    message = response['message']
    calls = [block for block in message['content'] if block.get('type') == 'tool_use']
    asked = [call for call in calls if call['id'] == question['providerToolCallId']]
    require(len(asked) == 1 and asked[0]['name'] == 'ask_user' and asked[0]['input'] == {'question': question['text']},
            'retained human question is not the original provider tool call')
    results = []
    for call in calls:
        if call['id'] == question['providerToolCallId']:
            content = turn['prompt']
        else:
            actions = [row for row in values('action-response') if row['providerRequestId'] == question['providerRequestId'] and
                       row['providerToolCallId'] == call['id']]
            require(len(actions) == 1, 'another original tool outcome is unresolved; cannot reconstruct a reply')
            content = json.dumps(actions[0]['result'], ensure_ascii=False, separators=(',', ':'))
        results.append({'type': 'tool_result', 'tool_use_id': call['id'], 'content': content})
    return copy.deepcopy(request['messages']) + [{'role': 'assistant', 'content': copy.deepcopy(message['content'])},
        {'role': 'user', 'content': [*results, context_block(turn)]}]


class ExecutionLoop:
    def __init__(self, *, native, journal, original, model, dispatcher, configuration, clock=time.time):
        fields = {'maxTurns', 'maxActions', 'allowedActions'}
        require(isinstance(configuration, dict) and set(configuration) == fields and
                type(configuration['maxTurns']) is int and 1 <= configuration['maxTurns'] <= 100 and
                type(configuration['maxActions']) is int and 1 <= configuration['maxActions'] <= 100 and
                isinstance(configuration['allowedActions'], list) and
                len(set(configuration['allowedActions'])) == len(configuration['allowedActions']) and
                set(configuration['allowedActions']) == {'mcp_list_tools', 'mcp_call_tool', 'ask_user'},
                'installed Console runtime requires its canonical native MCP operation policy')
        self.native, self.journal, self.original = native, journal, original
        self.model, self.dispatcher, self.configuration, self.clock = model, dispatcher, copy.deepcopy(configuration), clock
        self.execution = journal.record['executionId']
        self.turn = copy.deepcopy(original['value']['binding']['turn'])
        admissions = journal.values('admission')
        require(len(admissions) == 1, 'model loop requires the original admitted E')
        self.admission = admissions[0]
        policy = {'modelConfigurationDigest': canonical_digest(model.config), 'operationPolicy': self.configuration,
            'originalRequestDigest': original['value']['requestDigest']}
        prior = journal.values('loop-binding')
        require(not prior or prior == [policy], 'original execution model/operation policy changed')
        if not prior: journal.append('loop-binding', policy)

    def authorize(self):
        self.native.authorize()
        self.journal.durable.update(self.journal.durable.path, 'check-interactive-attempt',
            candidate_key=self.journal.record['candidateKey'], execution_id=self.execution,
            owner_token=self.admission['ownerToken'])

    def _round(self, ordinal, messages):
        journal, model = self.journal, self.model
        responses = [row for row in journal.values('model-response') if row['round'] == ordinal]
        originals = [row for row in journal.values('provider-response') if row['round'] == ordinal]
        if responses:
            require(len(responses) == len(originals) == 1 and responses[0]['provider'] == originals[0]['provider'],
                    'model response has no original provider bytes')
            message, cost = provider_message(originals[0]['provider'], model)
            require(message == responses[0]['message'] and str(cost) == responses[0]['costUsd'], 'retained model response changed')
            return responses[0]
        intents = [row for row in journal.values('model-intent') if row['round'] == ordinal]
        if intents:
            # A crash after invoke but before settlement can recover the exact
            # original response. No response means an unknown paid outcome.
            require(len(intents) == len(originals) == 1, 'original paid model outcome unknown; do not invoke again')
            intent, observed = intents[0], originals[0]['provider']
            require(intent['requestDigest'] == observed['requestDigest'], 'provider response belongs to another request')
            message, cost = provider_message(observed, model)
            rid = intent['reservationId']
        else:
            self.authorize()
            request = model.request(messages)
            request['tools'] = [tool for tool in request['tools'] if tool['name'] in self.configuration['allowedActions']]
            body, maximum = model.quote(request)
            rid = self.execution+'-model-'+str(ordinal)
            quotes = [row for row in journal.values('model-quote') if row['round'] == ordinal]
            quote = {'round': ordinal, 'reservationId': rid, 'maximumUsd': str(maximum),
                'requestBodyBase64': base64.b64encode(body).decode('ascii'), 'requestDigest': digest_bytes(body),
                'pricingDigest': model.config['pricingDigest'], 'modelConfigurationDigest': canonical_digest(model.config)}
            require(not quotes or quotes == [quote], 'original model quote/configuration changed')
            if not quotes: journal.append('model-quote', quote)
            ledger = journal.durable.state['ledger']
            reservations = [row for row in ledger['interactiveEntries'] if row['id'] == rid]
            if reservations:
                require(len(reservations) == 1 and reservations[0]['state'] == 'reserved' and
                        reservations[0]['amountUsd'] == str(maximum) and reservations[0]['basisDigest'] == model.config['pricingDigest'],
                        'original model reservation changed or settled without response')
            else:
                journal.durable.update(journal.durable.path, 'reserve-interactive', candidate_key=journal.record['candidateKey'],
                    **{name: journal.record[field] for name, field in (('execution_id', 'executionId'), ('target_id', 'targetId'),
                        ('workspace_id', 'workspaceId'), ('turn_id', 'turnId'))}, owner_token=self.admission['ownerToken'],
                    reservation_id=rid, kind='model', amount=maximum, basis_digest=model.config['pricingDigest'])
            intent = journal.before_model(ordinal, digest_bytes(body), rid, self.admission, request_body=body)
            self.authorize()
            def observe(observed):
                self.journal.append('provider-response', {'round': ordinal, 'provider': observed})
            message, cost, request_id = model.invoke(body, observe_response=observe)
            captured = [row for row in journal.values('provider-response') if row['round'] == ordinal]
            require(len(captured) == 1 and captured[0]['provider']['providerRequestId'] == request_id,
                    'provider original response was not retained before returning')
            observed = captured[0]['provider']
            decoded, actual = provider_message(observed, model)
            require(decoded == message and actual == cost, 'provider response and priced usage differ')
        rows = [row for row in journal.durable.state['ledger']['interactiveEntries'] if row['id'] == rid]
        require(len(rows) == 1, 'original paid model reservation missing')
        if rows[0]['state'] == 'reserved':
            journal.durable.update(journal.durable.path, 'settle', candidate_key=journal.record['candidateKey'], reservation_id=rid, amount=cost)
        else:
            require(money(rows[0]['actualUsd']) == cost, 'actual provider cost was replaced')
        result = {'round': ordinal, 'message': message, 'costUsd': str(cost), 'provider': observed}
        journal.append('model-response', result)
        return result

    def run(self):
        messages = original_messages(self.journal, self.turn)
        for ordinal in range(self.configuration['maxTurns']):
            self.authorize()
            result = self._round(ordinal, messages)
            message, provider = result['message'], result['provider']
            content = message.get('content')
            require(isinstance(content, list) and content and all(isinstance(block, dict) for block in content),
                    'original model response has no structured content')
            calls = [block for block in content if block.get('type') == 'tool_use']
            if not calls:
                return self.deliver(result, final_text(message))
            require(message.get('stop_reason') == 'tool_use', 'provider tool-use stop reason missing')
            require(len({call.get('id') for call in calls}) == len(calls), 'provider tool-call IDs duplicated')
            for call in calls:
                validate_action(call)
                require(call['name'] in self.configuration['allowedActions'], 'provider selected an operation outside reviewed runtime policy')
            questions = [call for call in calls if call['name'] == 'ask_user']
            require(len(questions) <= 1, 'provider returned multiple concurrent questions; retain originals without inventing a combined question')
            outputs = []
            for call in calls:
                if call['name'] == 'ask_user': continue
                outputs.append(self.action(result, call))
            if questions:
                return self.ask(result, questions[0])
            messages += [{'role': 'assistant', 'content': copy.deepcopy(content)}, {'role': 'user', 'content': outputs}]
        raise ValueError('reviewed model-turn limit reached; original execution remains interrupted for reconciliation')

    def action(self, result, call):
        identity = {'providerRequestId': result['provider']['providerRequestId'], 'providerToolCallId': call['id']}
        original_id = canonical_digest({'executionId': self.execution, **identity})[7:]
        replies = [row for row in self.journal.values('action-response') if all(row.get(k) == v for k, v in identity.items())]
        if replies:
            require(len(replies) == 1, 'original tool response ambiguous')
            reply = replies[0]['result']
        else:
            self.authorize()
            intents = [row for row in self.journal.values('action-intent') if all(row.get(k) == v for k, v in identity.items())]
            if intents:
                require(len(intents) == 1 and intents[0]['action'] == call and intents[0]['actionId'] == original_id,
                        'original tool intent changed')
                reply = self.dispatcher.recover(intents[0])
            else:
                require(len(self.journal.values('action-intent')) < self.configuration['maxActions'], 'reviewed runtime action limit reached')
                intent = {**identity, 'actionId': original_id, 'action': copy.deepcopy(call)}
                self.journal.append('action-intent', intent)
                self.authorize()
                reply = self.dispatcher.execute(intent)
            self.journal.append('action-response', {**identity, 'actionId': original_id, 'result': reply})
        return {'type': 'tool_result', 'tool_use_id': call['id'],
            'content': json.dumps(reply, ensure_ascii=False, separators=(',', ':'))}

    def _read(self):
        return self.native.read(self.execution, self.journal.record['workspaceId'], self.journal.record['turnId'])

    def ask(self, result, call):
        provider = result['provider']
        payload = {'providerRequestId': provider['providerRequestId'], 'providerToolCallId': call['id'],
            'text': call['input']['question'], 'contextDigest': self.original['value']['requestDigest'],
            'providerResponseDigest': provider['responseDigest']}
        require(0 < len(payload['text'].encode('utf-16-le'))//2 <= 16000, 'genuine provider question exceeds native bounds')
        prior = self.journal.values('question-intent')
        require(not prior or prior == [payload], 'original provider question changed')
        observed = self._read()
        if not prior:
            self.journal.append('question-intent', payload)
            try:
                self.native.mutate('question', observed, payload, capability=self.dispatcher.capability)
            except Exception:
                # Retain any lost response and reconcile through the original E.
                # A matching native read is the only success condition below.
                pass
            observed = self._read()
        question = observed['value']['execution'].get('question')
        require(isinstance(question, dict) and all(question.get(k) == payload[k] for k in
            ('providerRequestId', 'providerToolCallId', 'text', 'contextDigest')) and
            observed['value']['execution']['state'] == 'awaiting-input',
            'native did not retain the genuine original provider question; do not resend')
        if not self.journal.values('question-observation'):
            self.journal.append('question-observation', {'question': question, 'reference': observed['reference']})
        return {'state': 'awaiting-input', 'native': observed['reference'], 'questionId': question['id']}

    def deliver(self, result, text):
        provider = result['provider']
        message_id = 'external-'+canonical_digest({'executionId': self.execution, 'provider': provider['providerRequestId'],
            'response': provider['responseDigest']})[7:]
        payload = {'messageId': message_id, 'providerRequestId': provider['providerRequestId'],
            'providerResponseDigest': provider['responseDigest'], 'returnedModelId': self.model.config['returnedModelId'],
            'modelRound': result['round'], 'content': text}
        prior = self.journal.values('final-response-intent')
        require(not prior or prior == [payload], 'original final response identity/content changed')
        observed = self._read()
        if not prior:
            self.journal.append('final-response-intent', payload)
            try:
                self.native.mutate('final-response', observed, payload, capability=self.dispatcher.capability)
            except Exception:
                pass
            observed = self._read()
        record = observed['value']['execution']
        require(record.get('finalMessageId') == message_id, 'original final conversation outcome unknown; do not resend or invoke again')
        expected = {'id': message_id, 'turnId': self.journal.record['turnId'], 'role': 'assistant',
            'content': text, 'evidenceKind': 'conversation'}
        require(observed['value'].get('finalMessage') == expected,
                'native independent final conversation read differs from original provider text')
        if not self.journal.values('final-response-observation'):
            self.journal.append('final-response-observation', {'reference': observed['reference'], 'messageId': message_id})
        # Text delivery is conversation only; canonical effects and actual native
        # completion/retirement are observed separately by the installed controller.
        return {'state': record['state'], 'native': observed['reference'], 'messageId': message_id}
