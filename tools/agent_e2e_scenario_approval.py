"""One approval POST by a separate principal. The POST is not publication.

`agent_e2e_scenario_operations` still never approves. This adapter is the only
local-docker caller of the proposal approve route, and only with a credential
that differs from the requester. Callers must then read the proposal back.
"""
from __future__ import annotations

import os

from agent_e2e_pack import PackError
from agent_e2e_scenario_operations import token


def require(value, reason):
    if not value:
        raise PackError(reason)


def approve_once(*, requester_credential_env, approver_credential_env, approver_http, pending, requested_by):
    require(isinstance(requester_credential_env, str) and isinstance(approver_credential_env, str) and
            requester_credential_env != approver_credential_env,
            'approval requires a separate credential reference')
    requester_secret = os.environ.get(requester_credential_env)
    approver_secret = os.environ.get(approver_credential_env)
    require(isinstance(requester_secret, str) and requester_secret and isinstance(approver_secret, str) and
            approver_secret and requester_secret != approver_secret,
            'approval credential is missing or identical to the requester')
    require(isinstance(pending, dict) and pending.get('status') == 'awaiting-approval',
            'approval requires a retained pending proposal')
    proposal_id = token(pending.get('proposalId'), 'publication proposal')
    require(isinstance(requested_by, str) and requested_by, 'approval requires the observed requester principal')
    answer = approver_http.json('approve-'+proposal_id, 'POST', '/api/v1/admin/proposals/'+proposal_id+'/approve')
    observation = answer['observation']
    require(observation['httpStatus'] in (200, 202), 'separate approval was not accepted')
    body = observation.get('body')
    if isinstance(body, dict) and isinstance(body.get('resolvedBy'), str):
        require(body['resolvedBy'] and body['resolvedBy'] != requested_by, 'approver is the requester')
    return {'qualification': False, 'proposalId': proposal_id, 'requestedBy': requested_by,
            'reference': answer['reference'], 'httpStatus': observation['httpStatus']}
