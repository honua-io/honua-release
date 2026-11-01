import json
from types import SimpleNamespace

import pytest

from agent_e2e_local_recorder import ProjectionError
from agent_studio_provider_evidence import inspect_region


def test_provider_region_uses_only_an_approved_host_and_public_result():
    calls = []
    result = {'region': 'us-west-2', 'containerId': 'a' * 64, 'imageId': 'sha256:' + 'b' * 64}
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(result).encode())
    assert inspect_region('honua-eval-4', 'case-project', 'bedrock', run=run) == result
    assert calls[0][0][0] == 'ssh' and calls[0][0][-2] == 'honua-eval-4'
    with pytest.raises(ProjectionError, match='invalid'):
        inspect_region('example.com', 'case-project', 'bedrock', run=run)
    with pytest.raises(ProjectionError, match='invalid'):
        inspect_region('honua-eval-4', 'case-project', 'bedrock;bad', run=run)


def test_provider_region_fails_closed_on_missing_or_unverified_configuration():
    def failed(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout=b'')
    with pytest.raises(ProjectionError, match='could not be verified'):
        inspect_region('honua-eval-4', 'case-project', 'bedrock', run=failed)
    def malformed(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=b'{"region":"unknown"}')
    with pytest.raises(ProjectionError, match='malformed'):
        inspect_region('honua-eval-4', 'case-project', 'bedrock', run=malformed)
