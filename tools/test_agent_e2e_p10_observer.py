"""Actual independent MCP HTTP replay with fixture services, never live evidence."""
import copy
import json

import pytest

from agent_e2e_p10_observer import P10Observer, query_ids
from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_semantics import response_evidence
from test_agent_e2e_mcp import server, standard, result
from test_agent_e2e_query_projection import query


BINDING = {'service': 'case-a-shelters', 'layerId': 503}


def output(ids=('s3',)):
    return {'content': [], 'isError': False, 'structuredContent': {
        'serviceId': BINDING['service'], 'layerId': BINDING['layerId'], 'exceededTransferLimit': False,
        'resultOffset': 0, 'returnedCount': len(ids), 'geojson': {'type': 'FeatureCollection', 'features': [
            {'type': 'Feature', 'properties': {'source_id': value}, 'geometry': None} for value in ids]}}}


class Http:
    def __init__(self):
        self.calls = []
        self.authorizations = 0

    def authorize(self):
        self.authorizations += 1

    def json(self, step, method, path):
        self.calls.append((step, method, path))
        return {'observation': response_evidence(200, json.dumps(query()).encode()),
                'reference': {'key': step, 'sha256': canonical_digest(query())}}


@pytest.fixture
def observer(tmp_path, monkeypatch, server):
    monkeypatch.setenv('P10_OBSERVER_TEST', 'private-independent-observer')
    records = {}
    def retain(name, value):
        assert name not in records
        records[name] = copy.deepcopy(value)
        return {'key': name, 'sha256': canonical_digest(value)}
    http = Http()
    observed = P10Observer(http=http, mcp={'endpoint': server.url, 'credentialKind': 'bearer',
        'credentialEnv': 'P10_OBSERVER_TEST', 'workflowView': None}, retain=retain, binding=BINDING)
    return observed, records, http


def action(where='capacity > 50 AND is_open = true'):
    return {'type': 'tool_use', 'id': 'chosen', 'name': 'mcp_call_tool', 'input': {
        'name': 'honua_query_features', 'arguments': {'serviceId': BINDING['service'],
            'layerId': BINDING['layerId'], 'where': where, 'outFields': ['source_id'], 'returnGeometry': False}}}


def test_replays_actual_model_arguments_under_independent_identity_and_retains_raw_http(observer, server):
    observed, records, http = observer
    chosen = action()
    requests = []
    def respond(request):
        if request and request.get('method') == 'tools/call':
            requests.append(request['params'])
            return result(request, output())
        return standard(request)
    server.respond = respond
    before = observed.source('before-model-action')
    dispatch = {'actionId': 'a'*64, 'requestDigest': canonical_digest(chosen)}
    actual = observed.replay(chosen, dispatch, before, {'agentResult': {'content': output()}})
    assert requests == [{'name': 'honua_query_features', 'arguments': chosen['input']['arguments']}]
    assert actual['result'] == {'resourceIds': ['s3']} and actual['projectionError'] is None
    assert actual['before']['snapshot'] == actual['after']['snapshot']
    assert len(http.calls) == 2 and http.authorizations == 1
    assert any(key.startswith('query-observer-raw-') and value for key, value in records.items())
    assert 'private-independent-observer' not in json.dumps(records)


def test_wrong_model_filter_is_not_replaced_with_expected_solution(observer, server):
    observed, _, _ = observer
    chosen = action('capacity >= 50 OR capacity IS NULL')
    seen = []
    def respond(request):
        if request and request.get('method') == 'tools/call':
            seen.append(request['params']['arguments']['where'])
            return result(request, output(('s1', 's2', 's3', 's4')))
        return standard(request)
    server.respond = respond
    answer = observed.replay(chosen, {'actionId': 'b'*64, 'requestDigest': canonical_digest(chosen)},
        observed.source('before'), {'agentResult': {'content': output(('s1', 's2', 's3', 's4'))}})
    assert seen == ['capacity >= 50 OR capacity IS NULL']
    assert answer['result']['resourceIds'] == ['s1', 's2', 's3', 's4']


def test_worker_claim_cannot_override_independent_result(observer, server):
    observed, _, _ = observer
    server.respond = lambda request: result(request, output(('s2',))) if request and request.get('method') == 'tools/call' else standard(request)
    answer = observed.replay(action(), {'actionId': 'c'*64, 'requestDigest': 'sha256:'+'a'*64},
        observed.source('before'), {'agentResult': {'content': output(('s3',))}})
    assert answer['projectionError'] == 'PackError'


@pytest.mark.parametrize('change', ['foreign-service', 'foreign-layer', 'write', 'terminal'])
def test_privileged_observer_never_replays_unowned_or_mutating_model_action(observer, server, change):
    observed, _, _ = observer
    chosen = action()
    if change == 'foreign-service': chosen['input']['arguments']['serviceId'] = 'shared'
    elif change == 'foreign-layer': chosen['input']['arguments']['layerId'] = 1
    elif change == 'write': chosen['input']['name'] = 'honua_publish_service'
    else: chosen['name'] = 'terminal'
    with pytest.raises(PackError, match='model-selected owned-layer'):
        observed.replay(chosen, {'actionId': 'd'*64}, {}, {})
    assert not server.seen


@pytest.mark.parametrize('change', ['truncated', 'paged', 'count', 'missing-source', 'duplicate', 'wrong-count'])
def test_incomplete_query_cannot_become_exact_result(change):
    value, args = output(), action()['input']['arguments']
    body = value['structuredContent']
    if change == 'truncated': body['exceededTransferLimit'] = True
    elif change == 'paged': body['resultOffset'] = 1
    elif change == 'count': args['returnCountOnly'] = True
    elif change == 'missing-source': body['geojson']['features'][0]['properties'].clear()
    elif change == 'duplicate':
        body['geojson']['features'].append(copy.deepcopy(body['geojson']['features'][0])); body['returnedCount'] = 2
    else: body['returnedCount'] = 9
    with pytest.raises(PackError):
        query_ids(value, args, BINDING)
