"""A/B fixture controls; unsupported modules do not claim live evidence."""
import copy
import hashlib
import json
import math
from types import SimpleNamespace

import pytest

from agent_e2e_local_api import Stack
from agent_e2e_local_expectations import load_expected
from agent_e2e_local_runner import load_scenario, oracle
from agent_e2e_local_scenarios import P11


@pytest.mark.parametrize('scenario_id', ['P08', 'P09', 'P11', 'P13', 'P19'])
def test_runner_discovers_fail_closed_scenarios(scenario_id):
    scenario = load_scenario(scenario_id)
    assert scenario.id == scenario_id
    assert scenario.supported_variants == ()
    assert scenario.fault_plan('A') is None
    assert scenario.sessions_required() == 1


@pytest.mark.parametrize('variant', ['A', 'B'])
def test_metric_buffer_fixture_rejects_wrong_radius(variant):
    expected = load_expected('P11', variant)['buffer.metric']
    cx, cy = expected['center3857']
    radius = expected['radiusMetres']
    ring = [[cx + radius * math.cos(2 * math.pi * i / 128),
             cy + radius * math.sin(2 * math.pi * i / 128)] for i in range(128)]
    ring.append(ring[0][:])
    observed = {'srid': 3857, 'sourceId': expected['sourceId'],
                'geometry': {'type': 'Polygon', 'coordinates': [ring]}}
    oracle.verify('buffer.metric', observed, expected)
    wrong = copy.deepcopy(observed)
    wrong['geometry']['coordinates'][0][1][0] += 100
    with pytest.raises(oracle.VerificationError):
        oracle.verify('buffer.metric', wrong, expected)


@pytest.mark.parametrize('variant', ['A', 'B'])
def test_p11_seed_idempotent_and_declares_recorded_source_projection(monkeypatch, tmp_path, variant):
    from agent_e2e_scenario_backend import TownSetup
    class FakeTown:
        def __init__(self, recorder, manifest):
            self.recorder = recorder
        def prepare(self, **kwargs):
            self.recorder.calls.append(('prepare', kwargs))
            return {'layers': {name: {'service': 'case-' + name, 'layerId': i}
                               for i, name in enumerate(('parcels', 'flood_zones', 'shelters'))}}
    class FakeRecorder:
        def __init__(self): self.calls = []
        def json(self, *args): self.calls.append(args)
        def mcp_call(self, *args): self.calls.append(args)
    monkeypatch.setattr('agent_e2e_local_scenarios.w2g3_common.town_setup', lambda recorder, variant: FakeTown(recorder, None))
    recorder = FakeRecorder()
    stack = Stack('http://127.0.0.1:1', 'controller', None, 'case-123', 'compose.yml', str(tmp_path), 'sha256:test',
                  {'recorder': recorder, 'ensureConnection': lambda: 'fixture-connection'})
    first = P11.scenario.seed(stack, variant)
    assert P11.scenario.seed(stack, variant) == first
    assert sum(call[0] == 'prepare' for call in recorder.calls) == 1
    with pytest.raises(ValueError):
        P11.scenario.seed(stack, 'B' if variant == 'A' else 'A')
    observation = P11.scenario.observe(stack, first, variant)[0]
    assert observation.assertion_id == 'source-content.unchanged'
    assert observation.observed is None
    assert observation.evidence == {'projection': 'extract/v1'}
    assert recorder.calls[-1][0] == 'p11-shelters-after'


@pytest.mark.parametrize('variant', ['A', 'B'])
def test_p13_seed_creates_two_distinct_saved_maps_once(monkeypatch, tmp_path, variant):
    from agent_e2e_local_scenarios import P13
    class FakeTown:
        def __init__(self, recorder, manifest): pass
        def prepare(self, **kwargs):
            return {'layers': {name: {'service': 'case-' + name, 'layerId': i}
                               for i, name in enumerate(('parcels', 'flood_zones', 'shelters'))}}
    class FakeRecorder:
        def __init__(self): self.calls = []; self.maps = {}
        def mcp_call(self, *args): self.calls.append(args)
        def json(self, step, method, path, body=None, **kwargs):
            self.calls.append((step, method, path, body))
            prefix = step.rsplit('-', 1)[0]
            if step.endswith('-create'):
                self.maps[prefix] = {'itemId': body['itemId'], 'ownerId': body['ownerId'],
                                     'draftId': prefix + '-draft', 'versionId': prefix + '-version'}
                value, status = self.maps[prefix], 201
            elif step.endswith('-validate'):
                value, status = {'status': 'valid', 'diagnostics': []}, 200
            elif step.endswith('-save'):
                value, status = {**self.maps[prefix], 'versionNumber': 1, 'contentHash': 'a' * 64}, 201
            elif step.endswith('-owner-read'):
                value, status = self.maps[step[:-len('-owner-read')]], 200
            elif step.endswith('-before') and prefix != 'p13-shelters':
                m = self.maps[prefix]
                value, status = {'items': [{'itemId': m['itemId'], 'currentVersionId': m['versionId']}],
                                 'total': 1, 'nextCursor': None}, 200
            else:
                value, status = {}, 200
            return {'observation': {'httpStatus': status, 'body': {'success': True, 'data': value}}}
    monkeypatch.setattr('agent_e2e_local_scenarios.w2g3_common.town_setup', lambda recorder, variant: FakeTown(recorder, None))
    recorder = FakeRecorder()
    stack = Stack('http://127.0.0.1:1', 'controller', None, 'case-123', 'compose.yml', str(tmp_path), 'sha256:test',
                  {'recorder': recorder, 'ensureConnection': lambda: 'fixture-connection', 'agentPrincipal': 'agent-id', 'addPrincipal': lambda name, grants: 'shared-id'})
    seeded = P13.scenario.seed(stack, variant)
    count = len(recorder.calls)
    assert P13.scenario.seed(stack, variant) == seeded
    assert len(recorder.calls) == count
    assert seeded.state['target']['itemId'] != seeded.state['shared']['itemId']
    assert seeded.state['target']['versionId'] != seeded.state['shared']['versionId']
    assert seeded.state['target']['owner'] != seeded.state['shared']['owner']
    observations = P13.scenario.observe(stack, seeded, variant)
    assert [item.assertion_id for item in observations] == ['version.restored', 'shared.preserved', 'source-content.unchanged']
    with pytest.raises(ValueError):
        P13.scenario.seed(stack, 'B' if variant == 'A' else 'A')


@pytest.mark.parametrize('scenario_id', ['P11', 'P13'])
@pytest.mark.parametrize('variant', ['A', 'B'])
def test_seeded_shelter_preservation_rejects_mutation(scenario_id, variant):
    expected = load_expected(scenario_id, variant)['source-content.unchanged']
    baseline = copy.deepcopy(expected['resources'])
    for resource in baseline:
        resource['content']['serviceId'] = 'fixture-service'
    expected = {**expected, 'resources': copy.deepcopy(baseline)}
    observed = {'profile': expected['profile'], 'before': copy.deepcopy(baseline), 'after': copy.deepcopy(baseline)}
    oracle.verify('source-content.unchanged', observed, expected)
    observed['after'][0]['content']['features'][0]['properties']['name'] = 'changed'
    with pytest.raises(oracle.VerificationError):
        oracle.verify('source-content.unchanged', observed, expected)


@pytest.mark.parametrize('variant', ['A', 'B'])
def test_town_setup_verifies_only_selected_fixture_bytes(monkeypatch, variant):
    from agent_e2e_local_scenarios import w2g3_common
    original = w2g3_common.verify_corpus
    calls = []
    def verify(root, manifest):
        paths = {row['path'] for row in manifest['files']}
        assert paths == {f'town-{variant}/{name}.3857.geojson' for name in ('parcels', 'flood_zones', 'shelters')}
        calls.append(paths)
        return original(root, manifest)
    monkeypatch.setattr(w2g3_common, 'verify_corpus', verify)
    setup = w2g3_common.town_setup(object(), variant)
    assert len(calls) == 1 and set(setup.files) == calls[0]


@pytest.mark.parametrize('variant', ['A', 'B'])
def test_frozen_restore_projection_rejects_actual_noop(variant):
    from agent_e2e_local_api import Observation
    from agent_e2e_local_expectations import load_projections
    from agent_e2e_local_recorder import Record
    from agent_e2e_local_runner import score_assertions
    item = '11111111-1111-1111-1111-111111111111'
    version = '22222222-2222-2222-2222-222222222222'
    owner, digest = 'agent', 'a' * 64
    def record(step, path, value):
        raw = json.dumps({'data': value}).encode()
        return Record(step, 'observe', 'http', 'controller', 'controller', 'GET', path, 200,
                      raw, 'sha256:' + hashlib.sha256(raw).hexdigest())
    v1 = {'versionId': version, 'createdBy': owner, 'envelope': {'body': {'layers': [{}, {}, {'style': {'color':'blue'}}]}}}
    v2 = {'versionId': 'new', 'createdBy': owner, 'envelope': {'body': {'layers': [{}, {}, {'style': {'color':'red'}}]}}}
    root = '/api/v1/studio/content-items/' + item + '/versions'
    records = {'p13-target-after': record('p13-target-after', '/api/v1/studio/content-items?workspaceId=case', {'items':[{'itemId':item,'currentVersionId':version}]}),
               'p13-target-v1': record('p13-target-v1', root + '/' + version, {'contentHash':digest, **v1}),
               'p13-target-versions': record('p13-target-versions', root, {'versions':[v1,v2]})}
    expected = load_expected('P13', variant)['version.restored']
    roles = {'seeded:map:target':item, 'seeded:version:v1':version, 'seeded:hash:v1':digest}
    observation = Observation('version.restored', None, {'projection':'extract/v1'})
    def score(rows):
        return score_assertions(['version.restored'], [observation], {'version.restored':expected}, records=rows,
                                roles=roles, run_facts={'agentPrincipal':owner}, scenario_id='P13',
                                frozen=load_projections('P13', variant))[0]['status']
    assert score(records) == 'pass'
    records['p13-target-versions'] = record('p13-target-versions', root, {'versions':[v1]})
    assert score(records) == 'fail'


def test_restore_frozen_projection_with_recorded_http_and_wrong_live_history(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from agent_e2e_local_api import Observation
    from agent_e2e_local_expectations import load_projections
    from agent_e2e_local_recorder import ControllerRecorder
    from agent_e2e_local_runner import score_assertions
    item = '11111111-1111-1111-1111-111111111111'
    v1id = '22222222-2222-2222-2222-222222222222'
    owner, digest = 'agent', 'a' * 64
    def version(vid, style):
        return {'versionId':vid, 'createdBy':owner, 'envelope':{'body':{'layers':[{}, {}, {'style':{'color':style}}]}}}
    v1, v2 = version(v1id,'blue'), version('new','red')
    history = {'versions':[v1,v2]}
    root = '/api/v1/studio/content-items/' + item + '/versions'
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            if self.path.startswith('/api/v1/studio/content-items?'):
                data = {'items':[{'itemId':item,'currentVersionId':v1id}]}
            elif self.path == root: data = history
            elif self.path == root + '/' + v1id: data = {'contentHash':digest, **v1}
            else: self.send_error(404); return
            raw = json.dumps({'data':data}).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
    server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for wrong in (False, True):
            if wrong: history['versions'] = [v1]
            recorder = ControllerRecorder(base_url=f'http://127.0.0.1:{server.server_port}',
                workdir=str(tmp_path / str(wrong)), candidate_digest='sha256:'+'a'*64,
                attempt_id='test', principals={'controller':(None,'controller')})
            recorder.set_phase('observe')
            recorder.json('p13-target-after','GET','/api/v1/studio/content-items?workspaceId=case')
            recorder.json('p13-target-v1','GET',root+'/'+v1id)
            recorder.json('p13-target-versions','GET',root)
            result = score_assertions(['version.restored'], [Observation('version.restored',None,{'projection':'extract/v1'})],
                {'version.restored':load_expected('P13','A')['version.restored']}, records=recorder.records,
                roles={'seeded:map:target':item,'seeded:version:v1':v1id,'seeded:hash:v1':digest},
                run_facts={'agentPrincipal':owner}, scenario_id='P13', frozen=load_projections('P13','A'))
            assert result[0]['status'] == ('fail' if wrong else 'pass')
    finally:
        server.shutdown(); server.server_close()


@pytest.mark.parametrize('scenario_id', ['P11','P13'])
@pytest.mark.parametrize('variant', ['A','B'])
def test_frozen_town_projection_rejects_altered_polygon(scenario_id, variant):
    from pathlib import Path
    from agent_e2e_local_api import Observation
    from agent_e2e_local_expectations import load_projections
    from agent_e2e_local_recorder import Record
    from agent_e2e_local_runner import score_assertions
    root = Path(__file__).resolve().parents[2] / 'certification/agent-e2e/corpus'
    roles, records = {}, {}
    stem = scenario_id.lower()
    for index, name in enumerate(('parcels', 'flood_zones', 'shelters')):
        service = 'case-' + name
        roles[f'seeded:service:{name}'] = service
        roles[f'seeded:layer:{name}'] = index
        features = json.loads((root/f'town-{variant}'/f'{name}.3857.geojson').read_text())['features']
        params = {'tool':'honua_query_features', 'arguments':{'serviceId':service,'layerId':index}}
        argraw = json.dumps(params).encode()
        for phase, suffix in (('seed','before'),('observe','after')):
            step = f'{stem}-{name.replace("_", "-")}-{suffix}'
            body = {'jsonrpc':'2.0','id':1,'result':{'structuredContent':{'serviceId':service,
                'returnedCount':len(features),'exceededTransferLimit':False,'resultOffset':0,
                'geojson':{'type':'FeatureCollection','features':copy.deepcopy(features)}}}}
            raw = json.dumps(body).encode()
            records[step] = Record(step,phase,'mcp','controller','controller','POST','/mcp#tools/call',200,
                raw,'sha256:'+hashlib.sha256(raw).hexdigest(),request_arguments_raw=argraw)
    obs=Observation('source-content.unchanged',None,{'projection':'extract/v1'})
    expected={'source-content.unchanged':load_expected(scenario_id,variant)['source-content.unchanged']}
    def score():
        return score_assertions(list(expected),[obs],expected,records=records,roles=roles,scenario_id=scenario_id,
                                frozen=load_projections(scenario_id,variant))[0]['status']
    assert score() == 'pass'
    step=f'{stem}-flood-zones-after'; old=records[step]; body=old.json
    body['result']['structuredContent']['geojson']['features'][0]['geometry']['coordinates'][0][0][0] += 42
    raw=json.dumps(body).encode()
    records[step]=Record(step,'observe','mcp','controller','controller','POST','/mcp#tools/call',200,
        raw,'sha256:'+hashlib.sha256(raw).hexdigest(),request_arguments_raw=old.request_arguments_raw)
    assert score() == 'fail'


@pytest.mark.parametrize('variant', ['A','B'])
def test_frozen_shared_map_snapshot_rejects_changed_pointer(variant):
    from agent_e2e_local_api import Observation
    from agent_e2e_local_expectations import load_projections
    from agent_e2e_local_recorder import Record
    from agent_e2e_local_runner import score_assertions
    roles={'seeded:workspace:shared':'shared-workspace','seeded:owner:shared':'shared-owner',
           'seeded:map:shared':'shared-item','seeded:version:shared':'shared-v1'}
    path='/api/v1/studio/content-items?family=map&workspaceId=shared-workspace&owner=shared-owner&limit=100'
    def record(step,phase,current):
        body={'data':{'total':1,'items':[{'itemId':'shared-item','currentVersionId':current,
             'publishedVersionId':None,'ownerId':'shared-owner','workspaceId':'shared-workspace'}]}}
        raw=json.dumps(body).encode()
        return Record(step,phase,'http','controller','controller','GET',path,200,raw,
                      'sha256:'+hashlib.sha256(raw).hexdigest())
    records={'p13-shared-before':record('p13-shared-before','seed','shared-v1'),
             'p13-shared-after':record('p13-shared-after','observe','shared-v1')}
    expected={'shared.preserved':load_expected('P13',variant)['shared.preserved']}
    obs=Observation('shared.preserved',None,{'projection':'extract/v1'})
    def score():
        return score_assertions(list(expected),[obs],expected,records=records,roles=roles,scenario_id='P13',
                                frozen=load_projections('P13',variant))[0]['status']
    assert score() == 'pass'
    records['p13-shared-after']=record('p13-shared-after','observe','foreign-v2')
    assert score() == 'fail'
