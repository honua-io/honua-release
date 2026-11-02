import pytest

from agent_studio_customer import CustomerScript, CustomerScriptError


def test_customer_uses_only_frozen_turns_and_one_continuation():
    policy = CustomerScript([{'after': 'first-reply', 'say': 'First frozen reply'},
                             {'after': 'question', 'say': 'Frozen clarification'}])
    assert policy.reply('progress') == {'kind': 'send', 'text': 'First frozen reply', 'continuation': False}
    assert policy.reply('question') == {'kind': 'send', 'text': 'Frozen clarification', 'continuation': False}
    assert policy.reply('progress') == {'kind': 'wait'}
    assert policy.reply('stall') == {'kind': 'send', 'text': 'continue', 'continuation': True}
    with pytest.raises(CustomerScriptError, match='stalled'):
        policy.reply('stall')
    assert policy.reply('done') == {'kind': 'done'}


def test_customer_refuses_to_invent_a_clarification():
    with pytest.raises(CustomerScriptError, match='no frozen answer'):
        CustomerScript([]).reply('question')
    with pytest.raises(CustomerScriptError, match='invalid'):
        CustomerScript([{'after': 'question', 'say': '', 'extra': 'steering'}])
