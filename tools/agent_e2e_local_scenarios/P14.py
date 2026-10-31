"""Approved public map: independent fixture with separate approval deferred.

Blocked until core can bind a model-created proposal to a complete, reviewed
inventory and a fresh native read. The proposal list is active-only and cannot
establish completeness after approval. Source data depends on server #6815.
"""
from __future__ import annotations

from agent_e2e_local_api import Scenario
from agent_e2e_local_projections_w2g4 import (observe_partial, observe_native_proposal,
    role_bindings, seed_saved_map, source_and_custody)


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
        # Proposal IDs are minted by the server. The current core exposes
        # captured action results only after all sessions, and the active-only
        # proposal list is not a reviewed inventory. Do not approve an
        # unbound proposal or infer its ID from model prose.
        return None

    def observe(self, stack, seeded, variant, run=None):
        source_and_custody(stack, seeded)
        observe_native_proposal(stack, seeded, 'p14')
        return observe_partial(self.id, stack, seeded, variant)


scenario = P14()
