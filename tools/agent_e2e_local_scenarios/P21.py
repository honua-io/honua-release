"""P21 post-commit recovery: partial fixture and independent readback.

The case remains disabled until recovery, duplicates, publication and source
facts have complete frozen projections and fixture-derived expectations.
"""
from pathlib import Path
from urllib.parse import quote

from agent_e2e_local_api import Observation, Scenario, Seeded
from agent_e2e_scenario_operations import data

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = '/api/v1/studio/package-drafts?family=map&limit=200'


class P21(Scenario):
    id = 'P21'
    supported_variants = ()

    def seed(self, stack, variant):
        from agent_e2e_scenario_backend import TownSetup
        from agent_e2e_p10_observer import source_path
        if variant not in ('A', 'B'):
            raise ValueError('unsupported P21 variant')
        if 'p21Seed' in stack.extra:
            if stack.extra['p21Seed']['variant'] != variant:
                raise ValueError('case already seeded for a different variant')
            return Seeded(stack.extra['p21Seed'])
        recorder = stack.extra['recorder']
        namespace = 'p21_' + stack.compose_project.replace('-', '')[-16:]
        connection = stack.extra['ensureConnection']()
        town = TownSetup(recorder, ROOT / 'certification/agent-e2e/corpus/manifest.v1.json').prepare(
            variant=variant, namespace=namespace, starting_state='published-town', connection_id=connection)
        layer = town['layers']['shelters']
        recorder.json('p21-source-before', 'GET', source_path(layer))
        recorder.json('p21-drafts-before', 'GET', INVENTORY, principal='agent')
        state = {'variant': variant, 'owner': stack.extra['agentPrincipal'],
                 'shared': {'service': layer['service'], 'layerId': layer['layerId']}}
        stack.extra['p21Seed'] = state
        return Seeded(state)

    def fault_plan(self, variant):
        if variant not in ('A', 'B'):
            raise ValueError('unsupported variant')
        return {'kind': 'response_lost_after_commit', 'id': 'p21-map-create',
                'after': None, 'match': {'tool': 'honua_studio_create_draft'},
                'commit': {'pointer': '/result/structuredContent/draftId'}}

    def needs_approver(self):
        return True

    def observe(self, stack, seeded, variant, run=None):
        from agent_e2e_p10_observer import source_path
        if run is None or variant != seeded.state['variant'] or run.agent_principal != seeded.state['owner']:
            raise ValueError('P21 run differs from seeded case')
        events = [event for event in run.fault_events if event.get('id') == 'p21-map-create']
        if (len(events) != 1 or events[0].get('kind') != 'response_lost_after_commit'
                or events[0].get('tool') != 'honua_studio_create_draft'
                or events[0].get('forwarded') is not True or events[0].get('httpStatus') != 503
                or events[0].get('upstreamOutcome') != 'ok'
                or type(events[0].get('upstreamStatus')) is not int
                or not 200 <= events[0]['upstreamStatus'] < 300
                or not events[0].get('upstreamResponseSha256') or not events[0].get('committedResourceId')):
            raise ValueError('P21 needs one confirmed post-commit fault')
        draft_id = str(events[0]['committedResourceId'])
        recorder = stack.extra['recorder']
        draft = data(recorder.json('p21-committed-draft', 'GET',
                     '/api/v1/studio/package-drafts/' + quote(draft_id, safe='')))
        if draft.get('draftId') != draft_id or draft.get('ownerId') != run.agent_principal:
            raise ValueError('faulted draft is absent or belongs to another owner')
        recorder.json('p21-drafts-after', 'GET', INVENTORY, principal='agent')
        recorder.json('p21-source-after', 'GET', source_path(seeded.state['shared']))
        return [Observation(name, None, {'projection': 'extract/v1'}) for name in
                ('fault.observed', 'recovery.completed', 'duplicates.absent',
                 'publication.public', 'source-content.unchanged')]

    def expected(self, variant):
        raise NotImplementedError('P21 has no complete independent expectations')


scenario = P21()
