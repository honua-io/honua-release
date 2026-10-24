"""Finite saved IDs replay actual worker MCP + paired native HTTP originals."""
import copy
import json
from datetime import datetime, timezone

import pytest

from agent_e2e_native_authoring_bindings import PROFILE, retain, replay
from agent_e2e_native_bindings import validate_case, resolve, export
from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_gate import oracle_predicates
from agent_e2e_semantics import response_evidence
from test_agent_e2e_studio_composition_observer import authoring, CREATE, SAVE
from test_agent_e2e_scenario_backend import adapter
from test_agent_e2e_setup_http import server


@pytest.fixture
def saved(authoring, adapter):
    context = {**adapter.http.context, 'campaignMode': 'qualification', 'startedAt': authoring.binding['notBefore'],
               'completedAt': datetime.now(timezone.utc).isoformat()}
    attempt = canonical_digest({key: context[key] for key in ('campaignId', 'caseId', 'attempt')})
    adapter.durable._fixture_identity = lambda _: attempt
    declaration = {'profile': PROFILE, 'fields': ['savedVersionId'], 'relationships': {
        **{key: authoring.binding[key] for key in ('itemId', 'family', 'namespace', 'ownerPrincipalId')},
        'startingVersionId': None, 'minimumVersionNumber': 1},
        'observers': {'1': {'endpoint': adapter.http.client.endpoint, 'attemptId': adapter.http.identity['attemptId'], 'credentialKind': 'api-key'}}}
    # This desired content digest is fixed BEFORE model actions and never filled
    # from a candidate response. Only the genuinely random saved ID is dynamic.
    expected = {'resourceId': declaration['relationships']['itemId'], 'versionId': {'$native': 'save.savedVersionId'},
                'documentSha256': canonical_digest(authoring.create_args['body'])[7:]}
    authoring.execute(CREATE, authoring.create_args)
    selected = authoring.execute(SAVE, {'draftId': authoring.state.draft['draftId'], 'generation': 1})
    context['completedAt'] = datetime.now(timezone.utc).isoformat()
    context['modelActions'] = adapter.durable.state['journals'][attempt]
    references = {'saveHandle': selected['nativeReferences']['handle'], 'savedVersion': selected['nativeReferences']['version'],
                  'currentItem': selected['nativeReferences']['current']}
    report = retain(adapter.durable, context, name='save', declaration=declaration, responses=references, selection=selected,
                    started_at=context['startedAt'], completed_at=context['completedAt'])
    return {'context': context, 'attempt': attempt, 'declaration': declaration, 'report': report, 'expected': expected,
            'selected': selected, 'references': references, 'durable': adapter.durable}


def test_native_saved_id_resolves_only_from_same_original_model_and_exact_native_version(saved, authoring):
    value = saved
    case = {'assertions': {'state.saved': value['expected']}, 'cleanup': {'resourceIds': []},
            'nativeBindings': {'save': value['declaration']}}
    validate_case(case)
    expected = resolve('state.saved', value['expected'], case['nativeBindings'], {'save': value['report']}, value['context'])
    assert expected['versionId'] == authoring.state.current
    assert expected['documentSha256'] == value['expected']['documentSha256']
    oracle_predicates.verify('state.saved', authoring.observer.saved(value['selected']['resource']), expected)
    artifacts = {}
    def evidence(name, body):
        artifacts[name] = body
        return {'path': name, 'sha256': canonical_digest(body)}
    assert set(export(value['durable'], value['context'], case['nativeBindings'], evidence)) == {'save'}
    assert artifacts['native-binding-save-saved'] == value['report']


def test_runtime_identity_cannot_change_frozen_body_style_or_geometry_expectations(saved, authoring):
    wanted = copy.deepcopy(saved['expected'])
    wanted['documentSha256'] = 'e'*64
    resolved = resolve('state.saved', wanted, {'save': saved['declaration']}, {'save': saved['report']}, saved['context'])
    assert resolved['documentSha256'] == 'e'*64
    with pytest.raises(ValueError): oracle_predicates.verify('state.saved', authoring.observer.saved(saved['selected']['resource']), resolved)
    for assertion, field in [('map.style', 'styleDocumentSha256'), ('geometry.crs', 'coordinateDigest'), ('render.actual', 'layerIds')]:
        with pytest.raises(PackError):
            validate_case({'assertions': {assertion: {field: {'$native': 'save.savedVersionId'}}}, 'cleanup': {'resourceIds': []},
                           'nativeBindings': {'save': saved['declaration']}})


@pytest.mark.parametrize('change', ['different-action', 'different-result', 'missing-transcript', 'foreign-owner',
    'old-version', 'later-current', 'reused-starting-version', 'cross-attempt'])
def test_rehashed_native_report_cannot_substitute_unrelated_later_or_old_save(saved, change):
    report, declaration, context = (copy.deepcopy(saved[key]) for key in ('report', 'declaration', 'context'))
    if change == 'different-action': report['selection']['ordinal'] += 1
    elif change == 'different-result': report['selection']['publicResult']['content']['structuredContent']['version']['envelope']['body']['title'] = 'not-the-original-model-result'
    elif change == 'missing-transcript': context['modelActions'] = []
    elif change == 'cross-attempt': context['attempt'] = 2
    elif change == 'reused-starting-version':
        declaration['relationships']['startingVersionId'] = saved['selected']['resource']['versionId']
        declaration['relationships']['minimumVersionNumber'] = 2
        report['contractDigest'] = canonical_digest(declaration)
    else:
        key = 'currentItem' if change == 'later-current' else 'savedVersion'
        pair = report['records'][key]
        body = pair['response']['response']['body'].get('data', pair['response']['response']['body'])
        if change == 'foreign-owner': body['ownerId'] = 'unrelated-owner'
        elif change == 'old-version': body['createdAt'] = '2020-01-01T00:00:00+00:00'
        else: body['items'][0]['currentVersionId'] = '00000000-0000-0000-0000-000000000099'
        if change in ('foreign-owner', 'old-version'):
            version = report['selection']['publicResult']['content']['structuredContent']['version']
            version.update(ownerId=body['ownerId']) if change == 'foreign-owner' else version.update(createdAt=body['createdAt'])
            selected = next(row for row in context['modelActions'] if row['action'] == report['selection']['ordinal'])
            selected['resultDigest'] = canonical_digest(report['selection']['publicResult'])
        pair['response']['response'] = response_evidence(200, json.dumps(pair['response']['response']['body']).encode())
    with pytest.raises(PackError): replay(report, declaration, context)


def test_original_binding_cannot_be_rebound_after_a_later_save(saved, authoring):
    authoring.state.draft['envelope']['body']['title'] = 'Later unrelated edit'
    later = authoring.execute(SAVE, {'draftId': authoring.state.draft['draftId'], 'generation': authoring.state.draft['generation']})
    references = {**saved['references'], 'currentItem': later['nativeReferences']['current']}
    with pytest.raises(PackError, match='later/unrelated'):
        retain(saved['durable'], saved['context'], name='another-save', declaration=saved['declaration'], responses=references,
               selection=saved['selected'], started_at=saved['context']['startedAt'], completed_at=datetime.now(timezone.utc).isoformat())
