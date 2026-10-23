"""Resolve only a job ID from the original model action and independent GETs.

Geometry, source identity and metric expectations stay frozen independently.
Consecutive reads of the same terminal state do not establish a transition.
"""
from __future__ import annotations

import copy
import re
from urllib.parse import urlsplit

from agent_e2e_job_observer import PROCESS, detail, identifier, selected_plan
from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_studio_observer import instant

PROFILE = 'honua.managed-job-bindings/v1'
ALLOWED = {('job.completed', 'resourceId'): 'jobId'}
IDENTITY = ('campaignId', 'caseId', 'attempt', 'campaignMode', 'componentBindingDigest')


def require(condition, reason):
    if not condition:
        raise PackError(reason)


def contract(value):
    require(isinstance(value, dict) and set(value) == {'profile', 'fields', 'observers', 'relationships'} and
            value['profile'] == PROFILE and value['fields'] == ['jobId'], 'invalid finite managed-job binding contract')
    relation = value['relationships']
    require(isinstance(relation, dict) and set(relation) == {'processId', 'requesterPrincipalId'} and
            relation['processId'] == PROCESS and isinstance(relation['requesterPrincipalId'], str) and
            0 < len(relation['requesterPrincipalId']) <= 512,
            'managed job must freeze its reviewed process and exact native requester')
    observers = value['observers']
    require(isinstance(observers, dict) and set(observers) in ({'1'}, {'1', '2'}), 'job observers require exact attempt numbers')
    for observer in observers.values():
        require(isinstance(observer, dict) and set(observer) == {'endpoint', 'attemptId', 'credentialKind'},
                'missing independent job observer identity')
        origin = urlsplit(observer['endpoint']); origin.port
        require(origin.hostname and not origin.username and not origin.password and not origin.query and
                not origin.fragment and origin.path == '' and all(33 <= ord(c) <= 126 for c in observer['endpoint']) and
                not any(c in observer['endpoint'] for c in ('?', '#', '\\')) and
                (origin.scheme == 'https' or origin.scheme == 'http' and origin.hostname in ('localhost', '127.0.0.1', '::1')) and
                observer['credentialKind'] in ('api-key', 'bearer') and isinstance(observer['attemptId'], str) and
                re.fullmatch(r'[A-Za-z0-9-]{1,80}', observer['attemptId']), 'invalid job observer origin/credential/attempt')
    require(len({row['attemptId'] for row in observers.values()}) == len(observers), 'job retry observer identity is reused')


def observation(report, declaration, context):
    from agent_e2e_native_bindings import original
    contract(declaration)
    require(isinstance(report, dict) and set(report) == {'schemaVersion', 'profile', 'name', 'context', 'contractDigest',
            'stage', 'records', 'selection', 'values'} and type(report['schemaVersion']) is int and
            report['schemaVersion'] == 1 and report['profile'] == PROFILE and report['stage'] == 'completed' and
            report['contractDigest'] == canonical_digest(declaration) and
            report['context'] == {key: context[key] for key in IDENTITY} and
            str(context['attempt']) in declaration['observers'], 'job report differs from original frozen candidate/attempt/contract')
    records, selection = report['records'], report['selection']
    require(isinstance(records, list) and 2 <= len(records) <= 82 and isinstance(selection, dict) and
            set(selection) == {'ordinal', 'actionId', 'request', 'publicResult'} and
            type(selection['ordinal']) is int and selection['ordinal'] > 0, 'job report lacks bounded original action/read sequence')
    request, public = selection['request'], selection['publicResult']
    require(isinstance(request, dict) and set(request) == {'name', 'input'} and
            isinstance(public, dict) and set(public) == {'content', 'isError', 'errorCategory'} and
            public['isError'] is False and public['errorCategory'] is None,
            'job identity cannot come from a model claim or failed operation')
    fingerprint = selected_plan(request)
    expected_action = canonical_digest({**{key: context[key] for key in ('campaignId', 'caseId', 'attempt', 'componentBindingDigest')},
        'ordinal': selection['ordinal']})[7:]
    matched = [row for row in context.get('modelActions', []) if row.get('event') == 'action' and row.get('action') == selection['ordinal']]
    require(selection['actionId'] == expected_action and len(matched) == 1 and matched[0].get('kind') == 'mcp_call_tool' and
            matched[0].get('requestDigest') == canonical_digest(request) and matched[0].get('resultDigest') == canonical_digest(public) and
            matched[0].get('isError') is False, 'native job was not the same successful original model action in the authenticated transcript')
    content = public['content']
    require(isinstance(content, dict) and content.get('isError', False) is False and
            isinstance(content.get('structuredContent'), dict), 'model execution lacks its native structured job result')
    result = content['structuredContent']
    job = identifier(result.get('jobId'))
    require(result.get('resourceUri') == 'honua://jobs/'+job, 'native execute resource belongs to another job')
    selected = {'jobId': job, 'requestFingerprint': fingerprint, 'createdAt': result.get('createdAt')}
    binding = {'requesterPrincipalId': declaration['relationships']['requesterPrincipalId'], 'notBefore': context['startedAt']}
    observer = declaration['observers'][str(context['attempt'])]
    states, last_time, steps = [], None, set()
    for pair in records:
        value = original(pair, observer=observer, candidate=context['componentBindingDigest'], path='/api/v1/admin/jobs/'+job)
        row = detail(value, selection=selected, binding=binding, observed_at=instant(context['completedAt']))
        updated = instant(row['updatedAt'])
        step = pair['intent']['step']
        require(step not in steps and (last_time is None or last_time <= updated),
                'job read sequence reuses an original or moves native time backwards')
        steps.add(step); last_time = updated
        if not states or states[-1] != row['status']:
            require(not states or states[-1] not in ('Succeeded', 'Failed', 'Cancelled'), 'job changed after a terminal observation')
            states.append(row['status'])
    require(states[-1] == 'Succeeded' and len(states) >= 2, 'completed job lacks independently observed lifecycle transition')
    require(report['values'] == {'jobId': job}, 'bound job identity differs from original native reads')
    return {'resourceId': job, 'states': states}


def replay(report, declaration, context, *, complete=True):
    return {'jobId': observation(report, declaration, context)['resourceId']}


def retain(durable, context, *, name, declaration, responses, selection, started_at, completed_at):
    """Retain authenticated native originals; never construct a synthetic state."""
    attempt = durable._fixture_identity(context)
    require(isinstance(name, str) and re.fullmatch(r'[a-z][a-z0-9-]{0,40}', name) and
            isinstance(responses, list) and 2 <= len(responses) <= 82, 'invalid bounded job observation references')
    records = []
    for reference in responses:
        require(isinstance(reference, dict) and set(reference) == {'key', 'sha256'}, 'job originals require retained response references')
        response = durable.state.get('scenarioOperations', {}).get(reference['key'])
        require(isinstance(response, dict) and canonical_digest(response) == reference['sha256'], 'native job response is absent or changed')
        intent_ref = response.get('intentReference', {})
        intent = durable.state.get('scenarioOperations', {}).get(intent_ref.get('key'))
        require(isinstance(intent, dict) and canonical_digest(intent) == intent_ref.get('sha256'), 'native job intent is absent or changed')
        records.append({'intent': copy.deepcopy(intent), 'response': copy.deepcopy(response)})
    report = {'schemaVersion': 1, 'profile': PROFILE, 'name': name, 'stage': 'completed',
        'context': {key: context[key] for key in IDENTITY}, 'contractDigest': canonical_digest(declaration),
        'records': records, 'selection': {key: copy.deepcopy(selection[key]) for key in ('ordinal', 'actionId', 'request', 'publicResult')},
        'values': {'jobId': selection['jobId']}}
    replay_context = {**context, 'startedAt': started_at, 'completedAt': completed_at,
        'modelActions': [row for row in durable.state.get('journals', {}).get(attempt, []) if row.get('event') == 'action']}
    replay(report, declaration, replay_context)
    key = attempt+'/'+name+'/completed'
    require(key not in durable.state.setdefault('nativeBindings', {}), 'original job binding already retained; no rebinding')
    durable.state['nativeBindings'][key] = copy.deepcopy(report)
    durable._persist()
    return report
