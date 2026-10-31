"""Save and reopen a seeded town map across a runner-observed restart.

The expected map and source contents come from the seeded town fixture. The
agent's save and subsequent read are bound to sealed actions and fresh reads.
Typed source attributes depend on honua-server #6815.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from agent_e2e_local_api import Observation, Scenario, Seeded
from agent_e2e_scenario_backend import TownSetup
from agent_e2e_scenario_operations import data, publication_request, submit_publication
from agent_e2e_p10_observer import source_path
from agent_e2e_studio_observer import guid

ROOT = Path(__file__).resolve().parents[2]
ITEMS = '/api/v1/studio/content-items?limit=200'


class P12(Scenario):
    id = 'P12'
    supported_variants = ('A', 'B')

    def sessions_required(self):
        return 2

    def seed(self, stack, variant):
        if variant not in self.supported_variants:
            raise ValueError('unsupported development variant')
        if 'p12Seed' in stack.extra:
            seed = stack.extra['p12Seed']
            if seed['variant'] != variant:
                raise ValueError('case already seeded for a different variant')
            return Seeded(copy.deepcopy(seed))
        if stack.extra.get('p12SeedStarted'):
            raise RuntimeError('P12 seed interrupted; do not repeat mutations')
        stack.extra['p12SeedStarted'] = True
        rec = stack.extra['recorder']
        namespace = 'p12_' + stack.compose_project.replace('-', '')[-16:]
        town = TownSetup(rec, ROOT / 'certification/agent-e2e/corpus/manifest.v1.json').prepare(
            variant=variant, namespace=namespace, starting_state='imported-town')
        credentials = stack.extra['databaseHandoff']('p12', 'honua_data')
        connection = data(rec.json('p12-connection-create', 'POST', '/api/v1/admin/connections/', {
            'name': 'p12-source-' + stack.compose_project[-12:], 'host': credentials['host'],
            'port': credentials['port'], 'databaseName': credentials['database'],
            'username': credentials['username'], 'password': credentials['password'],
            'sslRequired': False, 'sslMode': 'Disable'}), (201,))['connectionId']
        binding = town['layers']['shelters']
        service = namespace + '-shelters'
        published = submit_publication(rec, publication_request(connection=connection,
            schema=binding['schema'], table=binding['table'], service=service,
            geometry_type='Point', srid=3857), step='publish-shelters')
        if published['status'] != 'completed':
            raise RuntimeError('fixture publication was not completed')
        binding.update(service=service, layerId=published['layerId'])
        rec.json('p12-source-before', 'GET', source_path(binding))
        rec.json('p12-items-before', 'GET', ITEMS)
        map_body = json.loads((ROOT / 'certification/terminal-journey/targets/local-docker.json').read_text())['execution']['mapBody']
        map_body['mapPackageId'] = ('kestrel' if variant == 'A' else 'willow') + '-map'
        map_body['sourceBindings'] = [{'sourceId': 'shelters', 'protocol': 'ogc_features',
                                       'locator': {'collectionId': str(binding['layerId'])}}]
        map_body['mapSpec']['sources'] = {'shelters': {'type': 'geojson',
            'data': '/ogc/features/collections/' + str(binding['layerId']) + '/items'}}
        map_body['mapSpec']['layers'] = [{'id': 'shelters', 'source': 'shelters', 'type': 'circle',
                                         'paint': {'circle-color': '#ef2020'}}]
        draft = data(rec.json('p12-seed-draft', 'POST', '/api/v1/studio/package-drafts', {
            'packageKey': map_body['mapPackageId'], 'workspaceId': namespace,
            'ownerId': rec.principals['agent'][1],
            'envelope': {'family': 'map', 'schemaVersion': '1.0', 'format': 'honua_map_package.v1', 'body': map_body}
        }), (201,))
        if draft.get('envelope', {}).get('body') != map_body:
            raise ValueError('seed draft does not match the reviewed map fixture')
        draft_id = guid(draft['draftId'])
        saved = data(rec.json('p12-seed-save', 'POST', '/api/v1/studio/package-drafts/' + draft_id + '/content-versions', {}), (201,))
        item_id, version_id = guid(saved['itemId']), guid(saved['versionId'])
        initial = data(rec.json('p12-seed-version', 'GET', '/api/v1/studio/content-items/' + item_id + '/versions/' + version_id))
        if initial.get('itemId') != item_id or initial.get('versionId') != version_id or initial.get('envelope', {}).get('body') != map_body:
            raise ValueError('seeded version does not match the reviewed map fixture')
        seed = {'variant': variant, 'itemId': item_id, 'draftId': draft_id, 'initialVersionId': version_id,
                'shelters': binding}
        stack.extra['p12Seed'] = copy.deepcopy(seed)
        return Seeded(copy.deepcopy(seed))

    def between_sessions(self, stack, seeded, variant, after_session):
        if after_session != 1 or variant != seeded.state['variant']:
            raise ValueError('invalid P12 between-session state')
        rec = stack.extra['recorder']
        listing = data(rec.json('p12-items-between', 'GET', ITEMS))
        rows = listing['items']
        if listing['total'] != len(rows):
            raise ValueError('content inventory is incomplete')
        matches = [r for r in rows if r.get('itemId') == seeded.state['itemId']]
        if len(matches) != 1:
            raise ValueError('seeded item missing or ambiguous')
        version = guid(matches[0]['currentVersionId'])
        if version == seeded.state['initialVersionId']:
            raise ValueError('no new saved version observed')
        seeded.state['savedVersionId'] = version
        rec.json('p12-version-between', 'GET', '/api/v1/studio/content-items/' + seeded.state['itemId'] + '/versions/' + version)
        return {'restartServer': True}

    def observe(self, stack, seeded, variant, run=None):
        if variant != seeded.state['variant']:
            raise ValueError('seed variant differs')
        rec, state = stack.extra['recorder'], seeded.state
        rec.json('p12-source-after', 'GET', source_path(state['shelters']))
        rec.json('p12-version-after', 'GET', '/api/v1/studio/content-items/' + state['itemId'] + '/versions/' + state['savedVersionId'])
        saves = [a for a in rec.actions() if a.session == 1 and a.kind == 'tool_call'
                 and a.tool == 'honua_studio_save_version' and isinstance(a.arguments, dict)
                 and a.arguments.get('draftId') == state['draftId']]
        reads = [a for a in rec.actions() if a.session == 2 and a.kind == 'tool_call'
                 and a.tool == 'honua_studio_get_version' and isinstance(a.arguments, dict)
                 and a.arguments.get('itemId') == state['itemId'] and a.arguments.get('versionId') == state['savedVersionId']]
        if saves:
            rec.action_result('p12-save-action', saves[-1])
        if reads:
            rec.replay('p12-reopen-read', reads[-1])
        return [Observation(a, None, {'projection': 'extract/v1'}) for a in self.expected(variant)]

    def handoff(self, stack, seeded, variant, run=None):
        return {'object': {'resources': {'list': []}, 'facts': {'object': {
            'names': {'list': [{'body': 'p12-version-after', 'pointer': '/data/packageKey'}]}}}}}


scenario = P12()
