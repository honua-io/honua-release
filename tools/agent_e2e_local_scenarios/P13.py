"""Restore the seeded map while preserving town and shared state.

Frozen observations cover version history, the shared map, service settings,
complete map/service inventories, and all three town layers. Typed feature reads
require honua-server #6815. Inventory baselines fail closed if unexpected
resources already exist on the fresh stack.
"""
from pathlib import Path
from urllib.parse import quote, urlencode

from agent_e2e_local_api import Observation, RoleBinding, Scenario, Seeded
from agent_e2e_scenario_operations import data

ROOT = Path(__file__).resolve().parents[2]


class P13(Scenario):
    id = 'P13'
    supported_variants = ('A', 'B')

    @staticmethod
    def _map(recorder, *, prefix, namespace, owner, principal, town):
        item_id = ('11111111-1111-4111-8111-111111111111' if prefix == 'p13-target'
                   else '22222222-2222-4222-8222-222222222222')
        layers = [{'id': name, 'sourceId': str(town['layers'][name]['service']), 'visible': True}
                  for name in ('parcels', 'flood_zones', 'shelters')]
        layers[2]['style'] = {'type': 'circle', 'paint': {'circle-color': '#315a86'}}
        envelope = {'family': 'map', 'schemaVersion': '1.0', 'format': 'honua_map_package.v1',
                    'bindings': [], 'dependencies': [], 'provenance': [],
                    'body': {'title': 'Town map', 'layers': layers}}
        create = data(recorder.json(prefix + '-create', 'POST', '/api/v1/studio/package-drafts', {
            'itemId': item_id, 'packageKey': namespace, 'workspaceId': namespace,
            'ownerId': owner, 'envelope': envelope}), (201,))
        if create.get('itemId') != item_id or create.get('ownerId') != owner:
            raise ValueError('seed map identity differs')
        draft = create['draftId']
        validation = data(recorder.json(prefix + '-validate', 'POST',
                                        '/api/v1/studio/package-drafts/' + draft + '/validate', {}))
        if validation.get('status') != 'valid' or validation.get('diagnostics') != []:
            raise ValueError('seed map validation failed')
        version = data(recorder.json(prefix + '-save', 'POST',
                                    '/api/v1/studio/package-drafts/' + draft + '/content-versions',
                                    {'changeNote': 'Initial isolated fixture'}), (201,))
        if (version.get('itemId') != item_id or version.get('ownerId') != owner
                or version.get('versionNumber') != 1 or not version.get('versionId')
                or not version.get('contentHash')):
            raise ValueError('seed map version differs')
        path = '/api/v1/studio/content-items?' + urlencode({'family': 'map', 'workspaceId': namespace,
                                                            'owner': owner, 'limit': 100})
        listing = data(recorder.json(prefix + '-before', 'GET', path))
        rows = listing.get('items')
        if (not isinstance(rows, list) or len(rows) != 1 or listing.get('total') != 1
                or listing.get('nextCursor') is not None or rows[0].get('itemId') != item_id
                or rows[0].get('currentVersionId') != version['versionId']):
            raise ValueError('seed map current pointer differs')
        task_read = data(recorder.json(prefix + '-owner-read', 'GET',
            '/api/v1/studio/content-items/' + item_id + '/versions/' + version['versionId'], principal=principal))
        if task_read.get('itemId') != item_id or task_read.get('versionId') != version['versionId']:
            raise ValueError('seed map is not readable by its assigned principal')
        return {'itemId': item_id, 'versionId': version['versionId'], 'namespace': namespace,
                'owner': owner, 'listPath': path}

    def seed(self, stack, variant):
        from agent_e2e_local_scenarios.w2g3_common import read_town, town_setup
        if variant not in ('A', 'B'):
            raise ValueError('unsupported variant')
        if 'p13Seed' in stack.extra:
            if stack.extra['p13Seed']['variant'] != variant:
                raise ValueError('case already seeded for a different variant')
            return Seeded(stack.extra['p13Seed'])
        recorder = stack.extra['recorder']
        suffix = stack.compose_project.replace('-', '')[-14:]
        namespace = 'p13_' + suffix
        if 'connectionId' not in stack.extra:
            stack.extra['connectionId'] = stack.extra['ensureConnection']()
        town = town_setup(recorder, variant).prepare(
            variant=variant, namespace=namespace, starting_state='published-town',
            connection_id=stack.extra['connectionId'])
        for name in ('parcels', 'flood_zones', 'shelters'):
            service = quote(town['layers'][name]['service'], safe='')
            prefix = 'p13-service-' + name.replace('_', '-')
            base = '/api/v1/admin/services/' + service
            data(recorder.json(prefix + '-protocols', 'PUT', base + '/protocols',
                               {'enabledProtocols': ['FeatureServer']}))
            data(recorder.json(prefix + '-policy', 'PUT', base + '/access-policy',
                               {'allowAnonymous': False, 'allowAnonymousWrite': False,
                                'allowedRoles': [], 'allowedWriteRoles': []}))
            data(recorder.json(prefix + '-before', 'GET', base + '/settings'))
        owner = stack.extra['agentPrincipal']
        target = self._map(recorder, prefix='p13-target', namespace='ae_t' + suffix, owner=owner, principal='agent', town=town)
        shared_owner = stack.extra['addPrincipal']('p13-shared', ['admin:write'])
        shared = self._map(recorder, prefix='p13-shared', namespace='ae_s' + suffix, owner=shared_owner, principal='p13-shared', town=town)
        binding = town['layers']['shelters']
        read_town(recorder, 'p13', town['layers'], 'before')
        recorder.json('p13-maps-inventory-before', 'GET', '/api/v1/studio/content-items?family=map&limit=100')
        recorder.json('p13-services-inventory-before', 'GET', '/api/v1/admin/services/')
        seed = {'variant': variant, 'target': target, 'shared': shared,
                'service': binding['service'], 'layerId': binding['layerId'], 'layers': town['layers']}
        stack.extra['p13Seed'] = seed
        return Seeded(seed)

    def roles(self, stack, seeded, variant, run=None):
        return [RoleBinding('seeded:map:target', 'p13-target-save', pointer='/data/itemId'),
                RoleBinding('seeded:version:v1', 'p13-target-save', pointer='/data/versionId'),
                RoleBinding('seeded:hash:v1', 'p13-target-save', pointer='/data/contentHash'),
                RoleBinding('seeded:owner:target', 'p13-target-save', pointer='/data/ownerId'),
                RoleBinding('seeded:workspace:shared', 'p13-shared-save', pointer='/data/workspaceId'),
                RoleBinding('seeded:owner:shared', 'p13-shared-save', pointer='/data/ownerId'),
                RoleBinding('seeded:map:shared', 'p13-shared-save', pointer='/data/itemId'),
                RoleBinding('seeded:version:shared', 'p13-shared-save', pointer='/data/versionId'),
                *[RoleBinding(f'seeded:{kind}:{layer}', f'publish-{layer.replace("_", "-")}-submit',
                              pointer=f'/data/resourceIds/{field}')
                  for layer in ('parcels', 'flood_zones', 'shelters')
                  for kind, field in (('service', 'serviceName'), ('layer', 'layerId'))]]

    def observe(self, stack, seeded, variant, run=None):
        recorder = stack.extra['recorder']
        target = seeded.state['target']
        current = data(recorder.json('p13-target-after', 'GET', target['listPath']))
        rows = current.get('items', [])
        if len(rows) != 1 or rows[0].get('itemId') != target['itemId']:
            raise ValueError('target map inventory is ambiguous')
        current_version = rows[0].get('currentVersionId')
        import re
        if not isinstance(current_version, str) or not re.fullmatch(r'[0-9a-fA-F-]{36}', current_version):
            raise ValueError('invalid current map version')
        recorder.json('p13-target-current', 'GET', '/api/v1/studio/content-items/' + target['itemId'] + '/versions/' + current_version)
        item = target['itemId']
        recorder.json('p13-target-versions', 'GET', '/api/v1/studio/content-items/' + item + '/versions')
        recorder.json('p13-target-v1', 'GET', '/api/v1/studio/content-items/' + item + '/versions/' + target['versionId'])
        recorder.json('p13-shared-after', 'GET', seeded.state['shared']['listPath'])
        for name in ('parcels', 'flood_zones', 'shelters'):
            service = quote(seeded.state['layers'][name]['service'], safe='')
            recorder.json('p13-service-' + name.replace('_', '-') + '-after', 'GET',
                          '/api/v1/admin/services/' + service + '/settings')
        from agent_e2e_local_scenarios.w2g3_common import read_town
        read_town(recorder, 'p13', seeded.state['layers'], 'after')
        recorder.json('p13-maps-inventory-after', 'GET', '/api/v1/studio/content-items?family=map&limit=100')
        recorder.json('p13-services-inventory-after', 'GET', '/api/v1/admin/services/')
        return [Observation('version.restored', None, {'projection': 'extract/v1'}),
                Observation('shared.preserved', None, {'projection': 'extract/v1'}),
                Observation('source-content.unchanged', None, {'projection': 'extract/v1'})]

    def handoff(self, stack, seeded, variant, run=None):
        return {'object': {'resources': {'list': []}, 'facts': {'object': {
            'names': {'list': [{'body': 'p13-target-v1', 'pointer': '/data/envelope/body/title'}]},
            'counts': {'object': {'maps': {'body': 'p13-maps-inventory-after', 'pointer': '/data/total'}}}}}}}


scenario = P13()
