"""Private sharing on the candidate's published route, with a non-admin reader.

The controller seeds a saved map, never a publication. The model chooses the
publication route; a sealed action is only a locator. Fresh authenticated and
anonymous reads must corroborate the exact seeded version. Tenant isolation and
worker replacement retain the runner's preregistered exclusions. Source reads
expect typed fixture attributes; this case depends on honua-server #6815.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from agent_e2e_local_api import Observation, Scenario, Seeded
from agent_e2e_local_expectations import load_roles
from agent_e2e_p10_observer import source_path
from agent_e2e_local_scenarios.w2g3_common import town_setup
from agent_e2e_scenario_operations import data
from agent_e2e_studio_observer import guid, route_path

ROOT = Path(__file__).resolve().parents[2]
TOOL = 'honua_studio_propose_publication'


class P15(Scenario):
    id = 'P15'
    supported_variants = ('A', 'B')

    def agent_permissions(self, variant):
        # Sharing is the task here; approval separation is evaluated by P14/P17.
        return ['admin:*']

    def seed(self, stack, variant):
        self.expected(variant)
        cache = stack.extra.get('p15Seed')
        if cache is not None:
            if cache['variant'] != variant:
                raise ValueError('case already seeded for a different variant')
            return Seeded(copy.deepcopy(cache))
        if stack.extra.get('p15SeedStarted'):
            raise ValueError('incomplete seed; reconcile the original case without retry')
        stack.extra['p15SeedStarted'] = True
        rec = stack.extra['recorder']
        connection = stack.extra.get('connectionId')
        if connection is None:
            connection = stack.extra['ensureConnection']()
            stack.extra['connectionId'] = connection
        namespace = 'p15_' + stack.compose_project.replace('-', '')[-16:]
        town = town_setup(rec, variant).prepare(
            variant=variant, namespace=namespace, starting_state='published-town', connection_id=connection)
        stack.extra['addPrincipal']('operator', ['read:*'])
        body = json.loads((ROOT / 'certification/terminal-journey/targets/local-docker.json').read_text())['execution']['mapBody']
        body['mapPackageId'] = ('kestrel' if variant == 'A' else 'willow') + '-map'
        body['sourceBindings'] = []
        body['mapSpec']['sources'] = {}
        body['mapSpec']['layers'] = []
        for name, binding in [('shelters', town['layers']['shelters'])]:
            url = '/ogc/features/collections/' + str(binding['layerId']) + '/items'
            body['sourceBindings'].append({'sourceId': name, 'protocol': 'ogc_features',
                                          'locator': {'collectionId': str(binding['layerId'])}})
            body['mapSpec']['sources'][name] = {'type': 'geojson', 'data': url}
            body['mapSpec']['layers'].append({'id': name, 'source': name,
                'type': 'circle' if name == 'shelters' else 'fill',
                'paint': {'circle-color': '#ef2020'} if name == 'shelters' else {'fill-color': '#777777'}})
        draft = data(rec.json('p15-map-draft', 'POST', '/api/v1/studio/package-drafts', {
            'packageKey': body['mapPackageId'], 'workspaceId': namespace,
            'ownerId': rec.principals['agent'][1],
            'envelope': {'family': 'map', 'schemaVersion': '1.0', 'format': 'honua_map_package.v1', 'body': body}
        }), (201,))
        draft_id = guid(draft['draftId'])
        saved = data(rec.json('p15-map-saved', 'POST',
            '/api/v1/studio/package-drafts/' + draft_id + '/content-versions', {}), (201,))
        seed = {'variant': variant, 'itemId': guid(saved['itemId']), 'versionId': guid(saved['versionId']),
                'contentHash': saved['contentHash'], 'shelters': town['layers']['shelters']}
        rec.json('p15-source-before', 'GET', source_path(seed['shelters']))
        stack.extra['p15Seed'] = copy.deepcopy(seed)
        return Seeded(seed)

    def roles(self, stack, seeded, variant, run=None):
        return load_roles(self.id)

    def observe(self, stack, seeded, variant, run=None):
        rec, state = stack.extra['recorder'], seeded.state
        if state['variant'] != variant:
            raise ValueError('variant differs from original seed')
        rec.json('p15-source-after', 'GET', source_path(state['shelters']))
        rec.json('p15-bootstrap-probe', 'GET', '/api/v1/admin/api-keys/', principal='bootstrap')
        operator_id = rec.principals['operator'][1]
        grants = data(rec.json('p15-operator-grants', 'GET',
            '/api/v1/admin/api-keys/' + operator_id + '/effective-permissions'))
        if (grants.get('id') != operator_id or grants.get('permissions') != ['read:*']
                or grants.get('status') != 'active' or grants.get('canAuthenticate') is not True):
            raise ValueError('operator is not an active, independent, non-admin read-only principal')
        selected = [a for a in rec.actions() if a.kind == 'tool_call' and a.tool == TOOL
                    and isinstance(a.arguments, dict)
                    and all(a.arguments.get(k) == state[k] for k in ('itemId', 'versionId', 'contentHash'))]
        if selected:
            # No publication mutation is replayed by the controller.
            try:
                path = route_path(selected[-1].arguments.get('route'))
            except ValueError:
                # The model selected an invalid path. Leave the publication reads
                # absent so that assertion fails, while independent evidence is scored.
                return [Observation(a, None, {'projection': 'extract/v1'}) for a in self.expected(variant)]
            answer = rec.json('p15-operator', 'GET', path, principal='operator')
            if answer['observation']['httpStatus'] == 200:
                artifact = data(answer)
                if (any(artifact.get(k) != state[k] for k in ('itemId', 'versionId', 'contentHash'))
                        or artifact.get('visibility') != 'team'):
                    raise ValueError('published route is not the exact team-visible saved version')
            rec.json('p15-anonymous', 'GET', path, principal='anonymous')
        return [Observation(a, None, {'projection': 'extract/v1'}) for a in self.expected(variant)]

    def handoff(self, stack, seeded, variant, run=None):
        # Status words unavailable in a native response are not invented. The
        # final-answer checker can still check names/counts and fail unsupported claims.
        if 'p15-operator' not in stack.extra['recorder'].records:
            return None
        return {'object': {'resources': {'list': []}, 'facts': {'object': {
            'names': {'list': [{'body': 'p15-operator', 'pointer': '/data/packageKey'}]}}}}}


scenario = P15()
