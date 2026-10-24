"""One new saved-version identity joined to original model and native evidence.

Only state.saved/state.reopened versionId can resolve from this finite profile.
Item, family, owner, namespace and minimum new version are source-frozen. Body,
style, geometry, rendering and content expectations remain independently frozen;
observed content never becomes an expected value.
"""
from __future__ import annotations

import copy
import re
from urllib.parse import urlencode, urlsplit

from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_studio_observer import guid, instant

PROFILE = 'honua.studio-authoring-bindings/v1'
ALLOWED = {('state.saved', 'versionId'): 'savedVersionId', ('state.reopened', 'versionId'): 'savedVersionId'}
IDENTITY = ('campaignId', 'caseId', 'attempt', 'campaignMode', 'componentBindingDigest')
SOURCES = {'saveHandle', 'savedVersion', 'currentItem'}


def require(condition, reason):
    if not condition:
        raise PackError(reason)


def contract(value):
    require(isinstance(value, dict) and set(value) == {'profile', 'fields', 'observers', 'relationships'} and
            value['profile'] == PROFILE and value['fields'] == ['savedVersionId'], 'invalid finite native authoring binding contract')
    relations = value['relationships']
    require(isinstance(relations, dict) and set(relations) == {'itemId', 'family', 'namespace', 'ownerPrincipalId',
        'startingVersionId', 'minimumVersionNumber'} and relations['family'] in ('map', 'dashboard') and
        isinstance(relations['namespace'], str) and re.fullmatch(r'ae_[a-z0-9_]{3,35}', relations['namespace']) and
        isinstance(relations['ownerPrincipalId'], str) and 0 < len(relations['ownerPrincipalId']) <= 512 and
        type(relations['minimumVersionNumber']) is int and relations['minimumVersionNumber'] >= 1,
        'authoring binding must freeze exact item/owner/workspace/family and new-version boundary')
    guid(relations['itemId'])
    if relations['startingVersionId'] is not None:
        guid(relations['startingVersionId'])
        require(relations['minimumVersionNumber'] >= 2, 'a saved starting fixture requires a demonstrably later version')
    observers = value['observers']
    require(isinstance(observers, dict) and set(observers) in ({'1'}, {'1', '2'}), 'authoring observers require exact attempt numbers')
    for observer in observers.values():
        require(isinstance(observer, dict) and set(observer) == {'endpoint', 'attemptId', 'credentialKind'}, 'missing authoring observer identity')
        endpoint = urlsplit(observer['endpoint']); endpoint.port
        require(endpoint.hostname and not endpoint.username and not endpoint.password and not endpoint.query and
            not endpoint.fragment and endpoint.path == '' and all(33 <= ord(char) <= 126 for char in observer['endpoint']) and
            not any(char in observer['endpoint'] for char in ('?', '#', '\\')) and
            (endpoint.scheme == 'https' or endpoint.scheme == 'http' and endpoint.hostname in ('127.0.0.1', 'localhost', '::1')) and
            observer['credentialKind'] in ('api-key', 'bearer') and isinstance(observer['attemptId'], str) and
            re.fullmatch(r'[A-Za-z0-9-]{1,80}', observer['attemptId']), 'invalid authoring observer origin/credential/attempt')
    require(len({row['attemptId'] for row in observers.values()}) == len(observers), 'authoring retries cannot reuse observer identity')


def replay(report, declaration, context, *, complete=True):
    from agent_e2e_native_bindings import original
    contract(declaration)
    require(isinstance(report, dict) and set(report) == {'schemaVersion', 'profile', 'name', 'context', 'contractDigest',
            'stage', 'records', 'selection', 'values'} and report['schemaVersion'] == 1 and report['profile'] == PROFILE and
            report['stage'] == 'saved' and report['contractDigest'] == canonical_digest(declaration) and
            report['context'] == {key: context[key] for key in IDENTITY} and set(report['records']) == SOURCES,
            'authoring report differs from original frozen candidate/attempt/contract')
    observer = declaration['observers'][str(context['attempt'])]
    relations, records, selection = declaration['relationships'], report['records'], report['selection']
    require(isinstance(selection, dict) and set(selection) == {'ordinal', 'actionId', 'request', 'publicResult'} and
            type(selection['ordinal']) is int and selection['ordinal'] > 0, 'authoring report lacks its original selected model action')
    request, public = selection['request'], selection['publicResult']
    require(isinstance(request, dict) and set(request) == {'name', 'input'} and request['name'] == 'mcp_call_tool' and
            set(request['input']) == {'name', 'arguments'} and request['input']['name'] == 'honua_studio_save_version' and
            isinstance(public, dict) and set(public) == {'content', 'isError', 'errorCategory'} and
            public['isError'] is False and public['errorCategory'] is None,
            'authoring identity cannot come from a model claim or a different operation')
    expected_action = canonical_digest({**{key: context[key] for key in ('campaignId', 'caseId', 'attempt', 'componentBindingDigest')},
        'ordinal': selection['ordinal']})[7:]
    actions = context.get('modelActions')
    matched = [row for row in actions or [] if row.get('event') == 'action' and row.get('action') == selection['ordinal']]
    require(selection['actionId'] == expected_action and len(matched) == 1 and matched[0].get('kind') == 'mcp_call_tool' and
            matched[0].get('requestDigest') == canonical_digest(request) and matched[0].get('resultDigest') == canonical_digest(public) and
            matched[0].get('isError') is False, 'native save was not the same original successful model action in the authenticated transcript')
    payload = public['content']
    require(isinstance(payload, dict) and payload.get('isError', False) is False and isinstance(payload.get('structuredContent'), dict),
            'model save lacks its actual native structured result')
    output = payload['structuredContent']
    operation, version_output = output.get('operation'), output.get('version')
    require(isinstance(operation, dict) and isinstance(version_output, dict) and operation.get('status') == 'Completed',
            'model save did not produce an actual completed native version')
    operation_id = operation.get('operationInstanceId')
    require(isinstance(operation_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,200}', operation_id), 'invalid original save handle identity')
    def read(role, path):
        value = original(records[role], observer=observer, candidate=context['componentBindingDigest'], path=path)
        require(isinstance(value, dict), 'native authoring original lacks a resource object')
        return value
    handle = read('saveHandle', '/api/v1/operations/handles/'+operation_id)
    begin, end = instant(context['startedAt']), instant(context['completedAt'])
    require(handle.get('operationInstanceId') == operation_id and handle.get('operationId') == 'studio.draft.save-version' and
            handle.get('status') == 'Completed' and begin <= instant(handle.get('createdAt')) <= end,
            'native save handle is unrelated, pending or outside the original producer attempt')
    version_id = guid(version_output.get('versionId'))
    version = read('savedVersion', '/api/v1/studio/content-items/'+relations['itemId']+'/versions/'+version_id)
    resources = handle.get('resourceIds', {})
    require(version == version_output and version.get('itemId') == resources.get('itemId') == relations['itemId'] and
            resources.get('versionId') == version_id and version.get('ownerId') == relations['ownerPrincipalId'] and
            version.get('createdBy') == relations['ownerPrincipalId'] and
            version.get('workspaceId') == version.get('packageKey') == relations['namespace'] and
            version.get('versionId') != relations['startingVersionId'] and type(version.get('versionNumber')) is int and
            version['versionNumber'] >= relations['minimumVersionNumber'] and
            begin <= instant(version.get('createdAt')) <= end and isinstance(version.get('contentHash'), str) and
            re.fullmatch(r'[0-9a-f]{64}', version['contentHash']) and resources.get('contentHash') == version['contentHash'] and
            isinstance(version.get('envelope'), dict) and version['envelope'].get('family') == relations['family'] and
            version['envelope'].get('schemaVersion') == '1.0' and
            version.get('sourceDraftId') == guid(request['input']['arguments'].get('draftId')),
            'native saved version is not the exact new immutable output of this original model save')
    listing = read('currentItem', '/api/v1/studio/content-items?'+urlencode({'family': relations['family'],
        'workspaceId': relations['namespace'], 'owner': relations['ownerPrincipalId'], 'limit': 100}))
    require(type(listing.get('total')) is int and listing['total'] == 1 and listing.get('nextCursor') is None and
            isinstance(listing.get('items'), list) and len(listing['items']) == 1, 'native current item listing is missing/ambiguous')
    item = listing['items'][0]
    require(item.get('itemId') == relations['itemId'] and item.get('workspaceId') == item.get('packageKey') == relations['namespace'] and
            item.get('ownerId') == relations['ownerPrincipalId'] and item.get('currentVersionId') == version_id and
            item.get('publishedVersionId') is None, 'a later/unrelated/public version replaced the original model save')
    require(report['values'] == {'savedVersionId': version_id}, 'authoring identity differs from original native evidence')
    return copy.deepcopy(report['values'])


def retain(durable, context, *, name, declaration, responses, selection, started_at, completed_at):
    attempt = durable._fixture_identity(context)
    require(isinstance(name, str) and re.fullmatch(r'[a-z][a-z0-9-]{0,40}', name) and set(responses) == SOURCES,
            'native authoring retention requires exact frozen binding name and original GET references')
    records = {}
    for role, reference in responses.items():
        require(isinstance(reference, dict) and set(reference) == {'key', 'sha256'}, 'authoring originals cannot come from model payloads')
        response = durable.state.get('scenarioOperations', {}).get(reference['key'])
        require(isinstance(response, dict) and canonical_digest(response) == reference['sha256'], 'authoring original response is absent or changed')
        intent_ref = response.get('intentReference', {})
        intent = durable.state.get('scenarioOperations', {}).get(intent_ref.get('key'))
        require(isinstance(intent, dict) and canonical_digest(intent) == intent_ref.get('sha256'), 'authoring original intent is absent or changed')
        records[role] = {'intent': copy.deepcopy(intent), 'response': copy.deepcopy(response)}
    public = selection['publicResult']
    version = public['content']['structuredContent']['version']
    report = {'schemaVersion': 1, 'profile': PROFILE, 'name': name,
        'context': {key: context[key] for key in IDENTITY}, 'contractDigest': canonical_digest(declaration), 'stage': 'saved',
        'records': records, 'selection': {key: copy.deepcopy(selection[key]) for key in ('ordinal', 'actionId', 'request', 'publicResult')},
        'values': {'savedVersionId': version['versionId']}}
    replay_context = {**context, 'startedAt': started_at, 'completedAt': completed_at,
        'modelActions': [row for row in durable.state.get('journals', {}).get(attempt, []) if row.get('event') == 'action']}
    replay(report, declaration, replay_context)
    key = attempt+'/'+name+'/saved'
    require(key not in durable.state.setdefault('nativeBindings', {}), 'original saved-version binding already retained; no rebinding')
    durable.state['nativeBindings'][key] = copy.deepcopy(report)
    durable._persist()
    return report
