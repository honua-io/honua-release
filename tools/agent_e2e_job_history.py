"""Validate native store-written transitions; never reconstruct missed states."""
from __future__ import annotations

import copy

from agent_e2e_pack import PackError
from agent_e2e_studio_observer import instant

STATUSES = {'Queued', 'Provisioning', 'Running', 'Succeeded', 'Failed', 'Cancelled'}
TERMINAL = {'Succeeded', 'Failed', 'Cancelled'}
FIELDS = {'version', 'statusTransitions', 'statusTransitionsTruncated'}
EVENT_FIELDS = {'jobId', 'version', 'attemptCount', 'occurredAt', 'fromStatus', 'toStatus'}
MAX_VERSION = 9007199254740991


def require(condition, reason):
    if not condition:
        raise PackError(reason)


def transitions(row, *, not_before, observed_at):
    """Return complete native history, or None for an older/legacy empty row.

    Redis TIME orders history independently of application createdAt/updatedAt.
    Same-millisecond events and version gaps are legitimate. The observer's
    original attempt window bounds history without assuming those clocks equal.
    """
    present = FIELDS.intersection(row)
    if not present:
        return None
    require(present == FIELDS and type(row['version']) is int and 1 <= row['version'] <= MAX_VERSION and
            type(row.get('attemptCount')) is int and row['attemptCount'] >= 0 and
            row['statusTransitionsTruncated'] is False and isinstance(row['statusTransitions'], list) and
            len(row['statusTransitions']) <= 256,
            'native job history is partial, truncated or has invalid version metadata')
    history = row['statusTransitions']
    previous, last_time = None, None
    for event in history:
        require(isinstance(event, dict) and set(event) == EVENT_FIELDS and event['jobId'] == row['jobId'] and
                type(event['version']) is int and 1 <= event['version'] <= row['version'] and
                type(event['attemptCount']) is int and 0 <= event['attemptCount'] <= row['attemptCount'] and
                event['toStatus'] in STATUSES and (event['fromStatus'] is None or event['fromStatus'] in STATUSES),
                'native transition differs from its actual job/version/attempt/status')
        occurred = instant(event['occurredAt'])
        require(instant(not_before) <= occurred <= observed_at,
                'native transition lies outside the original observed attempt window')
        if previous is None:
            require(event['version'] == 1 and event['fromStatus'] is None,
                    'native history lacks its actual initial write; no backfilled prefix')
        else:
            require(event['version'] > previous['version'] and event['attemptCount'] >= previous['attemptCount'] and
                    event['fromStatus'] == previous['toStatus'] and event['toStatus'] != previous['toStatus'] and
                    previous['toStatus'] not in TERMINAL and occurred >= last_time,
                    'native history is reordered, discontinuous or changes a terminal job')
        previous, last_time = event, occurred
    if history:
        require(history[-1]['toStatus'] == row['status'], 'native history does not end at the observed current status')
        return copy.deepcopy(history)
    return None


def advance(previous, current, *, previous_version, current_version):
    """A later native GET must preserve every previously observed transition."""
    require(current_version >= previous_version and len(current) >= len(previous) and
            current[:len(previous)] == previous and
            all(event['version'] > previous_version for event in current[len(previous):]) and
            (current_version > previous_version or current == previous),
            'native job history changed or regressed between independent reads')
