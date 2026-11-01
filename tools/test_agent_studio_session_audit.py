import pytest

from agent_e2e_local_recorder import ProjectionError
from agent_studio_session_audit import assistant_usage, controller_actors, snapshot, verify, verify_capabilities


class Recorder:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.requests = []

    def json(self, step, method, path):
        self.requests.append((step, method, path))
        return {'observation': {'httpStatus': 200, 'body': next(self.pages)}}


def test_audit_snapshots_page_and_reject_controller_activity():
    before = snapshot(Recorder([{'items': [{'auditId': 1, 'actor': 'admin'}], 'nextCursor': None}]), 'before')
    recorder = Recorder([
        {'items': [{'auditId': 3, 'actor': 'admin'}], 'nextCursor': 'next+page'},
        {'items': [{'auditId': 2, 'actor': 'customer'}, {'auditId': 1, 'actor': 'admin'}], 'nextCursor': None},
    ])
    after = snapshot(recorder, 'after')
    assert recorder.requests[1][2].endswith('cursor=next%2Bpage')
    with pytest.raises(ProjectionError, match='controller principal appeared'):
        verify(before, after, frozenset({'admin'}))
    after[3]['actor'] = 'customer'
    verify(before, after, frozenset({'admin'}))


def test_audit_snapshots_reject_incomplete_or_mutated_evidence():
    with pytest.raises(ProjectionError, match='baseline'):
        verify({1: {'actor': 'admin'}}, {}, frozenset({'admin'}))
    with pytest.raises(ProjectionError, match='baseline'):
        verify({1: {'actor': 'admin'}}, {1: {'actor': 'customer'}}, frozenset({'admin'}))
    with pytest.raises(ProjectionError, match='cursor'):
        snapshot(Recorder([{'items': [], 'nextCursor': 'x'}, {'items': [], 'nextCursor': 'x'}]), 'after')
    assert snapshot(Recorder([{'items': []}]), 'after') == {}
    with pytest.raises(ProjectionError, match='unavailable'):
        snapshot(Recorder([{}]), 'after')


def test_managed_controller_identity_is_matched_from_server_inventory():
    key_id = '12345678-1234-1234-1234-123456789abc'
    recorder = Recorder([{'data': [{'id': key_id, 'keyPrefix': 'hnua_example'}]}])
    actors = controller_actors(recorder, 'hnua_example-secret')
    with pytest.raises(ProjectionError, match='controller principal appeared'):
        verify({}, {1: {'actor': 'apikey:api-key:' + key_id}}, actors)


def test_assistant_usage_requires_the_customer_and_actual_bedrock_model():
    model = 'us.anthropic.claude-sonnet-4-6'
    user = 'subject:issuer:customer@tenant:public'
    event = {'actor': 'bearer:subject:issuer:customer', 'resourceType': 'studio_ai_provider',
             'action': 'studio_ai.chat', 'outcome': 'Success',
             'details': '{"kind":"bedrock","model":"' + model + '","promptTokens":10,"completionTokens":4}'}
    assert assistant_usage({}, {1: event}, user_id=user, subject='customer', model=model, observed_calls=1) == [
        {'prompt_tokens': 10, 'completion_tokens': 4}]
    with pytest.raises(ProjectionError, match='customer'):
        assistant_usage({}, {1: {**event, 'actor': 'other'}}, user_id=user, subject='customer',
                        model=model, observed_calls=1)
    with pytest.raises(ProjectionError, match='every model call'):
        assistant_usage({}, {1: event}, user_id=user, subject='customer', model=model, observed_calls=2)
    with pytest.raises(ProjectionError, match='model or usage'):
        assistant_usage({}, {1: event}, user_id=user, subject='customer', model='another-model', observed_calls=1)


def test_effective_default_provider_must_be_configured_bedrock_sonnet():
    model = 'us.anthropic.claude-sonnet-4-6'
    provider = {'provider': 'default', 'kind': 'bedrock', 'model': model, 'isDefault': True,
                'configured': True, 'toolSupport': True}
    def recorder(row):
        return Recorder([{'data': {'enabled': True, 'defaultProvider': 'default', 'providers': [row]}}])
    assert verify_capabilities(recorder(provider), model) == 'default'
    with pytest.raises(ProjectionError, match='required configured'):
        verify_capabilities(recorder({**provider, 'model': 'another-model'}), model)
    with pytest.raises(ProjectionError, match='required configured'):
        verify_capabilities(recorder({**provider, 'configured': False}), model)


def test_assistant_usage_accepts_only_the_minted_customer_key():
    model = 'us.anthropic.claude-sonnet-4-6'
    key = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
    user = 'subject:issuer:customer@tenant:public'
    event = {'actor': 'apikey:api-key:' + key, 'resourceType': 'studio_ai_provider',
             'action': 'studio_ai.chat', 'outcome': 'Success',
             'details': '{"kind":"bedrock","model":"' + model + '","promptTokens":10,"completionTokens":4}'}
    assert assistant_usage({}, {1: event}, user_id=user, subject='customer', model=model,
                           observed_calls=1, agent_key_id=key)
    with pytest.raises(ProjectionError, match='customer'):
        assistant_usage({}, {1: event}, user_id=user, subject='customer', model=model,
                        observed_calls=1, agent_key_id='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb')
