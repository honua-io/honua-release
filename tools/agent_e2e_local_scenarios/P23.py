"""P23 is unsupported: the requested scoped project cleanup is not observable.

The reviewed proposal requires another tenant, prohibited by the release rules.
The Studio endpoints expose draft deletion and content lists, but no project
resource or project deletion/readback. A draft-only inventory cannot prove the
project and every owned resource were deleted. This needs a same-tenant foreign
principal fixture, a real owned-project lifecycle and complete controller
inventory/readback (including shared-service and source snapshots).
"""
from agent_e2e_local_api import Scenario


class P23(Scenario):
    id = 'P23'
    supported_variants = ()

    def seed(self, stack, variant):
        raise NotImplementedError('P23 requires an independently inventoried project')

    def observe(self, stack, seeded, variant, run=None):
        raise NotImplementedError('P23 requires complete owned-project readback')

    def expected(self, variant):
        raise NotImplementedError('P23 has no complete independent expectations')


scenario = P23()
