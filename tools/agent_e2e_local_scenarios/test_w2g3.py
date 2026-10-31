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
            return {'layers': {'shelters': {'service': 'case-shelters', 'layerId': 1}}}
    class FakeRecorder:
        def __init__(self): self.calls = []
        def json(self, *args): self.calls.append(args)
    monkeypatch.setattr('agent_e2e_scenario_backend.TownSetup', FakeTown)
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
    assert observation.evidence['spec']['protectedContent'][0]['before'] == 'p11-shelters-before'
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
    monkeypatch.setattr('agent_e2e_scenario_backend.TownSetup', FakeTown)
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
    assert [item.assertion_id for item in observations] == ['source-content.unchanged']
    with pytest.raises(ValueError):
        P13.scenario.seed(stack, 'B' if variant == 'A' else 'A')


@pytest.mark.parametrize('scenario_id', ['P11', 'P13'])
@pytest.mark.parametrize('variant', ['A', 'B'])
def test_seeded_shelter_preservation_rejects_mutation(scenario_id, variant):
    expected = load_expected(scenario_id, variant)['source-content.unchanged']
    baseline = expected['resources']
    observed = {'profile': expected['profile'], 'before': copy.deepcopy(baseline), 'after': copy.deepcopy(baseline)}
    oracle.verify('source-content.unchanged', observed, expected)
    observed['after'][0]['content']['features'][0]['properties']['name'] = 'changed'
    with pytest.raises(oracle.VerificationError):
        oracle.verify('source-content.unchanged', observed, expected)


def test_p13_restore_projection_rejects_noop_wrong_style_and_pointer():
    from agent_e2e_local_projections_w2g3 import project_restore
    from agent_e2e_local_recorder import ProjectionError, Record
    def record(path, data, *, seed=False):
        body = {'success': True, 'data': data}
        raw = json.dumps(body).encode()
        return Record('fixture', 'seed' if seed else 'observe', 'http', 'controller', 'controller-id',
                      'POST' if seed else 'GET', path, 201 if seed else 200,
                      raw, 'sha256:' + hashlib.sha256(raw).hexdigest(), body)
    item, v1, owner = 'item-1', 'version-1', 'agent-1'
    def version(vid, number, color):
        return {'itemId': item, 'versionId': vid, 'ownerId': owner, 'createdBy': owner,
                'versionNumber': number, 'contentHash': 'a' * 64,
                'envelope': {'body': {'layers': [{'id': 'shelters', 'style': {'color': color}}]}}}
    first, changed = version(v1, 1, 'blue'), version('version-2', 2, 'red')
    root = '/api/v1/studio/content-items/' + item + '/versions'
    seed_record = record('/api/v1/studio/package-drafts/draft/content-versions', copy.deepcopy(first), seed=True)
    records = {'p13-target-save': seed_record, 'p13-target-after': record('/api/v1/studio/content-items?workspaceId=fixture',
                    {'total': 1, 'nextCursor': None, 'items': [{'itemId': item, 'ownerId': owner, 'currentVersionId': v1}]}),
               'p13-target-versions': record(root, {'itemId': item, 'versions': [first, changed]}),
               'p13-target-v1': record(root + '/' + v1, first)}
    expected = {'resourceId': item, 'restoredVersionId': v1, 'documentSha256': 'a' * 64}
    actual = project_restore(records, item_id=item, version_id=v1, agent_principal=owner)
    oracle.verify('version.restored', actual, expected)
    for mutate in ('no-op', 'same-style', 'wrong-pointer', 'foreign-author'):
        bad = copy.deepcopy(records)
        if mutate == 'no-op': bad['p13-target-versions'].json['data']['versions'].pop()
        if mutate == 'same-style': bad['p13-target-versions'].json['data']['versions'][1]['envelope']['body']['layers'][0]['style']['color'] = 'blue'
        if mutate == 'wrong-pointer': bad['p13-target-after'].json['data']['items'][0]['currentVersionId'] = 'version-2'
        if mutate == 'foreign-author': bad['p13-target-versions'].json['data']['versions'][1]['createdBy'] = 'foreign'
        step = 'p13-target-after' if mutate == 'wrong-pointer' else 'p13-target-versions'
        bad[step] = record(bad[step].path, bad[step].json['data'])
        with pytest.raises(ProjectionError):
            project_restore(bad, item_id=item, version_id=v1, agent_principal=owner)
