"""Model-selected MCP mutation and independent HTTP style controls; no live qualification."""
import copy
import json

import pytest

from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_studio_style_observer import observe, project, validate
from test_agent_e2e_studio_composition_observer import authoring, CREATE, SAVE
from test_agent_e2e_studio_authoring_backend import session
from test_agent_e2e_scenario_backend import adapter
from test_agent_e2e_setup_http import server

CONFIG = {'floodLayerId': 'floods', 'shelterLayerId': 'shelters'}
INITIAL = {'layers': [{'id': 'floods', 'sourceId': 'flood-source', 'visible': True},
    {'id': 'shelters', 'sourceId': 'shelter-source', 'visible': True}], 'view': {'bbox': [0, 0, 10, 10]},
    'mapSpec': {'version': 8, 'sources': {'flood-source': {'type': 'geojson'}, 'shelter-source': {'type': 'geojson'}},
        'layers': [{'id': 'floods', 'source': 'flood-source', 'type': 'fill',
                    'paint': {'fill-color': '#ff0000', 'fill-opacity': 1}},
                   {'id': 'shelters', 'source': 'shelter-source', 'type': 'circle', 'paint': {'circle-radius': 8}}]}}
DESIRED = copy.deepcopy(INITIAL)
DESIRED['mapSpec']['layers'][0]['paint'] = {'fill-color': '#0088ff', 'fill-opacity': 0.35}
DESIRED['mapSpec']['layers'].append({'id': 'shelter-names', 'type': 'symbol', 'source': 'shelter-source',
    'metadata': {'honua:compositionLayerId': 'shelters'},
    'layout': {'text-field': ['to-string', ['get', 'name']], 'text-size': 14}, 'paint': {'text-color': '#111111'}})
EXPECTED = {'styleDocumentSha256': canonical_digest(DESIRED['mapSpec'])[7:], 'labelField': 'name',
            'floodColor': '#0088ff', 'floodOpacity': 0.35}


def prepare(authoring, body=INITIAL):
    authoring.execute(CREATE, {**authoring.create_args, 'body': copy.deepcopy(body)})
    return authoring.execute(SAVE, {'draftId': authoring.state.draft['draftId'], 'generation': 1})['resource']


def mutate_and_save(authoring, body=DESIRED):
    edit = authoring.execute('honua_studio_update_draft', {'draftId': authoring.state.draft['draftId'],
        'generation': authoring.state.draft['generation'], 'packageKey': authoring.binding['namespace'],
        'schemaVersion': '1.0', 'body': copy.deepcopy(body)})
    saved = authoring.execute(SAVE, {'draftId': authoring.state.draft['draftId'],
        'generation': authoring.state.draft['generation']})
    return edit, saved


def check(authoring, initial, edit, saved):
    return observe(authoring.observer, config=CONFIG, initial=initial, saved=saved['resource'],
        save_selection=saved, selections=[edit, saved])


def test_real_worker_mcp_edit_save_and_native_http_prove_actual_style_not_reference_metadata(authoring):
    initial = prepare(authoring)
    edit, saved = mutate_and_save(authoring)
    result = check(authoring, initial, edit, saved)
    assert result['actual'] == {'mapId': authoring.binding['itemId'], **EXPECTED}
    assert result['mutationActionId'] == edit['actionId'] and result['saveActionId'] == saved['actionId']
    assert result['mutationReference'] == edit['nativeReferences']['draft']
    import agent_e2e_gate
    agent_e2e_gate.oracle_predicates.verify('map.style', result['actual'], {'mapId': authoring.binding['itemId'], **EXPECTED})


@pytest.mark.parametrize('change', ['no-mutation', 'other-draft', 'wrong-generation', 'wrong-actor',
    'different-save', 'same-version', 'old-version', 'wrong-model-style', 'no-native-reference'])
def test_actual_style_cannot_be_credited_to_missing_foreign_stale_or_unrelated_producer(authoring, change):
    initial = prepare(authoring)
    edit, saved = mutate_and_save(authoring)
    if change == 'no-mutation': edit['tool'] = 'honua_studio_get_draft'
    elif change == 'other-draft': edit['resource']['draftId'] = '99999999-9999-4999-8999-999999999999'
    elif change == 'wrong-generation': edit['resource']['generation'] += 1
    elif change == 'wrong-actor': edit['resource']['updatedBy'] = 'other-actor'
    elif change == 'different-save': saved['tool'] = 'honua_studio_get_version'
    elif change == 'same-version': initial['versionId'] = saved['resource']['versionId']
    elif change == 'old-version': initial['versionNumber'] = saved['resource']['versionNumber']
    elif change == 'wrong-model-style': edit['resource']['envelope']['body']['mapSpec'] = INITIAL['mapSpec']
    elif change == 'no-native-reference': edit['nativeReferences'].pop('draft')
    with pytest.raises(PackError): check(authoring, initial, edit, saved)


def test_style_reference_metadata_alone_cannot_replace_actual_persisted_maplibre_document(authoring):
    initial = prepare(authoring)
    metadata = copy.deepcopy(INITIAL)
    metadata['styleRefs'] = [{'styleId': 'honua-inline:floods', 'body': {'floods': {
        'paint': {'fill-color': '#0088ff', 'fill-opacity': 0.35}}}}]
    edit, saved = mutate_and_save(authoring, metadata)
    with pytest.raises(PackError, match='unchanged prepared style'): check(authoring, initial, edit, saved)


@pytest.mark.parametrize('change', ['color', 'opacity'])
def test_wrong_actual_color_or_opacity_fails_frozen_style_oracle_without_adapting_expected(authoring, change):
    initial = prepare(authoring)
    body = copy.deepcopy(DESIRED)
    body['mapSpec']['layers'][0]['paint']['fill-color' if change == 'color' else 'fill-opacity'] = '#00ff00' if change == 'color' else 0.9
    edit, saved = mutate_and_save(authoring, body)
    result = check(authoring, initial, edit, saved)
    import agent_e2e_gate
    with pytest.raises(ValueError):
        agent_e2e_gate.oracle_predicates.verify('map.style', result['actual'], {'mapId': authoring.binding['itemId'], **EXPECTED})
    assert EXPECTED['styleDocumentSha256'] == canonical_digest(DESIRED['mapSpec'])[7:]


@pytest.mark.parametrize('change', ['literal-name', 'hidden-label', 'invisible-text', 'other-source', 'wrong-owner',
    'duplicate-label', 'bool-opacity', 'missing-opacity'])
def test_missing_hidden_misbound_or_literal_labels_and_nonactual_flood_style_are_not_observed(authoring, change):
    initial = prepare(authoring)
    body = copy.deepcopy(DESIRED)
    label = body['mapSpec']['layers'][-1]
    if change == 'literal-name': label['layout']['text-field'] = 'name'
    elif change == 'hidden-label': label['layout']['visibility'] = 'none'
    elif change == 'invisible-text': label['paint']['text-opacity'] = 0
    elif change == 'other-source': label['source'] = 'flood-source'
    elif change == 'wrong-owner': label['metadata']['honua:compositionLayerId'] = 'floods'
    elif change == 'duplicate-label': body['mapSpec']['layers'].append({**copy.deepcopy(label), 'id': 'other-names'})
    elif change == 'bool-opacity': body['mapSpec']['layers'][0]['paint']['fill-opacity'] = True
    elif change == 'missing-opacity': body['mapSpec']['layers'][0]['paint'].pop('fill-opacity')
    edit, saved = mutate_and_save(authoring, body)
    with pytest.raises(PackError): check(authoring, initial, edit, saved)


def test_unrelated_initial_style_cannot_already_be_the_completed_task(authoring):
    initial = prepare(authoring, DESIRED)
    edit, saved = mutate_and_save(authoring)
    with pytest.raises(PackError, match='unchanged prepared style'): check(authoring, initial, edit, saved)


def test_style_observation_configuration_cannot_select_model_derived_hashes_or_invent_layer_aliases():
    for config in [{'floodLayerId': 'same', 'shelterLayerId': 'same'}, {**CONFIG, 'styleDocumentSha256': 'a'*64}]:
        with pytest.raises(PackError): validate(config)


def test_session_observes_saved_style_but_does_not_substitute_missing_actual_browser_render(session, authoring):
    initial = prepare(authoring)
    session.case = {'scenarioId': 'P09', 'assertions': ['map.style', 'render.actual', 'state.saved', 'source-content.unchanged']}
    session.config = {'styling': CONFIG}
    session.durable._fixture_identity = lambda context: session.key
    session.retain('initial-fixture', {'fixture': {'initialVersion': initial}})
    path = session.bundle/'expectations.json'
    case = json.loads(path.read_text())
    expected = case['cases'][session.context['caseId']]['assertions']
    expected.pop('map.composition')
    expected['map.style'] = {'mapId': authoring.binding['itemId'], **EXPECTED}
    from agent_e2e_native_authoring_bindings import PROFILE
    expected['state.saved'] = {'resourceId': authoring.binding['itemId'],
        'versionId': {'$native': 'save.savedVersionId'}, 'documentSha256': canonical_digest(DESIRED)[7:]}
    case['cases'][session.context['caseId']]['nativeBindings'] = {'save': {
        'profile': PROFILE, 'fields': ['savedVersionId'], 'relationships': {
            **{key: authoring.binding[key] for key in ('itemId', 'family', 'namespace', 'ownerPrincipalId')},
            'startingVersionId': initial['versionId'], 'minimumVersionNumber': initial['versionNumber']+1},
        'observers': {'1': {'endpoint': authoring.observer.http.client.endpoint,
            'attemptId': authoring.observer.http.identity['attemptId'], 'credentialKind': 'api-key'}}}}
    path.write_text(json.dumps(case))
    session.retain('prepare-start', {'startedAt': authoring.binding['notBefore']})
    edit, saved = mutate_and_save(authoring)
    session.retain('authoring-ordinal-3', edit)
    session.retain('authoring-ordinal-4', saved)
    assert session.verify() == {'status': 'fail', 'failedAssertions': ['render.actual']}
    assert session.value('verification')['unsupportedAssertions'] == ['render.actual']
    assert session.value('authoring-style')['actual'] == expected['map.style']


def test_preflight_keeps_authored_style_expected_independent_and_refuses_completed_setup(authoring):
    from agent_e2e_studio_style_observer import preflight
    initial = prepare(authoring)
    wanted = {'mapId': authoring.binding['itemId'], **EXPECTED}
    preflight(config=CONFIG, initial=initial, expected=wanted, item_id=authoring.binding['itemId'])
    completed = copy.deepcopy(initial)
    completed['envelope']['body'] = copy.deepcopy(DESIRED)
    with pytest.raises(PackError, match='already contains the completed style task'):
        preflight(config=CONFIG, initial=completed, expected=wanted, item_id=authoring.binding['itemId'])
    wanted['styleDocumentSha256'] = {'$native': 'candidate-body-hash'}
    with pytest.raises(PackError, match='source-frozen expected content'):
        preflight(config=CONFIG, initial=initial, expected=wanted, item_id=authoring.binding['itemId'])


@pytest.mark.parametrize('body', [None, [], 'claimed-style'])
def test_malformed_native_body_cannot_become_style_evidence(authoring, body):
    from agent_e2e_studio_style_observer import document
    initial = prepare(authoring)
    initial['envelope']['body'] = body
    with pytest.raises(PackError, match='not an observed document'):
        document(initial, CONFIG)
