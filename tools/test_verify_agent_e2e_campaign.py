import pytest

import agent_e2e_gate as gate
import verify_agent_e2e_campaign as verifier


def verified_statement(path):
    return [{'verificationResult': {'statement': {'subject': [
        {'digest': {'sha256': gate.digest(path.read_bytes()).removeprefix('sha256:')}}]},
        'signature': {'certificate': {
            'runInvocationURI': 'https://github.com/honua-io/honua-release/actions/runs/123/attempts/2',
            'sourceRepositoryDigest': 'a' * 40,
            'sourceRepositoryURI': 'https://github.com/honua-io/honua-release',
            'runnerEnvironment': 'github-hosted',
            'buildSignerURI': 'https://github.com/honua-io/honua-release/.github/workflows/agent-e2e-campaign.yml@refs/heads/trunk'
        }}}}]


def test_verifier_executes_cryptographic_verification_with_exact_identity(tmp_path):
    path = tmp_path / 'inventory.json'
    path.write_text('{}')
    calls = []
    def invoke(args):
        calls.append(args)
        return verified_statement(path)
    verifier.authenticate(path, 'a' * 40, 123, 2, invoke=invoke)
    command = calls[0]
    assert command[:3] == ['gh', 'attestation', 'verify']
    assert command[command.index('--source-digest') + 1] == 'a' * 40
    assert command[command.index('--source-ref') + 1] == 'refs/heads/trunk'
    assert '--deny-self-hosted-runners' in command
    assert '--cert-identity' in command
    assert '--signer-workflow' not in command  # Mutually exclusive with exact certificate identity.


@pytest.mark.parametrize('field,value', [
    ('runInvocationURI', 'https://github.com/honua-io/honua-release/actions/runs/123/attempts/1'),
    ('sourceRepositoryDigest', 'b' * 40),
    ('sourceRepositoryURI', 'https://github.com/untrusted/fork'),
    ('runnerEnvironment', 'self-hosted'),
    ('buildSignerURI', 'https://github.com/honua-io/honua-release/.github/workflows/preflight.yml@refs/heads/trunk'),
])
def test_verified_other_producer_cannot_substitute(tmp_path, field, value):
    path = tmp_path / 'inventory.json'
    path.write_text('{}')
    output = verified_statement(path)
    output[0]['verificationResult']['signature']['certificate'][field] = value
    with pytest.raises(gate.Invalid):
        verifier.authenticate(path, 'a' * 40, 123, 2, invoke=lambda args: output)


def test_empty_verification_result_refuses(tmp_path):
    path = tmp_path / 'inventory.json'
    path.write_text('{}')
    with pytest.raises(gate.Invalid, match='no cryptographically verified'):
        verifier.authenticate(path, 'a' * 40, 123, 2, invoke=lambda args: [])
