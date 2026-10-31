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

    def mcp(self, message, principal):
        method = message.get('method')
        if method == 'initialize':
            return {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'fake'}}
        if method == 'tools/list':
            return {'tools': self.TOOLS}
        if method == 'resources/read':
            return {'contents': [{'uri': message['params']['uri'], 'text': '{"processes":[]}'}]}
        if method == 'tools/call':
            name, arguments = message['params']['name'], message['params']['arguments']
            self.mcp_calls.append((principal['id'], name, arguments))
            if name == 'honua_query_features':
                return self.query(arguments)
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
    with pytest.raises(ValueError):  # a created role needs an owner or a seed-phase baseline
        resolve_roles([RoleBinding('created:svc', 'after-publish', items='/data', id='/x')],
                      recorder.records, agent_principal='agent')


def test_created_role_requires_exactly_one_new_owned_resource():
    from agent_e2e_local_projections import resolve_roles
    from agent_e2e_local_recorder import Record

    def record(step, phase, items):
        raw = json.dumps({'items': items}).encode()
        return Record(step, phase, 'http', 'controller', None, 'GET', '/maps', 200, raw,
                      'sha256:' + hashlib.sha256(raw).hexdigest(), json.loads(raw))
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
    assert 'temperature' not in sent[0] and sent[1]['temperature'] == 0
