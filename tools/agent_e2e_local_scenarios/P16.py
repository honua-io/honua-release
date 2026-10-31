"""Blocked by the credential boundary: recorder principals are runner-minted seed credentials; there is no runner hook to consume a model-created operation secret and register the same issued key for before/after probes. Controller metadata reads cannot prove issued-key access. Frozen secret-scan artifact identities also have no binding to the runner artifact inventory.
"""
from __future__ import annotations

REASON = 'Blocked by the credential boundary: recorder principals are runner-minted seed credentials; there is no runner hook to consume a model-created operation secret and register the same issued key for before/after probes. Controller metadata reads cannot prove issued-key access. Frozen secret-scan artifact identities also have no binding to the runner artifact inventory.'

from agent_e2e_local_api import Scenario as BaseScenario
from agent_e2e_local_projections_w2g4 import observe_partial, role_bindings


class LocalScenario(BaseScenario):
    id = "P16"
    supported_variants = ()

    def seed(self, stack, variant):
        self.expected(variant)  # validate the development variant before touching the stack
        raise NotImplementedError("P16: " + REASON)

    def needs_approver(self):
        return False

    def roles(self, stack, seeded, variant, run=None):
        return role_bindings(self.id, seeded)

    def observe(self, stack, seeded, variant, run=None):
        return observe_partial(self.id, stack, seeded, variant)

    def cleanup(self, stack, seeded):
        if seeded.state:
            raise ValueError("Unsupported scenario cannot claim cleanup ownership")


scenario = LocalScenario()
