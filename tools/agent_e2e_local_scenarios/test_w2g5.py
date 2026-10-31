"""Fail-closed controls for the unsupported resilience cases."""
from __future__ import annotations

import copy
import importlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
from agent_e2e_local_api import Seeded, Stack

spec = importlib.util.spec_from_file_location('corpus_oracle', ROOT / 'certification/agent-e2e/corpus/oracle.py')
oracle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oracle)


@pytest.fixture(params=['P21', 'P22', 'P23'])
def scenario(request):
    return importlib.import_module('agent_e2e_local_scenarios.' + request.param).scenario


@pytest.fixture
def stack(tmp_path):
    return Stack('http://127.0.0.1:1', 'controller-only', None, 'case', 'unused',
                 str(tmp_path), 'sha256:' + '0' * 64)


def test_unsupported_case_cannot_seed_score_or_mutate(scenario, stack, tmp_path):
    assert scenario.supported_variants == ()
    assert scenario.id in type(scenario).__name__
    assert scenario.needs_approver() == (scenario.id in ('P21', 'P22'))
    for variant in ('A', 'B'):
        with pytest.raises(NotImplementedError):
            scenario.expected(variant)
    assert scenario.cleanup(stack, Seeded({})) is None
    assert list(tmp_path.iterdir()) == []
    assert not (ROOT / 'certification/agent-e2e/expectations/local' /
                (scenario.id + '.json')).exists()


def test_fixture_source_preservation_negative_control(scenario):
    # This tests the oracle, not an observation from an unsupported scenario.
    features = json.loads((ROOT / 'certification/agent-e2e/corpus/town-A/shelters.3857.geojson').read_text())
    snapshot = {'resourceId': 'fixture-shelters', 'content': features}
    expected = {'profile': 'honua.protected-content/v1', 'resources': [snapshot]}
    observed = {'profile': expected['profile'], 'before': [copy.deepcopy(snapshot)],
                'after': [copy.deepcopy(snapshot)]}
    oracle.verify('source-content.unchanged', observed, expected)
    observed['after'][0]['content']['features'][0]['properties']['name'] = 'changed'
    with pytest.raises(oracle.VerificationError):
        oracle.verify('source-content.unchanged', observed, expected)


@pytest.mark.parametrize(('scenario_id', 'assertion', 'expected', 'wrong'), [
    ('P21', 'fault.observed',
     {'injectionId': 'injection-1', 'targetOperationId': 'map-1',
      'observedFailure': {'status': 503, 'count': 1}},
     {'injectionId': 'injection-1', 'targetOperationId': 'map-1',
      'observedFailure': {'status': 200, 'count': 1}}),
    ('P22', 'session.fresh',
     {'previousSessionId': 'session-1', 'currentSessionId': 'session-2',
      'visibleConversationMessageIds': []},
     {'previousSessionId': 'session-1', 'currentSessionId': 'session-1',
      'visibleConversationMessageIds': []}),
    ('P23', 'cleanup.complete', {'resourceIds': []},
     {'resourceIds': ['owned-resource-still-present']}),
])
def test_scenario_oracle_negative_control(scenario_id, assertion, expected, wrong):
    # Synthetic controls exercise predicates only; they are not case evidence.
    scenario = importlib.import_module('agent_e2e_local_scenarios.' + scenario_id).scenario
    assert scenario.supported_variants == ()
    oracle.verify(assertion, copy.deepcopy(expected), expected)
    with pytest.raises(oracle.VerificationError):
        oracle.verify(assertion, wrong, expected)


def test_runner_contract():
    p21 = importlib.import_module('agent_e2e_local_scenarios.P21').scenario
    p22 = importlib.import_module('agent_e2e_local_scenarios.P22').scenario
    for variant in ('A', 'B'):
        assert p21.fault_plan(variant) == {
            'kind': 'response_lost_after_commit', 'id': 'p21-map-create',
            'after': None, 'match': {'tool': 'honua_studio_create_draft'},
            'commit': {'pointer': '/result/structuredContent/draftId'}}
        assert p22.fault_plan(variant) is None
    assert p22.sessions_required() == 2



def test_p21_fault_forwards_then_hides_successful_response():
    from agent_e2e_local_faults import FaultInjector, validate_fault_plan
    p21 = importlib.import_module('agent_e2e_local_scenarios.P21').scenario
    calls = []
    injector = FaultInjector(validate_fault_plan(p21.fault_plan('A')))
    def candidate(kind, action):
        calls.append(action)
        return {'status': 'ok', '_rawResponse': (200, b'{"jsonrpc":"2.0","id":1,"result":{"structuredContent":{"draftId":"d1"}}}')}
    execute = injector.wrap(candidate)
    action = {'tool': 'honua_studio_create_draft', 'arguments': {}}
    assert execute('tool_call', action)['httpStatus'] == 503
    assert calls == [action]
    event = injector.events[0]
    assert event['forwarded'] is True and event['upstreamOutcome'] == 'ok'
    assert event['upstreamStatus'] == 200 and event['upstreamResponseSha256']
    assert event['committedResourceId'] == 'd1'


def test_p21_failed_upstream_is_not_evidence_of_commit():
    from agent_e2e_local_faults import FaultInjector
    p21 = importlib.import_module('agent_e2e_local_scenarios.P21').scenario
    injector = FaultInjector(p21.fault_plan('A'))
    action = {'tool': 'honua_studio_create_draft', 'arguments': {}}
    injector.wrap(lambda *_: {'status': 'fail', '_rawResponse': (400, b'error')})('tool_call', action)
    # Rejected requests do not arm a misleading committed fault.
    assert injector.events == []
    assert injector.skipped[0]['upstreamOutcome'] == 'fail'
    assert injector.skipped[0]['upstreamStatus'] == 400


@pytest.mark.parametrize('variant', ['A', 'B'])
def test_p23_seed_records_distinct_owners_and_is_idempotent(stack, monkeypatch, variant):
    import agent_e2e_scenario_backend as backend
    import agent_e2e_p10_observer as observer
    class FakeTown:
        def __init__(self, recorder, manifest):
            self.recorder = recorder
        def prepare(self, **kwargs):
            return {'layers': {'shelters': {'service': 'shared', 'layerId': 1}}}
    class FakeRecorder:
        def __init__(self):
            self.calls = []
        def json(self, step, method, path, body=None, *, principal='controller'):
            self.calls.append((step, method, path, body, principal))
            if method == 'GET':
                return {'observation': {'httpStatus': 200, 'body': {}}}
            result = {**body, 'draftId': 'draft-' + principal}
            return {'observation': {'httpStatus': 201, 'body': {'data': result}}}
    monkeypatch.setattr(backend, 'TownSetup', FakeTown)
    monkeypatch.setattr(observer, 'source_path', lambda layer: '/shared/source')
    recorder = FakeRecorder()
    stack.extra.update(recorder=recorder, agentPrincipal='agent-id',
                       addPrincipal=lambda name, grants: 'foreign-id',
                       ensureConnection=lambda: 'connection-id')
    p23 = importlib.import_module('agent_e2e_local_scenarios.P23').scenario
    seed = p23.seed(stack, variant)
    assert p23.seed(stack, variant) == seed
    assert seed.state['owned']['owner'] == 'agent-id'
    assert seed.state['foreign']['owner'] == 'foreign-id'
    assert seed.state['shared']['service'] == 'shared'
    assert [call[4] for call in recorder.calls] == [
        'controller', 'agent', 'p23-foreign', 'controller', 'controller', 'controller']
    from agent_e2e_local_api import RunRecord
    run = RunRecord('agent-id', 1, (), (), ())
    result = p23.observe(stack, seed, variant, run)
    assert [row.assertion_id for row in result] == [
        'cleanup.complete', 'shared.preserved', 'source-content.unchanged']
    assert all(row.observed is None for row in result)
    assert [call[0] for call in recorder.calls[-3:]] == [
        'p23-inventory-after', 'p23-foreign-after', 'p23-source-after']
    with pytest.raises(ValueError):
        p23.observe(stack, seed, variant, RunRecord('foreign-id', 1, (), (), ()))
    with pytest.raises(ValueError):
        p23.seed(stack, 'B' if variant == 'A' else 'A')


@pytest.mark.parametrize('variant', ['A', 'B'])
def test_p22_seed_saves_and_reads_owned_version(stack, monkeypatch, variant):
    import agent_e2e_scenario_backend as backend
    import agent_e2e_p10_observer as observer
    class FakeTown:
        def __init__(self, recorder, manifest):
            pass
        def prepare(self, **kwargs):
            return {'layers': {'shelters': {'service': 'shared', 'layerId': 1}}}
    class FakeRecorder:
        def __init__(self):
            self.calls = []
            self.item = None
        def json(self, step, method, path, body=None, *, principal='controller'):
            self.calls.append(step)
            if step == 'p22-draft-create':
                self.item = body['itemId']
                value = {'draftId': 'draft-1', 'itemId': self.item, 'ownerId': 'agent-id'}
                status = 201
            elif step == 'p22-draft-validate':
                value, status = {'status': 'valid', 'diagnostics': []}, 200
            elif step in ('p22-draft-save', 'p22-version-before'):
                value = {'versionId': 'version-1', 'itemId': self.item, 'ownerId': 'agent-id'}
                status = 201 if step == 'p22-draft-save' else 200
            else:
                value, status = {}, 200
            return {'observation': {'httpStatus': status, 'body': {'data': value}}}
    monkeypatch.setattr(backend, 'TownSetup', FakeTown)
    monkeypatch.setattr(observer, 'source_path', lambda layer: '/shared/source')
    recorder = FakeRecorder()
    stack.extra.update(recorder=recorder, agentPrincipal='agent-id', ensureConnection=lambda: 'connection-id')
    p22 = importlib.import_module('agent_e2e_local_scenarios.P22').scenario
    seeded = p22.seed(stack, variant)
    assert seeded.state['versionId'] == 'version-1'
    assert p22.seed(stack, variant) == seeded
    assert recorder.calls == ['p22-source-before', 'p22-draft-create', 'p22-draft-validate',
                              'p22-draft-save', 'p22-version-before']
    from agent_e2e_local_api import RunRecord
    result = p22.observe(stack, seeded, variant, RunRecord('agent-id', 2, (), (), ()))
    assert len(result) == 5 and all(row.observed is None for row in result)
    assert recorder.calls[-2:] == ['p22-version-after', 'p22-source-after']
    with pytest.raises(ValueError):
        p22.observe(stack, seeded, variant, RunRecord('agent-id', 1, (), (), ()))


@pytest.mark.parametrize('variant', ['A', 'B'])
def test_p21_reads_committed_draft_and_rejects_missing_fault(stack, monkeypatch, variant):
    import agent_e2e_scenario_backend as backend
    import agent_e2e_p10_observer as observer
    from agent_e2e_local_api import RunRecord
    class FakeTown:
        def __init__(self, recorder, manifest):
            pass
        def prepare(self, **kwargs):
            return {'layers': {'shelters': {'service': 'shared', 'layerId': 1}}}
    class FakeRecorder:
        def __init__(self):
            self.calls = []
        def json(self, step, method, path, body=None, *, principal='controller'):
            self.calls.append(step)
            value = {'draftId': 'draft-1', 'ownerId': 'agent-id'} if step == 'p21-committed-draft' else {}
            return {'observation': {'httpStatus': 200, 'body': {'data': value}}}
    monkeypatch.setattr(backend, 'TownSetup', FakeTown)
    monkeypatch.setattr(observer, 'source_path', lambda layer: '/shared/source')
    recorder = FakeRecorder()
    stack.extra.update(recorder=recorder, agentPrincipal='agent-id', ensureConnection=lambda: 'connection-id')
    p21 = importlib.import_module('agent_e2e_local_scenarios.P21').scenario
    seeded = p21.seed(stack, variant)
    assert p21.seed(stack, variant) == seeded
    with pytest.raises(ValueError):
        p21.observe(stack, seeded, variant, RunRecord('agent-id', 1, (), (), ()))
    event = {'id': 'p21-map-create', 'kind': 'response_lost_after_commit',
             'tool': 'honua_studio_create_draft', 'forwarded': True, 'httpStatus': 503,
             'upstreamOutcome': 'ok', 'upstreamStatus': 200,
             'upstreamResponseSha256': 'sha256:' + '1' * 64, 'committedResourceId': 'draft-1'}
    rows = p21.observe(stack, seeded, variant, RunRecord('agent-id', 1, (event,), (), ()))
    assert len(rows) == 5 and all(row.observed is None for row in rows)
    assert recorder.calls == ['p21-source-before', 'p21-drafts-before', 'p21-committed-draft',
                              'p21-drafts-after', 'p21-source-after']
    bad = {**event, 'committedResourceId': 'wrong-draft'}
    with pytest.raises(ValueError):
        p21.observe(stack, seeded, variant, RunRecord('agent-id', 1, (bad,), (), ()))
