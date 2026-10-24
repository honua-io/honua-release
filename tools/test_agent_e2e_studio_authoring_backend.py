"""Actual worker-created state drives authoring verification, never fixture success."""
import json

import pytest

import agent_e2e_studio_authoring_backend as backend
from agent_e2e_pack import PackError, canonical_digest
from test_agent_e2e_studio_composition_observer import authoring, CREATE, SAVE
from test_agent_e2e_scenario_backend import adapter
from test_agent_e2e_setup_http import server


@pytest.fixture
def session(authoring, adapter, tmp_path):
    value = backend.Session.__new__(backend.Session)
    value.durable, value.bundle = adapter.durable, tmp_path
    value.context = {**adapter.http.context, 'campaignMode': 'development'}
    value.key = canonical_digest(backend.identity(value.context))
    value.campaign = {key: value.context[key] for key in ('campaignId', 'campaignMode', 'componentBindingDigest')}
    value.case = {'scenarioId': 'P08', 'assertions': ['map.composition', 'render.actual', 'source-content.unchanged']}
    value.observer_for_recovery = lambda: authoring.observer
    snapshot = {'resourceId': 'source-data', 'content': {'features': [{'id': 'immutable-source'}]}}
    value.source = lambda **_: {'snapshots': [snapshot]}
    value.retain('source-before', {'snapshots': [snapshot]})
    expected = {'map.composition': {'mapId': authoring.binding['itemId'], 'orderedLayerIds': ['parcels'],
        'visibility': {'parcels': True}, 'extent': [0, 0, 100, 100]},
        'render.actual': {'layerIds': ['parcels'], 'minimumNonBackgroundPixels': 1, 'regions': []},
        'source-content.unchanged': {'profile': 'honua.protected-content/v1', 'resources': [snapshot]}}
    (tmp_path/'expectations.json').write_text(json.dumps({'cases': {value.context['caseId']: {'assertions': expected}}}))
    return value


def create_and_compose(authoring, session, *, save):
    created = authoring.execute(CREATE, authoring.create_args)
    session.retain('authoring-ordinal-1', created)
    added = authoring.execute('honua_studio_add_layer', {'draftId': created['resource']['draftId'], 'generation': 1,
        'layer': {'id': 'parcels', 'sourceId': 'native-source', 'visible': True}})
    session.retain('authoring-ordinal-2', added)
    viewed = authoring.execute('honua_studio_set_view', {'draftId': created['resource']['draftId'], 'generation': 2,
        'view': {'bbox': [0, 0, 100, 100], 'crs': 'EPSG:3857'}})
    session.retain('authoring-ordinal-3', viewed)
    if save:
        saved = authoring.execute(SAVE, {'draftId': created['resource']['draftId'], 'generation': 3})
        session.retain('authoring-ordinal-4', saved)


def test_actual_model_create_compose_and_save_verifies_composition_but_not_missing_render(session, authoring):
    create_and_compose(authoring, session, save=True)
    assert session.verify() == {'status': 'fail', 'failedAssertions': ['render.actual']}
    observed = session.value('verification')
    assert observed['modelCreateCount'] == observed['modelSaveCount'] == 1
    assert observed['unsupportedAssertions'] == ['render.actual']
    assert observed['observations']['map.composition']['orderedLayerIds'] == ['parcels']
    assert session.value('authoring-final')['resource']['versionId'] == authoring.state.current


def test_model_must_save_its_created_composition_before_it_can_qualify(session, authoring):
    create_and_compose(authoring, session, save=False)
    result = session.verify()
    assert result['failedAssertions'] == ['map.composition', 'render.actual']
    assert session.value('verification')['modelSaveCount'] == 0
    assert session.value('authoring-final') is None


def test_unsaved_edit_after_model_save_cannot_qualify_old_version_as_current_composition(session, authoring):
    create_and_compose(authoring, session, save=True)
    authoring.state.draft['envelope']['body']['layers'][0]['visible'] = False
    assert session.verify()['failedAssertions'] == ['map.composition', 'render.actual']
    assert session.value('authoring-final') is None


def test_session_joins_new_saved_id_to_original_model_action_and_keeps_frozen_content(session, authoring):
    from agent_e2e_native_authoring_bindings import PROFILE
    session.case['assertions'].append('state.saved')
    session.retain('prepare-start', {'startedAt': authoring.binding['notBefore']})
    session.durable._fixture_identity = lambda _: session.key
    path = session.bundle/'expectations.json'
    expected = json.loads(path.read_text())
    case = expected['cases'][session.context['caseId']]
    desired_body = {'title': 'Model selected draft', 'layers': [{'id': 'parcels', 'sourceId': 'native-source', 'visible': True}],
                    'view': {'bbox': [0, 0, 100, 100], 'crs': 'EPSG:3857'}}
    case['assertions']['state.saved'] = {'resourceId': authoring.binding['itemId'], 'versionId': {'$native': 'save.savedVersionId'},
        'documentSha256': canonical_digest(desired_body)[7:]}
    case['nativeBindings'] = {'save': {'profile': PROFILE, 'fields': ['savedVersionId'], 'relationships': {
        **{key: authoring.binding[key] for key in ('itemId', 'family', 'namespace', 'ownerPrincipalId')},
        'startingVersionId': None, 'minimumVersionNumber': 1}, 'observers': {'1': {
            'endpoint': authoring.observer.http.client.endpoint, 'attemptId': authoring.observer.http.identity['attemptId'], 'credentialKind': 'api-key'}}}}
    path.write_text(json.dumps(expected))
    create_and_compose(authoring, session, save=True)
    assert session.verify()['failedAssertions'] == ['render.actual']
    report = session.durable.state['nativeBindings'][session.key+'/save/saved']
    assert report['values']['savedVersionId'] == authoring.state.current
    assert session.value('verification')['observations']['state.saved']['documentSha256'] == case['assertions']['state.saved']['documentSha256']


@pytest.mark.parametrize('mode,case', [('qualification', 'P08:A:aws-ecs-redis-on'),
    ('development', 'P08:C:aws-ecs-redis-on'), ('development', 'P08:A:aws-eks'),
    ('development', 'P04:A:aws-ecs-redis-on'), ('development', 'L03:A:aws-lambda')])
def test_authoring_does_not_claim_unimplemented_targets_or_other_families(tmp_path, mode, case):
    with pytest.raises(PackError, match='only explicit A/B ECS development'):
        backend.Session(durable=None, campaign={'campaignMode': mode}, bundle=tmp_path,
                        configuration={}, context={'caseId': case})
