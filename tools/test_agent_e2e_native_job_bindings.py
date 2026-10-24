"""Job identity replay uses actual HTTP originals and the original action journal."""
import copy
import json
from datetime import datetime, timezone

import pytest

from agent_e2e_native_job_bindings import PROFILE, observation, retain, replay
from agent_e2e_native_bindings import validate_case, resolve, export
from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_semantics import response_evidence
from agent_e2e_gate import oracle_predicates
from test_agent_e2e_job_observer import native_job
from test_agent_e2e_scenario_backend import adapter
from test_agent_e2e_setup_http import server


@pytest.fixture
def job(native_job, adapter):
    context = {**adapter.http.context, 'campaignMode': 'qualification',
        'startedAt': native_job.binding['notBefore'], 'completedAt': datetime.now(timezone.utc).isoformat()}
    attempt = canonical_digest({key: context[key] for key in ('campaignId', 'caseId', 'attempt')})
    adapter.durable._fixture_identity = lambda _: attempt
    native_job.dispatch['actionId'] = canonical_digest({**{key: context[key] for key in
        ('campaignId', 'caseId', 'attempt', 'componentBindingDigest')}, 'ordinal': native_job.dispatch['ordinal']})[7:]
    selection = native_job.select()
    native_job.succeed()
    result = native_job.observer.results(selection, step='verification')
    event = {'event': 'action', 'action': selection['ordinal'], 'kind': 'mcp_call_tool',
        'requestDigest': canonical_digest(selection['request']), 'resultDigest': canonical_digest(selection['publicResult']), 'isError': False}
    adapter.durable.state['journals'] = {attempt: [event]}
    context.update(modelActions=[event], completedAt=datetime.now(timezone.utc).isoformat())
    declaration = {'profile': PROFILE, 'fields': ['jobId'], 'relationships': {
        'processId': 'analytics.buffer-aggregate-managed', 'requesterPrincipalId': native_job.binding['requesterPrincipalId']},
        'observers': {'1': {'endpoint': adapter.http.client.endpoint,
            'attemptId': adapter.http.identity['attemptId'], 'credentialKind': 'api-key'}}}
    references = [selection['initial']['reference'], result['nativeReferences']['completed']]
    report = retain(adapter.durable, context, name='buffer', declaration=declaration, responses=references,
        selection=selection, started_at=context['startedAt'], completed_at=context['completedAt'])
    return {'context': context, 'declaration': declaration, 'report': report, 'durable': adapter.durable,
        'references': references, 'selection': selection, 'expected': {'resourceId': {'$native': 'buffer.jobId'},
            'terminalState': 'Succeeded', 'requiredStates': ['Running', 'Succeeded']}}


def test_native_job_id_resolves_from_exact_original_action_and_independent_lifecycle(job):
    case = {'assertions': {'job.completed': job['expected']}, 'cleanup': {'resourceIds': []},
        'nativeBindings': {'buffer': job['declaration']}}
    validate_case(case)
    expected = resolve('job.completed', job['expected'], case['nativeBindings'], {'buffer': job['report']}, job['context'])
    assert expected['resourceId'] == 'gp-actual-job'
    assert expected['requiredStates'] == ['Running', 'Succeeded']
    actual = observation(job['report'], job['declaration'], job['context'])
    oracle_predicates.verify('job.completed', actual, expected)
    emitted = []
    exported = export(job['durable'], job['context'], case['nativeBindings'], lambda name, value: emitted.append((name, value)) or name)
    assert exported == {'buffer': 'native-binding-buffer-completed'}
    assert emitted[0][1] == job['report']


@pytest.mark.parametrize('assertion,field', [('buffer.metric','sourceId'), ('map.composition','mapId'),
    ('state.saved','versionId'), ('job.completed','terminalState')])
def test_job_binding_cannot_replace_geometry_source_or_other_assertion_expectations(job, assertion, field):
    with pytest.raises(PackError):
        validate_case({'assertions': {assertion: {field: {'$native': 'buffer.jobId'}}},
            'cleanup': {'resourceIds': []}, 'nativeBindings': {'buffer': job['declaration']}})


@pytest.mark.parametrize('change', ['request', 'result', 'missing-journal', 'other-action', 'foreign-requester',
    'foreign-candidate', 'foreign-job', 'foreign-observer', 'backward-time', 'repeat-original', 'terminal-twice', 'synthesized-state'])
def test_rehashed_or_relabelled_report_cannot_invent_a_successful_job_lifecycle(job, change):
    report, declaration, context = (copy.deepcopy(job[key]) for key in ('report', 'declaration', 'context'))
    if change == 'request': report['selection']['request']['input']['arguments']['plan']['steps'][0]['inputs']['distance'] = '501'
    elif change == 'result': report['selection']['publicResult']['content']['structuredContent']['jobId'] = 'other'
    elif change == 'missing-journal': context['modelActions'] = []
    elif change == 'other-action': report['selection']['actionId'] = 'f'*64
    elif change == 'foreign-candidate': report['context']['componentBindingDigest'] = 'sha256:'+'f'*64
    elif change == 'foreign-observer':
        declaration['observers']['1']['attemptId'] = 'another-attempt'
        report['contractDigest'] = canonical_digest(declaration)
    elif change == 'repeat-original': report['records'] = [report['records'][1]]*2
    elif change == 'terminal-twice':
        report['records'][0]['response']['response']['body']['data'] = copy.deepcopy(report['records'][1]['response']['response']['body']['data'])
    else:
        row = report['records'][1]['response']['response']['body']['data']
        if change == 'foreign-requester': row['requestedBy'] = 'another-owner'
        elif change == 'foreign-job': row['jobId'] = 'another-job'
        elif change == 'backward-time': row['updatedAt'] = '2000-01-01T00:00:00Z'
        else: row['status'] = 'Completed'
    # A changed native response must remain internally consistent. Refusal must
    # enforce the original action/relationship, not merely spot stale raw bytes.
    for pair in report['records']:
        body = pair['response']['response']['body']
        pair['response']['response'] = response_evidence(200, json.dumps(body).encode())
    with pytest.raises((PackError, ValueError)):
        replay(report, declaration, context)


def test_binding_does_not_fill_a_missed_required_state(job):
    expected = resolve('job.completed', job['expected'], {'buffer': job['declaration']}, {'buffer': job['report']}, job['context'])
    expected['requiredStates'] = ['Queued', 'Running', 'Succeeded']
    with pytest.raises(ValueError, match='transition absent'):
        oracle_predicates.verify('job.completed', observation(job['report'], job['declaration'], job['context']), expected)


def test_retention_requires_original_durable_response_and_cannot_rebind(job):
    arguments = dict(name='buffer', declaration=job['declaration'], responses=job['references'],
        selection=job['selection'], started_at=job['context']['startedAt'], completed_at=job['context']['completedAt'])
    with pytest.raises(PackError, match='already retained'):
        retain(job['durable'], job['context'], **arguments)
    arguments['name'] = 'another'
    arguments['responses'] = copy.deepcopy(job['references'])
    arguments['responses'][0]['sha256'] = 'sha256:'+'f'*64
    with pytest.raises(PackError, match='absent or changed'):
        retain(job['durable'], job['context'], **arguments)


def test_lost_lease_cannot_export_retained_job_observation(job):
    job['durable'].alive = False
    with pytest.raises(PackError, match='lease lost'):
        export(job['durable'], job['context'], {'buffer': job['declaration']}, lambda *_: pytest.fail('must not export'))
