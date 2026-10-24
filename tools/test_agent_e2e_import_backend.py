"""Native task/submission originals drive the shared import session predicates."""
import copy
import json
from pathlib import Path

import pytest

import agent_e2e_import_backend as backend
from agent_e2e_pack import PackError, canonical_digest, digest_bytes
from test_agent_e2e_import_observer import imported, CREATE
from test_agent_e2e_scenario_backend import adapter
from test_agent_e2e_setup_http import server


@pytest.fixture
def session(imported, adapter, tmp_path):
    value = backend.Session.__new__(backend.Session)
    value.durable, value.bundle = adapter.durable, tmp_path
    value.context = {**adapter.http.context, 'campaignMode': 'qualification'}
    value.key = canonical_digest(backend.identity(value.context))
    value.campaign = {key: value.context[key] for key in ('campaignId', 'campaignMode', 'componentBindingDigest')}
    value.case = {'scenarioId': 'P04', 'assertions': ['features.exact', 'geometry.crs', 'source-content.unchanged']}
    value.config = {'import': {'destinations': copy.deepcopy(imported.observer.binding['destinations'])}}
    value.observer_for_recovery = lambda: imported.observer
    snapshot = {'resourceId': 'original-source-file', 'content': {'sha256': 'a'*64, 'bytes': 4096}}
    value.source = lambda: [copy.deepcopy(snapshot)]
    value.retain('source-before', [snapshot])
    features = [{'type': 'Feature', 'id': 'p1', 'properties': {'source_id': 'p1'},
        'geometry': {'type': 'Polygon', 'coordinates': [[[0,0],[0,10],[10,0],[0,0]]]}}]
    expected = {'features.exact': {'srid': 3857, 'features': features, 'tolerance': 0},
        'geometry.crs': {'sourceSrid': 4326, 'returnedSrid': 3857, 'coordinateDigest': canonical_digest([
            {'sourceId': row['id'], 'geometry': row['geometry']} for row in features])[7:]},
        'source-content.unchanged': {'profile': 'honua.protected-content/v1', 'resources': [snapshot]}}
    value.expectations = expected
    return value


def verify(value):
    (value.bundle/'expectations.json').write_text(json.dumps({'cases': {value.context['caseId']: {'assertions': value.expectations}}}))
    return value.verify()


def submitted(session, imported):
    created = imported.execute(CREATE, imported.create)
    session.retain('import-ordinal-1', created)
    selected = imported.submit()
    session.retain('import-ordinal-2', selected)


def test_completed_native_import_drives_exact_features_crs_and_preservation(session, imported):
    submitted(session, imported)
    imported.complete()
    assert verify(session) == {'status': 'pass', 'failedAssertions': []}
    assert session.value('completed-imports')[0]['submission']['job']['output']['physicalTableName'] == 'actual_provider_physical_table'
    assert session.value('verification')['unsupportedAssertions'] == []


def test_queued_or_prepared_native_work_never_succeeds_from_model_claim(session, imported):
    submitted(session, imported)
    session.retain('model-result-3', {'agentResult': {'content': {'status': 'completed', 'featureCount': 1}}})
    assert verify(session) == {'status': 'fail', 'failedAssertions': ['features.exact', 'geometry.crs']}
    assert session.value('completed-imports') is None


def test_missing_csv_invalid_row_report_stays_an_explicit_failure(session, imported):
    session.case['assertions'].append('csv.invalid-reported')
    session.expectations['csv.invalid-reported'] = {'validSourceIds': ['p1'], 'invalidSourceIds': ['bad1'], 'reportedReasons': ['out-of-bounds']}
    submitted(session, imported)
    imported.complete()
    assert verify(session)['failedAssertions'] == ['csv.invalid-reported']
    assert session.value('verification')['unsupportedAssertions'] == ['csv.invalid-reported']


def test_owner_handoff_is_forbidden_until_task_worker_stop_is_independently_observed(session):
    with pytest.raises(PackError, match='independently stopped'):
        session.prepare_cleanup_ownership()


def test_primary_case_cannot_drop_flood_input_or_substitute_same_projection():
    root = Path(__file__).resolve().parents[1]/'certification/agent-e2e/corpus'
    sources, destinations = [], []
    for name in ('parcels.4326.geojson', 'flood_zones.3857.geojson'):
        raw = (root/'town-A'/name).read_bytes()
        content = {'sha256': digest_bytes(raw)[7:], 'bytes': len(raw)}
        sources.append({'resourceId': 'town-A/'+name, 'content': content})
        destinations.append({'fileName': name, 'sourceSha256': content['sha256'], 'totalBytes': content['bytes']})
    case = {'scenarioId': 'P04', 'variant': 'A'}
    backend.primary_sources(case, root, destinations, sources)
    with pytest.raises(PackError, match='every authored primary dataset'):
        backend.primary_sources(case, root, destinations[:1], sources)
    wrong = [destinations[0], {**destinations[1], 'fileName': 'flood_zones.4326.geojson'}]
    with pytest.raises(PackError, match='every authored primary dataset'):
        backend.primary_sources(case, root, wrong, sources)


@pytest.mark.parametrize('mode,case', [('qualification', 'P04:A:aws-ecs-redis-on'),
    ('development', 'P04:C:aws-ecs-redis-on'), ('development', 'P04:A:aws-eks'),
    ('development', 'P06:A:aws-ecs-redis-on'), ('development', 'B03:A:aws-batch')])
def test_import_adapter_cannot_claim_other_targets_heldout_or_full_campaign(tmp_path, mode, case):
    with pytest.raises(PackError, match='only explicit A/B ECS development'):
        backend.Session(durable=None, campaign={'campaignMode': mode}, bundle=tmp_path,
                        configuration={}, context={'caseId': case})
