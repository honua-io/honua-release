"""P13 is unsupported pending an assertion-specific restore/history projection.

The seed creates two separately owned maps and the published town. The runner
must verify a post-seed shelter style change and restoration to the seeded v1,
plus complete shared-map, service and source snapshots. A final v1 pointer alone
would also accept a no-op. The current runner exposes no assertion projection
registry hook for those checks.
"""
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

from agent_e2e_local_api import Observation, RoleBinding, Scenario, Seeded
from agent_e2e_scenario_operations import data

ROOT = Path(__file__).resolve().parents[2]


class P13(Scenario):
    id = 'P13'
    supported_variants = ()

    @staticmethod
    def _map(recorder, *, prefix, namespace, owner, principal, town):
        item_id = str(uuid4())
        layers = [{'id': name, 'sourceId': str(town['layers'][name]['service']), 'visible': True}
                  for name in ('parcels', 'flood_zones', 'shelters')]
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
        from agent_e2e_p10_observer import source_path
        from agent_e2e_scenario_backend import TownSetup
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
        town = TownSetup(recorder, ROOT / 'certification/agent-e2e/corpus/manifest.v1.json').prepare(
            variant=variant, namespace=namespace, starting_state='published-town',
            connection_id=stack.extra['connectionId'])
        owner = stack.extra['agentPrincipal']
        target = self._map(recorder, prefix='p13-target', namespace='ae_t' + suffix, owner=owner, principal='agent', town=town)
        shared_owner = stack.extra['addPrincipal']('p13-shared', ['admin:write'])
        shared = self._map(recorder, prefix='p13-shared', namespace='ae_s' + suffix, owner=shared_owner, principal='p13-shared', town=town)
        binding = town['layers']['shelters']
        recorder.json('p13-shelters-before', 'GET', source_path(binding))
        seed = {'variant': variant, 'target': target, 'shared': shared,
                'service': binding['service'], 'layerId': binding['layerId']}
        stack.extra['p13Seed'] = seed
        return Seeded(seed)

    def roles(self, stack, seeded, variant, run=None):
        return [RoleBinding('seeded:map:target', 'p13-target-save', pointer='/data/itemId'),
                RoleBinding('seeded:version:v1', 'p13-target-save', pointer='/data/versionId'),
                RoleBinding('seeded:hash:v1', 'p13-target-save', pointer='/data/contentHash')]

    def observe(self, stack, seeded, variant, run=None):
        from agent_e2e_p10_observer import source_path
        recorder = stack.extra['recorder']
        target = seeded.state['target']
        recorder.json('p13-target-after', 'GET', target['listPath'])
        item = target['itemId']
        recorder.json('p13-target-versions', 'GET', '/api/v1/studio/content-items/' + item + '/versions')
        recorder.json('p13-target-v1', 'GET', '/api/v1/studio/content-items/' + item + '/versions/' + target['versionId'])
        recorder.json('p13-shared-after', 'GET', seeded.state['shared']['listPath'])
        recorder.json('p13-shelters-after', 'GET', source_path(seeded.state))
        return [Observation('source-content.unchanged', None, {'projection': 'extract/v1', 'spec': {
            'protectedContent': [{'label': 'shelters', 'before': 'p13-shelters-before',
                                  'after': 'p13-shelters-after'}]}})]


scenario = P13()
