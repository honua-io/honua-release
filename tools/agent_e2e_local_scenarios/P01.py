"""P01 is unsupported: candidate provenance and deployment identity are unavailable.

The runner supplies a candidate digest, but the candidate.identity oracle also
requires server source and OCI digests and client artifact digests. It does not
provide those bound provenance facts or a controller-observed deployment identity.
Bootstrap credential revocation is recorded; worker replacement is preregistered
as excluded on local Docker. The runner starts the stack before the task, so the
pack owner must decide how task installation is measured in this arrangement.
"""
from agent_e2e_local_api import Scenario


class P01(Scenario):
    id = 'P01'
    supported_variants = ()

    def seed(self, stack, variant):
        raise RuntimeError('P01 unsupported: candidate provenance and deployment identity unavailable')

    def observe(self, stack, seeded, variant, run=None):
        raise RuntimeError('P01 unsupported: candidate provenance and deployment identity unavailable')


scenario = P01()
