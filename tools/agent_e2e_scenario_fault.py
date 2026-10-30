"""Arm one controller fault after a retained commit. No second injection.

The pack fault is a single HTTP 503 on map create, after commit and before the
response. This adapter persists that intent before calling the already deployed
local injector. It does not invent the product's 503 body.
"""
from __future__ import annotations

import copy

from agent_e2e_pack import PackError, canonical_digest


def require(value, reason):
    if not value:
        raise PackError(reason)


EXPECTED = {'kind': 'http-transient', 'status': 503, 'count': 1, 'operationClass': 'map-create',
            'trigger': 'after-commit-before-response', 'injectionActor': 'controller',
            'requireCommittedResourceObservation': True}


def arm(*, durable, context, fault, commit, injector):
    require(context.get('caseId', '').endswith(':local-docker'), 'fault adapter is local-docker only')
    require(fault == EXPECTED, 'fault differs from the reviewed map-create contract')
    require(isinstance(commit, dict) and isinstance(commit.get('resourceId'), str) and commit['resourceId'] and
            commit.get('committed') is True, 'fault injection requires an observed committed resource')
    identity = {key: context[key] for key in ('campaignId', 'caseId', 'attempt')}
    key = canonical_digest({**identity, 'operationClass': 'map-create'})
    durable._require_live_lease()
    injections = durable.state.setdefault('faultInjections', {})
    require(key not in injections, 'fault already armed; observe the retained injection')
    record = {**identity, 'status': 503, 'count': 1, 'resourceId': commit['resourceId'], 'armed': True,
              'injectionId': None}
    injections[key] = record
    durable._persist()
    observed = injector(copy.deepcopy(record))
    require(isinstance(observed, dict) and isinstance(observed.get('injectionId'), str) and observed['injectionId'] and
            observed.get('status') == 503 and observed.get('count') == 1,
            'fault injector did not retain the single 503')
    record['injectionId'] = observed['injectionId']
    durable._persist()
    return {'qualification': False, 'injectionId': observed['injectionId'], 'targetOperationId': commit['resourceId'],
            'observedFailure': {'status': 503, 'count': 1}}
