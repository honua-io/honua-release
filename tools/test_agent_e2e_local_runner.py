"""Local corpus runner: full loop against a fake candidate (HTTP + MCP) and a fake model.

No Docker and no Bedrock. The fake candidate is a real loopback HTTP server, so the
runner's own recorder (SetupHttp + agent_e2e_mcp.Client), the canary model path,
the projections and corpus/oracle.verify all run unmodified.
"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

import agent_e2e_local_runner as runner
from agent_e2e_local_api import Observation, RoleBinding, Seeded, Stack
from agent_e2e_local_recorder import ControllerRecorder
from agent_e2e_local_scenarios.P10 import P10

ROOT = Path(__file__).resolve().parents[1]

# Test-only enumeration contracts for the fake candidate's inventories (the real registry is reviewed core code).
TEST_CONTRACTS = (
    {'kind': 'http', 'method': 'GET', 'path': r'/maps', 'paging': {'cursor': '/next'}},
    {'kind': 'mcp', 'tool': 'honua_list_maps', 'paging': {'total': '/result/total'}},
    {'kind': 'http', 'method': 'POST', 'path': r'/api/v1/admin/maps/search', 'paging': {'cursor': '/next'}},
    {'kind': 'http', 'method': 'GET', 'path': r'/api/v1/admin/maps', 'paging': 'unpaged'},
)


@pytest.fixture(autouse=True)
def _test_enumeration_contracts(monkeypatch):
    import agent_e2e_local_projections as projections_module
    monkeypatch.setattr(projections_module, 'ENUMERATION_CONTRACTS',
                        projections_module.ENUMERATION_CONTRACTS + TEST_CONTRACTS)
DIGEST = 'sha256:' + 'a' * 64
ADMIN = 'admin-' + 'k' * 40
CORRECT_WHERE = 'is_open = true AND capacity > 50'
JOURNEY = json.loads((ROOT / 'certification/terminal-journey/journey.v1.json').read_text())


def shelters(variant='A'):
    return json.loads((ROOT / f'certification/agent-e2e/corpus/town-{variant}/shelters.3857.geojson').read_text())['features']


class Candidate:
    """In-memory stand-in for the candidate's admin API, FeatureServer and MCP endpoint."""

    def __init__(self):
        self.keys = {}          # key secret -> {'id', 'revoked', 'permissions'}
        self.features = shelters()
        self.by_service = {}
        self.mcp_calls = []
        self.approvals = []
        self.maps = []
        self.lock = threading.Lock()

    def principal(self, headers):
        secret = headers.get('X-API-Key')
        if secret == ADMIN:
            return {'id': 'admin', 'revoked': False, 'permissions': ['admin:write']}
        row = self.keys.get(secret)
        return row if row and not row['revoked'] else None

    def features_of(self, service):
        return self.by_service.get(service, self.features)

    def feature_server(self, service=None):
        features = self.features_of(service)
        fields = [{'name': 'objectid', 'type': 'esriFieldTypeOID'}] + [
            {'name': name, 'type': 'esriFieldTypeString'} for name in ('capacity', 'is_open', 'name', 'source_id')]
        return {'geometryType': 'esriGeometryPoint', 'exceededTransferLimit': False,
                'spatialReference': {'wkid': 3857}, 'objectIdFieldName': 'objectid', 'fields': fields,
                'features': [{'attributes': {'objectid': i + 1, **f['properties']},
                              'geometry': {'x': f['geometry']['coordinates'][0], 'y': f['geometry']['coordinates'][1]}}
                             for i, f in enumerate(features)]}

    def query(self, arguments):
        rows = self.features
        if arguments.get('where') == CORRECT_WHERE:
            rows = [f for f in rows if f['properties']['is_open'] and (f['properties']['capacity'] or 0) > 50]
        return {'content': [{'type': 'text', 'text': f'{len(rows)} features'}], 'isError': False,
                'structuredContent': {'serviceId': arguments.get('serviceId'), 'layerId': arguments.get('layerId'),
                                      'exceededTransferLimit': False, 'resultOffset': 0, 'returnedCount': len(rows),
                                      'geojson': {'type': 'FeatureCollection', 'features': rows}}}

    TOOLS = [
        {'name': 'honua_query_features', 'description': 'Query features of a published layer.',
         'inputSchema': {'type': 'object', 'properties': {'serviceId': {'type': 'string'}, 'layerId': {'type': 'integer'},
                                                           'where': {'type': 'string'}}}},
        {'name': 'honua_update_features', 'description': 'Edit features of a published layer.',
         'inputSchema': {'type': 'object', 'properties': {'serviceId': {'type': 'string'}}}},
    ]
    EXTRA_TOOLS = [
        {'name': 'honua_create_map', 'description': 'Create a map.', 'inputSchema': {'type': 'object'}},
        {'name': 'honua_ingest_dataset', 'description': 'Load an inline dataset.', 'inputSchema': {'type': 'object'}},
    ]
    extra_tools = False

    def mcp(self, message, principal):
        method = message.get('method')
        if method == 'initialize':
            return {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'fake'}}
        if method == 'tools/list':
            return {'tools': self.TOOLS + (self.EXTRA_TOOLS if self.extra_tools else [])}
        if method == 'resources/read':
            return {'contents': [{'uri': message['params']['uri'], 'text': '{"processes":[]}'}]}
        if method == 'tools/call':
            name, arguments = message['params']['name'], message['params']['arguments']
            self.mcp_calls.append((principal['id'], name, arguments))
            if name == 'honua_query_features':
                return self.query(arguments)
            if name == 'honua_create_map' and arguments.get('title') == 'bad':
                return {'content': [{'type': 'text', 'text': 'invalid title'}], 'isError': True}
            if name == 'honua_create_map' and arguments.get('pending'):
                return {'content': [{'type': 'text', 'text': 'accepted'}], 'isError': False,
                        'structuredContent': {'status': 'Accepted'}}
            if name == 'honua_create_map':
                map_id = f'map-{len(self.maps) + 1}'
                self.maps.append({'id': map_id, 'title': arguments.get('title', '')})
                return {'content': [{'type': 'text', 'text': 'created'}], 'isError': False,
                        'structuredContent': {'mapId': map_id}}
            if name == 'honua_ingest_dataset':
                rows = arguments.get('data', '').splitlines()[1:]
                bad = [{'row': i + 2, 'reason': 'missing coordinate'} for i, row in enumerate(rows) if ',,' in row]
                return {'content': [{'type': 'text', 'text': 'ingested'}], 'isError': False,
                        'structuredContent': {'rowCount': len(rows) - len(bad), 'errors': bad}}
            if name == 'honua_update_features' and arguments.get('serviceId') not in (None, 'p10-shelters'):
                service = arguments['serviceId']
                self.by_service[service] = [dict(f, properties={**f['properties'], 'capacity': 999})
                                            for f in self.features_of(service)]
                return {'content': [{'type': 'text', 'text': 'updated'}], 'isError': False}
            if name == 'honua_update_features':
                self.features = [dict(f, properties={**f['properties'], 'capacity': 999}) for f in self.features]
                return {'content': [{'type': 'text', 'text': 'updated'}], 'isError': False}
        raise KeyError(method)


def serve(candidate):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, value=None, content_type='application/json'):
            raw = b'' if value is None else json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def body(self):
            length = int(self.headers.get('Content-Length') or 0)
            return json.loads(self.rfile.read(length) or b'null')

        def do_GET(self):
            with candidate.lock:
                principal = candidate.principal(self.headers)
                path = urlsplit(self.path).path
                if principal is None:
                    return self.reply(401, {'error': 'unauthorized'})
                if path == '/api/v1/admin/api-keys/':
                    return self.reply(200, {'data': []})
                if path == '/api/v1/admin/maps':
                    limit = int(dict(parse_qs(urlsplit(self.path).query)).get('limit', ['10'])[0])
                    return self.reply(200, {'items': candidate.maps[:limit]})
                if path == '/api/v1/admin/version':
                    return self.reply(200, {'data': {'version': 'fake'}})
                if path.endswith('/effective-permissions'):
                    return self.reply(200, {'data': {'status': 'active'}})
                if '/FeatureServer/' in path and path.endswith('/query'):
                    service = path.split('/rest/services/', 1)[1].split('/', 1)[0]
                    return self.reply(200, candidate.feature_server(service if service in candidate.by_service else None))
                return self.reply(404, {'error': 'not found'})

        def do_DELETE(self):
            self.reply(204)

        def do_POST(self):
            with candidate.lock:
                principal = candidate.principal(self.headers)
                path = urlsplit(self.path).path
                body = self.body()
                if principal is None:
                    return self.reply(401, {'error': 'unauthorized'})
                if path == '/mcp':
                    if 'id' not in body:
                        return self.reply(202)
                    try:
                        result = candidate.mcp(body, principal)
                    except KeyError:
                        return self.reply(200, {'jsonrpc': '2.0', 'id': body['id'],
                                                'error': {'code': -32601, 'message': 'unknown'}})
                    return self.reply(200, {'jsonrpc': '2.0', 'id': body['id'], 'result': result})
                if path == '/api/v1/admin/api-keys/':
                    if 'admin:write' not in principal['permissions']:
                        return self.reply(403, {'error': 'forbidden'})
                    number = len(candidate.keys) + 1
                    secret = f'secret-{number}-' + 'x' * 32
                    candidate.keys[secret] = {'id': f'key-{number}', 'revoked': False,
                                              'permissions': body['permissions']}
                    return self.reply(201, {'data': {'apiKey': {'id': f'key-{number}'}, 'key': secret}})
                if path.startswith('/api/v1/admin/api-keys/') and path.endswith('/revoke'):
                    key_id = path.split('/')[-2]
                    for row in candidate.keys.values():
                        if row['id'] == key_id:
                            row['revoked'] = True
                    return self.reply(200, {'data': {'status': 'revoked'}})
                if path.startswith('/api/v1/admin/proposals/') and path.endswith('/approve'):
                    if 'admin:approve' not in principal['permissions']:
                        return self.reply(403, {'error': 'forbidden'})
                    candidate.approvals.append((principal['id'], path.split('/')[-2]))
                    return self.reply(200, {'data': {'status': 'Approved'}})
                if path == '/api/v1/admin/maps/search':
                    rows = [{'id': 'map-old', 'owner': 'key-2'}] if body.get('scope') == 'all' else []
                    return self.reply(200, {'items': rows, 'next': None})
                if path == '/api/v1/operations/service.publish/submit':
                    return self.reply(200, {'data': {'operationId': 'service.publish', 'status': 'Completed',
                                                     'resourceIds': {'serviceName': body['serviceName'], 'layerId': '0'}}})
                return self.reply(404, {'error': 'not found'})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class FakeModel:
    """Scripted model replies; records every messages array the proxy would send."""

    def __init__(self, replies):
        self.replies, self.sent, self.admitted = list(replies), [], 0

    def ensure_admission(self):
        self.admitted += 1
        return []

    def complete(self, messages, certification):
        self.sent.append(json.loads(json.dumps(messages)))
        reply = self.replies.pop(0)
        content = reply(messages) if callable(reply) else reply
        return content, {'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 110}, 5, {'bindings': certification}


class FakeP10(P10):
    """P10 with the town upload replaced by one recorded publish handle (no import pipeline)."""

    def seed(self, stack, variant):
        from agent_e2e_p10_observer import source_path
        recorder = stack.extra['recorder']
        recorder.json('publish-shelters-submit', 'POST', '/api/v1/operations/service.publish/submit',
                      {'serviceName': 'p10-shelters'})
        binding = {'service': 'p10-shelters', 'layerId': 0}
        recorder.json('p10-source-before', 'GET', source_path(binding))
        return Seeded({'variant': variant, **binding})


class FakeRuntime(runner.LocalRuntime):
    def __init__(self, base_url, model, tmp_path):
        super().__init__(driver=None, model='us.anthropic.claude-sonnet-4-6', release_id='2026.1-rc.2',
                         max_model_calls=10, max_actions=10, max_tokens=100000)
        self.base_url, self.fake_model, self.tmp_path = base_url, model, tmp_path
        self.torn_down = False

    def setup(self, case_id, candidate_digest, needs_approver=False, agent_permissions=('admin:write',)):
        import os
        os.environ[self.ADMIN_ENV] = ADMIN
        self._secrets.append(ADMIN)
        workdir = self.tmp_path / 'case'
        workdir.mkdir(mode=0o700)
        self.workdirs.append(str(workdir))
        stack = Stack(base_url=self.base_url, admin_key=ADMIN, approver_key=None, compose_project='corpus-test',
                      compose_file='none', workdir=str(workdir), candidate_digest=candidate_digest,
                      extra={'workspaceId': 'tj-test'})
        self._prepare(stack, list(agent_permissions))
        if needs_approver:
            stack = self._mint_approver(stack)
        return stack

    def proxy(self, stack):
        self.fake_model.config = self._endpoint(stack)
        return self.fake_model

    def teardown(self, stack):
        self.torn_down = True


def case(variant='A'):
    return runner.select_case(runner.PACK, 'P10', variant)


def tool(where, name='honua_query_features'):
    args = {'serviceId': 'p10-shelters', 'layerId': 0}
    if where is not None:
        args['where'] = where
    return json.dumps({'kind': 'tool_call', 'tool': name, 'arguments': args, 'intent': 'query'})


FINAL = json.dumps({'kind': 'final', 'answer': 'Two shelters match: Centre and the North refuge.'})


@pytest.fixture
def candidate():
    state = Candidate()
    server = serve(state)
    state.base_url = f'http://127.0.0.1:{server.server_address[1]}'
    yield state
    server.shutdown()


def run_case(candidate, tmp_path, replies, scenario=None, variant='A'):
    model = FakeModel(replies)
    runtime = FakeRuntime(candidate.base_url, model, tmp_path)
    receipt = runner.execute_case(case_id=f'P10:{variant}:local-docker', scenario=scenario or FakeP10(),
                                  variant=variant, case=case(variant), candidate_digest=DIGEST, runtime=runtime)
    return receipt, model, runtime


def test_full_loop_passes_on_correct_state_with_runner_owned_evidence(candidate, tmp_path):
    receipt, model, runtime = run_case(candidate, tmp_path, ['I will look into it.', tool(CORRECT_WHERE), FINAL])
    assert receipt['status'] == 'pass', receipt
    assert [a['status'] for a in receipt['assertions']] == ['pass', 'pass']
    assert receipt['finalAnswerCheck']['status'] == 'pass'
    assert receipt['nudges'] == 1 and receipt['temperature'] is None and receipt['attempt'] == 1
    assert runtime.torn_down
    # Evidence hashes are the recorder's own records of raw response bytes.
    records = {row['step']: row for row in receipt['controllerRecords']}
    for row in receipt['assertions']:
        for item in row['evidence']:
            raw = base64.b64decode(json.loads((tmp_path / 'case/controller-records' / (item['step'] + '.json'))
                                              .read_text())['bodyBase64'])
            assert item['sha256'] == records[item['step']]['sha256'] == 'sha256:' + hashlib.sha256(raw).hexdigest()
    replay = records['p10-replay-query']
    first = receipt['sessions'][0]['actions'][0]
    assert replay['principal'] == 'controller' and replay['replayOf'] == f"occ:1:1:{first['digest']}"
    # The model acted with its own agent credential; the controller replay used the admin key.
    assert [c[0] for c in candidate.mcp_calls] == ['key-2', 'admin']
    # Custody: bootstrap minted the agent key, then was revoked; the probe is a recorded 401.
    assert receipt['custody']['agentMintedBy'] == 'bootstrap'
    assert receipt['custody']['postRevocationProbeStatus'] == 401
    assert receipt['custody']['bootstrapRevokedCredentialIds'] == ['key-1']
    # Logical roles were resolved from the seed-phase publish handle.
    assert {k: v for k, v in receipt['roles'].items() if k.startswith('seeded:')} == {
        'seeded:layer:shelters': '0', 'seeded:service:shelters': 'p10-shelters'}
    assert receipt['roles']['session:current'] == receipt['sessions'][0]['sessionId']
    # Receipts carry digests, never action bodies or the private log path.
    assert 'logPath' not in json.dumps(receipt) and CORRECT_WHERE not in json.dumps(receipt)
    runner.validate_receipt(receipt)


def test_model_visible_bytes_carry_only_prompt_tool_surface_and_protocol(candidate, tmp_path):
    receipt, model, _ = run_case(candidate, tmp_path, [tool(CORRECT_WHERE), FINAL])
    first = model.sent[0]
    assert [m['role'] for m in first] == ['system', 'user']
    payload = json.loads(first[1]['content'])
    assert payload['stage']['customerRequest'] == case()['prompts']['A']
    assert payload['serverAuthoredToolView']['tools'] == Candidate.TOOLS
    assert set(payload) == {'task', 'candidateRelease', 'stage', 'serverAuthoredToolView'}
    # Harness-authored bytes: everything the model sees except the server-authored tool view.
    user = json.dumps({**payload, 'serverAuthoredToolView': None})
    forbidden = ['requiredSequence', 'requiredOutcome', 'planContract', 'currentState', 'taskOutcome',
                 'serviceId', 'layerId', 'where', 'is_open', 'capacity', 'source_id', 'geometry.buffer',
                 'p10-shelters', '"s3"', '"s4"', 'openCapacityOver50', 'Kestrel Central']
    forbidden += [stage['command'] for stage in JOURNEY['stages']]
    for text in forbidden + ['terminalSurface', '"observation"', 'honua_query_features', '"command"']:
        assert text not in user, text
    # The system message is the canary contract (it names server-authored tools, as the canary does)
    # plus the one completion sentence; no parameter, process, value or stage hint.
    system = first[0]['content']
    assert system == runner.corpus_system_contract(['honua_query_features', 'honua_update_features'])
    for text in forbidden:
        assert text not in system, text
    # The expected answer is nowhere in what the model saw before its first tool result.
    expected = runner.oracle.fixture_expectation('A', 'filter.exact')['resourceIds']
    assert json.dumps(expected) not in json.dumps(first)
    # The receipt hashes exactly the bytes sent on each call.
    for sent, digest in zip(model.sent, receipt['sessions'][0]['modelVisibleDigests']):
        assert digest == 'sha256:' + hashlib.sha256(json.dumps(sent).encode()).hexdigest()
    exported = sorted((tmp_path / 'case/model-visible').glob('*.json'))
    assert len(exported) == len(model.sent)
    assert 'temperature' not in json.dumps(model.sent)


def test_wrong_filter_fails(candidate, tmp_path):
    done = json.dumps({'kind': 'final', 'answer': 'Done: the open shelters are listed.'})
    receipt, _, _ = run_case(candidate, tmp_path, [tool('is_open = true'), done])
    assert receipt['status'] == 'fail'
    assert [a['status'] for a in receipt['assertions']] == ['fail', 'pass']
    # Claiming completion while the observed state failed is a false success.
    assert receipt['finalAnswerCheck']['status'] == 'fail' and receipt['finalAnswerCheck']['false_success']


def test_source_mutation_fails_preservation(candidate, tmp_path):
    receipt, _, _ = run_case(candidate, tmp_path, [tool(None, 'honua_update_features'), tool(CORRECT_WHERE), FINAL])
    assert receipt['status'] == 'fail'
    statuses = {a['assertion']: a['status'] for a in receipt['assertions']}
    assert statuses == {'filter.exact': 'fail', 'source-content.unchanged': 'fail'}


def test_no_query_fails_closed_and_prose_is_never_evidence(candidate, tmp_path):
    claim = json.dumps({'kind': 'final', 'answer': 'The matching shelters are s3 and s4.'})
    receipt, _, _ = run_case(candidate, tmp_path, [claim])
    assert receipt['status'] == 'fail'
    assert receipt['assertions'][0]['status'] == 'fail'
    assert 'unrecorded step' in receipt['assertions'][0]['reason']


def test_two_malformed_replies_fail_the_attempt_without_retry(candidate, tmp_path):
    receipt, model, _ = run_case(candidate, tmp_path, ['prose', 'more prose'])
    assert receipt['status'] == 'fail' and receipt['failure']['category'] == 'model'
    assert receipt['nudges'] == 1 and len(model.sent) == 2


def test_scenario_authored_values_and_unrecorded_bodies_cannot_pass(tmp_path):
    expected = {'filter.exact': {'resourceIds': ['s3', 's4']}}
    authored = Observation('filter.exact', {'resourceIds': ['s3', 's4']},
                           {'requests': [{'method': 'GET', 'url': '/q', 'status': 200,
                                          'body_b64': base64.b64encode(b'{"resourceIds":["s3","s4"]}').decode(),
                                          'sha256': 'x'}]})
    assert runner.score_assertions(['filter.exact'], [authored], expected, records={})[0]['status'] == 'fail'
    projected = Observation('filter.exact', None, {'projection': 'extract/v1',
                                                   'spec': {'object': {'resourceIds': {'body': 'nope', 'pointer': '/ids'}}}})
    result = runner.score_assertions(['filter.exact'], [projected], expected, records={})[0]
    assert result['status'] == 'fail' and 'unrecorded step' in result['reason']
    assert runner.score_assertions(['filter.exact'], [], expected, records={})[0]['reason'] == 'missing observation'


def test_held_out_variant_is_refused_before_any_work(tmp_path):
    with pytest.raises(ValueError):
        runner.select_case(runner.PACK, 'P10', 'C')
    with pytest.raises(ValueError):
        runner.build_inventory(runner.PACK, ['P10'], ['A', 'C'])
    # The full pack is refused by name before it is read.
    with pytest.raises(ValueError, match='A/B-only'):
        runner.select_case(ROOT / 'certification/agent-e2e/pack.v2.json', 'P10', 'A')
    tampered = tmp_path / 'pack.v2.AB-only.json'
    tampered.write_bytes(runner.PACK.read_bytes() + b' ')
    with pytest.raises(ValueError, match='digest'):
        runner.select_case(tampered, 'P10', 'A')
    out = tmp_path / 'receipt.json'
    assert runner.main(['case', '--scenario', 'P10', '--variant', 'C', '--model', 'm', '--image-ref', 'x',
                        '--output', str(out)]) == 1
    receipt = json.loads(out.read_text())
    assert receipt['status'] == 'infrastructure' and not receipt['sessions']


def test_aggregate_rejects_missing_duplicate_and_mixed_receipts(candidate, tmp_path):
    (tmp_path / 'a').mkdir()
    (tmp_path / 'b').mkdir()
    passing, _, _ = run_case(candidate, tmp_path / 'a', [tool(CORRECT_WHERE), FINAL])
    failing, _, _ = run_case(candidate, tmp_path / 'b', [tool('1=1'), FINAL])
    failing = {**failing, 'caseId': 'P10:B:local-docker'}
    cases = runner.build_inventory(runner.PACK, ['P10'], ['A', 'B'])
    summary = runner.aggregate(cases, [passing, failing])
    assert summary['overall']['passed'] == 1 and summary['overall']['total'] == 2
    assert summary['scenarios']['P10']['interval95'] == pytest.approx(runner.clopper_pearson(1, 2))
    for rows in ([passing], [passing, failing, passing], [passing, {**failing, 'candidateDigest': 'sha256:' + 'b' * 64}],
                 [passing, {**failing, 'attempt': 2}], [passing, {'caseId': failing['caseId'], 'status': 'pass'}],
                 [passing, {**failing, 'status': 'pass'}]):
        with pytest.raises(ValueError):
            runner.aggregate(cases, rows)
    paths = []
    for index, receipt in enumerate([passing, failing]):
        path = tmp_path / f'r{index}' / 'receipt.json'
        path.parent.mkdir()
        path.write_text(json.dumps(receipt))
        paths.append(path)
    assert runner.aggregate_files(cases, paths)['overall']['total'] == 2
    with pytest.raises(ValueError):
        runner.aggregate_files(cases, paths[:1])


def test_exact_binomial_boundaries_and_midpoint():
    assert runner.clopper_pearson(0, 10) == pytest.approx((0, 0.3084971078))
    assert runner.clopper_pearson(10, 10) == pytest.approx((0.6915028922, 1))
    assert runner.clopper_pearson(5, 10) == pytest.approx((0.187086, 0.812914), abs=1e-5)


def test_roles_resolve_only_from_recorded_phases(candidate, tmp_path):
    import os
    os.environ['HONUA_ADMIN_PASSWORD'] = ADMIN
    recorder = ControllerRecorder(base_url=candidate.base_url, workdir=tmp_path, candidate_digest=DIGEST,
                                  attempt_id='corpus-test', principals={'controller': ('HONUA_ADMIN_PASSWORD', 'admin')})
    recorder.json('seed-keys', 'GET', '/api/v1/admin/api-keys/')
    recorder.json('seed-publish', 'POST', '/api/v1/operations/service.publish/submit', {'serviceName': 'svc'})
    recorder.set_phase('session')
    with pytest.raises(ValueError):
        recorder.json('during-session', 'GET', '/api/v1/admin/api-keys/')
    recorder.set_phase('observe')
    candidate.keys['s'] = {'id': 'key-9', 'revoked': False, 'permissions': []}
    recorder.json('after-publish', 'POST', '/api/v1/operations/service.publish/submit', {'serviceName': 'new'})
    from agent_e2e_local_projections import resolve_roles, substitute_roles
    roles = resolve_roles([RoleBinding('seeded:service:svc', 'seed-publish', pointer='/data/resourceIds/serviceName')],
                          recorder.records, agent_principal='agent')
    assert substitute_roles({'id': {'$role': 'seeded:service:svc'}}, roles) == {'id': 'svc'}
    with pytest.raises(ValueError):
        substitute_roles({'id': {'$role': 'created:map'}}, roles)
    with pytest.raises(ValueError):  # a seeded role cannot come from an observe-phase read
        resolve_roles([RoleBinding('seeded:service:x', 'after-publish', pointer='/data/resourceIds/serviceName')],
                      recorder.records, agent_principal='agent')
    with pytest.raises(ValueError):  # a created role needs an owner and a seed-phase baseline
        resolve_roles([RoleBinding('created:svc', 'after-publish', items='/data', id='/x')],
                      recorder.records, agent_principal='agent')


def test_created_role_requires_exactly_one_new_owned_resource():
    from agent_e2e_local_projections import resolve_roles
    from agent_e2e_local_recorder import Record

    def record(step, phase, items):
        raw = json.dumps({'items': items, 'next': None}).encode()
        return Record(step, phase, 'http', 'controller', None, 'GET', '/maps', 200, raw,
                      'sha256:' + hashlib.sha256(raw).hexdigest())
    before = [{'id': 'm1', 'owner': 'other'}]
    records = {'before': record('before', 'seed', before),
               'after': record('after', 'observe', before + [{'id': 'm2', 'owner': 'agent'}, {'id': 'm3', 'owner': 'x'}])}
    binding = RoleBinding('created:map', 'after', items='/items', id='/id', owner='/owner', before='before')
    assert resolve_roles([binding], records, agent_principal='agent') == {'created:map': 'm2'}
    records['after'] = record('after', 'observe', before + [{'id': 'm2', 'owner': 'agent'}, {'id': 'm4', 'owner': 'agent'}])
    with pytest.raises(ValueError, match='matched 2'):
        resolve_roles([binding], records, agent_principal='agent')


def test_runtime_proxy_omits_temperature_like_the_shipped_client(monkeypatch, tmp_path):
    canary = runner._canary()
    runtime = runner.LocalRuntime(driver=None, model='us.anthropic.claude-sonnet-4-6', release_id='r')
    stack = Stack('http://127.0.0.1:9', ADMIN, None, 'p', 'f', str(tmp_path), DIGEST)
    sent = []

    class Stop(Exception):
        pass

    def capture(request, *, timeout):
        sent.append(json.loads(request.data))
        raise Stop()
    for client in (runtime.proxy(stack), canary.CandidateProxyClient(runtime._endpoint(stack))):
        monkeypatch.setattr(client, '_manifest', lambda headers: {})
        monkeypatch.setattr(client, '_open', capture)
        with pytest.raises(Stop):
            client.complete([{'role': 'user', 'content': 'x'}], {'runNonce': 'n'})
    # Trunk's canary (#1157) also omits temperature by default; the runner pins the omission explicitly.
    assert 'temperature' not in sent[0] and 'temperature' not in sent[1]


def _record(step, phase='observe', status=200, body=None, path='/x', principal_id='admin', kind='http', arguments=None):
    from agent_e2e_local_recorder import Record
    raw = json.dumps(body).encode()
    return Record(step, phase, kind, 'controller', principal_id, 'GET', path, status, raw,
                  'sha256:' + hashlib.sha256(raw).hexdigest(), None,
                  None if arguments is None else json.dumps(arguments).encode())


def test_permission_matrix_projection_binds_targets_principals_and_disclosure():
    roles = {'seeded:map:ours': 'map-1', 'seeded:map:theirs': 'map-2'}
    records = {'own': _record('own', body={'data': {'id': 'map-1'}}, path='/api/v1/maps/map-1', principal_id='key-a'),
               'foreign': _record('foreign', status=404, body={'error': 'not found'}, path='/api/v1/maps/map-2',
                                  principal_id='key-a'),
               'control': _record('control', body={'data': {'id': 'map-2'}}, path='/api/v1/maps/map-2',
                                  principal_id='key-b')}

    def row(step, role):
        return {'object': {'principalId': {'principal': step}, 'method': {'method': step},
                           'resourceId': {'target': step, 'role': role}, 'status': {'status': step},
                           'responseSha256': {'sha256': step},
                           'disclosedResourceIds': {'disclosed': step, 'roles': ['seeded:map:theirs']}}}
    spec = {'object': {'requests': {'list': [row('own', 'seeded:map:ours'), row('foreign', 'seeded:map:theirs'),
                                             row('control', 'seeded:map:theirs')]}}}
    expected = {'requests': [
        {'principalId': 'key-a', 'method': 'GET', 'resourceId': {'$role': 'seeded:map:ours'}, 'status': 200},
        {'principalId': 'key-a', 'method': 'GET', 'resourceId': {'$role': 'seeded:map:theirs'}, 'status': 404},
        {'principalId': 'key-b', 'method': 'GET', 'resourceId': {'$role': 'seeded:map:theirs'}, 'status': 200}]}
    obs = Observation('publication.private', None, {'projection': 'extract/v1', 'spec': spec})
    assert runner.score_assertions(['publication.private'], [obs], {'publication.private': expected}, records=records,
                                   roles=roles)[0]['status'] == 'pass'
    # A probe cannot claim a resource its request did not address.
    swapped = {'object': {'requests': {'list': [row('own', 'seeded:map:ours'), row('own', 'seeded:map:theirs'),
                                                row('control', 'seeded:map:theirs')]}}}
    bad = Observation('publication.private', None, {'projection': 'extract/v1', 'spec': swapped})
    result = runner.score_assertions(['publication.private'], [bad], {'publication.private': expected}, records=records, roles=roles)
    assert result[0]['status'] == 'fail' and 'did not address' in result[0]['reason']
    # A denial that discloses the foreign id fails.
    records['foreign'] = _record('foreign', status=404, body={'id': 'map-2'}, path='/api/v1/maps/map-2',
                                 principal_id='key-a')
    assert runner.score_assertions(['publication.private'], [obs], {'publication.private': expected}, records=records,
                                   roles=roles)[0]['status'] == 'fail'


def test_binding_projection_from_custody_and_secret_scan():
    custody = {'bootstrapPrincipalId': 'key-1', 'bootstrapRevokedCredentialIds': ['key-1'],
               'postRevocationProbeStatus': 401}
    spec = {'object': {'bootstrapPrincipalId': {'custody': 'bootstrapPrincipalId'},
                       'revokedCredentialIds': {'custody': 'bootstrapRevokedCredentialIds'},
                       'postRevocationProbeStatus': {'custody': 'postRevocationProbeStatus'}}}
    expected = {'bootstrapPrincipalId': 'key-1', 'revokedCredentialIds': ['key-1'], 'postRevocationProbeStatus': 401}
    obs = Observation('custody.bootstrap-revoked', None, {'projection': 'extract/v1', 'spec': spec})
    assert runner.score_assertions(['custody.bootstrap-revoked'], [obs], {'custody.bootstrap-revoked': expected},
                                   records={}, custody=custody)[0]['status'] == 'pass'
    custody['postRevocationProbeStatus'] = 200
    assert runner.score_assertions(['custody.bootstrap-revoked'], [obs], {'custody.bootstrap-revoked': expected},
                                   records={}, custody=custody)[0]['status'] == 'fail'
    from agent_e2e_local_projections import Context, evaluate
    records = {'cfg': _record('cfg', body={'connection': 'password=hunter2-secret'})}
    ctx = Context(records, credentials=('hunter2-secret', 'unused-credential'))
    assert evaluate({'leakedCredentials': ['cfg']}, ctx) == [hashlib.sha256(b'hunter2-secret').hexdigest()[:16]]


def test_protected_content_requires_seed_before_and_observe_after():
    from agent_e2e_local_projections import Context, evaluate
    records = {'b': _record('b', phase='seed', body={'data': {'title': 'Map', 'updatedAt': 1}}, path='/maps/map-1'),
               'a': _record('a', phase='observe', body={'data': {'title': 'Map', 'updatedAt': 2}}, path='/maps/map-1')}
    spec = {'protectedContent': [{'label': 'map', 'before': 'b', 'after': 'a', 'resource': ['seeded:map:m'],
                                  'content': {'object': {'title': {'body': '$step', 'pointer': '/data/title'}}}}]}
    roles = {'seeded:map:m': 'map-1'}
    value = evaluate(spec, Context(records, roles))
    assert value['before'] == value['after'] == [{'resourceId': 'map', 'content': {'title': 'Map'}}]
    swapped = {'protectedContent': [{'label': 'map', 'before': 'a', 'after': 'b', 'pointer': '/data',
                                     'resource': ['seeded:map:m']}]}
    with pytest.raises(ValueError, match='wrong phase'):
        evaluate(swapped, Context(records, roles))
    # Third review, item 1: a snapshot of another resource (or a different request) is not preservation.
    other = dict(records, a=_record('a', phase='observe', body={'data': {'title': 'Map', 'updatedAt': 2}},
                                    path='/maps/map-2'))
    with pytest.raises(ValueError, match='different requests'):
        evaluate(spec, Context(other, roles))
    both_other = {'b': _record('b', phase='seed', body={'data': {'title': 'Map'}}, path='/maps/map-2'),
                  'a': _record('a', phase='observe', body={'data': {'title': 'Map'}}, path='/maps/map-2')}
    with pytest.raises(ValueError, match='does not address'):
        evaluate(spec, Context(both_other, roles))


# ---- Astra review 5721829419: one negative control per blocking finding ------------------------

class TamperingP10(FakeP10):
    """A scenario that tries to change what gets scored after the controller read it."""

    def __init__(self, how):
        self.how = how

    def observe(self, stack, seeded, variant, run=None):
        rows = super().observe(stack, seeded, variant, run)
        recorder = stack.extra['recorder']
        record = recorder.records['p10-replay-query']
        if self.how == 'mutate-parsed':
            # Mutating a parsed view cannot reach scoring: every view is re-derived from raw bytes.
            features = record.json['result']['structuredContent']['geojson']['features']
            del features[:]
        elif self.how == 'forge-record':
            import dataclasses
            message = record.json
            content = message['result']['structuredContent']
            keep = {'s3', 's4'}
            content['geojson']['features'] = [f for f in content['geojson']['features']
                                              if f['properties']['source_id'] in keep]
            content['returnedCount'] = 2
            raw = json.dumps(message).encode()
            forged = dataclasses.replace(record, raw=raw, sha256='sha256:' + hashlib.sha256(raw).hexdigest())
            recorder._records['p10-replay-query'] = forged
        elif self.how == 'score-seed-read':
            rows[0] = Observation('filter.exact', None, {'projection': 'extract/v1', 'spec': {'object': {
                'resourceIds': {'sorted': {'map': {'body': 'p10-source-before', 'pointer': '/features'},
                                           'to': {'item': '/attributes/source_id'}}}}}})
        return rows


def test_finding1_scored_values_are_bound_to_runner_ledger_bytes(candidate, tmp_path):
    # A wrong query: a forged record that "fixes" the result must not pass.
    (tmp_path / 'a').mkdir()
    receipt, _, _ = run_case(candidate, tmp_path / 'a', [tool('is_open = true'), FINAL], scenario=TamperingP10('forge-record'))
    # Scoring reads the runtime vault, which still holds the bytes the controller actually received.
    assert receipt['assertions'][0]['status'] == 'fail' and 'differs' in receipt['assertions'][0]['reason']
    # A correct query: mutating the parsed view does not change the score.
    (tmp_path / 'b').mkdir()
    receipt, _, _ = run_case(candidate, tmp_path / 'b', [tool(CORRECT_WHERE), FINAL], scenario=TamperingP10('mutate-parsed'))
    assert receipt['assertions'][0]['status'] == 'pass'
    # A seed-phase read is never scored as post-session state.
    (tmp_path / 'c').mkdir()
    receipt, _, _ = run_case(candidate, tmp_path / 'c', [tool(CORRECT_WHERE), FINAL], scenario=TamperingP10('score-seed-read'))
    assert receipt['assertions'][0]['status'] == 'fail' and 'frozen projection' in receipt['assertions'][0]['reason']


def test_finding1_bytes_altered_after_recording_fail():
    import dataclasses
    record = _record('r', body={'resourceIds': ['s3', 's4']})
    tampered = dataclasses.replace(record, raw=b'{"resourceIds":["s3","s4","s9"]}')
    obs = Observation('filter.exact', None, {'projection': 'extract/v1',
                                             'spec': {'object': {'resourceIds': {'body': 'r', 'pointer': '/resourceIds'}}}})
    expected = {'filter.exact': {'resourceIds': ['s3', 's4']}}
    assert runner.score_assertions(['filter.exact'], [obs], expected, records={'r': record})[0]['status'] == 'pass'
    result = runner.score_assertions(['filter.exact'], [obs], expected, records={'r': tampered})[0]
    assert result['status'] == 'fail' and 'differ' in result['reason']


def test_finding2_worker_custody_is_not_synthesized_from_key_revocation(candidate, tmp_path):
    receipt, _, _ = run_case(candidate, tmp_path, [tool(CORRECT_WHERE), FINAL])
    custody = receipt['custody']
    for field in ('bootstrapWorkerId', 'taskWorkerId', 'bootstrapWorkerTerminalState', 'taskProvisioningCredentialIds'):
        assert field not in custody
    spec = {'object': {name: {'custody': name} for name in ('bootstrapWorkerId', 'taskWorkerId',
            'bootstrapWorkerTerminalState', 'taskPrincipalId', 'taskProvisioningCredentialIds')}}
    obs = Observation('custody.worker-replaced', None, {'projection': 'extract/v1', 'spec': spec})
    expected = {'custody.worker-replaced': {'bootstrapWorkerId': 'a', 'taskWorkerId': 'b', 'taskPrincipalId': 'b',
                'bootstrapWorkerTerminalState': 'terminated', 'taskProvisioningCredentialIds': []}}
    # On local Docker the prereg excludes it (reported, never a pass) ...
    result = runner.score_assertions(['custody.worker-replaced'], [obs], expected, records={}, custody=custody)[0]
    assert result == {'assertion': 'custody.worker-replaced', 'status': 'excluded',
                      'ruling': 'custody.worker-replaced@local-docker'}
    # ... and even if scored, no synthesized worker fact could satisfy it.
    from agent_e2e_local_projections import Context, evaluate
    with pytest.raises(ValueError, match='custody fact not recorded'):
        evaluate(spec, Context({}, custody=custody))


def test_finding3_created_roles_require_owner_and_post_seed_creation():
    from agent_e2e_local_projections import resolve_roles
    from agent_e2e_local_recorder import Record

    def record(step, phase, items, path='/maps'):
        raw = json.dumps({'items': items, 'next': None}).encode()
        return Record(step, phase, 'http', 'controller', None, 'GET', path, 200, raw,
                      'sha256:' + hashlib.sha256(raw).hexdigest())
    pre_seed_owned = {'id': 'm0', 'owner': 'agent'}
    foreign_new = {'id': 'm9', 'owner': 'someone-else'}
    records = {'before': record('before', 'seed', [pre_seed_owned]),
               'after': record('after', 'observe', [pre_seed_owned, foreign_new])}
    # Owner-only would have accepted the pre-seed resource; baseline-only the foreign one.
    for binding in (RoleBinding('created:map', 'after', items='/items', id='/id', owner='/owner'),
                    RoleBinding('created:map', 'after', items='/items', id='/id', before='before')):
        with pytest.raises(ValueError, match='authorship proof'):
            resolve_roles([binding], records, agent_principal='agent')
    both = RoleBinding('created:map', 'after', items='/items', id='/id', owner='/owner', before='before')
    with pytest.raises(ValueError, match='matched 0'):
        resolve_roles([both], records, agent_principal='agent')
    # The baseline must be the same enumeration, read before the session.
    records['other'] = record('other', 'seed', [], path='/other')
    with pytest.raises(ValueError, match='same enumeration'):
        resolve_roles([RoleBinding('created:map', 'after', items='/items', id='/id', owner='/owner', before='other')],
                      records, agent_principal='agent')


def test_finding4_replay_requires_membership_in_this_cases_action_log(candidate, tmp_path):
    import os
    from agent_e2e_local_recorder import LoggedAction, action_bytes
    from agent_e2e_local_recorder import EvidenceVault
    os.environ['HONUA_ADMIN_PASSWORD'] = ADMIN
    vault = EvidenceVault('corpus-test')
    recorder = ControllerRecorder(base_url=candidate.base_url, workdir=tmp_path, candidate_digest=DIGEST, vault=vault,
                                  attempt_id='corpus-test', principals={'controller': ('HONUA_ADMIN_PASSWORD', 'admin')})
    action = {'kind': 'tool_call', 'tool': 'honua_query_features',
              'arguments': {'serviceId': 'p10-shelters', 'layerId': 0, 'where': CORRECT_WHERE}}
    path = tmp_path / 'action-0001.json'
    path.write_bytes(action_bytes(action))
    digest = 'sha256:' + hashlib.sha256(action_bytes(action)).hexdigest()
    logged = LoggedAction(1, 1, digest, 'tool_call', action['tool'], action['arguments'], None, 'pass', str(path))
    recorder.set_phase('observe')
    with pytest.raises(ValueError, match='not in this case'):   # empty harness log: a nonmember
        recorder.replay('replay-empty', logged)
    assert not hasattr(recorder, 'append_actions')                 # scenarios cannot extend the inventory
    vault.append_actions(1, [{'session': 1, 'sequence': 1, 'digest': digest, 'logPath': str(path), 'status': 'pass'}])
    foreign = LoggedAction(2, 1, digest, 'tool_call', action['tool'], action['arguments'], None, 'pass', str(path))
    with pytest.raises(ValueError, match='not in this case'):   # a session this case never ran
        recorder.replay('replay-foreign', foreign)
    recorder.replay('replay-member', recorder.actions()[0])
    assert recorder.records['replay-member'].replay_of == 'occ:1:1:' + digest
    # Rewriting the private file cannot change what is replayed: the vault holds the sealed bytes.
    path.write_bytes(action_bytes({**action, 'arguments': {**action['arguments'], 'where': '1=1'}}))
    recorder.replay('replay-after-file-edit', recorder.actions()[0])
    assert recorder.records['replay-after-file-edit'].request_arguments['arguments']['where'] == CORRECT_WHERE
    entry, raw, link = vault._actions[0]
    vault._actions[0] = ((entry[0], entry[1], 'sha256:' + 'f' * 64) + entry[3:], raw, link)
    with pytest.raises(ValueError, match='chain'):                # altered log entry
        recorder.actions()
    with pytest.raises(ValueError):                               # sessions are sealed once
        vault.append_actions(1, [])


def test_finding5_model_visible_bytes_offer_only_admitted_actions(candidate, tmp_path):
    receipt, model, _ = run_case(candidate, tmp_path, ['not json', tool(CORRECT_WHERE), FINAL])
    for sent in model.sent:
        rendered = json.dumps(sent)
        for text in ('terminal', 'command', 'requiredSequence', 'terminalSurface', 'approval'):
            assert text not in rendered.replace('honua_update_features', ''), text
    # The corrective turn names only admitted kinds.
    assert 'tool_call, resource_read or final' in model.sent[1][-1]['content']
    # A terminal action is malformed (corrected once), never executed.
    receipt, model, _ = run_case(candidate, tmp_path / 'x' if (tmp_path / 'x').mkdir() is None else None, [
        json.dumps({'kind': 'terminal_command', 'command': 'honua help'}), FINAL])
    assert receipt['nudges'] == 1 and receipt['sessions'][0]['actions'] == []


def test_finding6_export_failure_never_reattributes_a_scored_failure():
    failed = {'status': 'fail', 'failure': {'phase': 'model-session', 'category': 'post-action-undetermined'}}
    runner.record_export_failure(failed, 'disk full')
    assert failed['status'] == 'fail' and failed['evidenceExport'][0]['status'] == 'fail'
    assert 'infrastructure' not in failed
    passed = {'status': 'pass', 'infrastructure': []}
    runner.record_export_failure(passed, 'disk full')
    assert passed['status'] == 'infrastructure'


# ---- pluggable model sessions -----------------------------------------------------------------

class McpSpeakingSession:
    """Stands in for a CLI agent: it talks MCP to the runner's recording proxy, never to the stack."""
    agent_type = 'test-cli'

    def __init__(self, runtime, stack):
        self.stack = stack

    def run(self, context):
        import os
        from agent_e2e_local_api import SessionResult
        from agent_e2e_mcp import Client, McpError
        endpoint = context.mcp_proxy()
        assert endpoint.url.startswith('http://127.0.0.1:') and ADMIN not in endpoint.token
        os.environ['TEST_PROXY_TOKEN'] = endpoint.token.removeprefix('Bearer ')
        client = Client(endpoint.url, credential_env='TEST_PROXY_TOKEN', credential_kind='bearer', timeout=60)
        client.initialize()
        assert [t['name'] for t in client.list_tools()['tools']] == ['honua_query_features', 'honua_update_features']
        client.call_tool('honua_query_features', {'serviceId': 'p10-shelters', 'layerId': 0, 'where': CORRECT_WHERE})
        with pytest.raises(McpError):
            client.read_resource('honua://catalog/secrets')
        client.close()
        return SessionResult(agent_type=self.agent_type, model_id='test-model', final_answer='Two shelters match.',
                             temperature=None, temperature_policy='cli default',
                             extra={'claimedActions': [{'tool': 'honua_update_features'}]})


def test_pluggable_session_actions_are_recorded_by_the_runner_proxy(candidate, tmp_path):
    runner.SESSIONS['test-cli'] = McpSpeakingSession
    try:
        model = FakeModel([])
        runtime = FakeRuntime(candidate.base_url, model, tmp_path)
        runtime.agent = 'test-cli'
        receipt = runner.execute_case(case_id='P10:A:local-docker', scenario=FakeP10(), variant='A', case=case('A'),
                                      candidate_digest=DIGEST, runtime=runtime)
    finally:
        del runner.SESSIONS['test-cli']
    assert receipt['agentType'] == 'test-cli' and receipt['arm'] == 'agent:test-cli/corpus-v1'
    assert receipt['model'] == 'test-model' and model.sent == []
    actions = receipt['sessions'][0]['actions']
    assert [(a['kind'], a.get('tool'), a['status']) for a in actions] == [
        ('tool_call', 'honua_query_features', 'pass'), ('resource_read', None, 'fail')]
    # The proxy forwarded with the agent credential; the session never held it.
    assert [c[0] for c in candidate.mcp_calls] == ['key-2', 'admin']
    assert [a['status'] for a in receipt['assertions']] == ['pass', 'pass']
    runner.validate_receipt(receipt)
    with pytest.raises(ValueError, match='mix agent arms'):
        default = {**receipt, 'caseId': 'P10:B:local-docker', 'agentType': 'bedrock-proxy', 'arm': runner.ARM,
                   'sessions': [{**receipt['sessions'][0], 'modelVisibleDigests': ['sha256:' + 'c' * 64]}]}
        runner.aggregate(runner.build_inventory(runner.PACK, ['P10'], ['A', 'B']), [receipt, default])


def test_unknown_agent_is_refused_before_any_stack(tmp_path):
    out = tmp_path / 'receipt.json'
    assert runner.main(['case', '--scenario', 'P10', '--variant', 'A', '--model', 'm', '--agent', 'nope',
                        '--image-ref', 'x', '--output', str(out)]) == 1
    assert json.loads(out.read_text())['status'] == 'infrastructure'


# ---- runner hooks for scenario lanes ------------------------------------------------------------

class HookP10(FakeP10):
    """FakeP10 with configurable hooks (two sessions, restart, approver, fault, files, reports)."""

    def __init__(self, *, sessions=1, restart=False, approver=False, fault=None, files=(), report=False,
                 approve_with=None):
        self.n, self.restart, self.approver, self.fault = sessions, restart, approver, fault
        self.files, self.report, self.approve_with = list(files), report, approve_with
        self.between_calls = []

    def sessions_required(self): return self.n
    def needs_approver(self): return self.approver
    def fault_plan(self, variant): return self.fault
    def customer_files(self, variant): return self.files

    def between_sessions(self, stack, seeded, variant, after_session):
        self.between_calls.append((after_session, stack.extra['recorder'].phase))
        if self.approve_with:
            stack.extra['approve'](self.approve_with)
        return {'restartServer': True} if self.restart else None

    def observe(self, stack, seeded, variant, run=None):
        rows = super().observe(stack, seeded, variant, run)
        recorder = stack.extra['recorder']
        if self.report:
            ingest = [a for a in recorder.actions() if a.tool == 'honua_ingest_dataset']
            recorder.action_result('import-report', ingest[-1])
        return rows


def run_hook_case(candidate, tmp_path, replies, scenario, monkeypatch=None):
    model = FakeModel(replies)
    runtime = FakeRuntime(candidate.base_url, model, tmp_path)
    receipt = runner.execute_case(case_id='P10:A:local-docker', scenario=scenario, variant='A', case=case('A'),
                                  candidate_digest=DIGEST, runtime=runtime)
    return receipt, model, runtime


def test_between_sessions_restart_is_a_recorded_compose_restart(candidate, tmp_path, monkeypatch):
    import subprocess
    started = {'t': '2026-10-31T07:00:00Z'}
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv[:2])
        out = ''
        if argv[:2] == ['docker', 'ps']:
            out = 'cid-1\n'
        elif argv[:2] == ['docker', 'inspect']:
            out = f'cid-1 {started["t"]} running\n'
        return subprocess.CompletedProcess(argv, 0, out, '')
    monkeypatch.setattr(subprocess, 'run', fake_run)

    def compose_restart(self, stack):
        assert stack.extra['recorder'].phase == 'between'
        started['t'] = '2026-10-31T07:05:00Z'
        return 0
    monkeypatch.setattr(FakeRuntime, 'compose_restart', compose_restart)
    scenario = HookP10(sessions=2, restart=True)
    receipt, model, _ = run_hook_case(candidate, tmp_path, [FINAL, tool(CORRECT_WHERE), FINAL], scenario)
    assert scenario.between_calls == [(1, 'between')]
    fact = receipt['betweenSessions'][0]['restart']
    assert fact['restarted'] and fact['ready'] and fact['command'] == 'docker compose restart server'
    assert fact['containersBefore'] != fact['containersAfter']
    records = {r['step']: r for r in receipt['controllerRecords']}
    assert records['between-1-ready-1']['phase'] == 'between' and records['between-1-ready-1']['status'] == 200
    assert receipt['custody']['restart:between-1'] == fact
    assert len(receipt['sessions']) == 2 and receipt['status'] == 'pass', receipt['assertions']
    # Session 2 is a fresh context: its first request carries none of session 1's messages.
    assert len(model.sent[1]) == 2


def test_restart_is_refused_while_the_model_session_runs(candidate, tmp_path):
    runtime = FakeRuntime(candidate.base_url, FakeModel([]), tmp_path)
    stack = runtime.setup('P10:A:local-docker', DIGEST)
    stack.extra['recorder'].set_phase('session')
    with pytest.raises(ValueError, match='outside the model session'):
        runtime.restart_server(stack, 'mid')


def test_compose_restart_uses_the_drivers_compose_project(monkeypatch, tmp_path):
    seen = []

    class Compose:
        def _run(self, *args, timeout):
            seen.append(args)
            return type('R', (), {'returncode': 0})()

    class Driver:
        @staticmethod
        def _rehydrate(request):
            seen.append(request['workspaceId'])
            return {'workdir': str(tmp_path)}, {'compose': {}}, {}, None, None

        @staticmethod
        def _compose_for(target, manifest, workdir):
            return Compose(), 'http://127.0.0.1:1', 'image'
    monkeypatch.setattr(runner, '_live_driver_module', lambda: Driver)
    stack = Stack('http://127.0.0.1:1', ADMIN, None, 'p', 'f', str(tmp_path), DIGEST, {'workspaceId': 'tj-1'})
    assert runner.LocalRuntime(None, model='m', release_id='r').compose_restart(stack) == 0
    assert seen == ['tj-1', ('restart', 'server')]


def test_fault_plan_fires_once_after_trigger_and_is_recorded(candidate, tmp_path):
    candidate.extra_tools = True
    plan = {'kind': 'http_503_once', 'id': 'map-503', 'after': {'tool': 'honua_create_map'}, 'match': None}
    create = json.dumps({'kind': 'tool_call', 'tool': 'honua_create_map', 'arguments': {}})
    receipt, model, _ = run_hook_case(candidate, tmp_path,
                                      [create, tool(CORRECT_WHERE), tool(CORRECT_WHERE), FINAL], HookP10(fault=plan))
    events = receipt['faultEvents']
    actions = receipt['sessions'][0]['actions']
    assert len(events) == 1 and events[0]['id'] == 'map-503' and events[0]['httpStatus'] == 503
    assert events[0]['actionDigest'] == actions[1]['digest']
    assert [a['status'] for a in actions] == ['pass', 'fail', 'pass']
    assert receipt['faultPlan']['id'] == 'map-503'
    # The model saw the 503 as the result of its call; the server never received that request.
    assert '503' in model.sent[2][-1]['content']
    assert [c[1] for c in candidate.mcp_calls].count('honua_query_features') == 2  # one model call + one replay


def test_fault_that_never_triggers_records_no_event_and_bad_plans_are_refused(candidate, tmp_path):
    plan = {'kind': 'http_503_once', 'after': {'tool': 'honua_create_map'}, 'match': None}
    receipt, _, _ = run_hook_case(candidate, tmp_path, [tool(CORRECT_WHERE), FINAL], HookP10(fault=plan))
    assert receipt['faultEvents'] == []
    for bad in ({'kind': 'http_503_once', 'after': 'map.created'}, {'kind': 'drop-db'},
                {'kind': 'http_503_once', 'after': {'tool': 'x'}, 'extra': 1}):
        with pytest.raises(ValueError):
            runner.validate_fault_plan(bad)
    from agent_e2e_local_faults import FaultInjector
    injector = FaultInjector({'kind': 'http_503_once', 'after': {'tool': 'a'}, 'match': {'tool': 'b'}})
    execute = injector.wrap(lambda kind, action: {'status': 'ok'})
    assert execute('tool_call', {'tool': 'b'})['status'] == 'ok'          # not armed yet
    assert execute('tool_call', {'tool': 'a'})['status'] == 'ok'          # trigger
    assert execute('tool_call', {'tool': 'c'})['status'] == 'ok'          # not a match
    assert execute('tool_call', {'tool': 'b'})['httpStatus'] == 503       # fires
    assert execute('tool_call', {'tool': 'b'})['status'] == 'ok'          # once only
    assert len(injector.events) == 1


def test_separate_approver_is_minted_recorded_and_never_model_visible(candidate, tmp_path):
    scenario = HookP10(sessions=2, approver=True, approve_with='prop-1')
    receipt, model, runtime = run_hook_case(candidate, tmp_path, [FINAL, tool(CORRECT_WHERE), FINAL], scenario)
    approver_id = receipt['custody']['approverPrincipalId']
    assert candidate.approvals == [(approver_id, 'prop-1')]
    record = {r['step']: r for r in receipt['controllerRecords']}['runner-approve-1']
    assert record['principal'] == 'approver' and record['principalId'] == approver_id and record['phase'] == 'between'
    assert approver_id not in (receipt['custody']['agentPrincipalId'],)
    key = next(k for k, v in candidate.keys.items() if v['id'] == approver_id)
    assert key not in json.dumps(model.sent) and key not in json.dumps(receipt)
    assert receipt['status'] == 'pass'


def test_approve_without_an_approver_principal_is_refused(candidate, tmp_path):
    runtime = FakeRuntime(candidate.base_url, FakeModel([]), tmp_path)
    stack = runtime.setup('P10:A:local-docker', DIGEST)
    assert stack.approver_key is None and 'approve' not in stack.extra
    with pytest.raises(ValueError, match='no approver'):
        runtime.approve(stack, 'prop-1')


def test_customer_files_are_frozen_customer_visible_and_shown_with_digests(candidate, tmp_path, monkeypatch):
    import agent_e2e_local_expectations as expectations
    files = ['town-A/invalid-rows.csv']
    original = expectations.load_document

    def documents(scenario_id):
        document = original('P10')
        return {**document, 'scenario': scenario_id, 'customerFiles': {'A': files}} if scenario_id == 'P05' else original(scenario_id)
    monkeypatch.setattr(expectations, 'load_document', documents)

    class P05(HookP10):
        id = 'P05'
    receipt, model, _ = run_hook_case(candidate, tmp_path, [tool(CORRECT_WHERE), FINAL], P05(files=files))
    raw = (ROOT / 'certification/agent-e2e/corpus/town-A/invalid-rows.csv').read_bytes()
    digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
    assert receipt['customerFiles'] == [{'corpusPath': files[0], 'mediaType': 'text/csv', 'sha256': digest}]
    attached = json.loads(model.sent[0][1]['content'])['customerAttachments']
    assert attached == [{'name': 'invalid-rows.csv', 'mediaType': 'text/csv', 'sha256': digest, 'content': raw.decode()}]
    # Third review, item 4: controller-only data, other variants, other scenarios and undeclared files are refused.
    for bad, scenario_id, variant in ((['oracles/town-A.json'], 'P05', 'A'), (['town-A/postgis.sql'], 'P05', 'A'),
                                      (['town-B/invalid-rows.csv'], 'P05', 'A'), (['town-A/invalid-rows.csv'], 'P10', 'A'),
                                      (['town-A/shelters.csv', 'town-A/shelters.csv'], 'P05', 'A'), (['nope.csv'], 'P05', 'A')):
        with pytest.raises(ValueError):
            runner.stage_customer_files(tmp_path / 'x', bad, scenario_id, variant)
    receipt, model, _ = run_hook_case(candidate, tmp_path / 'y' if (tmp_path / 'y').mkdir() is None else None,
                                      [tool(CORRECT_WHERE), FINAL], P05(files=['town-A/shelters.csv']))
    assert receipt['status'] == 'infrastructure' and not model.sent  # differs from the frozen inventory


def test_import_report_comes_from_the_captured_server_response_to_the_sealed_action(candidate, tmp_path):
    candidate.extra_tools = True
    data = 'name,lon,lat\na,1,2\nb,,\n'
    ingest = json.dumps({'kind': 'tool_call', 'tool': 'honua_ingest_dataset',
                         'arguments': {'format': 'csv', 'data': data}})
    receipt, _, _ = run_hook_case(candidate, tmp_path, [ingest, tool(CORRECT_WHERE), FINAL], HookP10(report=True))
    record = {r['step']: r for r in receipt['controllerRecords']}['import-report']
    assert record['origin'] == 'session-result' and record['principal'] == 'agent'
    assert record['replayOf'] == f"occ:1:1:{receipt['sessions'][0]['actions'][0]['digest']}"
    from agent_e2e_local_projections import Context, evaluate
    from agent_e2e_local_recorder import Record
    raw = base64.b64decode(json.loads((tmp_path / 'case/controller-records/import-report.json').read_text())['bodyBase64'])
    rec = Record('import-report', 'observe', 'mcp', 'agent', None, 'POST', '/mcp#tools/call', 200, raw,
                 'sha256:' + hashlib.sha256(raw).hexdigest())
    reasons = evaluate({'map': {'body': 'import-report', 'pointer': '/result/structuredContent/errors'},
                        'to': {'item': '/reason'}}, Context({'import-report': rec}))
    assert reasons == ['missing coordinate'] and record['sha256'] == rec.sha256


def test_action_result_refuses_unsealed_or_altered_responses(candidate, tmp_path):
    import os
    from agent_e2e_local_recorder import LoggedAction, action_bytes
    from agent_e2e_local_recorder import EvidenceVault
    os.environ['HONUA_ADMIN_PASSWORD'] = ADMIN
    vault = EvidenceVault('corpus-test')
    recorder = ControllerRecorder(base_url=candidate.base_url, workdir=tmp_path, candidate_digest=DIGEST, vault=vault,
                                  attempt_id='corpus-test', principals={'controller': ('HONUA_ADMIN_PASSWORD', 'admin')})
    action = {'kind': 'tool_call', 'tool': 'honua_ingest_dataset', 'arguments': {}}
    path, response = tmp_path / 'action-0001.json', tmp_path / 'action-0001.response'
    path.write_bytes(action_bytes(action))
    response.write_bytes(b'{"jsonrpc":"2.0","id":"1","result":{"content":[]}}')
    digest = 'sha256:' + hashlib.sha256(action_bytes(action)).hexdigest()
    row = {'session': 1, 'sequence': 1, 'digest': digest, 'logPath': str(path), 'status': 'pass',
           'resultRawPath': str(response), 'resultHttpStatus': 200,
           'resultRawSha256': 'sha256:' + hashlib.sha256(response.read_bytes()).hexdigest()}
    logged = LoggedAction(1, 1, digest, 'tool_call', 'honua_ingest_dataset', {}, None, 'pass', str(path))
    recorder.set_phase('observe')
    with pytest.raises(ValueError, match='not in this case'):
        recorder.action_result('report-unsealed', logged)
    vault.append_actions(1, [row])
    response.write_bytes(b'{"jsonrpc":"2.0","id":"1","result":{"content":[],"forged":true}}')
    with pytest.raises(ValueError, match='digest mismatch'):
        recorder.action_result('report-altered', logged)


def test_exclusions_are_listed_never_passed_and_only_from_the_prereg_list(candidate, tmp_path):
    from agent_e2e_local_expectations import check_exclusion
    assert check_exclusion('P03', 'tenant.denied', None) == 'tenant.denied'
    assert check_exclusion('P18', 'mutations.absent', {'$excluded': 'P18'}) == 'P18'
    assert check_exclusion('P15', 'custody.worker-replaced', None) == 'custody.worker-replaced@local-docker'
    for scenario_id, assertion, value in (('P10', 'filter.exact', {'$excluded': 'P18'}),
                                          ('P03', 'tenant.denied', {'$excluded': 'founder-ruling-no-multi-tenant-2026.1'})):
        with pytest.raises(ValueError):
            check_exclusion(scenario_id, assertion, value)
    results = runner.score_assertions(['tenant.denied'], [], {'tenant.denied': {'requests': []}}, records={},
                                      scenario_id='P07')
    assert results == [{'assertion': 'tenant.denied', 'status': 'excluded', 'ruling': 'tenant.denied'}]

    class P18(FakeP10):
        id = 'P18'
        def expected(self, variant): return {a: {'$excluded': 'P18'} for a in runner.select_case(runner.PACK, 'P18', variant)['assertions']}
    runtime = FakeRuntime(candidate.base_url, FakeModel([]), tmp_path)
    receipt = runner.execute_case(case_id='P18:A:local-docker', scenario=P18(), variant='A',
                                  case=runner.select_case(runner.PACK, 'P18', 'A'), candidate_digest=DIGEST, runtime=runtime)
    assert receipt['status'] == 'excluded' and not receipt['sessions'] and not runtime.workdirs
    runner.validate_receipt(receipt)
    # Exclusions are reported and stay in n: a passing P10 and an excluded P18 are 1 of 2.
    (tmp_path / 'a').mkdir()
    passing, _, _ = run_case(candidate, tmp_path / 'a', [tool(CORRECT_WHERE), FINAL])
    cases = runner.build_inventory(runner.PACK, ['P10', 'P18'], ['A'])
    summary = runner.aggregate(cases, [passing, receipt])
    assert summary['overall'] == {**summary['overall'], 'passed': 1, 'total': 2, 'excluded': 1}
    assert summary['overall']['excludedCases'] == ['P18:A:local-docker']
    assert summary['exclusions']['list'] == ['tenant.denied', 'P18', 'custody.worker-replaced@local-docker']


def test_third_review5_unregistered_exclusions_cannot_shrink_or_relabel_cases(candidate, tmp_path):
    (tmp_path / 'a').mkdir()
    passing, _, _ = run_case(candidate, tmp_path / 'a', [tool(CORRECT_WHERE), FINAL])
    forged = {**passing, 'caseId': 'P10:B:local-docker', 'status': 'excluded', 'sessions': [],
              'assertions': [{'assertion': a, 'status': 'excluded', 'ruling': 'P18'} for a in case('B')['assertions']]}
    with pytest.raises(ValueError, match='preregistered'):
        runner.validate_receipt(forged)
    with pytest.raises(ValueError):
        runner.aggregate(runner.build_inventory(runner.PACK, ['P10'], ['A', 'B']), [passing, forged])
    partial = {**passing, 'assertions': passing['assertions'][:1]}
    with pytest.raises(ValueError, match='inventory'):
        runner.validate_receipt(partial)
    relabel = {**passing, 'assertions': [{**passing['assertions'][0], 'status': 'excluded', 'ruling': 'tenant.denied'},
                                         passing['assertions'][1]]}
    with pytest.raises(ValueError, match='preregistered'):
        runner.validate_receipt(relabel)


# ---- Astra re-review 5722009491 -----------------------------------------------------------------

class OwnQueryP10(FakeP10):
    """A scenario that runs its own (correct) controller query instead of replaying the model's."""

    def __init__(self, how):
        self.how = how

    def observe(self, stack, seeded, variant, run=None):
        from agent_e2e_local_recorder import LoggedAction, action_bytes
        from agent_e2e_p10_observer import source_path
        recorder = stack.extra['recorder']
        recorder.json('p10-source-after', 'GET', source_path({'service': 'p10-shelters', 'layerId': 0}))
        arguments = {'serviceId': 'p10-shelters', 'layerId': 0, 'where': CORRECT_WHERE}
        if self.how == 'own-query':
            recorder.mcp_call('p10-replay-query', 'honua_query_features', arguments)
        elif self.how == 'fabricated-action':
            action = {'kind': 'tool_call', 'tool': 'honua_query_features', 'arguments': arguments}
            path = Path(stack.workdir) / 'fabricated.json'
            path.write_bytes(action_bytes(action))
            fake = LoggedAction(1, 2, 'sha256:' + hashlib.sha256(action_bytes(action)).hexdigest(), 'tool_call',
                                action['tool'], arguments, None, 'pass', str(path))
            try:
                recorder.replay('p10-replay-query', fake)
            except ValueError:
                pass
        return [Observation('filter.exact', None, {'projection': 'extract/v1'}),
                Observation('source-content.unchanged', None, {'projection': 'extract/v1'})]


@pytest.mark.parametrize('how', ['own-query', 'fabricated-action'])
def test_rereview1_2_wrong_model_query_cannot_pass_through_scenario_queries(candidate, tmp_path, how):
    receipt, _, _ = run_case(candidate, tmp_path, [tool('is_open = true'), FINAL], scenario=OwnQueryP10(how))
    assert receipt['assertions'][0]['status'] == 'fail', receipt['assertions'][0]
    assert receipt['status'] == 'fail'


def test_rereview1_vault_binds_record_metadata_not_only_bytes(tmp_path):
    import dataclasses
    from agent_e2e_local_recorder import EvidenceVault
    vault = EvidenceVault('case-1')
    record = _record('r', phase='seed', body={'resourceIds': ['s3']})
    vault.deliver(record)
    assert vault.records()['r'] is record
    for change in ({'phase': 'observe'}, {'principal': 'agent'}, {'status': 201}, {'path': '/y'},
                   {'replay_of': 'sha256:' + 'a' * 64}, {'origin': 'session-result'}):
        vault._records['r'] = dataclasses.replace(record, **change)
        with pytest.raises(ValueError, match='altered'):
            vault.records()
    vault._records['r'] = record
    with pytest.raises(ValueError):
        vault.deliver(record)  # one write per step


def test_rereview1_frozen_read_rules_reject_a_different_request(candidate, tmp_path):
    frozen = runner.frozen_projections(P10(), 'A')
    assert set(frozen) == {'filter.exact', 'source-content.unchanged'}
    record = _record('p10-source-after', body={'features': []}, path='/rest/services/x/FeatureServer/0/query?f=json&where=1%3D0')
    with pytest.raises(ValueError, match='path'):
        runner.check_reads('p10-source-after', record, frozen['source-content.unchanged']['reads']['p10-source-after'], {})


def test_rereview3_created_roles_need_complete_identical_enumerations():
    from agent_e2e_local_projections import resolve_roles
    from agent_e2e_local_recorder import Record

    def record(step, phase, body, arguments=None, kind='mcp'):
        raw = json.dumps({'jsonrpc': '2.0', 'id': '1', 'result': body}).encode()
        return Record(step, phase, kind, 'controller', None, 'POST', '/mcp#tools/call', 200, raw,
                      'sha256:' + hashlib.sha256(raw).hexdigest(), None,
                      None if arguments is None else json.dumps(arguments).encode())
    scope = {'tool': 'honua_list_maps', 'arguments': {'owner': 'agent'}}
    rows = [{'id': 'm1', 'owner': 'agent'}]
    binding = RoleBinding('created:map', 'after', items='/result/items', id='/id', owner='/owner', before='before')
    complete = {'before': record('before', 'seed', {'items': [], 'total': 0}, scope),
                'after': record('after', 'observe', {'items': rows, 'total': 1}, scope)}
    assert resolve_roles([binding], complete, agent_principal='agent') == {'created:map': 'm1'}
    incomplete = {**complete, 'before': record('before', 'seed', {'items': [], 'total': 3}, scope)}
    with pytest.raises(ValueError, match='count contradicts'):   # the pre-existing m1 may be on an unread page
        resolve_roles([binding], incomplete, agent_principal='agent')
    other_scope = {**complete, 'before': record('before', 'seed', {'items': [], 'total': 0},
                                                {'tool': 'honua_list_maps', 'arguments': {'owner': 'someone'}})}
    with pytest.raises(ValueError, match='different request scope'):
        resolve_roles([binding], other_scope, agent_principal='agent')
    paged = RoleBinding('created:map', 'after', items='/result/items', id='/id', owner='/owner', before='before')
    more = {**complete, 'after': record('after', 'observe', {'items': rows, 'next': 'cursor-2'}, scope)}
    with pytest.raises(ValueError, match='further page'):
        resolve_roles([paged], more, agent_principal='agent')
    unreviewed = {k: record(k, r.phase, json.loads(r.raw)['result'], {'tool': 'honua_list_other', 'arguments': {}})
                  for k, r in complete.items()}
    with pytest.raises(ValueError, match='no reviewed enumeration contract'):
        resolve_roles([binding], unreviewed, agent_principal='agent')
    with pytest.raises(ValueError, match='reviewed enumeration contract, not the binding'):
        resolve_roles([RoleBinding('created:map', 'after', items='/result/items', id='/id', owner='/owner', before='before',
                                   total='/result/total')], complete, agent_principal='agent')


class CrashingSession:
    agent_type = 'test-crash'

    def __init__(self, runtime, stack):
        pass

    def run(self, context):
        context.execute('tool_call', {'kind': 'tool_call', 'tool': 'honua_query_features',
                                      'arguments': {'serviceId': 'p10-shelters', 'layerId': 0, 'where': CORRECT_WHERE}})
        raise RuntimeError('adapter crashed')


class LostResponseSession(McpSpeakingSession):
    agent_type = 'test-lost'

    def run(self, context):
        import os
        from agent_e2e_local_api import SessionResult
        from agent_e2e_mcp import Client, McpError
        endpoint = context.mcp_proxy()
        os.environ['TEST_PROXY_TOKEN'] = endpoint.token.removeprefix('Bearer ')
        client = Client(endpoint.url, credential_env='TEST_PROXY_TOKEN', credential_kind='bearer', timeout=60)
        client.initialize()
        with pytest.raises(McpError):
            client.call_tool('honua_query_features', {'serviceId': 'p10-shelters', 'layerId': 0, 'where': CORRECT_WHERE})
        return SessionResult(agent_type=self.agent_type, model_id='test-model', final_answer='Done: two shelters.')


@pytest.mark.parametrize('name,factory', [('test-crash', CrashingSession), ('test-lost', LostResponseSession)])
def test_rereview4_session_crash_or_lost_response_is_a_recorded_failure(candidate, tmp_path, monkeypatch, name, factory):
    import agent_e2e_local_mcp_proxy
    forward = agent_e2e_local_mcp_proxy.RecordingMcpProxy.forward

    def lossy(self, body, headers, *, method='POST'):
        if body and b'"tools/call"' in body:
            raise OSError('connection reset after send')
        return forward(self, body, headers, method=method)
    monkeypatch.setattr(agent_e2e_local_mcp_proxy.RecordingMcpProxy, 'forward', lossy)
    runner.SESSIONS[name] = factory
    try:
        runtime = FakeRuntime(candidate.base_url, FakeModel([]), tmp_path)
        runtime.agent = name
        receipt = runner.execute_case(case_id='P10:A:local-docker', scenario=FakeP10(), variant='A', case=case('A'),
                                      candidate_digest=DIGEST, runtime=runtime)
    finally:
        del runner.SESSIONS[name]
    assert receipt['status'] == 'fail' and receipt['failure']['category'] == 'post-action-undetermined', receipt
    actions = receipt['sessions'][0]['actions']
    assert len(actions) == 1 and Path(json.loads(json.dumps(actions))[0].get('digest', '')).name
    assert not receipt['infrastructure']
    if name == 'test-lost':
        assert actions[0]['status'] == 'unknown'


def test_rereview5_session_registration_works_through_the_script_entrypoint(tmp_path):
    import os
    import subprocess
    import sys
    (tmp_path / 'fake_sessions_entry.py').write_text(
        'import agent_e2e_local_runner as runner\n'
        'class S:\n    agent_type = "fake-cli"\n    def __init__(self, runtime, stack): pass\n'
        'runner.register_session("fake-cli", S)\n')
    env = {**os.environ, 'PYTHONPATH': str(tmp_path), 'HONUA_CORPUS_SESSION_MODULES': 'fake_sessions_entry'}
    script = str(ROOT / 'tools/agent_e2e_local_runner.py')
    listed = subprocess.run([sys.executable, script, 'agents'], capture_output=True, text=True, env=env, timeout=120)
    assert listed.returncode == 0 and 'fake-cli' in json.loads(listed.stdout), listed.stderr
    out = tmp_path / 'receipt.json'
    subprocess.run([sys.executable, script, 'case', '--scenario', 'P10', '--variant', 'A', '--model', 'm',
                    '--agent', 'fake-cli', '--image-ref', 'x', '--output', str(out)], env=env, timeout=120)
    reason = json.loads(out.read_text())['infrastructure'][0]['reason']
    assert 'unknown agent session' not in reason and 'image reference' in reason


# ---- Astra third review 5722096789 --------------------------------------------------------------

class OtherResourceP10(FakeP10):
    """Reads an untouched look-alike resource for the 'after' snapshot (or for both)."""

    def __init__(self, both=False):
        self.both = both

    def seed(self, stack, variant):
        from agent_e2e_p10_observer import source_path
        if not self.both:
            return super().seed(stack, variant)
        recorder = stack.extra['recorder']
        recorder.json('publish-shelters-submit', 'POST', '/api/v1/operations/service.publish/submit',
                      {'serviceName': 'p10-shelters'})
        recorder.json('p10-source-before', 'GET', source_path({'service': 'p10-lookalike', 'layerId': 0}))
        return Seeded({'variant': variant, 'service': 'p10-shelters', 'layerId': 0})

    def observe(self, stack, seeded, variant, run=None):
        from agent_e2e_p10_observer import source_path
        recorder = stack.extra['recorder']
        recorder.json('p10-source-after', 'GET', source_path({'service': 'p10-lookalike', 'layerId': 0}))
        query = self.selected_query(recorder.actions(), seeded)
        if query is not None:
            recorder.replay('p10-replay-query', query)
        return [Observation('filter.exact', None, {'projection': 'extract/v1'}),
                Observation('source-content.unchanged', None, {'projection': 'extract/v1'})]


@pytest.mark.parametrize('both', [False, True])
def test_third_review1_preservation_reads_must_be_the_protected_resource(candidate, tmp_path, both):
    mutate = json.dumps({'kind': 'tool_call', 'tool': 'honua_update_features',
                         'arguments': {'serviceId': 'p10-shelters'}})
    receipt, _, _ = run_case(candidate, tmp_path, [tool(CORRECT_WHERE), mutate, FINAL], scenario=OtherResourceP10(both))
    preservation = receipt['assertions'][1]
    assert preservation['status'] == 'fail' and receipt['status'] == 'fail', preservation
    assert ('different requests' if not both else 'does not address') in preservation['reason']


def test_third_review2_completeness_pointer_must_exist_and_post_scopes_are_identity(candidate, tmp_path):
    import os
    from agent_e2e_local_projections import resolve_roles
    from agent_e2e_local_recorder import EvidenceVault
    os.environ['HONUA_ADMIN_PASSWORD'] = ADMIN
    recorder = ControllerRecorder(base_url=candidate.base_url, workdir=tmp_path, candidate_digest=DIGEST,
                                  vault=EvidenceVault('c'), attempt_id='c',
                                  principals={'controller': ('HONUA_ADMIN_PASSWORD', 'admin')})
    recorder.json('maps-before', 'POST', '/api/v1/admin/maps/search', {'scope': 'mine'})
    recorder.set_phase('observe')
    recorder.json('maps-after', 'POST', '/api/v1/admin/maps/search', {'scope': 'all'})
    recorder.json('maps-after-mine', 'POST', '/api/v1/admin/maps/search', {'scope': 'mine'})
    assert recorder.records['maps-before'].request_arguments_raw == b'{"scope":"mine"}'
    scoped = RoleBinding('created:map', 'maps-after', items='/items', id='/id', owner='/owner',
                         before='maps-before')
    with pytest.raises(ValueError, match='different request scope'):  # body-defined scopes differ
        resolve_roles([scoped], dict(recorder.records), agent_principal='key-2')
    wrong_cursor = RoleBinding('created:map', 'maps-after-mine', items='/items', id='/id', owner='/owner',
                               before='maps-before', next_page='/nextCursor')
    with pytest.raises(ValueError, match='not the binding'):  # a binding cannot choose its own completeness proof
        resolve_roles([wrong_cursor], dict(recorder.records), agent_principal='key-2')


def test_third_review3_truncated_proxy_response_is_an_unknown_outcome(candidate, tmp_path, monkeypatch):
    import agent_e2e_local_mcp_proxy
    forward = agent_e2e_local_mcp_proxy.RecordingMcpProxy.forward

    def truncated(self, body, headers, *, method='POST'):
        status, raw, ctype, session = forward(self, body, headers, method=method)
        if body and b'"tools/call"' in body:
            return status, raw[: len(raw) // 2], ctype, session
        return status, raw, ctype, session
    monkeypatch.setattr(agent_e2e_local_mcp_proxy.RecordingMcpProxy, 'forward', truncated)
    runner.SESSIONS['test-lost'] = LostResponseSession
    try:
        runtime = FakeRuntime(candidate.base_url, FakeModel([]), tmp_path)
        runtime.agent = 'test-lost'
        receipt = runner.execute_case(case_id='P10:A:local-docker', scenario=FakeP10(), variant='A', case=case('A'),
                                      candidate_digest=DIGEST, runtime=runtime)
    finally:
        del runner.SESSIONS['test-lost']
    assert receipt['status'] == 'fail' and receipt['failure']['category'] == 'post-action-undetermined'
    action = receipt['sessions'][0]['actions'][0]
    assert action['status'] == 'unknown' and action['resultRawSha256'].startswith('sha256:')


# ---- core2: runner capabilities for the remaining scenarios ------------------------------------

def _restart_fixture(monkeypatch, restart_changes_start=True):
    import subprocess
    started = {'t': '2026-10-31T07:00:00Z'}

    def fake_run(argv, **kwargs):
        out = ''
        if argv[:2] == ['docker', 'ps']:
            out = 'cid-1\n'
        elif argv[:2] == ['docker', 'inspect']:
            out = f'cid-1 {started["t"]} running\n'
        return subprocess.CompletedProcess(argv, 0, out, '')
    monkeypatch.setattr(subprocess, 'run', fake_run)

    def compose_restart(self, stack):
        if restart_changes_start:
            started['t'] = '2026-10-31T07:05:00Z'
        return 0
    monkeypatch.setattr(FakeRuntime, 'compose_restart', compose_restart)


@pytest.mark.parametrize('restarted', [True, False])
def test_core2_restart_yields_an_oracle_lifecycle_only_when_observed(candidate, tmp_path, monkeypatch, restarted):
    _restart_fixture(monkeypatch, restarted)
    receipt, _, _ = run_hook_case(candidate, tmp_path, [FINAL, tool(CORRECT_WHERE), FINAL], HookP10(sessions=2, restart=True))
    lifecycle = receipt['custody']['lifecycle:between-1']
    expected = {'resourceId': 'server', 'terminalState': 'ready', 'requiredStates': ['running', 'restarted']}
    if restarted:
        assert lifecycle == {'resourceId': 'server', 'states': ['running', 'restarted', 'running', 'ready']}
        runner.oracle.verify('restart.observed', lifecycle, expected)
    else:  # same start time: no restart was observed, so the lifecycle cannot satisfy the oracle
        assert 'restarted' not in lifecycle['states']
        with pytest.raises(ValueError):
            runner.oracle.verify('restart.observed', lifecycle, expected)
    spec = {'custody': 'lifecycle:between-1'}
    obs = Observation('restart.observed', None, {'projection': 'extract/v1'})
    frozen = {'restart.observed': {'spec': spec, 'reads': {}}}
    result = runner.score_assertions(['restart.observed'], [obs], {'restart.observed': expected}, records={},
                                     custody=receipt['custody'], frozen=frozen)[0]
    assert result['status'] == ('pass' if restarted else 'fail')


def _sealed_action_results(tmp_path, rows):
    from agent_e2e_local_recorder import EvidenceVault, action_bytes
    vault = EvidenceVault('c')
    sealed = []
    for number, (tool_name, status, reply) in enumerate(rows, start=1):
        action = {'kind': 'tool_call', 'tool': tool_name, 'arguments': {}}
        path, response = tmp_path / f'a{number}.json', tmp_path / f'a{number}.response'
        path.write_bytes(action_bytes(action))
        response.write_bytes(json.dumps({'jsonrpc': '2.0', 'id': '1', 'result': reply}).encode())
        sealed.append({'session': 1, 'sequence': number, 'digest': 'sha256:' + hashlib.sha256(action_bytes(action)).hexdigest(),
                       'logPath': str(path), 'status': status, 'resultRawPath': str(response), 'resultHttpStatus': 200,
                       'resultRawSha256': 'sha256:' + hashlib.sha256(response.read_bytes()).hexdigest()})
    vault.append_actions(1, sealed)
    return vault


def test_core2_created_role_by_agent_authorship_and_complete_page(tmp_path):
    from agent_e2e_local_projections import resolve_roles
    from agent_e2e_local_recorder import Record

    def listing(step, phase, items, path='/api/v1/admin/maps?limit=100'):
        raw = json.dumps({'items': items}).encode()
        return Record(step, phase, 'http', 'controller', None, 'GET', path, 200, raw, 'sha256:' + hashlib.sha256(raw).hexdigest())
    vault = _sealed_action_results(tmp_path, [('honua_create_map', 'pass', {'structuredContent': {'mapId': 'map-2'}}),
                                               ('honua_create_map', 'fail', {'structuredContent': {'mapId': 'map-3'}})])
    results = vault.action_results()
    records = {'before': listing('before', 'seed', [{'id': 'map-1'}]),
               'after': listing('after', 'observe', [{'id': 'map-1'}, {'id': 'map-2'}, {'id': 'map-3'}, {'id': 'map-4'}])}
    created_by = {'tool': 'honua_create_map', 'pointer': '/result/structuredContent/mapId'}
    binding = RoleBinding('created:map', 'after', items='/items', id='/id', before='before', created_by=created_by)
    # map-3 came from a failed call and map-4 from nobody: only the agent's successful creation binds.
    assert resolve_roles([binding], records, agent_principal='agent', action_results=results) == {'created:map': 'map-2'}
    with pytest.raises(ValueError, match='matched 0'):
        resolve_roles([binding], {**records, 'before': listing('before', 'seed', [{'id': 'map-1'}, {'id': 'map-2'}])},
                      agent_principal='agent', action_results=results)
    # Completeness comes only from the reviewed contract plus the absence of contradicting evidence.
    def pair(after_body_extra=None, path='/api/v1/admin/maps?limit=100'):
        out = {'before': listing('before', 'seed', [{'id': 'map-1'}], path=path),
               'after': listing('after', 'observe', [{'id': 'map-1'}, {'id': 'map-2'}], path=path)}
        if after_body_extra:
            raw = json.dumps({'items': [{'id': 'map-1'}, {'id': 'map-2'}], **after_body_extra}).encode()
            out['after'] = Record('after', 'observe', 'http', 'controller', None, 'GET', path, 200, raw,
                                  'sha256:' + hashlib.sha256(raw).hexdigest())
        return out
    for extra, message in (({'next': 'p2'}, 'further page'), ({'hasMore': True}, 'truncation'),
                           ({'totalCount': 9}, 'count contradicts')):
        with pytest.raises(ValueError, match=message):
            resolve_roles([binding], pair(extra), agent_principal='agent', action_results=results)
    with pytest.raises(ValueError, match='start at the beginning'):
        resolve_roles([binding], pair(path='/api/v1/admin/maps?limit=100&offset=50'), agent_principal='agent',
                      action_results=results)
    with pytest.raises(ValueError, match='no reviewed enumeration contract'):
        resolve_roles([binding], pair(path='/api/v1/admin/unreviewed'), agent_principal='agent', action_results=results)
    connections = pair(path='/api/v1/admin/connections')
    assert resolve_roles([binding], connections, agent_principal='agent', action_results=results) == {'created:map': 'map-2'}
    with pytest.raises(ValueError, match='authorship'):
        resolve_roles([RoleBinding('created:map', 'after', items='/items', id='/id', before='before', owner='/o',
                                   created_by=created_by)],
                      records, agent_principal='agent', action_results=results)


def test_core2_session_roles_bind_exact_session_expectations(candidate, tmp_path):
    receipt, model, _ = run_hook_case(candidate, tmp_path, [FINAL, tool(CORRECT_WHERE), FINAL], HookP10(sessions=2))
    roles = receipt['roles']
    first, second = receipt['sessions'][0], receipt['sessions'][1]
    assert roles['session:previous'] == first['sessionId'] and roles['session:current'] == second['sessionId']
    assert roles['session:handoff-sha256'] == first['finalAnswerSha256']
    assert all(m.startswith('S2-') for m in roles['session:visible-message-ids'])
    from agent_e2e_local_projections import substitute_roles
    expected = substitute_roles({'previousSessionId': {'$role': 'session:previous'},
                                 'currentSessionId': {'$role': 'session:current'},
                                 'visibleConversationMessageIds': {'$role': 'session:visible-message-ids'}}, roles)
    observed = {'previousSessionId': first['sessionId'], 'currentSessionId': second['sessionId'],
                'visibleConversationMessageIds': roles['session:visible-message-ids']}
    runner.oracle.verify('session.fresh', observed, expected)
    with pytest.raises(ValueError):  # the same session twice is not a fresh session
        runner.oracle.verify('session.fresh', {**observed, 'previousSessionId': second['sessionId']},
                             {**expected, 'previousSessionId': second['sessionId']})
    assert runner.session_roles(['s-1'], [{'messageIds': ['S1-M1']}]) == {
        'session:current': 's-1', 'session:visible-message-ids': ['S1-M1']}


def test_core2_response_lost_after_commit_forwards_then_answers_503(candidate, tmp_path):
    candidate.extra_tools = True
    plan = {'kind': 'response_lost_after_commit', 'id': 'map-lost', 'after': None, 'match': {'tool': 'honua_create_map'},
            'commit': {'pointer': '/result/structuredContent/mapId'}}
    create = json.dumps({'kind': 'tool_call', 'tool': 'honua_create_map', 'arguments': {'title': 'Shelters'}})
    receipt, model, _ = run_hook_case(candidate, tmp_path, [create, tool(CORRECT_WHERE), FINAL], HookP10(fault=plan))
    event = receipt['faultEvents'][0]
    action = receipt['sessions'][0]['actions'][0]
    assert candidate.maps == [{'id': 'map-1', 'title': 'Shelters'}]           # the request committed
    assert '503' in model.sent[1][-1]['content'] and 'map-1' not in model.sent[1][-1]['content']  # reply lost
    assert event['forwarded'] and event['upstreamOutcome'] == 'ok' and event['upstreamStatus'] == 200
    assert event['httpStatus'] == 503 and event['actionDigest'] == action['digest']
    assert action['status'] == 'fail' and event['upstreamResponseSha256'] == action['resultRawSha256']
    for bad in ({'kind': 'response_lost_after_commit', 'after': None, 'match': None},
                {'kind': 'response_lost_after_commit', 'after': None, 'match': {'tool': 'x'}},
                {'kind': 'http_503_once', 'after': None, 'match': None}):
        with pytest.raises(ValueError):
            runner.validate_fault_plan(bad)


def test_core2_between_baselines_and_session_bound_replays(tmp_path):
    import dataclasses
    from agent_e2e_local_projections import Context, evaluate
    saved = {'data': {'title': 'Map', 'version': 'v2'}}
    records = {'b': _record('b', phase='between', body=saved, path='/maps/map-1'),
               'a': _record('a', phase='observe', body=saved, path='/maps/map-1')}
    spec = {'protectedContent': [{'label': 'map', 'before': 'b', 'after': 'a', 'resource': ['created:map'],
                                  'beforePhase': 'between', 'pointer': '/data'}]}
    value = evaluate(spec, Context(records, {'created:map': 'map-1'}))
    assert value['before'] == value['after']
    with pytest.raises(ValueError, match='wrong phase'):
        evaluate({'protectedContent': [{**spec['protectedContent'][0], 'beforePhase': 'seed'}]},
                 Context(records, {'created:map': 'map-1'}))
    logged = {'kind': 'tool_call', 'tool': 'honua_get_map', 'arguments': {'mapId': 'map-1'}, '_session': 1}
    replay = dataclasses.replace(_record('r', kind='mcp', path='/mcp#tools/call',
                                         arguments={'tool': 'honua_get_map', 'arguments': {'mapId': 'map-1'}}),
                                 method='POST', replay_of='sha256:x')
    rule = {'kind': 'mcp', 'method': 'POST', 'path': '/mcp#tools/call', 'principal': 'controller', 'phase': 'observe',
            'replayOfModelAction': True, 'session': 2}
    with pytest.raises(ValueError, match='session 2'):
        runner.check_reads('r', replay, rule, {'sha256:x': logged})
    runner.check_reads('r', replay, rule, {'sha256:x': {**logged, '_session': 2}})


def test_core2_import_projections_and_inventory_preservation(tmp_path):
    from agent_e2e_local_projections import Context, evaluate
    polygon = {'geometryType': 'esriGeometryPolygon', 'exceededTransferLimit': False, 'spatialReference': {'wkid': 3857},
               'objectIdFieldName': 'objectid', 'fields': [{'name': 'objectid'}, {'name': 'source_id'}, {'name': 'zone'}],
               'features': [{'attributes': {'objectid': 1, 'source_id': 'f1', 'zone': 'AE'},
                             'geometry': {'rings': [[[0, 0], [1, 0], [1, 1], [0, 0]]]}}]}
    features = evaluate({'featureServerFeatures': 'q'}, Context({'q': _record('q', body=polygon)}))
    assert features == {'srid': 3857, 'features': [{'type': 'Feature', 'id': 'f1', 'properties': {'source_id': 'f1', 'zone': 'AE'},
                                                    'geometry': {'type': 'Polygon', 'coordinates': [[[0, 0], [1, 0], [1, 1], [0, 0]]]}}]}
    stringly = {**polygon, 'fields': [{'name': 'objectid'}, {'name': 'properties'}],
                'features': [{'attributes': {'objectid': 1, 'properties': '{"source_id":"f1"}'}, 'geometry': {'rings': []}}]}
    with pytest.raises(ValueError, match='field inventory'):   # honua-server #6815 shape is not typed fields
        evaluate({'featureServerFeatures': 'q'}, Context({'q': _record('q', body=stringly)}))
    raw = (ROOT / 'certification/agent-e2e/corpus/town-A/invalid-rows.csv').read_bytes()
    report = _record('report', kind='mcp', body={'jsonrpc': '2.0', 'id': '1',
                     'result': {'structuredContent': {'errors': [{'row': 2}, {'row': 3}]}}})
    spec = {'customerFileColumn': 'town-A/invalid-rows.csv', 'column': 'source_id', 'firstDataRow': 2,
            'rows': {'map': {'body': 'report', 'pointer': '/result/structuredContent/errors'}, 'to': {'item': '/row'}}}
    ctx = Context({'report': report}, customer_files={'town-A/invalid-rows.csv': raw})
    assert evaluate(spec, ctx) == ['bad1', 'bad2']
    with pytest.raises(ValueError, match='not a frozen attachment'):
        evaluate(spec, Context({'report': report}))
    out_of_range = _record('report', kind='mcp', body={'jsonrpc': '2.0', 'id': '1',
                           'result': {'structuredContent': {'errors': [{'row': 99}]}}})
    with pytest.raises(ValueError, match='not in the file'):
        evaluate(spec, Context({'report': out_of_range}, customer_files={'town-A/invalid-rows.csv': raw}))
    # Inventory preservation needs the identical request with an exact frozen path.
    inventory = {'b': _record('b', phase='seed', body={'items': [1]}, path='/api/v1/admin/maps?limit=100'),
                 'a': _record('a', phase='observe', body={'items': [1]}, path='/api/v1/admin/maps?limit=100')}
    pspec = {'protectedContent': [{'label': 'maps', 'before': 'b', 'after': 'a', 'resource': 'inventory', 'pointer': '',
                                   'items': '/items'}]}
    expected = {'shared.preserved': {'profile': 'honua.protected-content/v1', 'resources': [{'resourceId': 'maps', 'content': {'items': [1]}}]}}
    rule = {'kind': 'http', 'method': 'GET', 'principal': 'controller', 'path': r'/api/v1/admin/maps\?limit=100'}
    obs = Observation('shared.preserved', None, {'projection': 'extract/v1'})
    loose = {'shared.preserved': {'spec': pspec, 'reads': {'b': {**rule, 'phase': 'seed'}, 'a': {**rule, 'phase': 'observe'}}}}
    result = runner.score_assertions(['shared.preserved'], [obs], expected, records=inventory, frozen=loose)[0]
    assert result['status'] == 'fail' and 'exact frozen path' in result['reason']
    exact = {'shared.preserved': {'spec': pspec, 'reads': {k: {**v, 'exactPath': '/api/v1/admin/maps?limit=100'}
                                                             for k, v in loose['shared.preserved']['reads'].items()}}}
    assert runner.score_assertions(['shared.preserved'], [obs], expected, records=inventory, frozen=exact)[0]['status'] == 'pass'


def test_core2_canaries_and_model_visible_scans(candidate, tmp_path):
    runtime = FakeRuntime(candidate.base_url, FakeModel([]), tmp_path)
    stack = runtime.setup('P10:A:local-docker', DIGEST)
    value = stack.extra['registerCanary']('datasource-password')
    with pytest.raises(ValueError):
        stack.extra['registerCanary']('datasource-password')
    from agent_e2e_local_projections import Context, evaluate
    leaked = _record('cfg', body={'connection': 'password=' + value})
    clean = _record('cfg', body={'connection': 'password=[set]'})
    canaries = runtime._canaries[stack.compose_project]
    assert evaluate({'matchedCanaries': ['cfg']}, Context({'cfg': leaked}, canaries=canaries)) == ['datasource-password']
    assert evaluate({'matchedCanaries': ['cfg']}, Context({'cfg': clean}, canaries=canaries)) == []
    assert evaluate({'matchedCanaries': ['cfg', '$model-visible']},
                    Context({'cfg': clean}, canaries=canaries, model_visible=(value.encode(),))) == ['datasource-password']
    with pytest.raises(ValueError, match='no model-visible'):
        evaluate({'leakedCredentials': ['$model-visible']}, Context({}, credentials=('x' * 10,)))
    stack.extra['recorder'].set_phase('session')
    with pytest.raises(ValueError):
        stack.extra['registerCanary']('late')
def test_prereg_exclusions_match_the_preregistration():
    import re
    import agent_e2e_local_expectations as expectations
    assert expectations.PREREG_EXCLUSIONS == ('tenant.denied', 'P18', 'custody.worker-replaced@local-docker')
    assert '#1163' in expectations.PREREG_REFERENCE
    document = ROOT / expectations.PREREG_SOURCE
    if not document.is_file():
        pytest.skip('the preregistration lands with honua-io/honua-release#1163')
    text = document.read_text(encoding='utf-8')
    section = text.split('## Exclusions', 1)[1].split('\n## ', 1)[0]
    assert '`tenant.denied`' in section and 'P18' in section and '`custody.worker-replaced`' in section
    listed = re.search(r'"exclusions":\[([^\]]*)\]', text)
    assert listed and [x.strip().strip('"') for x in listed.group(1).split(',')] == list(expectations.PREREG_EXCLUSIONS)


def test_request_arguments_accessor_is_mcp_only():
    import json as _json
    from agent_e2e_local_recorder import Record
    body = _json.dumps({'scope': 'mine'}).encode()
    http = Record('h', 'observe', 'http', 'controller', None, 'POST', '/x', 200, b'{}',
                  'sha256:' + hashlib.sha256(b'{}').hexdigest(), None, body)
    mcp = Record('m', 'observe', 'mcp', 'controller', None, 'POST', '/mcp#tools/call', 200, b'{}',
                 'sha256:' + hashlib.sha256(b'{}').hexdigest(), None, _json.dumps({'tool': 't', 'arguments': {}}).encode())
    assert http.request_arguments is None and http.request_arguments_raw == body
    assert mcp.request_arguments == {'tool': 't', 'arguments': {}}


# ---- Astra review 5722460139 of #1171 -----------------------------------------------------------

def test_core2r1_custody_facts_come_only_from_the_runtime_vault(candidate, tmp_path, monkeypatch):
    _restart_fixture(monkeypatch, restart_changes_start=False)   # no restart is observed
    runtime = FakeRuntime(candidate.base_url, FakeModel([]), tmp_path)
    stack = runtime.setup('P10:A:local-docker', DIGEST)
    runtime.restart_server(stack, 'probe')
    # A scenario rewrites the visible custody dict to claim a restart.
    stack.extra['custody']['lifecycle:probe'] = {'resourceId': 'server', 'states': ['running', 'restarted', 'running', 'ready']}
    stack.extra['custody']['lifecycle:invented'] = {'resourceId': 'server', 'states': ['running', 'restarted', 'ready']}
    authoritative = runtime.custody(stack)
    assert 'restarted' not in authoritative['lifecycle:probe']['states'] and 'lifecycle:invented' not in authoritative
    expected = {'restart.observed': {'resourceId': 'server', 'terminalState': 'ready', 'requiredStates': ['restarted']}}
    obs = Observation('restart.observed', None, {'projection': 'extract/v1'})
    for name in ('lifecycle:probe', 'lifecycle:invented'):
        frozen = {'restart.observed': {'spec': {'custody': name}, 'reads': {}}}
        assert runner.score_assertions(['restart.observed'], [obs], expected, records={}, custody=authoritative,
                                       frozen=frozen)[0]['status'] == 'fail'
    vault = runtime.vault(stack)
    raw, steps, digest = vault._custody['lifecycle:probe']
    vault._custody['lifecycle:probe'] = (raw.replace(b'"running"', b'"restarted"', 1), steps, digest)
    with pytest.raises(ValueError, match='custody fact altered'):
        runtime.custody(stack)
    with pytest.raises(ValueError, match='unrecorded evidence'):
        vault.attest('x', {}, ['never-recorded'])


def test_core2r2_records_bind_the_action_occurrence_not_just_its_digest(tmp_path, candidate):
    import os
    from agent_e2e_local_recorder import EvidenceVault, action_bytes
    os.environ['HONUA_ADMIN_PASSWORD'] = ADMIN
    vault = EvidenceVault('c')
    recorder = ControllerRecorder(base_url=candidate.base_url, workdir=tmp_path, candidate_digest=DIGEST, vault=vault,
                                  attempt_id='c', principals={'controller': ('HONUA_ADMIN_PASSWORD', 'admin')})
    action = {'kind': 'tool_call', 'tool': 'honua_query_features',
              'arguments': {'serviceId': 'p10-shelters', 'layerId': 0, 'where': CORRECT_WHERE}}
    for session in (1, 2):   # the identical request in both sessions
        path, response = tmp_path / f's{session}.json', tmp_path / f's{session}.response'
        path.write_bytes(action_bytes(action))
        response.write_bytes(json.dumps({'jsonrpc': '2.0', 'id': '1', 'result': {'session': session}}).encode())
        vault.append_actions(session, [{'session': session, 'sequence': 1, 'logPath': str(path), 'status': 'pass',
                                        'digest': 'sha256:' + hashlib.sha256(action_bytes(action)).hexdigest(),
                                        'resultRawPath': str(response), 'resultHttpStatus': 200,
                                        'resultRawSha256': 'sha256:' + hashlib.sha256(response.read_bytes()).hexdigest()}])
    recorder.set_phase('observe')
    first = [a for a in recorder.actions() if a.session == 1][0]
    record = recorder.action_result('captured-session-1', first)
    assert record.replay_of.startswith('occ:1:1:')
    model_actions = vault.model_actions()
    assert len(model_actions) == 2   # two occurrences of the same bytes stay distinct
    rule = {'kind': 'mcp', 'method': 'POST', 'path': '/mcp#tools/call', 'principal': 'agent', 'phase': 'observe',
            'origin': 'session-result', 'replayOfModelAction': True}
    runner.check_reads('captured-session-1', record, {**rule, 'session': 1}, model_actions)
    with pytest.raises(ValueError, match='session 2'):
        runner.check_reads('captured-session-1', record, {**rule, 'session': 2}, model_actions)
    assert record.index()['afterSession'] == 0 and 'afterSession' in record.metadata('c')


def test_core2r3_partial_inventories_fail_preservation(tmp_path):
    from agent_e2e_local_projections import Context, evaluate
    path = '/api/v1/admin/maps?limit=100'
    spec = {'protectedContent': [{'label': 'maps', 'before': 'b', 'after': 'a', 'resource': 'inventory',
                                  'pointer': '', 'items': '/items'}]}
    for body, message in (({'items': [1], 'next': 'p2'}, 'further page'), ({'items': [1], 'total': 5}, 'count contradicts')):
        records = {'b': _record('b', phase='seed', body=body, path=path), 'a': _record('a', phase='observe', body=body, path=path)}
        with pytest.raises(ValueError, match=message):
            evaluate(spec, Context(records))
    unreviewed = {'b': _record('b', phase='seed', body={'items': [1]}, path='/other'),
                  'a': _record('a', phase='observe', body={'items': [1]}, path='/other')}
    with pytest.raises(ValueError, match='no reviewed enumeration contract'):
        evaluate(spec, Context(unreviewed))
    with pytest.raises(ValueError, match='items'):
        evaluate({'protectedContent': [{**spec['protectedContent'][0], 'items': None}]},
                 Context({'b': _record('b', phase='seed', body={'items': [1]}, path=path),
                          'a': _record('a', phase='observe', body={'items': [1]}, path=path)}))


@pytest.mark.parametrize('first', ['bad', 'pending'])
def test_core2r4_lost_response_fault_fires_only_on_a_confirmed_commit(candidate, tmp_path, first):
    candidate.extra_tools = True
    plan = {'kind': 'response_lost_after_commit', 'id': 'map-lost', 'after': None, 'match': {'tool': 'honua_create_map'},
            'commit': {'pointer': '/result/structuredContent/mapId'}}
    rejected = json.dumps({'kind': 'tool_call', 'tool': 'honua_create_map',
                           'arguments': {'title': 'bad'} if first == 'bad' else {'pending': True}})
    good = json.dumps({'kind': 'tool_call', 'tool': 'honua_create_map', 'arguments': {'title': 'Shelters'}})
    receipt, model, _ = run_hook_case(candidate, tmp_path, [rejected, good, tool(CORRECT_WHERE), FINAL], HookP10(fault=plan))
    # The rejected/noncommitted reply reached the model unchanged; the fault stayed armed for the real commit.
    assert '503' not in model.sent[1][-1]['content']
    events = receipt['faultEvents']
    assert len(events) == 1 and events[0]['actionDigest'] == receipt['sessions'][0]['actions'][1]['digest']
    assert events[0]['committedResourceId'] == 'map-1' and events[0]['upstreamStatus'] == 200
    assert receipt['faultSkipped'][0]['actionDigest'] == receipt['sessions'][0]['actions'][0]['digest']


def test_core2r4_lost_response_needs_commit_on_the_proxy_path_too():
    from agent_e2e_local_faults import FaultInjector
    plan = {'kind': 'response_lost_after_commit', 'after': None, 'match': {'tool': 'honua_create_map'},
            'commit': {'pointer': '/result/structuredContent/mapId'}}
    action = {'kind': 'tool_call', 'tool': 'honua_create_map', 'arguments': {}}

    def reply(result=None, error=None):
        body = {'jsonrpc': '2.0', 'id': '1'}
        body.update({'error': error} if error else {'result': result})
        return json.dumps(body).encode()
    injector = FaultInjector(plan)
    for status, raw in ((500, reply({'structuredContent': {'mapId': 'm'}})),
                        (200, reply(error={'code': -32602, 'message': 'invalid'})),
                        (200, reply({'isError': True, 'structuredContent': {'mapId': 'm'}})),
                        (200, reply({'structuredContent': {'status': 'Accepted'}}))):
        execution = {'status': 'ok' if status == 200 else 'fail'}
        assert injector.lose('tool_call', action, execution, status, raw) is None
    assert len(injector.skipped) == 4 and not injector.events
    event = injector.lose('tool_call', action, {'status': 'ok'}, 201, reply({'structuredContent': {'mapId': 'm'}}))
    assert event['committedResourceId'] == 'm' and event['upstreamStatus'] == 201


# ---- core3: services-stage capabilities ---------------------------------------------------------

def test_core3_controller_sql_reads_are_recorded_select_only(candidate, tmp_path):
    import os
    from agent_e2e_local_recorder import EvidenceVault
    os.environ['HONUA_ADMIN_PASSWORD'] = ADMIN
    seen = []

    def executor(query):
        seen.append(query)
        return 200, json.dumps([{'source_id': 's1', 'capacity': 80}]).encode()
    vault = EvidenceVault('c')
    recorder = ControllerRecorder(base_url=candidate.base_url, workdir=tmp_path, candidate_digest=DIGEST, vault=vault,
                                  attempt_id='c', principals={'controller': ('HONUA_ADMIN_PASSWORD', 'admin')},
                                  sql_executor=executor)
    recorder.set_phase('observe')
    rows = recorder.sql('imported-rows', 'SELECT source_id, capacity FROM honua_data.p05_points ORDER BY source_id;')
    assert rows == [{'source_id': 's1', 'capacity': 80}]
    record = vault.records()['imported-rows']
    assert (record.kind, record.method, record.principal) == ('sql', 'SELECT', 'controller')
    assert record.path == 'SELECT source_id, capacity FROM honua_data.p05_points ORDER BY source_id'
    for bad in ('DELETE FROM t', 'SELECT 1; DROP TABLE t', 'UPDATE t SET a = 1'):
        with pytest.raises(ValueError, match='one SELECT'):
            recorder.sql('bad-' + str(len(seen)), bad)
    rule = {'kind': 'sql', 'method': 'SELECT', 'principal': 'controller', 'phase': 'observe',
            'path': r'SELECT source_id, capacity FROM honua_data\.p05_points ORDER BY source_id'}
    runner.check_reads('imported-rows', record, rule, {})
    with pytest.raises(ValueError, match='path'):
        runner.check_reads('imported-rows', record, {**rule, 'path': r'SELECT \* FROM other'}, {})


def test_core3_sql_read_runs_read_only_in_the_case_db_container(monkeypatch, tmp_path):
    import subprocess
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs.get('input')))
        if argv[:2] == ['docker', 'ps']:
            return subprocess.CompletedProcess(argv, 0, 'db-1\n', '')
        return subprocess.CompletedProcess(argv, 0, b'[{"a":1}]\n', b'')
    monkeypatch.setattr(subprocess, 'run', fake_run)
    stack = Stack('http://127.0.0.1:1', ADMIN, None, 'corpus-x', 'f', str(tmp_path), DIGEST, {})
    status, raw = runner.LocalRuntime(None, model='m', release_id='r').sql_read(stack, 'SELECT 1 AS a')
    assert status == 200 and raw == b'[{"a":1}]'
    exec_argv, script = calls[-1]
    assert exec_argv[:3] == ['docker', 'exec', '-i'] and 'PGOPTIONS=-c default_transaction_read_only=on' in exec_argv
    assert b'json_agg' in script and b'SELECT 1 AS a' in script


def test_core3_database_handoff_is_seeded_attested_attached_and_redacted(candidate, tmp_path, monkeypatch):
    import agent_e2e_local_expectations as expectations
    statements = []

    def fake_psql(self, stack, sql, *, read_only):
        statements.append((sql, read_only))
        if read_only:
            return 0, json.dumps([{'rolname': 'customer_town', 'rolcanlogin': True}]).encode()
        return 0, b''
    monkeypatch.setattr(FakeRuntime, 'psql', fake_psql)
    monkeypatch.setattr(FakeRuntime, '_db_container', lambda self, stack: 'db-1')
    original = expectations.load_document
    monkeypatch.setattr(expectations, 'load_document', lambda sid: (
        {**original('P10'), 'scenario': 'P03', 'customerHandoffs': {'A': ['database:town']}} if sid == 'P03' else original(sid)))

    class P03(HookP10):
        id = 'P03'

        def seed(self, stack, variant):
            seeded = super().seed(stack, variant)
            self.handoff_value = stack.extra['databaseHandoff']('town', 'honua_data')
            return seeded
    scenario = P03()
    receipt, model, runtime = run_hook_case(candidate, tmp_path, [tool(CORRECT_WHERE), FINAL], scenario)
    handoff = scenario.handoff_value
    assert handoff['username'] == 'customer_town' and len(handoff['password']) == 32
    create, read_only = statements[0]
    assert not read_only and 'GRANT SELECT ON ALL TABLES IN SCHEMA honua_data TO customer_town' in create
    attached = json.loads(model.sent[0][1]['content'])['customerAttachments']
    assert attached[0]['name'] == 'database-connection.json' and json.loads(attached[0]['content'])['password'] == handoff['password']
    assert receipt['custody']['databaseHandoff:town'] == {k: v for k, v in handoff.items() if k != 'password'}
    assert handoff['password'] not in json.dumps(receipt)
    assert receipt['customerFiles'][-1]['corpusPath'] == 'handoff:database:town'
    # A handoff the scenario did not seed through the runner is refused.
    monkeypatch.setattr(expectations, 'load_document', lambda sid: (
        {**original('P10'), 'scenario': 'P03', 'customerHandoffs': {'A': ['database:other']}} if sid == 'P03' else original(sid)))
    (tmp_path / 'z').mkdir()
    receipt, model, _ = run_hook_case(candidate, tmp_path / 'z', [tool(CORRECT_WHERE), FINAL], P03())
    assert receipt['status'] == 'infrastructure' and not model.sent
    stack = Stack('http://127.0.0.1:1', ADMIN, None, 'p', 'f', str(tmp_path), DIGEST, {'recorder': type('R', (), {'phase': 'observe'})()})
    with pytest.raises(ValueError, match='during seed'):
        runner.LocalRuntime(None, model='m', release_id='r').database_handoff(stack, 'late', 'honua_data')


def test_core3_attachment_snapshots_come_from_runner_digests(tmp_path):
    from agent_e2e_local_projections import Context, evaluate
    path = 'town-A/shelters.3857.geojson'
    custody = {'customerFiles:before': {path: 'sha256:aa'}, 'customerFiles:after': {path: 'sha256:aa'}}
    value = evaluate({'attachmentSnapshots': [path]}, Context({}, custody=custody))
    runner.oracle.verify('source-content.unchanged', value,
                         {'profile': 'honua.protected-content/v1', 'resources': [{'resourceId': path, 'content': {'sha256': 'sha256:aa'}}]})
    changed = {**custody, 'customerFiles:after': {path: 'sha256:bb'}}
    with pytest.raises(ValueError):
        runner.oracle.verify('source-content.unchanged', evaluate({'attachmentSnapshots': [path]}, Context({}, custody=changed)),
                             {'profile': 'honua.protected-content/v1', 'resources': [{'resourceId': path, 'content': {'sha256': 'sha256:aa'}}]})
    with pytest.raises(ValueError, match='not staged'):
        evaluate({'attachmentSnapshots': ['town-A/other.csv']}, Context({}, custody=custody))
    with pytest.raises(ValueError, match='not recorded by the runner'):
        evaluate({'attachmentSnapshots': [path]}, Context({}))


def test_core3_read_rules_select_the_source_by_role(tmp_path):
    import dataclasses
    replay = dataclasses.replace(_record('q', kind='mcp', path='/mcp#tools/call',
                                         arguments={'tool': 'honua_query_features', 'arguments': {'serviceId': 'svc-b', 'layerId': 0}}),
                                 method='POST', replay_of='occ:1:1:sha256:x')
    logged = {'occ:1:1:sha256:x': {'kind': 'tool_call', 'tool': 'honua_query_features',
                                   'arguments': {'serviceId': 'svc-b', 'layerId': 0}, '_session': 1}}
    rule = {'kind': 'mcp', 'method': 'POST', 'path': '/mcp#tools/call', 'principal': 'controller', 'phase': 'observe',
            'replayOfModelAction': True, 'addresses': ['seeded:service:roads']}
    runner.check_reads('q', replay, rule, logged, {'seeded:service:roads': 'svc-b'})
    with pytest.raises(ValueError, match='does not address'):  # a query of another same-tool service
        runner.check_reads('q', replay, rule, logged, {'seeded:service:roads': 'svc-a'})
    with pytest.raises(ValueError, match='does not address'):
        runner.check_reads('q', replay, rule, logged, {})
