"""Real HTTP/accounting with provider doubles, never live model qualification."""
import copy
import io
import json
from types import SimpleNamespace

import pytest

from agent_e2e_console_loop import ExecutionLoop, original_messages
from agent_e2e_console_runtime import ExecutionJournal
from agent_e2e_controller import BedrockModel, ControllerError
from agent_e2e_pack import PackError
from test_agent_e2e_console_runtime import runtime, original, E
from test_agent_e2e_interactive_budget import interactive
from test_agent_e2e_setup_http import server
from test_agent_e2e_budget import KEY, BASIS


class Provider:
    def __init__(self, messages):
        self.messages, self.invocations, self.quotes, self.lose = messages, [], [], False
    def count_tokens(self, **kwargs):
        self.quotes.append(copy.deepcopy(kwargs))
        return {'inputTokens': 100}
    def invoke_model(self, **kwargs):
        self.invocations.append(copy.deepcopy(kwargs))
        if self.lose: raise TimeoutError('unacknowledged provider invocation')
        ordinal = len(self.invocations)-1
        message = self.messages[ordinal]
        return {'body': io.BytesIO(json.dumps(message, ensure_ascii=False).encode()),
            'ResponseMetadata': {'RequestId': 'provider-original-'+str(ordinal)}}


def answer(content=None, *, stop='end_turn'):
    return {'model': 'fixture-returned', 'id': 'message-original', 'stop_reason': stop,
        'content': content or [{'type': 'text', 'text': 'The actual original response 😀'}],
        'usage': {'input_tokens': 100, 'output_tokens': 10}}


@pytest.fixture
def loop(runtime, server):
    journal = ExecutionJournal(runtime.durable, original(runtime))
    runtime.durable.update(runtime.durable.path, 'acquire-interactive-attempt', candidate_key=KEY,
        execution_id=E, target_id='target-original', workspace_id='workspace-original', turn_id='turn-original',
        owner_token='a'*32, expires_epoch=runtime.now+600)
    journal.append('admission', {'executionId': E, 'ownerToken': 'a'*32})
    provider = Provider([answer()])
    model = BedrockModel({'region': 'us-east-1', 'modelId': 'fixture-profile', 'returnedModelId': 'fixture-returned',
        'tokenCountingModelArn': 'arn:aws:bedrock:us-east-1::foundation-model/fixture-returned',
        'pricingDigest': BASIS, 'inputUsdPerMillion': '3', 'outputUsdPerMillion': '15', 'maxOutputTokens': 128}, client=provider)
    state = SimpleNamespace(lost_post=False, mutations=[], executions=0, recoveries=0, fail_action_once=False)
    def respond(method, path, raw):
        if method == 'POST':
            payload = json.loads(raw)
            state.mutations.append((path, payload))
            execution = runtime.value['execution']
            execution['stateRevision'] += 1
            if path.endswith('/final-response'):
                execution['finalMessageId'] = payload['messageId']
                runtime.value['finalMessage'] = {'id': payload['messageId'], 'turnId': payload['turnId'],
                    'role': 'assistant', 'content': payload['content'], 'evidenceKind': 'conversation'}
            elif path.endswith('/question'):
                execution['state'] = 'awaiting-input'
                execution['question'] = {'id': 'question-original', **{key: payload[key] for key in
                    ('providerRequestId', 'providerToolCallId', 'text', 'contextDigest')}}
            else: raise AssertionError(path)
            if state.lost_post: return None
        return 200, {}, json.dumps(runtime.value).encode()
    server.respond = respond
    runtime.value['execution']['state'] = 'running'
    class Dispatcher:
        capability = 'private-capability-'+'7'*32
        def execute(self, intent):
            state.executions += 1
            if state.fail_action_once: raise TimeoutError('tool reply acknowledgement lost')
            return {'nativeResponse': 'the actual retained tool fixture'}
        def recover(self, intent):
            state.recoveries += 1
            return {'nativeResponse': 'the actual retained tool fixture'}
    dispatcher = Dispatcher()
    config = {'maxTurns': 4, 'maxActions': 8, 'allowedActions': ['mcp_list_tools', 'mcp_call_tool', 'ask_user']}
    def build():
        return ExecutionLoop(native=runtime.control, journal=journal, original=original(runtime),
            model=model, dispatcher=dispatcher, configuration=config)
    return SimpleNamespace(runtime=runtime, provider=provider, model=model, state=state, journal=journal,
        dispatcher=dispatcher, config=config, build=build)


@pytest.mark.parametrize('lost_post', [False, True])
def test_genuine_final_bytes_cost_and_independent_readback_survive_lost_reply(loop, lost_post):
    loop.state.lost_post = lost_post
    result = loop.build().run()
    assert result['state'] == 'running'  # Conversation never invents execution completion.
    assert loop.runtime.value['finalMessage']['content'] == 'The actual original response 😀'
    assert len(loop.provider.invocations) == 1
    assert len(loop.journal.values('provider-response')) == len(loop.journal.values('model-response')) == 1
    charge = loop.runtime.durable.state['ledger']['interactiveEntries'][0]
    assert charge['actualUsd'] == '0.00045' and charge['state'] == 'settled'
    resumed = loop.build().run()
    assert resumed['state'] == result['state'] and resumed['messageId'] == result['messageId']
    assert resumed['native'] != result['native']  # A new independent GET has a new immutable original.
    assert loop.journal.values('final-response-observation') == [{'reference': result['native'], 'messageId': result['messageId']}]
    assert len(loop.provider.invocations) == len(loop.state.mutations) == 1


def test_unknown_paid_invocation_stays_reserved_and_is_never_invoked_again(loop):
    loop.provider.lose = True
    with pytest.raises(TimeoutError): loop.build().run()
    assert len(loop.provider.invocations) == 1
    assert loop.runtime.durable.state['ledger']['interactiveEntries'][0]['state'] == 'reserved'
    with pytest.raises(PackError, match='outcome unknown'): loop.build().run()
    assert len(loop.provider.invocations) == 1 and not loop.state.mutations


def test_unexpected_long_context_response_retains_raw_usage_and_unresolved_liability(loop):
    loop.provider.messages[0]['usage']['input_tokens'] = 200001
    with pytest.raises(ControllerError, match='actual provider usage'): loop.build().run()
    assert len(loop.journal.values('provider-response')) == 1
    assert not loop.journal.values('model-response')
    assert loop.runtime.durable.state['ledger']['interactiveEntries'][0]['state'] == 'reserved'
    with pytest.raises(PackError, match='unpriced'): loop.build().run()
    assert len(loop.provider.invocations) == 1 and not loop.state.mutations


def test_lost_settlement_ack_recovers_original_provider_without_second_charge(loop):
    update = loop.runtime.durable.update
    def lose(path, operation, **kwargs):
        value = update(path, operation, **kwargs)
        if operation == 'settle': raise TimeoutError('settlement acknowledgement lost')
        return value
    loop.runtime.durable.update = lose
    with pytest.raises(TimeoutError): loop.build().run()
    assert len(loop.provider.invocations) == 1 and loop.journal.values('provider-response')
    loop.runtime.durable.update = update
    loop.build().run()
    assert len(loop.provider.invocations) == 1
    assert len(loop.runtime.durable.state['ledger']['interactiveEntries']) == 1


def test_lost_tool_reply_recovers_same_provider_action_before_another_model_round(loop):
    loop.provider.messages = [answer([{'type': 'tool_use', 'id': 'call-original', 'name': 'mcp_list_tools', 'input': {}}], stop='tool_use'), answer()]
    loop.state.fail_action_once = True
    with pytest.raises(TimeoutError): loop.build().run()
    loop.state.fail_action_once = False
    loop.build().run()
    assert loop.state.executions == loop.state.recoveries == 1 and len(loop.provider.invocations) == 2
    second = json.loads(loop.provider.invocations[1]['body'])
    assert second['messages'][-1]['content'][0]['tool_use_id'] == 'call-original'
    assert len(loop.runtime.durable.state['ledger']['interactiveEntries']) == 2


def test_actual_provider_question_is_native_retained_and_not_a_final_or_approval(loop):
    loop.provider.messages = [answer([{'type': 'tool_use', 'id': 'question-call-original', 'name': 'ask_user',
        'input': {'question': 'Which source layer should I use?'}}], stop='tool_use')]
    loop.state.lost_post = True
    result = loop.build().run()
    assert result['state'] == 'awaiting-input' and result['questionId'] == 'question-original'
    question = loop.runtime.value['execution']['question']
    assert question['providerRequestId'] == 'provider-original-0' and question['providerToolCallId'] == 'question-call-original'
    assert not loop.journal.values('final-response-intent')
    resumed = loop.build().run()
    assert resumed['state'] == result['state'] and resumed['questionId'] == result['questionId']
    assert resumed['native'] != result['native']
    assert loop.journal.values('question-observation') == [{'question': question, 'reference': result['native']}]
    assert len(loop.provider.invocations) == 1


def test_reply_reconstructs_original_provider_question_only_after_original_retirement_and_settlement(loop):
    loop.provider.messages = [answer([{'type': 'tool_use', 'id': 'question-call-original', 'name': 'ask_user',
        'input': {'question': 'Which source layer should I use?'}}], stop='tool_use')]
    loop.build().run()
    for kind, amount in (('infrastructure', '0.1'), ('cleanup', '0.05')):
        rid = 'old-'+kind
        loop.runtime.durable.update(loop.runtime.durable.path, 'reserve-interactive', candidate_key=KEY,
            execution_id=E, target_id='target-original', workspace_id='workspace-original', turn_id='turn-original',
            owner_token='a'*32, reservation_id=rid, kind=kind, amount='1', basis_digest=BASIS)
        loop.runtime.durable.update(loop.runtime.durable.path, 'settle', candidate_key=KEY, reservation_id=rid, amount=amount)
    loop.runtime.value['retirement'] = {'phase': 'retired', 'executionId': E, 'requestDigest': BASIS}
    retired = original(loop.runtime)
    loop.journal.append('retirement-observation', {'reference': retired['reference'], 'retirement': copy.deepcopy(loop.runtime.value['retirement'])})
    next_id = 'execution-'+'6'*32
    loop.runtime.value.pop('retirement')
    loop.runtime.value['execution'] = {'executionId': next_id, 'state': 'running', 'stateRevision': 1}
    turn = loop.runtime.value['binding']['turn']
    turn.update(turnId='turn-reply', prompt='Use the original roads layer.', replyToQuestionId='question-original',
        replyToTurnId='turn-original', replyToExecutionId=E)
    observed = loop.runtime.control.read(next_id, 'workspace-original', 'turn-reply')
    journal = ExecutionJournal(loop.runtime.durable, observed)
    with pytest.raises(PackError, match='still holds unsettled'): original_messages(journal, turn)
    loop.runtime.durable.update(loop.runtime.durable.path, 'release-interactive-attempt', candidate_key=KEY,
        execution_id=E, owner_token='a'*32, cleanup_status='pass',
        reservation_ids={'infrastructure': 'old-infrastructure', 'cleanup': 'old-cleanup'})
    messages = original_messages(journal, turn)
    assert messages[0]['content'][0]['text'] == 'Actual fixture question'
    assert messages[-2]['role'] == 'assistant' and messages[-2]['content'][0]['id'] == 'question-call-original'
    assert messages[-1]['content'][0] == {'type': 'tool_result', 'tool_use_id': 'question-call-original',
        'content': 'Use the original roads layer.'}
    # Changing navigation or the original question never becomes a new blank
    # prompt or a reused approval; it breaks the explicit linkage.
    changed = copy.deepcopy(turn); changed['replyToQuestionId'] = 'unrelated-question'
    with pytest.raises(PackError, match='genuine provider question'): original_messages(journal, changed)


@pytest.mark.parametrize('change', ['pricing', 'operation-policy', 'original-response'])
def test_recovery_refuses_changed_original_model_policy_or_provider_response(loop, change):
    loop.build().run()
    if change == 'pricing': loop.model.config['inputUsdPerMillion'] = '4'
    elif change == 'operation-policy': loop.config['maxActions'] = 9
    else:
        loop.journal.values('provider-response')[0]['provider']['rawBodyBase64'] = 'e30='
    with pytest.raises(PackError): loop.build().run()
    assert len(loop.provider.invocations) == 1


def test_final_text_is_never_truncated_or_summarized(loop):
    loop.provider.messages = [answer([{'type': 'text', 'text': 'x'*16001}])]
    with pytest.raises(PackError, match='without truncation'): loop.build().run()
    assert not loop.state.mutations and loop.journal.values('provider-response')
    assert loop.runtime.durable.state['ledger']['interactiveEntries'][0]['state'] == 'settled'
