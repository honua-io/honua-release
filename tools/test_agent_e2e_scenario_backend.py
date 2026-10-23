"""Real HTTP adapter boundaries with local fixture responses; never live receipts."""
import copy
from email.parser import BytesParser
from email.policy import default
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_e2e_budget import update
from agent_e2e_pack import PackError, load_json
from agent_e2e_scenario_backend import ScenarioHttp, TownSetup, observe_point_selections
from agent_e2e_state import preserve_history
from agent_e2e_semantics import fixture
from test_agent_e2e_query_projection import query
from test_agent_e2e_setup_http import server

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = {'campaignId': 'c', 'caseId': 'P10:A:local-docker', 'attempt': 1,
           'componentBindingDigest': 'sha256:'+'a'*64}


@pytest.fixture
def adapter(tmp_path, server, monkeypatch):
    monkeypatch.setenv('SCENARIO_TEST_TOKEN', 'private-scenario-credential')
    ledger = tmp_path/'ledger.json'
    candidate = CONTEXT['componentBindingDigest']
    update(ledger, 'init', candidate_key=candidate)
    for kind in ('infrastructure', 'cleanup'):
        update(ledger, 'reserve', candidate_key=candidate, reservation_id=kind,
               campaign_id='c', case_id=CONTEXT['caseId'], attempt=1, kind=kind,
               amount='1', basis_digest='sha256:'+'b'*64)
    class Durable:
        def __init__(self):
            self.store = SimpleNamespace(candidate=candidate)
            self.state = {'ledger': load_json(ledger)}
            self.snapshots = []
            self.alive = True
        def _require_live_lease(self):
            if not self.alive:
                raise PackError('lease lost')
        def _persist(self):
            if self.snapshots:
                preserve_history(self.snapshots[-1], self.state)
            self.snapshots.append(copy.deepcopy(self.state))
    durable = Durable()
    def make():
        return ScenarioHttp(durable=durable, context=CONTEXT, endpoint=server.url,
            credential_env='SCENARIO_TEST_TOKEN', attempt_id='attempt-1')
    return SimpleNamespace(durable=durable, make=make, http=make())


def test_seeds_actual_source_bytes_and_binds_product_ids(adapter, server):
    uploads = []
    def respond(method, path, body):
        if method == 'GET':
            value = {'data': {'connectionId': 'connection-42'}}
        elif path.endswith('/import/upload'):
            headers = server.seen[-1][2]
            parsed = BytesParser(policy=default).parsebytes(('Content-Type: '+headers['Content-Type']+'\r\n\r\n').encode()+body)
            parts = {part.get_param('name', header='content-disposition'): part.get_payload(decode=True)
                     for part in parsed.iter_parts()}
            assert parts['OverwriteExisting'] == b'false'
            assert parts['SourceSrid'] == b'3857'
            uploads.append(parts['file'])
            value = {'success': True, 'featureCount': len(json.loads(parts['file'])['features']),
                     'physicalTableName': parts['TableName'].decode(), 'schema': 'honua_data'}
        else:
            assert method == 'POST' and path == '/api/v1/admin/connections/connection-42/layers'
            value = {'data': {'layerId': 500+len(uploads)}}
        return 200, {}, json.dumps(value).encode()
    server.respond = respond
    manifest = ROOT/'certification/agent-e2e/corpus/manifest.v1.json'
    result = TownSetup(adapter.http, manifest).prepare(variant='A', namespace='case_a',
        starting_state='published-town', connection_id='connection-42')
    assert result['layers']['shelters']['layerId'] == 503
    assert result['layers']['shelters']['service'] == 'case_a-shelters'
    for name, raw in zip(TownSetup.SOURCES, uploads):
        assert raw == (manifest.parent/f'town-A/{name}.3857.geojson').read_bytes()
    assert len(adapter.durable.state['scenarioOperations']) == 14
    assert 'private-scenario-credential' not in json.dumps(adapter.durable.state)


def test_reconstructed_setup_cannot_replay_a_retained_mutation(adapter, server):
    adapter.http.json('once', 'POST', '/api/v1/admin/connections/c/layers', {'table': 'owned'})
    with pytest.raises(PackError, match='already retained'):
        adapter.make().json('once', 'POST', '/api/v1/admin/connections/c/layers', {'table': 'owned'})
    assert len(server.seen) == 1


def test_setup_requires_prepaid_liability_and_live_lease(adapter, server):
    for row in adapter.durable.state['ledger']['entries']:
        row['state'] = 'settled'
        row['actualUsd'] = '0'
    with pytest.raises(PackError, match='prepaid'):
        adapter.http.json('unpaid', 'POST', '/create', {})
    assert not server.seen
    adapter.durable.alive = False
    with pytest.raises(PackError, match='lease lost'):
        adapter.http.json('lost', 'GET', '/read')


def test_independent_point_checks_read_candidate_and_reject_wrong_selection(adapter, server):
    wrong = [False]
    def respond(method, path, body):
        assert method == 'GET'
        clause = parse_qs(urlsplit(path).query)['where'][0]
        rows = fixture()['features']
        if clause != '1=1':
            rows = [row for row in rows if
                (row['properties']['capacity'] is None and 'IS NULL' in clause) or
                (row['properties']['capacity'] is not None and
                 (row['properties']['capacity'] > 50 if '> 50' in clause else row['properties']['capacity'] >= 50))]
        if wrong[0] and clause != '1=1':
            rows = []
        return 200, {}, json.dumps(query(rows)).encode()
    server.respond = respond
    result = observe_point_selections(adapter.http, {'service': 'actual-service', 'layerId': 503})
    assert len(result['checks']) == 3 and result['qualification'] is False
    assert len(server.seen) == 9
    # Fresh attempt identity avoids treating replay as the assertion failure.
    wrong[0] = True
    observer = ScenarioHttp(durable=adapter.durable, context=CONTEXT, endpoint=server.url,
        credential_env='SCENARIO_TEST_TOKEN', attempt_id='attempt-2')
    with pytest.raises(PackError):
        observe_point_selections(observer, {'service': 'actual-service', 'layerId': 503})


def test_town_setup_refuses_to_solve_empty_deployment_agent_tasks(adapter):
    setup = TownSetup(adapter.http, ROOT/'certification/agent-e2e/corpus/manifest.v1.json')
    with pytest.raises(PackError, match='unsupported town starting state'):
        setup.prepare(variant='A', namespace='case_a', starting_state='empty-deployment')
