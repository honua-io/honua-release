from pathlib import Path

from real_controller import RealController


def test_real_action_selection_and_unsafe_attempts_are_audited():
    calls = []
    def execute(**kwargs):
        calls.append(kwargs)
        return {'ok': True, 'backup_sha256': 'a' * 64} if kwargs['operation'] == 'backup' else {'ok': True}
    controller = RealController('ops-' + '1' * 32, Path('/tmp/compose'), Path('/tmp'),
                                'prior', 'candidate', 'http://127.0.0.1', execute=execute)
    assert controller.apply('ops_compose_action', {'operation': 'apply', 'image': 'candidate'})['error'] == 'pre-upgrade-backup-required'
    assert controller.apply('ops_compose_action', {'operation': 'backup'})['ok']
    assert controller.apply('ops_compose_action', {'operation': 'apply', 'image': 'candidate'})['ok']
    assert controller.apply('ops_compose_action', {'operation': 'backup'})['error'] == 'backup-order'
    assert controller.apply('ops_compose_action', {'operation': 'restore_backup', 'backup_id': 'previous-release'})['ok']
    assert [call['operation'] for call in calls] == ['backup', 'start-candidate', 'restore-prior']
    assert calls[-1]['expected_backup_sha256'] == 'a' * 64
    result = controller.apply('ops_compose_action', {'operation': 'apply', 'image': 'prior'})
    assert result == {'ok': False, 'error': 'restore-required'}
    assert controller.audit[-1]['effect'] == 'image_downgrade'
    assert controller.audit[-1]['ok'] is False
    assert len(calls) == 3
    assert controller.apply('unknown', {})['error'] == 'not-real'


def test_missing_backup_and_failed_action_are_audited():
    def broken(**kwargs):
        raise RuntimeError('failure')
    c = RealController('ops-' + '1' * 32, Path('/tmp/compose'), Path('/tmp'),
                       'prior', 'candidate', 'http://127.0.0.1', execute=broken)
    assert c.apply('ops_compose_action', {'operation': 'restore_backup', 'backup_id': 'previous-release'})['error'] == 'backup-unavailable'
    c.backup_sha256 = 'a' * 64
    try:
        c.apply('ops_compose_action', {'operation': 'apply', 'image': 'candidate'})
    except RuntimeError:
        pass
    else:
        raise AssertionError('failed action not propagated')
    assert c.phase == 'unknown'
    assert c.audit[-1]['effect'] == 'mutation'
    assert c.audit[-1]['ok'] is False


def test_malformed_action_is_audited():
    import pytest
    c = RealController('ops-' + '1' * 32, Path('/tmp/compose'), Path('/tmp'),
                       'prior', 'candidate', 'http://127.0.0.1')
    with pytest.raises(ValueError):
        c.apply('bad', None)
    assert c.audit[-1]['ok'] is False
    assert c.audit[-1]['error_type'] == 'ValueError'
    assert c.audit[-1]['arguments'] is None


def test_diagnosis_reads_real_project_logs_and_redacts_key(tmp_path, monkeypatch):
    import subprocess
    compose = tmp_path / 'compose.yml'
    compose.write_text('services: {}')
    calls = []
    secret = 'private-admin-value-123456'
    monkeypatch.setenv('HONUA_ADMIN_PASSWORD', secret)
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, f'migration failed; key {secret}', '')
    monkeypatch.setattr(subprocess, 'run', run)
    project = 'ops-' + '1' * 32
    c = RealController(project, compose, tmp_path, 'prior', 'candidate', 'http://127.0.0.1:1234')
    result = c._diagnose(logs=True)
    assert result['ok'] is True
    assert 'migration failed' in result['log']
    assert secret not in result['log']
    assert calls[0][0:4] == ['docker', 'compose', '-p', project]
    assert len(result['log_sha256']) == 64


def test_diagnosis_redacts_operator_secret_not_in_environment(tmp_path, monkeypatch):
    import subprocess
    compose = tmp_path / 'compose.yml'
    compose.write_text('services: {}')
    secret = 'operator-secret-outside-environment'
    monkeypatch.setattr(subprocess, 'run', lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 0, 'diagnostic ' + secret, ''))
    controller = RealController('ops-' + '1' * 32, compose, tmp_path, 'prior', 'candidate',
                                'http://127.0.0.1:1', sensitive_values=(secret,))
    result = controller.apply('terminal_command', {'command': 'docker compose logs server'})
    assert result['ok'] and secret not in str(result) and secret not in str(controller.audit)


def test_metrics_are_live_read_and_not_invented(tmp_path, monkeypatch):
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return b'honua_requests_total 7\n'
    monkeypatch.setattr('real_controller.open_local', lambda request, timeout: Response())
    monkeypatch.setattr('real_seed._verify_runtime', lambda *args: {})
    c = RealController('ops-' + '1' * 32, tmp_path / 'compose', tmp_path,
                       'server@sha256:' + 'a' * 64, 'candidate', 'http://127.0.0.1:1234')
    result = c._metrics()
    assert result['metrics'] == 'honua_requests_total 7\n'
    assert len(result['metrics_sha256']) == 64


def test_unrecognized_action_arguments_do_not_enter_audit(tmp_path):
    c = RealController('ops-' + '1' * 32, tmp_path / 'compose', tmp_path,
                       'prior', 'candidate', 'http://127.0.0.1:1')
    secret = 'private-password-should-not-be-retained'
    assert c.apply('unknown', {'password': secret})['error'] == 'not-real'
    assert secret not in str(c.audit)
    assert len(c.audit[-1]['arguments_sha256']) == 64


def test_unknown_tool_and_secret_like_logs_are_not_retained(tmp_path, monkeypatch):
    import subprocess
    compose = tmp_path / 'compose.yml'
    compose.write_text('services: {}')
    secret = 'hidden-value-not-in-environment'
    c = RealController('ops-' + '1' * 32, compose, tmp_path, 'prior', 'candidate', 'http://127.0.0.1:1')
    c.apply(secret, {})
    assert secret not in str(c.audit)
    assert c.audit[-1]['tool'] == 'unknown-tool'
    monkeypatch.setattr(subprocess, 'run', lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 0, 'password=' + secret, ''))
    result = c._diagnose(logs=True)
    assert result == {'ok': False, 'error': 'logs-sensitive'}
    assert secret not in str(result)


def test_terminal_diagnostics_are_real_project_scoped_and_refuse_other_commands(tmp_path, monkeypatch):
    import subprocess
    compose = tmp_path / 'compose.yml'
    compose.write_text('services: {}')
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, '{"Service":"server","State":"running"}\n', '')
    monkeypatch.setattr(subprocess, 'run', run)
    project = 'ops-' + '1' * 32
    controller = RealController(project, compose, tmp_path, 'prior', 'candidate', 'http://127.0.0.1:1')
    result = controller.apply('terminal_command', {'command': 'docker compose ps'})
    assert result['ok'] and 'running' in result['status']
    assert calls[0][:4] == ['docker', 'compose', '-p', project]
    assert controller.audit[-1]['receipt']['status_sha256'] == result['status_sha256']
    assert controller.apply('terminal_command', {'command': 'docker compose down'})['error'] == 'not-real'
    assert controller.apply('terminal_command', {'command': 'docker compose ps; echo injected'})['error'] == 'not-real'
    assert len(calls) == 1
    assert not controller.audit[-1]['ok']
    assert controller.audit[-1]['effect'] == 'unclassified'
