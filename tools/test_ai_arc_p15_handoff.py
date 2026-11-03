"""Rehearsal of record 41722009358 (A2): P15's source read serves the typed boolean is_open as GeoServices 0/1
(#6815's typed columns), so the frozen boolean expectation failed on type alone (A25); and the agent was never told
which saved map to share or the operators' audience (A26)."""
import hashlib
import json

import pytest

import agent_e2e_local_projections as projections
from agent_e2e_local_expectations import load_expected, load_projections
from agent_e2e_local_recorder import Record

SVC, LAYER = 'agent-service', 2
PATH = ('/rest/services/agent-service/FeatureServer/2/query?f={f}&where=1%3D1&outFields=source_id%2Cname%2Ccapacity'
        '%2Cis_open&returnGeometry=true&outSR=3857&orderByFields=source_id+ASC&resultRecordCount=1000')


def _record(step, phase, body, f):
    raw = json.dumps(body).encode()
    return Record(step, phase, 'http', 'controller', 'controller-id', 'GET', PATH.format(f=f), 200, raw,
                  'sha256:' + hashlib.sha256(raw).hexdigest())


def _bodies(expected_features, flip=None):
    """The server's two views of the same unchanged rows: GeoServices JSON (booleans as 0/1) and typed GeoJSON."""
    esri = {'geometryType': 'esriGeometryPoint', 'spatialReference': {'wkid': 3857, 'latestWkid': 3857},
            'objectIdFieldName': 'id', 'exceededTransferLimit': False,
            'fields': [{'name': n} for n in ('id', 'source_id', 'name', 'capacity', 'is_open')], 'features': []}
    geo = {'type': 'FeatureCollection', 'features': []}
    for index, feature in enumerate(expected_features, 1):
        props = feature['properties']
        x, y = feature['geometry']['coordinates']
        esri['features'].append({'attributes': {'id': index, 'source_id': props['source_id'], 'name': props['name'],
                                                'capacity': props['capacity'], 'is_open': int(props['is_open'])},
                                 'geometry': {'x': x, 'y': y}})
        typed = dict(props)
        if flip == props['source_id']:
            typed['is_open'] = not typed['is_open']
        geo['features'].append({'type': 'Feature', 'properties': typed,
                                'geometry': {'type': 'Point', 'coordinates': [x, y]}})
    return esri, geo


def _score(records):
    from agent_e2e_local_api import Observation
    from agent_e2e_local_runner import score_assertions
    expected = {'source-content.unchanged': load_expected('P15', 'A', 'arc')['source-content.unchanged']}
    frozen = {'source-content.unchanged': load_projections('P15', 'A', 'arc')['source-content.unchanged']}
    roles = {'seeded:service:shelters': SVC, 'seeded:layer:shelters': LAYER}
    return score_assertions(['source-content.unchanged'],
                            [Observation('source-content.unchanged', None, {'projection': 'extract/v1'})],
                            expected, records=records, roles=roles, scenario_id='P15', frozen=frozen)[0]


def _records(flip_after=None, geojson=True):
    features = load_expected('P15', 'A', 'arc')['source-content.unchanged']['resources'][0]['content']['features']
    esri, geo = _bodies(features)
    _, geo_after = _bodies(features, flip=flip_after)
    records = {'p15-source-before': _record('p15-source-before', 'seed', esri, 'json'),
               'p15-source-after': _record('p15-source-after', 'observe', esri, 'json')}
    if geojson:
        records['p15-source-before-geojson'] = _record('p15-source-before-geojson', 'seed', geo, 'geojson')
        records['p15-source-after-geojson'] = _record('p15-source-after-geojson', 'observe', geo_after, 'geojson')
    return records


def test_unchanged_source_rows_pass_when_the_typed_booleans_cross_check():
    row = _score(_records())
    assert row['status'] == 'pass', row


def test_a_boolean_that_disagrees_with_its_typed_view_fails():
    row = _score(_records(flip_after='s3'))
    assert row['status'] == 'fail' and 'boolean feature value differs' in row['reason'], row


def test_without_the_typed_companion_reads_the_check_fails_closed():
    row = _score(_records(geojson=False))
    assert row['status'] == 'fail', row


def test_the_map_note_names_the_saved_version_and_the_audience_for_every_case():
    import ai_arc_services as S
    seeded = {'itemId': '11111111-2222-3333-4444-555555555555', 'versionId': '66666666-7777-8888-9999-000000000000',
              'contentHash': 'sha256:' + 'a' * 64}
    note = S.customer_map_note(seeded)
    assert seeded['itemId'] in note and seeded['versionId'] in note and seeded['contentHash'] in note
    assert 'visibility `team`' in note and 'operators are the team audience' in note
    assert 'honua_' not in note                                 # it names no tool
    for bad in ({**seeded, 'itemId': 'x'}, {k: v for k, v in seeded.items() if k != 'contentHash'},
                {**seeded, 'contentHash': 'a b'}):
        with pytest.raises(ValueError):
            S.customer_map_note(bad)


def test_stage_four_names_the_same_operators_audience():
    """A26: stage four's 'share it with our operators' needs the same audience term as P15; one wording for both."""
    import inspect
    import agent_e2e_studio_arc as studio
    import ai_arc_services as S
    assert S.MAP_NOTE.endswith(studio.AUDIENCE_NOTE)
    assert "'\\n' + AUDIENCE_NOTE" in inspect.getsource(studio)


def test_an_absent_probe_step_names_why():
    from agent_e2e_local_runner import MISSING_STEP_REASONS
    assert 'honua_studio_propose_publication' in MISSING_STEP_REASONS['p15-operator']
