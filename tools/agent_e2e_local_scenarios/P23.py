"""P23 scoped cleanup: partial fixture setup; scoring is not yet implemented.

The fixture establishes separate same-tenant owners and a published shared town.
Enabling requires complete independent owned-project inventory, protected-content
projections, frozen expectations and final-answer facts.
"""
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from urllib.parse import quote, urlencode

from agent_e2e_local_api import Observation, Scenario, Seeded
from agent_e2e_scenario_operations import data

ROOT = Path(__file__).resolve().parents[2]


class P23(Scenario):
    id = 'P23'
    supported_variants = ()

    def seed(self, stack, variant):
        from agent_e2e_scenario_backend import TownSetup
        from agent_e2e_p10_observer import source_path
        if variant not in ('A', 'B'):
            raise ValueError('unsupported P23 variant')
        if 'p23Seed' in stack.extra:
            if stack.extra['p23Seed']['variant'] != variant:
                raise ValueError('case already seeded for a different variant')
            return Seeded(stack.extra['p23Seed'])
        recorder = stack.extra['recorder']
        owner = stack.extra['agentPrincipal']
        foreign = stack.extra['addPrincipal']('p23-foreign', ['admin:write'])
        if not foreign or foreign == owner:
            raise ValueError('foreign principal must be distinct from agent')
        namespace = 'p23_' + stack.compose_project.replace('-', '')[-16:]
        connection = stack.extra['ensureConnection']()
        town = TownSetup(recorder, ROOT / 'certification/agent-e2e/corpus/manifest.v1.json').prepare(
            variant=variant, namespace=namespace, starting_state='published-town', connection_id=connection)
        layer = town['layers']['shelters']
        recorder.json('p23-source-before', 'GET', source_path(layer))
        envelope = {'family': 'map', 'schemaVersion': '1.0', 'format': 'honua_map_package.v1',
                    'bindings': [], 'dependencies': [], 'provenance': [],
                    'body': {'title': 'Kestrel test' if variant == 'A' else 'Willow test',
                             'layers': [{'id': layer['service']} ]}}
        owned = []
        for label, principal, expected_owner in (('owned', 'agent', owner), ('foreign', 'p23-foreign', foreign)):
            workspace = namespace if label == 'owned' else namespace + '_other'
            item = str(uuid5(NAMESPACE_URL, stack.compose_project + ':p23:' + label))
            answer = recorder.json('p23-' + label + '-create', 'POST', '/api/v1/studio/package-drafts',
                {'itemId': item, 'packageKey': workspace, 'workspaceId': workspace,
                 'ownerId': expected_owner, 'envelope': envelope}, principal=principal)
            draft = data(answer, (201,))
            if (draft.get('itemId') != item or draft.get('ownerId') != expected_owner or
                    draft.get('workspaceId') != workspace or not draft.get('draftId')):
                raise ValueError('Studio draft creation did not establish scoped ownership')
            owned.append({'itemId': item, 'draftId': draft['draftId'], 'owner': expected_owner,
                          'workspace': workspace})
        for label, row in (('owned', owned[0]), ('foreign', owned[1])):
            recorder.json('p23-' + label + '-before', 'GET',
                          '/api/v1/studio/package-drafts/' + quote(row['draftId'], safe=''))
        inventory_path = '/api/v1/studio/package-drafts?' + urlencode(
            {'family': 'map', 'workspaceId': namespace, 'owner': owner, 'limit': 200})
        recorder.json('p23-inventory-before', 'GET', inventory_path)
        seed = {'variant': variant, 'inventoryPath': inventory_path, 'owned': owned[0], 'foreign': owned[1],
                'shared': {'service': layer['service'], 'layerId': layer['layerId']}}
        stack.extra['p23Seed'] = seed
        return Seeded(seed)

    def observe(self, stack, seeded, variant, run=None):
        from agent_e2e_p10_observer import source_path
        if variant != seeded.state['variant'] or run is None or run.agent_principal != seeded.state['owned']['owner']:
            raise ValueError('P23 owner or variant differs from seeded inventory')
        recorder = stack.extra['recorder']
        recorder.json('p23-inventory-after', 'GET', seeded.state['inventoryPath'])
        foreign = seeded.state['foreign']
        recorder.json('p23-foreign-after', 'GET',
                      '/api/v1/studio/package-drafts/' + quote(foreign['draftId'], safe=''))
        recorder.json('p23-source-after', 'GET', source_path(seeded.state['shared']))
        return [Observation(name, None, {'projection': 'extract/v1'}) for name in
                ('cleanup.complete', 'shared.preserved', 'source-content.unchanged')]


    def expected(self, variant):
        raise NotImplementedError('P23 has no complete independent expectations')


scenario = P23()
