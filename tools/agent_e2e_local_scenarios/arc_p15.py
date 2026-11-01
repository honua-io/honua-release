"""ARC P15 uses the prior agent-published service, never a controller-published service."""
from __future__ import annotations

import json
import re
from urllib.parse import quote

from agent_e2e_local_api import Seeded
from agent_e2e_local_expectations import load_roles
from agent_e2e_local_scenarios.P15 import P15, ROOT
from agent_e2e_p10_observer import source_path
from agent_e2e_scenario_operations import data
from agent_e2e_studio_observer import guid


class ArcP15(P15):
    expectation_profile = 'arc'

    def source_path(self, binding):
        return source_path(binding).replace('outFields=%2A', 'outFields=source_id%2Cname%2Ccapacity%2Cis_open')

    def roles(self, stack, seeded, variant, run=None):
        return load_roles('P15', 'arc')

    def seed(self, stack, variant):
        shared = stack.extra.get('arcSharedService')
        if (not isinstance(shared, dict) or shared.get('variant') != variant
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', str(shared.get('service', '')))
                or type(shared.get('layerId')) is not int or shared['layerId'] < 0
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', str(shared.get('caseReceiptSha256', '')))):
            raise ValueError('prior agent publication evidence is missing or invalid')
        rec = stack.extra['recorder']
        binding = {'service': shared['service'], 'layerId': shared['layerId']}
        meta = rec.json('arc-p15-service-meta', 'GET', '/rest/services/' + quote(binding['service'], safe='') + '/FeatureServer?f=json')
        body = meta['observation']['body']
        layers = body.get('layers')
        if (body.get('serviceName') != binding['service'] or not isinstance(layers, list) or len(layers) != 1
                or not isinstance(layers[0], dict) or layers[0].get('id') != binding['layerId']):
            raise ValueError('prior agent publication is not live at the recorded service and layer')
        stack.extra['addPrincipal']('operator', ['read:*'])
        namespace = 'p15_' + stack.compose_project.replace('-', '')[-16:]
        body = json.loads((ROOT / 'certification/terminal-journey/targets/local-docker.json').read_text())['execution']['mapBody']
        body['mapPackageId'] = ('kestrel' if variant == 'A' else 'willow') + '-map'
        url = '/ogc/features/collections/' + str(binding['layerId']) + '/items'
        body['sourceBindings'] = [{'sourceId': 'shelters', 'protocol': 'ogc_features',
                                   'locator': {'collectionId': str(binding['layerId'])}}]
        body['mapSpec']['sources'] = {'shelters': {'type': 'geojson', 'data': url}}
        body['mapSpec']['layers'] = [{'id': 'shelters', 'source': 'shelters', 'type': 'circle',
                                     'paint': {'circle-color': '#ef2020'}}]
        draft = data(rec.json('p15-map-draft', 'POST', '/api/v1/studio/package-drafts', {
            'packageKey': body['mapPackageId'], 'workspaceId': namespace, 'ownerId': rec.principals['agent'][1],
            'envelope': {'family': 'map', 'schemaVersion': '1.0', 'format': 'honua_map_package.v1', 'body': body}
        }), (201,))
        saved = data(rec.json('p15-map-saved', 'POST', '/api/v1/studio/package-drafts/' + guid(draft['draftId']) +
                              '/content-versions', {}), (201,))
        state = {'variant': variant, 'itemId': guid(saved['itemId']), 'versionId': guid(saved['versionId']),
                 'contentHash': saved['contentHash'], 'shelters': binding,
                 'priorCaseReceiptSha256': shared['caseReceiptSha256']}
        rec.json('p15-source-before', 'GET', self.source_path(binding))
        return Seeded(state)


scenario = ArcP15()
