"""P02 remains unsupported pending independent evidence for all three assertions.

Core must supply a bounded scan of task artifacts, logs and final answers against
private canaries and credential fingerprints, with retained artifact digests. It
must bind model-created configuration digests and datasource identities to trusted
pre-restart records. The current restart helper records changed container start
times, but not the stopped state required by the lifecycle oracle. The inventory
reads below are partial evidence only and do not prove task success.
"""
from agent_e2e_local_api import Scenario, Seeded
from agent_e2e_scenario_operations import body


class P02(Scenario):
    id = 'P02'
    supported_variants = ()

    def seed(self, stack, variant):
        if variant not in ('A', 'B'):
            raise ValueError('unsupported development variant')
        if 'p02Seed' not in stack.extra:
            response = stack.extra['recorder'].json('p02-connections-seed', 'GET', '/api/v1/admin/connections/')
            body(response)
            stack.extra['p02Seed'] = {'variant': variant}
        if stack.extra['p02Seed']['variant'] != variant:
            raise ValueError('case already seeded for a different variant')
        return Seeded(stack.extra['p02Seed'].copy())

    def observe(self, stack, seeded, variant, run=None):
        if seeded.state.get('variant') != variant:
            raise ValueError('seed variant differs')
        recorder = stack.extra['recorder']
        body(recorder.json('p02-connections-before-restart', 'GET', '/api/v1/admin/connections/'))
        fact = stack.extra['restartServer']('p02')
        if fact.get('restarted') is not True:
            raise RuntimeError('controller restart did not complete')
        body(recorder.json('p02-connections-after-restart', 'GET', '/api/v1/admin/connections/'))
        raise RuntimeError('P02 unsupported: inventory alone cannot prove persistence or secret absence')


scenario = P02()
