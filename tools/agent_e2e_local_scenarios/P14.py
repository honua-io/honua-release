"""Approved public map: independent fixture and guarded separate approval.

Blocked until core can bind a model-created proposal to a complete, reviewed
inventory and a fresh native read. The proposal list is active-only and cannot
establish completeness after approval. Source data depends on server #6815.
"""
from __future__ import annotations

import re
from agent_e2e_local_api import Scenario
from agent_e2e_local_projections_w2g4 import (observe_partial, observe_native_proposal,
    publication_actions, role_bindings, seed_saved_map, source_and_custody)
from agent_e2e_studio_observer import route_path


class P14(Scenario):
    id = 'P14'
    supported_variants = ()

    def seed(self, stack, variant):
        self.expected(variant)
        return seed_saved_map(stack, variant)

    def needs_approver(self):
        return True

    def sessions_required(self):
        return 2

    def roles(self, stack, seeded, variant, run=None):
        return role_bindings(self.id, seeded)

    def between_sessions(self, stack, seeded, variant, after_session):
        if after_session != 1 or seeded.state['variant'] != variant:
            raise ValueError('approval is restricted to this case after session one')
        rec, state = stack.extra['recorder'], seeded.state
        actions = publication_actions(rec, state)
        if len(actions) != 1 or actions[0].arguments.get('visibility') != 'public':
            return None
        route = actions[0].arguments.get('route')
        route_path(route)  # validate the product route before it can authorize any action
        requester = rec.principals['agent'][1]
        approver = rec.principals.get('approver', (None, None))[1]
        if not requester or not approver or requester == approver:
            raise ValueError('distinct native approval principal unavailable')
        answer = rec.json('p14-pending-list', 'GET', '/api/v1/admin/proposals')
        body = answer['observation']['body']
        if (answer['observation']['httpStatus'] != 200 or not isinstance(body, dict)
                or not isinstance(body.get('proposals'), list) or len(body['proposals']) > 100):
            raise ValueError('bounded pending proposal inventory unavailable')
        expected_diff = [f'{key}={value}' for key, value in (
            ('itemId', state['itemId']), ('versionId', state['versionId']), ('contentHash', state['contentHash']),
            ('route', route), ('visibility', 'public'))]
        matches = []
        for index, row in enumerate(body['proposals']):
            if not isinstance(row, dict) or row.get('requestedBy') != requester or row.get('status') != 'AwaitingApproval':
                continue
            proposal_id = row.get('proposalId')
            if not isinstance(proposal_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', proposal_id):
                raise ValueError('invalid pending proposal identity')
            detail = rec.json(f'p14-pending-{index}', 'GET', '/api/v1/admin/proposals/' + proposal_id)
            value = detail['observation']['body']
            if (detail['observation']['httpStatus'] == 200 and isinstance(value, dict)
                    and value.get('proposalId') == proposal_id and value.get('requestedBy') == requester
                    and value.get('kind') == 'StudioDraftMutation' and value.get('status') == 'AwaitingApproval'
                    and value.get('diff') == expected_diff):
                matches.append(proposal_id)
        if len(matches) == 1:
            stack.extra['approve'](matches[0])
        return None

    def observe(self, stack, seeded, variant, run=None):
        source_and_custody(stack, seeded)
        observe_native_proposal(stack, seeded, 'p14')
        return observe_partial(self.id, stack, seeded, variant)


scenario = P14()
