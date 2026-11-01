import json

import pytest

from agent_e2e_studio_customer_session import run_customer
from agent_studio_customer import CustomerScriptError


def state(text, number, **overrides):
    return json.dumps({'assistantMessages': number, 'modelCalls': number, 'dispatchedToolCalls': 0,
                       'streaming': False, 'boundHit': False, 'status': 'complete', 'text': text, **overrides})


def test_customer_sends_only_the_request_and_frozen_clarifications():
    states = iter([state('Earlier answer', 1), state('Which layers?', 2), state('Which style?', 3),
                   state('Saved and done.', 4)])
    actions = []
    def invoke(name, arguments):
        actions.append((name, arguments))
        return next(states) if name == 'studio_chat_observe' else 'ok'
    result = run_customer(invoke=invoke, prompt='Make a map', script=[
        {'after': 'first-reply', 'say': 'Frozen first reply'}, {'after': 'question', 'say': 'Frozen answer'}],
        sleep=lambda _: None)
    assert [args['text'] for name, args in actions if name == 'studio_customer_type'] == [
        'Make a map', 'Frozen first reply', 'Frozen answer']
    assert result['answer'] == 'Saved and done.' and result['turns'] == 3 and result['nudges'] == 0


def test_customer_rejects_unobserved_bounds_and_unanswered_questions():
    calls = 0
    def invoke(name, arguments):
        nonlocal calls
        if name != 'studio_chat_observe':
            return 'ok'
        calls += 1
        return state('Earlier answer', 0) if calls == 1 else state('Another question?', 1)
    with pytest.raises(CustomerScriptError, match='no frozen answer'):
        run_customer(invoke=invoke, prompt='Make a map', script=[], sleep=lambda _: None)
    calls = 0
    def bounded(name, arguments):
        nonlocal calls
        if name != 'studio_chat_observe':
            return 'ok'
        calls += 1
        return state('', 0) if calls == 1 else state('Working', 1, boundHit=True)
    with pytest.raises(CustomerScriptError, match='bound exceeded'):
        run_customer(invoke=bounded, prompt='Make a map', script=[], sleep=lambda _: None)
