"""P11 remains unsupported until local job-to-output and saved-map evidence is bound.

The runner needs an assertion-specific projection for the authenticated job and
its persisted artifact, a post-seed agent-owned job locator, and a map/output
binding. A fixture buffer alone cannot prove the requested saved result was made.
"""
from pathlib import Path

from agent_e2e_local_api import Observation, Scenario, Seeded

ROOT = Path(__file__).resolve().parents[2]


class P11(Scenario):
    id = 'P11'
    supported_variants = ()

    def seed(self, stack, variant):
        from agent_e2e_local_scenarios.w2g3_common import town_setup
        from agent_e2e_p10_observer import source_path
        if variant not in ('A', 'B'):
            raise ValueError('unsupported variant')
        if 'p11Seed' in stack.extra:
            if stack.extra['p11Seed']['variant'] != variant:
                raise ValueError('case already seeded for a different variant')
            return Seeded(stack.extra['p11Seed'])
        recorder = stack.extra['recorder']
        namespace = 'p11_' + stack.compose_project.replace('-', '')[-16:]
        if 'connectionId' not in stack.extra:
            stack.extra['connectionId'] = stack.extra['ensureConnection']()
        town = town_setup(recorder, variant).prepare(
            variant=variant, namespace=namespace, starting_state='published-town',
            connection_id=stack.extra['connectionId'])
        binding = town['layers']['shelters']
        recorder.json('p11-shelters-before', 'GET', source_path(binding))
        seed = {'variant': variant, 'service': binding['service'], 'layerId': binding['layerId']}
        stack.extra['p11Seed'] = seed
        return Seeded(seed)

    def observe(self, stack, seeded, variant, run=None):
        from agent_e2e_p10_observer import source_path
        stack.extra['recorder'].json('p11-shelters-after', 'GET', source_path(seeded.state))
        return [Observation('source-content.unchanged', None, {'projection': 'extract/v1', 'spec': {
            'protectedContent': [{'label': 'shelters', 'before': 'p11-shelters-before',
                                  'after': 'p11-shelters-after'}]}})]


scenario = P11()
