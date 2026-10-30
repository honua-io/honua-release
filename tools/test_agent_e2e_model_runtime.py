"""Original profile bindings and private execution contracts; no inference calls."""
import copy
from decimal import Decimal
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

import agent_e2e_campaign as producer
from agent_e2e_controller import BedrockModel, ControllerError
from agent_e2e_pack import PackError, digest_bytes, load_json

sys.path.insert(0, str(Path(__file__).parent/'hosted_pilot'))
from controller_entry import approved_model_environment
from controller_handoff import model_runtime

PROFILE = Path(__file__).resolve().parents[1]/'certification/agent-e2e/model-profile.json'
PRICING = PROFILE.with_name('model-pricing.json')


def configuration():
    return {**load_json(PRICING), 'pricingDigest': digest_bytes(PRICING.read_bytes()), 'maxOutputTokens': 1024}


def pin(model):
    return {name: model[name] for name in ('modelId', 'returnedModelId', 'profileArn', 'tokenCountingModelArn')} | {
        'profileSha256': model['profile']['sha256']}


def test_original_aws_profile_binds_explicit_counting_member_without_name_conversion():
    model = configuration()
    producer.validate_model_profile(model, load_json(PROFILE))
    model['modelId'] = model['profileArn']
    producer.validate_model_profile(model, load_json(PROFILE))


@pytest.mark.parametrize('change', ['wrong-profile', 'wrong-arn', 'missing-member', 'duplicate-member',
    'inactive', 'cross-region', 'profile-as-counter', 'unrelated-tokenizer'])
def test_changed_profile_or_tokenizer_is_refused(change):
    model, profile = configuration(), load_json(PROFILE)
    if change == 'wrong-profile': model['modelId'] = 'another-profile'
    elif change == 'wrong-arn': model['profileArn'] = profile['inferenceProfileArn']+'-other'
    elif change == 'missing-member': profile['models'] = []
    elif change == 'duplicate-member': profile['models'].append(copy.deepcopy(profile['models'][0]))
    elif change == 'inactive': profile['status'] = 'INACTIVE'
    elif change == 'cross-region': model['tokenCountingModelArn'] = profile['models'][1]['modelArn']
    elif change == 'profile-as-counter': model['tokenCountingModelArn'] = profile['inferenceProfileArn']
    else: model['tokenCountingModelArn'] = 'arn:aws:bedrock:us-east-1::foundation-model/anthropic.unrelated-v1:0'
    with pytest.raises(PackError): producer.validate_model_profile(model, profile)


def test_quote_counts_original_request_with_exact_regional_member_and_preserves_invocation_profile():
    config = configuration()
    client = Mock()
    client.count_tokens.return_value = {'inputTokens': 17}
    model = BedrockModel(config, client=client)
    body, maximum = model.quote(model.request([{'role': 'user', 'content': 'Original prompt'}]))
    client.count_tokens.assert_called_once_with(modelId=config['tokenCountingModelArn'], input={'invokeModel': {'body': body}})
    assert model.config['modelId'] == config['modelId'] != config['tokenCountingModelArn']
    assert maximum > 0
    client.invoke_model.assert_not_called()


def test_no_profile_fallback_or_guessed_token_counter():
    model = configuration()
    del model['tokenCountingModelArn']
    client = Mock()
    with pytest.raises(ControllerError, match='token-counting'): BedrockModel(model, client=client)
    client.count_tokens.assert_not_called()


@pytest.mark.parametrize('count', [200000, 200001])
def test_actual_geo_standard_prices_stop_at_supported_input_boundary_before_invoke(count):
    config = configuration()
    assert config['inputUsdPerMillion'] == '3.30' and config['outputUsdPerMillion'] == '16.50'
    assert config['returnedModelId'] == 'claude-sonnet-4-6'
    assert config['modelId'] == 'us.anthropic.claude-sonnet-4-6'
    client = Mock()
    client.count_tokens.return_value = {'inputTokens': count}
    model = BedrockModel(config, client=client)
    request = model.request([{'role': 'user', 'content': 'Original source pricing boundary'}])
    if count == 200000:
        _, maximum = model.quote(request)
        assert maximum == Decimal('0.676896')
    else:
        with pytest.raises(ControllerError, match='no long-context pricing fallback'): model.quote(request)
    client.invoke_model.assert_not_called()


@pytest.mark.parametrize('field,value', [('promptCaching', True), ('batch', True),
    ('inputTokenLimit', 200001), ('serviceTier', 'priority')])
def test_unpriced_usage_modes_cannot_enter_standard_quote(field, value):
    config = configuration()
    config['pricingScope'][field] = value
    client = Mock()
    with pytest.raises(ControllerError, match='pricing scope'): BedrockModel(config, client=client)
    client.count_tokens.assert_not_called(); client.invoke_model.assert_not_called()


def test_private_child_requires_exact_installed_and_frozen_model_identity():
    model = configuration()
    runtime = {'region': model['region'], 'accountId': '585192672263', 'model': pin(model)}
    frozen = {name: model[name] for name in ('modelId', 'returnedModelId')}
    environment = model_runtime(runtime['model'], model['region'], runtime['accountId'])
    assert approved_model_environment(runtime, frozen, {'model': model}, environment) == environment


@pytest.mark.parametrize('change', ['missing-env', 'changed-env', 'counting-model', 'profile-hash',
    'frozen-model', 'returned-model', 'missing-pin', 'extra-authority', 'wrong-account'])
def test_private_child_cannot_select_another_model_or_bypass_installed_runtime(change):
    model = configuration()
    runtime = {'region': model['region'], 'accountId': '585192672263', 'model': pin(model)}
    frozen = {name: model[name] for name in ('modelId', 'returnedModelId')}
    environment = model_runtime(runtime['model'], model['region'], runtime['accountId'])
    if change == 'missing-env': environment.pop('TERMINAL_MODEL_NAME')
    elif change == 'changed-env': environment['TERMINAL_MODEL_NAME'] = 'other'
    elif change == 'counting-model': model['tokenCountingModelArn'] += '-other'
    elif change == 'profile-hash': model['profile']['sha256'] = 'sha256:'+'b'*64
    elif change == 'frozen-model': frozen['modelId'] = 'other'
    elif change == 'returned-model': model['returnedModelId'] = 'other'
    elif change == 'missing-pin': runtime.pop('model')
    elif change == 'wrong-account': runtime['accountId'] = '123456789012'
    else: runtime['model']['arbitraryEnvironment'] = 'override'
    with pytest.raises(ValueError): approved_model_environment(runtime, frozen, {'model': model}, environment)
