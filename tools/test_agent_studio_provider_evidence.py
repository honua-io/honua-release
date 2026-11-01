import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent_e2e_local_recorder import ProjectionError
from agent_studio_provider_evidence import _INSPECT, inspect_region


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


def test_remote_inspection_emits_region_but_not_server_secrets(tmp_path):
    docker = tmp_path / 'docker'
    docker.write_text('#!' + sys.executable + '\n'
        'import json,sys\n'
        'if sys.argv[1] == "ps": print("a"*64)\n'
        'else: print(json.dumps([{"Id":"a"*64,"Image":"sha256:"+"b"*64,'
        '"Config":{"Env":["StudioAiProxy__Providers__bedrock__Region=us-west-2",'
        '"AWS_SECRET_ACCESS_KEY=do-not-disclose"],"Cmd":[],"Entrypoint":[]}}]))\n')
    docker.chmod(0o700)
    result = subprocess.run([sys.executable, '-c', _INSPECT, 'case-project', 'bedrock'],
        env={**os.environ, 'PATH': str(tmp_path)}, capture_output=True, check=True)
    assert b'do-not-disclose' not in result.stdout + result.stderr
    assert json.loads(result.stdout)['region'] == 'us-west-2'
