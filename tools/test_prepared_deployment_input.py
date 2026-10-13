"""Real Git/adoption controls; synthetic snapshots never certify deployments."""
import json
import os
from pathlib import Path
import subprocess

from test_candidate_input_action import git, history  # noqa: F401

SCRIPT = Path(__file__).resolve().parents[1] / '.github/actions/prepared-deployment-input/adopt.sh'


def snapshot(history, *, extra=None, status='unadmitted', qualified=False, namespace='prepared-deployments'):
    repo, parent, _ = history
    (repo / 'platform-manifest.yaml').write_text('prepared: true\n')
    (repo / 'deployment-preparation.json').write_text(json.dumps({
        'schema': 'honua.deployment-preparation/v1', 'status': status, 'candidateQualified': qualified}))
    if extra:
        (repo / extra).write_text('not admitted\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'synthetic prepared inputs')
    sha = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'push', 'origin', f'HEAD:refs/{namespace}/{sha}')
    git(repo, 'reset', '--hard', parent)
    return sha


def adopt(history, sha, candidate=''):
    repo, parent, scratch = history
    env = {**os.environ, 'PREPARED_REF': sha, 'CANDIDATE_REF': candidate, 'RUN_SHA': parent,
           'RUN_REF': 'refs/heads/trunk', 'RUN_REPOSITORY': 'honua-io/honua-release',
           'CHECKOUT_DIRECTORY': str(repo), 'RUNNER_TEMP': str(scratch),
           'GITHUB_OUTPUT': str(scratch / 'outputs'), 'REVIEWED_RELEASE_SHA': ''}
    return subprocess.run(['bash', str(SCRIPT)], env=env, text=True, capture_output=True, timeout=30)


def test_prepared_data_adopted_without_candidate_claim(history):
    result = adopt(history, snapshot(history))
    assert result.returncode == 0, result.stderr
    assert json.loads((history[0] / 'deployment-preparation.json').read_text())['candidateQualified'] is False
    assert len(git(history[0], 'worktree', 'list').splitlines()) == 1


def test_candidate_namespace_is_not_prepared_namespace(history):
    result = adopt(history, snapshot(history, namespace='nightly-candidates'))
    assert result.returncode != 0
    assert (history[0] / 'platform-manifest.yaml').read_text() == 'original: true\n'


def test_prepared_snapshot_cannot_change_executable_files(history):
    result = adopt(history, snapshot(history, extra='untrusted.sh'))
    assert result.returncode != 0
    assert 'unexpected prepared deployment change' in result.stdout


def test_prepared_snapshot_cannot_claim_qualification(history):
    result = adopt(history, snapshot(history, status='qualified', qualified=True))
    assert result.returncode != 0
    assert 'explicitly unadmitted' in result.stderr


def test_prepared_and_candidate_inputs_are_mutually_exclusive(history):
    result = adopt(history, snapshot(history), candidate='a' * 40)
    assert result.returncode != 0
    assert 'mutually exclusive' in result.stdout
