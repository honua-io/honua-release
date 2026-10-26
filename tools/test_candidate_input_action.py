"""Exercise the real adoption shell against local canonical-origin Git histories."""
from pathlib import Path
import os
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / '.github/actions/candidate-input/adopt.sh'
CANONICAL = 'https://github.com/honua-io/honua-release'


def git(directory, *args):
    return subprocess.check_output(['git', '-C', str(directory), *args], text=True,
                                   stderr=subprocess.DEVNULL).strip()


@pytest.fixture
def history(tmp_path):
    remote = tmp_path / 'origin.git'
    repo = tmp_path / 'release'
    git(tmp_path, 'init', '--bare', '-b', 'trunk', str(remote))
    git(tmp_path, 'init', '-b', 'trunk', str(repo))
    git(repo, 'config', 'user.name', 'Test')
    git(repo, 'config', 'user.email', 'test@example.invalid')
    git(repo, 'remote', 'add', 'origin', CANONICAL)
    # Git fetch is real; only transport is redirected to the isolated test remote.
    git(repo, 'config', f'url.{remote}.insteadOf', CANONICAL)
    for name in ('platform-manifest.yaml', 'compatibility-matrix.yaml'):
        (repo / name).write_text('original: true\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'reviewed trunk')
    parent = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'push', 'origin', 'trunk')
    return repo, parent, tmp_path


def snapshot(history, *, extra=None, namespace_sha=None):
    repo, parent, _ = history
    (repo / 'platform-manifest.yaml').write_text('candidate: true\n')
    if extra:
        (repo / extra).write_text('unreviewed executable\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'generated data snapshot')
    sha = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'push', 'origin', f'HEAD:refs/nightly-candidates/{namespace_sha or sha}')
    git(repo, 'reset', '--hard', parent)
    return sha


def adopt(history, candidate, *, reviewed=None, repository='honua-io/honua-release', run_sha=None,
          run_ref='refs/heads/trunk'):
    repo, parent, scratch = history
    env = {**os.environ, 'CANDIDATE_REF': candidate, 'RUN_SHA': run_sha or parent,
           'RUN_REF': run_ref, 'RUN_REPOSITORY': repository,
           'CHECKOUT_DIRECTORY': str(repo), 'REVIEWED_RELEASE_SHA': reviewed or '',
           'RUNNER_TEMP': str(scratch), 'GITHUB_OUTPUT': str(scratch / 'outputs')}
    return subprocess.run(['bash', str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)


def test_existing_train_default_adopts_snapshot_and_reports_exact_bytes(history):
    result = adopt(history, snapshot(history))
    assert result.returncode == 0, result.stderr
    repo, _, scratch = history
    assert (repo / 'platform-manifest.yaml').read_text() == 'candidate: true\n'
    assert 'manifest_sha256=' in (scratch / 'outputs').read_text()
    assert len(git(repo, 'worktree', 'list').splitlines()) == 1


def test_cross_repository_uses_real_reviewed_release_head_not_sdk_sha(history):
    result = adopt(history, snapshot(history), reviewed=history[1],
                   repository='honua-io/honua-sdk-js', run_sha='f' * 40)
    assert result.returncode == 0, result.stderr


def test_cross_repository_requires_explicit_release_context(history):
    result = adopt(history, snapshot(history), repository='honua-io/honua-sdk-js')
    assert result.returncode != 0
    assert 'require reviewed_release_sha' in result.stdout


def test_claimed_reviewed_sha_must_match_actual_checkout(history):
    result = adopt(history, snapshot(history), reviewed='f' * 40)
    assert result.returncode != 0
    assert (history[0] / 'platform-manifest.yaml').read_text() == 'original: true\n'


def test_other_origin_cannot_claim_release_lineage(history):
    sha = snapshot(history)
    git(history[0], 'remote', 'set-url', 'origin', 'https://github.com/other/honua-release')
    result = adopt(history, sha, reviewed=history[1])
    assert result.returncode != 0
    assert 'canonical honua-release origin' in result.stdout


def test_snapshot_cannot_change_executable_code(history):
    result = adopt(history, snapshot(history, extra='unreviewed.sh'), reviewed=history[1])
    assert result.returncode != 0
    assert 'unexpected candidate change: unreviewed.sh' in result.stdout
    assert (history[0] / 'platform-manifest.yaml').read_text() == 'original: true\n'


def test_candidate_namespace_must_resolve_to_named_commit(history):
    snapshot(history, namespace_sha='f' * 40)
    result = adopt(history, 'f' * 40, reviewed=history[1])
    assert result.returncode != 0


def test_release_checkout_must_be_canonical_trunk_ancestor(history):
    sha = snapshot(history)
    repo = history[0]
    (repo / 'unreviewed.txt').write_text('branch only\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'unreviewed side branch')
    result = adopt(history, sha, reviewed=git(repo, 'rev-parse', 'HEAD'))
    assert result.returncode != 0
    assert 'not reviewed canonical trunk history' in result.stdout


def test_snapshot_parent_must_belong_to_reviewed_checkout_history(history):
    repo, parent, _ = history
    (repo / 'branch.txt').write_text('not on trunk\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'unreviewed parent')
    branch_parent = git(repo, 'rev-parse', 'HEAD')
    sha = snapshot((repo, branch_parent, history[2]))
    git(repo, 'reset', '--hard', parent)
    result = adopt(history, sha, reviewed=parent)
    assert result.returncode != 0
    assert 'not reviewed trunk history' in result.stdout


def test_descendant_reviewed_trunk_accepts_an_older_snapshot_parent(history):
    repo, _, _ = history
    sha = snapshot(history)
    (repo / 'reviewed.txt').write_text('later trunk\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'later reviewed trunk')
    reviewed = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'push', 'origin', 'trunk')
    result = adopt(history, sha, reviewed=reviewed)
    assert result.returncode == 0, result.stderr


def test_merge_candidate_cannot_hide_a_second_parent(history):
    repo, parent, _ = history
    git(repo, 'switch', '-c', 'side')
    (repo / 'compatibility-matrix.yaml').write_text('side: true\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'side input')
    git(repo, 'switch', 'trunk')
    (repo / 'platform-manifest.yaml').write_text('main: true\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'main input')
    git(repo, 'merge', '--no-ff', 'side', '-m', 'two-parent candidate')
    sha = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'push', 'origin', f'HEAD:refs/nightly-candidates/{sha}')
    git(repo, 'reset', '--hard', parent)
    result = adopt(history, sha, reviewed=parent)
    assert result.returncode != 0
    assert 'exactly one parent' in result.stdout


def test_symlinked_manifest_is_refused_before_adoption(history):
    repo, parent, _ = history
    (repo / 'platform-manifest.yaml').unlink()
    (repo / 'platform-manifest.yaml').symlink_to('compatibility-matrix.yaml')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'symlink candidate manifest')
    sha = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'push', 'origin', f'HEAD:refs/nightly-candidates/{sha}')
    git(repo, 'reset', '--hard', parent)
    result = adopt(history, sha, reviewed=parent)
    assert result.returncode != 0
    assert 'must be a regular file: platform-manifest.yaml' in result.stdout
    assert (repo / 'platform-manifest.yaml').read_text() == 'original: true\n'


def test_default_non_trunk_run_preserves_mismatched_parent_refusal(history):
    sha = snapshot(history)
    result = adopt(history, sha, run_sha='f' * 40, run_ref='refs/tags/honua-2026.1')
    assert result.returncode != 0
    assert "not this run's commit" in result.stdout


def test_versioned_deployment_bundle_is_copied_byte_for_byte(history):
    repo, _, _ = history
    (repo / 'certification').mkdir()
    bundle = b'{"schema":"honua.deployment-qualification-bundle/v1","records":{}}\n'
    (repo / 'certification/deployment-qualification.json').write_bytes(bundle)
    candidate = snapshot(history)
    result = adopt(history, candidate)
    assert result.returncode == 0, result.stderr
    assert (repo / 'certification/deployment-qualification.json').read_bytes() == bundle
    # Adoption does not treat an empty bundle as qualification; the validator owns admission.


def test_snapshot_without_deployment_bundle_removes_stale_local_proof(history):
    candidate = snapshot(history)
    repo, _, _ = history
    (repo / 'certification').mkdir()
    proof = repo / 'certification/deployment-qualification.json'
    proof.write_text('{"stale":true}\n')
    result = adopt(history, candidate)
    assert result.returncode == 0, result.stderr
    assert not proof.exists()


def test_symlinked_deployment_bundle_refused_before_any_adoption(history):
    repo, _, _ = history
    (repo / 'certification').mkdir()
    (repo / 'certification/deployment-qualification.json').symlink_to('../platform-manifest.yaml')
    candidate = snapshot(history)
    result = adopt(history, candidate)
    assert result.returncode != 0
    assert (repo / 'platform-manifest.yaml').read_text() == 'original: true\n'


def test_windows_git_toplevel_spelling_still_adopts(history, tmp_path_factory):
    """gate-dr runs candidate-input on windows-latest, where git.exe reports the toplevel as
    `D:/a/...` while Git Bash's `pwd -P` says `/d/a/...`. The nightly train's gate_dr passes a
    candidate_ref, so the action must accept the checkout root however git spells it."""
    shim = tmp_path_factory.mktemp('windows-git')
    real = subprocess.check_output(['bash', '-c', 'command -v git'], text=True).strip()
    (shim / 'git').write_text(
        '#!/bin/bash\n'
        'if [ "$*" = "rev-parse --show-toplevel" ]; then\n'
        f'  top="$("{real}" rev-parse --show-toplevel)"; echo "D:${{top}}"; exit 0\n'
        'fi\n'
        f'exec "{real}" "$@"\n')
    (shim / 'git').chmod(0o755)
    repo, parent, scratch = history
    candidate = snapshot(history)
    env = {**os.environ, 'PATH': f"{shim}{os.pathsep}{os.environ['PATH']}", 'CANDIDATE_REF': candidate,
           'RUN_SHA': parent, 'RUN_REF': 'refs/heads/trunk', 'RUN_REPOSITORY': 'honua-io/honua-release',
           'CHECKOUT_DIRECTORY': str(repo), 'REVIEWED_RELEASE_SHA': '', 'RUNNER_TEMP': str(scratch),
           'GITHUB_OUTPUT': str(scratch / 'outputs')}
    result = subprocess.run(['bash', str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert (repo / 'platform-manifest.yaml').read_text() == 'candidate: true\n'


def test_subdirectory_is_still_refused_as_a_checkout_root(history):
    repo, parent, scratch = history
    (repo / 'nested').mkdir()
    env = {**os.environ, 'CANDIDATE_REF': snapshot(history), 'RUN_SHA': parent, 'RUN_REF': 'refs/heads/trunk',
           'RUN_REPOSITORY': 'honua-io/honua-release', 'CHECKOUT_DIRECTORY': str(repo / 'nested'),
           'REVIEWED_RELEASE_SHA': '', 'RUNNER_TEMP': str(scratch), 'GITHUB_OUTPUT': str(scratch / 'outputs')}
    result = subprocess.run(['bash', str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert (repo / 'platform-manifest.yaml').read_text() == 'original: true\n'


def test_adoption_shell_is_checked_out_with_lf_on_windows_runners():
    """windows-latest checks out with core.autocrlf=true; a CRLF adopt.sh dies on `pipefail\\r`."""
    root = SCRIPT.parents[3]
    for path in ('.github/actions/candidate-input/adopt.sh', '.github/actions/candidate-input/action.yml'):
        attributes = subprocess.check_output(['git', '-C', str(root), 'check-attr', 'eol', '--', path], text=True)
        assert attributes.strip().endswith('eol: lf'), attributes
