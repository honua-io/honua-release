from pathlib import Path

import pytest

from ga_actions import _GATE, gate_functions, invoke

PRIOR = 'ghcr.io/honua-io/honua-server@sha256:' + 'a' * 64
CANDIDATE = 'ghcr.io/honua-io/honua-server@sha256:' + 'b' * 64
PROJECT = 'ops-' + 'a' * 32


def test_gate_functions_are_taken_from_real_gate():
    source = _GATE.read_text()
    functions = gate_functions(source)
    assert 'cu_restore_backup() {' in functions
    assert 'cu_start_server() {' in functions
    assert 'run_compose_upgrade_rollback() {' not in functions
    with pytest.raises(ValueError, match='missing or ambiguous'):
        gate_functions(source.replace('cu_schema() {', 'missing_schema() {'))


def test_backup_is_scoped_to_trial_compose_and_fails_closed(tmp_path, monkeypatch):
    compose = tmp_path / 'compose.yml'
    compose.write_text('services: {}\n')
    log = tmp_path / 'calls'
    docker = tmp_path / 'docker'
    docker.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$GA_TEST_CALLS"
printf 'real dump bytes'
''')
    docker.chmod(0o755)
    monkeypatch.setenv('PATH', f'{tmp_path}:' + __import__('os').environ['PATH'])
    monkeypatch.setenv('GA_TEST_CALLS', str(log))
    params = dict(project=PROJECT, compose_file=compose, output=tmp_path,
                  prior_image=PRIOR, candidate_image=CANDIDATE, base_url='http://127.0.0.1:1')
    result = invoke(operation='backup', **params)
    assert result['ok'] is True
    assert (tmp_path / 'compose-pre-upgrade.dump').read_text() == 'real dump bytes'
    assert f'compose -p {PROJECT} -f {compose}' in log.read_text()
    assert invoke(operation='backup', **params)['ok'] is False
    with pytest.raises(ValueError, match='project'):
        invoke(operation='backup', **{**params, 'project': 'production'})
    with pytest.raises(ValueError, match='immutable'):
        invoke(operation='backup', **{**params, 'candidate_image': PRIOR})


def test_failed_restore_never_starts_prior(tmp_path, monkeypatch):
    import hashlib
    import os

    compose = tmp_path / 'compose.yml'
    compose.write_text('services: {}\n')
    backup = tmp_path / 'compose-pre-upgrade.dump'
    backup.write_bytes(b'backup')
    log = tmp_path / 'calls'
    docker = tmp_path / 'docker'
    docker.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$GA_TEST_CALLS"
case "$*" in
  *pg_restore*) exit 1 ;;
  *'ps -q --status running'*) exit 0 ;;
  *postgis_full_version*) echo postgis ;;
esac
exit 0
''')
    docker.chmod(0o755)
    monkeypatch.setenv('PATH', f'{tmp_path}:' + os.environ['PATH'])
    monkeypatch.setenv('GA_TEST_CALLS', str(log))
    result = invoke(operation='restore-prior', project=PROJECT, compose_file=compose,
                    output=tmp_path, prior_image=PRIOR, candidate_image=CANDIDATE,
                    base_url='http://127.0.0.1:1',
                    expected_backup_sha256=hashlib.sha256(b'backup').hexdigest())
    assert result['ok'] is False
    assert 'up -d --force-recreate' not in log.read_text()
    with pytest.raises(ValueError, match='digest mismatch'):
        invoke(operation='restore-prior', project=PROJECT, compose_file=compose,
               output=tmp_path, prior_image=PRIOR, candidate_image=CANDIDATE,
               base_url='http://127.0.0.1:1', expected_backup_sha256='0' * 64)


def test_unready_trial_container_cannot_use_other_ready_server(tmp_path, monkeypatch):
    import os

    compose = tmp_path / 'compose.yml'
    compose.write_text('services: {}\n')
    docker = tmp_path / 'docker'
    docker.write_text('''#!/bin/sh
case "$*" in
  *'ps -q --status running server'*) echo trial-container ;;
  exec*) exit 1 ;;
  *'logs server'*) echo 'no migration approval' ;;
esac
exit 0
''')
    docker.chmod(0o755)
    monkeypatch.setenv('PATH', f'{tmp_path}:' + os.environ['PATH'])
    monkeypatch.setenv('CHAOS_READY_ATTEMPTS', '1')
    result = invoke(operation='start-candidate', project=PROJECT, compose_file=compose,
                    output=tmp_path, prior_image=PRIOR, candidate_image=CANDIDATE,
                    base_url='http://127.0.0.1:1', timeout=10)
    assert result['ok'] is False
