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


def test_customer_stalls_once_without_steering_and_then_stops():
    now = [0.0]
    actions = []
    def clock():
        return now[0]
    def sleep(_seconds):
        now[0] += 61
    def invoke(name, arguments):
        actions.append((name, arguments))
        return state('', 0) if name == 'studio_chat_observe' else 'ok'
    with pytest.raises(CustomerScriptError, match='stalled after the continuation'):
        run_customer(invoke=invoke, prompt='Make a map', script=[], timeout=2700, clock=clock, sleep=sleep)
    texts = [args['text'] for name, args in actions if name == 'studio_customer_type']
    assert texts == ['Make a map', 'continue']


def test_customer_does_not_treat_an_explicit_noncompletion_as_done():
    now = [0.0]
    states = iter([state('', 0), state('I have not saved the map.', 1), state('Saved and done.', 2)])
    actions = []
    def invoke(name, arguments):
        actions.append((name, arguments))
        return next(states) if name == 'studio_chat_observe' else 'ok'
    result = run_customer(invoke=invoke, prompt='Make a map',
        script=[{'after': 'first-reply', 'say': 'Frozen response'}],
        clock=lambda: now[0], sleep=lambda _: now.__setitem__(0, now[0] + 1))
    assert result['answer'] == 'Saved and done.'
    assert [args['text'] for name, args in actions if name == 'studio_customer_type'] == [
        'Make a map', 'Frozen response']
