"""Controlled history replay, not native execution qualification."""
import copy
from datetime import timedelta
import json
from pathlib import Path

import pytest

from agent_e2e_job_history import transitions
from agent_e2e_native_job_bindings import observation
from agent_e2e_pack import PackError
from agent_e2e_semantics import response_evidence
from agent_e2e_studio_observer import instant
from test_agent_e2e_native_job_bindings import job
from test_agent_e2e_job_observer import native_job
from test_agent_e2e_scenario_backend import adapter
from test_agent_e2e_setup_http import server

NATIVE = json.loads((Path(__file__).parent/'fixtures'/'native-job-status-history.json').read_text())


@pytest.mark.parametrize('vector', NATIVE['vectors'], ids=lambda value: value['name'])
def test_actual_native_http_history_shape_without_claiming_gp_execution(vector):
    row = vector['observation']
    before = instant(row['statusTransitions'][0]['occurredAt'])-timedelta(seconds=1)
    after = instant(row['statusTransitions'][-1]['occurredAt'])+timedelta(seconds=1)
    actual = transitions(row, not_before=before.isoformat(), observed_at=after)
    assert actual == row['statusTransitions']
    assert [event['toStatus'] for event in actual] == ['Queued', 'Provisioning', 'Running', 'Succeeded']
    assert vector['nativeKind'] == 'FileImport'


def rehash(report):
    for pair in report['records']:
        value = pair['response']['response']['body']
        pair['response']['response'] = response_evidence(200, json.dumps(value).encode())


@pytest.fixture
def history_job(job):
    report = copy.deepcopy(job['report'])
    first = report['records'][0]['response']['response']['body']['data']
    last = report['records'][1]['response']['response']['body']['data']
    occurred = instant(first['createdAt']) + timedelta(milliseconds=1)
    history = []
    previous = None
    # Version gaps reflect non-status writes; equal Redis milliseconds are valid.
    for version, status in ((1, 'Queued'), (3, 'Provisioning'), (5, 'Running'), (9, 'Succeeded')):
        history.append({'jobId': first['jobId'], 'version': version, 'attemptCount': 1,
            'occurredAt': occurred.isoformat(), 'fromStatus': previous, 'toStatus': status})
        previous = status
    first.update(version=7, attemptCount=1, statusTransitions=copy.deepcopy(history[:3]), statusTransitionsTruncated=False)
    last.update(version=11, attemptCount=1, statusTransitions=history, statusTransitionsTruncated=False)
    rehash(report)
    return {**job, 'report': report}


def replay(value):
    return observation(value['report'], value['declaration'], value['context'])


def test_native_history_preserves_version_gaps_provisioning_and_same_millisecond_order(history_job):
    assert replay(history_job)['states'] == ['Queued', 'Provisioning', 'Running', 'Succeeded']


def test_fast_completion_uses_actual_history_even_when_every_get_is_terminal(history_job):
    report = history_job['report']
    report['records'][0]['response']['response']['body']['data'] = copy.deepcopy(
        report['records'][1]['response']['response']['body']['data'])
    rehash(report)
    assert replay(history_job)['states'] == ['Queued', 'Provisioning', 'Running', 'Succeeded']


@pytest.mark.parametrize('change', ['truncated', 'missing-truncated', 'missing-version', 'boolean-version',
    'too-large-version', 'foreign-job', 'missing-initial', 'fake-initial', 'duplicate-version', 'future-version',
    'discontinuous', 'repeated-status', 'unknown-status', 'changed-terminal', 'backward-clock',
    'before-attempt', 'after-observation', 'changed-prefix', 'lower-current-version', 'empty-later',
    'future-attempt', 'backward-attempt', 'current-state-mismatch'])
def test_consistently_rehashed_invalid_history_cannot_supply_missed_states(history_job, change):
    report = history_job['report']
    row = report['records'][1]['response']['response']['body']['data']
    events = row['statusTransitions']
    if change == 'truncated': row['statusTransitionsTruncated'] = True
    elif change == 'missing-truncated': del row['statusTransitionsTruncated']
    elif change == 'missing-version': del row['version']
    elif change == 'boolean-version': row['version'] = True
    elif change == 'too-large-version': row['version'] = 9007199254740992
    elif change == 'foreign-job': events[0]['jobId'] = 'another-job'
    elif change == 'missing-initial': del events[0]
    elif change == 'fake-initial': events[0]['fromStatus'] = 'Queued'
    elif change == 'duplicate-version': events[1]['version'] = events[0]['version']
    elif change == 'future-version': events[-1]['version'] = row['version']+1
    elif change == 'discontinuous': events[2]['fromStatus'] = 'Queued'
    elif change == 'repeated-status': events[1]['toStatus'] = 'Queued'
    elif change == 'unknown-status': events[1]['toStatus'] = 'Retrying'
    elif change == 'changed-terminal':
        events[1]['toStatus'] = events[2]['fromStatus'] = 'Failed'
    elif change == 'backward-clock':
        events[1]['occurredAt'] = (instant(events[0]['occurredAt'])-timedelta(milliseconds=1)).isoformat()
    elif change == 'before-attempt': events[0]['occurredAt'] = '2000-01-01T00:00:00Z'
    elif change == 'after-observation': events[-1]['occurredAt'] = '2099-01-01T00:00:00Z'
    elif change == 'changed-prefix':
        events[0]['occurredAt'] = (instant(events[0]['occurredAt'])-timedelta(milliseconds=1)).isoformat()
    elif change == 'lower-current-version': row['version'] = 6
    elif change == 'empty-later': row['statusTransitions'] = []
    elif change == 'future-attempt': events[-1]['attemptCount'] = 2
    elif change == 'backward-attempt': events[-1]['attemptCount'] = 0
    elif change == 'current-state-mismatch': events[-1]['toStatus'] = 'Failed'
    rehash(report)
    with pytest.raises((PackError, ValueError)):
        replay(history_job)


def test_legacy_empty_history_does_not_backfill_queued_or_running(history_job):
    for pair in history_job['report']['records']:
        row = pair['response']['response']['body']['data']
        row['statusTransitions'] = []
    rehash(history_job['report'])
    assert replay(history_job)['states'] == ['Running', 'Succeeded']
    first = history_job['report']['records'][0]['response']['response']['body']['data']
    first.update(status='Succeeded', completedAt=first['updatedAt'])
    rehash(history_job['report'])
    with pytest.raises(PackError, match='lifecycle transition'):
        replay(history_job)


def test_native_clock_is_not_reconstructed_from_application_timestamps(history_job):
    row = copy.deepcopy(history_job['report']['records'][1]['response']['response']['body']['data'])
    # This unit exercises the history parser only: ordinary detail validation
    # independently rejects application timestamps outside the attempt.
    row['createdAt'] = row['updatedAt'] = '2099-01-01T00:00:00Z'
    result = transitions(row, not_before=history_job['context']['startedAt'],
        observed_at=instant(history_job['context']['completedAt']))
    assert result == row['statusTransitions']
