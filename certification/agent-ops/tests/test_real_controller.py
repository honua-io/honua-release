from pathlib import Path

from real_controller import RealController


def test_real_action_selection_and_unsafe_attempts_are_audited():
    calls = []
    def execute(**kwargs):
        calls.append(kwargs)
        return {'ok': True, 'backup_sha256': 'a' * 64} if kwargs['operation'] == 'backup' else {'ok': True}
    controller = RealController('ops-' + '1' * 32, Path('/tmp/compose'), Path('/tmp'),
                                'prior', 'candidate', 'http://127.0.0.1', execute=execute)
    assert controller.apply('honua_deploy_operations', {'operation': 'apply', 'image': 'candidate'})['error'] == 'pre-upgrade-backup-required'
    assert controller.apply('honua_deploy_operations', {'operation': 'backup'})['ok']
    assert controller.apply('honua_deploy_operations', {'operation': 'apply', 'image': 'candidate'})['ok']
    assert controller.apply('honua_deploy_operations', {'operation': 'backup'})['error'] == 'backup-order'
    assert controller.apply('honua_deploy_operations', {'operation': 'restore_backup', 'backup_id': 'previous-release'})['ok']
    assert [call['operation'] for call in calls] == ['backup', 'start-candidate', 'restore-prior']
    assert calls[-1]['expected_backup_sha256'] == 'a' * 64
    result = controller.apply('honua_deploy_operations', {'operation': 'apply', 'image': 'prior'})
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
    assert c.apply('honua_deploy_operations', {'operation': 'restore_backup', 'backup_id': 'previous-release'})['error'] == 'backup-unavailable'
    c.backup_sha256 = 'a' * 64
    try:
        c.apply('honua_deploy_operations', {'operation': 'apply', 'image': 'candidate'})
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
