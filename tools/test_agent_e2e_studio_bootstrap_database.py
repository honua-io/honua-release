"""Actual SQL + retained HTTP prefreeze lifecycle; no model/product qualification."""
import copy
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_state import DurableExecution
from agent_e2e_studio_backend import prepare_initial_fixture, Session
from test_agent_e2e_backend import Store
from test_agent_e2e_fixture_database import postgres, fixture_db
from test_agent_e2e_studio_database import studio_db
from test_agent_e2e_setup_http import server


@pytest.fixture
def bootstrap(studio_db, server, tmp_path, monkeypatch):
    import psycopg
    from psycopg.types.json import Jsonb
    database, dsn = studio_db
    monkeypatch.setenv('BOOTSTRAP_SETUP_TOKEN', 'private-prefreeze-admin')
    monkeypatch.setenv('BOOTSTRAP_REQUESTER_TOKEN', 'private-task-key')
    durable = DurableExecution(Store(), tmp_path/'ledger.json', initialize=True)
    operation = {'operationId': 'native-studio-prefreeze', 'liabilityBasisDigest': 'sha256:'+'b'*64,
                 'reservationIds': {'infrastructure': 'bootstrap-infrastructure', 'cleanup': 'bootstrap-cleanup'}}
    for kind, reservation in operation['reservationIds'].items():
        durable.update(durable.path, 'reserve-operation', candidate_key=durable.store.candidate,
            reservation_id=reservation, operation_id=operation['operationId'], purpose='bootstrap', kind=kind,
            amount='1', basis_digest=operation['liabilityBasisDigest'])
    item, draft, version = (str(uuid4()) for _ in range(3))
    namespace, owner = 'ae_bootstrap_fixture', 'api-key:'+str(uuid4())
    context = {'workspaceId': 'native-contract-workspace', 'turnId': 'native-contract-turn'}
    envelope = {'family': 'map', 'schemaVersion': '1.0', 'format': 'honua_map_package.v1',
        'bindings': [], 'dependencies': [], 'provenance': [],
        'body': {'title': 'Actual PostgreSQL + HTTP contract only', 'layers': [{'id': 'actual-source-fixture'}]}}
    native_validation = {'status': 'valid', 'diagnostics': []}
    draft_body = {'draftId': draft, 'itemId': item, 'packageKey': namespace, 'workspaceId': namespace,
        'ownerId': owner, 'family': 'map', 'generation': 1, 'baseVersionId': None,
        'envelope': envelope, 'validation': native_validation}
    version_body = {'versionId': version, 'itemId': item, 'sourceDraftId': draft, 'ownerId': owner,
        'packageKey': namespace, 'workspaceId': namespace, 'versionNumber': 1, 'contentHash': 'a'*64,
        'envelope': envelope, 'validation': native_validation}
    state = SimpleNamespace(change=None)
    def respond(method, path, raw):
        headers = {key.lower(): value for key, value in server.seen[-1][2].items()}
        requester = headers.get('x-api-key') == 'private-task-key'
        if requester:
            assert headers['x-honua-workspace'] == context['workspaceId'] and headers['x-honua-turn'] == context['turnId']
        with psycopg.connect(dsn) as connection:
            if '/content-items?' in path:
                rows = connection.execute('SELECT item_id::text,current_version_id::text,published_version_id::text FROM honua.studio_content_items').fetchall()
                listing = [{'itemId': row[0], 'currentVersionId': row[1], 'publishedVersionId': row[2],
                    'ownerId': owner, 'packageKey': namespace, 'workspaceId': namespace} for row in rows]
                result, status = {'items': listing, 'total': len(listing), 'nextCursor': None}, 200
            elif path == '/api/v1/studio/package-drafts':
                assert not requester and method == 'POST' and json.loads(raw)['itemId'] == item
                connection.execute('INSERT INTO honua.studio_content_items VALUES (%s,%s,%s,%s,NULL,NULL)',
                    (item, owner, namespace, namespace))
                connection.execute('INSERT INTO honua.studio_package_drafts VALUES (%s,%s,%s,%s,%s,%s)',
                    (draft, item, owner, namespace, namespace, Jsonb(envelope)))
                result, status = draft_body, 201
                if state.change == 'lost-create-reply':
                    connection.commit()
                    return None
            elif path.endswith('/validate'):
                result, status = native_validation, 200
            elif path.endswith('/content-versions'):
                connection.execute('INSERT INTO honua.studio_content_versions VALUES (%s,%s,%s,%s,%s,%s,1,%s,%s)',
                    (version, item, draft, owner, namespace, namespace, 'a'*64, Jsonb(envelope)))
                connection.execute('UPDATE honua.studio_content_items SET current_version_id=%s WHERE item_id=%s', (version, item))
                result, status = version_body, 201
            else:
                assert requester and path.endswith('/versions/'+version)
                result, status = version_body, 200
        return status, {}, json.dumps({'success': True, 'data': result}).encode()
    server.respond = respond
    configuration = {'candidateKey': durable.store.candidate, 'operation': operation, 'database': database.config,
        'http': {'endpoint': server.url, 'setupCredentialEnv': 'BOOTSTRAP_SETUP_TOKEN', 'setupCredentialKind': 'api-key',
            'requesterCredentialEnv': 'BOOTSTRAP_REQUESTER_TOKEN', 'requesterCredentialKind': 'api-key', 'requesterAgentContext': context},
        'itemId': item, 'namespace': namespace, 'ownerPrincipalId': owner, 'envelope': envelope}
    try:
        yield SimpleNamespace(durable=durable, database=database, configuration=configuration, state=state, dsn=dsn)
    finally:
        durable.close()


def test_native_prefreeze_preparation_retains_actual_http_sql_and_original_cost_liability(bootstrap, server):
    value = bootstrap
    reference = prepare_initial_fixture(durable=value.durable, configuration=value.configuration)
    record = value.durable.state['studioBootstrapFixtures'][reference['key']]
    assert reference['sha256'] == canonical_digest(record) and record['ownership']['baseline'] == record['baseline']
    assert record['fixture']['initialItem']['publishedVersionId'] is None
    assert len(record['nativeOriginals']) == 12 and reference['qualification'] is False
    assert all(row['state'] == 'reserved' and row['actualUsd'] is None for row in value.durable.state['ledger']['operations'])
    assert value.durable.state['ledger']['entries'] == []
    raw = json.dumps(value.durable.state)
    assert 'private-prefreeze-admin' not in raw and 'private-task-key' not in raw
    adopted = Session.__new__(Session)
    adopted.durable, adopted.campaign = value.durable, {'componentBindingDigest': value.durable.store.candidate}
    adopted.config = {'studio': {'bootstrapFixture': {'key': reference['key'], 'sha256': reference['sha256']}}}
    assert adopted.initial_fixture() == record
    seen = len(server.seen)
    with pytest.raises(PackError, match='already started'):
        prepare_initial_fixture(durable=value.durable, configuration=value.configuration)
    assert len(server.seen) == seen
    retained = {}
    def retain(kind, evidence):
        retained[kind] = copy.deepcopy(evidence)
        return {'sha256': canonical_digest(evidence)}
    value.database.cleanup(record['ownership'], authorize=lambda _: value.durable._require_live_lease(), retain=retain)
    assert set(retained) == {'intent', 'observation'}
    assert value.database.baseline()['snapshot'] == record['baseline']['snapshot']


def test_lost_actual_create_reply_keeps_sql_original_baseline_and_no_retry(bootstrap, server):
    import psycopg
    value = bootstrap
    value.state.change = 'lost-create-reply'
    with pytest.raises(PackError, match='reconcile without replay'):
        prepare_initial_fixture(durable=value.durable, configuration=value.configuration)
    with psycopg.connect(value.dsn) as connection:
        assert connection.execute('SELECT count(*) FROM honua.studio_content_items').fetchone()[0] == 1
    assert not value.durable.state.get('studioBootstrapFixtures')
    assert len(value.durable.state['studioBootstrapOperations']) == 2
    seen = len(server.seen)
    with pytest.raises(PackError, match='already started'):
        prepare_initial_fixture(durable=value.durable, configuration=value.configuration)
    assert len(server.seen) == seen


def test_missing_bootstrap_cleanup_liability_prevents_first_http_or_sql_effect(bootstrap, server):
    import psycopg
    value = bootstrap
    value.configuration['operation']['reservationIds']['cleanup'] = 'absent-native-liability'
    with pytest.raises(PackError, match='prepaid bootstrap'):
        prepare_initial_fixture(durable=value.durable, configuration=value.configuration)
    assert not server.seen and not value.durable.state.get('studioBootstrapOperations')
    with psycopg.connect(value.dsn) as connection:
        assert connection.execute('SELECT count(*) FROM honua.studio_content_items').fetchone()[0] == 0
