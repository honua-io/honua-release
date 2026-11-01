"""Stage-three to stage-four handoff: what the agent published in services, read back by the harness, is what
Studio (#1198) starts from; a readback gap is a harness-undetermined services outcome, never the arm's."""
from __future__ import annotations

import base64
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai_arc_runner as R  # noqa: E402
from test_ai_arc_runner import ARC, A1, CAND_IN, FakeSession, FakeStage, modules, run  # noqa: E402

KEY = 'k-agent-1'
START, END = '2026-11-01T20:00:00.000000Z', '2026-11-01T20:30:00.000000Z'
ROLES = {'created:service:shelters': 'kestrel-shelters', 'child:layer:shelters': 0,
         'seeded:table:shelters': 'arc10_x_shelters', 'seeded:schema:shelters': 'honua_data',
         'seeded:connection:shelters': 'conn-1'}
FIELDS = [{'name': 'source_id', 'type': 'esriFieldTypeString'}, {'name': 'capacity', 'type': 'esriFieldTypeInteger'}]


def event(audit_id=7, actor='api-key:' + KEY, resource='op-1', ts='2026-11-01T20:10:00Z', details=None):
    return {'auditId': audit_id, 'actor': actor, 'outcome': 'Success', 'action': 'operation.completed',
            'resourceType': 'operation_instance', 'resourceId': resource, 'timestamp': ts,
            'details': details or 'operationId=service.publish;status=Completed;x=1'}


def controller(events, *, handles=None, meta=None):
    handles = handles if handles is not None else {'op-1': ('kestrel-shelters', 0)}
    calls = []

    def http(method, url, key):
        calls.append(url)
        assert method == 'GET' and key == 'admin'
        path = url.split('http://c', 1)[1]
        if path.startswith(R.AUDIT_PATH):
            return {'status': 200, 'json': {'items': events, 'nextCursor': None}}
        if path.startswith('/api/v1/operations/handles/'):
            op = path.rsplit('/', 1)[1]
            if op not in handles:
                return {'status': 404, 'json': {}}
            service, layer = handles[op]
            return {'status': 200, 'json': {'data': {'operationInstanceId': op, 'operationId': 'service.publish',
                                                     'status': 'Completed',
                                                     'resourceIds': {'serviceName': service, 'layerId': layer}}}}
        if path.startswith('/rest/services/'):
            return meta if meta is not None else {'status': 200, 'json': {'geometryType': 'esriGeometryPoint',
                                                                          'fields': FIELDS}}
        return {'status': 404}
    return http, calls


def ctx_for(tmp_path, http, variant='A'):
    slot = R.Slot('local-docker', 'vm', 'arc-abcdef1234', controller={'base_url': 'http://c', 'admin_key': 'admin'})
    records = tmp_path / 'services' / 'corpus' / 'P10' / 'export' / 'controller-records'
    records.mkdir(parents=True)
    for name in ('parcels', 'flood-zones', 'shelters'):
        body = {'success': True, 'physicalTableName': 'arc10_x_' + name.replace('-', '_'), 'schema': 'honua_data'}
        (records / f'import-{name}.json').write_text(json.dumps({'bodyBase64': base64.b64encode(
            json.dumps(body).encode()).decode()}))
    return R.StageContext(arc=ARC, target='local-docker', arm=A1, variant=variant, run_dir=tmp_path,
                          private_dir=tmp_path, slot=slot, candidate=CAND_IN, install=ARC['versionPair']['install'],
                          limits=ARC['limits']['services'], deadline=time.monotonic() + 60, options={'http': http})


def cases(roles=None, status='pass', principal=KEY):
    return [{'scenario': 'P10', 'receipt': {'status': status, 'roles': dict(ROLES if roles is None else roles)},
             'agentPrincipal': principal},
            {'scenario': 'P15', 'receipt': {'status': 'pass', 'roles': {}}, 'agentPrincipal': 'k-agent-2'}]


def test_the_handoff_names_what_the_agent_published_with_fields_geometry_connection_and_tables(tmp_path):
    http, calls = controller([event(), event(8, actor='api-key:someone-else', resource='op-9')])
    handoff = R.studio_services_handoff(ctx_for(tmp_path, http), cases(), 'town-A', START, END)
    assert handoff == {
        'schema': R.STUDIO_HANDOFF_SCHEMA, 'variant': 'A', 'fixtureId': 'town-A', 'principal': 'api-key:' + KEY,
        'agentKeyId': KEY, 'startedAt': START, 'endedAt': END, 'connectionId': 'conn-1',
        'layers': {'shelters': {'service': 'kestrel-shelters', 'layerId': 0, 'operationInstanceId': 'op-1',
                                'auditId': 7, 'geometryType': 'esriGeometryPoint', 'fields': FIELDS}},
        'tables': {n: {'schema': 'honua_data', 'table': 'arc10_x_' + n} for n in R.STUDIO_LAYERS}}
    # Only what the agent published crosses (Gap 1 ruling): no parcels or flood-zone service is invented.
    assert set(handoff['layers']) == {'shelters'}
    assert any('action=operation.completed' in c and 'from=' in c for c in calls)
    assert not any('op-9' in c for c in calls)        # a foreign actor's operation is never followed


@pytest.mark.parametrize('events,handles,meta,roles,principal,reason', [
    ([event(actor='api-key:harness')], None, None, None, KEY, 'no unique completed agent publication'),
    ([event(ts='2026-11-01T19:59:59Z')], None, None, None, KEY, 'no unique completed agent publication'),
    ([event(details='operationId=service.delete;status=Completed')], None, None, None, KEY, 'no unique'),
    ([event()], {'op-1': ('other-service', 0)}, None, None, KEY, 'no unique completed agent publication'),
    ([event(), event(8, resource='op-2')], {'op-1': ('kestrel-shelters', 0), 'op-2': ('kestrel-shelters', 0)},
     None, None, KEY, 'no unique completed agent publication'),
    ([event()], None, {'status': 500}, None, KEY, 'controller read failed'),
    ([event()], None, {'status': 200, 'json': {'fields': FIELDS}}, None, KEY, 'fields or geometry type'),
    ([event()], None, None, {k: v for k, v in ROLES.items() if not k.startswith(('created', 'child'))}, KEY,
     'published no fixture layer'),
    ([event()], None, None, {**ROLES, 'seeded:table:shelters': 'arc10_x_parcels'}, KEY, 'differs from the imported'),
    ([event()], None, None, None, None, 'agent key is unknown'),
])
def test_every_readback_gap_refuses_the_handoff(tmp_path, events, handles, meta, roles, principal, reason):
    http, _ = controller(events, handles=handles, meta=meta)
    with pytest.raises(ValueError, match=reason):
        R.studio_services_handoff(ctx_for(tmp_path, http), cases(roles, principal=principal), 'town-A', START, END)


def test_a_failed_publishing_case_gives_no_handoff(tmp_path):
    http, _ = controller([event()])
    with pytest.raises(ValueError, match='did not pass'):
        R.studio_services_handoff(ctx_for(tmp_path, http), cases(status='fail'), 'town-A', START, END)


def services_evidence(handoff_status):
    observations = [{'assertion': a, 'status': 'pass'} for a in R.required_assertions(ARC, 'services', 'local-docker')
                    if not R.is_excluded(a, 'local-docker', ARC['exclusions'])]
    context = {'nestedAnswerChecks': True, 'failure': None, 'nudges': 0, 'elapsedSeconds': 10, 'utterances': [],
               'approvals': [], 'scope': None, 'shutdown': {'complete': True}, 'harnessUnsafe': [], 'sessions': [],
               'studioHandoff': handoff_status}
    return observations, context


@pytest.mark.parametrize('status,outcome', [({'status': 'verified', 'handoff': {}}, 'pass'),
                                            ({'status': 'failed', 'reason': 'audit unreadable'}, 'undetermined')])
def test_a_handoff_readback_failure_is_a_harness_undetermined_services_outcome(status, outcome):
    observations, context = services_evidence(status)
    actions = [{'kind': 'tool_call', 'tool': 'honua_publish_service', 'arguments': {}, 'status': 'pass'}]
    scored = R.score_stage(ARC, 'local-docker', 'services', observations, actions, 'done', None, context,
                           ARC['limits']['services'])
    assert scored['outcome'] == outcome
    if outcome == 'undetermined':
        assert scored['cause'].startswith('harness: stage-three handoff readback failed')


class PublishingServices(FakeStage):
    def __init__(self, passes=True, verified=True):
        super().__init__('services', passes=passes)
        self.verified = verified

    def observe(self, ctx, row):
        rows = super().observe(ctx, row)
        self.studio_handoff = ({'status': 'verified', 'handoff': {'schema': R.STUDIO_HANDOFF_SCHEMA, 'layers': {}}}
                               if self.verified else {'status': 'failed', 'reason': 'no unique publication'})
        return rows

    def harness_context(self, ctx):
        return {'studioHandoff': self.studio_handoff, 'notAKey': 1}


class SeesHandoff(FakeStage):
    def __init__(self):
        super().__init__('studio')
        self.seen = 'unset'

    def run(self, ctx, session, prompt):
        self.seen = ctx.slot.facts.get('studioServices')
        return super().run(ctx, session, prompt)


def test_stage_four_starts_from_the_verified_handoff_only_after_a_services_pass(tmp_path):
    studio = SeesHandoff()
    receipt, _ = run(tmp_path, mods=modules(services=PublishingServices(), studio=studio))
    services = receipt['stages'][2]
    assert services['outcome'] == 'pass' and studio.seen == {'schema': R.STUDIO_HANDOFF_SCHEMA, 'layers': {}}
    assert services['studioServicesSha256'] == R.sha256(R.canonical(studio.seen))
    found = [p for p in tmp_path.rglob('context.json') if p.parent.name == 'services']
    assert found
    context = json.loads(found[0].read_bytes())
    assert context['studioHandoff']['status'] == 'verified' and 'notAKey' not in context


@pytest.mark.parametrize('passes,verified,outcome', [(False, True, 'fail'), (True, False, 'undetermined')])
def test_no_handoff_without_a_services_pass_and_studio_is_blocked(tmp_path, passes, verified, outcome):
    studio = SeesHandoff()
    receipt, _ = run(tmp_path, mods=modules(services=PublishingServices(passes, verified), studio=studio))
    assert receipt['stages'][2]['outcome'] == outcome and receipt['stages'][3]['outcome'] == 'blocked'
    assert studio.seen == 'unset' and 'studioServicesSha256' not in receipt['stages'][2]


def services_ctx(tmp_path, case=None, variant='A'):
    ctx = ctx_for(tmp_path, lambda *a: {'status': None}, variant=variant)
    ctx.slot.vm = object()
    ctx.case = case
    return ctx


def test_development_and_held_out_services_take_the_fixture_path(tmp_path):
    stages = {s['id']: s for s in ARC['stages']}
    stage = R.ServicesStage(tuple(stages['services']['scenarios']), stages['services']['developmentFaults'])
    assert stage.fixture(services_ctx(tmp_path)) == ('town-A', 'http_503_once')
    assert stage.fixture(services_ctx(tmp_path / 'b', variant='B')) == ('town-B', 'response_lost_after_commit')
    held = services_ctx(tmp_path / 'h', case={'fixtures': 'town-B', 'fault': 'http_503_once'}, variant='H')
    assert stage.fixture(held) == ('town-B', 'http_503_once')
    assert stage.unavailable(held) is None                 # held-out is no longer refused
    assert 'catalog differs' in R.ServicesStage(('P10',), {'A': 'http_503_once'}).unavailable(services_ctx(tmp_path / 'c'))
    assert 'not in the committed fixture catalog' in stage.unavailable(
        services_ctx(tmp_path / 'u', case={'fixtures': 'town-Z', 'fault': 'http_503_once'}, variant='H'))
    assert 'no services fault' in R.ServicesStage(tuple(stages['services']['scenarios']), {}).unavailable(
        services_ctx(tmp_path / 'f'))


def test_development_services_goals_are_customer_language():
    goals = ARC['targets']['local-docker']['goals']['services']
    assert set(goals) == {'A', 'B'}
    assert all(len(g) >= 40 and not R.forbidden_hits(g) for g in goals.values())


def test_a_background_import_binds_the_one_table_added_between_retained_discoveries(tmp_path):
    """Sol 0373385b item 2: a 202 import's acceptance has no physical identity; completed_import's discovery does."""
    http, _ = controller([event()])
    ctx = ctx_for(tmp_path, http)
    records = tmp_path / 'services' / 'corpus' / 'P10' / 'export' / 'controller-records'

    def record(step, body):
        (records / f'{step}.json').write_text(json.dumps({'bodyBase64': base64.b64encode(json.dumps(body).encode()).decode()}))
    record('import-flood-zones', {'jobId': 'j1', 'statusUrl': '/api/v1/admin/import/jobs/j1'})
    record('before-tables-flood-zones', {'tables': [{'schema': 'honua_data', 'table': 'arc10_x_parcels'}]})
    record('complete-flood-zones-tables-after', {'tables': [{'schema': 'honua_data', 'table': 'arc10_x_parcels'},
                                                            {'schema': 'honua_data', 'table': 'arc10_x_flood_zones_2'}]})
    handoff = R.studio_services_handoff(ctx, cases(), 'town-A', START, END)
    assert handoff['tables']['flood_zones'] == {'schema': 'honua_data', 'table': 'arc10_x_flood_zones_2'}
    record('complete-flood-zones-tables-after', {'tables': [{'schema': 'honua_data', 'table': 'arc10_x_parcels'}]})
    with pytest.raises(ValueError, match='does not bind one new table'):
        R.studio_services_handoff(ctx, cases(), 'town-A', START, END)


class SlowReadback(PublishingServices):
    """Case work passes; the controller readback is slow (past the stage deadline) and then fails."""
    def observe(self, ctx, row):
        return FakeStage.observe(self, ctx, row)

    def harness_readback(self, ctx, observations):
        time.sleep(0.3)
        raise TimeoutError('controller slow')

    def readback_done(self, result):
        self.studio_handoff = result

    def readback_failed(self, reason):
        self.studio_handoff = {'status': 'failed', 'reason': reason}

    def harness_context(self, ctx):
        return {'studioHandoff': getattr(self, 'studio_handoff', None)}


def test_slow_controller_readback_is_harness_undetermined_never_charged_to_the_arm(tmp_path, monkeypatch):
    """Sol 0373385b item 1: readback runs on its own budget after the stage; the case assertions are kept."""
    arc = json.loads(json.dumps(ARC))
    monkeypatch.setattr(R, 'HANDOFF_READBACK_SECONDS', 5)
    services = SlowReadback()
    studio = SeesHandoff()
    receipt, _ = run(tmp_path, mods=modules(services=services, studio=studio), arc=arc)
    row = receipt['stages'][2]
    assert row['outcome'] == 'undetermined' and 'handoff readback failed' in row['cause']
    assert all(a['status'] == 'pass' for a in row['assertions'])
    assert receipt['stages'][3]['outcome'] == 'blocked' and studio.seen == 'unset'


def test_a_readback_that_overruns_its_own_budget_is_recorded_as_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(R, 'HANDOFF_READBACK_SECONDS', 0.05)

    class Hangs(SlowReadback):
        def harness_readback(self, ctx, observations):
            time.sleep(2)
    services = Hangs()
    receipt, _ = run(tmp_path, mods=modules(services=services))
    assert receipt['stages'][2]['outcome'] == 'undetermined'
    assert 'readback did not complete' in services.studio_handoff['reason']


def test_a_readback_that_succeeds_after_its_timeout_never_turns_the_failure_into_a_pass(tmp_path, monkeypatch):
    """Sol 1861983b item 1: the late worker returns a verified handoff, but only the runner commits results."""
    monkeypatch.setattr(R, 'HANDOFF_READBACK_SECONDS', 0.05)
    late = threading.Event()

    class LateSuccess(SlowReadback):
        def harness_readback(self, ctx, observations):
            time.sleep(0.4)
            late.set()
            return {'status': 'verified', 'handoff': {'schema': R.STUDIO_HANDOFF_SCHEMA, 'layers': {}}}
    services, studio = LateSuccess(), SeesHandoff()
    receipt, _ = run(tmp_path, mods=modules(services=services, studio=studio))
    late.wait(2)
    assert receipt['stages'][2]['outcome'] == 'undetermined' and studio.seen == 'unset'
    assert services.studio_handoff['status'] == 'failed'

