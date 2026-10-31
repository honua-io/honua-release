"""P24 is unsupported: candidate provenance and independent map evidence are missing.

The runner lacks bound server/client candidate provenance, a fresh-session map
read, and independently decoded browser render evidence. Worker replacement is
preregistered as excluded on local Docker. Attribute filtering depends
on the typed-field import fix in honua-server #6815. It also starts the stack before
the task, so installation attribution requires a pack-owner decision.
"""
from agent_e2e_local_api import Scenario


class P24(Scenario):
    id = 'P24'
    supported_variants = ()

    def needs_approver(self):
        return True

    def seed(self, stack, variant):
        raise RuntimeError('P24 unsupported: candidate provenance and map evidence unavailable')

    def observe(self, stack, seeded, variant, run=None):
        raise RuntimeError('P24 unsupported: candidate provenance and map evidence unavailable')


scenario = P24()
