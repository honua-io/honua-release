"""P11 remains unsupported until local job-to-output and saved-map evidence is bound.

The runner needs a post-seed agent-owned job locator, a frozen projection for
the persisted buffer artifact and an independently verified map/output binding.
The frozen town feature projections use typed attributes, so they depend on
honua-server #6815. A fixture buffer alone cannot prove the requested saved result was made.
"""
from pathlib import Path

from agent_e2e_local_api import Observation, RoleBinding, Scenario, Seeded

ROOT = Path(__file__).resolve().parents[2]


class P11(Scenario):
    id = 'P11'
    supported_variants = ()

    def seed(self, stack, variant):
        from agent_e2e_local_scenarios.w2g3_common import read_town, town_setup
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
        read_town(recorder, 'p11', town['layers'], 'before')
        seed = {'variant': variant, 'service': binding['service'], 'layerId': binding['layerId'], 'layers': town['layers']}
        stack.extra['p11Seed'] = seed
        return Seeded(seed)

    def roles(self, stack, seeded, variant, run=None):
        return [RoleBinding(f'seeded:{kind}:{layer}', f'publish-{layer.replace("_", "-")}-submit',
                            pointer=f'/data/resourceIds/{field}')
                for layer in ('parcels', 'flood_zones', 'shelters')
                for kind, field in (('service', 'serviceName'), ('layer', 'layerId'))]

    def observe(self, stack, seeded, variant, run=None):
        from agent_e2e_local_scenarios.w2g3_common import read_town
        read_town(stack.extra['recorder'], 'p11', seeded.state['layers'], 'after')
        return [Observation('source-content.unchanged', None, {'projection': 'extract/v1'})]


scenario = P11()
