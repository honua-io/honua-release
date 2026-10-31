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
        self.mcp_calls = []
        self.approvals = []
        self.lock = threading.Lock()

    def principal(self, headers):
        secret = headers.get('X-API-Key')
        if secret == ADMIN:
            return {'id': 'admin', 'revoked': False, 'permissions': ['admin:write']}
        row = self.keys.get(secret)
        return row if row and not row['revoked'] else None

    def feature_server(self):
        fields = [{'name': 'objectid', 'type': 'esriFieldTypeOID'}] + [
            {'name': name, 'type': 'esriFieldTypeString'} for name in ('capacity', 'is_open', 'name', 'source_id')]
        return {'geometryType': 'esriGeometryPoint', 'exceededTransferLimit': False,
                'spatialReference': {'wkid': 3857}, 'objectIdFieldName': 'objectid', 'fields': fields,
                'features': [{'attributes': {'objectid': i + 1, **f['properties']},
                              'geometry': {'x': f['geometry']['coordinates'][0], 'y': f['geometry']['coordinates'][1]}}
                             for i, f in enumerate(self.features)]}

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
            if name == 'honua_create_map':
                return {'content': [{'type': 'text', 'text': 'created'}], 'isError': False,
                        'structuredContent': {'mapId': 'map-1'}}
            if name == 'honua_ingest_dataset':
                rows = arguments.get('data', '').splitlines()[1:]
                bad = [{'row': i + 2, 'reason': 'missing coordinate'} for i, row in enumerate(rows) if ',,' in row]
                return {'content': [{'type': 'text', 'text': 'ingested'}], 'isError': False,
                        'structuredContent': {'rowCount': len(rows) - len(bad), 'errors': bad}}
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
                if path == '/api/v1/admin/version':
                    return self.reply(200, {'data': {'version': 'fake'}})
                if path.endswith('/effective-permissions'):
                    return self.reply(200, {'data': {'status': 'active'}})
                if '/FeatureServer/' in path and path.endswith('/query'):
                    return self.reply(200, candidate.feature_server())
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
    assert replay['principal'] == 'controller' and replay['replayOf'] == receipt['sessions'][0]['actions'][0]['digest']
    # The model acted with its own agent credential; the controller replay used the admin key.
    assert [c[0] for c in candidate.mcp_calls] == ['key-2', 'admin']
    # Custody: bootstrap minted the agent key, then was revoked; the probe is a recorded 401.
    assert receipt['custody']['agentMintedBy'] == 'bootstrap'
    assert receipt['custody']['postRevocationProbeStatus'] == 401
    assert receipt['custody']['bootstrapRevokedCredentialIds'] == ['key-1']
    # Logical roles were resolved from the seed-phase publish handle.
    assert receipt['roles'] == {'seeded:layer:shelters': '0', 'seeded:service:shelters': 'p10-shelters'}
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
        raw = json.dumps({'items': items}).encode()
        return Record(step, phase, 'http', 'controller', None, 'GET', '/maps', 200, raw,
                      'sha256:' + hashlib.sha256(raw).hexdigest())
    before = [{'id': 'm1', 'owner': 'other'}]
    records = {'before': record('before', 'seed', before),
               'after': record('after', 'observe', before + [{'id': 'm2', 'owner': 'agent'}, {'id': 'm3', 'owner': 'x'}])}
    binding = RoleBinding('created:map', 'after', items='/items', id='/id', owner='/owner', before='before',
                          next_page='/next')
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
    assert 'temperature' not in sent[0] and sent[1]['temperature'] == 0


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
    obs = Observation('tenant.denied', None, {'projection': 'extract/v1', 'spec': spec})
    assert runner.score_assertions(['tenant.denied'], [obs], {'tenant.denied': expected}, records=records,
                                   roles=roles)[0]['status'] == 'pass'
    # A probe cannot claim a resource its request did not address.
    swapped = {'object': {'requests': {'list': [row('own', 'seeded:map:ours'), row('own', 'seeded:map:theirs'),
                                                row('control', 'seeded:map:theirs')]}}}
    bad = Observation('tenant.denied', None, {'projection': 'extract/v1', 'spec': swapped})
    result = runner.score_assertions(['tenant.denied'], [bad], {'tenant.denied': expected}, records=records, roles=roles)
    assert result[0]['status'] == 'fail' and 'did not address' in result[0]['reason']
    # A denial that discloses the foreign id fails.
    records['foreign'] = _record('foreign', status=404, body={'id': 'map-2'}, path='/api/v1/maps/map-2',
                                 principal_id='key-a')
    assert runner.score_assertions(['tenant.denied'], [obs], {'tenant.denied': expected}, records=records,
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
    records = {'b': _record('b', phase='seed', body={'data': {'title': 'Map', 'updatedAt': 1}}),
               'a': _record('a', phase='observe', body={'data': {'title': 'Map', 'updatedAt': 2}})}
    spec = {'protectedContent': [{'label': 'map', 'before': 'b', 'after': 'a',
                                  'content': {'object': {'title': {'body': '$step', 'pointer': '/data/title'}}}}]}
    value = evaluate(spec, Context(records))
    assert value['before'] == value['after'] == [{'resourceId': 'map', 'content': {'title': 'Map'}}]
    swapped = {'protectedContent': [{'label': 'map', 'before': 'a', 'after': 'b', 'pointer': '/data'}]}
    with pytest.raises(ValueError, match='wrong phase'):
        evaluate(swapped, Context(records))


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
    result = runner.score_assertions(['custody.worker-replaced'], [obs], expected, records={}, custody=custody)[0]
    assert result['status'] == 'fail' and 'custody fact not recorded' in result['reason']


def test_finding3_created_roles_require_owner_and_post_seed_creation():
    from agent_e2e_local_projections import resolve_roles
    from agent_e2e_local_recorder import Record

    def record(step, phase, items, path='/maps'):
        raw = json.dumps({'items': items}).encode()
        return Record(step, phase, 'http', 'controller', None, 'GET', path, 200, raw,
                      'sha256:' + hashlib.sha256(raw).hexdigest())
    pre_seed_owned = {'id': 'm0', 'owner': 'agent'}
    foreign_new = {'id': 'm9', 'owner': 'someone-else'}
    records = {'before': record('before', 'seed', [pre_seed_owned]),
               'after': record('after', 'observe', [pre_seed_owned, foreign_new])}
    # Owner-only would have accepted the pre-seed resource; baseline-only the foreign one.
    for binding in (RoleBinding('created:map', 'after', items='/items', id='/id', owner='/owner'),
                    RoleBinding('created:map', 'after', items='/items', id='/id', before='before')):
        with pytest.raises(ValueError, match='both'):
            resolve_roles([binding], records, agent_principal='agent')
    both = RoleBinding('created:map', 'after', items='/items', id='/id', owner='/owner', before='before',
                       next_page='/next')
    with pytest.raises(ValueError, match='matched 0'):
        resolve_roles([both], records, agent_principal='agent')
    # The baseline must be the same enumeration, read before the session.
    records['other'] = record('other', 'seed', [], path='/other')
    with pytest.raises(ValueError, match='same enumeration'):
        resolve_roles([RoleBinding('created:map', 'after', items='/items', id='/id', owner='/owner', before='other',
                                   next_page='/next')],
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
    assert recorder.records['replay-member'].replay_of == digest
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


def test_customer_files_are_manifest_checked_and_shown_with_digests(candidate, tmp_path):
    files = ['town-A/invalid-rows.csv']
    receipt, model, _ = run_hook_case(candidate, tmp_path, [tool(CORRECT_WHERE), FINAL], HookP10(files=files))
    raw = (ROOT / 'certification/agent-e2e/corpus/town-A/invalid-rows.csv').read_bytes()
    digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
    assert receipt['customerFiles'] == [{'corpusPath': files[0], 'mediaType': 'text/csv', 'sha256': digest}]
    attached = json.loads(model.sent[0][1]['content'])['customerAttachments']
    assert attached == [{'name': 'invalid-rows.csv', 'mediaType': 'text/csv', 'sha256': digest,
                         'content': raw.decode()}]
    for bad in (['../oracles/town-A.json'], ['oracles/town-A.json', 'oracles/town-A.json'], ['nope.csv']):
        with pytest.raises(ValueError):
            runner.stage_customer_files(tmp_path / 'x', bad)


def test_import_report_comes_from_the_captured_server_response_to_the_sealed_action(candidate, tmp_path):
    candidate.extra_tools = True
    data = 'name,lon,lat\na,1,2\nb,,\n'
    ingest = json.dumps({'kind': 'tool_call', 'tool': 'honua_ingest_dataset',
                         'arguments': {'format': 'csv', 'data': data}})
    receipt, _, _ = run_hook_case(candidate, tmp_path, [ingest, tool(CORRECT_WHERE), FINAL], HookP10(report=True))
    record = {r['step']: r for r in receipt['controllerRecords']}['import-report']
    assert record['origin'] == 'session-result' and record['principal'] == 'agent'
    assert record['replayOf'] == receipt['sessions'][0]['actions'][0]['digest']
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


def test_exclusions_are_listed_never_passed_and_only_by_registered_rulings(candidate, tmp_path):
    marker = {'$excluded': 'founder-ruling-no-multi-tenant-2026.1'}
    from agent_e2e_local_expectations import check_exclusion
    assert check_exclusion('P03', 'tenant.denied', marker) == 'founder-ruling-no-multi-tenant-2026.1'
    assert check_exclusion('P18', 'mutations.absent', marker)
    for scenario_id, assertion, value in (('P10', 'filter.exact', marker), ('P03', 'tenant.denied', {'$excluded': 'other'})):
        with pytest.raises(ValueError):
            check_exclusion(scenario_id, assertion, value)
    # A scored case lists the excluded assertion and passes on the rest only.
    results = runner.score_assertions(['tenant.denied'], [], {'tenant.denied': marker}, records={}, scenario_id='P07')
    assert results == [{'assertion': 'tenant.denied', 'status': 'excluded', 'ruling': marker['$excluded']}]

    class Excluded(FakeP10):
        id = 'P18'
        def expected(self, variant): return {a: dict(marker) for a in case('A')['assertions']}
    runtime = FakeRuntime(candidate.base_url, FakeModel([]), tmp_path)
    receipt = runner.execute_case(case_id='P10:A:local-docker', scenario=Excluded(), variant='A', case=case('A'),
                                  candidate_digest=DIGEST, runtime=runtime)
    assert receipt['status'] == 'excluded' and not receipt['sessions'] and not runtime.workdirs
    assert {a['status'] for a in receipt['assertions']} == {'excluded'}
    runner.validate_receipt(receipt)
    with pytest.raises(ValueError):
        runner.validate_receipt({**receipt, 'assertions': [{'assertion': 'x', 'status': 'pass'}]})
    (tmp_path / 'a').mkdir()
    passing, _, _ = run_case(candidate, tmp_path / 'a', [tool(CORRECT_WHERE), FINAL])
    excluded = {**receipt, 'caseId': 'P10:B:local-docker'}
    summary = runner.aggregate(runner.build_inventory(runner.PACK, ['P10'], ['A', 'B']), [passing, excluded])
    assert summary['overall']['total'] == 1 and summary['overall']['passed'] == 1
    assert summary['overall']['excludedCases'] == ['P10:B:local-docker']
    assert [c['caseId'] for c in summary['cases']] == ['P10:A:local-docker', 'P10:B:local-docker']


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
    binding = RoleBinding('created:map', 'after', items='/result/items', id='/id', owner='/owner', before='before', total='/result/total')
    complete = {'before': record('before', 'seed', {'items': [], 'total': 0}, scope),
                'after': record('after', 'observe', {'items': rows, 'total': 1}, scope)}
    assert resolve_roles([binding], complete, agent_principal='agent') == {'created:map': 'm1'}
    incomplete = {**complete, 'before': record('before', 'seed', {'items': [], 'total': 3}, scope)}
    with pytest.raises(ValueError, match='incomplete'):   # the pre-existing m1 may be on an unread page
        resolve_roles([binding], incomplete, agent_principal='agent')
    other_scope = {**complete, 'before': record('before', 'seed', {'items': [], 'total': 0},
                                                {'tool': 'honua_list_maps', 'arguments': {'owner': 'someone'}})}
    with pytest.raises(ValueError, match='different request scope'):
        resolve_roles([binding], other_scope, agent_principal='agent')
    paged = RoleBinding('created:map', 'after', items='/result/items', id='/id', owner='/owner', before='before',
                        next_page='/result/next')
    more = {**complete, 'after': record('after', 'observe', {'items': rows, 'next': 'cursor-2'}, scope)}
    with pytest.raises(ValueError, match='further pages'):
        resolve_roles([paged], more, agent_principal='agent')
    with pytest.raises(ValueError, match='completeness'):
        resolve_roles([RoleBinding('created:map', 'after', items='/result/items', id='/id', owner='/owner', before='before')],
                      complete, agent_principal='agent')


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
