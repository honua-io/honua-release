from copy import deepcopy
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from ecs_handoff import seal, verify, approve_ledger

BINDING = {'run_id': '123', 'attempt': '1', 'cell': 'aws-ecs/redis-on', 'scenario': 'O01'}
ROW = {'tool': 'honua_propose_platform_release_convergence', 'proposalId': 'proposal01'}


def test_signed_ledger_rejects_forgery_replay_and_wrong_scenario():
    key = Ed25519PrivateKey.generate()
    envelope = seal(key, BINDING, [ROW])
    assert verify(key.public_key(), envelope, BINDING) == [ROW]
    tampered = deepcopy(envelope)
    tampered['ledger']['proposals'][0]['proposalId'] = 'proposal02'
    with pytest.raises(ValueError): verify(key.public_key(), tampered, BINDING)
    with pytest.raises(ValueError): verify(Ed25519PrivateKey.generate().public_key(), envelope, BINDING)
    with pytest.raises(ValueError): verify(key.public_key(), envelope, {**BINDING, 'attempt': '2'})
    with pytest.raises(ValueError): seal(key, {**BINDING, 'scenario': 'O02'}, [ROW])
    with pytest.raises(ValueError): seal(key, BINDING, [ROW, ROW])


def test_approval_requires_independent_policy_and_records_failure():
    key = Ed25519PrivateKey.generate()
    envelope = seal(key, BINDING, [ROW])
    class Approver:
        def __init__(self): self.calls = []
        def approve(self, value):
            self.calls.append(value)
            return {'executionOperationId': 'operation01'}
    approver = Approver()
    assert approve_ledger(key.public_key(), envelope, BINDING, approver, lambda _: False)[0]['status'] == 'refused'
    assert approver.calls == []
    result = approve_ledger(key.public_key(), envelope, BINDING, approver, lambda _: True)
    assert result[0]['executionOperationId'] == 'operation01'
    assert approver.calls == ['proposal01']


def test_controller_snapshot_uses_real_read_interfaces():
    from ecs_observe import DatabaseJournal, controller_snapshot
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, params=None):
            self.sql, self.params = sql, params
        def fetchall(self): return [('141.sql',), ('142.sql',)]
        def fetchone(self): return (1,)
    class Connection:
        def cursor(self): return Cursor()
    class ECS:
        def task_digests(self): return {'image_digest': 'sha256:' + 'a' * 64}
    journal = DatabaseJournal(Connection())
    assert journal.contains('142.sql')
    assert controller_snapshot(ECS(), journal)['journal'] == ['141.sql', '142.sql']
    with pytest.raises(ValueError): journal.contains("bad'; DROP TABLE x;--.sql")


def _fake_approver_process(conn, public_key, envelope, binding, product_state):
    """A distinct process stands in for the approval principal and product executor."""
    class Product:
        def approve(self, proposal_id):
            assert proposal_id == 'proposal01'
            product_state['image_digest'] = 'sha256:' + 'a' * 64
            product_state['journal'] = ['142.sql']
            return {'executionOperationId': 'execution01'}
    result = approve_ledger(public_key, envelope, binding, Product(), lambda _: True)
    conn.send(result)
    conn.close()


def test_fake_model_proxy_approver_and_controller_readback(tmp_path):
    import multiprocessing
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'tools'))
    from agent_cli_sessions import RecordingProxy
    from ecs_recording import ProposalCapture
    from ecs_observe import DatabaseJournal, controller_snapshot
    capture = ProposalCapture(lambda kind, action: {
        'status': 'ok', 'result': {'structuredContent': {'proposalId': 'proposal01'}}})
    view = {'tools': [{'name': ROW['tool'], 'inputSchema': {'type': 'object'}}]}
    with RecordingProxy(view, capture, tmp_path, 1, 5, server_name='honua') as proxy:
        proxy.rpc('tools/call', {'name': ROW['tool'], 'arguments': {}})
        assert proxy.actions[0]['status'] == 'pass'
    key = Ed25519PrivateKey.generate()
    envelope = seal(key, BINDING, capture.records)
    manager = multiprocessing.Manager()
    product_state = manager.dict({'image_digest': 'sha256:' + 'b' * 64, 'journal': []})
    parent, child = multiprocessing.Pipe()
    process = multiprocessing.Process(target=_fake_approver_process,
        args=(child, key.public_key(), envelope, BINDING, product_state))
    process.start()
    result = parent.recv()
    process.join(timeout=10)
    assert process.exitcode == 0 and result[0]['executionOperationId'] == 'execution01'
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql): pass
        def fetchall(self): return [(script,) for script in product_state['journal']]
    class Connection:
        def cursor(self): return Cursor()
    class ECS:
        def task_digests(self): return {'image_digest': product_state['image_digest']}
    observed = controller_snapshot(ECS(), DatabaseJournal(Connection()))
    assert observed['ecs']['image_digest'] == 'sha256:' + 'a' * 64
    assert observed['journal'] == ['142.sql']
    manager.shutdown()


def test_controller_database_connection_uses_cell_secret_and_read_only_tls():
    from ecs_observe import _database_parameters
    params = _database_parameters('Host=db.example;Port=5432;Database=honua;Username=reader;Password=hidden')
    assert params['host'] == 'db.example' and params['dbname'] == 'honua'
    assert params['sslmode'] == 'require' and 'read_only=on' in params['options']
    with pytest.raises(ValueError): _database_parameters('Host=db;Host=other;Database=x;Username=x;Password=x')
    with pytest.raises(ValueError): _database_parameters('Host=db;Database=x;Username=x')


def test_cell_controller_binding_reads_target_outputs_and_private_connection():
    from types import SimpleNamespace
    from ecs_observe import open_cell_controller_state
    class Target:
        name = 'aws-ecs'
        _workdir = '/private/cell'
        def _tf(self, workdir, command, flag, name):
            assert (workdir, command, flag) == ('/private/cell', 'output', '-raw')
            return SimpleNamespace(stdout={'ecs_cluster_name': 'cell-cluster',
                                           'ecs_service_name': 'cell-service'}[name])
        def _connection_string(self): return 'private-cell-secret'
    captured = {}
    def open_state(**kwargs):
        captured.update(kwargs)
        return ('ecs', 'database')
    assert open_cell_controller_state(Target(), open_state=open_state) == ('ecs', 'database')
    assert captured == {'cluster': 'cell-cluster', 'service': 'cell-service',
                        'database_dsn': 'private-cell-secret'}
    target = Target()
    target._workdir = None
    with pytest.raises(ValueError): open_cell_controller_state(target, open_state=open_state)
