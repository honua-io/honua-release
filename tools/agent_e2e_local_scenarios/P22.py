"""P22 is unsupported: fresh session identities cannot be frozen for the oracle.

The runner creates session IDs at run time, but the oracle requires both IDs
in the frozen expectation and logical roles can bind only recorded resource
reads, not session facts. A seeded saved map and publication readback are also
needed; session count alone cannot prove state reuse.
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
