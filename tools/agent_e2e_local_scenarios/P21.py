"""P21 is unsupported pending independent map and publication observations.

The post-commit hook is armed for the real Studio draft-create tool. A fired
fault alone does not prove the upstream operation committed: require its
successful captured response and a controller read of the committed map, plus
independent recovery, duplicate, publication and source observations.
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
        return {'kind': 'response_lost_after_commit', 'id': 'p21-map-create',
                'after': None, 'match': {'tool': 'honua_studio_create_draft'},
                'commit': {'pointer': '/result/structuredContent/draftId'}}

    def needs_approver(self):
        return True

    def observe(self, stack, seeded, variant, run=None):
        raise NotImplementedError('P21 requires independently bound fault and recovery evidence')

    def expected(self, variant):
        raise NotImplementedError('P21 has no complete independent expectations')


scenario = P21()
