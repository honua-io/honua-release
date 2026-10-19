"""Execute the actual preparation publisher and adoption shell against real local Git."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from test_candidate_input_action import git, history  # noqa: F401
from test_nightly_certification import step, workflow
from test_prepared_deployment_input import adopt


SERVER = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64


def prepare(history):
    repo, parent, scratch = history
    directory = repo / 'resolved-candidate'
    directory.mkdir()
    resolved = {'components': {'honua-server': {'sha': SERVER, 'digest': DIGEST,
                'image': 'ghcr.io/honua-io/honua-server@' + DIGEST}}}
    (directory / 'platform-manifest.yaml').write_text(yaml.safe_dump(resolved))
    (directory / 'compatibility-matrix.yaml').write_text('resolved: true\n')
    (directory / 'deployment-preparation.json').write_text(json.dumps({
        'schema': 'honua.deployment-preparation/v1', 'status': 'unadmitted',
        'candidateQualified': False}))
    # actions/setup-python supplies `python`; model that exact executable name locally.
    binaries = scratch / 'python-bin'
    binaries.mkdir()
    (binaries / 'python').symlink_to(sys.executable)
    env = {**os.environ, 'PATH': str(binaries) + os.pathsep + os.environ['PATH'],
           'GITHUB_SHA': parent, 'GITHUB_OUTPUT': str(scratch / 'publish-output')}
    return directory, env, resolved


def publish(history, env):
    script = step(workflow()['jobs']['resolve'], 'Publish unadmitted resolved inputs')['run']
    return subprocess.run(['bash', '--noprofile', '--norc', '-c', script], cwd=history[0],
                          env=env, capture_output=True, text=True, timeout=30)


def test_real_preparation_publisher_preserves_reviewed_code_and_capacity_envelope(history):
    repo, _, scratch = history
    (repo / 'certification').mkdir()
    (repo / 'certification/capacity-envelope.v1.json').write_text('{"frozen":"unchanged"}\n')
    (repo / 'producer.py').write_text('# reviewed executable\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'reviewed capacity policy')
    git(repo, 'push', 'origin', 'trunk')
    parent = git(repo, 'rev-parse', 'HEAD')
    history = repo, parent, scratch
    _, env, resolved = prepare(history)
    result = publish(history, env)
    assert result.returncode == 0, result.stderr
    sha = Path(env['GITHUB_OUTPUT']).read_text().strip().split('=', 1)[1]
    assert git(repo, 'rev-list', '--parents', '-n', '1', sha).split() == [sha, parent]
    assert set(git(repo, 'diff', '--name-only', parent, sha).splitlines()) == {
        'platform-manifest.yaml', 'compatibility-matrix.yaml', 'deployment-preparation.json'}
    for path in ('producer.py', 'certification/capacity-envelope.v1.json'):
        assert git(repo, 'show', f'{sha}:{path}') == git(repo, 'show', f'{parent}:{path}')
    actual = yaml.safe_load(git(repo, 'show', f'{sha}:platform-manifest.yaml'))
    assert actual == resolved
    assert yaml.safe_load(git(repo, 'show', f'{parent}:platform-manifest.yaml')) != resolved
    assert git(repo, 'ls-remote', 'origin', f'refs/prepared-deployments/{sha}').split()[0] == sha
    assert not git(repo, 'ls-remote', 'origin', 'refs/nightly-candidates/*')
    git(repo, 'reset', '--hard', parent)
    admitted = adopt(history, sha)
    assert admitted.returncode == 0, admitted.stdout + admitted.stderr
    assert yaml.safe_load((repo / 'platform-manifest.yaml').read_text()) == resolved
    assert json.loads((repo / 'deployment-preparation.json').read_text())['candidateQualified'] is False


@pytest.mark.parametrize('fault', ['qualified', 'staged-code', 'wrong-parent', 'symlink'])
def test_preparation_publisher_refuses_unreviewed_or_qualified_inputs(history, fault):
    repo, _, _ = history
    directory, env, _ = prepare(history)
    if fault == 'qualified':
        path = directory / 'deployment-preparation.json'
        marker = json.loads(path.read_text())
        marker['candidateQualified'] = True
        path.write_text(json.dumps(marker))
    elif fault == 'staged-code':
        (repo / 'unreviewed.sh').write_text('exit 0\n')
        git(repo, 'add', 'unreviewed.sh')
    elif fault == 'wrong-parent':
        env['GITHUB_SHA'] = 'f' * 40
    else:
        path = directory / 'platform-manifest.yaml'
        path.unlink()
        path.symlink_to(repo / 'platform-manifest.yaml')
    result = publish(history, env)
    assert result.returncode != 0
    assert not Path(env['GITHUB_OUTPUT']).exists()
    assert not git(repo, 'ls-remote', 'origin', 'refs/prepared-deployments/*')
