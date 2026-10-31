"""P24 is unsupported: independent worker custody and map evidence are missing.

The runner needs bootstrap worker identity and termination, task worker identity
and provisioning credential inventory, a fresh-session map read,
and independently decoded browser render evidence. It also starts the stack before
the task, so installation attribution requires a pack-owner decision.
"""
from agent_e2e_local_api import Scenario


class P24(Scenario):
    id = 'P24'
    supported_variants = ()

    def needs_approver(self):
        return True

    def seed(self, stack, variant):
        raise RuntimeError('P24 unsupported: worker custody and map evidence unavailable')

    def observe(self, stack, seeded, variant, run=None):
        raise RuntimeError('P24 unsupported: worker custody and map evidence unavailable')


scenario = P24()
