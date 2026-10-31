"""P22 is unsupported: publication requires a separate approver.

The runtime rejects approver setup and supplies no independently seeded saved
map with a fixture-derived public route or independent final-answer handoff.
Two runner-minted session identities alone cannot prove saved state was reused.
"""
from agent_e2e_local_api import Scenario


class P22(Scenario):
    id = 'P22'
    supported_variants = ()

    def sessions_required(self):
        return 2

    def needs_approver(self):
        return True

    def seed(self, stack, variant):
        raise NotImplementedError('P22 needs an independently established saved map')

    def observe(self, stack, seeded, variant, run=None):
        raise NotImplementedError('P22 needs saved-map and publication readback')

    def expected(self, variant):
        raise NotImplementedError('P22 has no complete independent expectations')


scenario = P22()
