"""P22 resume handoff: partial saved-map fixture, not yet scored.

Enabling requires independently verified session-2 reuse, publication and source
observations, frozen expectations and a final-answer handoff.
"""
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from agent_e2e_local_api import Observation, Scenario, Seeded
from agent_e2e_scenario_operations import data

ROOT = Path(__file__).resolve().parents[2]


class P22(Scenario):
    id = 'P22'
    supported_variants = ()

    def sessions_required(self):
        return 2

    def needs_approver(self):
        return True

    def seed(self, stack, variant):
        from agent_e2e_scenario_backend import TownSetup
        from agent_e2e_p10_observer import source_path
        if variant not in ('A', 'B'):
            raise ValueError('unsupported P22 variant')
        if 'p22Seed' in stack.extra:
            if stack.extra['p22Seed']['variant'] != variant:
                raise ValueError('case already seeded for a different variant')
            return Seeded(stack.extra['p22Seed'])
        recorder = stack.extra['recorder']
        owner = stack.extra['agentPrincipal']
        namespace = 'p22_' + stack.compose_project.replace('-', '')[-16:]
        connection = stack.extra['ensureConnection']()
        town = TownSetup(recorder, ROOT / 'certification/agent-e2e/corpus/manifest.v1.json').prepare(
            variant=variant, namespace=namespace, starting_state='published-town', connection_id=connection)
        layer = town['layers']['shelters']
        recorder.json('p22-source-before', 'GET', source_path(layer))
        item_id = str(uuid5(NAMESPACE_URL, stack.compose_project + ':p22:map'))
        envelope = {'family': 'map', 'schemaVersion': '1.0', 'format': 'honua_map_package.v1',
                    'bindings': [], 'dependencies': [], 'provenance': [],
                    'body': {'title': 'Kestrel map' if variant == 'A' else 'Willow map',
                             'layers': [{'id': layer['service']}]}}
        draft = data(recorder.json('p22-draft-create', 'POST', '/api/v1/studio/package-drafts',
            {'itemId': item_id, 'packageKey': namespace, 'workspaceId': namespace,
             'ownerId': owner, 'envelope': envelope}, principal='agent'), (201,))
        draft_id = draft.get('draftId')
        if not draft_id or draft.get('ownerId') != owner or draft.get('itemId') != item_id:
            raise ValueError('saved map draft ownership differs')
        validation = data(recorder.json('p22-draft-validate', 'POST',
            '/api/v1/studio/package-drafts/' + draft_id + '/validate', {}, principal='agent'))
        if validation.get('status') != 'valid' or validation.get('diagnostics') != []:
            raise ValueError('saved map draft did not validate')
        saved = data(recorder.json('p22-draft-save', 'POST',
            '/api/v1/studio/package-drafts/' + draft_id + '/content-versions',
            {'changeNote': 'Initial case fixture'}, principal='agent'), (201,))
        version_id = saved.get('versionId')
        if not version_id or saved.get('itemId') != item_id or saved.get('ownerId') != owner:
            raise ValueError('saved map version ownership differs')
        read = data(recorder.json('p22-version-before', 'GET',
            '/api/v1/studio/content-items/' + item_id + '/versions/' + version_id))
        if read != saved:
            raise ValueError('saved map version readback differs')
        state = {'variant': variant, 'owner': owner, 'itemId': item_id, 'draftId': draft_id,
                 'versionId': version_id, 'namespace': namespace,
                 'shared': {'service': layer['service'], 'layerId': layer['layerId']}}
        stack.extra['p22Seed'] = state
        return Seeded(state)

    def observe(self, stack, seeded, variant, run=None):
        from agent_e2e_p10_observer import source_path
        if (run is None or run.sessions != 2 or run.agent_principal != seeded.state['owner']
                or variant != seeded.state['variant']):
            raise ValueError('P22 requires the seeded owner and two sessions')
        recorder = stack.extra['recorder']
        recorder.json('p22-version-after', 'GET', '/api/v1/studio/content-items/' +
                      seeded.state['itemId'] + '/versions/' + seeded.state['versionId'])
        recorder.json('p22-source-after', 'GET', source_path(seeded.state['shared']))
        return [Observation(name, None, {'projection': 'extract/v1'}) for name in
                ('session.fresh', 'state.reused', 'duplicates.absent',
                 'publication.public', 'source-content.unchanged')]


    def expected(self, variant):
        raise NotImplementedError('P22 has no complete independent expectations')


scenario = P22()
