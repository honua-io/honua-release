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
