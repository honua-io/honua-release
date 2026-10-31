"""P01 is unsupported: the runner lacks bootstrap and task worker custody.

It records bootstrap credential revocation, but not independent bootstrap worker
identity and termination, task worker identity, and task-worker provisioning
credential inventory. The runner starts the stack before the task; the pack owner
must decide how task installation is measured in this arrangement.
"""
from agent_e2e_local_api import Scenario


class P01(Scenario):
    id = 'P01'
    supported_variants = ()

    def seed(self, stack, variant):
        raise RuntimeError('P01 unsupported: worker custody unavailable')

    def observe(self, stack, seeded, variant, run=None):
        raise RuntimeError('P01 unsupported: worker custody unavailable')


scenario = P01()
