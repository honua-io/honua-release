"""P21 is unsupported: the local runtime rejects all fault plans.

It needs a single 503 after a committed map create, independently bound to the
commit and recorded as fired, plus a distinct publication approver. The current
runtime has no way to replace the response of a committed map-create request.
"""
from agent_e2e_local_api import Scenario


class P21(Scenario):
    id = 'P21'
    supported_variants = ()

    def seed(self, stack, variant):
        raise NotImplementedError('P21 requires a post-commit map-create fault')

    def fault_plan(self, variant):
        if variant not in ('A', 'B'):
            raise ValueError('unsupported variant')
        # This plan documents the available hook, but cannot implement the pack's
        # after-commit-before-response fault: the runner intercepts before forwarding.
        return {'kind': 'http_503_once', 'id': 'p21-map-create',
                'after': {'tool': 'honua_create_map'},
                'match': {'tool': 'honua_create_map'}}

    def needs_approver(self):
        return True

    def observe(self, stack, seeded, variant, run=None):
        raise NotImplementedError('P21 requires independently bound fault and recovery evidence')

    def expected(self, variant):
        raise NotImplementedError('P21 has no complete independent expectations')


scenario = P21()
