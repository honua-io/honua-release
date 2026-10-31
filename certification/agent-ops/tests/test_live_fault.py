import pytest

from live_fault import MigrationFault


class Holder:
    def poll(self): return None
    def wait(self, timeout): return 0


def test_fault_fires_only_for_observed_locked_migration(tmp_path, monkeypatch):
    compose = tmp_path / 'compose.yml'
    compose.write_text('services: {}')
    fault = MigrationFault('ops-' + 'a' * 32, compose, pending_migrations=('142_test.sql',), candidate_image_id='sha256:' + 'b' * 64)
    calls = []
    def sql(statement):
        calls.append(statement)
        if 'SELECT count(*) FROM pg_stat_activity' in statement: return '0'
        if 'SELECT count(*) FROM public.schema_versions' in statement: return '0'
        if 'SELECT l.pid FROM pg_locks' in statement: return '12'
        if 'SELECT a.pid FROM pg_stat_activity' in statement: return '34'
        if 'SELECT pg_terminate_backend(a.pid)' in statement: return 't'
        return 't'
    monkeypatch.setattr('live_fault.subprocess.Popen', lambda *a, **k: Holder())
    monkeypatch.setattr(fault, '_sql', sql)
    monkeypatch.setattr(fault, '_candidate_address', lambda: ('a' * 64, '172.20.0.5'))
    monkeypatch.setattr(fault, '_logs', lambda: "Executing Database Server script '142_test.sql'\nmigration failed")
    assert fault.arm()['armed'] is True
    receipt = fault.wait_and_fire()
    assert receipt['runner_pid'] == 34
    assert receipt['terminate_result'] == 't'
    assert receipt['backend_absent'] and receipt['journal_absent']
    assert receipt['script'] == '142_test.sql'
    assert receipt['paused_with_xid_and_write_lock'] is True
    assert any('a.pid=34' in sql and 'pg_blocking_pids(a.pid)' in sql for sql in calls)
    assert fault.holder is None
    with pytest.raises(RuntimeError, match='already armed'):
        fault.arm()


def test_no_termination_proof_is_never_a_fired_receipt(tmp_path, monkeypatch):
    compose = tmp_path / 'compose.yml'
    compose.write_text('services: {}')
    fault = MigrationFault('ops-' + 'a' * 32, compose, pending_migrations=('142_test.sql',), candidate_image_id='sha256:' + 'b' * 64)
    monkeypatch.setattr('live_fault.subprocess.Popen', lambda *a, **k: Holder())
    def sql(statement):
        if 'SELECT count(*) FROM pg_stat_activity' in statement: return '0'
        if 'SELECT count(*) FROM public.schema_versions' in statement: return '0'
        if 'SELECT l.pid FROM pg_locks' in statement: return '12'
        if 'SELECT a.pid FROM pg_stat_activity' in statement: return '34'
        if 'SELECT pg_terminate_backend(a.pid)' in statement: return 'f'
        return 't'
    monkeypatch.setattr(fault, '_sql', sql)
    monkeypatch.setattr(fault, '_candidate_address', lambda: ('a' * 64, '172.20.0.5'))
    monkeypatch.setattr(fault, '_logs', lambda: "Executing Database Server script '142_test.sql'\nmigration failed")
    fault.arm()
    with pytest.raises(RuntimeError, match='not interrupted'):
        fault.wait_and_fire()
    assert fault.fired is None
    assert fault.holder is None
