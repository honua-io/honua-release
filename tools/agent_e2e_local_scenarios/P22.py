"""P22 is unsupported pending independently observed saved-map reuse.

The runner now supplies fresh-session roles; this case still needs a seeded
saved map, frozen expectations and projections for reuse, duplicate absence,
public publication, unchanged source and final-answer facts.
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
